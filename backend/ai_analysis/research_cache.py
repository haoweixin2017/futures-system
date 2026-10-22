"""Owner-scoped settings and immutable public evidence selected at a frozen cutoff."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func as sa_func, or_
from sqlalchemy.exc import IntegrityError

from backend.ai_analysis import future_trend, registry
from backend.common.product_specs import extract_product
from backend.models.database import (AIResearchSettingsModel, AIResearchJobModel,
    AIResearchMaterialModel, AIModelConfigModel, InstrumentModel)
from backend.storage.mysql_client import SessionLocal
from backend.storage.universe_sync import get_universe_cache


def _now():
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _iso(value):
    return value.isoformat() + '+08:00' if value else None


def _settings(row):
    # 旧配置的关联关系只保留在历史冻结批次中，不再参与当前采集和预测。
    contracts = [{'instrument': item['instrument'], 'related': []}
                 for item in json.loads(row.contracts_json)] if row else []
    return {'enabled': bool(row.enabled) if row else False,
        'ai_enabled': bool(row.ai_enabled) if row else False,
        'model_config_id': row.model_config_id if row else None,
        'interval_minutes': row.interval_minutes if row else 60,
        'contracts': contracts, 'config_version': row.config_version if row else 0,
        'updated_at': _iso(row.updated_at) if row else None,
        'pause_reason': row.pause_reason if row else None,
        'product_count': len(topics_for(contracts)) - bool(contracts),
        'topic_count': len(topics_for(contracts))}


def topics_for(contracts, instrument=None):
    selected = [r for r in contracts if instrument is None or r['instrument'] == instrument]
    if not selected:
        return []
    symbols = [r['instrument'] for r in selected]
    return ['macro'] + sorted({'product:' + extract_product(s) for s in symbols})


def _collection_group(contracts, instrument):
    """Reading can reuse a configured product without adding collection targets."""
    exact = next((group for group in contracts if group['instrument'] == instrument), None)
    return exact or next((group for group in contracts
                         if extract_product(group['instrument']) == extract_product(instrument)), None)


def get_settings(owner):
    with SessionLocal() as db:
        return _settings(db.get(AIResearchSettingsModel, owner))


def model_status(owner, model_id, db=None):
    if not model_id:
        return {'available': False, 'reason': '未选择研究模型'}
    models = registry.list_models(owner)
    item = next((m for m in models if m['model_config_id'] == model_id), None)
    if not item:
        return {'available': False, 'reason': '研究模型不存在或已删除'}
    if item['provider'] not in {'openai_compatible', 'google_gemini'}:
        return {'available': False, 'reason': '研究模型提供方不支持工具调用'}
    with SessionLocal() as session:
        row = registry.get_model(session, owner, model_id)
        frozen = {k:getattr(row,k) for k in ('model_config_id','display_name','provider','base_url',
            'model_name','credential_ref','is_local_service','max_context_tokens','max_output_tokens','timeout_seconds')}
        frozen['temperature'] = float(row.temperature)
    return {'available': item['available'], 'reason': item['unavailable_reason'],
            'model_display_name': item['display_name'], 'model_name': item['model_name'],
            'config_hash':registry.config_hash(frozen)}


def _normalize_contracts(db, contracts):
    known = {s.upper() for s, in db.query(InstrumentModel.symbol).all()}
    known.update(s.upper() for s in get_universe_cache() if not s.startswith('__'))
    result, seen = [], set()
    for item in contracts:
        symbol = str(item.get('instrument') or '').strip().upper()
        if not re.fullmatch(r'[A-Z]{1,3}\d{3,4}', symbol) or symbol not in known:
            raise ValueError(f'未知或非具体月份合约：{symbol}')
        if item.get('related'):
            raise ValueError('关联合约参考功能已移除，请将合约添加为独立采集目标')
        if symbol not in seen:
            result.append({'instrument': symbol, 'related': []})
            seen.add(symbol)
    return result


def save_settings(owner, data):
    with SessionLocal() as db:
        previous = db.get(AIResearchSettingsModel, owner)
        previous_topics = set(topics_for(json.loads(previous.contracts_json))) if previous else set()
        previous_model = previous.model_config_id if previous else None
        previous_ai = bool(previous.ai_enabled) if previous else False
        contracts = _normalize_contracts(db, data.get('contracts', []))
        interval = data.get('interval_minutes', 60)
        if interval not in (15, 30, 60, 120):
            raise ValueError('采集间隔仅支持15、30、60、120分钟')
        model_id = data.get('model_config_id') or None
        enabled = bool(data.get('enabled'))
        # 字段未提交(None)时保留已有值；只有显式提交才覆盖。新记录默认 False。
        if data.get('ai_enabled') is None:
            ai_enabled = bool(previous.ai_enabled) if previous else False
        else:
            ai_enabled = bool(data['ai_enabled'])
        if ai_enabled:
            if not model_id:
                raise ValueError('开启 AI 辅助研究前必须选择研究模型')
            state = model_status(owner, model_id)
            if not state['available']:
                raise ValueError(state['reason'])
        if enabled:
            # 固定采集只要求有效目标名单，不再要求研究模型。
            if not contracts:
                raise ValueError('开启定时采集前至少配置一个目标合约')
        expected = data.get('config_version', 0)
        values = dict(enabled=enabled, ai_enabled=ai_enabled, model_config_id=model_id,
            interval_minutes=interval, contracts_json=_json(contracts),
            config_version=expected+1, updated_at=_now(),
            pause_reason=None, paused_model_hash=None)
        if expected == 0:
            if previous:
                raise RuntimeError('资料采集设置已变更，请刷新后重新保存')
            db.add(AIResearchSettingsModel(owner_username=owner, **values))
            try:
                db.flush()
            except IntegrityError as exc:
                raise RuntimeError('资料采集设置已变更，请刷新后重新保存') from exc
        elif db.query(AIResearchSettingsModel).filter_by(owner_username=owner,
                config_version=expected).update(values, synchronize_session=False) != 1:
            raise RuntimeError('资料采集设置已变更，请刷新后重新保存')
        # Superseded queued calls have not spent resources and must not be claimed.
        db.query(AIResearchJobModel).filter_by(owner_username=owner, status='QUEUED').update(
            {'status':'CANCELLED', 'active_key':None, 'completed_at':_now(),
             'error':'采集设置版本已改变'}, synchronize_session=False)
        # Re-added products are new scope even when old historical jobs exist.
        seen = set()
        for job in db.query(AIResearchJobModel).filter_by(owner_username=owner).filter(
                AIResearchJobModel.status != 'CANCELLED').order_by(
                AIResearchJobModel.queued_at.desc(), AIResearchJobModel.id.desc()):
            if job.topic in seen:
                continue
            seen.add(job.topic)
            if job.completed_at and job.topic in topics_for(contracts):
                job.next_check_at = _now() if job.topic not in previous_topics or model_id != previous_model \
                    or ai_enabled != previous_ai else (job.completed_at + timedelta(minutes=interval))
        db.commit()
    return get_settings(owner)


def contract_status(instrument, cutoff, targets=None):
    result = future_trend.contract_lifecycle(instrument, targets or [], cutoff)
    term = result.get('last_trading_day') or result.get('last_trading_day_by_rule')
    expired = bool(term and cutoff.strftime('%Y%m%d') > term)
    bound_unknown = (result.get('status') == 'RULE_BOUND' and
        (cutoff.strftime('%Y%m%d') >= result['last_trading_day_not_before'] or
         any(t['status'] == 'UNKNOWN' for t in result['targets'])))
    return dict(result, instrument=instrument, expired=expired,
                warning='已到期，请更换具体合约' if expired else
                        '期限未核实' if result.get('status') == 'UNKNOWN' or bound_unknown else '')


def canonical_url(value):
    parsed = urlsplit(value)
    query = [(k,v) for k,v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('spm','from','source')]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/',
                      urlencode(sorted(query)), ''))


def persist_sources(db, job, rows, fetched_at):
    """Never overwrite an old body or its first/version fetch timestamps."""
    references, inserted = [], 0
    for source in rows:
        if source.get('content_type') != 'BODY' or not source.get('body'):
            continue
        body = str(source['body'])
        # Compute from body ourselves; never trust a model or summary-supplied hash.
        content_hash = _hash(body)
        url = canonical_url(source['url'])
        url_hash = _hash(url)
        material = db.query(AIResearchMaterialModel).filter_by(owner_username=job.owner_username,
            url_hash=url_hash, content_hash=content_hash).first()
        metadata = {k:v for k,v in source.items() if k not in ('body','content_hash','source_id')}
        if not material:
            first = db.query(AIResearchMaterialModel.first_fetched_at).filter_by(
                owner_username=job.owner_username, url_hash=url_hash).order_by(
                AIResearchMaterialModel.first_fetched_at).first()
            version_time = future_trend._timestamp(source.get('fetched_at')) or fetched_at
            material = AIResearchMaterialModel(material_id=str(uuid.uuid4()),
                owner_username=job.owner_username, url=url, url_hash=url_hash, content_hash=content_hash,
                title=str(source.get('title') or '')[:512], body=body, metadata_json=_json(metadata),
                published_at=future_trend._timestamp(source.get('published_at')),
                first_fetched_at=first[0] if first else version_time, version_fetched_at=version_time,
                original_job_id=job.job_id)
            db.add(material); db.flush()
            inserted += 1
        # Verification and applicability are facts about this check, not changes to an old version.
        references.append({'material_id':material.material_id, 'url':material.url, **{k:source.get(k) for k in (
            'dimension','scope','product_code','applicable_contracts','valid_from','valid_to',
            'macro_series','series_id','latest_release','latest_verified_at','applicable_from',
            'applicable_to','excerpt','data_period','freshness_policy','verification_ttl_hours','expected_update_by',
            'exchange','warehouse_quantity','warehouse_unit','warehouse_totals','document_hash')}})
    return references, inserted


MANUAL_DIMENSIONS = ('fundamentals', 'macro', 'events', 'related_markets')


def export_materials(owner, topic=None, dimension=None, method=None, series=None, q=None,
                     instrument=None, limit=500):
    """按查询筛选导出含正文的资料记录, 字段与手工录入/导入格式兼容。"""
    listed = list_materials(owner, topic=topic, dimension=dimension, method=method, series=series,
                            q=q, instrument=instrument, page=1, page_size=limit)
    with SessionLocal() as db:
        rows = db.query(AIResearchMaterialModel).filter(
            AIResearchMaterialModel.material_id.in_([item['material_id'] for item in listed['items']])
        ).all() if listed['items'] else []
    bodies = {row.material_id: row.body for row in rows}
    records = []
    for item in listed['items']:
        records.append({
            'target': 'macro' if item['dimension'] == 'macro' else (instrument or None),
            'product_code': item.get('product_code'),
            'dimension': item['dimension'],
            'title': item['title'],
            'body': bodies.get(item['material_id']) or '',
            'url': item['url'],
            'source_name': item.get('source_name'),
            'published_at': item.get('published_at') if item.get('published_at') != 'UNKNOWN' else None,
            'data_period': item.get('data_period') if item.get('data_period') != 'UNKNOWN' else None,
        })
    return {'total': listed['total'], 'exported': len(records), 'limit': limit, 'records': records}


def persist_manual_materials(owner, records, now):
    """手工录入/批量导入: 资料写入素材库, 并生成一条已发布的手工采集检查 (trigger=MANUAL),
    使其与常规采集一样参与适用性判定与预测冻结; 来源诚实标记为 MANUAL。"""
    config = get_settings(owner)
    configured = {r['instrument'] for r in config['contracts']}
    topics = set(topics_for(config['contracts']))
    grouped = {}
    for record in records:
        target, dimension = record['target'], record['dimension']
        title, body = str(record['title']).strip(), str(record['body']).strip()
        if not title or not body:
            raise ValueError('标题与正文不能为空')
        if target == 'macro':
            if dimension != 'macro':
                raise ValueError('公共宏观目标只能录入宏观维度资料')
            topic, scope, product = 'macro', 'macro', None
        else:
            if target not in configured:
                raise ValueError(f'{target} 未在已保存的目标合约名单中, 请先在采集设置保存')
            if dimension not in MANUAL_DIMENSIONS:
                raise ValueError(f'维度无效: {dimension}')
            if dimension == 'macro':
                raise ValueError('宏观维度资料请选择公共宏观目标')
            topic, scope, product = 'product:' + extract_product(target), 'product', extract_product(target)
        url = (record.get('url') or '').strip()
        if url and not url.lower().startswith(('http://', 'https://')):
            raise ValueError(f'原文链接必须是 http/https 地址: {url}')
        url = url or 'manual://note/' + _hash(title + '\n' + body)[:16] + '/' + topic
        published_at = (record.get('published_at') or '').strip()
        if published_at and future_trend._timestamp(published_at) is None:
            raise ValueError(f'发布日期无效: {published_at}')
        grouped.setdefault(topic, []).append({
            'url': url, 'title': title[:512], 'body': body, 'excerpt': body[:300],
            'content_type': 'BODY', 'dimension': dimension, 'scope': scope,
            'product_code': product, 'applicable_contracts': [],
            'source_kind': 'MANUAL', 'collection_method': 'MANUAL',
            'source_name': (str(record.get('source_name') or '').strip() or '手工录入')[:64],
            'published_at': published_at or None,
            'fetched_at': now.isoformat(),
            'data_period': (str(record.get('data_period') or '').strip() or 'UNKNOWN')[:64],
        })
    details = []
    with SessionLocal() as db:
        for topic, rows in grouped.items():
            fresh = []
            for row in rows:
                exists = db.query(AIResearchMaterialModel.id).filter_by(
                    owner_username=owner, url_hash=_hash(canonical_url(row['url'])),
                    content_hash=_hash(row['body'])).first()
                if not exists:
                    fresh.append(row)
            if not fresh:
                details.append({'topic': topic, 'inserted': 0, 'total': len(rows)})
                continue
            published = topic in topics
            job = AIResearchJobModel(job_id=str(uuid.uuid4()), owner_username=owner, topic=topic,
                active_key=None, config_version=config['config_version'], config_json=_json(config),
                model_json='{}', trigger='MANUAL', status='COMPLETED', queued_at=now, started_at=now,
                completed_at=now, next_check_at=now + timedelta(minutes=config['interval_minutes']),
                published=published, calls_json='[]', error=None)
            db.add(job)
            db.flush()
            references, inserted = persist_sources(db, job, fresh, now)
            result = {'sources': [{k: v for k, v in row.items() if k != 'body'} for row in fresh],
                'research': {'status': 'COMPLETED', 'summary': f'手工录入 {inserted} 条资料',
                             'fixed_status': 'COMPLETED', 'rule_status': 'SKIPPED', 'ai_status': 'SKIPPED'},
                'material_refs': references, 'checks_publishable': published,
                'new_material_count': inserted, 'valid_material_count': inserted,
                'outcome': 'NEW_MATERIAL' if inserted else 'NO_NEW_MATERIAL', 'ai_derivations': []}
            job.result_json = _json(result)
            details.append({'topic': topic, 'inserted': inserted, 'total': len(rows)})
        db.commit()
    inserted_total = sum(item['inserted'] for item in details)
    return {'inserted': inserted_total, 'duplicates': len(records) - inserted_total, 'details': details}


def _source(material, ref, job, checked_at=None):
    metadata = json.loads(material.metadata_json)
    metadata.update({k:v for k,v in ref.items() if v is not None})
    metadata.update(material_id=material.material_id, url=material.url, title=material.title,
        content_hash=material.content_hash, published_at=metadata.get('published_at') or 'UNKNOWN',
        first_fetched_at=_iso(material.first_fetched_at), fetched_at=_iso(material.version_fetched_at),
        version_fetched_at=_iso(material.version_fetched_at), original_job_id=material.original_job_id,
        check_job_id=job.job_id, checked_at=_iso(checked_at or job.completed_at))
    if metadata.get('series_id') == 'EIA:WPSR':
        # Older stored rows used week-ending as release date; never propagate that error.
        metadata['published_at'] = 'UNKNOWN'
    metadata.pop('body', None)
    return metadata


def _invalid_reasons(source, cutoff, interval):
    reasons = []
    published = future_trend._timestamp(source.get('published_at'))
    fetched = future_trend._timestamp(source.get('version_fetched_at'))
    if not published and not future_trend._verified_data_period(source, cutoff):
        reasons.append('UNKNOWN_PUBLICATION_TIME')
    elif published and published > cutoff:
        reasons.append('AFTER_CUTOFF_PUBLICATION')
    if not fetched or fetched > cutoff:
        reasons.append('AFTER_CUTOFF_FETCH')
    if source.get('freshness_policy') == 'VERIFIED_DATA_PERIOD' and not future_trend._verified_data_period(source, cutoff):
        reasons.append('OFFICIAL_DATA_PERIOD_UNVERIFIED')
    dimension = source.get('dimension')
    expected = future_trend._timestamp(source.get('expected_update_by'))
    if expected and cutoff > expected:
        reasons.append('REPORT_UPDATE_OVERDUE')
    # 明确注册的官方周期性统计/报告使用 LATEST_VERIFIED_RELEASE：
    # 核验有效期独立于轮询频率；网络失败不会把月报立即变成过期内容。
    periodic = dimension == 'macro' or source.get('freshness_policy') == 'LATEST_VERIFIED_RELEASE'
    if periodic:
        checked = future_trend._timestamp(source.get('latest_verified_at'))
        if (source.get('source_kind') != 'OFFICIAL' or not source.get('latest_release') or not checked
                or checked > cutoff or cutoff - checked > timedelta(
                    hours=int(source.get('verification_ttl_hours') or 168))):
            reasons.append('LATEST_RELEASE_UNVERIFIED')
    elif published and (cutoff.date()-published.date()).days > (
            14 if dimension == 'fundamentals' else 7):
        reasons.append('EXPIRED')
    return reasons


def _published_result(job, cutoff):
    """Select a committed stage without exposing a later AI completion at an earlier cutoff."""
    result = json.loads(job.result_json or '{}')
    if job.completed_at and job.completed_at <= cutoff and job.published:
        return dict(result, _published_at=job.completed_at)
    for stage in reversed(result.get('fixed_snapshots') or []):
        published = future_trend._timestamp(stage.get('published_at'))
        if stage.get('publishable') and published and published <= cutoff:
            return dict(stage, _published_at=published)
    if job.completed_at and job.completed_at <= cutoff and result.get('checks_publishable'):
        return dict(result, _published_at=job.completed_at)
    return None


def freeze_materials(owner, instrument, cutoff, targets=None):
    cutoff = future_trend._timestamp(cutoff.isoformat() if isinstance(cutoff, datetime) else cutoff)
    if cutoff is None:
        raise ValueError('预测截点无效')
    config = get_settings(owner)
    group = _collection_group(config['contracts'], instrument)
    state = contract_status(instrument, cutoff, targets)
    permitted = {instrument} if not state['expired'] else set()
    selected, background, checks = [], [], []
    derivations = []
    if group:
        topic_set = topics_for([group])
        with SessionLocal() as db:
            completed_jobs = db.query(AIResearchJobModel).filter(
                AIResearchJobModel.owner_username == owner, AIResearchJobModel.topic.in_(topic_set),
                or_(AIResearchJobModel.started_at <= cutoff,
                    AIResearchJobModel.completed_at <= cutoff)).order_by(
                AIResearchJobModel.started_at.desc(), AIResearchJobModel.id.desc()).all()
            published_results = {job.job_id: _published_result(job, cutoff) for job in completed_jobs}
            for job in completed_jobs:
                result = published_results[job.job_id]
                summary = job_summary(job)
                if result and (not job.completed_at or job.completed_at > cutoff):
                    summary.update(status='RUNNING', completed_at=None, next_check_at=None,
                        research=result.get('research') or {}, call_count=0, input_tokens=0,
                        output_tokens=0, latency_ms=None, outcome=None, error=None)
                elif not job.completed_at or job.completed_at > cutoff:
                    continue
                checks.append(summary)
            completed_jobs = [job for job in completed_jobs if published_results[job.job_id]]
            completed_jobs.sort(key=lambda job: (published_results[job.job_id]['_published_at'], job.id), reverse=True)
            jobs = [job for job in completed_jobs if job.published or
                    published_results[job.job_id].get('publishable')]
            material_ids = {ref['material_id'] for job in jobs
                for ref in published_results[job.job_id].get('material_refs', [])}
            versions = {m.material_id:m for m in db.query(AIResearchMaterialModel).filter(
                AIResearchMaterialModel.owner_username == owner,
                AIResearchMaterialModel.material_id.in_(material_ids)).all()} if material_ids else {}
            seen = set()
            latest_releases = {}
            for job in completed_jobs:
                result = published_results[job.job_id]
                # A successful official index check remains a fact when reading its body fails.
                # The configuration/enable gate still excludes superseded running jobs.
                if not (job.published or result.get('checks_publishable') or result.get('publishable')):
                    continue
                for check in (result.get('research') or {}).get('latest_release_checks', []):
                    series = check.get('series_id') or check.get('macro_series')
                    if series and check.get('latest_release') and check.get('status') == 'COMPLETED':
                        latest_releases.setdefault(series, check)
                # AI 派生结果通过 material_id 引用原文；截点前完成的才可选入。
                for derivation in result.get('ai_derivations') or []:
                    if derivation.get('status') != 'COMPLETED' or derivation.get('prompt_version') != 'extract-v2':
                        continue
                    created = future_trend._timestamp(derivation.get('created_at'))
                    if created and created > cutoff:
                        continue
                    derivations.append(derivation)
            for job in jobs:
                result = published_results[job.job_id]
                for ref in result.get('material_refs', []):
                    material = versions.get(ref['material_id'])
                    if not material or material.version_fetched_at > cutoff:
                        continue
                    source = _source(material, ref, job, result['_published_at'])
                    product = source.get('product_code') or (job.topic.split(':',1)[1] if ':' in job.topic else None)
                    matches = [s for s in permitted if extract_product(s) == product]
                    if source.get('dimension') != 'macro' and not matches:
                        continue
                    applicable = source.get('applicable_contracts') or []
                    if source.get('scope') == 'contract':
                        matches = [s for s in matches if s in applicable]
                        if not matches:
                            continue
                    key = (material.url_hash, source['dimension'], product, tuple(sorted(applicable)))
                    if key in seen:
                        continue
                    seen.add(key)
                    source['product_code'] = product
                    source['applicable_contracts'] = applicable
                    source['reference_contracts'] = []
                    source['target_contracts'] = [instrument] if instrument in matches else []
                    source['is_target_evidence'] = instrument in matches
                    validity = _invalid_reasons(source, cutoff, config['interval_minutes'])
                    series = source.get('series_id') or source.get('macro_series')
                    latest = latest_releases.get(series)
                    if series and latest:
                        if (canonical_url(latest['url']) != canonical_url(source['url']) or
                                (latest.get('content_hash') and latest['content_hash'] != source['content_hash'])):
                            validity.append('SUPERSEDED_OFFICIAL_RELEASE')
                        else:
                            source['latest_verified_at'] = latest.get('verified_at')
                            validity = _invalid_reasons(source, cutoff, config['interval_minutes'])
                    start = future_trend._timestamp(source.get('valid_from') or source.get('applicable_from'))
                    end = future_trend._timestamp(source.get('valid_to') or source.get('applicable_to'))
                    target_dates = [datetime.strptime(t['trading_day'],'%Y%m%d') for t in targets or []]
                    if target_dates and ((start and all(t.date()<start.date() for t in target_dates))
                                         or (end and all(t.date()>end.date() for t in target_dates))):
                        validity.append('OUTSIDE_APPLICABLE_PERIOD')
                    source['warnings'] = list(dict.fromkeys((source.get('warnings') or []) + validity))
                    source['stale'] = bool(validity)
                    if future_trend._verified_data_period(source, cutoff):
                        source['validity_basis'] = 'OFFICIAL_VERIFIED_DATA_PERIOD'
                    (background if validity else selected).append(source)
    # Prefer the target's own direct evidence, then macro.
    from backend.ai_analysis.web_research import fundamental_coverage
    metric_counts = {id(s): sum(item['status'] == 'AVAILABLE' for item in fundamental_coverage([s], instrument))
        if s['dimension'] == 'fundamentals' and s['is_target_evidence'] else 0 for s in selected}
    selected.sort(key=lambda s:(0 if s['is_target_evidence'] else 1 if s['dimension']=='macro' else 2,
        0 if s.get('source_kind')=='OFFICIAL' else 1,
        -metric_counts[id(s)],
        -(future_trend._timestamp(s['published_at']) or future_trend._timestamp(s.get('data_period'))
          or cutoff).replace(tzinfo=timezone(timedelta(hours=8))).timestamp()))
    bounded, counts = [], {}
    for source in selected:
        dimension = source['dimension']
        if len(bounded) >= 24 or counts.get(dimension,0) >= 6:
            continue
        source['excerpt'] = str(source.get('excerpt') or '')[:2000]
        source['source_id'] = 'S' + str(len(bounded)+1)
        bounded.append(source); counts[dimension] = counts.get(dimension,0)+1
    # 把实际选入的 AI 派生结果与来源一并冻结；没有派生结果时仍使用真实原文摘录。
    attached_derivations, derivation_ids = [], set()
    selected_ids = {source.get('material_id'): source['source_id'] for source in bounded}
    for derivation in derivations:
        source_id = selected_ids.get(derivation.get('material_id'))
        if not source_id or derivation.get('material_id') in derivation_ids:
            continue
        derivation_ids.add(derivation.get('material_id'))
        attached_derivations.append({'source_id': source_id, 'material_id': derivation.get('material_id'),
            'purpose': derivation.get('purpose'), 'prompt_version': derivation.get('prompt_version'),
            'facts': derivation.get('facts') or [], 'unverified': derivation.get('unverified') or [],
            'created_at': derivation.get('created_at'), 'reused': bool(derivation.get('reused'))})
    # 金融股指期货 (CFFEX 指数) 没有商品式供需库存基本面, 宏观维度即其基本面口径:
    # fundamentals 不计入覆盖缺口, 否则股指永远只能是"部分可用" (2026-10-07 口径修正)。
    financial_index = extract_product(instrument) in ('IH', 'IF', 'IC', 'IM')
    missing = [d for d in future_trend.EXTERNAL_DIMENSIONS if d not in counts
               and not (financial_index and d == 'fundamentals')]
    gaps = list(missing)
    from backend.ai_analysis.web_research import fundamental_coverage
    expired_candidates = [s for s in background if s.get('stale') and
        set(s.get('warnings') or []) <= {'EXPIRED', 'REPORT_UPDATE_OVERDUE', 'SUPERSEDED_OFFICIAL_RELEASE'}]
    fundamental = fundamental_coverage([s for s in bounded + expired_candidates if s['dimension'] == 'fundamentals'
                                       and s['is_target_evidence']], instrument)
    if not financial_index:
        if 'fundamentals' not in gaps:
            from backend.ai_analysis.web_research import _fundamental_detail
            if not any(s['dimension']=='fundamentals' and s['is_target_evidence'] and
                       _fundamental_detail(s, instrument) for s in bounded):
                gaps.append('fundamentals')
        if any(item['status'] == 'MISSING' for item in fundamental) and 'fundamentals' not in gaps:
            gaps.append('fundamentals')
    cache_state = 'NOT_CONFIGURED' if not group else 'AVAILABLE' if not gaps else 'PARTIAL' if bounded else 'EMPTY'
    newest = next((job for job in checks if job['status'] in ('COMPLETED','PARTIAL','FAILED')), {})
    public = {'status':'AVAILABLE' if not gaps else 'PARTIAL' if bounded else 'MISSING',
        'sources':bounded, 'background_sources':background, 'missing_dimensions':missing,
        'coverage_gaps':gaps, 'errors':[], 'cache_state':cache_state,
        'fundamental_coverage': fundamental,
        'related_configuration_status': 'NOT_CONFIGURED',
        'config_version':config['config_version'], 'collection_enabled':config['enabled'], 'related_contracts':[],
        'related_contract_status':[], 'contract_status':state,
        'cutoff':_iso(cutoff), 'truncated_count':len(selected)-len(bounded),
        'freshness_policy':{'fundamentals_days':14,'events_days':7,'related_markets_days':7,
                            'macro':'LATEST_OFFICIAL_RELEASE_VERIFIED_WITHIN_7_DAYS'},
        'ai_derivations':attached_derivations,
        'research':{'status':'CACHED', 'mode':'SCHEDULED_CACHE', 'events':[],
                    'last_check_at':newest.get('completed_at'),
                    'completed_at':newest.get('completed_at'), 'jobs':checks}}
    public['hash'] = _hash(_json(public))
    return public


def job_summary(job):
    result = json.loads(job.result_json or '{}')
    calls = json.loads(job.calls_json or '[]')
    research = result.get('research') or {}
    return {'job_id':job.job_id, 'topic':job.topic, 'status':job.status, 'trigger':job.trigger,
        'config_version':job.config_version, 'queued_at':_iso(job.queued_at),
        'started_at':_iso(job.started_at), 'completed_at':_iso(job.completed_at),
        'next_check_at':_iso(job.next_check_at), 'published':job.published,
        'research':research, 'call_count':len(calls),
        'input_tokens':sum(c.get('input_tokens') or 0 for c in calls),
        'output_tokens':sum(c.get('output_tokens') or 0 for c in calls),
        'latency_ms':int((job.completed_at-job.started_at).total_seconds()*1000)
            if job.completed_at and job.started_at else None,
        'new_material_count':result.get('new_material_count',0),
        'valid_material_count':result.get('valid_material_count',0), 'error':job.error,
        'outcome':result.get('outcome')}


def next_due(job, config, model_hash, now):
    """model_hash is the frozen AI snapshot hash, or None when AI is off/unavailable."""
    if not job:
        return now
    job_model = json.loads(job.model_json or '{}')
    if bool(job_model) != bool(model_hash):
        return now
    if job_model and registry.config_hash(job_model) != model_hash:
        return now
    return job.next_check_at or (job.completed_at + timedelta(minutes=config['interval_minutes'])
                                if job.completed_at else now)


def get_material(owner, material_id):
    with SessionLocal() as db:
        row = db.query(AIResearchMaterialModel).filter_by(owner_username=owner, material_id=material_id).first()
        # An audited but unpublished result is not a current source; it remains owner-only.
        if not row:
            raise LookupError('资料版本不存在')
        metadata = json.loads(row.metadata_json)
        if metadata.get('series_id') == 'EIA:WPSR':
            metadata['published_at'] = 'UNKNOWN'
        return {**metadata, 'material_id':row.material_id, 'url':row.url,
            'title':row.title, 'body':row.body, 'content_hash':row.content_hash,
            'first_fetched_at':_iso(row.first_fetched_at), 'version_fetched_at':_iso(row.version_fetched_at),
            'original_job_id':row.original_job_id}


def list_materials(owner, topic=None, dimension=None, method=None, series=None, q=None,
                   page=1, page_size=20, instrument=None, cutoff=None, history_for=None):
    """Owner-scoped collected-material browser; never returns full bodies."""
    def meta_eq(field, value):
        return sa_func.json_unquote(sa_func.json_extract(
            AIResearchMaterialModel.metadata_json, '$.' + field)) == value

    from sqlalchemy.orm import aliased, defer
    moment = future_trend._timestamp(cutoff) if cutoff else _now()
    if moment is None:
        raise ValueError('资料截点无效')
    frozen = freeze_materials(owner, instrument, moment) if instrument else None
    contextual = {}
    if frozen:
        for source in frozen['background_sources'] + frozen['sources']:
            key = source.get('material_id')
            if key not in contextual or source.get('is_target_evidence'):
                contextual[key] = source
    with SessionLocal() as db:
        # Applicability belongs to each collection check, never to a mutable original version.
        uses = {}
        for job in db.query(AIResearchJobModel).filter_by(owner_username=owner).order_by(
                AIResearchJobModel.started_at.asc(), AIResearchJobModel.id.asc()).all():
            result = _published_result(job, moment)
            if not result:
                continue
            refs = result.get('material_refs') or []
            for ref in refs:
                if topic == 'macro' and ref.get('dimension') != 'macro':
                    continue
                if topic and topic.startswith('product:') and ref.get('product_code') != topic.split(':', 1)[1]:
                    continue
                if dimension and ref.get('dimension') != dimension:
                    continue
                uses[ref['material_id']] = ref
        base = db.query(AIResearchMaterialModel).filter(
            AIResearchMaterialModel.owner_username == owner,
            AIResearchMaterialModel.version_fetched_at <= moment)
        if instrument:
            products = {extract_product(instrument)}
            related_ids = [key for key, ref in uses.items() if ref.get('product_code') in products
                           or ref.get('dimension') == 'macro']
            base = base.filter(or_(meta_eq('dimension', 'macro'),
                sa_func.json_unquote(sa_func.json_extract(AIResearchMaterialModel.metadata_json,
                                                         '$.product_code')).in_(products),
                AIResearchMaterialModel.material_id.in_(related_ids)))
        if history_for:
            origin = db.query(AIResearchMaterialModel).filter_by(owner_username=owner,
                material_id=history_for).first()
            if not origin:
                raise LookupError('资料版本不存在')
            base = base.filter(AIResearchMaterialModel.url_hash == origin.url_hash)
        else:
            newer = aliased(AIResearchMaterialModel)
            base = base.filter(~db.query(newer.id).filter(
                newer.owner_username == owner, newer.url_hash == AIResearchMaterialModel.url_hash,
                newer.version_fetched_at <= moment, or_(
                    newer.version_fetched_at > AIResearchMaterialModel.version_fetched_at,
                    (newer.version_fetched_at == AIResearchMaterialModel.version_fetched_at) &
                    (newer.id > AIResearchMaterialModel.id))).exists())
        if topic == 'macro':
            base = base.filter(or_(meta_eq('dimension', 'macro'), AIResearchMaterialModel.material_id.in_(uses)))
        elif topic and topic.startswith('product:'):
            base = base.filter(or_(meta_eq('product_code', topic.split(':', 1)[1]),
                                  AIResearchMaterialModel.material_id.in_(uses)))
        elif topic:
            raise ValueError('主题参数无效')
        if dimension:
            # 维度是资料自身的存储分类; 不按历史批次的引用记录放行, 否则
            # 曾被其他维度引用过的资料会混入当前筛选 (相关市场混进基本面等)。
            base = base.filter(meta_eq('dimension', dimension))
        if method:
            base = base.filter(meta_eq('collection_method', method))
        if series:
            base = base.filter(or_(meta_eq('series_id', series), meta_eq('macro_series', series)))
        if q:
            keyword = str(q).strip()[:100]
            if keyword:
                escaped = keyword.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                like = '%' + escaped + '%'
                base = base.filter(or_(AIResearchMaterialModel.title.like(like),
                                       AIResearchMaterialModel.body.like(like)))
        total = base.count()
        rows = base.options(defer(AIResearchMaterialModel.body)).order_by(AIResearchMaterialModel.version_fetched_at.desc(),
                             AIResearchMaterialModel.id.desc()).offset(
            (page - 1) * page_size).limit(page_size).all()
        version_counts = {}
        latest_versions = {}
        if rows:
            grouped = db.query(AIResearchMaterialModel.url_hash,
                               sa_func.count(AIResearchMaterialModel.id)).filter(
                AIResearchMaterialModel.owner_username == owner,
                AIResearchMaterialModel.version_fetched_at <= moment,
                AIResearchMaterialModel.url_hash.in_([row.url_hash for row in rows])).group_by(
                AIResearchMaterialModel.url_hash).all()
            version_counts = {url_hash: count for url_hash, count in grouped}
            for version in db.query(AIResearchMaterialModel.material_id, AIResearchMaterialModel.url_hash).filter(
                    AIResearchMaterialModel.owner_username == owner,
                    AIResearchMaterialModel.version_fetched_at <= moment,
                    AIResearchMaterialModel.url_hash.in_([row.url_hash for row in rows])).order_by(
                    AIResearchMaterialModel.version_fetched_at.desc(), AIResearchMaterialModel.id.desc()):
                latest_versions.setdefault(version.url_hash, version.material_id)
        items = []
        for row in rows:
            try:
                meta = json.loads(row.metadata_json)
            except (TypeError, ValueError):
                meta = {}
            # 维度展示与维度筛选保持同一口径 (存储分类), 不被适用性引用/上下文改写
            meta.update({k: v for k, v in uses.get(row.material_id, {}).items()
                         if v is not None and k != 'dimension'})
            context = contextual.get(row.material_id)
            if context:
                meta.update({k: v for k, v in context.items() if k != 'dimension'})
            reasons = context.get('warnings', []) if context else ['NOT_SELECTED_FOR_TARGET'] if instrument else []
            validity = ('VALID' if not context.get('stale') else
                        'SUPERSEDED' if 'SUPERSEDED_OFFICIAL_RELEASE' in reasons else
                        'UNVERIFIED' if any('UNVERIFIED' in r or 'UNKNOWN' in r for r in reasons) else
                        'BACKGROUND') if context else 'BACKGROUND' if instrument else 'UNCHECKED'
            if latest_versions.get(row.url_hash) != row.material_id:
                validity, reasons = 'SUPERSEDED', ['SUPERSEDED_VERSION']
            items.append({'material_id': row.material_id, 'url': row.url, 'title': row.title,
                'dimension': meta.get('dimension'),
                'series_id': meta.get('series_id') or meta.get('macro_series'),
                'data_period': meta.get('data_period') or 'UNKNOWN',
                'collection_method': meta.get('collection_method'),
                'source_name': meta.get('source_name'), 'source_kind': meta.get('source_kind'),
                'product_code': meta.get('product_code'),
                'published_at': 'UNKNOWN' if meta.get('series_id') == 'EIA:WPSR' else
                    _iso(row.published_at) if row.published_at else 'UNKNOWN',
                'first_fetched_at': _iso(row.first_fetched_at),
                'version_fetched_at': _iso(row.version_fetched_at),
                'content_hash': row.content_hash,
                'excerpt': str(meta.get('excerpt') or '')[:300],
                'version_count': version_counts.get(row.url_hash, 1),
                'validity': validity, 'validity_reasons': reasons,
                'relationship': 'MACRO' if meta.get('dimension') == 'macro' else
                    'TARGET' if context and context.get('is_target_evidence') else
                    'REFERENCE' if context and context.get('reference_contracts') else 'OTHER',
                'checked_at': meta.get('checked_at'),
                'facts': next((d['facts'] for d in (frozen or {}).get('ai_derivations', [])
                               if d.get('material_id') == row.material_id), [])})
    return {'items': items, 'total': total, 'page': page, 'page_size': page_size,
            'instrument': instrument, 'cutoff': _iso(moment), 'history_for': history_for}


def source_distribution(rows):
    counts = {}
    clusters = {}
    unknown = 0
    for row in rows:
        host = (urlsplit(row['url']).hostname or 'UNKNOWN').lower()
        source = 'eastmoney.com' if host == 'eastmoney.com' or host.endswith('.eastmoney.com') else host
        counts[source] = counts.get(source, 0) + 1
        body = re.sub(r'\s+', '', row.get('body') or '')
        if len(body) < 80:
            unknown += 1
        else:
            clusters.setdefault(_hash(body), set()).add(source)
    total = sum(counts.values())
    return {'total': total, 'sources': counts,
        'eastmoney_percent': round(100 * counts.get('eastmoney.com', 0) / total, 2) if total else None,
        'content_clusters':{'known_content_clusters':len(clusters), 'unknown_content_documents':unknown,
            'cross_domain_reprint_candidates':sum(len(hosts)>1 for hosts in clusters.values()),
            'source_cluster_presence':{host:sum(host in hosts for hosts in clusters.values()) for host in counts},
            'origin_status':'UNVERIFIED', 'method':'Exact normalized full-body match; different wording may be missed'}}


def get_source_distribution(owner):
    with SessionLocal() as db:
        versions = db.query(AIResearchMaterialModel.id, AIResearchMaterialModel.url,
            AIResearchMaterialModel.original_job_id, AIResearchMaterialModel.body).filter_by(owner_username=owner).order_by(AIResearchMaterialModel.id).all()
        recent = db.query(AIResearchJobModel).filter_by(owner_username=owner, published=True).order_by(
            AIResearchJobModel.completed_at.desc(), AIResearchJobModel.id.desc()).limit(6).all()
        rows = [dict(id=v.id, url=v.url, job_id=v.original_job_id, body=v.body) for v in versions]
        latest = {row['url']: row for row in rows}
        return {'scope': 'OWNER_ALL', 'historical_versions': source_distribution(rows),
            'latest_stored_documents': source_distribution(list(latest.values())),
            'recent_jobs': [dict(job_id=job.job_id, topic=job.topic, completed_at=_iso(job.completed_at),
                **source_distribution([row for row in rows if row['job_id'] == job.job_id])) for job in recent]}


def get_status(owner, instrument=None, all_contracts=False):
    from backend.ai_analysis import channel_policy
    config = get_settings(owner)
    collection_group = _collection_group(config['contracts'], instrument) if instrument else None
    topic_set = (topics_for([collection_group]) if instrument and not all_contracts and collection_group
                 else topics_for(config['contracts'], None if all_contracts else instrument))
    with SessionLocal() as db:
        jobs = db.query(AIResearchJobModel).filter(AIResearchJobModel.owner_username == owner,
            AIResearchJobModel.topic.in_(topic_set)).order_by(AIResearchJobModel.queued_at.desc(),
                                                          AIResearchJobModel.id.desc()).all()
        latest, due_jobs = {}, {}
        for job in jobs:
            if job.topic not in latest:
                latest[job.topic] = job_summary(job)
            if job.status != 'CANCELLED':
                due_jobs.setdefault(job.topic, job)
        checked = [j.completed_at for j in jobs if j.status in ('COMPLETED','PARTIAL','FAILED') and j.completed_at]
        successful = [j.completed_at for j in jobs if j.published and
            json.loads(j.result_json or '{}').get('valid_material_count',0) and j.completed_at]
        paused_hash = db.get(AIResearchSettingsModel, owner).paused_model_hash if config['config_version'] else None
    status = model_status(owner,config['model_config_id'])
    if config['ai_enabled'] and config['pause_reason'] and paused_hash and paused_hash == status.get('config_hash'):
        status = dict(status, available=False, reason=config['pause_reason'])
    # Scheduling never depends on the model; the hash only tracks AI-stage relevance.
    ai_hash = status.get('config_hash') if config['ai_enabled'] and status['available'] else None
    future_checks = [_iso(next_due(due_jobs.get(t), config, ai_hash, _now()))
                     for t in topic_set if not due_jobs.get(t) or due_jobs[t].status not in ('QUEUED','RUNNING')]
    groups = [g for g in config['contracts'] if all_contracts or instrument is None or g['instrument']==instrument]
    if instrument and collection_group and not any(g['instrument'] == instrument for g in groups):
        groups = [*groups, {'instrument':instrument, 'related':[]}]
    materials = freeze_materials(owner,instrument,_now()) if instrument else None
    summaries = []
    for group in groups:
        frozen = materials if group['instrument'] == instrument else freeze_materials(owner,group['instrument'],_now())
        summaries.append({'instrument':group['instrument'], 'related':group['related'],
            'status':frozen['status'], 'cache_state':frozen['cache_state'],
            'missing_dimensions':frozen['missing_dimensions'], 'coverage_gaps':frozen['coverage_gaps'],
            'fundamental_coverage': frozen['fundamental_coverage'],
            'dimensions':{d:'AVAILABLE' if d not in frozen['coverage_gaps'] else
                'INSUFFICIENT' if d not in frozen['missing_dimensions'] else
                'UNVERIFIED' if any(s['dimension']==d and any(w in s['warnings'] for w in
                                    ('LATEST_RELEASE_UNVERIFIED','SUPERSEDED_OFFICIAL_RELEASE'))
                                    for s in frozen['background_sources']) else
                'EXPIRED' if any(s['dimension']==d for s in frozen['background_sources']) else 'MISSING'
                for d in future_trend.EXTERNAL_DIMENSIONS}, 'contract_status':frozen['contract_status'],
            'related_contract_status':frozen['related_contract_status']})
    return {'settings':config, 'topics':list(latest.values()), 'contracts':summaries,
        'materials':materials, 'model_status':status, 'source_distribution':get_source_distribution(owner),
        'channel_policy':channel_policy.summary(),
        'last_check_at':_iso(max(checked)) if checked else None,
        'last_success_at':_iso(max(successful)) if successful else None,
        'next_check_at':min(future_checks) if config['enabled'] and future_checks else None}
