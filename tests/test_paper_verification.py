import json
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

from tests import test_bootstrap  # noqa: F401
from backend.ai_analysis import paper_verification as service
from backend.ai_analysis.paper_verification_models import VerificationPlan, VerificationTrial
from backend.models.database import AIAnalysisBatchModel, AIAnalysisRunModel, AIAnalysisCallModel
from backend.storage.mysql_client import SessionLocal
from backend.storage.redis_client import RedisClient


MODEL_ID = '7336359a-08a9-435d-b40f-94d462303a26'
SESSION = {'session': 'DAY', 'label': '日盘第二节', 'status': 'ACTIVE',
           'start_time': '2026-10-08T10:30:00', 'end_time': '2026-10-08T11:30:00'}


class VerificationTests(unittest.TestCase):
    def test_auto_paper_default_and_report_live_pnl_are_read_only(self):
        plan=self.create(outcome=True,auto=True)
        self.assertEqual(plan['config']['execution_mode'],'AUTO_PAPER')
        self.assertEqual(plan['config']['signal_timeout_minutes'],5)
        self.assertEqual(plan['config']['position_timeout_minutes'],10)
        self.submit(plan)
        self.finish_run('batch-test')
        self.quote('2026-10-08T10:45:12')
        service.tick(datetime(2026,10,8,10,45,12))
        self.quote('2026-10-08T10:46:12',last=104,bid=103,ask=104)
        with SessionLocal() as db:
            before=db.query(VerificationTrial).one().state_json
        with patch.object(service,'dispatch_analysis') as dispatch:
            report=service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,46,12))
            dispatch.assert_not_called()
        row=report['trials'][0]
        self.assertEqual(row['live_pnl']['trading_net_pnl'],6)
        self.assertEqual(report['live']['open_count'],1)
        self.assertEqual(report['live']['holding_net_pnl'],8)
        self.assertEqual(report['report']['incurred_trading_cost'],2)
        self.assertEqual(report['report']['cash_net_pnl'],report['report']['total_net_pnl']-2)
        self.assertEqual(report['live']['estimated_total_net_pnl'],report['report']['total_net_pnl']+6)
        self.assertEqual(row['state']['entry_deadline_at'],'2026-10-08T10:50:10')
        self.assertEqual(row['state']['position_deadline_at'],'2026-10-08T10:55:10')
        with SessionLocal() as db:
            self.assertEqual(db.query(VerificationTrial).one().state_json,before)

    def test_auto_paper_unfilled_signal_retries_after_five_minutes(self):
        plan=self.create(outcome=True,auto=True)
        self.submit(plan)
        self.finish_run('batch-test')
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'next'}) as dispatch:
            service.tick(datetime(2026,10,8,10,45,12))
            service.tick(datetime(2026,10,8,10,50,9))
            dispatch.assert_not_called()
            service.tick(datetime(2026,10,8,10,50,10))
            self.assertEqual(dispatch.call_count,1)
        self.assertTrue(service.get_report('alice',plan['plan_id'])['trials'][0]['reason'].startswith('SIGNAL_TIMEOUT'))

    def test_auto_upgrade_preserves_terminal_ledger_and_separates_open_deadline(self):
        plan=self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        self.quote('2026-10-08T10:45:12')
        service.tick(datetime(2026,10,8,10,45,12))
        with SessionLocal() as db:
            db.add(VerificationTrial(trial_id='old-auto-terminal',plan_id=plan['plan_id'],trading_day='20261008',
                session_label='old',session_start=datetime(2026,10,8,10,30),session_end=datetime(2026,10,8,11,30),
                analysis_due_at=datetime(2026,10,8,10,44),status='SKIPPED',reason='old',state_json='{"old":"preserved"}'))
            db.commit()
        with patch.object(service,'dispatch_analysis') as dispatch:
            result=service.upgrade_auto_paper_rules('alice',plan['plan_id'],now=datetime(2026,10,8,10,46))
            dispatch.assert_not_called()
        self.assertEqual(result['config']['execution_mode'],'AUTO_PAPER')
        with SessionLocal() as db:
            self.assertEqual(db.query(VerificationTrial).filter_by(trial_id='old-auto-terminal').one().state_json,'{"old":"preserved"}')
        self.quote('2026-10-08T10:51:12',last=104,bid=103,ask=104)
        with patch.object(service,'dispatch_analysis') as dispatch:
            service.tick(datetime(2026,10,8,10,51,12))
            dispatch.assert_not_called()
        row=next(r for r in service.get_report('alice',plan['plan_id'])['trials'] if r['status']=='OPEN')
        self.assertEqual(row['state']['signal_deadline_at'],'2026-10-08T10:55:10')
        self.assertEqual(row['state']['entry_time'],'2026-10-08T10:45:12')

    def test_live_pnl_rejects_another_trading_day_and_does_not_hide_realized_book(self):
        plan=self.create(outcome=True,auto=True)
        self.submit(plan)
        self.finish_run('batch-test')
        self.quote('2026-10-08T10:45:12')
        service.tick(datetime(2026,10,8,10,45,12))
        self.quote('2026-10-08T10:46:12')
        self.redis.hset('quote:sina:FU2611',mapping={'trading_day':'20261009'})
        report=service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,46,12))
        self.assertEqual(report['trials'][0]['live_pnl']['status'],'STALE_OR_MISSING')
        self.assertEqual(report['report']['incurred_trading_cost'],2)
        self.assertIsNone(report['live']['trading_net_pnl'])
        self.assertIsNone(report['live']['estimated_total_net_pnl'])
        self.assertEqual(report['trials'][0]['status'],'OPEN')

    def test_incurred_commission_survives_unverifiable_exit(self):
        plan = self.create(outcome=True,auto=True)
        self.submit(plan)
        self.finish_run('batch-test')
        self.quote('2026-10-08T10:45:12')
        service.tick(datetime(2026,10,8,10,45,12))
        with SessionLocal() as db:
            row = db.query(VerificationTrial).one()
            row.status = 'UNVERIFIABLE'
            row.reason = 'MISSING_SESSION_END_QUOTE'
            db.commit()
        report = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,11,30))
        self.assertEqual(report['live']['open_count'],0)
        self.assertEqual(report['report']['incurred_trading_cost'],2)
        self.assertEqual(report['report']['cash_net_pnl'],report['report']['total_net_pnl']-2)
        self.assertEqual(report['live']['estimated_total_net_pnl'],report['report']['cash_net_pnl'])

    def test_completed_ten_minute_chart_is_accepted_and_tampering_rejected(self):
        from backend.ai_analysis.session_window import chart_for_completion
        for tamper in (False, True):
            with self.subTest(tamper=tamper):
                self.setUp()
                plan = self.create(outcome=True)
                self.submit(plan)
                self.finish_run('batch-test')
                with SessionLocal() as db:
                    run = db.query(AIAnalysisRunModel).one()
                    parsed = json.loads(run.parsed_response_json)
                    parsed['session_chart'] = chart_for_completion(parsed['session_chart'], run.completed_at)
                    if tamper:
                        parsed['session_chart']['evidence_cutoff_time'] = '2026-10-08T10:44:00'
                    run.parsed_response_json = json.dumps(parsed)
                    db.commit()
                self.quote('2026-10-08T10:45:12')
                service.tick(datetime(2026,10,8,10,45,12))
                row = service.get_report('alice',plan['plan_id'])['trials'][0]
                self.assertEqual(row['status'], 'SKIPPED' if tamper else 'OPEN')
                if not tamper:
                    self.assertEqual(row['state']['outcome_policy'], 'ACTUAL_NET')

    def test_rejects_trigger_already_crossed_at_frozen_reference(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        with SessionLocal() as db:
            run = db.query(AIAnalysisRunModel).one()
            parsed = json.loads(run.parsed_response_json)
            parsed['session_chart']['trigger_price'] = 95
            run.parsed_response_json = json.dumps(parsed)
            db.commit()
        self.quote('2026-10-08T10:45:12')
        service.tick(datetime(2026,10,8,10,45,12))
        row = service.get_report('alice', plan['plan_id'])['trials'][0]
        self.assertEqual(row['status'], 'SKIPPED')
        self.assertNotIn('entry_price', row['state'])
        self.assertIn('触发', row['reason'])

    def test_delayed_scan_accepts_fresh_trigger_quote_within_deadline(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        service.tick(datetime(2026,10,8,10,45,12))
        self.quote('2026-10-08T10:55:09')
        with patch.object(service, 'dispatch_analysis', return_value={'batch_id':'unexpected-next'}) as dispatch:
            service.tick(datetime(2026,10,8,10,55,11))
            dispatch.assert_not_called()
        report = service.get_report('alice', plan['plan_id'])
        self.assertEqual(report['trials'][0]['status'], 'OPEN')
        self.assertEqual(report['trials'][0]['state']['entry_time'], '2026-10-08T10:55:09')
        self.assertEqual(report['report']['signal_timeout_losses'], 0)

    def test_rejects_long_trigger_equal_to_frozen_reference(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        with SessionLocal() as db:
            run = db.query(AIAnalysisRunModel).one()
            parsed = json.loads(run.parsed_response_json)
            parsed['session_chart']['trigger_price'] = 99
            run.parsed_response_json = json.dumps(parsed)
            db.commit()
        service.tick(datetime(2026,10,8,10,45,12))
        self.assertEqual(service.get_report('alice',plan['plan_id'])['trials'][0]['status'], 'SKIPPED')

    def test_short_trigger_must_be_below_frozen_reference(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        with SessionLocal() as db:
            run = db.query(AIAnalysisRunModel).one()
            parsed = json.loads(run.parsed_response_json)
            parsed['direction'] = 'SHORT'
            parsed['session_chart'].update(direction='SHORT', trigger_price=101,
                target_price=90, invalidation_price=110)
            run.parsed_response_json = json.dumps(parsed)
            db.commit()
        self.quote('2026-10-08T10:45:12')
        service.tick(datetime(2026,10,8,10,45,12))
        self.assertEqual(service.get_report('alice',plan['plan_id'])['trials'][0]['status'], 'SKIPPED')

    def test_stop_before_signal_deadline_never_counts_timeout_loss(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        service.tick(datetime(2026,10,8,10,45,12))
        service.stop_plan('alice', plan['plan_id'], now=datetime(2026,10,8,10,55,9))
        self.quote('2026-10-08T10:55:09')
        service.tick(datetime(2026,10,8,10,55,11))
        report = service.get_report('alice', plan['plan_id'])
        self.assertEqual(report['plan']['status'], 'STOPPED')
        self.assertEqual(report['trials'][0]['reason'], 'STOPPED')
        self.assertEqual(report['report']['losses'], 0)
        self.assertNotIn('entry_price', report['trials'][0]['state'])

    def test_report_exposes_ai_basis_for_rejected_result_without_model_dispatch(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test', publication='DRAFT_ONLY')
        with SessionLocal() as db:
            run = db.query(AIAnalysisRunModel).one()
            parsed = json.loads(run.parsed_response_json)
            parsed.update(summary='等待突破，警惕回落', confidence_score=68,
                          risks=['量能不足'], entry_condition='向上突破100',
                          invalidation_condition='跌破90', review_status='FAILED_DRAFT_RETAINED',
                          evidence=[{'side':'LONG', 'time':'2026-10-08T10:44:00',
                                     'field':'close', 'value':99, 'reason':'接近压力'}],
                          api_key='never-export-this')
            run.parsed_response_json = json.dumps(parsed)
            db.commit()
        service.tick(datetime(2026,10,8,10,45,12))
        with patch.object(service, 'dispatch_analysis') as dispatch:
            report = service.get_report('alice', plan['plan_id'])
            dispatch.assert_not_called()
        self.assertEqual(report['trials'][0]['status'], 'SKIPPED')
        result = report['trials'][0]['ai_result']
        self.assertEqual(result['summary'], '等待突破，警惕回落')
        self.assertEqual(result['confidence_score'], 68)
        self.assertEqual(result['publication_status'], 'DRAFT_ONLY')
        self.assertEqual(result['evidence'][0]['reason'], '接近压力')
        self.assertNotIn('never-export-this', json.dumps(report))

    def test_ai_output_read_is_resilient_and_checks_batch_owner(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        self.alter_run(parsed_response_json='[invalid json')
        report = service.get_report('alice',plan['plan_id'])
        self.assertIn('格式无效', report['trials'][0]['ai_result']['error'])
        with SessionLocal() as db:
            db.query(AIAnalysisBatchModel).one().owner_username = 'bob'
            db.commit()
        report = service.get_report('alice',plan['plan_id'])
        self.assertIsNone(report['trials'][0]['ai_result'])

    def test_ai_output_ignores_overflowed_numeric_fields(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        self.alter_run(parsed_response_json=json.dumps({'summary':'仍可读取的结论',
            'confidence_score':10**400, 'evidence':[{'reason':'异常数字', 'value':10**400}]}))
        result = service.get_report('alice',plan['plan_id'])['trials'][0]['ai_result']
        self.assertEqual(result['summary'], '仍可读取的结论')
        self.assertNotIn('confidence_score',result)
        self.assertNotIn('value',result['evidence'][0])

    def test_valid_short_trigger_enters_and_missing_reference_is_rejected(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        with SessionLocal() as db:
            run = db.query(AIAnalysisRunModel).one()
            parsed = json.loads(run.parsed_response_json)
            parsed['direction'] = 'SHORT'
            parsed['session_chart'].update(direction='SHORT', trigger_price=98,
                target_price=90, invalidation_price=110)
            run.parsed_response_json = json.dumps(parsed)
            batch = db.query(AIAnalysisBatchModel).one()
            snapshot = json.loads(batch.snapshot_json)
            del snapshot['session_forecast']['reference_price']
            batch.snapshot_json = json.dumps(snapshot)
            db.commit()
        service.tick(datetime(2026,10,8,10,45,12))
        self.assertEqual(service.get_report('alice',plan['plan_id'])['trials'][0]['status'], 'SKIPPED')
        with SessionLocal() as db:
            row = db.query(VerificationTrial).one()
            row.status = 'SUBMITTED'
            row.state_json = json.dumps({'result_driven':True})
            batch = db.query(AIAnalysisBatchModel).one()
            snapshot['session_forecast']['reference_price'] = 99
            batch.snapshot_json = json.dumps(snapshot)
            db.commit()
        self.quote('2026-10-08T10:45:14',last=98,bid=97,ask=98)
        service.tick(datetime(2026,10,8,10,45,14))
        self.assertEqual(service.get_report('alice',plan['plan_id'])['trials'][0]['status'], 'OPEN')

    def test_activity_before_first_analysis_has_next_time_without_dispatch(self):
        plan = self.create(outcome=True)
        with patch.object(service,'dispatch_analysis') as dispatch:
            activity = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10))['activity']
            dispatch.assert_not_called()
        self.assertEqual(activity['phase'],'WAITING_NEXT_ANALYSIS')
        self.assertEqual(activity['next_at'],'2026-10-08T10:45:00')
        self.assertIn('等待',activity['current_action'])

    def test_activity_model_pending_and_waiting_for_entry_are_distinct(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        activity = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,46))['activity']
        self.assertEqual(activity['phase'],'ANALYZING')
        self.assertEqual(activity['next_at'],'2026-10-08T10:55:00')
        overdue = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,56))['activity']
        self.assertEqual(overdue['phase'],'ANALYSIS_OVERDUE')
        self.assertIsNone(overdue['next_at'])
        self.finish_run('batch-test')
        service.tick(datetime(2026,10,8,10,45,12))
        activity = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,46))['activity']
        self.assertEqual(activity['phase'],'WAITING_TRIGGER')
        self.assertEqual(activity['next_at'],'2026-10-08T10:55:10')
        overdue = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,56))['activity']
        self.assertEqual(overdue['phase'],'SIGNAL_OVERDUE')
        self.assertIsNone(overdue['next_at'])

    def test_activity_stop_never_promises_another_analysis(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        report = service.stop_plan('alice',plan['plan_id'],now=datetime(2026,10,8,10,46))
        self.assertEqual(report['activity']['phase'],'STOPPING')
        self.assertIsNone(report['activity']['next_at'])
        with SessionLocal() as db:
            row = db.query(VerificationPlan).filter_by(plan_id=plan['plan_id']).one()
            row.status = 'STOPPED'
            db.commit()
        activity = service.get_report('alice',plan['plan_id'])['activity']
        self.assertEqual(activity['phase'],'STOPPED')
        self.assertIn('没有新的分析',activity['next_action'])

    def setUp(self):
        with SessionLocal() as db:
            for model in (VerificationTrial, VerificationPlan, AIAnalysisCallModel,
                          AIAnalysisRunModel, AIAnalysisBatchModel):
                db.query(model).delete()
            db.commit()
        self.redis = RedisClient.get_client()
        self.redis.delete('quote:sina:FU2611')
        self.runtime = {'model_config_id': MODEL_ID, 'display_name': '测试模型',
                        'provider': 'openai_compatible', 'model_name': 'fixture',
                        'base_url': 'http://127.0.0.1:11434/v1', 'api_key': 'never-export-this'}
        self.patches = [patch.object(service.registry, 'model_runtime_config', return_value=self.runtime),
                        patch.object(service.FuturesTradingCalendar, 'analysis_session_options', return_value=[SESSION])]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def create(self, repeat=False, driven=False, outcome=False, auto=False):
        plan = service.create_plan('alice', {'instrument': 'FU2611', 'model_config_id': MODEL_ID,
            'commission_per_side': '2', 'slippage_ticks': '1',
            'model_input_price_per_million': '10', 'model_output_price_per_million': '20'},
            now=datetime(2026, 10, 8, 10, 0))['plan']
        if not auto:
            with SessionLocal() as db:
                row=db.query(VerificationPlan).filter_by(plan_id=plan['plan_id']).one()
                legacy_config=json.loads(row.config_json)
                legacy_config.pop('execution_mode',None)
                legacy_config['signal_timeout_minutes']=10
                row.config_json=json.dumps(legacy_config)
                db.commit()
            plan['config']=legacy_config
        if not outcome:
            with SessionLocal() as db:
                row = db.query(VerificationPlan).filter_by(plan_id=plan['plan_id']).one()
                config = json.loads(row.config_json)
                config.pop('analysis_result_trigger', None)
                config['rule_version'] = '1.2'
                row.config_json = json.dumps(config)
                db.commit()
            plan['config'] = config
        if not driven and not outcome:
            # Existing regression fixtures represent frozen version 1.0 plans.
            with SessionLocal() as db:
                row = db.query(VerificationPlan).filter_by(plan_id=plan['plan_id']).one()
                config = json.loads(row.config_json)
                config.pop('analysis_schedule_mode', None)
                config['analysis_interval_minutes'] = 10 if repeat else 0
                config['rule_version'] = '1.1' if repeat else '1.0'
                row.config_json = json.dumps(config)
                db.commit()
            plan['config'] = config
        return plan

    def real_plan(self, now, instrument='FU2611', repeat=False):
        self.patches[1].stop()
        plan = service.create_plan('alice', {'instrument':instrument,'model_config_id':MODEL_ID,
            'commission_per_side':'2','slippage_ticks':'1','model_input_price_per_million':'10',
            'model_output_price_per_million':'20'},now=now)['plan']
        if repeat:
            with SessionLocal() as db:
                row=db.query(VerificationPlan).filter_by(plan_id=plan['plan_id']).one()
                config=json.loads(row.config_json)
                config.pop('analysis_schedule_mode',None)
                config['analysis_interval_minutes']=10
                row.config_json=json.dumps(config)
                db.commit()
        return plan

    def finish_run(self, batch_id, direction='LONG', publication='REVIEWED', missing_usage=False):
        with SessionLocal() as db:
            snapshot = {'analysis_target': 'CURRENT_SESSION', 'session_target': SESSION,
                        'session_forecast': {'start_time': '2026-10-08T10:45:00+08:00',
                                             'end_time': SESSION['end_time'], 'reference_price': '99'}}
            if not db.query(AIAnalysisBatchModel).filter_by(batch_id=batch_id).first():
                db.add(AIAnalysisBatchModel(batch_id=batch_id, client_request_id=batch_id, owner_username='alice',
                instrument_id='FU2611', instrument_name='燃油', trading_day='20261008',
                snapshot_time=datetime(2026,10,8,10,45), snapshot_hash='fixture', snapshot_json=json.dumps(snapshot),
                selected_models_json=json.dumps([MODEL_ID]), status='COMPLETED'))
            chart = {'direction': direction, 'forecast_start_time': snapshot['session_forecast']['start_time'],
                'forecast_end_time': SESSION['end_time'], 'reference_price': 99,
                'trigger_price': 100 if direction == 'LONG' else None,
                'target_price': 110 if direction == 'LONG' else None,
                'invalidation_price': 90 if direction == 'LONG' else None}
            run = db.query(AIAnalysisRunModel).filter_by(batch_id=batch_id).first()
            if run:
                run.status,run.completed_at,run.call_count='SUCCEEDED',datetime(2026,10,8,2,45,10),1
                run.parsed_response_json=json.dumps({'direction':direction,'publication_status':publication,
                                                     'session_chart':chart})
            else:
                db.add(AIAnalysisRunModel(run_id='test-run', batch_id=batch_id, model_config_id=MODEL_ID,
                model_display_name='测试模型', provider='fixture', model_name='fixture', base_url_snapshot='http://localhost',
                config_json='{}', config_hash=service.registry.config_hash(self.runtime), status='SUCCEEDED',
                completed_at=datetime(2026,10,8,2,45,10), call_count=1,
                parsed_response_json=json.dumps({'direction': direction, 'publication_status':publication,
                                                'session_chart':chart})))
            db.add(AIAnalysisCallModel(call_id='call-test', run_id='test-run', sequence_no=1, call_type='SINGLE',
                request_json='{}', status='SUCCEEDED', input_tokens=None if missing_usage else 100,
                output_tokens=None if missing_usage else 50, completed_at=datetime(2026,10,8,2,45,10)))
            db.commit()

    def dispatch_pending(self, plan, trial):
        self.finish_run('batch-test')
        with SessionLocal() as db:
            db.query(AIAnalysisCallModel).delete()
            run = db.query(AIAnalysisRunModel).first()
            run.status,run.completed_at,run.call_count='RUNNING',None,0
            db.query(AIAnalysisBatchModel).first().client_request_id=trial.trial_id
            db.commit()
        return {'batch_id':'batch-test'}

    def quote(self, stamp, last=100, bid=99, ask=100):
        self.redis.hset('quote:sina:FU2611', mapping={'timestamp':stamp,'last_price':str(last),
            'bid_price_1':str(bid),'ask_price_1':str(ask),'trading_day':'20261008'})

    def submit(self, plan):
        with patch.object(service, 'dispatch_analysis', return_value={'batch_id':'batch-test'}):
            service.tick(datetime(2026,10,8,10,45))

    def alter_run(self, **fields):
        with SessionLocal() as db:
            run = db.query(AIAnalysisRunModel).first()
            for name, value in fields.items():
                setattr(run, name, value)
            db.commit()

    def test_freezes_rules_and_owner_and_only_one_active_plan(self):
        plan = self.create()
        self.assertEqual(plan['config']['analysis_delay_minutes'], 15)
        self.assertEqual(plan['config']['volume'], 1)
        self.assertEqual(plan['config']['target_days'], 20)
        self.assertNotIn('api_key', json.dumps(plan))
        with self.assertRaises(LookupError):
            service.get_report('bob', plan['plan_id'])
        with self.assertRaises(ValueError):
            self.create()

    def test_due_window_once_and_waits_for_real_result_availability(self):
        plan = self.create()
        with patch.object(service, 'dispatch_analysis', side_effect=self.dispatch_pending) as dispatch:
            service.tick(datetime(2026,10,8,10,44,59))
            self.assertEqual(dispatch.call_count, 0)
            service.tick(datetime(2026,10,8,10,45))
            service.tick(datetime(2026,10,8,10,45,1))
            self.assertEqual(dispatch.call_count, 1)
        self.finish_run('batch-test')
        self.quote('2026-10-08T10:45:09')
        service.tick(datetime(2026,10,8,10,45,10))
        trial = service.get_report('alice', plan['plan_id'])['trials'][0]
        self.assertEqual(trial['status'], 'WAITING_TRIGGER')
        self.assertEqual(trial['result_available_at'], '2026-10-08T10:45:10')
        self.quote('2026-10-08T10:45:12')
        service.tick(datetime(2026,10,8,10,45,12))
        trial = service.get_report('alice', plan['plan_id'])['trials'][0]
        self.assertEqual(trial['status'], 'OPEN')
        self.assertEqual(trial['state']['entry_price'], 101)
        self.quote('2026-10-08T10:50:20', last=111, bid=110, ask=111)
        service.tick(datetime(2026,10,8,10,50,20))
        report = service.get_report('alice', plan['plan_id'])
        self.assertEqual(report['report']['closed_trades'], 1)
        self.assertAlmostEqual(report['report']['trading_net_pnl'], 76)
        self.assertAlmostEqual(report['report']['model_cost'], .002)
        self.assertAlmostEqual(report['report']['total_net_pnl'], 75.998)

    def test_missed_window_is_skip_and_never_dispatches(self):
        plan = self.create()
        with patch.object(service, 'dispatch_analysis') as dispatch:
            service.tick(datetime(2026,10,8,10,47))
            service.tick(datetime(2026,10,8,10,48))
            dispatch.assert_not_called()
        self.assertEqual(len(service.get_report('alice',plan['plan_id'])['trials']), 1)
        self.assertEqual(service.get_report('alice',plan['plan_id'])['trials'][0]['status'], 'SKIPPED')

    def test_no_direction_still_charges_all_model_cost(self):
        plan = self.create()
        with patch.object(service, 'dispatch_analysis', return_value={'batch_id':'batch-test'}):
            service.tick(datetime(2026,10,8,10,45))
        self.finish_run('batch-test', direction='NO_JUDGMENT')
        service.tick(datetime(2026,10,8,10,45,12))
        report = service.get_report('alice',plan['plan_id'])
        self.assertEqual(report['trials'][0]['status'], 'SKIPPED')
        self.assertAlmostEqual(report['report']['total_net_pnl'], -.002)

    def test_missing_paid_usage_is_reported_not_silently_zero(self):
        plan = self.create()
        with patch.object(service, 'dispatch_analysis', return_value={'batch_id':'batch-test'}):
            service.tick(datetime(2026,10,8,10,45))
        self.finish_run('batch-test', direction='NO_JUDGMENT', missing_usage=True)
        service.tick(datetime(2026,10,8,10,45,12))
        report = service.get_report('alice',plan['plan_id'])
        self.assertEqual(report['report']['model_cost_unknown'], 1)
        self.assertFalse(report['report']['is_complete'])

    def test_stop_and_restart_do_not_resubmit_inflight_analysis(self):
        plan = self.create()
        with patch.object(service, 'dispatch_analysis', side_effect=self.dispatch_pending) as dispatch:
            service.tick(datetime(2026,10,8,10,45))
            service.stop_plan('alice',plan['plan_id'], now=datetime(2026,10,8,10,45,2))
            service.tick(datetime(2026,10,8,10,45,3))
            self.assertEqual(dispatch.call_count, 1)
        self.finish_run('batch-test',direction='NO_JUDGMENT')
        service.tick(datetime(2026,10,8,10,45,12))
        report = service.get_report('alice',plan['plan_id'])
        self.assertEqual(report['plan']['status'], 'STOPPED')
        self.assertAlmostEqual(report['report']['model_cost'], .002)

    def test_submission_is_unknown_before_dispatch_and_recovers_committed_batch_after_exception(self):
        plan = self.create()
        def interrupted(_plan, row):
            with SessionLocal() as db:
                saved = db.query(VerificationTrial).filter_by(trial_id=row.trial_id).one()
                self.assertEqual(saved.status, 'SUBMITTING')
                self.assertFalse(json.loads(saved.state_json)['model_cost_known'])
            self.finish_run('batch-test', direction='NO_JUDGMENT')
            with SessionLocal() as db:
                db.query(AIAnalysisBatchModel).first().client_request_id = row.trial_id
                db.commit()
            raise RuntimeError('fixture response interrupted after commit')
        with patch.object(service, 'dispatch_analysis', side_effect=interrupted) as dispatch:
            service.tick(datetime(2026,10,8,10,45))
            first = service.get_report('alice',plan['plan_id'])['trials'][0]
            self.assertEqual(first['status'], 'SUBMITTED', first['reason'])
            self.assertFalse(first['model_cost_known'])
            service.tick(datetime(2026,10,8,10,45,12))
            self.assertEqual(dispatch.call_count, 1)
        report = service.get_report('alice',plan['plan_id'])
        self.assertAlmostEqual(report['report']['model_cost'], .002)
        self.assertEqual(report['trials'][0]['status'], 'SKIPPED')

    def test_definitely_unsubmitted_request_skips_zero_cost_and_never_retries(self):
        plan = self.create()
        with patch.object(service, 'dispatch_analysis', side_effect=ValueError('fixture unavailable')) as dispatch:
            service.tick(datetime(2026,10,8,10,45))
            service.tick(datetime(2026,10,8,10,45,1))
            self.assertEqual(dispatch.call_count, 1)
        row = service.get_report('alice',plan['plan_id'])['trials'][0]
        self.assertEqual(row['status'], 'SKIPPED')
        self.assertTrue(row['model_cost_known'])
        self.assertEqual(row['model_cost'], 0)

    def test_broken_parsed_json_preserves_cost_and_does_not_abort_scheduler(self):
        plan = self.create()
        self.submit(plan)
        self.finish_run('batch-test')
        for broken in ('{broken', '[]', 'null', '{"session_chart":[]}'):
            with self.subTest(parsed=broken):
                with SessionLocal() as db:
                    row = db.query(VerificationTrial).first()
                    row.status = 'SUBMITTED'
                    row.state_json = json.dumps({'model_cost':None,'model_cost_known':False})
                    db.query(AIAnalysisRunModel).first().parsed_response_json = broken
                    db.commit()
                service.tick(datetime(2026,10,8,10,45,12))
                report = service.get_report('alice',plan['plan_id'])
                self.assertEqual(report['trials'][0]['status'], 'SKIPPED')
                self.assertAlmostEqual(report['report']['model_cost'], .002)

    def test_missing_submitted_batch_or_run_becomes_terminal_with_unknown_fee(self):
        for missing in ('batch', 'run'):
            with self.subTest(missing=missing):
                with SessionLocal() as db:
                    for model in (VerificationTrial,VerificationPlan,AIAnalysisCallModel,
                                  AIAnalysisRunModel,AIAnalysisBatchModel):
                        db.query(model).delete()
                    db.commit()
                plan = self.create()
                self.submit(plan)
                if missing == 'run':
                    self.finish_run('batch-test')
                    with SessionLocal() as db:
                        db.query(AIAnalysisCallModel).delete()
                        db.query(AIAnalysisRunModel).delete()
                        db.commit()
                service.tick(datetime(2026,10,8,10,45,12))
                report = service.get_report('alice',plan['plan_id'])
                self.assertEqual(report['trials'][0]['status'], 'SKIPPED')
                self.assertEqual(report['report']['model_cost_unknown'], 1)

    def test_restart_recovers_submitting_by_request_owner_without_resubmission(self):
        plan = self.create()
        self.submit(plan)
        self.finish_run('batch-test', direction='NO_JUDGMENT')
        with SessionLocal() as db:
            row = db.query(VerificationTrial).first()
            row.status, row.batch_id = 'SUBMITTING', None
            db.query(AIAnalysisBatchModel).first().client_request_id = row.trial_id
            db.commit()
        with patch.object(service, 'dispatch_analysis') as dispatch:
            service.tick(datetime(2026,10,8,10,45,12))
            dispatch.assert_not_called()
        report = service.get_report('alice',plan['plan_id'])
        self.assertEqual(report['trials'][0]['batch_id'], 'batch-test')
        self.assertAlmostEqual(report['report']['model_cost'], .002)

    def test_multiple_waiting_trials_cannot_open_multiple_positions_in_one_scan(self):
        plan = self.create()
        state = {'status':'WAITING_TRIGGER','direction':'LONG','trigger_price':100,
                 'target_price':110,'invalidation_price':90,
                 'available_at':'2026-10-08T10:45:10','session_end':SESSION['end_time'],
                 'model_cost':0,'model_cost_known':True}
        with SessionLocal() as db:
            for offset in (0,1):
                db.add(VerificationTrial(trial_id=str(uuid.uuid4()),plan_id=plan['plan_id'],
                    trading_day='20261008',session_label='fixture',
                    session_start=datetime(2026,10,8,10,30,offset),session_end=datetime(2026,10,8,11,30),
                    analysis_due_at=datetime(2026,10,8,10,45,offset),status='WAITING_TRIGGER',
                    state_json=json.dumps(state)))
            db.commit()
        self.quote('2026-10-08T10:45:12')
        service.tick(datetime(2026,10,8,10,45,12))
        rows = service.get_report('alice',plan['plan_id'])['trials']
        self.assertEqual(sum(row['status']=='OPEN' for row in rows), 1)
        self.assertEqual(sum(row['status']=='SKIPPED' for row in rows), 1)

    def dated_sessions(self, dt, instrument):
        return [{**SESSION,'start_time':dt.replace(hour=10,minute=30,second=0,microsecond=0).isoformat(),
                 'end_time':dt.replace(hour=11,minute=30,second=0,microsecond=0).isoformat()}]

    def test_entire_offline_trading_days_are_recorded_as_missed_without_model_calls(self):
        with patch.object(service.FuturesTradingCalendar,'analysis_session_options',side_effect=self.dated_sessions):
            plan = self.create()
            with patch.object(service,'dispatch_analysis') as dispatch:
                service.tick(datetime(2026,10,12,10,47))
                dispatch.assert_not_called()
            report = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,12,10,47))
        self.assertEqual([row['trading_day'] for row in report['trials']], ['20261008','20261009','20261012'])
        self.assertEqual(report['monitoring']['missed_windows'], 3)
        self.assertEqual(report['monitoring']['expected_trials'], 3)
        self.assertFalse(report['report']['is_complete'])

    def test_windows_before_plan_creation_are_not_recorded_as_missed(self):
        earlier = {**SESSION,'start_time':'2026-10-08T09:00:00','end_time':'2026-10-08T10:15:00'}
        with patch.object(service.FuturesTradingCalendar,'analysis_session_options',return_value=[earlier,SESSION]):
            plan = self.create()
            with patch.object(service,'dispatch_analysis',return_value={'batch_id':'batch-test'}):
                service.tick(datetime(2026,10,8,10,45))
            rows = service.get_report('alice',plan['plan_id'])['trials']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session_start'], SESSION['start_time'])

    def test_twenty_day_end_records_all_missed_windows_and_stops(self):
        with patch.object(service.FuturesTradingCalendar,'analysis_session_options',side_effect=self.dated_sessions):
            plan = self.create()
            end = datetime.strptime(plan['end_day'],'%Y%m%d').replace(hour=16)
            with patch.object(service,'dispatch_analysis') as dispatch:
                service.tick(end)
                dispatch.assert_not_called()
            report = service.get_report('alice',plan['plan_id'],now=end)
        self.assertEqual(report['plan']['status'], 'COMPLETED')
        self.assertEqual(len(report['trials']), 20)
        self.assertEqual(report['monitoring']['observed_trading_days'], 20)
        self.assertFalse(report['monitoring']['complete'])

    def test_stopped_monitoring_range_does_not_grow_with_future_reports(self):
        with patch.object(service.FuturesTradingCalendar,'analysis_session_options',side_effect=self.dated_sessions):
            plan = self.create()
            service.stop_plan('alice',plan['plan_id'],now=datetime(2026,10,8,10,50))
            report = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,15,10,50))
        self.assertEqual(report['monitoring']['expected_trials'], 1)
        self.assertFalse(report['monitoring']['complete'])

    def test_model_hash_window_publication_and_invalid_price_are_rejected_but_charged(self):
        plan = self.create()
        self.submit(plan)
        self.finish_run('batch-test')
        mutations = [('hash', {'config_hash':'wrong'}),('review', {'parsed_response_json':json.dumps(
            {'direction':'LONG','publication_status':'DRAFT_ONLY'})}),
            ('window', {'parsed_response_json':json.dumps({'direction':'LONG','publication_status':'REVIEWED',
                'session_chart':{'direction':'LONG','forecast_start_time':'2026-10-08T10:44:59',
                  'forecast_end_time':SESSION['end_time'],'trigger_price':100,'target_price':110,'invalidation_price':90}})})]
        with SessionLocal() as db:
            original = db.query(AIAnalysisRunModel).first().parsed_response_json
        for name, fields in mutations:
            with self.subTest(case=name):
                with SessionLocal() as db:
                    db.query(VerificationTrial).first().status='SUBMITTED'
                    run=db.query(AIAnalysisRunModel).first()
                    run.config_hash=service.registry.config_hash(self.runtime)
                    run.parsed_response_json=original
                    for key,value in fields.items():
                        setattr(run,key,value)
                    db.commit()
                service.tick(datetime(2026,10,8,10,45,12))
                report=service.get_report('alice',plan['plan_id'])
                self.assertEqual(report['trials'][0]['status'],'SKIPPED')
                self.assertAlmostEqual(report['report']['model_cost'], .002)

    def test_auto_paper_accepts_sina_delay_with_recent_receipt_and_marks_live_consistently(self):
        plan = self.create(outcome=True, auto=True)
        self.submit(plan)
        self.finish_run('batch-test')
        self.quote('2026-10-08T10:46:00')
        self.redis.hset('quote:sina:FU2611', 'received_at', '2026-10-08T10:46:22')
        service.tick(datetime(2026,10,8,10,46,23))
        report = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,46,23))
        self.assertEqual(report['trials'][0]['status'],'OPEN')
        self.assertEqual(report['trials'][0]['state']['entry_time'],'2026-10-08T10:46:00')
        self.assertEqual(report['trials'][0]['live_pnl']['status'],'LIVE')
        self.assertEqual(report['trials'][0]['state']['quote_policy'],'SINA_SOURCE_30S_RECEIPT_10S')

    def test_live_scan_checks_receipt_against_time_after_redis_read(self):
        plan = self.create(outcome=True, auto=True)
        self.submit(plan)
        self.finish_run('batch-test')
        self.quote('2026-10-08T10:46:00')
        self.redis.hset('quote:sina:FU2611', 'received_at', '2026-10-08T10:46:23.500000')
        times = iter([datetime(2026,10,8,10,46,23), datetime(2026,10,8,10,46,24)])
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return next(times)
        with patch.object(service, 'datetime', Clock):
            service.tick()
        self.assertEqual(service.get_report('alice',plan['plan_id'])['trials'][0]['status'],'OPEN')

    def test_wrong_trading_day_and_stale_quote_never_open(self):
        plan=self.create()
        self.submit(plan)
        self.finish_run('batch-test')
        self.quote('2026-10-08T10:45:12')
        self.redis.hset('quote:sina:FU2611','trading_day','20261009')
        service.tick(datetime(2026,10,8,10,45,12))
        self.assertEqual(service.get_report('alice',plan['plan_id'])['trials'][0]['status'],'WAITING_TRIGGER')
        self.redis.hset('quote:sina:FU2611','trading_day','20261008')
        service.tick(datetime(2026,10,8,10,45,23))
        self.assertEqual(service.get_report('alice',plan['plan_id'])['trials'][0]['status'],'WAITING_TRIGGER')

    def test_after_final_due_uses_next_period_and_counts_twenty_actual_days(self):
        for instrument in ('FU2611','IF2610'):
            with self.subTest(instrument=instrument):
                with SessionLocal() as db:
                    db.query(VerificationTrial).delete()
                    db.query(VerificationPlan).delete()
                    db.commit()
                plan=self.real_plan(datetime(2026,10,8,15,0,1),instrument)
                self.assertEqual(plan['start_day'],'20261009')
                self.assertEqual(plan['end_day'],'20261105')

    def test_friday_night_due_belongs_to_monday_and_is_recorded_once(self):
        plan=self.real_plan(datetime(2026,10,9,20))
        self.assertEqual(plan['start_day'],'20261012')
        with patch.object(service,'dispatch_analysis',side_effect=self.dispatch_pending) as dispatch:
            service.tick(datetime(2026,10,9,21,14,59))
            self.assertEqual(dispatch.call_count,0)
            service.tick(datetime(2026,10,9,21,15))
            service.tick(datetime(2026,10,9,21,15,1))
            self.assertEqual(dispatch.call_count,1)
        report=service.get_report('alice',plan['plan_id'],now=datetime(2026,10,9,21,15,1))
        self.assertEqual(len(report['trials']),1)
        self.assertEqual(report['trials'][0]['trading_day'],'20261012')
        self.assertEqual(report['trials'][0]['session_start'],'2026-10-09T21:00:00')
        self.assertEqual(report['monitoring']['expected_trials'],1)

    def test_other_owner_request_is_not_recovered_as_this_plan_batch(self):
        plan=self.create()
        def wrong_owner(_plan,row):
            self.finish_run('batch-test')
            with SessionLocal() as db:
                batch=db.query(AIAnalysisBatchModel).first()
                batch.client_request_id,batch.owner_username=row.trial_id,'bob'
                db.commit()
            raise ValueError('fixture other owner exists')
        with patch.object(service,'dispatch_analysis',side_effect=wrong_owner):
            service.tick(datetime(2026,10,8,10,45))
        row=service.get_report('alice',plan['plan_id'])['trials'][0]
        self.assertEqual(row['status'],'SKIPPED')
        self.assertIsNone(row['batch_id'])
        self.assertEqual(row['model_cost'],0)

    def test_completed_result_without_timestamp_is_skipped_and_keeps_fee(self):
        plan=self.create()
        self.submit(plan)
        self.finish_run('batch-test')
        self.alter_run(completed_at=None)
        service.tick(datetime(2026,10,8,10,45,12))
        report=service.get_report('alice',plan['plan_id'])
        self.assertEqual(report['trials'][0]['status'],'SKIPPED')
        self.assertAlmostEqual(report['report']['model_cost'],.002)

    def test_dispatch_uses_latest_plan_prompt_and_keeps_previous_batch(self):
        from types import SimpleNamespace
        plan = self.create()
        trial = SimpleNamespace(trial_id=str(uuid.uuid4()),session_start=datetime(2026,10,8,10,30))
        bodies = []
        with patch('backend.api.ai_routes.create_analysis_for_owner',side_effect=lambda owner,instrument,body:bodies.append(body) or {'batch_id':'mock'}):
            with SessionLocal() as db:
                row=db.query(VerificationPlan).filter_by(plan_id=plan['plan_id']).one()
                service.dispatch_analysis(row,trial)
            service.stop_plan('alice',plan['plan_id'],now=datetime(2026,10,8,10,1))
            service.tick(datetime(2026,10,8,10,1))
            service.update_requirements('alice',plan['plan_id'],'独立自定义提示词')
            service.resume_plan('alice',plan['plan_id'],now=datetime(2026,10,8,10,2))
            with SessionLocal() as db:
                row=db.query(VerificationPlan).filter_by(plan_id=plan['plan_id']).one()
                service.dispatch_analysis(row,trial)
        self.assertEqual(bodies[0].analysis_requirements,service.REQUIREMENTS)
        self.assertEqual(bodies[1].analysis_requirements,'独立自定义提示词')

    def test_resume_preserves_ledger_and_excludes_pause_and_old_retries(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test',direction='NO_JUDGMENT')
        service.tick(datetime(2026,10,8,10,45,12))
        service.stop_plan('alice',plan['plan_id'],now=datetime(2026,10,8,10,46))
        service.tick(datetime(2026,10,8,10,46))
        before = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,46))
        self.assertEqual(before['plan']['status'],'STOPPED')
        resumed = service.resume_plan('alice',plan['plan_id'],now=datetime(2026,10,8,11))
        self.assertEqual(resumed['plan']['status'],'RUNNING')
        self.assertEqual(resumed['trials'],before['trials'])
        self.assertEqual(resumed['plan']['config']['commission_per_side'],2)
        self.assertEqual(len(resumed['plan']['config']['resume_history']),1)
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'resumed'}) as dispatch:
            service.tick(datetime(2026,10,8,11))
            service.tick(datetime(2026,10,8,11))
            self.assertEqual(dispatch.call_count,1)
        report = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,11))
        self.assertEqual(len(report['trials']),2)
        self.assertEqual(report['monitoring']['missed_windows'],0)
        self.assertEqual(report['trials'][0]['state'],before['trials'][0]['state'])

    def test_resume_rejects_pending_completed_expired_and_other_active(self):
        plan = self.create(outcome=True)
        with self.assertRaises(ValueError):
            service.resume_plan('alice',plan['plan_id'],now=datetime(2026,10,8,11))
        service.stop_plan('alice',plan['plan_id'],now=datetime(2026,10,8,10,20))
        service.tick(datetime(2026,10,8,10,20))
        with self.assertRaises(LookupError):
            service.resume_plan('bob',plan['plan_id'],now=datetime(2026,10,8,11))
        with self.assertRaises(ValueError):
            service.resume_plan('alice',plan['plan_id'],now=datetime(2027,1,1,11))
        other = self.create(outcome=True)
        with self.assertRaises(ValueError):
            service.resume_plan('alice',plan['plan_id'],now=datetime(2026,10,8,11))
        with SessionLocal() as db:
            db.query(VerificationPlan).filter_by(plan_id=other['plan_id']).one().status='STOPPED'
            db.commit()
        with patch.object(service.registry,'config_hash',return_value='changed'):
            with self.assertRaises(ValueError):
                service.resume_plan('alice',plan['plan_id'],now=datetime(2026,10,8,11))

    def test_service_rejects_invalid_cost_contract_and_missing_parameters(self):
        base={'instrument':'FU2611','model_config_id':MODEL_ID,'commission_per_side':2,
              'slippage_ticks':1,'model_input_price_per_million':10,'model_output_price_per_million':20}
        for changes in ({'instrument':'FU0'},{'instrument':'ZZ2611'},{'commission_per_side':'NaN'},
                        {'slippage_ticks':'1.2'},{'model_output_price_per_million':-1}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    service.create_plan('alice',{**base,**changes},now=datetime(2026,10,8,10))
        with self.assertRaises(ValueError):
            service.create_plan('alice',{'instrument':'FU2611'},now=datetime(2026,10,8,10))

    def test_all_calls_including_failed_review_are_charged(self):
        plan=self.create()
        self.submit(plan)
        self.finish_run('batch-test',publication='DRAFT_ONLY')
        with SessionLocal() as db:
            db.add(AIAnalysisCallModel(call_id='review-failed',run_id='test-run',sequence_no=2,
                call_type='REVIEW',request_json='{}',status='FAILED',input_tokens=200,output_tokens=100))
            db.query(AIAnalysisRunModel).first().call_count=2
            db.commit()
        service.tick(datetime(2026,10,8,10,45,12))
        report=service.get_report('alice',plan['plan_id'])
        self.assertEqual(report['trials'][0]['status'],'SKIPPED')
        self.assertAlmostEqual(report['report']['model_cost'],.006)

    def test_manual_early_stop_is_never_completed_after_late_service_restart(self):
        plan=self.create()
        with patch.object(service,'dispatch_analysis',side_effect=self.dispatch_pending):
            service.tick(datetime(2026,10,8,10,45))
        service.stop_plan('alice',plan['plan_id'],now=datetime(2026,10,8,10,45,2))
        self.finish_run('batch-test',direction='NO_JUDGMENT')
        end=datetime.strptime(plan['end_day'],'%Y%m%d').replace(hour=16)
        service.tick(end)
        report=service.get_report('alice',plan['plan_id'],now=end)
        self.assertEqual(report['plan']['status'],'STOPPED')
        self.assertEqual(report['monitoring']['observed_trading_days'],1)
        self.assertFalse(report['monitoring']['complete'])
        self.assertFalse(report['report']['is_complete'])
        self.assertAlmostEqual(report['report']['model_cost'],.002)

    def test_legacy_stopping_recovery_records_prior_missed_days_only(self):
        with patch.object(service.FuturesTradingCalendar,'analysis_session_options',side_effect=self.dated_sessions):
            plan=self.create()
            with SessionLocal() as db:
                saved=db.query(VerificationPlan).one()
                saved.status,saved.stopped_at='STOPPING',datetime(2026,10,9,12)
                db.commit()
            with patch.object(service,'dispatch_analysis') as dispatch:
                service.tick(datetime(2026,10,12,12))
                dispatch.assert_not_called()
            report=service.get_report('alice',plan['plan_id'],now=datetime(2026,10,12,12))
        self.assertEqual([row['trading_day'] for row in report['trials']],['20261008','20261009'])
        self.assertEqual(report['monitoring']['expected_trials'],2)
        self.assertEqual(report['monitoring']['recorded_trials'],2)
        self.assertEqual(report['plan']['status'],'STOPPED')

    def test_corrupted_snapshot_window_types_are_skipped_with_cost(self):
        plan=self.create()
        self.submit(plan)
        self.finish_run('batch-test')
        with SessionLocal() as db:
            original=json.loads(db.query(AIAnalysisBatchModel).one().snapshot_json)
        for field in ('session_target','session_forecast'):
            with self.subTest(field=field):
                with SessionLocal() as db:
                    db.query(VerificationTrial).one().status='SUBMITTED'
                    db.query(AIAnalysisBatchModel).one().snapshot_json=json.dumps({**original,field:[]})
                    db.commit()
                service.tick(datetime(2026,10,8,10,45,12))
                report=service.get_report('alice',plan['plan_id'])
                self.assertEqual(report['trials'][0]['status'],'SKIPPED')
                self.assertAlmostEqual(report['report']['model_cost'],.002)

    def test_incomplete_call_rows_mean_unknown_fee_instead_of_undercharging(self):
        plan=self.create()
        self.submit(plan)
        self.finish_run('batch-test',direction='NO_JUDGMENT')
        self.alter_run(call_count=2)
        service.tick(datetime(2026,10,8,10,45,12))
        report=service.get_report('alice',plan['plan_id'])
        self.assertEqual(report['report']['model_cost_unknown'],1)
        self.assertFalse(report['report']['is_complete'])

    def test_twenty_recorded_days_without_missed_windows_finish_complete(self):
        with patch.object(service.FuturesTradingCalendar,'analysis_session_options',side_effect=self.dated_sessions):
            plan=self.create()
            end=datetime.strptime(plan['end_day'],'%Y%m%d').replace(hour=16)
            day=datetime(2026,10,8)
            with SessionLocal() as db:
                while day.date()<=end.date():
                    if service.FuturesTradingCalendar.is_trading_day(day.date()):
                        db.add(VerificationTrial(trial_id=str(uuid.uuid4()),plan_id=plan['plan_id'],
                            trading_day=day.strftime('%Y%m%d'),session_label='fixture',
                            session_start=day.replace(hour=10,minute=30),session_end=day.replace(hour=11,minute=30),
                            analysis_due_at=day.replace(hour=10,minute=45),status='SKIPPED',reason='NO_JUDGMENT',
                            state_json=json.dumps({'model_cost':0,'model_cost_known':True})))
                    day+=timedelta(days=1)
                db.commit()
            with patch.object(service,'dispatch_analysis') as dispatch:
                service.tick(end)
                dispatch.assert_not_called()
            report=service.get_report('alice',plan['plan_id'],now=end)
        self.assertEqual(report['plan']['status'],'COMPLETED')
        self.assertEqual(report['monitoring']['missed_windows'],0)
        self.assertTrue(report['monitoring']['complete'])
        self.assertTrue(report['report']['is_complete'])
        self.assertIsNone(report['report']['win_rate_pct'])


    def test_repeat_windows_and_restart_are_idempotent(self):
        plan = self.create(repeat=True)
        self.assertEqual(plan['config']['analysis_interval_minutes'], 10)
        with patch.object(service, 'dispatch_analysis', return_value={'batch_id':'pending'}) as dispatch:
            for at in (datetime(2026,10,8,10,44,59), datetime(2026,10,8,10,45),
                       datetime(2026,10,8,10,54,59), datetime(2026,10,8,10,55),
                       datetime(2026,10,8,10,55,1), datetime(2026,10,8,11,5)):
                service.tick(at)
            self.assertEqual(dispatch.call_count, 3)
        report = service.get_report('alice', plan['plan_id'], now=datetime(2026,10,8,11,5))
        self.assertEqual([r['analysis_due_at'][-8:] for r in report['trials']],
                         ['10:45:00','10:55:00','11:05:00'])
        self.assertEqual({r['session_start'] for r in report['trials']}, {SESSION['start_time']})
        self.assertEqual(report['monitoring']['expected_trials'], 3)

    def test_repeat_stops_at_session_end_and_records_missed_windows(self):
        plan = self.create(repeat=True)
        with patch.object(service, 'dispatch_analysis') as dispatch:
            service.tick(datetime(2026,10,8,11,30))
            dispatch.assert_not_called()
        report = service.get_report('alice', plan['plan_id'], now=datetime(2026,10,8,11,30))
        self.assertEqual([r['analysis_due_at'][-8:] for r in report['trials']],
                         ['10:45:00','10:55:00','11:05:00','11:15:00'])
        self.assertEqual(report['monitoring']['missed_windows'], 4)

    def test_repeat_plan_created_mid_session_uses_next_ten_minute_window(self):
        plan = self.real_plan(datetime(2026,10,9,14,1),repeat=True)
        self.assertEqual(plan['start_day'], '20261009')
        with patch.object(service, 'dispatch_analysis', return_value={'batch_id':'pending'}) as dispatch:
            service.tick(datetime(2026,10,9,14,4,59))
            dispatch.assert_not_called()
            service.tick(datetime(2026,10,9,14,5))
            self.assertEqual(dispatch.call_count, 1)

    def test_new_result_supersedes_unfilled_signal(self):
        plan = self.create(repeat=True)
        with patch.object(service, 'dispatch_analysis', return_value={'batch_id':'batch-test'}):
            service.tick(datetime(2026,10,8,10,45))
        self.finish_run('batch-test')
        with SessionLocal() as db:
            db.add(VerificationTrial(trial_id=str(uuid.uuid4()),plan_id=plan['plan_id'],
                trading_day='20261008',session_label='old',session_start=datetime(2026,10,8,10,30),
                session_end=datetime(2026,10,8,11,30),analysis_due_at=datetime(2026,10,8,10,35),
                status='WAITING_TRIGGER',state_json=json.dumps({'model_cost':.01,'model_cost_known':True})))
            db.commit()
        service.tick(datetime(2026,10,8,10,45,12))
        report = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,45,12))
        old = next(r for r in report['trials'] if r['session_label']=='old')
        self.assertEqual(old['status'], 'SKIPPED')
        self.assertIn('新分析', old['reason'])
        self.assertAlmostEqual(report['report']['model_cost'], .012)

    def test_repeat_costs_and_position_timeout_keep_original_frozen_levels(self):
        plan = self.create(repeat=True)
        with patch.object(service, 'dispatch_analysis', return_value={'batch_id':'batch-test'}):
            service.tick(datetime(2026,10,8,10,45))
        self.finish_run('batch-test')
        self.redis.hset('quote:sina:FU2611', mapping={
            'timestamp':'2026-10-08T10:45:11','trading_day':'20261008',
            'last_price':100,'bid_price_1':99,'ask_price_1':101})
        service.tick(datetime(2026,10,8,10,45,12))
        with patch.object(service, 'dispatch_analysis', return_value={'batch_id':'batch-second'}):
            service.tick(datetime(2026,10,8,10,55))
        with SessionLocal() as db:
            batch = db.query(AIAnalysisBatchModel).filter_by(batch_id='batch-test').one()
            run = db.query(AIAnalysisRunModel).filter_by(run_id='test-run').one()
            call = db.query(AIAnalysisCallModel).filter_by(call_id='call-test').one()
            def clone(row):
                return {c.name:getattr(row,c.name) for c in row.__table__.columns if c.name!='id'}
            fields = clone(batch)
            snapshot = json.loads(fields['snapshot_json'])
            snapshot['session_forecast']['start_time'] = '2026-10-08T10:55:00+08:00'
            fields.update(batch_id='batch-second',client_request_id='batch-second',
                          snapshot_json=json.dumps(snapshot))
            db.add(AIAnalysisBatchModel(**fields))
            fields = clone(run)
            parsed = json.loads(fields['parsed_response_json'])
            parsed['session_chart']['forecast_start_time'] = snapshot['session_forecast']['start_time']
            fields.update(run_id='run-second',batch_id='batch-second',
                          completed_at=datetime(2026,10,8,2,55,10),parsed_response_json=json.dumps(parsed))
            db.add(AIAnalysisRunModel(**fields))
            fields = clone(call)
            fields.update(call_id='call-second',run_id='run-second')
            db.add(AIAnalysisCallModel(**fields))
            db.commit()
        self.redis.hset('quote:sina:FU2611', mapping={
            'timestamp':'2026-10-08T10:55:11','last_price':102,'bid_price_1':101,'ask_price_1':103})
        service.tick(datetime(2026,10,8,10,55,12))
        report = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,55,12))
        self.assertEqual([r['status'] for r in report['trials']], ['CLOSED','SKIPPED'])
        self.assertEqual(report['trials'][0]['state']['exit_reason'], 'RESULT_TIMEOUT')
        self.assertEqual(report['trials'][0]['state']['entry_time'], '2026-10-08T10:45:11')
        self.assertEqual(report['trials'][0]['state']['target_price'], 110)
        self.assertAlmostEqual(report['report']['model_cost'], .004)

    def test_old_window_index_migration_preserves_records_and_is_idempotent(self):
        from sqlalchemy import text
        from backend.storage import mysql_client
        plan = self.create()
        with patch.object(service, 'dispatch_analysis', return_value={'batch_id':'pending'}):
            service.tick(datetime(2026,10,8,10,45))
        with mysql_client.engine.begin() as conn:
            conn.execute(text('ALTER TABLE ai_paper_verification_trial '
                              'ADD UNIQUE INDEX uq_verification_plan_session (plan_id, session_start), '
                              'DROP INDEX uq_verification_plan_due'))
        mysql_client._ensure_verification_window_index()
        mysql_client._ensure_verification_window_index()
        self.assertEqual(len(service.get_report('alice',plan['plan_id'])['trials']), 1)
        with mysql_client.engine.connect() as conn:
            indexes = {r[0] for r in conn.execute(text(
                "SELECT INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='ai_paper_verification_trial'"))}
        self.assertIn('uq_verification_plan_due', indexes)
        self.assertNotIn('uq_verification_plan_session', indexes)

    def test_trade_outcome_waits_for_market_not_model_completion(self):
        plan = self.create(outcome=True)
        self.assertEqual(plan['config']['analysis_result_trigger'], 'TRADE_OUTCOME')
        self.submit(plan)
        self.finish_run('batch-test')
        with patch.object(service, 'dispatch_analysis') as dispatch:
            service.tick(datetime(2026,10,8,10,45,12))
            service.tick(datetime(2026,10,8,10,46,12))
            service.tick(datetime(2026,10,8,10,55,9))
            dispatch.assert_not_called()
        row = service.get_report('alice', plan['plan_id'])['trials'][0]
        self.assertEqual(row['status'], 'WAITING_TRIGGER')
        self.assertEqual(row['state']['signal_deadline_at'], '2026-10-08T10:55:10')

    def test_trade_outcome_unfilled_signal_expires_after_ten_minutes(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'next'}) as dispatch:
            service.tick(datetime(2026,10,8,10,45,12))
            service.tick(datetime(2026,10,8,10,55,10))
            self.assertEqual(dispatch.call_count,1)
        row=service.get_report('alice',plan['plan_id'])['trials'][0]
        self.assertEqual(row['status'],'SKIPPED')
        self.assertIn('SIGNAL_TIMEOUT',row['reason'])
        report = service.get_report('alice',plan['plan_id'])['report']
        self.assertEqual(report['signal_timeout_losses'], 1)
        self.assertEqual(report['losses'], 1)
        self.assertEqual(report['closed_trades'], 0)

    def test_trade_outcome_open_waits_for_exit_and_restarts_once(self):
        plan=self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'next'}) as dispatch:
            self.quote('2026-10-08T10:45:12')
            service.tick(datetime(2026,10,8,10,45,12))
            service.tick(datetime(2026,10,8,10,56))
            dispatch.assert_not_called()
            self.quote('2026-10-08T10:56:02',last=111,bid=110,ask=111)
            service.tick(datetime(2026,10,8,10,56,2))
            self.assertEqual(dispatch.call_count,1)
            service.tick(datetime(2026,10,8,10,56,3))
            self.assertEqual(dispatch.call_count,1)
        self.assertEqual(service.get_report('alice',plan['plan_id'])['trials'][0]['status'],'CLOSED')

    def test_open_result_timeout_settles_at_current_price_and_restarts(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'next'}) as dispatch:
            self.quote('2026-10-08T10:45:12')
            service.tick(datetime(2026,10,8,10,45,12))
            self.quote('2026-10-08T10:55:09',last=104,bid=103,ask=104)
            service.tick(datetime(2026,10,8,10,55,9))
            dispatch.assert_not_called()
            self.quote('2026-10-08T10:55:10',last=104,bid=103,ask=104)
            service.tick(datetime(2026,10,8,10,55,10))
            self.assertEqual(dispatch.call_count, 1)
        report = service.get_report('alice',plan['plan_id'])
        row = report['trials'][0]
        self.assertEqual(row['status'], 'CLOSED')
        self.assertEqual(row['state']['exit_reason'], 'RESULT_TIMEOUT')
        self.assertEqual(row['state']['exit_time'], '2026-10-08T10:55:10')
        self.assertEqual(row['state']['exit_price'], 102)
        self.assertEqual(report['report']['losses'], 0)
        self.assertEqual(report['report']['wins'], 1)
        self.assertEqual(row['state']['outcome_policy'], 'ACTUAL_NET')

    def test_overdue_position_report_shows_pending_quote_and_blocks_new_analysis(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        with patch.object(service,'dispatch_analysis') as dispatch:
            self.quote('2026-10-08T10:45:12')
            service.tick(datetime(2026,10,8,10,45,12))
            service.tick(datetime(2026,10,8,10,55,10))
            dispatch.assert_not_called()
        report = service.get_report('alice',plan['plan_id'])
        self.assertTrue(report['trials'][0]['state']['settlement_pending'])
        self.assertEqual(report['report']['settlement_pending_count'],1)
        self.assertEqual(report['activity']['phase'],'WAITING_SETTLEMENT_QUOTE')
        self.assertEqual(report['report']['closed_trades'],0)
        self.assertFalse(report['report']['is_complete'])

    def test_trade_outcome_pre_entry_invalidation_restarts(self):
        plan=self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'next'}) as dispatch:
            self.quote('2026-10-08T10:45:12',last=89,bid=89,ask=90)
            service.tick(datetime(2026,10,8,10,45,12))
            self.assertEqual(dispatch.call_count,1)
        row=service.get_report('alice',plan['plan_id'])['trials'][0]
        self.assertIn('PRE_ENTRY_INVALIDATED',row['reason'])
        report = service.get_report('alice',plan['plan_id'])['report']
        self.assertEqual(report['pre_entry_losses'],1)
        self.assertEqual(report['losses'],1)
        self.assertEqual(report['closed_trades'],0)

    def test_upgrade_outcome_preserves_history_and_cancels_old_pending_schedule(self):
        plan=self.create(driven=True)
        self.submit(plan)
        self.finish_run('batch-test')
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'pending-new'}):
            service.tick(datetime(2026,10,8,10,45,12))
        upgraded=service.upgrade_trade_outcome_schedule('alice',plan['plan_id'],datetime(2026,10,8,10,46))
        self.assertEqual(upgraded['config']['rule_version'],'1.3')
        self.assertEqual(len(upgraded['config']['schedule_history']),1)
        report=service.get_report('alice',plan['plan_id'])
        self.assertEqual(len(report['trials']),2)
        self.assertTrue(report['trials'][1]['state']['analysis_expired'])
        self.assertNotIn('next_analysis_due_at',report['trials'][1]['state'])
        self.assertAlmostEqual(report['trials'][0]['model_cost'],.002)
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'new-rule'}) as dispatch:
            service.tick(datetime(2026,10,8,10,46))
            self.assertEqual(dispatch.call_count,1)
        again=service.upgrade_trade_outcome_schedule('alice',plan['plan_id'],datetime(2026,10,8,10,47))
        self.assertEqual(len(again['config']['schedule_history']),1)

    def test_direction_result_immediately_dispatches_next_analysis_once(self):
        plan = self.create(driven=True)
        self.assertEqual(plan['config']['analysis_schedule_mode'], 'RESULT_DRIVEN')
        self.submit(plan)
        self.finish_run('batch-test')
        with patch.object(service,'dispatch_analysis', return_value={'batch_id':'next-batch'}) as dispatch:
            service.tick(datetime(2026,10,8,10,45,12))
            service.tick(datetime(2026,10,8,10,45,13))
            self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(len(service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,45,13))['trials']), 2)

    def test_no_direction_waits_five_minutes_from_observed_result(self):
        plan = self.create(driven=True)
        self.submit(plan)
        self.finish_run('batch-test', direction='NO_JUDGMENT')
        with patch.object(service,'dispatch_analysis', return_value={'batch_id':'next-batch'}) as dispatch:
            service.tick(datetime(2026,10,8,10,45,12))
            service.tick(datetime(2026,10,8,10,50,11))
            dispatch.assert_not_called()
            service.tick(datetime(2026,10,8,10,50,12))
            self.assertEqual(dispatch.call_count, 1)

    def test_timeout_restarts_and_late_result_only_settles_cost(self):
        plan = self.create(driven=True)
        with patch.object(service,'dispatch_analysis',side_effect=self.dispatch_pending):
            service.tick(datetime(2026,10,8,10,45))
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'next-batch'}) as dispatch:
            service.tick(datetime(2026,10,8,10,54,59))
            dispatch.assert_not_called()
            service.tick(datetime(2026,10,8,10,55))
            self.assertEqual(dispatch.call_count, 1)
        report=service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,55))
        self.assertEqual(report['trials'][0]['status'], 'SKIPPED')
        self.assertIn('10分钟',report['trials'][0]['reason'])
        self.assertFalse(report['trials'][0]['model_cost_known'])
        self.finish_run('batch-test')
        self.alter_run(completed_at=datetime(2026,10,8,2,55,10))
        with patch.object(service,'dispatch_analysis') as dispatch:
            service.tick(datetime(2026,10,8,10,55,12))
            dispatch.assert_not_called()
        report=service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,55,12))
        self.assertEqual(report['trials'][0]['status'], 'SKIPPED')
        self.assertTrue(report['trials'][0]['model_cost_known'])
        self.assertAlmostEqual(report['trials'][0]['model_cost'],.002)

    def test_driven_session_end_does_not_dispatch_successor(self):
        plan=self.create(driven=True)
        self.submit(plan)
        self.finish_run('batch-test')
        self.alter_run(completed_at=datetime(2026,10,8,3,30))
        with patch.object(service,'dispatch_analysis') as dispatch:
            service.tick(datetime(2026,10,8,11,30))
            dispatch.assert_not_called()

    def prepare_successor(self, due):
        plan = self.create(outcome=True)
        self.submit(plan)
        with SessionLocal() as db:
            row = db.query(VerificationTrial).filter_by(plan_id=plan['plan_id']).one()
            row.status = 'SKIPPED'
            row.state_json = json.dumps({'model_cost':0, 'model_cost_known':True,
                                        'next_analysis_due_at':due})
            db.commit()
        return plan

    def test_last_five_minutes_excludes_successor_and_monitoring_window(self):
        plan = self.prepare_successor('2026-10-08T11:25:00')
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'next'}) as dispatch:
            service.tick(datetime(2026,10,8,11,25))
            service.tick(datetime(2026,10,8,11,29,59))
            dispatch.assert_not_called()
        report = service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,11,29,59))
        self.assertEqual(len(report['trials']), 1)
        self.assertEqual(report['monitoring']['expected_trials'], 1)
        self.assertEqual(report['activity']['phase'],'END_BUFFER')
        self.assertNotEqual(report['activity']['next_at'],'2026-10-08T11:25:00')

    def test_second_before_cutoff_can_dispatch_but_delayed_scan_cannot(self):
        plan = self.prepare_successor('2026-10-08T11:24:59')
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'next'}) as dispatch:
            service.tick(datetime(2026,10,8,11,24,59))
            self.assertEqual(dispatch.call_count, 1)

    def test_scan_delayed_into_last_five_minutes_does_not_dispatch(self):
        plan = self.prepare_successor('2026-10-08T11:24:59')
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'next'}) as dispatch:
            service.tick(datetime(2026,10,8,11,25))
            dispatch.assert_not_called()
        row = service.get_report('alice',plan['plan_id'])['trials'][-1]
        self.assertEqual(row['status'], 'SKIPPED')
        self.assertTrue(row['reason'].startswith('SESSION_CUTOFF:'))
        self.assertEqual(row['model_cost'], 0)

    def test_final_five_minutes_still_settles_existing_position(self):
        plan = self.create(outcome=True)
        self.submit(plan)
        self.finish_run('batch-test')
        self.alter_run(completed_at=datetime(2026,10,8,3,15,10))
        with SessionLocal() as db:
            row = db.query(VerificationTrial).filter_by(plan_id=plan['plan_id']).one()
            state = json.loads(row.state_json)
            state['analysis_deadline_at'] = '2026-10-08T11:25:00'
            row.state_json = json.dumps(state)
            db.commit()
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'next'}) as dispatch:
            self.quote('2026-10-08T11:15:12')
            service.tick(datetime(2026,10,8,11,15,12))
            self.quote('2026-10-08T11:25:10',last=104,bid=103,ask=104)
            service.tick(datetime(2026,10,8,11,25,10))
            dispatch.assert_not_called()
        row = service.get_report('alice',plan['plan_id'])['trials'][0]
        self.assertEqual(row['status'], 'CLOSED')
        self.assertEqual(row['state']['exit_reason'], 'RESULT_TIMEOUT')

    def test_driven_stop_keeps_tracking_expired_model_cost(self):
        plan=self.create(driven=True)
        with patch.object(service,'dispatch_analysis',side_effect=self.dispatch_pending):
            service.tick(datetime(2026,10,8,10,45))
        service.stop_plan('alice',plan['plan_id'],now=datetime(2026,10,8,10,46))
        with patch.object(service,'dispatch_analysis') as dispatch:
            service.tick(datetime(2026,10,8,10,55))
            dispatch.assert_not_called()
        self.assertEqual(service.get_report('alice',plan['plan_id'])['plan']['status'],'STOPPING')
        self.finish_run('batch-test')
        self.alter_run(completed_at=datetime(2026,10,8,2,55,10))
        service.tick(datetime(2026,10,8,10,55,12))
        report=service.get_report('alice',plan['plan_id'])
        self.assertEqual(report['plan']['status'],'STOPPED')
        self.assertAlmostEqual(report['report']['model_cost'],.002)

    def test_result_at_deadline_is_void_even_if_scanned_without_prior_timeout(self):
        plan=self.create(driven=True)
        self.submit(plan)
        self.finish_run('batch-test')
        self.alter_run(completed_at=datetime(2026,10,8,2,55))
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'next'}) as dispatch:
            service.tick(datetime(2026,10,8,10,55))
            self.assertEqual(dispatch.call_count,1)
        row=service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,55))['trials'][0]
        self.assertEqual(row['status'],'SKIPPED')
        self.assertTrue(row['model_cost_known'])
        self.assertAlmostEqual(row['model_cost'],.002)

    def test_pending_restart_does_not_duplicate_and_timeout_uses_actual_submission(self):
        plan=self.create(driven=True)
        with patch.object(service,'dispatch_analysis',side_effect=self.dispatch_pending) as dispatch:
            service.tick(datetime(2026,10,8,10,45,40))
            service.tick(datetime(2026,10,8,10,45,42))
            service.tick(datetime(2026,10,8,10,55,39))
            self.assertEqual(dispatch.call_count,1)
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'next'}) as dispatch:
            service.tick(datetime(2026,10,8,10,55,40))
            self.assertEqual(dispatch.call_count,1)

    def test_switch_to_driven_preserves_old_monitoring_scope(self):
        plan=self.create(repeat=True)
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'old'}):
            service.tick(datetime(2026,10,8,10,45))
        with SessionLocal() as db:
            p=db.query(VerificationPlan).filter_by(plan_id=plan['plan_id']).one()
            old=json.loads(p.config_json)
            config={**old,'analysis_schedule_mode':'RESULT_DRIVEN',
                    'analysis_rule_effective_at':'2026-10-08T10:50:00',
                    'schedule_history':[{'config':old,'start':'2026-10-08T10:00:00',
                                         'end':'2026-10-08T10:50:00'}]}
            p.config_json=json.dumps(config)
            db.commit()
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'new'}) as dispatch:
            service.tick(datetime(2026,10,8,10,50))
            self.assertEqual(dispatch.call_count,1)
        report=service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,10,50))
        self.assertEqual(report['monitoring']['expected_trials'],2)
        self.assertEqual([r['analysis_due_at'][-8:] for r in report['trials']],['10:45:00','10:50:00'])

    def test_subsecond_next_window_is_not_duplicated_after_database_roundtrip(self):
        plan=self.create(driven=True)
        self.submit(plan)
        self.finish_run('batch-test')
        with patch.object(service,'dispatch_analysis',return_value={'batch_id':'next'}) as dispatch:
            service.tick(datetime(2026,10,8,10,45,12,123456))
            service.tick(datetime(2026,10,8,10,45,14,123456))
            service.tick(datetime(2026,10,8,10,45,16,123456))
            self.assertEqual(dispatch.call_count,1)

    def test_expired_new_result_does_not_block_legacy_pending_result(self):
        plan=self.create()
        self.submit(plan)
        self.finish_run('batch-test')
        self.alter_run(completed_at=datetime(2026,10,8,3,2))
        with SessionLocal() as db:
            db.add(VerificationTrial(trial_id=str(uuid.uuid4()),plan_id=plan['plan_id'],
                trading_day='20261008',session_label='late',session_start=datetime(2026,10,8,10,30),
                session_end=datetime(2026,10,8,11,30),analysis_due_at=datetime(2026,10,8,10,50),
                result_available_at=datetime(2026,10,8,11,1),status='SKIPPED',
                state_json=json.dumps({'analysis_expired':True,'model_cost':0,'model_cost_known':True})))
            db.commit()
        service.tick(datetime(2026,10,8,11,2))
        report=service.get_report('alice',plan['plan_id'],now=datetime(2026,10,8,11,2))
        self.assertEqual(report['trials'][0]['status'],'WAITING_TRIGGER')

if __name__ == '__main__':
    unittest.main()
