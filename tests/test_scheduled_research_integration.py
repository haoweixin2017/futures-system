import copy
import json
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import test_bootstrap
except ImportError:
    from tests import test_bootstrap

from backend.ai_analysis import future_trend, orchestrator, provider, snapshot
from backend.api import ai_routes
from tests import test_ai_analysis as ai_tests
from tests.test_future_trend_integration import bar
from tests import test_future_trend as future_tests


class ScheduledResearchAPITests(unittest.TestCase):
    setUp = ai_tests.AIAnalysisTests.setUp
    login = ai_tests.AIAnalysisTests.login
    create_model = ai_tests.AIAnalysisTests.create_model
    model_body = staticmethod(ai_tests.AIAnalysisTests.model_body)
    seed_market = ai_tests.AIAnalysisTests.seed_market

    def test_manual_materials_endpoint_authenticates_and_validates(self):
        from backend.ai_analysis import research_cache
        with patch.object(research_cache, 'persist_manual_materials',
                          return_value={'inserted': 1, 'duplicates': 0, 'details': []}) as persist:
            response = ai_tests.client.post('/api/v1/ai/research/materials/manual', json={
                'records': [{'target': 'FU2611', 'dimension': 'fundamentals',
                             'title': '标题', 'body': '正文'}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['inserted'], 1)
        self.assertEqual(persist.call_args.args[0], 'alice')
        missing = ai_tests.client.post('/api/v1/ai/research/materials/manual', json={
            'records': [{'target': 'FU2611', 'dimension': 'fundamentals', 'title': '缺正文'}]})
        self.assertEqual(missing.status_code, 422)

    def test_material_export_endpoint_reaches_export_route(self):
        from backend.ai_analysis import research_cache
        with patch.object(research_cache, 'export_materials',
                          return_value={'total': 0, 'exported': 0, 'limit': 500, 'records': []}) as export:
            response = ai_tests.client.get('/api/v1/ai/research/materials/export?instrument=FU2611&dimension=fundamentals')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(export.call_args.kwargs['instrument'], 'FU2611')
        self.assertEqual(export.call_args.kwargs['dimension'], 'fundamentals')
        self.assertEqual(export.call_args.args[0], 'alice')

    def test_research_endpoints_use_authenticated_owner_and_map_failures(self):
        cache = SimpleNamespace(
            get_settings=lambda owner: {'owner': owner, 'config_version': 0},
            save_settings=lambda owner, data: {'owner': owner, **data},
            get_status=lambda owner, instrument=None: {'owner': owner, 'instrument': instrument},
            get_material=lambda owner, material_id: {'owner': owner, 'material_id': material_id})
        scheduler = SimpleNamespace(enqueue_refresh=lambda owner, instrument=None:
                                    {'jobs': [{'owner': owner, 'instrument': instrument}]})
        with patch.object(ai_routes, 'research_cache', cache, create=True), \
             patch.object(ai_routes, 'research_scheduler', scheduler, create=True):
            self.assertEqual(ai_tests.client.get('/api/v1/ai/research/settings').json(),
                             {'owner': 'alice', 'config_version': 0})
            response = ai_tests.client.put('/api/v1/ai/research/settings', json={
                'enabled': False, 'model_config_id': None, 'interval_minutes': 60,
                'contracts': [{'instrument': 'FU2611', 'related': []}], 'config_version': 0})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['owner'], 'alice')
            self.assertEqual(ai_tests.client.post('/api/v1/ai/research/refresh',
                json={'instrument': 'FU2611'}).json()['jobs'][0]['owner'], 'alice')
            self.assertEqual(ai_tests.client.get('/api/v1/ai/research/status?instrument=FU2611').json(),
                             {'owner': 'alice', 'instrument': 'FU2611'})
            self.assertEqual(ai_tests.client.get('/api/v1/ai/research/materials/M1').json(),
                             {'owner': 'alice', 'material_id': 'M1'})
            for exc, status in [(ValueError('bad input'), 422), (LookupError('not owned'), 404),
                                (RuntimeError('configuration conflict'), 409)]:
                cache.get_material = Mock(side_effect=exc)
                self.assertEqual(ai_tests.client.get('/api/v1/ai/research/materials/M1').status_code, status)
            self.login('bob')
            self.assertEqual(ai_tests.client.get('/api/v1/ai/research/settings').json()['owner'], 'bob')
            ai_tests.client.cookies.clear()
            self.assertEqual(ai_tests.client.get('/api/v1/ai/research/settings').status_code, 401)

    def test_research_settings_reject_bad_interval_and_negative_version(self):
        for interval, version in [(20, 0), (60, -1)]:
            response = ai_tests.client.put('/api/v1/ai/research/settings', json={
                'enabled': False, 'model_config_id': None, 'interval_minutes': interval,
                'contracts': [], 'config_version': version})
            self.assertEqual(response.status_code, 422, response.text)

    def test_research_settings_cannot_enable_removed_reference_feature(self):
        response = ai_tests.client.put('/api/v1/ai/research/settings', json={
            'contracts': [{'instrument': 'FU2611', 'related': ['SC2611']}],
            'config_version': 0})
        self.assertEqual(response.status_code, 422, response.text)

    def test_future_prediction_freezes_cache_without_reference_market(self):
        model = self.create_model()
        self.seed_market()
        snap, digest = snapshot.build_snapshot('RB2610')
        snap['analysis_target'] = 'FUTURE_TREND'
        snap['future_targets'] = [{'horizon_days': 3, 'trading_day': '20260921'},
                                  {'horizon_days': 5, 'trading_day': '20260923'}]
        frozen = {'sources': [{'source_id': 'S1', 'material_id': 'M1', 'excerpt': '库存下降'}],
                  'background_sources': [], 'missing_dimensions': [], 'config_version': 7,
                  'related_contracts': [], 'cache_state': 'AVAILABLE',
                  'research': {'status': 'COMPLETED'}}
        seen = []
        def freeze(owner, instrument, cutoff, targets=None):
            seen.append((owner, instrument, cutoff, targets))
            return copy.deepcopy(frozen)
        related = [{'instrument': {'symbol': 'SC2611'}, 'market': {'bars_60m': []},
                    'quality': {'status': 'MISSING'}}]
        cache = SimpleNamespace(freeze_materials=freeze)
        with patch.object(ai_routes, 'research_cache', cache, create=True), \
             patch.object(snapshot, 'build_snapshot', return_value=(snap, digest)), \
             patch.object(snapshot, 'build_related_market_snapshots', return_value=related, create=True) as build_related, \
             patch.object(orchestrator, 'submit_batch'):
            response = ai_tests.client.post('/api/v1/ai/analysis/RB2610', json={
                'client_request_id': 'f0697386-1c56-4f6a-a3d7-cdbf76874880',
                'analysis_target': 'FUTURE_TREND', 'use_review_summaries': False,
                'model_config_ids': [model['model_config_id']]})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['public_materials'], frozen)
        self.assertEqual(response.json()['related_contracts'], [])
        self.assertEqual(response.json()['related_market_snapshots'], [])
        build_related.assert_not_called()
        self.assertEqual(seen, [('alice', 'RB2610', snap['market']['snapshot_time'], snap['future_targets'])])
        frozen['sources'][0]['excerpt'] = '后续更新'
        detail = ai_tests.client.get('/api/v1/ai/analysis/batches/' + response.json()['batch_id']).json()
        self.assertEqual(detail['public_materials']['sources'][0]['excerpt'], '库存下降')
        self.assertNotEqual(detail['snapshot_hash'], digest)

    def test_pending_legacy_research_status_never_triggers_prediction_search(self):
        model = self.create_model()
        self.seed_market()
        snap, digest = snapshot.build_snapshot('RB2610')
        snap.update(analysis_target='FUTURE_TREND', public_materials={'research': {'status': 'PENDING'}})
        cache = SimpleNamespace(freeze_materials=lambda *args, **kwargs: snap['public_materials'])
        with patch.object(ai_routes, 'research_cache', cache, create=True), \
             patch.object(snapshot, 'build_snapshot', return_value=(snap, digest)), \
             patch.object(orchestrator, 'submit_batch'):
            response = ai_tests.client.post('/api/v1/ai/analysis/RB2610', json={
                'client_request_id': '3f3cdde5-48bc-4435-ab73-30db67f90ff0',
                'analysis_target': 'FUTURE_TREND', 'use_review_summaries': False,
                'model_config_ids': [model['model_config_id']]})
        with patch.object(orchestrator.executor, 'submit') as submit:
            orchestrator.submit_batch(response.json()['batch_id'])
        self.assertEqual(submit.call_count, 1)
        self.assertIs(submit.call_args.args[0], orchestrator.run_analysis)

    def test_material_query_endpoint_passes_owner_and_filters(self):
        captured = {}
        def list_materials(owner, **kwargs):
            captured['owner'] = owner
            captured.update(kwargs)
            own = owner == 'alice'
            return {'items': [{'material_id': 'M1', 'title': '资料'}] if own else [],
                    'total': 1 if own else 0,
                    'page': kwargs.get('page', 1), 'page_size': kwargs.get('page_size', 20)}
        cache = SimpleNamespace(list_materials=list_materials,
            get_settings=lambda owner: {'owner': owner, 'config_version': 0},
            save_settings=lambda owner, data: {'owner': owner, **data},
            get_status=lambda owner, instrument=None: {'owner': owner},
            get_material=lambda owner, material_id: {'owner': owner, 'material_id': material_id})
        with patch.object(ai_routes, 'research_cache', cache, create=True):
            response = ai_tests.client.get('/api/v1/ai/research/materials')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['total'], 1)
            self.assertEqual(captured['owner'], 'alice')
            filtered = ai_tests.client.get(
                '/api/v1/ai/research/materials?topic=product:FU&dimension=macro'
                '&method=FIXED_SOURCE&series=NBS:PMI&q=%E5%BA%93%E5%AD%98&page=2&page_size=50')
            self.assertEqual(filtered.status_code, 200, filtered.text)
            self.assertEqual((captured['topic'], captured['dimension'], captured['method'],
                              captured['series'], captured['q'], captured['page'], captured['page_size']),
                             ('product:FU', 'macro', 'FIXED_SOURCE', 'NBS:PMI', '库存', 2, 50))
            bad = ai_tests.client.get('/api/v1/ai/research/materials?page=0')
            self.assertEqual(bad.status_code, 422, bad.text)
        self.login('bob')
        self.assertEqual(ai_tests.client.get('/api/v1/ai/research/materials').json()['total'], 0,
                         '跨用户不得看到他人资料')
        ai_tests.client.cookies.clear()
        self.assertEqual(ai_tests.client.get('/api/v1/ai/research/materials').status_code, 401)

    def test_future_prediction_never_triggers_fixed_collection_or_web_access(self):
        model = self.create_model()
        self.seed_market()
        snap, digest = snapshot.build_snapshot('RB2610')
        snap['analysis_target'] = 'FUTURE_TREND'
        snap['future_targets'] = [{'horizon_days': 3, 'trading_day': '20260921'},
                                  {'horizon_days': 5, 'trading_day': '20260923'}]
        frozen = {'sources': [], 'background_sources': [], 'missing_dimensions': ['fundamentals'],
                  'config_version': 1, 'related_contracts': [], 'cache_state': 'EMPTY',
                  'research': {'status': 'CACHED'}}
        cache = SimpleNamespace(freeze_materials=lambda *args, **kwargs: frozen)
        with patch.object(ai_routes, 'research_cache', cache, create=True), \
             patch.object(snapshot, 'build_snapshot', return_value=(snap, digest)), \
             patch.object(orchestrator, 'submit_batch'), \
             patch('backend.ai_analysis.fixed_research.collect_fixed') as collect_fixed, \
             patch('backend.ai_analysis.fixed_research.collect_rule_search') as collect_rule, \
             patch('backend.ai_analysis.web_research.search_public_web') as search_web, \
             patch('backend.ai_analysis.web_research.read_public_page') as read_page, \
             patch('backend.ai_analysis.web_research.latest_official_macro') as macro:
            response = ai_tests.client.post('/api/v1/ai/analysis/RB2610', json={
                'client_request_id': 'a5b1c9de-6f7a-4c1e-9d2b-3e4f5a6b7c8d',
                'analysis_target': 'FUTURE_TREND', 'use_review_summaries': False,
                'model_config_ids': [model['model_config_id']]})
        self.assertEqual(response.status_code, 202, response.text)
        collect_fixed.assert_not_called()
        collect_rule.assert_not_called()
        search_web.assert_not_called()
        read_page.assert_not_called()
        macro.assert_not_called()

    def test_retained_majority_recalculates_usage_after_merging_valid_evidence(self):
        model = self.create_model()
        self.seed_market()
        snap, digest = snapshot.build_snapshot('RB2610')
        snap['analysis_target'] = 'FUTURE_TREND'
        snap['quality']['can_judge'] = True
        snap['future_targets'] = future_trend.targets('RB2610', datetime(2026, 9, 16, 10, 40), '20260916', 100)
        snap['contract_lifecycle'] = {'status':'RULE_BASED', 'targets':[
            {**t, 'status':'WITHIN_RULE_TERM'} for t in snap['future_targets']]}
        snap['public_materials'] = {'sources': [], 'missing_dimensions': list(future_trend.EXTERNAL_DIMENSIONS)}
        with patch.object(snapshot, 'build_snapshot', return_value=(snap, digest)), \
             patch.object(ai_routes.research_cache, 'freeze_materials', return_value=snap['public_materials']), \
             patch.object(orchestrator, 'submit_batch'):
            created = ai_tests.client.post('/api/v1/ai/analysis/RB2610', json={
                'client_request_id': '7eaaab86-3255-4ac8-a505-4516d8ed3eca',
                'analysis_target': 'FUTURE_TREND', 'use_review_summaries': False,
                'self_consistency_votes': 3, 'model_config_ids': [model['model_config_id']]})
        self.assertEqual(created.status_code, 202, created.text)
        result = {**ai_tests.required_fields(), 'instrument': 'RB2610', 'trading_day': '20260916',
            'snapshot_time': snap['market']['snapshot_time'], 'direction': 'LONG', 'confidence_score': 70,
            'evidence': ai_tests.evidence_for(snap), 'alternative_scenario': '跌破支撑转空',
            'horizon_forecasts': [{**target, 'direction': 'LONG', 'confidence_score': 70,
                'summary': '等待确认', 'entry_condition': '确认条件', 'invalidation_condition': '失效条件'}
                for target in snap['future_targets']],
            'dimensions': {d: {'status': 'MISSING' if d in future_trend.EXTERNAL_DIMENSIONS else 'AVAILABLE',
                'summary': '行情依据或资料不足', 'citations': []} for d in future_trend.DIMENSIONS}}
        volume_result = copy.deepcopy(result)
        row = snap['market']['bars_1m'][0]
        volume_result['evidence'] += [{'side': 'CONTEXT', 'time': row['bar_time'],
            'field': 'volume', 'value': float(row['volume']), 'reason': '量价参考'}]
        replies = [provider.ProviderResult(200, json.dumps(value), json.dumps(value), 'vote-test', 50, 10, 1)
                   for value in (result, volume_result, result)] + [ValueError('review unavailable')]
        with patch.object(provider, 'chat_completion', side_effect=replies):
            orchestrator.run_analysis(created.json()['runs'][0]['run_id'])
        detail = ai_tests.client.get('/api/v1/ai/analysis/batches/' + created.json()['batch_id']).json()
        run = detail['runs'][0]
        self.assertEqual(run['status'], 'SUCCEEDED', run.get('error'))
        self.assertEqual(run['result']['review_status'], 'FAILED_DRAFT_RETAINED')
        self.assertEqual(run['result']['used_dimensions'], ['technical', 'volume_oi'])


class FrozenRelatedMarketTests(unittest.TestCase):
    def test_related_bars_are_closed_before_cutoff_and_use_extra_oi_baseline(self):
        cutoff = datetime(2026, 9, 30, 15)
        rows = []
        for index in range(64):
            item = bar('20260930', (cutoff - timedelta(hours=62-index)).isoformat(), '60m')
            item.instrument = 'SC2611'
            item.open_interest = Decimal(1000 + index * 2)
            rows.append(item)
        redis = Mock()
        redis.hgetall.return_value = {'timestamp': cutoff.isoformat(), 'trading_day': '20260930', 'last_price': '500'}
        with patch.object(snapshot.RedisClient, 'get_client', return_value=redis), \
             patch.object(snapshot, '_load_bars', return_value=rows), \
             patch.object(snapshot, '_instrument_meta', return_value=('原油', 1000, Decimal('0.1'))), \
             patch.object(snapshot.FuturesTradingCalendar, 'session_end', side_effect=lambda dt, _: dt + timedelta(hours=1)), \
             patch.object(snapshot.FuturesTradingCalendar, 'is_trading_day', return_value=True):
            frozen = snapshot.build_related_market_snapshots(['SC2611'], cutoff.isoformat())[0]
        bars = frozen['market']['bars_60m']
        self.assertEqual(len(bars), 60)
        self.assertEqual(bars[0]['open_interest_change_raw'], '2')
        self.assertIsNone(bars[0]['open_interest_change'])
        self.assertEqual(frozen['quality']['availability'], 'TERM_UNKNOWN')
        self.assertEqual(bars[-1]['bar_time'], '2026-09-30T14:00:00')
        self.assertEqual(frozen['instrument']['symbol'], 'SC2611')

    def test_missing_or_post_cutoff_quote_is_explicit_and_missing_prior_oi_is_null(self):
        row = bar('20260930', '2026-09-30T13:00:00', '60m')
        for quote, warning in [({}, 'QUOTE_MISSING'),
                               ({'timestamp': '2026-09-30T15:00:01', 'last_price': '500'}, 'QUOTE_AFTER_CUTOFF')]:
            redis = Mock(); redis.hgetall.return_value = quote
            with patch.object(snapshot.RedisClient, 'get_client', return_value=redis), \
                 patch.object(snapshot, '_load_bars', return_value=[row]), \
                 patch.object(snapshot, '_instrument_meta', return_value=('原油', 1000, Decimal('0.1'))):
                frozen = snapshot.build_related_market_snapshots(['SC2611'], '2026-09-30T15:00:00')[0]
            self.assertIsNone(frozen['market']['quote'])
            self.assertIn(warning, frozen['quality']['warnings'])
            self.assertIsNone(frozen['market']['bars_60m'][0]['open_interest_change'])

    def test_model_request_omits_full_bodies_background_and_research_logs(self):
        saved = {'analysis_target': 'FUTURE_TREND', 'public_materials': {
            'sources': [{'source_id': 'S1', 'excerpt': 'x'*2500, 'body': 'SECRET_FULL_BODY',
                         'full_text': 'SECRET_FULL_TEXT', 'content': 'SECRET_CONTENT'}],
            'background_sources': [{'excerpt': 'SECRET_BACKGROUND'}],
            'research': {'status': 'COMPLETED', 'events': ['SECRET_EVENT'],
                         'tool_records': ['SECRET_TOOL'], 'raw_response_text': 'SECRET_LOG',
                         'jobs': [{'calls': [{'request_json': 'SECRET_JOB_LOG'}]}]}}}
        model = orchestrator._model_snapshot(json.dumps(saved))
        self.assertNotIn('SECRET_', json.dumps(model))
        self.assertEqual(len(model['public_materials']['sources'][0]['excerpt']), 2000)
        self.assertEqual(saved['public_materials']['sources'][0]['body'], 'SECRET_FULL_BODY')

    def test_used_related_contracts_are_derived_only_from_valid_reference_citations(self):
        result, frozen = future_tests.FutureTrendTests()._result()
        frozen.update(instrument={'symbol': 'FU2611'}, related_contracts=['SC2611', 'BU2611'])
        frozen['public_materials'] = {'sources': [
            {'source_id': 'S1', 'dimension': 'related_markets', 'excerpt': '原油库存下降',
             'reference_contracts': ['SC2611']}],
            'missing_dimensions': ['fundamentals', 'macro', 'events']}
        result['dimensions']['related_markets'].update(status='AVAILABLE', citations=[
            {'source_id': 'S1', 'quote': '原油库存下降'}])
        result['used_related_contracts'] = ['BU2611']
        checked = future_trend.validate_result(result, frozen)
        self.assertEqual(checked['used_related_contracts'], ['SC2611'])
        self.assertEqual(checked['used_dimensions'], ['related_markets'])

    def test_related_numeric_evidence_must_match_its_own_frozen_contract(self):
        result, frozen = future_tests.FutureTrendTests()._result()
        frozen.update(instrument={'symbol': 'FU2611'}, related_contracts=['SC2611'],
            related_market_snapshots=[{'instrument': {'symbol': 'SC2611'},
                'market': {'quote': {'timestamp': '2026-09-30T15:00:00', 'last_price': '500'}, 'bars_60m': []}}])
        result['dimensions']['related_markets']['market_evidence'] = [{
            'instrument': 'SC2611', 'time': '2026-09-30T15:00:00', 'field': 'last_price',
            'value': 501, 'reason': '原油参考'}]
        with self.assertRaisesRegex(ValueError, '关联'):
            future_trend.validate_result(result, frozen)

    def test_valid_related_numeric_reference_counts_without_public_source(self):
        result, frozen = future_tests.FutureTrendTests()._result()
        frozen.update(instrument={'symbol': 'FU2611'}, related_contracts=['SC2611', 'BU2611'],
            related_market_snapshots=[{'instrument': {'symbol': 'SC2611'},
                'market': {'quote': {'timestamp': '2026-09-30T15:00:00', 'last_price': '500'}, 'bars_60m': []}}])
        result['dimensions']['related_markets'].update(status='PARTIAL', market_evidence=[{
            'instrument': 'SC2611', 'time': '2026-09-30T15:00:00', 'field': 'last_price',
            'value': 500, 'reason': '原油仅作参考，不能假设正相关'}])
        checked = future_trend.validate_result(result, frozen)
        self.assertEqual(checked['used_dimensions'], ['related_markets'])
        self.assertEqual(checked['used_related_contracts'], ['SC2611'])

    def test_related_public_source_cannot_be_target_fundamentals(self):
        result, frozen = future_tests.FutureTrendTests()._result()
        frozen['public_materials'] = {'sources': [
            {'source_id': 'S1', 'dimension': 'fundamentals', 'excerpt': '原油库存下降',
             'reference_contracts': ['SC2611']}], 'missing_dimensions': ['macro', 'events', 'related_markets']}
        for status in ('AVAILABLE', 'MISSING'):
            result['dimensions']['fundamentals'].update(status=status, citations=[
                {'source_id': 'S1', 'quote': '原油库存下降'}])
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, '关联'):
                future_trend.validate_result(result, frozen)

    def test_known_expired_related_contract_is_not_read_from_market_cache(self):
        with patch.object(snapshot, '_load_bars') as load, \
             patch.object(snapshot.RedisClient, 'get_client', return_value=Mock()) as redis, \
             patch.object(snapshot, '_instrument_meta', return_value=('原油', 1000, Decimal('0.1'))):
            frozen = snapshot.build_related_market_snapshots(['SC2609'], '2026-10-02T15:00:00',
                {'SC2609': {'status': 'RULE_BASED', 'expired': True}})[0]
        self.assertEqual(frozen['quality']['status'], 'MISSING')
        self.assertIn('EXPIRED_CONTRACT', frozen['quality']['warnings'])
        self.assertIsNone(frozen['market']['quote'])
        load.assert_not_called()
        redis.return_value.hgetall.assert_not_called()

    def test_cache_contract_status_list_preserves_expiry_in_related_snapshot(self):
        with patch.object(snapshot, '_load_bars') as load, \
             patch.object(snapshot.RedisClient, 'get_client', return_value=Mock()), \
             patch.object(snapshot, '_instrument_meta', return_value=('燃料油', 1000, Decimal('1'))):
            frozen = snapshot.build_related_market_snapshots(['FU2610'], '2026-10-02T15:00:00',
                [{'instrument': 'FU2610', 'status': 'RULE_BASED', 'expired': True}])[0]
        self.assertIn('EXPIRED_CONTRACT', frozen['quality']['warnings'])
        self.assertTrue(frozen['contract_lifecycle']['expired'])
        load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
