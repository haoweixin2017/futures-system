import json
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import test_bootstrap
except ImportError:
    from tests import test_bootstrap

from backend.ai_analysis import snapshot, provider, orchestrator, settlement, research_cache
from backend.models.market_data import BarData
from tests import test_ai_analysis as ai_tests
from backend.models.database import AIAnalysisBatchModel, AIAnalysisCallModel, AIAnalysisRunModel, AIAnalysisEvaluationModel
from backend.storage.mysql_client import SessionLocal
from backend.ai_analysis.future_trend import targets, DIMENSIONS, EXTERNAL_DIMENSIONS


def bar(day, time, period):
    return BarData(instrument='FU2611', trading_day=day, period=period,
        bar_time=datetime.fromisoformat(time), open_price=Decimal('4000'),
        high_price=Decimal('4010'), low_price=Decimal('3990'), close_price=Decimal('4001'),
        volume=100, turnover=Decimal('400000'), open_interest=Decimal('1000'))


class FutureIntegrationTests(unittest.TestCase):
    def test_semantic_review_requires_all_checks_and_rejects_unresolved_claim(self):
        snap = {'review_policy_version':'1', 'public_materials':{'sources':[]}}
        reviewed = {'summary':'持仓下降，不能确认多空身份'}
        with self.assertRaises(ValueError):
            orchestrator._validate_semantic_review(reviewed, snap, {'summary':'多头获利了结'})
        reviewed['semantic_review'] = {key:{'status':'NOT_APPLICABLE','reason':'没有相关论断'}
            for key in ('position_identity','event_status','statistical_scope','quality_limits')}
        reviewed['semantic_review']['position_identity'] = {'status':'UNRESOLVED','reason':'无法确认资金身份'}
        with self.assertRaisesRegex(ValueError, 'position_identity'):
            orchestrator._validate_semantic_review(reviewed, snap, {'summary':'多头获利了结'})

    def test_semantic_revision_must_point_to_original_and_actual_replacement(self):
        snap = {'review_policy_version':'1', 'public_materials':{'sources':[]}}
        draft = {'summary':'多头获利了结'}
        reviewed = {'summary':'持仓下降，不能确认多空身份', 'semantic_review':{
            key:{'status':'NOT_APPLICABLE','reason':'没有相关论断'}
            for key in ('position_identity','event_status','statistical_scope','quality_limits')}}
        check = reviewed['semantic_review']['position_identity']
        check.update(status='REVISED', reason='总持仓变化不能证明身份',
                     original_claim='多头获利了结', revised_claim='仍有多头踩踏')
        with self.assertRaises(ValueError):
            orchestrator._validate_semantic_review(reviewed, snap, draft)
        check['revised_claim'] = reviewed['summary']
        orchestrator._validate_semantic_review(reviewed, snap, draft)

    def test_future_snapshot_unifies_oi_changes_and_separates_quote_aggregates(self):
        quote = {'trading_day':'20260930', 'timestamp':'2026-09-30T14:59:59',
                 'last_price':'4435', 'day_open':'4280', 'day_high':'4457',
                 'volume':'663490', 'open_interest':'126738'}
        rows = [bar('20260929','2026-09-29T15:00:00','1d'),
                bar('20260930','2026-09-30T15:00:00','1d')]
        rows[0].open_interest = Decimal('150615')
        rows[1].open_interest = Decimal('126738')
        rows[1].open_interest_change = Decimal('-40957')
        with patch.object(snapshot,'_instrument_meta',return_value=('燃料油',10,Decimal('1'))), \
             patch.object(snapshot,'_load_future_bars',side_effect=lambda i,p,*a: rows if p=='1d' else []):
            snap, _ = snapshot.build_future_snapshot('FU2611', quote, '', datetime(2026,10,2,19))
        daily = snap['market']['daily_bars_1d']
        self.assertIsNone(daily[0]['open_interest_change'])
        self.assertEqual(Decimal(daily[1]['open_interest_change']), Decimal('-23877'))
        self.assertEqual(daily[1]['trading_day'], '20260930')
        self.assertNotIn('volume', snap['market']['quote'])
        self.assertNotIn('day_open', snap['market']['quote'])
        self.assertEqual(snap['market']['raw_quote']['volume'], '663490')
        model = orchestrator._model_snapshot(json.dumps(snap))
        self.assertNotIn('raw_quote', model['market'])
        self.assertIn('相邻', model['market']['data_scope'])
        self.assertEqual(snap['contract_lifecycle']['targets'][0]['status'], 'WITHIN_RULE_TERM')

    def test_model_snapshot_filters_background_without_changing_saved_record(self):
        frozen = {'public_materials': {'sources':[
            {'source_id':'S1','excerpt':'近期库存'},
            {'source_id':'S2','excerpt':'旧库存','stale':True}],
            'background_sources':[{'source_id':'S3','excerpt':'更旧资料'}],
            'research':{'status':'PARTIAL','events':[{'type':'MODEL','message':'UNTRUSTED_RESEARCH_TEXT'}]}}}
        stored = json.dumps(frozen)
        model = orchestrator._model_snapshot(stored)
        self.assertEqual([s['source_id'] for s in model['public_materials']['sources']], ['S1'])
        self.assertNotIn('background_sources', model['public_materials'])
        self.assertNotIn('events', model['public_materials']['research'])
        self.assertEqual(json.loads(stored), frozen)

    def test_snapshot_filters_current_daily_and_future_intraday(self):
        quote = {'trading_day': '20260928', 'timestamp': '2026-09-28T10:01:00', 'last_price': '4000'}
        redis = Mock(); redis.hgetall.return_value = quote
        rows = {'1d': [bar('20260924', '2026-09-24T09:00:00', '1d'),
                       bar('20260928', '2026-09-28T09:00:00', '1d')],
                '60m': [bar('20260928', '2026-09-28T09:00:00', '60m'),
                        bar('20260928', '2026-09-28T10:00:00', '60m')],
                '15m': [bar('20260928', '2026-09-28T09:45:00', '15m')], '1m': []}
        with patch.object(snapshot.RedisClient, 'get_client', return_value=redis), \
             patch.object(snapshot, '_instrument_meta', return_value=('燃料油', 10, Decimal('1'))), \
             patch.object(snapshot, '_load_future_bars', side_effect=lambda i,p,*a: rows.get(p,[])), \
             patch.object(snapshot, '_load_bars', side_effect=lambda i,p,*a: rows.get(p,[])), \
             patch('backend.ai_analysis.future_trend.collect_materials') as collect:
            snap, digest = snapshot.build_future_snapshot('FU2611', quote, '', datetime(2026,9,28,10,1))
        self.assertEqual(len(snap['market']['daily_bars_1d']), 1)
        self.assertEqual(len(snap['market']['bars_60m']), 1)
        self.assertFalse(snap['quality']['can_judge'])
        self.assertEqual(snap['analysis_target'], 'FUTURE_TREND')
        self.assertEqual(len(digest), 64)
        self.assertEqual([t['horizon_days'] for t in snap['future_targets']], [3, 5])
        collect.assert_not_called()
        self.assertEqual(snap['public_materials']['research']['status'], 'NOT_REQUESTED')

    def test_future_prompt_and_blocked_result_have_both_horizons(self):
        snap = {'analysis_target':'FUTURE_TREND', 'instrument':{'symbol':'FU2611'},
                'market':{'trading_day':'20260930','snapshot_time':'2026-10-01T21:00:00+08:00'},
                'quality':{'can_judge':False,'blocking_reasons':['INSUFFICIENT_DAILY']},
                'future_targets':[{'horizon_days':3,'trading_day':'20261012'},
                                  {'horizon_days':5,'trading_day':'20261014'}],
                'public_materials':{'sources':[],'missing_dimensions':['macro','fundamentals','events','related_markets']}}
        prompt = orchestrator._system_prompt(snap)
        self.assertIn('horizon_forecasts', prompt)
        result = orchestrator._blocked_result(snap)
        self.assertEqual([f['direction'] for f in result['horizon_forecasts']], ['NO_JUDGMENT']*2)
        self.assertEqual(result['dimensions']['macro']['status'], 'MISSING')

    def test_majority_is_independent_for_three_and_five_days(self):
        def draft(d3,d5):
            return {'direction':d5,'confidence_score':70,'summary':'判断',
                    'horizon_forecasts':[{'horizon_days':3,'direction':d3}, {'horizon_days':5,'direction':d5}]}
        result = orchestrator._majority_draft([draft('LONG','SHORT'),draft('LONG','SHORT'),draft('SHORT','LONG')], 'FUTURE_TREND',3)
        self.assertEqual(result['horizon_forecasts'][0]['direction'],'LONG')
        self.assertEqual(result['horizon_forecasts'][1]['direction'],'SHORT')
        result = orchestrator._majority_draft([draft('LONG','LONG'),draft('SHORT','LONG')], 'FUTURE_TREND',2)
        self.assertEqual(result['horizon_forecasts'][0]['direction'],'NO_JUDGMENT')
        self.assertEqual(result['horizon_forecasts'][1]['direction'],'LONG')

    def test_majority_explanation_comes_from_winning_fifth_day(self):
        def draft(direction, summary):
            return {'direction':direction,'summary':summary,'confidence_score':70,
                    'horizon_forecasts':[{'horizon_days':d,'direction':direction} for d in (3,5)]}
        result = orchestrator._majority_draft([
            draft('SHORT','少数看空解释'), draft('LONG','多数看多解释'), draft('LONG','看多')], 'FUTURE_TREND',3)
        self.assertEqual(result['summary'],'多数看多解释')

    def test_four_horizons_vote_independently(self):
        def draft(directions):
            return {'direction':directions[1], 'confidence_score':70,
                    'summary':'test', 'horizon_forecasts':[
                        {'horizon_days':d, 'direction':direction, 'confidence_score':70}
                        for d, direction in zip((3,5,7,15), directions)]}
        result = orchestrator._majority_draft([
            draft(('LONG','SHORT','LONG','SHORT')),
            draft(('LONG','SHORT','SHORT','LONG')),
            draft(('SHORT','LONG','SHORT','LONG'))], 'FUTURE_TREND', 3)
        self.assertEqual([f['horizon_days'] for f in result['horizon_forecasts']], [3,5,7,15])
        self.assertEqual([f['direction'] for f in result['horizon_forecasts']],
                         ['LONG','SHORT','SHORT','LONG'])
        self.assertEqual(result['direction'], 'SHORT')

    def test_missing_horizon_does_not_receive_partial_correct_score(self):
        snap = {'future_targets':[{'horizon_days':3},{'horizon_days':5}]}
        parsed = {'future_metrics':{'3':{'rating':'WRONG','reference_price':4000,'realized_price':3900},
                                    '5':{'rating':'UNVERIFIABLE','reference_price':4000,'realized_price':None}}}
        result = settlement._settle_future(Mock(), Mock(), snap, parsed, datetime(2026,10,15))
        self.assertEqual(result['rating'],'UNVERIFIABLE')

    def test_partial_settlement_keeps_fifth_day_pending(self):
        db = Mock()
        db.query.return_value.filter_by.return_value.first.return_value = SimpleNamespace(close_price=4100)
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            SimpleNamespace(high_price=4120,low_price=3980,bar_time=datetime(2026,10,12,14,59))]
        snap = {'instrument':{'price_tick':1}, 'market':{'snapshot_time':'2026-10-01T21:00:00+08:00'},
                'future_targets':[{'horizon_days':d,'trading_day':day,'bar_time':f'{date}T14:59:00',
                    'due_time':f'{date}T15:02:00','price_field':'close_price','reference_price':4000}
                    for d,day,date in [(3,'20261012','2026-10-12'),(5,'20261014','2026-10-14')]]}
        result = {'direction':'SHORT','horizon_forecasts':[{'horizon_days':3,'direction':'LONG'}, {'horizon_days':5,'direction':'SHORT'}]}
        outcome = settlement._settle_future(db, SimpleNamespace(instrument_id='FU2611'), snap,result, datetime(2026,10,12,16))
        self.assertIsNone(outcome)
        self.assertEqual(result['future_metrics']['3']['rating'], 'CORRECT')
        self.assertNotIn('5',result['future_metrics'])
        self.assertAlmostEqual(result['future_metrics']['3']['max_rise_pct'],3)

    @staticmethod
    def _settle_targets():
        return [{'horizon_days':d,'trading_day':day,'bar_time':f'{date}T14:59:00',
                 'due_time':f'{date}T15:02:00','price_field':'close_price','reference_price':4000}
                for d,day,date in [(3,'20261012','2026-10-12'),(5,'20261014','2026-10-14')]]

    def test_missing_target_bar_within_grace_keeps_pending(self):
        # 到期扫描与行情缺口回补存在竞态: 宽限期内缺目标行情不落终态, 等待回补后重评
        db = Mock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        snap = {'instrument':{'price_tick':1}, 'market':{'snapshot_time':'2026-10-01T21:00:00+08:00'},
                'future_targets':self._settle_targets()}
        result = {'direction':'SHORT','horizon_forecasts':[{'horizon_days':3,'direction':'LONG'}, {'horizon_days':5,'direction':'SHORT'}]}
        outcome = settlement._settle_future(db, SimpleNamespace(instrument_id='FU2611'), snap,result, datetime(2026,10,13,16))
        self.assertIsNone(outcome)
        self.assertNotIn('3', result['future_metrics'])
        self.assertNotIn('5', result['future_metrics'])

    def test_missing_target_bar_after_grace_settles_unverifiable(self):
        db = Mock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        snap = {'instrument':{'price_tick':1}, 'market':{'snapshot_time':'2026-10-01T21:00:00+08:00'},
                'future_targets':self._settle_targets()}
        result = {'direction':'SHORT','horizon_forecasts':[{'horizon_days':3,'direction':'LONG'}, {'horizon_days':5,'direction':'SHORT'}]}
        # 5日期限到期为10-14, 宽限期至10-17, now 取两个期限宽限期之外
        outcome = settlement._settle_future(db, SimpleNamespace(instrument_id='FU2611'), snap,result, datetime(2026,10,18,12))
        self.assertEqual(outcome['rating'], 'UNVERIFIABLE')
        self.assertIsNone(outcome['realized'])
        metric = result['future_metrics']['3']
        self.assertEqual(metric['rating'], 'UNVERIFIABLE')
        self.assertEqual(metric['reference_price'], 4000)
        self.assertIsNone(metric['threshold'])
        self.assertEqual(metric['sample_status'], 'NO_BARS')

    def test_invalid_frozen_target_without_quote_does_not_crash(self):
        # 冻结目标无效且快照无报价: 直接落 UNVERIFIABLE 终态, 不抛 TypeError
        snap = {'future_targets':[
            {'horizon_days':d,'trading_day':day,'bar_time':f'{date}T14:59:00',
             'due_time':f'{date}T15:02:00','price_field':'close_price','reference_price':None}
            for d,day,date in [(3,'20261012','2026-10-12'),(5,'20261014','2026-10-14')]]}
        result = {'direction':'SHORT','horizon_forecasts':[{'horizon_days':3,'direction':'LONG'}, {'horizon_days':5,'direction':'SHORT'}]}
        outcome = settlement._settle_future(Mock(), SimpleNamespace(instrument_id='FU2611'), snap,result, datetime(2026,10,13,16))
        self.assertEqual(outcome['rating'], 'UNVERIFIABLE')
        self.assertIsNone(outcome['reference'])
        self.assertEqual(result['future_metrics']['5']['rating'], 'UNVERIFIABLE')
        self.assertIsNone(result['future_metrics']['5']['threshold'])


class FutureAPITests(unittest.TestCase):
    setUp = ai_tests.AIAnalysisTests.setUp
    login = ai_tests.AIAnalysisTests.login
    create_model = ai_tests.AIAnalysisTests.create_model
    model_body = staticmethod(ai_tests.AIAnalysisTests.model_body)
    seed_market = ai_tests.AIAnalysisTests.seed_market

    def _pending_research_batch(self, two_models=False):
        models = [self.create_model()]
        if two_models:
            body = self.model_body()
            body['display_name'] = '研究备用模型'
            response = ai_tests.client.post('/api/v1/ai/models', json=body)
            self.assertEqual(response.status_code, 201, response.text)
            models.append(response.json())
        self.seed_market()
        snap, digest = snapshot.build_snapshot('RB2610')
        snap['analysis_target'] = 'FUTURE_TREND'
        snap['quality']['can_judge'] = True
        snap['public_materials'] = {'status':'PENDING', 'sources':[], 'background_sources':[],
            'missing_dimensions':list(EXTERNAL_DIMENSIONS), 'errors':[],
            'research':{'status':'PENDING','events':[]}}
        with patch.object(snapshot, 'build_snapshot', return_value=(snap, digest)), \
             patch.object(research_cache, 'freeze_materials', return_value=snap['public_materials']), \
             patch.object(orchestrator, 'submit_batch'):
            created = ai_tests.client.post('/api/v1/ai/analysis/RB2610', json={
                'client_request_id':'ef7d9e3b-fbd1-4771-8d0c-278773ab7433',
                'analysis_target':'FUTURE_TREND','use_review_summaries':False,
                'model_config_ids':[m['model_config_id'] for m in models]})
        self.assertEqual(created.status_code, 202, created.text)
        return created.json()['batch_id'], snap

    def test_restart_marks_pending_research_interrupted_without_reissuing(self):
        batch_id, _ = self._pending_research_batch()
        with patch.object(orchestrator.executor,'submit') as submit:
            orchestrator.mark_interrupted()
        detail = ai_tests.client.get(f'/api/v1/ai/analysis/batches/{batch_id}').json()
        self.assertEqual(detail['public_materials']['research']['status'], 'INTERRUPTED')
        submit.assert_not_called()

    def _check_citation_correction(self, correction_valid, identity_field=None, semantic=None, retry=False, unverifiable=False):
        batch_id, snap = self._pending_research_batch()
        snap['future_targets'] = targets('RB2610', datetime(2026,9,16,10,40), '20260916', 100)
        snap['contract_lifecycle'] = {'status':'RULE_BASED', 'targets':[
            {**t, 'status':'WITHIN_RULE_TERM'} for t in snap['future_targets']]}
        snap['public_materials'] = {'sources':[
            {'source_id':'S1','dimension':'fundamentals','excerpt':'库存下降，需求增加。'},
            {'source_id':'S2','dimension':'macro','excerpt':'工业增加值持续增长。'}],
            'missing_dimensions':['events','related_markets']}
        valid = {**ai_tests.required_fields(), 'instrument':'RB2610', 'trading_day':'20260916',
            'snapshot_time':snap['market']['snapshot_time'], 'direction':'LONG', 'confidence_score':70,
            'evidence':ai_tests.evidence_for(snap), 'alternative_scenario':'跌破支撑转空',
            'horizon_forecasts':[{'horizon_days':t['horizon_days'],'trading_day':t['trading_day'],
                'direction':'LONG','confidence_score':70,'summary':'等待确认',
                'entry_condition':'站稳压力','invalidation_condition':'跌破支撑'} for t in snap['future_targets']],
            'dimensions':{d:{'status':'MISSING' if d in EXTERNAL_DIMENSIONS else 'AVAILABLE',
                'summary':'行情依据或资料不足','citations':[]} for d in DIMENSIONS}}
        valid['dimensions']['fundamentals'].update(status='AVAILABLE',
            citations=[{'source_id':'S1','quote':'库存下降'}])
        invalid = json.loads(json.dumps(valid))
        if identity_field:
            invalid[identity_field] = 'SC2611' if identity_field == 'instrument' else '20260915'
        else:
            invalid['dimensions']['fundamentals']['citations'] = [{'source_id':'S2','quote':'工业增加值'}]
        if unverifiable:
            invalid['dimensions']['fundamentals']['citations'][0]['quote'] = '不在冻结正文中的改写'
        session = SessionLocal()
        try:
            batch = session.query(AIAnalysisBatchModel).filter_by(batch_id=batch_id).one()
            batch.snapshot_json = json.dumps(snap)
            run_id = session.query(AIAnalysisRunModel).filter_by(batch_id=batch_id).one().run_id
            session.commit()
        finally: session.close()
        reviewed = json.loads(json.dumps(valid))
        if semantic:
            snap['review_policy_version'] = '1'
            db = SessionLocal()
            try:
                db.query(AIAnalysisBatchModel).filter_by(batch_id=batch_id).one().snapshot_json = json.dumps(snap)
                db.commit()
            finally:
                db.close()
            if semantic == 'revision':
                valid['summary'] = '多头获利了结；计划释储已实现供应'
                reviewed['summary'] = '持仓下降无法确认多空身份；计划释储尚未实施'
                reviewed['semantic_review'] = {key:{'status':'NOT_APPLICABLE','reason':'没有相关论断'}
                    for key in ('position_identity','event_status','statistical_scope','quality_limits')}
                for key in ('position_identity', 'event_status'):
                    reviewed['semantic_review'][key] = {'status':'REVISED', 'reason':'原论断无证据支持',
                        'original_claim':valid['summary'], 'revised_claim':reviewed['summary']}
        outputs = [invalid, valid if correction_valid else invalid, reviewed]
        if retry:
            outputs.insert(0, provider.ProviderHTTPError('429', status_code=429, retry_after=0, request_id='retry-test'))
        def completion(*args, **kwargs):
            output = outputs.pop(0)
            if isinstance(output, Exception):
                raise output
            content = json.dumps(output)
            return provider.ProviderResult(200, content, content, 'correction-test', 50, 10, 1)
        with patch.object(provider,'chat_completion',side_effect=completion) as chat:
            orchestrator.run_analysis(run_id)
        self.assertEqual(chat.call_count, (3 if correction_valid else 2) + int(retry))
        correction = chat.call_args_list[1 + int(retry)].args[3]['messages']
        if identity_field:
            original = chat.call_args_list[0].args[3]['messages']
            self.assertEqual(correction[:2], original)
            frozen = json.loads(correction[1]['content'])
            self.assertEqual(frozen['market'], snap['market'])
            self.assertEqual(frozen['future_targets'], snap['future_targets'])
            self.assertIn('horizon_forecasts', correction[-1]['content'])
        self.assertEqual(json.loads(correction[1]['content'])['public_materials'], snap['public_materials'])
        self.assertEqual(json.loads(correction[-2]['content']), invalid)
        if not identity_field:
            if not unverifiable:
                self.assertIn('fundamentals', correction[-1]['content'])
            self.assertIn('S2', correction[-1]['content'])
        detail = ai_tests.client.get(f'/api/v1/ai/analysis/batches/{batch_id}').json()
        run = detail['runs'][0]
        self.assertEqual(run['status'], 'SUCCEEDED' if correction_valid else 'UNSTRUCTURED')
        self.assertEqual(run['call_count'], chat.call_count)
        session = SessionLocal()
        try:
            calls = session.query(AIAnalysisCallModel).filter_by(run_id=run_id).order_by(AIAnalysisCallModel.sequence_no).all()
            self.assertEqual([c.call_type for c in calls],
                (['SINGLE','SINGLE_RETRY','CORRECTION','REVIEW'] if retry else
                 ['SINGLE','CORRECTION','REVIEW'] if correction_valid else ['SINGLE','CORRECTION']))
            if retry:
                self.assertEqual(calls[0].http_status, 429)
                self.assertEqual(calls[0].provider_request_id, 'retry-test')
                self.assertEqual(calls[1].status, 'SUCCEEDED')
        finally: session.close()
        if correction_valid:
            self.assertEqual(run['result']['dimensions']['fundamentals']['citations'][0]['source_id'], 'S1')
            if semantic:
                self.assertEqual(run['result']['publication_status'], 'REVIEWED' if semantic == 'revision' else 'DRAFT_ONLY')
                self.assertEqual(run['result']['review_status'], 'SUCCEEDED' if semantic == 'revision' else 'FAILED_DRAFT_RETAINED')
                if semantic == 'revision':
                    self.assertEqual(run['result']['summary'], reviewed['summary'])
        else:
            self.assertIsNone(run['result'])
            self.assertIn('Unverifiable public source citation' if unverifiable else '引用维度不匹配', run['error'])

    def test_future_citation_mismatch_gets_one_audited_correction(self):
        self._check_citation_correction(True)

    def test_unverifiable_citation_gets_one_audited_correction(self):
        self._check_citation_correction(True, unverifiable=True)

    def test_unverifiable_citation_correction_failure_stops(self):
        self._check_citation_correction(False, unverifiable=True)

    def test_semantic_report_missing_retains_draft_in_api(self):
        self._check_citation_correction(True, semantic='missing')

    def test_rate_limit_retry_is_logged_once_and_review_sequence_remains_unique(self):
        self._check_citation_correction(True, retry=True)

    def test_identity_and_planned_supply_revision_is_persisted(self):
        self._check_citation_correction(True, semantic='revision')

    def test_unknown_contract_skips_models_and_records_rule_block(self):
        batch_id, snap = self._pending_research_batch(two_models=True)
        snap['future_targets'] = targets('RB2610', datetime(2026,9,16,10,40), '20260916', 100)
        snap['contract_lifecycle'] = {'status':'UNKNOWN', 'targets':[], 'limitations':'未核实'}
        db = SessionLocal()
        try:
            db.query(AIAnalysisBatchModel).filter_by(batch_id=batch_id).one().snapshot_json = json.dumps(snap)
            runs = [r.run_id for r in db.query(AIAnalysisRunModel).filter_by(batch_id=batch_id).all()]
            db.commit()
        finally:
            db.close()
        with patch.object(provider, 'chat_completion', side_effect=AssertionError('must not call')):
            for run_id in runs:
                orchestrator.run_analysis(run_id)
        detail = ai_tests.client.get(f'/api/v1/ai/analysis/batches/{batch_id}').json()
        for run in detail['runs']:
            self.assertEqual(run['status'], 'SUCCEEDED', run)
            self.assertEqual(run['call_count'], 0)
            self.assertFalse(run['result']['model_called'])
            self.assertEqual(run['result']['decision_origin'], 'RULE_BLOCKED')
            self.assertEqual([f['blocking_reason'] for f in run['result']['horizon_forecasts']],
                             ['CONTRACT_TERM_UNKNOWN'] * 2)

    def test_future_invalid_correction_stays_unstructured_without_more_retries(self):
        self._check_citation_correction(False)

    def test_future_instrument_correction_retains_full_frozen_input(self):
        self._check_citation_correction(True, 'instrument')

    def test_future_trading_day_correction_retains_full_frozen_input(self):
        self._check_citation_correction(True, 'trading_day')

    def test_future_api_accepts_model_instrument_object(self):
        parse = provider.parse_analysis_json

        def parse_object(content, *args, **kwargs):
            data = json.loads(content)
            data["instrument"] = {"symbol": data["instrument"], "name": "test"}
            return parse(json.dumps(data), *args, **kwargs)

        with patch.object(provider, "parse_analysis_json", side_effect=parse_object):
            self.test_future_api_model_history_and_settlement_preserve_manual_rating()

    def test_future_api_model_history_and_settlement_preserve_manual_rating(self):
        model = self.create_model()
        self.seed_market()
        snap, digest = snapshot.build_snapshot('RB2610')
        snap['analysis_target'] = 'FUTURE_TREND'
        snap['future_targets'] = targets('RB2610', datetime(2026,9,16,10,40), '20260916', 100)
        # Historical four-horizon snapshots retain their original validation/settlement.
        for days, day in [(7, '20260925'), (15, '20261014')]:
            date = datetime.strptime(day, '%Y%m%d').date().isoformat()
            snap['future_targets'].append({**snap['future_targets'][-1],
                'horizon_days':days, 'trading_day':day,
                'bar_time':date+'T14:59:00', 'due_time':date+'T15:02:00'})
        snap['contract_lifecycle'] = {'status':'RULE_BASED', 'targets':[
            {**t, 'status':'WITHIN_RULE_TERM'} for t in snap['future_targets']]}
        snap['public_materials'] = {'sources':[], 'background_sources':[
                                    {'source_id':'S99','excerpt':'OLD_BACKGROUND_MUST_NOT_REACH_MODEL','stale':True}],
                                    'missing_dimensions':list(EXTERNAL_DIMENSIONS),
                                    'errors':[], 'status':'MISSING'}
        final = {**ai_tests.required_fields(), 'instrument':'RB2610', 'trading_day':'20260916',
                 'snapshot_time':snap['market']['snapshot_time'], 'direction':'LONG', 'confidence_score':70,
                 'evidence':ai_tests.evidence_for(snap), 'alternative_scenario':'跌破支撑则转空',
                 'horizon_forecasts':[{'horizon_days':t['horizon_days'],'trading_day':t['trading_day'],
                    'direction':'LONG','confidence_score':70,'summary':'等待上破确认',
                    'entry_condition':'站稳压力','invalidation_condition':'跌破支撑'} for t in snap['future_targets']],
                 'dimensions':{d:{'status':'MISSING' if d in EXTERNAL_DIMENSIONS else 'AVAILABLE',
                    'summary':'缺少公开资料' if d in EXTERNAL_DIMENSIONS else '行情依据', 'citations':[]} for d in DIMENSIONS},
                 'future_metrics':{'3':{'rating':'CORRECT'},'5':{'rating':'CORRECT'}}}
        with patch.object(snapshot, 'build_snapshot', return_value=(snap,digest)), \
             patch.object(research_cache, 'freeze_materials', return_value=snap['public_materials']), \
             patch.object(orchestrator, 'submit_batch'):
            response = ai_tests.client.post('/api/v1/ai/analysis/RB2610', json={
                'client_request_id':'8360cc5a-954c-4a45-95be-a7eb229d6314',
                'model_config_ids':[model['model_config_id']], 'analysis_target':'FUTURE_TREND',
                'use_review_summaries':False})
        self.assertEqual(response.status_code,202,response.text)
        batch_id = response.json()['batch_id']
        db = SessionLocal()
        try:
            run_id = db.query(AIAnalysisRunModel).filter_by(batch_id=batch_id).one().run_id
        finally:
            db.close()
        fake = provider.ProviderResult(200, json.dumps(final), json.dumps(final), 'future-test',500,100,10)
        with patch.object(provider,'chat_completion',return_value=fake) as chat:
            orchestrator.run_analysis(run_id)
        self.assertNotIn('OLD_BACKGROUND_MUST_NOT_REACH_MODEL', str(chat.call_args.args[3]['messages']))
        detail = ai_tests.client.get(f'/api/v1/ai/analysis/batches/{batch_id}').json()
        self.assertEqual(detail['status'],'COMPLETED',detail)
        self.assertEqual(detail['future_targets'],snap['future_targets'])
        self.assertEqual([t['horizon_days'] for t in detail['future_targets']], [3,5,7,15])
        self.assertEqual(detail['public_materials'],snap['public_materials'])
        result = detail['runs'][0]['result']
        self.assertEqual([f['horizon_days'] for f in result['horizon_forecasts']], [3,5,7,15])
        self.assertNotIn('future_metrics',result)
        self.assertFalse(detail['use_review_summaries'])
        self.assertEqual(detail['used_review_summary_ids'],[])
        manual = ai_tests.client.put(f'/api/v1/ai/analysis/runs/{run_id}/evaluation',json={
            'rating':'WRONG','score':0,'comment':'人工评价保留'})
        self.assertEqual(manual.status_code,200,manual.text)
        def outcome(db,batch,frozen,now):
            target = frozen['settlement_target']
            due = datetime.fromisoformat(target['due_time'])
            if now < due: return None
            return {'reference':100,'realized':110 if target['horizon_days']==3 else None}
        with patch.object(settlement,'_settle_frozen_price',side_effect=outcome):
            settlement.settle_due_runs(datetime.fromisoformat(snap['future_targets'][0]['due_time']))
            db = SessionLocal()
            try:
                parsed = json.loads(db.query(AIAnalysisRunModel).filter_by(run_id=run_id).one().parsed_response_json)
                self.assertEqual(set(parsed['future_metrics']),{'3'})
                self.assertNotIn('settlement_metrics',parsed)
            finally: db.close()
            settlement.settle_due_runs(datetime.fromisoformat(snap['future_targets'][1]['due_time']))
            db = SessionLocal()
            try:
                parsed = json.loads(db.query(AIAnalysisRunModel).filter_by(run_id=run_id).one().parsed_response_json)
                self.assertEqual(set(parsed['future_metrics']), {'3', '5'})
                self.assertNotIn('settlement_metrics', parsed)
            finally: db.close()
            settlement.settle_due_runs(datetime.fromisoformat(snap['future_targets'][2]['due_time']))
            db = SessionLocal()
            try:
                parsed = json.loads(db.query(AIAnalysisRunModel).filter_by(run_id=run_id).one().parsed_response_json)
                self.assertEqual(set(parsed['future_metrics']), {'3', '5', '7'})
                self.assertNotIn('settlement_metrics', parsed)
            finally: db.close()
            settlement.settle_due_runs(datetime.fromisoformat(snap['future_targets'][-1]['due_time']))
        db = SessionLocal()
        try:
            parsed = json.loads(db.query(AIAnalysisRunModel).filter_by(run_id=run_id).one().parsed_response_json)
            self.assertEqual(parsed['future_metrics']['5']['rating'],'UNVERIFIABLE')
            self.assertEqual(set(parsed['future_metrics']), {'3', '5', '7', '15'})
            self.assertIn('settlement_metrics',parsed)
            evaluation = db.query(AIAnalysisEvaluationModel).filter_by(run_id=run_id).one()
            self.assertEqual(evaluation.comment,'人工评价保留')
        finally: db.close()
        self.login('bob')
        self.assertEqual(ai_tests.client.get(f'/api/v1/ai/analysis/batches/{batch_id}').status_code,404)


if __name__=='__main__': unittest.main()
