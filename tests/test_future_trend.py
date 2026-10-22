import copy
import json
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

try:
    import test_bootstrap
except ImportError:
    from tests import test_bootstrap

from backend.ai_analysis import future_trend


class FutureTrendTests(unittest.TestCase):
    def test_special_notice_overrides_exact_metadata_and_bad_notice_blocks(self):
        from pathlib import Path
        config = json.loads((Path(future_trend.__file__).resolve().parents[2] /
            'config/future_contract_rules.json').read_text(encoding='utf-8'))
        notice = {'instrument':'LH2611', 'source_url':'https://www.dce.com.cn/fixture',
            'source_title':'离线专项公告', 'published_on':'2026-10-05',
            'verified_on':'2026-10-06', 'last_trading_day':'2026-11-24'}
        targets = [{'horizon_days':3, 'trading_day':'20261124'},
                   {'horizon_days':5, 'trading_day':'20261125'}]
        config['special_notices'] = [notice]
        with patch.object(Path, 'read_text', return_value=json.dumps(config)):
            result = future_trend.contract_lifecycle('LH2611', targets, datetime(2026,10,6,14))
        self.assertEqual(result['last_trading_day'], '20261124')
        self.assertEqual(result['special_notice_status'], 'VERIFIED_OVERRIDE')
        self.assertEqual([r['status'] for r in result['targets']], ['WITHIN_RULE_TERM','AFTER_RULE_TERM'])
        for notices in ([dict(notice, source_url='')], [notice, notice],
                        [dict(notice, verified_on='2026-10-07')]):
            config['special_notices'] = notices
            with patch.object(Path, 'read_text', return_value=json.dumps(config)):
                self.assertEqual(future_trend.contract_lifecycle('LH2611', targets,
                    datetime(2026,10,6,14))['status'], 'UNKNOWN')

    def test_dce_official_contract_dates_are_verified_without_generic_rule_guessing(self):
        frozen = [{'horizon_days':3, 'trading_day':'20261012'},
                  {'horizon_days':5, 'trading_day':'20261014'}]
        for symbol, last in [('LH2611','20261125'), ('JD2612','20261228')]:
            with self.subTest(symbol=symbol):
                term = future_trend.contract_lifecycle(symbol, frozen, datetime(2026,10,6,14))
                self.assertEqual(term['status'], 'VERIFIED')
                self.assertEqual(term['last_trading_day'], last)
                self.assertEqual(term['term_basis'], 'OFFICIAL_CONTRACT_METADATA')
                self.assertEqual([r['status'] for r in term['targets']], ['WITHIN_RULE_TERM']*2)
                self.assertEqual(future_trend.contract_lifecycle(symbol, frozen,
                    datetime(2026,10,6,12))['status'], 'UNKNOWN')
        self.assertEqual(future_trend.contract_lifecycle('LH2612', frozen,
            datetime(2026,10,6,14))['status'], 'UNKNOWN')

    def test_contract_metadata_evidence_tamper_or_conflict_stays_unknown(self):
        from pathlib import Path
        config = json.loads((Path(future_trend.__file__).resolve().parents[2] /
            'config/future_contract_rules.json').read_text(encoding='utf-8'))
        record = next(r for r in config.get('verified_contracts', []) if r['instrument'] == 'LH2611')
        for changes in ({'sha256':'bad'}, {'last_trading_day':'20261124'},
                        {'verified_at':'2026-10-07T00:00:00'}):
            edited = copy.deepcopy(config)
            edited['verified_contracts'] = [dict(record, **changes)]
            with patch.object(Path, 'read_text', return_value=json.dumps(edited)):
                self.assertEqual(future_trend.contract_lifecycle('LH2611', [],
                    datetime(2026,10,6,14))['status'], 'UNKNOWN')

    def test_exact_term_boundary_and_calendar_year_remain_guarded(self):
        targets = [{'horizon_days':3, 'trading_day':'20261125'},
                   {'horizon_days':5, 'trading_day':'20261126'}]
        result = future_trend.contract_lifecycle('LH2611', targets, datetime(2026,10,6,14))
        self.assertEqual([r['status'] for r in result['targets']], ['WITHIN_RULE_TERM','AFTER_RULE_TERM'])
        self.assertEqual(future_trend.contract_lifecycle('LH2611',
            [dict(targets[0], trading_day='20270104')], datetime(2026,10,6,14))['status'], 'UNKNOWN')

    def test_research_contract_expiry_consumes_official_exact_date(self):
        from backend.ai_analysis.research_cache import contract_status
        self.assertFalse(contract_status('JD2612', datetime(2026,12,28))['expired'])
        expired = contract_status('JD2612', datetime(2026,12,29))
        self.assertTrue(expired['expired'])
        self.assertEqual(expired['warning'], '已到期，请更换具体合约')

    def test_verified_special_notice_overrides_standard_term_and_invalid_notice_blocks(self):
        from pathlib import Path
        config = json.loads((Path(future_trend.__file__).resolve().parents[2] /
            'config/future_contract_rules.json').read_text(encoding='utf-8'))
        notice = {'instrument':'IH2610', 'source_url':'https://www.cffex.com.cn/fixture',
            'source_title':'离线测试公告', 'published_on':'2026-10-05',
            'verified_on':'2026-10-06', 'last_trading_day':'2026-10-14'}
        targets = [{'horizon_days':3, 'trading_day':'20261014'},
                   {'horizon_days':5, 'trading_day':'20261015'}]
        config['special_notices'] = [notice]
        with patch.object(Path, 'read_text', return_value=json.dumps(config)):
            term = future_trend.contract_lifecycle('IH2610', targets, datetime(2026,10,6))
        self.assertEqual(term['special_notice_status'], 'VERIFIED_OVERRIDE')
        self.assertEqual([t['status'] for t in term['targets']], ['WITHIN_RULE_TERM','AFTER_RULE_TERM'])
        for invalid in (dict(notice, source_url=''), dict(notice, verified_on='2026-10-07'),
                        dict(notice, last_trading_day='2026-10-11')):
            config['special_notices'] = [invalid]
            with patch.object(Path, 'read_text', return_value=json.dumps(config)):
                self.assertEqual(future_trend.contract_lifecycle('IH2610', targets,
                    datetime(2026,10,6))['status'], 'UNKNOWN')

    def test_verified_official_terms_cover_bu_sc_and_ih(self):
        frozen = [{'horizon_days':3, 'trading_day':'20261012'},
                  {'horizon_days':5, 'trading_day':'20261014'}]
        for symbol, last in [('BU2611','20261116'), ('SC2611','20261030'), ('IH2610','20261016')]:
            with self.subTest(symbol=symbol):
                term = future_trend.contract_lifecycle(symbol, frozen, datetime(2026,10,6))
                self.assertEqual(term['last_trading_day_by_rule'], last)
                self.assertEqual([t['status'] for t in term['targets']], ['WITHIN_RULE_TERM']*2)
                self.assertEqual(term['special_notice_status'], 'NOT_CHECKED')

    def test_term_boundary_and_unknown_calendar_year(self):
        frozen = [{'horizon_days':3, 'trading_day':'20261016'},
                  {'horizon_days':5, 'trading_day':'20261019'}]
        term = future_trend.contract_lifecycle('IH2610', frozen, datetime(2026,10,6))
        self.assertEqual([t['status'] for t in term['targets']], ['WITHIN_RULE_TERM','AFTER_RULE_TERM'])
        self.assertEqual(future_trend.contract_lifecycle('BU2701', frozen, datetime(2026,10,6))['status'], 'UNKNOWN')

    def test_fu_contract_term_is_rule_based_and_does_not_guess_other_products(self):
        frozen = [{'horizon_days':3, 'trading_day':'20261012'},
                  {'horizon_days':5, 'trading_day':'20261014'},
                  {'horizon_days':15, 'trading_day':'20261102'}]
        term = future_trend.contract_lifecycle('FU2611', frozen, datetime(2026,10,2,19))
        self.assertEqual(term['delivery_month'], '2026-11')
        self.assertEqual(term['last_trading_day_by_rule'], '20261030')
        self.assertEqual([row['status'] for row in term['targets']],
                         ['WITHIN_RULE_TERM', 'WITHIN_RULE_TERM', 'AFTER_RULE_TERM'])
        self.assertIn('shfe.com.cn', term['source_url'])
        self.assertIn('调整', term['limitations'])
        for symbol, now in [('RB2611', datetime(2026,10,2)),
                            ('FU2613', datetime(2026,10,2)),
                            ('FU2702', datetime(2026,10,2)),
                            ('FU2611', datetime(2026,9,30))]:
            self.assertEqual(future_trend.contract_lifecycle(symbol, frozen, now)['status'], 'UNKNOWN')

    def test_material_freshness_windows_and_unknown_dates(self):
        rows = []
        for i, (dimension, date) in enumerate([
                ('fundamentals', '2026-09-17'), ('fundamentals', '2026-09-16'),
                ('events', '2026-09-24'), ('events', '2026-09-23'),
                ('related_markets', '2026-09-24'), ('related_markets', '2026-09-23'),
                ('fundamentals', 'UNKNOWN'), ('macro', '2026-09-30')]):
            rows.append({'url': f'https://futures.eastmoney.com/a/{i}.html',
                         'title': str(i), 'excerpt': '库存下降' + str(i),
                         'dimension': dimension, 'published_at': date,
                         'fetched_at': '2026-10-01T12:00:00+08:00'})
        sources = future_trend.normalize_materials(rows, datetime(2026, 10, 1, 12))
        self.assertEqual([s['stale'] for s in sources],
                         [False, True, False, True, False, True, True, False])

    def test_old_only_materials_are_background_and_dimension_missing(self):
        old = {'url': 'https://futures.eastmoney.com/a/old.html', 'title': '旧库存',
               'excerpt': '库存下降', 'dimension': 'fundamentals', 'published_at': '2026-04-16'}
        with patch.object(future_trend, '_search', return_value=[old]), \
             patch.object(future_trend, '_official_macro', return_value=[]):
            materials = future_trend.collect_materials('燃料油', datetime(2026, 10, 1))
        self.assertEqual(materials['sources'], [])
        self.assertEqual(len(materials['background_sources']), 1)
        self.assertEqual(materials['status'], 'MISSING')
        self.assertIn('fundamentals', materials['missing_dimensions'])
        with self.assertRaises(ValueError):
            future_trend.validate_citations([{'source_id': 'S1', 'quote': '库存下降'}],
                                            materials['background_sources'])

    def test_latest_official_macro_release_is_not_subject_to_news_window(self):
        row = {'url': 'https://www.stats.gov.cn/sj/zxfb/example.html', 'title': '最新月报',
               'excerpt': '官方月度发布', 'dimension': 'macro', 'source_kind': 'OFFICIAL',
               'latest_release': True, 'published_at': '2026-08-30'}
        self.assertFalse(future_trend.normalize_materials([row], datetime(2026, 10, 1))[0]['stale'])
        row['published_at'] = 'UNKNOWN'
        self.assertTrue(future_trend.normalize_materials([row], datetime(2026, 10, 1))[0]['stale'])

    def test_search_retries_old_results_and_sorts_newest_first(self):
        import json
        def response(entries):
            return 'jQuery(' + json.dumps({'result': {'cmsArticleWebOld': entries}}) + ')'
        def entry(date):
            return {'title': '燃料油库存' + date, 'date': date, 'content': '库存下降',
                    'url': 'https://futures.eastmoney.com/a/' + date + '.html'}
        with patch.object(future_trend, '_get', side_effect=[
                response([entry('2026-04-16')]),
                response([entry('2026-09-28'), entry('2026-09-30')])]) as get, \
             patch.object(future_trend, '_article', return_value=''):
            sources = future_trend._search('燃料油', now=datetime(2026, 10, 1))
        self.assertEqual(get.call_count, 2)
        self.assertEqual([s['published_at'] for s in sources[:2]], ['2026-09-30', '2026-09-28'])

    def test_two_horizons_skip_holidays_and_have_independent_due_dates(self):
        frozen = future_trend.targets('FU2611', datetime(2026, 10, 1), '20260930', 4000)
        self.assertEqual([t['horizon_days'] for t in frozen], [3, 5])
        self.assertEqual([t['trading_day'] for t in frozen],
                         ['20261012', '20261014'])
        self.assertEqual(len({t['due_time'] for t in frozen}), 2)

    def test_holiday_targets_are_frozen_trading_days(self):
        result = future_trend.targets('FU2611', datetime(2026, 10, 1, 21), '20260930', 4000)
        self.assertEqual([x['trading_day'] for x in result[:2]], ['20261012', '20261014'])
        self.assertEqual(result[0]['bar_time'], '2026-10-12T14:59:00')
        self.assertEqual(result[1]['reference_price'], 4000)

    def test_night_session_counts_after_its_own_trading_day(self):
        result = future_trend.targets('FU2611', datetime(2026, 9, 28, 22), '20260929', 4000)
        self.assertEqual(result[0]['trading_day'], '20261009')

    def test_no_night_instrument_before_open_starts_after_completed_day(self):
        result = future_trend.targets('IF2610', datetime(2026, 9, 29, 8), '20260928', 4000)
        self.assertEqual([row['trading_day'] for row in result[:2]], ['20261008', '20261012'])

    def test_night_instrument_before_day_open_keeps_started_trading_day(self):
        result = future_trend.targets('FU2611', datetime(2026, 9, 29, 8), '20260928', 4000)
        self.assertEqual([row['trading_day'] for row in result[:2]], ['20261009', '20261013'])

    def test_before_open_after_holiday_without_previous_night_uses_completed_day(self):
        result = future_trend.targets('FU2611', datetime(2026, 9, 28, 8), '20260924', 4000)
        self.assertEqual([row['trading_day'] for row in result[:2]], ['20260930', '20261009'])

    def test_holiday_reference_is_previous_actual_trading_day(self):
        result = future_trend.targets('IF2610', datetime(2026, 9, 25, 8), '20260924', 4000)
        self.assertEqual([row['trading_day'] for row in result[:2]], ['20260930', '20261009'])

    def test_day_open_boundary_assigns_current_trading_day(self):
        result = future_trend.targets('IF2610', datetime(2026, 9, 29, 9, 30), '20260928', 4000)
        self.assertEqual(result[0]['trading_day'], '20261009')

    def test_sources_deduplicate_and_exclude_future_publication(self):
        rows = [{'url': 'https://futures.eastmoney.com/a/1.html', 'title': '库存',
                 'published_at': '2026-09-30T12:00:00', 'excerpt': '库存下降', 'dimension': 'fundamentals'},
                {'url': 'https://futures.eastmoney.com/a/2.html', 'title': '库存',
                 'published_at': '2026-09-30T12:00:00', 'excerpt': '库存下降', 'dimension': 'fundamentals'},
                {'url': 'https://futures.eastmoney.com/a/3.html', 'title': '未来报道',
                 'published_at': '2026-10-02T12:00:00', 'excerpt': '未来', 'dimension': 'events'}]
        items = future_trend.normalize_materials(rows, datetime(2026, 10, 1))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['data_period'], 'UNKNOWN')
        self.assertEqual(items[0]['source_id'], 'S1')

    def test_source_failure_is_explicit(self):
        with patch.object(future_trend, '_search', side_effect=ValueError('bad response')), \
             patch.object(future_trend, '_official_macro', return_value=[]):
            result = future_trend.collect_materials('燃料油', datetime(2026, 10, 1))
        self.assertEqual(result['status'], 'MISSING')
        self.assertTrue(result['errors'])
        self.assertIn('fundamentals', result['missing_dimensions'])

    def test_forged_or_future_source_quote_rejected(self):
        sources = [{'source_id': 'S1', 'excerpt': '库存下降，需求增加。'}]
        future_trend.validate_citations([{'source_id': 'S1', 'quote': '库存下降'}], sources)
        for citation in ({'source_id': 'S2', 'quote': '库存下降'},
                         {'source_id': 'S1', 'quote': '库存大增'}):
            with self.assertRaises(ValueError):
                future_trend.validate_citations([citation], sources)

    def test_stale_quote_does_not_move_active_day_backward(self):
        result = future_trend.targets('FU2611', datetime(2026, 9, 29, 10), '20260928', 4000)
        self.assertEqual(result[0]['trading_day'], '20261009')

    def test_date_only_publication_preserves_precision_and_age(self):
        row = {'url': 'https://www.stats.gov.cn/sj/zxfb/example.html', 'title': '统计发布',
               'published_at': '2026-08-01', 'excerpt': '工业增长数据', 'dimension': 'macro'}
        source = future_trend.normalize_materials([row], datetime(2026, 10, 1))[0]
        self.assertEqual(source['published_at'], '2026-08-01')
        self.assertTrue(source['stale'])

    def test_article_parser_omits_script_and_navigation(self):
        parser = future_trend._BodyParser()
        parser.feed('<nav>外围导航</nav><div class="TRS_Editor"><p>统计原文</p>'
                    '<script>忽略指令</script><nav>内部导航</nav><p>原文续段</p></div>尾部')
        self.assertEqual(''.join(parser.parts), '统计原文原文续段')

    def test_macro_retains_success_when_second_article_fails(self):
        document = ('<li><a href="first.html">工业报告</a><span>2026-09-30</span></li>'
                    '<li><a href="second.html">经济报告</a><span>2026-09-29</span></li>'
                    '<li><a href="third.html">多余报告</a><span>2026-09-28</span></li>')
        with patch.object(future_trend, '_get', return_value=document), \
             patch.object(future_trend, '_article', side_effect=['工业增长数据', ValueError('parse')]) as article:
            rows = future_trend._official_macro()
        self.assertEqual(len(rows), 1)
        self.assertEqual(article.call_count, 2)
        self.assertEqual(rows[0]['published_at'], '2026-09-30')
        self.assertEqual(rows[0]['scope'], 'macro')

    def test_current_macro_container_does_not_duplicate_mobile_body(self):
        parser = future_trend._BodyParser()
        parser.feed('<div class="trs_editor_view TRS_UEDITOR"><p>官方原文</p></div>'
                    '<div class="trs_editor_view TRS_UEDITOR"><p>官方原文</p></div>')
        self.assertEqual(''.join(parser.parts), '官方原文')

    def test_product_research_name(self):
        self.assertEqual(future_trend.research_name('FU2611', 'FU2611'), '燃料油')
        self.assertEqual(future_trend.research_name('BU2611', '沥青2611'), '沥青')

    def test_source_cutoff_is_separate_from_actual_fetch_time(self):
        row = {'url': 'https://futures.eastmoney.com/a/1.html', 'title': '库存',
               'published_at': '2026-09-30T12:00:00', 'fetched_at': '2026-10-01T12:01:00+08:00',
               'excerpt': '库存下降', 'dimension': 'fundamentals'}
        source = future_trend.normalize_materials([row], datetime(2026, 10, 1, 12))[0]
        self.assertEqual(source['fetched_at'], row['fetched_at'])
        row['published_at'] = '2026-10-01T12:00:01+08:00'
        self.assertEqual(future_trend.normalize_materials([row], datetime(2026, 10, 1, 12)), [])

    def test_bad_citation_types_are_value_errors(self):
        for citations in ({}, [False], [{'source_id': [], 'quote': '库存下降'}]):
            with self.subTest(citations=citations), self.assertRaises(ValueError):
                future_trend.validate_citations(citations, [])

    def _result(self):
        frozen = future_trend.targets('FU2611', datetime(2026, 10, 1), '20260930', 4000)
        snapshot = {'future_targets': frozen, 'quality': {'can_judge': True},
                    'contract_lifecycle': {'status':'RULE_BASED', 'targets':[
                        {**t, 'status':'WITHIN_RULE_TERM'} for t in frozen]},
                    'public_materials': {'sources': [], 'missing_dimensions': list(future_trend.EXTERNAL_DIMENSIONS)}}
        result = {'direction': 'SHORT', 'confidence_score': 50, 'alternative_scenario': '替代情景', 'horizon_forecasts': [],
                  'evidence': [{'side': side, 'time': '2026-09-30T14:59:00', 'field': field}
                               for side in ('LONG', 'SHORT') for field in ('close_price', 'volume')],
                  'support_levels': [3900], 'resistance_levels': [4100],
                  'dimensions': {key: {'status': 'MISSING', 'summary': '缺少资料', 'citations': []}
                                 for key in future_trend.DIMENSIONS}}
        for target, direction in zip(frozen, ('LONG', 'SHORT', 'LONG', 'NEUTRAL')):
            result['horizon_forecasts'].append({'horizon_days': target['horizon_days'],
                'trading_day': target['trading_day'], 'direction': direction, 'confidence_score': 50,
                'summary': '条件性判断', 'entry_condition': '确认条件', 'invalidation_condition': '失效条件'})
        return result, snapshot

    def test_missing_five_day_forecast_is_rejected(self):
        result, snapshot = self._result()
        result['horizon_forecasts'] = [f for f in result['horizon_forecasts'] if f['horizon_days'] != 5]
        with self.assertRaises(ValueError):
            future_trend.validate_result(result, snapshot)

    def test_main_confidence_is_derived_from_validated_five_day_forecast(self):
        result, snapshot = self._result()
        result['confidence_score'] = 62
        result['horizon_forecasts'][0]['confidence_score'] = 62
        result['horizon_forecasts'][1]['confidence_score'] = 60
        future_trend.validate_result(result, snapshot)
        self.assertEqual(result['confidence_score'],60)
        self.assertEqual(result['horizon_forecasts'][0]['confidence_score'],62)

    def test_disputed_ranges_cannot_produce_precise_levels(self):
        result, snap = self._result()
        snap['quality']['market_checks'] = {'warnings':['CROSS_PERIOD_RANGE_UNVERIFIED']}
        with self.assertRaisesRegex(ValueError, '精确价位'):
            future_trend.validate_result(result, snap)
        result['support_levels'] = []
        result['resistance_levels'] = []
        future_trend.validate_result(result, snap)

    def test_existing_two_horizon_snapshot_remains_valid(self):
        result, snapshot = self._result()
        snapshot['future_targets'] = snapshot['future_targets'][:2]
        result['horizon_forecasts'] = result['horizon_forecasts'][:2]
        future_trend.validate_result(result, snapshot)

    def test_forecast_after_contract_term_requires_no_judgment(self):
        result, snapshot = self._result()
        target = snapshot['future_targets'][0]
        snapshot['contract_lifecycle']['targets'][0]['status'] = 'AFTER_RULE_TERM'
        with self.assertRaisesRegex(ValueError, 'contract term'):
            future_trend.validate_result(result, snapshot)
        result['horizon_forecasts'][0]['direction'] = 'NO_JUDGMENT'
        future_trend.validate_result(result, snapshot)

    def test_unknown_or_missing_term_cannot_give_direction(self):
        for lifecycle in (None, {'status':'UNKNOWN', 'targets':[]},
                          {'status':'RULE_BASED', 'targets':[]}):
            result, snapshot = self._result()
            snapshot['contract_lifecycle'] = lifecycle
            with self.subTest(lifecycle=lifecycle), self.assertRaisesRegex(ValueError, 'contract term'):
                future_trend.validate_result(result, snapshot)

    def test_unknown_fifth_day_does_not_block_verified_third_day(self):
        result, snapshot = self._result()
        snapshot['contract_lifecycle']['targets'][1]['status'] = 'UNKNOWN'
        result['direction'] = result['horizon_forecasts'][1]['direction'] = 'NO_JUDGMENT'
        future_trend.validate_result(result, snapshot)

    def test_unknown_term_cannot_hide_price_prediction(self):
        result, snapshot = self._result()
        snapshot['contract_lifecycle']['targets'][0]['status'] = 'UNKNOWN'
        result['horizon_forecasts'][0].update(direction='NO_JUDGMENT', target_price=4500)
        with self.assertRaisesRegex(ValueError, 'contract term'):
            future_trend.validate_result(result, snapshot)

    def test_dual_direction_result_and_frozen_dates(self):
        result, snapshot = self._result()
        future_trend.validate_result(result, snapshot)
        result['horizon_forecasts'][0]['trading_day'] = '20261013'
        with self.assertRaises(ValueError):
            future_trend.validate_result(result, snapshot)

    def test_missing_dimension_and_invalid_confidence_rejected(self):
        for mutation in ('dimension', 'confidence', 'quality', 'direction', 'missing'):
            result, snapshot = self._result()
            if mutation == 'dimension':
                result['dimensions'].pop('events')
            elif mutation == 'confidence':
                result['horizon_forecasts'][0]['confidence_score'] = float('nan')
            elif mutation == 'quality':
                snapshot['quality']['can_judge'] = False
            elif mutation == 'direction':
                result['direction'] = 'LONG'
            else:
                result['dimensions']['events']['status'] = 'AVAILABLE'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                future_trend.validate_result(result, snapshot)

    def test_one_directional_horizon_still_requires_balanced_evidence(self):
        result, snapshot = self._result()
        result['direction'] = result['horizon_forecasts'][1]['direction'] = 'NO_JUDGMENT'
        result['evidence'] = []
        with self.assertRaises(ValueError):
            future_trend.validate_result(result, snapshot)

    def test_external_available_dimension_requires_exact_quote(self):
        result, snapshot = self._result()
        snapshot['public_materials'] = {'sources': [{'source_id': 'S1', 'dimension': 'fundamentals',
            'excerpt': '库存下降，需求增加。'}], 'missing_dimensions': ['macro', 'events', 'related_markets']}
        dimension = result['dimensions']['fundamentals']
        dimension.update(status='AVAILABLE', citations=[{'source_id': 'S1', 'quote': '库存下降'}])
        future_trend.validate_result(result, snapshot)
        dimension['citations'][0]['quote'] = '库存大增'
        with self.assertRaises(ValueError):
            future_trend.validate_result(result, snapshot)

    def test_all_external_quotes_must_belong_to_the_dimension(self):
        result, snapshot = self._result()
        snapshot['public_materials'] = {'sources': [
            {'source_id': 'S1', 'dimension': 'fundamentals', 'excerpt': '库存下降，需求增加。'},
            {'source_id': 'S2', 'dimension': 'macro', 'excerpt': '工业增加值持续增长。'}],
            'missing_dimensions': ['events', 'related_markets']}
        result['dimensions']['fundamentals'].update(status='AVAILABLE', citations=[
            {'source_id': 'S1', 'quote': '库存下降'}, {'source_id': 'S2', 'quote': '工业增加值'}])
        with self.assertRaises(ValueError):
            future_trend.validate_result(result, snapshot)

    def test_model_future_metrics_are_removed(self):
        result, snapshot = self._result()
        result['future_metrics'] = {'score': 100, 'status': 'VERIFIED'}
        future_trend.validate_result(result, snapshot)
        self.assertNotIn('future_metrics', result)


if __name__ == '__main__':
    unittest.main()
