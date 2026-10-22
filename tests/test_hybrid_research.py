"""AI 触发、冷却、共享预算与失败隔离：AI 关闭保证 0 调用，AI 失败不丢固定结果。"""
import hashlib
import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_bootstrap
from backend.models.database import AIModelConfigModel, AIResearchJobModel, InstrumentModel
from backend.storage.mysql_client import SessionLocal


NOW = datetime(2026, 10, 2, 19)


def settings(**changes):
    value = {'enabled': True, 'ai_enabled': True, 'model_config_id': 'research-model-a',
             'interval_minutes': 60, 'config_version': 0, 'contracts': [
                 {'instrument': 'FU2611', 'related': []},
                 {'instrument': 'SC2611', 'related': []},
                 {'instrument': 'BU2611', 'related': []}]}
    return dict(value, **changes)


def policy_row(body='为保持流动性合理充裕，决定调整完善若干货币政策工具，自2026年10月15日起实施。',
               published='2026-09-29', **fields):
    return dict({'url': 'https://www.pbc.gov.cn/goutongjiaoliu/policy.html',
        'title': '中国人民银行调整完善若干货币政策工具', 'body': body, 'excerpt': body[:200],
        'content_hash': hashlib.sha256(body.encode()).hexdigest(), 'content_type': 'BODY',
        'published_at': published, 'fetched_at': NOW.isoformat(), 'data_period': 'UNKNOWN',
        'dimension': 'macro', 'scope': 'macro', 'product_code': None, 'source_kind': 'OFFICIAL',
        'source_name': '中国人民银行', 'extractable': True, 'applicable_contracts': [],
        'latest_release': True, 'series_id': 'PBC_NEWS_RELEASE:MONETARY_POLICY',
        'latest_verified_at': NOW.isoformat()}, **fields)


def event_row(body='燃料油炼厂停产事件：某炼厂因故障停产。', **fields):
    # 常规新闻正文由规则阶段处理；extractable=False 使扫描类测试免受提取调用干扰。
    return dict({'url': 'https://futures.eastmoney.com/a/event.html',
        'title': '燃料油炼厂停产', 'body': body, 'excerpt': body,
        'content_hash': hashlib.sha256(body.encode()).hexdigest(), 'content_type': 'BODY',
        'published_at': '2026-10-01', 'fetched_at': NOW.isoformat(), 'data_period': 'UNKNOWN',
        'dimension': 'events', 'scope': 'product', 'product_code': 'FU', 'source_kind': 'NEWS',
        'extractable': False, 'applicable_contracts': []}, **fields)


def fixed_result(rows=None, status='COMPLETED'):
    rows = [] if rows is None else rows
    return {'status': status, 'sources': rows, 'candidates': [], 'events': [], 'errors': [],
            'sources_checked': [{'source_id': 'NBS_LATEST_RELEASE', 'status': 'NOT_APPLICABLE'}],
            'latest_release_checks': [], 'checks_count': 1, 'reads': 0}


def rule_result(rows=None, candidates=None, searches=2, reads=0):
    return {'status': 'COMPLETED', 'sources': rows or [], 'candidates': candidates or [],
            'events': [], 'errors': [], 'searches': searches, 'reads': reads}


def extraction_reply(facts=None, unverified=None):
    payload = json.dumps({'facts': facts if facts is not None else [
        {'metric': '政策生效时间', 'value': '2026-10-15', 'unit': None, 'period': None,
         'region': None, 'quote': '自2026年10月15日起实施'}],
        'unverified': unverified or [], 'truncated': False, 'notes': ''}, ensure_ascii=False)
    return SimpleNamespace(input_tokens=120, output_tokens=60, latency_ms=200, content=payload)


NO_OP_SCAN = {'sources': [], 'background_sources': [],
              'research': {'status': 'COMPLETED', 'rounds': 0, 'search_count': 0,
                           'pages_read': 0, 'events': []}}


class HybridResearchTests(unittest.TestCase):
    def setUp(self):
        from backend.ai_analysis import research_cache as cache, research_scheduler as scheduler
        self.cache, self.scheduler = cache, scheduler
        self.clock = patch.object(cache, '_now', return_value=NOW)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.sclock = patch.object(scheduler, '_now', return_value=NOW)
        self.sclock.start(); self.addCleanup(self.sclock.stop)
        self.catalog = patch.object(cache, 'get_universe_cache', return_value={})
        self.catalog.start(); self.addCleanup(self.catalog.stop)
        # AI 派生结果的 created_at 也必须使用冻结时钟，保证截点比较可确定性
        self.wclock = patch.object(self.scheduler.web_research, '_stamp',
                                   return_value=NOW.isoformat() + '+08:00')
        self.wclock.start(); self.addCleanup(self.wclock.stop)
        from backend.models.database import AIResearchSettingsModel, AIResearchMaterialModel
        self.Settings, self.Material = AIResearchSettingsModel, AIResearchMaterialModel
        self.Job = AIResearchJobModel
        with SessionLocal() as db:
            for cls in (self.Job, self.Material, self.Settings):
                db.query(cls).delete()
            db.query(AIModelConfigModel).filter(AIModelConfigModel.model_config_id.like('research-model-%')).delete()
            db.add(AIModelConfigModel(model_config_id='research-model-a', owner_username='hybrid-a',
                display_name='研究测试', provider='openai_compatible', model_name='fixture',
                base_url='http://127.0.0.1:9987/v1', is_local_service=True, requires_api_key=False))
            for symbol in ('FU2611', 'SC2611', 'BU2611'):
                if not db.query(InstrumentModel).filter_by(symbol=symbol).first():
                    db.add(InstrumentModel(symbol=symbol, name=symbol, exchange='SHFE'))
            db.commit()

    def save(self, **kw):
        return self.cache.save_settings('hybrid-a', settings(**kw))

    def run_topic(self, topic='macro', fixed=None, rule=None, scan=None, completions=None):
        """Enqueue and run one topic with stage-level fakes; returns the job row summary."""
        jobs = self.scheduler.enqueue_refresh('hybrid-a')['jobs']
        job = next(j for j in jobs if j['topic'] == topic)
        fixed = fixed if fixed is not None else fixed_result()
        rule = rule if rule is not None else rule_result()
        patches = [patch('backend.ai_analysis.research_scheduler.fixed_research.collect_fixed', return_value=fixed),
                   patch('backend.ai_analysis.research_scheduler.fixed_research.collect_rule_search', return_value=rule)]
        if scan is None:
            scan = NO_OP_SCAN
        if callable(scan):
            patches.append(patch('backend.ai_analysis.research_scheduler.web_research.run_research',
                                 side_effect=scan))
        else:
            patches.append(patch('backend.ai_analysis.research_scheduler.web_research.run_research',
                                 return_value=scan))
        replies = list(completions) if completions is not None else []
        patches.append(patch('backend.ai_analysis.orchestrator._provider_completion',
                             side_effect=lambda c, b: replies.pop(0) if replies else extraction_reply()))
        for p in patches:
            p.start()
        try:
            self.scheduler.run_job(job['job_id'])
        finally:
            for p in patches:
                p.stop()
        with SessionLocal() as db:
            row = db.query(self.Job).filter_by(job_id=job['job_id']).one()
            db.refresh(row)
            return SimpleNamespace(job_id=row.job_id, status=row.status, published=row.published,
                calls=json.loads(row.calls_json), result=json.loads(row.result_json), error=row.error)

    def test_ai_disabled_guarantees_zero_model_calls(self):
        self.save(ai_enabled=False)
        run = self.run_topic(topic='product:FU',
                             fixed=fixed_result(rows=[event_row()]),
                             rule=rule_result())
        self.assertEqual(run.calls, [], 'AI 关闭时不允许任何模型调用')
        self.assertEqual(run.result['research']['ai_status'], 'DISABLED')
        self.assertEqual(run.result['research']['ai_call_count'], 0)
        self.assertEqual(run.status, 'COMPLETED')
        # 定时调度同样不依赖模型即可排队
        self.scheduler.schedule_due(NOW + timedelta(minutes=61))
        with SessionLocal() as db:
            queued = db.query(self.Job).filter_by(status='QUEUED', topic='product:FU').all()
            self.assertEqual(len(queued), 1)

    def test_new_complex_body_extracted_and_fabrications_rejected(self):
        self.save()
        fabricated = {'metric': '虚构库存', 'value': '999', 'unit': '万吨', 'period': None,
                      'region': None, 'quote': '这段引文并不存在于原文之中'}
        run = self.run_topic(topic='macro', fixed=fixed_result(rows=[policy_row()]),
                             completions=[extraction_reply(facts=[
                                 {'metric': '政策生效时间', 'value': '2026-10-15', 'unit': None,
                                  'period': None, 'region': None, 'quote': '自2026年10月15日起实施'},
                                 fabricated])])
        self.assertEqual(run.status, 'COMPLETED')
        self.assertEqual(run.result['research']['ai_status'], 'COMPLETED')
        self.assertEqual(run.result['research']['ai_call_count'], 1)
        self.assertEqual(run.calls[0]['purpose'], 'EXTRACTION')
        derivations = run.result['ai_derivations']
        self.assertEqual(len(derivations), 1)
        self.assertEqual(len(derivations[0]['facts']), 1, '有原文摘录支撑的事实才有效')
        self.assertEqual(derivations[0]['facts'][0]['value'], '2026-10-15')
        self.assertEqual(derivations[0]['unverified'][0]['metric'], '虚构库存',
                         '虚构摘录不得进入有效事实')
        self.assertEqual(derivations[0]['prompt_version'], 'extract-v2')
        self.assertNotIn('api_key', json.dumps(run.calls, ensure_ascii=False))

    def test_same_body_not_reextracted_and_previous_result_reused(self):
        self.save()
        run1 = self.run_topic(topic='macro', fixed=fixed_result(rows=[policy_row()]))
        self.assertEqual(run1.result['research']['ai_call_count'], 1)
        # 同一原文版本再次采集：不重复付费提取，复用上次派生结果
        run2 = self.run_topic(topic='macro', fixed=fixed_result(rows=[policy_row()]))
        self.assertEqual(run2.result['research']['ai_call_count'], 0)
        derivations = run2.result['ai_derivations']
        self.assertEqual(len(derivations), 1)
        self.assertTrue(derivations[0]['reused'])
        self.assertEqual(derivations[0]['facts'], run1.result['ai_derivations'][0]['facts'])

    def test_model_or_prompt_version_change_reprocesses_document(self):
        self.save()
        run1 = self.run_topic(topic='macro', fixed=fixed_result(rows=[policy_row()]))
        self.assertEqual(run1.result['research']['ai_call_count'], 1)
        # 更换模型（配置变化）→ 视为新处理
        with SessionLocal() as db:
            model = db.query(AIModelConfigModel).filter_by(model_config_id='research-model-a').one()
            model.model_name = 'fixture-v2'
            db.commit()
        run2 = self.run_topic(topic='macro', fixed=fixed_result(rows=[policy_row()]))
        self.assertEqual(run2.result['research']['ai_call_count'], 1)
        self.assertFalse(run2.result['ai_derivations'][0].get('reused'))
        # 提示词版本变化 → 同样重新处理
        with patch.object(self.scheduler.web_research, 'EXTRACTION_PROMPT_VERSION', 'extract-v3'):
            run3 = self.run_topic(topic='macro', fixed=fixed_result(rows=[policy_row()]))
        self.assertEqual(run3.result['research']['ai_call_count'], 1)
        self.assertEqual(run3.result['ai_derivations'][0]['prompt_version'], 'extract-v3')

    def test_first_gap_scan_runs_then_cooldown_blocks(self):
        self.save()
        def scan(snapshot, runtime, completion, **kw):
            completion(runtime, {'probe': 'scan'})
            return {'sources': [], 'background_sources': [],
                    'research': {'status': 'COMPLETED', 'rounds': 1, 'search_count': 1,
                                 'pages_read': 1, 'events': [], 'tool_support': 'SUPPORTED'}}
        # 缺口存在（无 fundamentals）：首次缺口补查立即触发
        run1 = self.run_topic(topic='product:FU', fixed=fixed_result(rows=[event_row()]),
                              scan=scan, completions=[extraction_reply()])
        self.assertEqual(run1.result['research']['ai_status'], 'COMPLETED')
        self.assertEqual(run1.result['research']['ai_call_count'], 1)
        self.assertEqual(run1.calls[0]['purpose'], 'GAP_SCAN')
        # 冷却期内再次触发：跳过且不再付费
        run2 = self.run_topic(topic='product:FU', fixed=fixed_result(rows=[event_row()]))
        self.assertEqual(run2.result['research']['ai_status'], 'SKIPPED_COOLDOWN')
        self.assertEqual(run2.result['research']['ai_call_count'], 0)
        self.assertIsNone(run2.result['research'].get('ai_stage'))
        # 冷却到期后允许再次扫描
        later = NOW + timedelta(minutes=61)
        with patch.object(self.scheduler, '_now', return_value=later), \
             patch.object(self.cache, '_now', return_value=later):
            run3 = self.run_topic(topic='product:FU', fixed=fixed_result(rows=[event_row()]),
                                  scan=scan, completions=[extraction_reply()])
        self.assertEqual(run3.result['research']['ai_status'], 'COMPLETED')
        self.assertEqual(run3.result['research']['ai_call_count'], 1)

    def test_no_gaps_first_scan_still_runs_then_no_trigger(self):
        self.save()
        # macro 主题序列齐全时没有缺口，但首次事件扫描仍允许
        run1 = self.run_topic(topic='macro', fixed=fixed_result(rows=[policy_row()]))
        self.assertEqual(run1.result['research']['ai_status'], 'COMPLETED')
        # 冷却期内且无缺口 → SKIPPED_NO_TRIGGER
        run2 = self.run_topic(topic='macro', fixed=fixed_result(rows=[policy_row()]))
        self.assertEqual(run2.result['research']['ai_status'], 'SKIPPED_NO_TRIGGER')
        self.assertEqual(run2.result['research']['ai_call_count'], 0)

    def test_failed_and_no_result_scans_still_cool_down(self):
        self.save()
        failed_scan = {'sources': [], 'background_sources': [],
                       'research': {'status': 'UNAVAILABLE', 'pause_required': True,
                                    'failure_kind': 'NO_TOOL_CALLS', 'reason': '模型未调用工具',
                                    'rounds': 1, 'search_count': 0, 'pages_read': 0, 'events': []}}
        run1 = self.run_topic(topic='product:FU', fixed=fixed_result(rows=[event_row()]), scan=failed_scan)
        self.assertEqual(run1.result['research']['ai_status'], 'FAILED')
        self.assertEqual(run1.status, 'PARTIAL', 'AI 失败时固定结果仍发布，任务记 PARTIAL')
        self.assertTrue(run1.published)
        self.assertTrue(len(run1.result['material_refs']) >= 1, 'AI 失败不得丢失固定资料')
        # 模型暂停标记已写入；固定任务队列继续
        with SessionLocal() as db:
            row = db.get(self.Settings, 'hybrid-a')
            self.assertIsNotNone(row.pause_reason)
            self.assertIsNotNone(row.paused_model_hash)
        run2 = self.run_topic(topic='product:FU', fixed=fixed_result(rows=[event_row()]))
        self.assertEqual(run2.result['research']['ai_status'], 'PAUSED_MODEL')
        self.assertEqual(run2.result['research']['ai_call_count'], 0)
        self.assertEqual(run2.status, 'COMPLETED', 'AI 暂停后固定阶段照常完成')
        # 模型参数更换后暂停解除；但失败扫描的冷却仍然生效（提取按新配置重跑）
        with SessionLocal() as db:
            model = db.query(AIModelConfigModel).filter_by(model_config_id='research-model-a').one()
            model.model_name = 'fixture-v2'
            db.commit()
        run3 = self.run_topic(topic='product:FU', fixed=fixed_result(rows=[event_row()]))
        self.assertEqual(run3.result['research']['ai_status'], 'SKIPPED_COOLDOWN')
        self.assertEqual(run3.calls, [], '冷却期内不发送任何模型请求')

    def test_interrupted_scan_leaves_cooldown_anchor(self):
        self.save()
        jobs = self.scheduler.enqueue_refresh('hybrid-a')['jobs']
        job = next(j for j in jobs if j['topic'] == 'product:FU')
        # 模拟已发送扫描请求后被重启中断：进度里记录了发送时间
        sent_at = NOW - timedelta(minutes=10)
        payload = {'sources': [], 'background_sources': [], 'research': {
            'ai_stage': {'kind': 'GAP_SCAN', 'status': 'RUNNING', 'started_at': sent_at.isoformat(),
                         'last_sent_at': sent_at.isoformat()}}}
        with SessionLocal() as db:
            row = db.query(self.Job).filter_by(job_id=job['job_id']).one()
            row.status = 'RUNNING'
            row.result_json = self.cache._json(payload)
            db.commit()
        self.scheduler.recover_interrupted()
        anchor = self.scheduler._scan_anchor('hybrid-a', 'product:FU')
        self.assertEqual(anchor, sent_at.isoformat())
        run = self.run_topic(topic='product:FU', fixed=fixed_result(rows=[event_row()]))
        self.assertEqual(run.result['research']['ai_status'], 'SKIPPED_COOLDOWN',
                         '中断前已发送的扫描按记录时间启动冷却，防止重启后立即重复付费')

    def test_shared_budget_caps_model_calls_and_skips_scan(self):
        self.save()
        rows = [policy_row(url=f'https://www.pbc.gov.cn/goutongjiaoliu/p{i}.html',
                           body=f'货币政策工具公告第{i}号，自2026年10月1{i}日起实施。')
                for i in range(5)]
        run = self.run_topic(topic='macro', fixed=fixed_result(rows=rows))
        self.assertEqual(run.result['research']['ai_call_count'], 3)
        self.assertEqual(len([d for d in run.result['ai_derivations'] if not d.get('reused')]), 3)
        self.assertIn('2 篇', run.result['research']['ai_trigger_reason'])
        self.assertEqual(run.status, 'COMPLETED')

    def test_product_gaps_reserve_call_after_many_extractions(self):
        self.save()
        rows = [event_row(url=f'https://futures.eastmoney.com/a/event{i}.html',
                          extractable=True, body=f'燃料油公告第{i}号，自2026年10月15日起实施。')
                for i in range(5)]
        def scan(snapshot, runtime, completion, **kw):
            self.assertEqual(kw['budget']['model_calls'], 1)
            completion(runtime, {'probe': 'scan'})
            return NO_OP_SCAN
        run = self.run_topic(topic='product:FU', fixed=fixed_result(rows=rows), scan=scan)
        self.assertEqual(run.result['research']['ai_call_count'], 3)
        self.assertEqual(len([d for d in run.result['ai_derivations'] if not d.get('reused')]), 2)

    def test_fixed_stage_consumes_budget_before_ai(self):
        self.save()
        def greedy_fixed(snapshot, topic, budget):
            budget.try_consume('searches', 8)
            budget.try_consume('reads', 12)
            return fixed_result()
        jobs = self.scheduler.enqueue_refresh('hybrid-a')['jobs']
        job = next(j for j in jobs if j['topic'] == 'product:FU')
        with patch('backend.ai_analysis.research_scheduler.fixed_research.collect_fixed',
                   side_effect=greedy_fixed), \
             patch('backend.ai_analysis.research_scheduler.fixed_research.collect_rule_search',
                   return_value=rule_result()), \
             patch('backend.ai_analysis.research_scheduler.web_research.run_research',
                   return_value=NO_OP_SCAN) as scan_run:
            self.scheduler.run_job(job['job_id'])
        scan_run.assert_not_called()
        with SessionLocal() as db:
            row = db.query(self.Job).filter_by(job_id=job['job_id']).one()
            research = json.loads(row.result_json)['research']
            self.assertEqual(research['ai_status'], 'SKIPPED_BUDGET')
            self.assertEqual(json.loads(row.calls_json), [])
            self.assertEqual(row.status, 'COMPLETED')

    def test_scan_discovery_tagged_and_published_with_fixed_results(self):
        self.save()
        def scan(snapshot, runtime, completion, **kw):
            completion(runtime, {'probe': 'scan'})
            return {'sources': [{'url': 'https://futures.eastmoney.com/a/aidiscovered.html',
                                 'title': '燃料油制裁事件进展', 'excerpt': '燃料油制裁事件进展正文',
                                 'published_at': '2026-10-02', 'dimension': 'events', 'scope': 'product',
                                 'product_code': 'FU', 'stale': False, 'warnings': [],
                                 'content_type': 'BODY', 'body': '燃料油制裁事件进展正文',
                                 'content_hash': hashlib.sha256('燃料油制裁事件进展正文'.encode()).hexdigest(),
                                 'fetched_at': NOW.isoformat()}],
                    'background_sources': [],
                    'research': {'status': 'COMPLETED', 'rounds': 1, 'search_count': 1, 'pages_read': 1,
                                 'events': [], 'tool_support': 'SUPPORTED'}}
        run = self.run_topic(topic='product:FU', fixed=fixed_result(rows=[event_row()]),
                             scan=scan, completions=[extraction_reply()])
        self.assertEqual(run.status, 'COMPLETED')
        self.assertTrue(run.published)
        rows = {row['url']: row for row in run.result['sources']}
        self.assertEqual(rows['https://futures.eastmoney.com/a/aidiscovered.html']['collection_method'],
                         'AI_DISCOVERY')
        self.assertEqual([call['purpose'] for call in run.calls], ['GAP_SCAN'])

    def test_frozen_cache_includes_selected_ai_derivations(self):
        self.save()
        self.run_topic(topic='macro', fixed=fixed_result(rows=[policy_row()]))
        frozen = self.cache.freeze_materials('hybrid-a', 'FU2611', NOW)
        self.assertEqual(len(frozen['sources']), 1)
        derivations = frozen['ai_derivations']
        self.assertEqual(len(derivations), 1)
        self.assertEqual(derivations[0]['source_id'], frozen['sources'][0]['source_id'])
        self.assertEqual(derivations[0]['facts'][0]['value'], '2026-10-15')
        # 派生结果完成时间晚于截点时不得进入该批预测（原文来源仍可用）
        with SessionLocal() as db:
            job = db.query(self.Job).filter_by(topic='macro').order_by(
                self.Job.id.desc()).first()
            payload = json.loads(job.result_json)
            payload['ai_derivations'][0]['created_at'] = (NOW + timedelta(minutes=5)).isoformat() + '+08:00'
            job.result_json = self.cache._json(payload)
            db.commit()
        frozen_at_cutoff = self.cache.freeze_materials('hybrid-a', 'FU2611', NOW)
        self.assertEqual(frozen_at_cutoff['ai_derivations'], [])
        self.assertEqual(len(frozen_at_cutoff['sources']), 1)


if __name__ == '__main__':
    unittest.main()
