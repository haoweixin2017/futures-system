import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

try:
    import test_bootstrap
except ImportError:
    from tests import test_bootstrap

from backend.ai_analysis import snapshot
from tests.test_future_market_quality import row
from backend.models.market_data import BarData


class FutureBarProvenanceTests(unittest.TestCase):
    def test_future_freeze_uses_history_without_polling_aggregate_cache(self):
        from backend.storage import history_backfill
        from backend.common.trading_time import FuturesTradingCalendar
        now = datetime(2026, 10, 15, 16)
        responses = {}
        for period in ('1d', '60m', '15m'):
            if period == '1d':
                labels = [datetime(2026, 10, 15, 15)]
                while len(labels) < 22:
                    labels.insert(0, datetime.combine(
                        FuturesTradingCalendar.previous_trading_day(labels[0].date()),
                        datetime.min.time()).replace(hour=15))
            else:
                labels = [datetime.fromisoformat(t) for t in
                          snapshot._future_expected_labels('FU2701', period, now)['HISTORY_END_CANDIDATE'][-25:]]
            responses[period] = [history_backfill._build_bar('FU2701', period, t,
                Decimal('100'), Decimal('101'), Decimal('99'), Decimal('100'), 10,
                Decimal('100'), Decimal('100'), {'source': 'sina_history',
                'label_semantics': 'DAILY_CLOSE' if period == '1d' else 'END',
                'coverage': 'UNKNOWN'}) for t in labels]
        # Source may already return the next, still-forming historical bar.
        responses['60m'].append(dict(responses['60m'][-1], bar_time=datetime(2026,10,15,22), trading_day='20261016'))
        with patch.object(history_backfill, 'fetch_sina_daily_bars', return_value=responses['1d']) as daily, \
             patch.object(history_backfill, 'fetch_sina_minute_bars', side_effect=lambda i,p: responses[p]) as minute, \
             patch.object(snapshot, '_instrument_meta', return_value=('燃料油',10,Decimal('1'))), \
             patch.object(snapshot, '_load_bars', return_value=[]) as local:
            result, _ = snapshot.build_future_snapshot('FU2701', {'trading_day':'20261015',
                'timestamp':'2026-10-15T15:00:00','last_price':'100'}, '', now)
        self.assertTrue(result['quality']['can_judge'], result['quality']['blocking_reasons'])
        self.assertEqual(daily.call_count, 1)
        self.assertEqual(minute.call_args_list, [unittest.mock.call('FU2701','60m'), unittest.mock.call('FU2701','15m')])
        self.assertEqual(local.call_args_list, [unittest.mock.call('FU2701','1m',5)])
        self.assertTrue(all(b['provenance']['source']=='sina_history'
                            for b in result['market']['bars_60m']))
        self.assertNotIn('2026-10-15T22:00:00', [b['bar_time'] for b in result['market']['bars_60m']])
        self.assertIn('SOURCE_COVERAGE_UNVERIFIED', result['quality']['warnings'])

    def test_failed_history_request_blocks_without_falling_back_to_partial_cache(self):
        from backend.storage import history_backfill
        with patch.object(history_backfill, 'fetch_sina_daily_bars', side_effect=RuntimeError('source unavailable')), \
             patch.object(history_backfill, 'fetch_sina_minute_bars', return_value=[]), \
             patch.object(snapshot, '_instrument_meta', return_value=('燃料油',10,Decimal('1'))), \
             patch.object(snapshot, '_load_bars', return_value=[]):
            result, _ = snapshot.build_future_snapshot('FU2701', {'trading_day':'20261015',
                'timestamp':'2026-10-15T15:00:00','last_price':'100'}, '', datetime(2026,10,15,16))
        self.assertIn('HISTORY_FETCH_FAILED_1D', result['quality']['blocking_reasons'])
        self.assertFalse(result['quality']['can_judge'])
        self.assertEqual(result['market']['daily_bars_1d'], [])

    def test_redis_source_survives_freeze_and_bad_json_is_unknown(self):
        data = dict(instrument='JD2612', trading_day='20260930', period='60m',
                    bar_time='2026-09-30T15:00:00', open_price='100', high_price='101',
                    low_price='99', close_price='100', volume=10, turnover='0', open_interest='100',
                    provenance_json='{"source":"sina_history","label_semantics":"END","coverage":"UNKNOWN"}')
        frozen = snapshot._closed_bar_json(snapshot._bar_from_dict(data))
        self.assertEqual(frozen['provenance']['label_semantics'], 'END')
        data['provenance_json'] = '{broken'
        self.assertEqual(snapshot._closed_bar_json(snapshot._bar_from_dict(data))['provenance']['source'], 'UNKNOWN')

    def test_partial_coverage_blocks_even_when_labels_are_contiguous(self):
        bars = [row(f'2026-09-{day}T{hour}:00:00') for day in ('29', '30')
                for hour in ('09', '10', '11', '13', '14')]
        for bar in bars:
            bar['provenance'] = {'source': 'sina_tick_aggregate', 'label_semantics': 'START',
                                 'coverage': 'PARTIAL'}
        quality = snapshot._future_market_quality('JD2612', {'60m': bars}, Decimal('1'), datetime(2026,10,6,10))
        self.assertIn('PARTIAL_COVERAGE_60M', quality['blocking_reasons'])
        self.assertFalse(bars[-1]['usage_limits']['volume_oi'])

    def test_known_labels_cannot_borrow_other_grid_for_recent_window(self):
        bars = [row(f'2026-09-{day}T{hour}:00:00') for day in ('29', '30')
                for hour in ('09', '10', '11', '13', '14')]
        for bar in bars:
            bar['provenance'] = {'source': 'sina_history', 'label_semantics': 'END', 'coverage': 'UNKNOWN'}
        quality = snapshot._future_market_quality('JD2612', {'60m': bars}, Decimal('1'), datetime(2026,10,6,10))
        self.assertIn('INSUFFICIENT_CONTIGUOUS_60M', quality['blocking_reasons'])

    def test_known_end_bar_is_closed_at_label_time(self):
        bar = BarData(instrument='JD2612', trading_day='20260930', period='60m',
                      bar_time=datetime(2026,9,30,10), open_price=Decimal('100'), high_price=Decimal('101'),
                      low_price=Decimal('99'), close_price=Decimal('100'), volume=10, turnover=Decimal('0'),
                      open_interest=Decimal('100'), provenance_json={'source':'sina_history',
                      'label_semantics':'END','coverage':'UNKNOWN'})
        with patch.object(snapshot, '_instrument_meta', return_value=('鸡蛋',10,Decimal('1'))), \
             patch.object(snapshot, '_load_future_bars', side_effect=lambda i,p,*a: [bar] if p=='60m' else []):
            result, _ = snapshot.build_future_snapshot('JD2612', {'trading_day':'20260930',
                'timestamp':'2026-09-30T10:05:00','last_price':'100'}, '', datetime(2026,9,30,10,5))
        self.assertEqual(len(result['market']['bars_60m']), 1)
        self.assertEqual(result['market']['bars_60m'][0]['bar_time'], '2026-09-30T10:00:00')

    def test_unknown_coverage_does_not_become_verified_from_known_label(self):
        bars = [row(f'2026-09-{day}T{hour}:00:00') for day in ('29', '30')
                for hour in ('09', '10', '11', '13', '14')]
        for bar in bars:
            bar['provenance'] = {'source':'sina_tick_aggregate', 'label_semantics':'START', 'coverage':'UNKNOWN'}
        quality = snapshot._future_market_quality('JD2612', {'60m':bars}, Decimal('1'), datetime(2026,10,6,10))
        self.assertEqual(quality['period_checks']['60m']['label_status'], 'VERIFIED')
        self.assertEqual(quality['period_checks']['60m']['continuity_status'], 'UNVERIFIED')
        self.assertIsNone(bars[-1]['open_interest_change'])


if __name__ == '__main__':
    unittest.main()
