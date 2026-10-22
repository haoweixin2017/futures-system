import hashlib
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from datetime import datetime, timedelta
from unittest.mock import patch

from tests import test_bootstrap
from backend.models.database import AIModelConfigModel, InstrumentModel
from backend.storage.mysql_client import SessionLocal


NOW = datetime(2026, 10, 2, 19)


def settings(**changes):
    value = {'enabled': False, 'ai_enabled': False, 'model_config_id': 'research-model-a',
             'interval_minutes': 60, 'config_version': 0, 'contracts': [
                 {'instrument': 'FU2611', 'related': []},
                 {'instrument': 'FU2612', 'related': []},
                 {'instrument': 'SC2611', 'related': []},
                 {'instrument': 'BU2611', 'related': []}]}
    return dict(value, **changes)


def evidence(body='燃料油库存下降，供应减少。', published='2026-10-01', dimension='fundamentals', **fields):
    return dict({'url': 'https://finance.eastmoney.com/a/202610011234.html',
        'title': '燃料油库存周报', 'body': body, 'excerpt': body,
        'content_hash': hashlib.sha256(body.encode()).hexdigest(), 'content_type': 'BODY',
        'published_at': published, 'fetched_at': NOW.isoformat(), 'data_period': '2026年9月',
        'dimension': dimension, 'scope': 'product', 'product_code': 'FU',
        'source_kind': 'NEWS', 'applicable_contracts': []}, **fields)


def fixed_result(rows=None, status='COMPLETED', latest_release_checks=None, sources_checked=None):
    rows = [evidence()] if rows is None else rows
    return {'status': status, 'sources': rows, 'candidates': [], 'events': [], 'errors': [],
            'sources_checked': sources_checked if sources_checked is not None else
                [{'source_id': 'FIXED', 'status': 'COMPLETED', 'checked_at': NOW.isoformat()}],
            'latest_release_checks': latest_release_checks or [], 'checks_count': 1}


def rule_result(rows=None, candidates=None, status='COMPLETED'):
    return {'status': status, 'sources': rows or [], 'candidates': candidates or [], 'events': [],
            'errors': [], 'searches': 2}


class ScheduledResearchTests(unittest.TestCase):
    def test_unlisted_same_product_contract_reuses_cache_without_new_collection(self):
        self.save(contracts=[{'instrument':'FU2611','related':[]}])
        self.complete(rows=[evidence(), evidence(url='https://finance.eastmoney.com/a/month.html',
            body='FU2611合约交割说明',scope='contract',applicable_contracts=['FU2611'])])
        with SessionLocal() as db:
            count = db.query(self.Job).count()
        frozen = self.cache.freeze_materials('research-a','FU2701',NOW)
        self.assertEqual(len(frozen['sources']),1)
        self.assertEqual(frozen['sources'][0]['target_contracts'],['FU2701'])
        self.assertNotEqual(frozen['cache_state'],'NOT_CONFIGURED')
        status = self.cache.get_status('research-a','FU2701')
        self.assertTrue(any(topic['topic']=='product:FU' for topic in status['topics']))
        self.assertTrue(any(row['instrument']=='FU2701' for row in status['contracts']))
        self.assertEqual(self.cache.get_settings('research-a')['contracts'],[{'instrument':'FU2611','related':[]}])
        with SessionLocal() as db:
            self.assertEqual(db.query(self.Job).count(),count)
        self.assertEqual(self.cache.freeze_materials('research-b','FU2701',NOW)['sources'],[])
        self.assertEqual(self.cache.freeze_materials('research-a','BU2701',NOW)['sources'],[])

    def test_status_exposes_disabled_channels_immediately(self):
        from backend.ai_analysis import channel_policy
        result = self.cache.get_status('research-a')
        self.assertEqual(result['channel_policy'], channel_policy.summary())
        self.assertTrue(any(not item['enabled'] and item['reason'] for item in result['channel_policy']))

    def setUp(self):
        from backend.ai_analysis import research_cache as cache, research_scheduler as scheduler
        self.cache, self.scheduler = cache, scheduler
        from backend.ai_analysis import alerts
        alerts._last_sent.clear()  # 告警限频是模块级状态, 避免跨测试消耗窗口
        self.clock = patch.object(cache, '_now', return_value=NOW)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.sclock = patch.object(scheduler, '_now', return_value=NOW)
        self.sclock.start(); self.addCleanup(self.sclock.stop)
        self.catalog = patch.object(cache, 'get_universe_cache', return_value={})
        self.catalog.start(); self.addCleanup(self.catalog.stop)
        from backend.models.database import AIResearchSettingsModel, AIResearchJobModel, AIResearchMaterialModel
        self.Settings, self.Job, self.Material = AIResearchSettingsModel, AIResearchJobModel, AIResearchMaterialModel
        with SessionLocal() as db:
            for cls in (self.Job, self.Material, self.Settings):
                db.query(cls).delete()
            db.query(AIModelConfigModel).filter(AIModelConfigModel.model_config_id.like('research-model-%')).delete()
            db.add(AIModelConfigModel(model_config_id='research-model-a', owner_username='research-a',
                display_name='研究测试', provider='openai_compatible', model_name='fixture',
                base_url='http://127.0.0.1:9987/v1', is_local_service=True, requires_api_key=False))
            for symbol in ('FU2611', 'FU2612', 'SC2611', 'BU2611', 'RB2611', 'CU2611', 'AU2611', 'AG2611'):
                if not db.query(InstrumentModel).filter_by(symbol=symbol).first():
                    db.add(InstrumentModel(symbol=symbol, name=symbol, exchange='SHFE'))
            db.commit()

    def save(self, **kw):
        return self.cache.save_settings('research-a', settings(**kw))

    def test_expired_metric_is_candidate_and_does_not_fill_valid_sources(self):
        self.save()
        self.complete(rows=[evidence(body='2026年9月燃料油库存100万吨。', published='2026-09-01')])
        frozen = self.cache.freeze_materials('research-a', 'FU2611', NOW)
        inventory = next(item for item in frozen['fundamental_coverage'] if '库存' in item['metric'])
        self.assertEqual(inventory['status'], 'MISSING')
        self.assertEqual(inventory['detail_status'], 'STALE')
        self.assertEqual(frozen['sources'], [])

    def complete(self, topic='product:FU', rows=None, fixed=None, rule=None, run=True):
        jobs = self.scheduler.enqueue_refresh('research-a')['jobs']
        job = next(j for j in jobs if j['topic'] == topic)
        fixed = fixed if fixed is not None else fixed_result(rows=rows)
        rule = rule if rule is not None else rule_result()
        if run:
            with patch('backend.ai_analysis.research_scheduler.fixed_research.collect_fixed', return_value=fixed), \
                 patch('backend.ai_analysis.research_scheduler.fixed_research.collect_rule_search', return_value=rule):
                self.scheduler.run_job(job['job_id'])
        return job

    def test_topics_deduplicate_independent_targets(self):
        saved = self.save()
        self.assertEqual(saved['topic_count'], 4)
        jobs = self.scheduler.enqueue_refresh('research-a')['jobs']
        self.assertEqual({j['topic'] for j in jobs}, {'macro', 'product:FU', 'product:SC', 'product:BU'})
        self.assertEqual(self.cache.freeze_materials('research-a', 'BU2611', NOW)['related_contracts'], [])
        self.assertEqual(self.cache.freeze_materials('research-a', 'FU2612', NOW)['related_contracts'], [])

    def test_settings_reject_removed_reference_feature(self):
        with self.assertRaisesRegex(ValueError, '已移除'):
            self.save(contracts=[{'instrument': 'FU2611', 'related': ['SC2611']}])

    def test_legacy_settings_do_not_expand_collection_or_new_prediction(self):
        self.save()
        self.complete(topic='product:SC', rows=[evidence(product_code='SC')])
        with SessionLocal() as db:
            db.get(self.Settings, 'research-a').contracts_json = json.dumps([
                {'instrument': 'FU2611', 'related': ['SC2611']}])
            db.commit()
        saved = self.cache.get_settings('research-a')
        self.assertEqual(saved['contracts'], [{'instrument': 'FU2611', 'related': []}])
        self.assertEqual(self.cache.topics_for([
            {'instrument': 'FU2611', 'related': ['SC2611']}]), ['macro', 'product:FU'])
        frozen = self.cache.freeze_materials('research-a', 'FU2611', NOW)
        self.assertEqual(frozen['related_contracts'], [])
        self.assertEqual(frozen['related_contract_status'], [])
        self.assertEqual(frozen['sources'], [])

    def test_shared_body_is_queryable_for_each_checked_product(self):
        self.save()
        self.complete(rows=[evidence()])
        self.complete(topic='product:SC', rows=[evidence(product_code='SC')])
        listed = self.cache.list_materials('research-a', topic='product:SC')
        self.assertEqual(listed['total'], 1)
        self.assertEqual(listed['items'][0]['product_code'], 'SC')
        with SessionLocal() as db:
            self.assertEqual(db.query(self.Material).count(), 1)
            self.assertEqual(json.loads(db.query(self.Material).one().metadata_json)['product_code'], 'FU')

    def test_fixed_sources_publish_before_rule_or_ai_finishes(self):
        self.save()
        observed = []
        def rule(*args):
            frozen = self.cache.freeze_materials('research-a', 'FU2611', NOW)
            observed.extend(frozen['sources'])
            self.assertEqual(frozen['ai_derivations'], [])
            return rule_result()
        jobs = self.scheduler.enqueue_refresh('research-a')['jobs']
        job = next(j for j in jobs if j['topic'] == 'product:FU')
        with patch.object(self.scheduler.fixed_research, 'collect_fixed', return_value=fixed_result()), \
             patch.object(self.scheduler.fixed_research, 'collect_rule_search', side_effect=rule):
            self.scheduler.run_job(job['job_id'])
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.cache.freeze_materials('research-a', 'FU2611',
            NOW-timedelta(microseconds=1))['sources'], [])

    def test_periodic_verification_does_not_depend_on_collection_interval(self):
        row = evidence(dimension='macro', source_kind='OFFICIAL', latest_release=True,
                       latest_verified_at=NOW.isoformat(), version_fetched_at=NOW.isoformat())
        for interval in (5, 60, 1440):
            self.assertEqual(self.cache._invalid_reasons(row, NOW+timedelta(hours=3), interval), [])

    def test_verified_warehouse_freezes_keeps_unknown_time_and_expires(self):
        from tests.test_remaining_sources import RemainingSourceTests
        self.save()
        row = evidence(**RemainingSourceTests().warehouse(latest_verified_at=NOW.isoformat(),
            version_fetched_at=NOW.isoformat()))
        self.complete(rows=[row])
        frozen = self.cache.freeze_materials('research-a', 'FU2611', NOW)
        self.assertEqual(len(frozen['sources']), 1)
        self.assertEqual(frozen['sources'][0]['published_at'], 'UNKNOWN')
        self.assertEqual(frozen['sources'][0]['validity_basis'], 'OFFICIAL_VERIFIED_DATA_PERIOD')
        self.assertIn('UNKNOWN_PUBLICATION_TIME', frozen['sources'][0]['warnings'])
        self.assertEqual(self.cache.freeze_materials('research-a', 'FU2611', NOW+timedelta(hours=25))['sources'], [])
        self.assertEqual(self.cache.freeze_materials('research-a', 'FU2611', NOW-timedelta(seconds=1))['sources'], [])

    def test_reused_warehouse_body_keeps_measurements_from_new_check(self):
        from tests.test_remaining_sources import RemainingSourceTests
        self.save()
        row = evidence(**RemainingSourceTests().warehouse(latest_verified_at=NOW.isoformat()))
        self.complete(rows=[row])
        self.complete(rows=[dict(row, warehouse_quantity=550, warehouse_unit='吨',
                                 warehouse_totals=[{'quantity': 550, 'unit': '吨'}])])
        frozen = self.cache.freeze_materials('research-a', 'FU2611', NOW)
        self.assertEqual(frozen['sources'][0].get('warehouse_quantity'), 550)
        with SessionLocal() as db:
            self.assertEqual(db.query(self.Material).count(), 1)

    def test_fixed_sources_survive_rule_failure(self):
        self.save()
        job = self.complete(run=False)
        with patch.object(self.scheduler.fixed_research, 'collect_fixed', return_value=fixed_result()), \
             patch.object(self.scheduler.fixed_research, 'collect_rule_search', side_effect=RuntimeError('rule failed')):
            self.scheduler.run_job(job['job_id'])
        self.assertEqual(len(self.cache.freeze_materials('research-a', 'FU2611', NOW)['sources']), 1)

    def test_completed_ai_result_is_hidden_before_completion(self):
        job = SimpleNamespace(completed_at=NOW+timedelta(minutes=1), published=True,
            result_json=json.dumps({'material_refs': [{'material_id': 'ai'}],
                'ai_derivations': [{'facts': ['future fact']}],
                'fixed_snapshots': [{'publishable': True, 'published_at': NOW.isoformat(),
                                     'material_refs': [{'material_id': 'fixed'}]}]}))
        result = self.cache._published_result(job, NOW)
        self.assertEqual(result['material_refs'], [{'material_id': 'fixed'}])
        self.assertNotIn('ai_derivations', result)

    def test_periodic_report_update_deadline_is_independent_of_verification(self):
        row = evidence(dimension='macro', source_kind='OFFICIAL', latest_release=True,
                       latest_verified_at=NOW.isoformat(), version_fetched_at=NOW.isoformat(),
                       expected_update_by=(NOW-timedelta(days=1)).isoformat())
        self.assertIn('REPORT_UPDATE_OVERDUE', self.cache._invalid_reasons(row, NOW, 60))

    def test_historical_query_does_not_use_later_product_check(self):
        self.save()
        self.complete(rows=[evidence()])
        later = NOW + timedelta(hours=1)
        with patch.object(self.cache, '_now', return_value=later), \
             patch.object(self.scheduler, '_now', return_value=later):
            self.complete(topic='product:SC', rows=[evidence(product_code='SC')])
        self.assertEqual(self.cache.list_materials('research-a', topic='product:SC',
                         cutoff=NOW.isoformat())['total'], 0)

    def test_settings_validate_scope_and_optimistic_version(self):
        for contracts in ([{'instrument':'UNKNOWN2611','related':[]}],
                          [{'instrument':'FU2611','related':['FU2611']}],
                          [{'instrument':'FU2611','related':['SC2611','BU2611','RB2611','CU2611','AU2611','AG2611']}]):
            with self.assertRaises(ValueError): self.save(contracts=contracts)
        saved = self.save()
        self.assertEqual(saved['config_version'], 1)
        with self.assertRaises(RuntimeError): self.save()
        self.assertEqual(self.cache.get_settings('research-b')['config_version'], 0)

    def test_fixed_mode_saves_queues_and_runs_without_model(self):
        saved = self.save(enabled=True, model_config_id=None)
        self.assertTrue(saved['enabled'])
        self.assertFalse(saved['ai_enabled'])
        self.assertEqual(self.cache.get_status('research-a')['next_check_at'],
                         NOW.isoformat() + '+08:00')
        self.scheduler.schedule_due(NOW)
        with SessionLocal() as db:
            queued = db.query(self.Job).filter_by(status='QUEUED').all()
            self.assertEqual(len(queued), 4)
        jobs = self.scheduler.enqueue_refresh('research-a')['jobs']
        with patch('backend.ai_analysis.research_scheduler.fixed_research.collect_fixed', return_value=fixed_result()), \
             patch('backend.ai_analysis.research_scheduler.fixed_research.collect_rule_search', return_value=rule_result()):
            for job in jobs:
                self.scheduler.run_job(job['job_id'])
        with SessionLocal() as db:
            for row in db.query(self.Job):
                self.assertEqual(json.loads(row.calls_json), [], '固定模式不允许任何模型调用')
                self.assertEqual(json.loads(row.model_json), {})
                self.assertTrue(row.published)
        self.assertEqual(len(self.cache.freeze_materials('research-a', 'FU2611', NOW)['sources']), 1)

    def test_ai_enabled_requires_valid_owned_model(self):
        with self.assertRaises(ValueError):
            self.save(ai_enabled=True, model_config_id='missing-model')
        saved = self.save(ai_enabled=True)
        self.assertTrue(saved['ai_enabled'])
        disabled = self.save(ai_enabled=False, enabled=True, config_version=saved['config_version'])
        self.assertFalse(disabled['ai_enabled'])
        self.assertTrue(disabled['enabled'])

    def test_omitted_ai_enabled_preserves_saved_value_and_new_records_default_false(self):
        saved = self.save(ai_enabled=True, enabled=True)
        self.assertTrue(saved['ai_enabled'])
        # 旧客户端不提交 ai_enabled 字段：已有值保留
        legacy_body = settings(enabled=True, config_version=saved['config_version'])
        legacy_body.pop('ai_enabled')
        kept = self.cache.save_settings('research-a', legacy_body)
        self.assertTrue(kept['ai_enabled'])
        self.assertFalse(self.cache.get_settings('research-b')['ai_enabled'])
        # 迁移补列默认 false：模拟旧库新增列后的存量行
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text(
                "INSERT INTO ai_research_settings (owner_username, enabled, interval_minutes, "
                "contracts_json, config_version, updated_at) VALUES "
                "('legacy-user', 1, 60, '[]', 3, '2026-10-01 08:00:00')"))
            db.commit()
        legacy = self.cache.get_settings('legacy-user')
        self.assertTrue(legacy['enabled'])
        self.assertFalse(legacy['ai_enabled'])

    def test_duplicate_refresh_reuses_active_job_and_failure_waits_from_completion(self):
        self.save(enabled=True)
        first = self.scheduler.enqueue_refresh('research-a')['jobs']
        again = self.scheduler.enqueue_refresh('research-a')['jobs']
        self.assertEqual([j['job_id'] for j in first], [j['job_id'] for j in again])
        job = next(j for j in first if j['topic']=='product:FU')
        with patch('backend.ai_analysis.research_scheduler.fixed_research.collect_fixed',
                   side_effect=ValueError('network failed')):
            self.scheduler.run_job(job['job_id'])
        with SessionLocal() as db:
            row = db.query(self.Job).filter_by(job_id=job['job_id']).one()
            self.assertEqual(row.status, 'FAILED')
            self.assertEqual(row.next_check_at, NOW + timedelta(minutes=60))

    def test_failed_scheduled_job_alerts_via_notifier(self):
        self.save(enabled=True)
        jobs = self.scheduler.enqueue_refresh('research-a')['jobs']
        job = next(j for j in jobs if j['topic'] == 'product:FU')
        with patch('backend.ai_analysis.research_scheduler.fixed_research.collect_fixed',
                   side_effect=ValueError('network failed')), \
             patch('backend.ai_analysis.alerts.notifier.notify') as notify:
            self.scheduler.run_job(job['job_id'])
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args.args[0], 'AI_RESEARCH_JOB_FAILED')
        self.assertEqual(notify.call_args.args[3], 'ai_research')

    def test_expired_lease_tick_alerts_interruption(self):
        self.save(enabled=True)
        jobs = self.scheduler.enqueue_refresh('research-a')['jobs']
        job = next(j for j in jobs if j['topic'] == 'product:FU')
        with SessionLocal() as db:
            row = db.query(self.Job).filter_by(job_id=job['job_id']).one()
            row.status, row.lease_until = 'RUNNING', NOW - timedelta(seconds=1)
            db.commit()
        with patch('backend.ai_analysis.research_scheduler.fixed_research.collect_fixed', return_value=fixed_result()), \
             patch('backend.ai_analysis.research_scheduler.fixed_research.collect_rule_search', return_value=rule_result()), \
             patch('backend.ai_analysis.alerts.notifier.notify') as notify:
            self.scheduler.tick(NOW)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args.args[0], 'AI_RESEARCH_JOB_INTERRUPTED')

    def test_tick_without_interruptions_still_claims_queued_job(self):
        # 回归: 无中断任务时 tick 也必须走到领取队列 (job_id 曾因缩进错误未定义, 调度循环每2秒崩溃)
        self.save(enabled=True)
        self.scheduler.enqueue_refresh('research-a')['jobs']
        with patch('backend.ai_analysis.research_scheduler.fixed_research.collect_fixed', return_value=fixed_result()), \
             patch('backend.ai_analysis.research_scheduler.fixed_research.collect_rule_search', return_value=rule_result()), \
             patch('backend.ai_analysis.alerts.notifier.notify') as notify:
            self.scheduler.tick(NOW)
        with SessionLocal() as db:
            statuses = sorted(j.status for j in db.query(self.Job).all())
        self.assertEqual(notify.call_count, 0)
        self.assertEqual(statuses.count('COMPLETED'), 1)

    def test_versions_keep_body_changes_and_cutoff_rejects_later_revision(self):
        self.save()
        self.complete()
        frozen = self.cache.freeze_materials('research-a', 'FU2611', NOW)
        self.assertEqual(len(frozen['sources']), 1)
        first_id = frozen['sources'][0]['material_id']
        self.complete()  # identical body does not manufacture a new version
        with SessionLocal() as db: self.assertEqual(db.query(self.Material).count(), 1)
        later = NOW + timedelta(hours=1)
        with patch.object(self.cache, '_now', return_value=later), patch.object(self.scheduler,'_now',return_value=later):
            self.complete(rows=[evidence(body='燃料油库存修订：供应增加。', fetched_at=later.isoformat())])
        with SessionLocal() as db: self.assertEqual(db.query(self.Material).count(), 2)
        self.assertEqual(self.cache.freeze_materials('research-a','FU2611',NOW)['sources'][0]['material_id'], first_id)
        self.assertNotEqual(self.cache.freeze_materials('research-a','FU2611',later)['sources'][0]['material_id'], first_id)
        self.assertEqual(frozen['sources'][0]['excerpt'], '燃料油库存下降，供应减少。')
        with self.assertRaises(LookupError): self.cache.get_material('research-b', first_id)

    def test_subsecond_fetch_and_completion_preserve_strict_cutoff(self):
        fetched = NOW + timedelta(microseconds=250000)
        with patch.object(self.cache, '_now', return_value=fetched), \
             patch.object(self.scheduler, '_now', return_value=fetched):
            self.save()
            job = self.complete(rows=[evidence(fetched_at=fetched.isoformat())])
        with SessionLocal() as db:
            self.assertEqual(db.get(self.Settings, 'research-a').updated_at, fetched)
            self.assertEqual(db.query(self.Job).filter_by(job_id=job['job_id']).one().completed_at, fetched)
            material = db.query(self.Material).one()
            self.assertEqual(material.first_fetched_at, fetched)
            self.assertEqual(material.version_fetched_at, fetched)
        self.assertEqual(self.cache.freeze_materials('research-a', 'FU2611',
            NOW + timedelta(microseconds=100000))['sources'], [])
        self.assertEqual(len(self.cache.freeze_materials('research-a', 'FU2611', fetched)['sources']), 1)

    def test_old_report_new_fetch_is_background_and_no_new_result_keeps_cache(self):
        self.save()
        self.complete()
        self.complete(rows=[])
        self.assertEqual(len(self.cache.freeze_materials('research-a','FU2611',NOW)['sources']), 1)
        self.complete(rows=[evidence(published='2026-08-01',url='https://finance.eastmoney.com/a/old.html')])
        frozen = self.cache.freeze_materials('research-a','FU2611',NOW)
        self.assertEqual(len(frozen['sources']), 1)
        self.assertTrue(any(s['published_at'].startswith('2026-08-01') for s in frozen['background_sources']))

    def test_frozen_check_time_excludes_cancelled_jobs_and_retains_disabled_state(self):
        self.save()
        self.complete()
        cancelled_at = NOW + timedelta(seconds=1)
        with SessionLocal() as db:
            for job in db.query(self.Job).filter_by(status='QUEUED'):
                job.status = 'CANCELLED'; job.completed_at = cancelled_at; job.active_key = None
            db.commit()
        frozen = self.cache.freeze_materials('research-a', 'FU2611', cancelled_at)
        self.assertEqual(frozen['research']['last_check_at'], self.cache._iso(NOW))
        self.assertFalse(frozen['collection_enabled'])
        self.assertTrue(any(job['status'] == 'CANCELLED' for job in frozen['research']['jobs']))

    def test_running_old_version_or_disabled_job_does_not_publish(self):
        saved = self.save(enabled=True)
        jobs = self.scheduler.enqueue_refresh('research-a')['jobs']
        job = next(j for j in jobs if j['topic']=='product:FU')
        def finish(snapshot, topic, budget):
            self.cache.save_settings('research-a', settings(enabled=False, config_version=saved['config_version']))
            return fixed_result()
        with patch('backend.ai_analysis.research_scheduler.fixed_research.collect_fixed', side_effect=finish):
            self.scheduler.run_job(job['job_id'])
        self.assertEqual(self.cache.freeze_materials('research-a','FU2611',NOW)['sources'], [])
        with SessionLocal() as db:
            row = db.query(self.Job).filter_by(job_id=job['job_id']).one()
            self.assertFalse(row.published)
            self.assertEqual(row.status, 'COMPLETED')

    def test_restart_interrupts_inflight_and_leaves_only_one_due_check(self):
        self.save(enabled=True)
        jobs = self.scheduler.enqueue_refresh('research-a')['jobs']
        with SessionLocal() as db:
            row = db.query(self.Job).filter_by(job_id=jobs[0]['job_id']).one()
            row.status='RUNNING'; row.started_at=NOW-timedelta(hours=3); row.lease_until=NOW-timedelta(hours=1)
            db.commit()
        self.scheduler.recover_interrupted()
        with SessionLocal() as db:
            self.assertEqual(db.query(self.Job).filter_by(status='INTERRUPTED').count(), 4)
        self.scheduler.schedule_due(NOW+timedelta(days=2))
        self.scheduler.schedule_due(NOW+timedelta(days=2))
        with SessionLocal() as db: self.assertEqual(db.query(self.Job).filter_by(status='QUEUED').count(), 4)

    def test_macro_verification_expiry_uses_checks_not_original_fetch_time(self):
        self.save()
        macro = evidence(body='制造业PMI官方发布。', dimension='macro', product_code=None, scope='macro',
            source_kind='OFFICIAL', url='https://www.stats.gov.cn/sj/zxfb/macro.html',
            published='2026-08-01', latest_release=True, macro_series='PMI',
            series_id='NBS:PMI', latest_verified_at=NOW.isoformat())
        self.complete(topic='macro', rows=[macro])
        self.assertNotIn('macro',self.cache.freeze_materials('research-a','FU2611',NOW)['missing_dimensions'])
        expired = self.cache.freeze_materials('research-a','FU2611',NOW+timedelta(days=8))
        self.assertIn('macro', expired['missing_dimensions'])
        self.assertTrue(any('LATEST_RELEASE_UNVERIFIED' in s['warnings'] for s in expired['background_sources']))

    def test_concurrent_enqueue_and_config_saves_are_atomic(self):
        self.save()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _:self.scheduler.enqueue_refresh('research-a'), range(2)))
        self.assertEqual({j['job_id'] for j in responses[0]['jobs']}, {j['job_id'] for j in responses[1]['jobs']})
        def save_once(_):
            try:
                self.save(config_version=1)
                return 'SAVED'
            except RuntimeError:
                return 'CONFLICT'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(save_once, range(2))), ['SAVED','CONFLICT'])

    def test_disable_cancels_queued_and_manual_disabled_refresh_remains_allowed(self):
        saved = self.save(enabled=True)
        queued = self.scheduler.enqueue_refresh('research-a')['jobs']
        self.save(enabled=False, config_version=saved['config_version'])
        with patch('backend.ai_analysis.research_scheduler.fixed_research.collect_fixed', return_value=fixed_result()) as run:
            for job in queued: self.scheduler.run_job(job['job_id'])
            run.assert_not_called()
        self.complete()
        self.assertEqual(len(self.cache.freeze_materials('research-a','FU2611',NOW)['sources']),1)

    def test_order_only_save_does_not_make_completed_topic_due(self):
        saved = self.save(enabled=True)
        for topic in self.cache.topics_for(saved['contracts']): self.complete(topic=topic, rows=[])
        self.save(enabled=True, config_version=1, contracts=list(reversed(saved['contracts'])))
        self.scheduler.schedule_due(NOW+timedelta(minutes=1))
        with SessionLocal() as db: self.assertEqual(db.query(self.Job).filter_by(status='QUEUED').count(),0)

    def test_independent_sc_sources_do_not_enter_fu_prediction(self):
        self.save()
        self.complete(topic='product:SC', rows=[evidence(product_code='SC',published='2026-09-22')])
        material = self.cache.freeze_materials('research-a','FU2611',NOW)
        self.assertIn('fundamentals',material['missing_dimensions'])
        self.assertEqual(material['sources'], [])

    def test_macro_new_release_invalidates_previous_url_even_if_new_body_unavailable(self):
        self.save()
        old = evidence(dimension='macro',product_code=None,scope='macro',source_kind='OFFICIAL',
            macro_series='PMI', series_id='NBS:PMI', latest_release=True,
            latest_verified_at=NOW.isoformat())
        self.complete(topic='macro',rows=[old])
        jobs = self.scheduler.enqueue_refresh('research-a')['jobs']
        job = next(j for j in jobs if j['topic']=='macro')
        fixed = fixed_result(rows=[], latest_release_checks=[
            {'source_id':'NBS_LATEST_RELEASE','macro_series':'PMI','series_id':'NBS:PMI',
             'url':'https://www.stats.gov.cn/new-pmi.html','latest_release':True,
             'verified_at':NOW.isoformat(),'status':'COMPLETED'}])
        with patch('backend.ai_analysis.research_scheduler.fixed_research.collect_fixed', return_value=fixed), \
             patch('backend.ai_analysis.research_scheduler.fixed_research.collect_rule_search', return_value=rule_result()):
            self.scheduler.run_job(job['job_id'])
        frozen = self.cache.freeze_materials('research-a','FU2611',NOW)
        self.assertIn('macro',frozen['missing_dimensions'])
        self.assertTrue(any('SUPERSEDED_OFFICIAL_RELEASE' in s['warnings'] for s in frozen['background_sources']))

    def test_failed_fixed_sources_fail_job_and_preserve_valid_cache(self):
        self.save()
        self.complete()
        job = next(j for j in self.scheduler.enqueue_refresh('research-a')['jobs'] if j['topic']=='product:FU')
        fixed = fixed_result(rows=[], status='FAILED',
                             sources_checked=[{'source_id': 'S1', 'status': 'FAILED', 'reason': 'HTTP 403'},
                                              {'source_id': 'S2', 'status': 'FAILED', 'reason': '请求超时'}])
        with patch('backend.ai_analysis.research_scheduler.fixed_research.collect_fixed', return_value=fixed), \
             patch('backend.ai_analysis.research_scheduler.fixed_research.collect_rule_search',
                   return_value=rule_result(status='FAILED')):
            self.scheduler.run_job(job['job_id'])
        with SessionLocal() as db:
            row = db.query(self.Job).filter_by(job_id=job['job_id']).one()
            self.assertEqual(row.status,'FAILED')
            self.assertEqual(json.loads(row.result_json)['outcome'],'FAILED')
            self.assertFalse(row.published)
            self.assertEqual(row.next_check_at,NOW+timedelta(minutes=60))
        self.assertEqual(len(self.cache.freeze_materials('research-a','FU2611',NOW)['sources']),1)

    def test_frozen_evidence_limits_and_contract_scope(self):
        self.save()
        rows = [evidence(url=f'https://finance.eastmoney.com/a/{i}.html',
                         body=f'第{i}份报告：' + '库存减少。'*600)
                for i in range(8)]
        rows.append(evidence(url='https://finance.eastmoney.com/a/contract.html',
                             body='合约专项：' + '库存减少。'*600,
                             scope='contract', applicable_contracts=['FU2612']))
        self.complete(rows=rows)
        frozen = self.cache.freeze_materials('research-a','FU2611',NOW)
        self.assertEqual(len(frozen['sources']),6)
        self.assertEqual(frozen['truncated_count'],2)
        self.assertTrue(all(len(s['excerpt'])<=2000 for s in frozen['sources']))

    def test_status_next_check_is_immediate_for_initial_enabled_configuration(self):
        self.save(enabled=True)
        self.assertEqual(self.cache.get_status('research-a')['next_check_at'],NOW.isoformat()+'+08:00')

    def test_readded_topic_is_due_and_interval_changes_match_status(self):
        saved = self.save(enabled=True)
        for topic in self.cache.topics_for(saved['contracts']): self.complete(topic=topic, rows=[])
        narrow = [{'instrument':'FU2611','related':[]}]
        self.save(enabled=True,config_version=1,contracts=narrow,interval_minutes=120)
        self.assertEqual(self.cache.get_status('research-a')['next_check_at'],(NOW+timedelta(minutes=120)).isoformat()+'+08:00')
        self.save(enabled=True,config_version=2,contracts=saved['contracts'],interval_minutes=120)
        self.assertEqual(self.cache.get_status('research-a')['next_check_at'],NOW.isoformat()+'+08:00')
        self.scheduler.schedule_due(NOW)
        with SessionLocal() as db:
            self.assertEqual({r.topic for r in db.query(self.Job).filter_by(status='QUEUED')},{'product:SC','product:BU'})

    def test_periodic_registered_series_usable_without_ai_and_related_crude_never_fills_fu(self):
        self.save()
        eia = evidence(body='US commercial crude oil inventories increased 0.9 million barrels.',
            published='2026-09-05', dimension='related_markets', product_code='FU', scope='product',
            source_kind='OFFICIAL', url='https://ir.eia.gov/wpsr/summary.txt',
            series_id='EIA:WPSR', latest_release=True, latest_verified_at=NOW.isoformat(),
            data_period='Week ending 2026-09-05', freshness_policy='LATEST_VERIFIED_RELEASE')
        self.complete(rows=[eia])
        frozen = self.cache.freeze_materials('research-a', 'FU2611', NOW)
        # 旧版本错误的 EIA 发布时间也必须在读取时纠正；不能用 week-ending 冒充发布时间。
        self.assertEqual(len(frozen['sources']), 0)
        self.assertEqual(frozen['background_sources'][0]['dimension'], 'related_markets')
        self.assertEqual(frozen['background_sources'][0]['data_period'], 'Week ending 2026-09-05')
        self.assertEqual(frozen['background_sources'][0]['published_at'], 'UNKNOWN')
        self.assertIn('UNKNOWN_PUBLICATION_TIME', frozen['background_sources'][0]['warnings'])
        # 只有相关原油证据时，FU 自身基本面不得标为充分
        self.assertIn('fundamentals', frozen['missing_dimensions'])
        self.assertIn('fundamentals', frozen['coverage_gaps'])
        # 核验有效期独立于采集频率。
        expired = self.cache.freeze_materials('research-a', 'FU2611', NOW + timedelta(days=8))
        self.assertIn('related_markets', expired['missing_dimensions'])
        self.assertTrue(any('LATEST_RELEASE_UNVERIFIED' in s['warnings']
                            for s in expired['background_sources']))
        # 非注册序列的旧新闻不享受周期例外
        self.complete(rows=[evidence(published='2026-08-01',
                                     url='https://finance.eastmoney.com/a/oldnews.html')])
        frozen2 = self.cache.freeze_materials('research-a', 'FU2611', NOW)
        self.assertTrue(all(s['dimension'] != 'fundamentals' for s in frozen2['sources']))
        self.assertTrue(any(s['published_at'].startswith('2026-08-01')
                            for s in frozen2['background_sources']))

    def test_material_list_is_owner_scoped_filtered_and_bodyless(self):
        self.save()
        macro = evidence(title='制造业PMI官方发布', body='制造业PMI官方发布。',
            dimension='macro', product_code=None, scope='macro', source_kind='OFFICIAL',
            url='https://www.stats.gov.cn/sj/zxfb/macro.html', published='2026-09-30',
            latest_release=True, macro_series='PMI', series_id='NBS:PMI',
            latest_verified_at=NOW.isoformat(), collection_method='FIXED_SOURCE')
        self.complete(rows=[evidence()])
        self.complete(topic='macro', rows=[macro])
        listed = self.cache.list_materials('research-a')
        self.assertEqual(listed['total'], 2)
        self.assertEqual(listed['page'], 1)
        self.assertTrue(all('body' not in item and 'body' not in json.dumps(item) for item in listed['items']))
        self.assertTrue(all(len(item['excerpt']) <= 300 for item in listed['items']))
        self.assertEqual(listed['items'][0]['series_id'], 'NBS:PMI', '按版本获取时间倒序')
        # 主题过滤：macro / product
        self.assertEqual(self.cache.list_materials('research-a', topic='macro')['total'], 1)
        self.assertEqual(self.cache.list_materials('research-a', topic='product:FU')['total'], 1)
        # 维度、来源方式、序列过滤
        self.assertEqual(self.cache.list_materials('research-a', dimension='macro')['total'], 1)
        self.assertEqual(self.cache.list_materials('research-a', method='FIXED_SOURCE')['total'], 1)
        self.assertEqual(self.cache.list_materials('research-a', series='NBS:PMI')['total'], 1)
        # 关键词命中标题
        self.assertEqual(self.cache.list_materials('research-a', q='库存周报')['total'], 1)
        self.assertEqual(self.cache.list_materials('research-a', q='不存在的关键词xyz')['total'], 0)
        # 所有者隔离
        self.assertEqual(self.cache.list_materials('research-b')['total'], 0)
        with self.assertRaises(ValueError):
            self.cache.list_materials('research-a', topic='bogus')

    def test_dimension_filter_follows_stored_classification_not_history_refs(self):
        # 回归: 资料曾被历史批次以其他维度引用时, 维度筛选仍按存储分类判定,
        # 展示徽章也不被引用/冻结上下文改写 (相关市场不得混入基本面筛选)
        self.save()
        related = evidence(body='美股收盘：市场焦点转向财报季。', dimension='related_markets',
                           url='https://finance.eastmoney.com/a/related-us.html')
        self.complete(rows=[evidence()])
        self.complete(rows=[related])
        with SessionLocal() as db:
            job = db.query(self.Job).filter_by(topic='product:FU').order_by(
                self.Job.id.desc()).first()
            result = json.loads(job.result_json)
            ref = next(r for r in result['material_refs'] if r['material_id'])
            self.assertEqual(ref['dimension'], 'related_markets')
            ref['dimension'] = 'fundamentals'  # 模拟历史批次把它按基本面引用
            job.result_json = json.dumps(result, ensure_ascii=False)
            db.commit()
        self.assertEqual(self.cache.list_materials('research-a', dimension='fundamentals')['total'], 1)
        listed = self.cache.list_materials('research-a', dimension='related_markets')
        self.assertEqual(listed['total'], 1)
        self.assertEqual(listed['items'][0]['dimension'], 'related_markets')

    def test_manual_material_entry_creates_published_check_and_dedups(self):
        self.save()
        now = NOW + timedelta(hours=2)
        record = {'target': 'FU2611', 'dimension': 'fundamentals', 'title': '手工录入库存周报',
                  'body': '手工录入正文：燃油库存下降。', 'published_at': NOW.isoformat()}
        result = self.cache.persist_manual_materials('research-a', [record], now)
        self.assertEqual(result['inserted'], 1)
        listed = self.cache.list_materials('research-a', method='MANUAL', cutoff=now.isoformat())
        self.assertEqual(listed['total'], 1)
        self.assertEqual(listed['items'][0]['collection_method'], 'MANUAL')
        # 已发布的手工检查使资料进入预测冻结, 且作为目标自身证据
        frozen = self.cache.freeze_materials('research-a', 'FU2611', now + timedelta(hours=1))
        manual = [s for s in frozen['sources'] if s.get('collection_method') == 'MANUAL']
        self.assertEqual(len(manual), 1)
        self.assertTrue(manual[0]['is_target_evidence'])
        # 重复录入同一内容: 跳过且不再生成新检查
        with SessionLocal() as db:
            jobs_before = db.query(self.Job).count()
        again = self.cache.persist_manual_materials('research-a', [record], now + timedelta(hours=3))
        self.assertEqual(again['inserted'], 0)
        with SessionLocal() as db:
            self.assertEqual(db.query(self.Job).count(), jobs_before)
        # 公共宏观目标只能宏观维度; 未配置合约拒绝; 无链接时合成稳定地址
        with self.assertRaises(ValueError):
            self.cache.persist_manual_materials('research-a', [
                {'target': 'macro', 'dimension': 'fundamentals', 'title': 'x', 'body': 'y'}], now)
        with self.assertRaises(ValueError):
            self.cache.persist_manual_materials('research-a', [
                {'target': 'CU2611', 'dimension': 'fundamentals', 'title': 'x', 'body': 'y'}], now)
        self.cache.persist_manual_materials('research-a', [
            {'target': 'macro', 'dimension': 'macro', 'title': '央行公告', 'body': '公开数据正文。'}], now)
        self.assertEqual(self.cache.list_materials('research-a', topic='macro', method='MANUAL',
            cutoff=now.isoformat())['total'], 1)

    def test_financial_index_exempts_commodity_fundamentals_gap(self):
        # C2 口径修正: 金融股指 (IH/IF/IC/IM) 无商品式供需库存基本面,
        # fundamentals 不计入覆盖缺口; 商品期货不受豁免影响
        with SessionLocal() as db:
            if not db.query(InstrumentModel).filter_by(symbol='IH2610').first():
                db.add(InstrumentModel(symbol='IH2610', name='上证50指数期货2610', exchange='CFFEX'))
                db.commit()
        self.save(contracts=[{'instrument': 'IH2610', 'related': []}, {'instrument': 'FU2611', 'related': []},
                             {'instrument': 'FU2612', 'related': []}, {'instrument': 'SC2611', 'related': []},
                             {'instrument': 'BU2611', 'related': []}])
        self.complete(topic='product:IH', rows=[
            evidence(body='政策事件落地，市场流动性充裕，成交活跃。', dimension='events',
                     product_code='IH', scope='product'),
            evidence(body='相关市场方面，海外股指普涨。', dimension='related_markets',
                     product_code='IH', scope='product',
                     url='https://finance.eastmoney.com/a/ih-related.html'),
            evidence(title='宏观制造业PMI官方发布', body='制造业PMI官方发布50.2。', dimension='macro',
                     source_kind='OFFICIAL', latest_release=True, latest_verified_at=NOW.isoformat(),
                     version_fetched_at=NOW.isoformat(), product_code=None, scope='macro',
                     url='https://www.stats.gov.cn/sj/zxfb/macro-ih.html')])
        frozen = self.cache.freeze_materials('research-a', 'IH2610', NOW)
        self.assertNotIn('fundamentals', frozen['coverage_gaps'])
        self.assertEqual(frozen['cache_state'], 'AVAILABLE')
        fu_frozen = self.cache.freeze_materials('research-a', 'FU2611', NOW)
        self.assertIn('fundamentals', fu_frozen['coverage_gaps'])

    def test_material_export_includes_body_and_import_fields(self):
        self.save()
        self.complete(rows=[evidence()])
        self.cache.persist_manual_materials('research-a', [
            {'target': 'FU2611', 'dimension': 'fundamentals', 'title': '手工录入库存周报',
             'body': '手工录入正文：燃油库存下降。', 'published_at': NOW.isoformat()}], NOW)
        exported = self.cache.export_materials('research-a', instrument='FU2611')
        self.assertGreaterEqual(exported['exported'], 2)
        record = next(r for r in exported['records'] if r['title'] == '手工录入库存周报')
        self.assertEqual(record['body'], '手工录入正文：燃油库存下降。')
        self.assertEqual(record['target'], 'FU2611')
        self.assertEqual(record['dimension'], 'fundamentals')
        self.assertIn('url', record)
        self.assertTrue(str(record['published_at']).startswith('2026-10-02T19:00:00'))

    def test_material_list_pagination_and_version_count(self):
        from types import SimpleNamespace
        from backend.ai_analysis import research_cache as cache
        self.save()
        rows = [evidence(url=f'https://finance.eastmoney.com/a/p{i}.html',
                         body=f'燃料油第{i}页资料：库存变化。') for i in range(25)]
        revised = evidence(body='燃料油修订版：库存上调。')
        rows.append(revised)
        job = SimpleNamespace(owner_username='research-a', job_id='list-job')
        with SessionLocal() as db:
            cache.persist_sources(db, job, rows, NOW)
            db.commit()
            cache.persist_sources(db, job, [evidence(body='燃料油初版：库存下降。')], NOW)
            db.commit()
        page1 = self.cache.list_materials('research-a', page=1, page_size=20)
        page2 = self.cache.list_materials('research-a', page=2, page_size=20)
        self.assertEqual((page1['total'], len(page1['items'])), (26, 20))
        self.assertEqual((page2['total'], len(page2['items'])), (26, 6))
        counted = {item['material_id']: item for item in page1['items'] + page2['items']}
        revised_items = [item for item in counted.values() if item['url'] == revised['url']]
        self.assertEqual(revised_items[0]['version_count'], 2, '同一 URL 的修订版本计数')
        history = self.cache.list_materials('research-a', history_for=revised_items[0]['material_id'])
        self.assertEqual(history['total'], 2)
        self.assertEqual(self.cache.list_materials('research-a', history_for=revised_items[0]['material_id'],
            cutoff=(NOW-timedelta(seconds=1)).isoformat())['total'], 0)

    def test_restart_schedules_one_fresh_check_for_due_interrupted_topics(self):
        self.save(enabled=True)
        original = {j['job_id'] for j in self.scheduler.enqueue_refresh('research-a')['jobs']}
        self.scheduler.recover_interrupted()
        self.scheduler.schedule_due(NOW)
        self.scheduler.schedule_due(NOW)
        with SessionLocal() as db:
            fresh = db.query(self.Job).filter_by(status='QUEUED').all()
            self.assertEqual(len(fresh),4)
            self.assertTrue(original.isdisjoint({j.job_id for j in fresh}))


if __name__ == '__main__':
    unittest.main()
