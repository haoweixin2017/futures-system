"""A separate serial worker for owner-scoped, bounded public research.

每个主题按固定顺序执行：固定来源检查 → 固定关键词搜索 → 按条件触发的 AI 辅助。
固定与规则阶段不消耗模型调用；AI 关闭或暂停时固定结果照常发布。
"""
import json
import logging
import threading
import uuid
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError

from backend.ai_analysis import credentials, fixed_research, future_trend, provider, registry
from backend.ai_analysis import research_cache as cache, web_research
from backend.ai_analysis.fixed_research import Budget
from backend.models.database import AIResearchJobModel, AIResearchSettingsModel
from backend.storage.mysql_client import SessionLocal

logger = logging.getLogger(__name__)
_now = cache._now
_stop = threading.Event()
_thread = None
_execution_lock = threading.Lock()


def _model(owner, model_id):
    state = cache.model_status(owner, model_id)
    if not state['available']:
        raise ValueError(state['reason'])
    runtime = registry.model_runtime_config(owner, model_id)
    with SessionLocal() as db:
        row = registry.get_model(db, owner, model_id)
        runtime['credential_ref'] = row.credential_ref
    return registry.public_snapshot(runtime)


def _enqueue(owner, config, model, topic, trigger, due):
    active_key = cache._hash(owner + ':' + topic)
    with SessionLocal() as db:
        # Serialize enqueue with settings saves, then rely on the unique active key.
        current = db.query(AIResearchSettingsModel).filter_by(owner_username=owner).with_for_update().first()
        if not current or current.config_version != config['config_version']:
            return None
        if trigger == 'SCHEDULED' and not current.enabled:
            return None
        job = db.query(AIResearchJobModel).filter_by(active_key=active_key).first()
        if job:
            return cache.job_summary(job)
        job = AIResearchJobModel(job_id=str(uuid.uuid4()), owner_username=owner, topic=topic,
            active_key=active_key, config_version=config['config_version'], config_json=cache._json(config),
            model_json=cache._json(model or {}), trigger=trigger, status='QUEUED', queued_at=due, calls_json='[]')
        db.add(job)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            job = db.query(AIResearchJobModel).filter_by(active_key=active_key).one()
        return cache.job_summary(job)


def enqueue_refresh(owner, instrument=None):
    config = cache.get_settings(owner)
    config['collection_cycle_id'] = str(uuid.uuid4())
    topics = cache.topics_for(config['contracts'], instrument)
    if not topics:
        raise ValueError('请先保存目标合约名单；只能更新已保存的合约组')
    # 固定采集不要求模型；AI 开启但模型不可用时仅 AI 阶段暂停。
    model = None
    if config.get('ai_enabled'):
        try:
            model = _model(owner, config['model_config_id'])
        except (ValueError, LookupError, credentials.CredentialError, provider.ProviderHTTPError):
            model = None
    jobs = [_enqueue(owner, config, model, t, 'MANUAL', _now()) for t in topics]
    return {'jobs': [j for j in jobs if j]}


def schedule_due(now=None):
    now = now or _now()
    with SessionLocal() as db:
        owners = [r.owner_username for r in db.query(AIResearchSettingsModel).filter_by(enabled=True)]
    for owner in owners:
        config = cache.get_settings(owner)
        config['collection_cycle_id'] = str(uuid.uuid4())
        model = None
        if config.get('ai_enabled'):
            try:
                model = _model(owner, config['model_config_id'])
            except (ValueError, LookupError, credentials.CredentialError, provider.ProviderHTTPError):
                model = None
        ai_hash = registry.config_hash(model) if model else None
        with SessionLocal() as db:
            row = db.get(AIResearchSettingsModel, owner)
            if not row:
                continue
            # 暂停原因只作用于 AI 阶段；模型更换或关闭 AI 后清除标记，固定采集从不因此停摆。
            if row.pause_reason and (not config.get('ai_enabled') or
                                     (ai_hash and row.paused_model_hash != ai_hash)):
                row.pause_reason = row.paused_model_hash = None
                db.commit()
            jobs = db.query(AIResearchJobModel).filter_by(owner_username=owner).filter(
                AIResearchJobModel.status != 'CANCELLED').order_by(AIResearchJobModel.queued_at.desc(),
                                                                AIResearchJobModel.id.desc()).all()
            latest = {}
            for job in jobs:
                latest.setdefault(job.topic, job)
            due_topics = []
            for topic in cache.topics_for(config['contracts']):
                job = latest.get(topic)
                if job and job.status in ('QUEUED','RUNNING'):
                    continue
                due = cache.next_due(job, config, ai_hash, now)
                if due <= now:
                    due_topics.append((topic, due))
        for topic, due in due_topics:
            _enqueue(owner, config, model, topic, 'SCHEDULED', due)


def recover_interrupted():
    """Never resume a model call whose process/session has been lost."""
    with SessionLocal() as db:
        for job in db.query(AIResearchJobModel).filter(AIResearchJobModel.status.in_(('QUEUED','RUNNING'))):
            job.status, job.active_key = 'INTERRUPTED', None
            job.completed_at = _now()
            job.next_check_at = job.completed_at
            job.lease_until = job.lease_token = None
            job.error = '服务重启中断，未重发原研究调用'
        db.commit()


def _claim(job_id):
    token = str(uuid.uuid4())
    with SessionLocal() as db:
        job = db.query(AIResearchJobModel).filter_by(job_id=job_id).first()
        if not job or job.status != 'QUEUED':
            return None
        config = db.query(AIResearchSettingsModel).filter_by(owner_username=job.owner_username).with_for_update().first()
        if not config or config.config_version != job.config_version or (job.trigger=='SCHEDULED' and not config.enabled):
            job.status, job.active_key, job.completed_at = 'CANCELLED', None, _now()
            db.commit()
            return None
        claimed = db.query(AIResearchJobModel).filter_by(job_id=job_id, status='QUEUED').update(
            {'status':'RUNNING', 'started_at':_now(), 'lease_token':token,
             'lease_until':_now()+timedelta(seconds=95)}, synchronize_session=False)
        db.commit()
        if not claimed:
            return None
        db.refresh(job)
        return {'job_id':job_id, 'owner':job.owner_username, 'topic':job.topic, 'token':token,
                'config':json.loads(job.config_json), 'model':json.loads(job.model_json or '{}')}


def _write_progress(claimed, result=None, calls=None):
    with SessionLocal() as db:
        values = {}
        if result is not None:
            row = db.query(AIResearchJobModel).filter_by(job_id=claimed['job_id'],
                status='RUNNING', lease_token=claimed['token']).first()
            previous = json.loads(row.result_json or '{}') if row else {}
            # Tool progress must not erase the already committed fixed-stage snapshot.
            values['result_json'] = cache._json(dict(previous, **result))
        if calls is not None:
            values['calls_json'] = cache._json(calls)
        db.query(AIResearchJobModel).filter_by(job_id=claimed['job_id'], status='RUNNING',
            lease_token=claimed['token']).update(values, synchronize_session=False)
        db.commit()


def _previous_derivations(owner, topic):
    """Successful AI extractions of this owner/topic, keyed by reuse_key."""
    found = {}
    with SessionLocal() as db:
        rows = db.query(AIResearchJobModel.result_json).filter_by(owner_username=owner, topic=topic).filter(
            AIResearchJobModel.status.in_(('COMPLETED', 'PARTIAL', 'FAILED'))).order_by(
            AIResearchJobModel.completed_at.desc(), AIResearchJobModel.id.desc()).limit(25).all()
    for (result_json,) in rows:
        for derivation in json.loads(result_json or '{}').get('ai_derivations') or []:
            key = derivation.get('reuse_key')
            if key and derivation.get('status') == 'COMPLETED' and key not in found:
                found[key] = derivation
    return found


def _scan_anchor(owner, topic):
    """Cooldown anchor for gap/event scans, recovered from persisted task audits."""
    with SessionLocal() as db:
        rows = db.query(AIResearchJobModel.result_json).filter_by(owner_username=owner, topic=topic).filter(
            AIResearchJobModel.status.in_(('COMPLETED', 'PARTIAL', 'FAILED', 'INTERRUPTED'))).order_by(
            AIResearchJobModel.completed_at.desc(), AIResearchJobModel.id.desc()).limit(8).all()
    for (result_json,) in rows:
        stage = (json.loads(result_json or '{}').get('research') or {}).get('ai_stage') or {}
        if stage.get('kind') != 'GAP_SCAN' or stage.get('status') not in ('COMPLETED', 'FAILED', 'RUNNING'):
            continue
        anchor = stage.get('completed_at') or stage.get('last_sent_at') or stage.get('started_at')
        if anchor:
            return anchor
    return None


def _ai_pause_state(owner):
    with SessionLocal() as db:
        row = db.get(AIResearchSettingsModel, owner)
        return (row.pause_reason, row.paused_model_hash) if row else (None, None)


def _hosts(rows):
    return {urlsplit(row['url']).hostname for row in rows if row.get('url')}


def _coverage_gaps(rows, allowed, product):
    present = {row.get('dimension') for row in rows}
    missing = [dimension for dimension in allowed if dimension not in present]
    gaps = list(missing)
    if 'fundamentals' in allowed and 'fundamentals' not in gaps and not any(
            web_research._fundamental_detail(row, product) for row in rows
            if row.get('dimension') == 'fundamentals'):
        gaps.append('fundamentals')
    if 'fundamentals' in allowed and 'fundamentals' not in gaps and any(
            item['status'] == 'MISSING' for item in web_research.fundamental_coverage(
                [row for row in rows if row.get('dimension') == 'fundamentals'], product)):
        gaps.append('fundamentals')
    return missing, gaps


def _unread_candidates(rule, published):
    return any(candidate.get('url') not in published for candidate in rule.get('candidates') or [])


def run_job(job_id):
    # Manual test/administrative callers use the same serial execution boundary.
    with _execution_lock:
        claimed = _claim(job_id)
        if not claimed:
            return
        owner, topic = claimed['owner'], claimed['topic']
        config = claimed['config']
        model_snapshot = dict(claimed['model'] or {})
        calls = []
        now_dt = _now()
        job_ref = SimpleNamespace(owner_username=owner, job_id=job_id)
        budget = Budget(model_calls=fixed_research.MAX_MODEL_CALLS if config.get('ai_enabled') else 0)
        product = fixed_research.topic_product(topic)
        allowed = ['macro'] if topic == 'macro' else ['fundamentals', 'events', 'related_markets']
        snapshot = {'owner': owner, 'topic': topic, 'product_code': product,
                    'collection_cycle_id': config.get('collection_cycle_id'),
                    'allowed_dimensions': allowed,
                    'market': {'snapshot_time': cache._iso(now_dt),
                               'instrument': topic.split(':', 1)[-1] if topic != 'macro' else ''},
                    'future_targets': []}
        research = {'status': 'RUNNING', 'topic': topic, 'product_code': product,
                    'dimensions': allowed, 'model_config_id': model_snapshot.get('model_config_id'),
                    'model_display_name': model_snapshot.get('display_name'),
                    'model_name': model_snapshot.get('model_name'), 'provider': model_snapshot.get('provider'),
                    'rounds': 0, 'search_count': 0, 'pages_read': 0, 'events': [],
                    'tool_support': 'UNKNOWN', 'tool_calls_requested': 0, 'pause_required': False,
                    'failure_kind': None, 'reason': None, 'latest_release_checks': [],
                    'fixed_status': None, 'rule_status': None, 'fixed_checks': 0, 'rule_search_count': 0,
                    'index_checks': 0, 'ai_enabled': bool(config.get('ai_enabled')),
                    'ai_status': 'DISABLED' if not config.get('ai_enabled') else None,
                    'ai_pause_reason': None, 'ai_trigger_reason': None,
                    'ai_call_count': 0, 'input_tokens': 0, 'output_tokens': 0,
                    'sources_checked': [], 'ai_stage': None}
        result = {'sources': [], 'background_sources': []}
        material_refs, derivations = [], []
        persisted_keys = set()
        new_count = 0
        error = None
        api_key = None
        ai = {'enabled': bool(config.get('ai_enabled')), 'ready': False, 'paused': False,
              'pause_write': False, 'state': None}

        def persist_rows(rows_to_persist):
            nonlocal new_count
            pending = [row for row in rows_to_persist
                       if (row.get('url'), row.get('content_hash')) not in persisted_keys]
            if not pending:
                return []
            with SessionLocal() as db:
                refs, inserted = cache.persist_sources(db, job_ref, pending, now_dt)
                db.commit()
            persisted_keys.update((row.get('url'), row.get('content_hash')) for row in pending)
            material_refs.extend(refs)
            new_count += inserted
            return refs

        def completion_for(purpose):
            def completion(model, body):
                if not budget.try_consume('model_calls'):
                    raise fixed_research.BudgetExhausted('模型调用次数已达本任务上限')
                audit = {'call_id': str(uuid.uuid4()), 'started_at': cache._iso(_now()),
                         'model_config_id': model['model_config_id'], 'purpose': purpose, 'status': 'RUNNING'}
                calls.append(audit)
                if purpose == 'GAP_SCAN' and research.get('ai_stage'):
                    # 冷却锚点在实际发送模型请求时落库，重启中断也按该时间计算冷却。
                    research['ai_stage']['last_sent_at'] = audit['started_at']
                    _write_progress(claimed, result=dict(result, research=research))
                _write_progress(claimed, calls=calls)
                try:
                    from backend.ai_analysis.orchestrator import _provider_completion
                    reply = _provider_completion(model, body)
                    audit.update(status='SUCCEEDED', input_tokens=reply.input_tokens,
                                 output_tokens=reply.output_tokens, latency_ms=reply.latency_ms)
                    return reply
                except Exception as exc:
                    audit.update(status='FAILED', error=provider.sanitize_error(exc, api_key))
                    raise
                finally:
                    audit['completed_at'] = cache._iso(_now())
                    _write_progress(claimed, calls=calls)
            return completion

        def publish_fixed_stage():
            with SessionLocal() as db:
                current = db.query(AIResearchSettingsModel).filter_by(owner_username=owner).with_for_update().first()
                job = db.query(AIResearchJobModel).filter_by(job_id=job_id, status='RUNNING',
                    lease_token=claimed['token']).with_for_update().first()
                if not job:
                    return
                publishable = bool(current and current.config_version == job.config_version and
                    (job.trigger == 'MANUAL' or current.enabled) and
                    topic in cache.topics_for(json.loads(current.contracts_json)))
                stage = json.loads(cache._json(dict(
                    published_at=cache._iso(_now()), publishable=publishable,
                    material_refs=material_refs, research=research)))
                result.setdefault('fixed_snapshots', []).append(stage)
                job.result_json = cache._json(dict(result, research=research))
                db.commit()

        def run_ai_stage():
            """A/B/C 三类 AI 触发；异常只作用于 AI 阶段，不阻断固定结果发布。"""
            nonlocal api_key
            runtime = dict(model_snapshot)
            runtime['api_key'] = credentials.credential_store.get(
                runtime['credential_ref']) if runtime.get('credential_ref') else None
            if runtime.get('credential_ref') and not runtime['api_key']:
                research['ai_status'] = 'PAUSED_MODEL'
                research['ai_pause_reason'] = '研究模型凭据已移除'
                research['ai_trigger_reason'] = 'AI 阶段暂停；固定与规则阶段照常执行'
                return
            api_key = runtime['api_key']
            runtime['_config_hash'] = registry.config_hash(claimed['model'])
            published = {row['url'] for row in result['sources']}
            existing = []
            group = next((g for g in config['contracts']
                          if cache.extract_product(g['instrument']) == product), None)
            if group:
                existing = [s for s in cache.freeze_materials(owner, group['instrument'], now_dt)['sources']
                            if s.get('is_target_evidence') or s.get('dimension') == 'macro']
            _, gaps = _coverage_gaps(result['sources'] + existing, allowed, product)
            anchor_iso = _scan_anchor(owner, topic)
            anchor = future_trend._timestamp(anchor_iso)
            cooldown = timedelta(minutes=max(60, config['interval_minutes']))
            cooldown_ok = anchor is None or now_dt - anchor >= cooldown
            reserve_scan = bool(gaps and cooldown_ok and
                                (budget.remaining('searches') or budget.remaining('reads')))
            # A. 已实际读取且未被处理过的新复杂正文提取
            extraction_rows = [row for row in fixed_rows + rule_rows
                               if row.get('extractable') and row.get('content_type') == 'BODY']
            reuse_table = _previous_derivations(owner, topic)
            deferred = 0
            for row in extraction_rows:
                ref = next((item for item in material_refs if item.get('url') == row['url']), None)
                if not ref:
                    continue
                reuse_key = web_research.build_reuse_key(
                    owner, topic, row.get('content_hash'), runtime['_config_hash'],
                    web_research.EXTRACTION_PROMPT_VERSION)
                reused = reuse_table.get(reuse_key)
                if reused:
                    derivations.append(dict(reused, reused=True))
                    continue
                if budget.remaining('model_calls') <= (1 if reserve_scan else 0) or (
                        reserve_scan and budget.time_left() <= 20):
                    deferred += 1
                    continue
                extraction_budget = fixed_research.Budget(deadline=budget.deadline - (20 if reserve_scan else 0))
                derivations.append(web_research.extract_document(
                    snapshot, dict(row, material_id=ref['material_id']),
                    runtime, completion_for('EXTRACTION'), extraction_budget))
            # B+C. 缺口补查与事件扫描共用一段 AI 流程和同一冷却时间。
            remaining = {'searches': budget.remaining('searches'),
                         'reads': budget.remaining('reads'),
                         'model_calls': budget.remaining('model_calls'),
                         'seconds': int(max(0, budget.time_left()))}
            triggers = ['事件扫描'] + (['缺口补查'] if gaps else [])
            if not cooldown_ok:
                if gaps:
                    research['ai_status'] = 'SKIPPED_COOLDOWN'
                    research['ai_trigger_reason'] = ('缺口补查冷却中；上次扫描完成 ' +
                                                     str(anchor_iso or '未知'))
                else:
                    research['ai_status'] = 'SKIPPED_NO_TRIGGER'
                    research['ai_trigger_reason'] = '无资料缺口且事件扫描未到期'
            elif remaining['model_calls'] <= 0 or (
                    not remaining['searches'] and not remaining['reads']
                    and not _unread_candidates(rule, published)):
                research['ai_status'] = 'SKIPPED_BUDGET'
                research['ai_trigger_reason'] = '共享预算已耗尽，AI 阶段未启动'
            else:
                research['ai_stage'] = {'kind': 'GAP_SCAN', 'status': 'RUNNING',
                                        'started_at': cache._iso(_now())}
                _write_progress(claimed, result=dict(result, research=research))
                scan_snapshot = dict(snapshot, allowed_dimensions=allowed)
                instruction = web_research.scan_instruction(scan_snapshot, {
                    'existing_coverage': [{'dimension': row.get('dimension'),
                                           'title': row.get('title'), 'url': row.get('url'),
                                           'data_period': row.get('data_period')}
                                          for row in result['sources'] + existing],
                    'pending_candidates': [{'title': c.get('title'), 'url': c.get('url'),
                                            'dimension': c.get('dimension')}
                                           for c in rule.get('candidates') or []],
                    'coverage_gaps': gaps}, remaining, anchor_iso)
                pending_fixed = [s.get('title') or s['source_id'] for s in research['sources_checked']
                                 if s['status'] == 'UNAVAILABLE']
                if pending_fixed:
                    instruction += ('\n固定序列尚未接入：' + '、'.join(pending_fixed) +
                                    '。不要每轮重复寻找这些稳定入口或尝试绕过访问限制；仅补查新发布的可核验资讯，并保留固定序列缺口。')
                run = web_research.run_research(
                    scan_snapshot, runtime, completion_for('GAP_SCAN'),
                    progress=lambda value: _write_progress(claimed, result=dict(result, ai_progress=value, research=research)),
                    time_limit=budget.time_left(), topic=topic,
                    initial_materials={'sources': result['sources'],
                                       'candidates': rule.get('candidates') or []},
                    budget={'deadline': budget.deadline, **remaining},
                    instruction=instruction)
                run_research = run.get('research') or {}
                research['rounds'] += run_research.get('rounds', 0)
                research['search_count'] += run_research.get('search_count', 0)
                research['pages_read'] += run_research.get('pages_read', 0)
                research['tool_calls_requested'] += run_research.get('tool_calls_requested', 0)
                research['events'].extend(run_research.get('events') or [])
                research['latest_release_checks'].extend(run_research.get('latest_release_checks') or [])
                if run_research.get('tool_support') not in (None, 'UNKNOWN'):
                    research['tool_support'] = run_research['tool_support']
                budget.used['searches'] += run_research.get('search_count', 0)
                budget.used['reads'] += run_research.get('pages_read', 0)
                existing_urls = {row['url'] for row in result['sources'] + result['background_sources']}
                new_run_rows = []
                for row in (run.get('sources') or []) + (run.get('background_sources') or []):
                    if row['url'] in existing_urls:
                        continue
                    if row['url'] not in published:
                        row['collection_method'] = 'AI_DISCOVERY'
                    new_run_rows.append(row)
                result['sources'].extend(row for row in new_run_rows if not row['stale'])
                result['background_sources'].extend(row for row in new_run_rows if row['stale'])
                persist_rows(new_run_rows)
                if run_research.get('pause_required') or run_research.get('status') == 'UNAVAILABLE':
                    research['ai_status'] = 'FAILED'
                    research['ai_pause_reason'] = run_research.get('reason')
                    research['ai_trigger_reason'] = run_research.get('reason')
                    research['pause_required'] = True
                    ai['pause_write'] = True
                elif run_research.get('status') == 'TIMEOUT':
                    research['ai_status'] = 'FAILED'
                    research['ai_trigger_reason'] = run_research.get('reason')
                else:
                    research['ai_status'] = 'COMPLETED'
                    research['ai_trigger_reason'] = '、'.join(triggers) + '完成'
                research['ai_stage'] = {'kind': 'GAP_SCAN',
                                        'status': 'COMPLETED' if research['ai_status'] == 'COMPLETED' else 'FAILED',
                                        'started_at': research['ai_stage']['started_at'],
                                        'completed_at': cache._iso(_now()),
                                        'last_sent_at': research['ai_stage'].get('last_sent_at')}
            if deferred:
                research['ai_trigger_reason'] = (research['ai_trigger_reason'] or '') + \
                    f'；另有 {deferred} 篇新正文因调用上限未提取'

        try:
            if ai['enabled']:
                ai['state'] = cache.model_status(owner, config['model_config_id'])
                ai['ready'] = bool(ai['state']['available'])
                pause_reason, paused_hash = _ai_pause_state(owner)
                ai['paused'] = bool(pause_reason and paused_hash
                                    and paused_hash == ai['state'].get('config_hash'))

            # ---- Stage 1: fixed sources (no model) ----
            fixed = fixed_research.collect_fixed(snapshot, topic, budget)
            research['fixed_status'] = fixed['status']
            research['fixed_checks'] = fixed['checks_count']
            research['index_checks'] = budget.used['index_checks']
            research['pages_read'] += fixed.get('reads', 0)
            research['sources_checked'] = fixed['sources_checked']
            research['latest_release_checks'].extend(fixed['latest_release_checks'])
            research['events'].extend(fixed['events'])
            fixed_rows = web_research.normalize_with_provenance(
                fixed['sources'], now_dt, allowed_hosts=_hosts(fixed['sources']))
            result['sources'].extend(row for row in fixed_rows if not row['stale'])
            result['background_sources'].extend(row for row in fixed_rows if row['stale'])
            persist_rows(fixed_rows)
            _write_progress(claimed, result=dict(result, research=research))

            # ---- Stage 2: rule keyword search (no model) ----
            publish_fixed_stage()
            rule = fixed_research.collect_rule_search(snapshot, topic, budget)
            research['rule_status'] = rule['status']
            research['rule_search_count'] = rule['searches']
            research['search_count'] += rule['searches']
            research['pages_read'] += rule.get('reads', 0)
            research['events'].extend(rule['events'])
            rule_rows = web_research.normalize_with_provenance(
                rule['sources'], now_dt, allowed_hosts=_hosts(rule['sources']))
            result['sources'].extend(row for row in rule_rows if not row['stale'])
            result['background_sources'].extend(row for row in rule_rows if row['stale'])
            persist_rows(rule_rows)
            _write_progress(claimed, result=dict(result, research=research))

            # ---- Stage 3: conditional AI assistance ----
            publish_fixed_stage()
            if not ai['enabled']:
                research['ai_trigger_reason'] = 'AI 辅助研究已关闭，固定采集不受影响'
            elif ai['paused']:
                research['ai_status'] = 'PAUSED_MODEL'
                research['ai_pause_reason'] = pause_reason or '研究模型上次失败后尚未变更'
                research['ai_trigger_reason'] = 'AI 阶段暂停；固定与规则阶段照常执行'
            elif not ai['ready']:
                research['ai_status'] = 'PAUSED_MODEL'
                research['ai_pause_reason'] = (ai['state'] or {}).get('reason') or '研究模型不可用'
                research['ai_trigger_reason'] = 'AI 阶段暂停；固定与规则阶段照常执行'
            else:
                try:
                    run_ai_stage()
                except Exception as exc:
                    message = provider.sanitize_error(exc, api_key)
                    research['ai_status'] = 'FAILED'
                    research['ai_trigger_reason'] = message
                    if research.get('ai_stage'):
                        research['ai_stage']['status'] = 'FAILED'
        except Exception as exc:
            error = provider.sanitize_error(exc, api_key)
            logger.warning('Research job %s failed: %s', job_id, error)
        completed = _now()
        had_rows = bool(result['sources'] or result['background_sources'] or material_refs)
        if error:
            research.update(status='FAILED', failure_kind='TASK_FAILED', reason=error)
        elif research['fixed_status'] == 'FAILED' and not had_rows:
            research.update(status='FAILED', failure_kind='NETWORK_FAILED',
                            reason='固定来源检查与规则搜索均失败')
        else:
            failures = []
            if research['fixed_status'] == 'FAILED':
                failed_sources = [item['source_id'] for item in research['sources_checked']
                                  if item['status'] == 'FAILED']
                failures.append('固定来源检查失败：' + ('、'.join(failed_sources) if failed_sources else '全部来源'))
            if research['rule_status'] == 'FAILED':
                failures.append('规则关键词搜索失败')
            if research['ai_status'] == 'FAILED':
                failures.append('AI辅助失败：' + (research.get('ai_pause_reason')
                                                 or research.get('ai_trigger_reason') or '原因未知'))
            research['status'] = 'PARTIAL' if failures else 'COMPLETED'
            research['reason'] = '；'.join(failures) or None
        research['ai_call_count'] = len(calls)
        research['input_tokens'] = sum(item.get('input_tokens') or 0 for item in calls)
        research['output_tokens'] = sum(item.get('output_tokens') or 0 for item in calls)
        final_status, final_topic, final_reason = None, None, ''
        with SessionLocal() as db:
            # A settings save blocks here or precedes this check. Both orders are well-defined.
            current = db.query(AIResearchSettingsModel).filter_by(owner_username=owner).with_for_update().first()
            job = db.query(AIResearchJobModel).filter_by(job_id=job_id, status='RUNNING',
                lease_token=claimed['token']).with_for_update().first()
            if not job:
                return
            persist_rows(result['sources'] + result['background_sources'])
            valid_count = sum(not cache._invalid_reasons(cache._source(
                db.query(cache.AIResearchMaterialModel).filter_by(material_id=ref['material_id']).one(), ref, job),
                completed, config['interval_minutes']) for ref in material_refs)
            failed = research['status'] == 'FAILED'
            if failed:
                job.status = 'FAILED'
            elif research['status'] == 'PARTIAL':
                job.status = 'PARTIAL'
            else:
                job.status = 'COMPLETED'
            job.completed_at = completed
            job.next_check_at = completed + timedelta(minutes=config['interval_minutes'])
            job.active_key = job.lease_token = job.lease_until = None
            job.error = error or research.get('reason')
            job.calls_json = cache._json(calls)
            publishable = bool(current and current.config_version==job.config_version and
                (job.trigger=='MANUAL' or current.enabled) and job.topic in cache.topics_for(json.loads(current.contracts_json)))
            job.published = not failed and publishable
            outcome = 'FAILED' if failed else 'NEW_MATERIAL' if new_count else 'NO_NEW_MATERIAL'
            job.result_json = cache._json(dict(result, research=research, material_refs=material_refs,
                checks_publishable=publishable, new_material_count=new_count,
                valid_material_count=valid_count, outcome=outcome, ai_derivations=derivations))
            # AI 暂停只标记 AI 阶段；固定采集与既有队列不受影响。
            if ai['pause_write'] and current and current.config_version == job.config_version:
                current.pause_reason = research.get('ai_pause_reason') or research.get('ai_trigger_reason')
                current.paused_model_hash = (ai['state'] or {}).get('config_hash')
            final_status, final_topic = job.status, job.topic
            final_reason = error or research.get('reason') or ''
            db.commit()
        if final_status == 'FAILED':
            from backend.ai_analysis import alerts
            alerts.notify_throttled(
                f'research:{final_topic}:FAILED', 'AI_RESEARCH_JOB_FAILED',
                f'定时资料采集失败：{final_topic}', 'ERROR', 'ai_research',
                {'reason': final_reason[:300]})


def tick(now=None):
    schedule_due(now)
    interrupted = []
    with SessionLocal() as db:
        # An expired lease is interrupted, never re-claimed and re-sent.
        for job in db.query(AIResearchJobModel).filter(AIResearchJobModel.status=='RUNNING', AIResearchJobModel.lease_until < (now or _now())):
            job.status, job.active_key, job.completed_at = 'INTERRUPTED', None, now or _now()
            job.next_check_at = job.completed_at
            job.error = '采集租约已过期，原调用不重发'
            interrupted.append(job.topic)
        db.commit()
        row = db.query(AIResearchJobModel).filter_by(status='QUEUED').order_by(
            AIResearchJobModel.queued_at, AIResearchJobModel.id).first()
        job_id = row.job_id if row else None
    for topic in interrupted:
        from backend.ai_analysis import alerts
        alerts.notify_throttled(
            f'research:{topic}:INTERRUPTED', 'AI_RESEARCH_JOB_INTERRUPTED',
            f'定时资料采集中断（租约过期，已调用模型的结果被丢弃）：{topic}', 'ERROR', 'ai_research')
    if job_id and not _stop.is_set():
        run_job(job_id)


def _loop():
    while not _stop.is_set():
        try:
            tick()
        except Exception:
            logger.exception('Public research worker tick failed')
        _stop.wait(2)


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    recover_interrupted()
    _stop.clear()
    _thread = threading.Thread(target=_loop, name='PublicResearch', daemon=True)
    _thread.start()


def stop():
    _stop.set()
    if _thread:
        _thread.join(timeout=3)
