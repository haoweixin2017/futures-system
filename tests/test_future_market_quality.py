import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

try:
    import test_bootstrap
except ImportError:
    from tests import test_bootstrap

from backend.ai_analysis import snapshot
from backend.models.market_data import BarData


def row(time, period='60m', oi=100, volume=10, day=None):
    from backend.common.trading_time import FuturesTradingCalendar
    dt = datetime.fromisoformat(time)
    return {'bar_time': time, 'trading_day': day or FuturesTradingCalendar.get_trading_day(dt),
            'open': '100', 'high': '101', 'low': '99', 'close': '100',
            'volume': volume, 'open_interest': str(oi), 'open_interest_change': None}


class FutureMarketQualityTests(unittest.TestCase):
    def test_related_oi_comparison_preserves_gap_span_without_single_period_claim(self):
        bars = [BarData(instrument='BU2611', trading_day=day, period='60m',
            bar_time=datetime.fromisoformat(stamp), open_price=Decimal('100'),
            high_price=Decimal('101'), low_price=Decimal('99'), close_price=Decimal('100'),
            volume=10, turnover=Decimal('0'), open_interest=Decimal(oi))
            for day, stamp, oi in [('20260915','2026-09-15T14:00:00',100),
                                  ('20260930','2026-09-30T14:00:00',120)]]
        with patch.object(snapshot, '_instrument_meta', return_value=('沥青',10,Decimal('1'))), \
             patch.object(snapshot, '_load_bars', return_value=bars), \
             patch.object(snapshot.RedisClient, 'get_client') as redis:
            redis.return_value.hgetall.return_value = {}
            result = snapshot.build_related_market_snapshots(['BU2611'], '2026-10-06T10:00:00')
        row = result[0]['market']['bars_60m'][-1]
        self.assertEqual(row['open_interest_change_raw'], '20')
        self.assertIsNone(row['open_interest_change'])
        self.assertEqual(row['open_interest_comparison']['span_seconds'], 15 * 86400)

    def quality(self, rows, instrument='JD2612', now=datetime(2026, 10, 6, 10)):
        self.assertTrue(callable(getattr(snapshot, '_future_market_quality', None)),
                        'future snapshot must freeze structured market checks')
        return snapshot._future_market_quality(instrument, rows, Decimal('1'), now)

    def test_jd_gap_blocks_recent_window_and_preserves_cross_gap_fact(self):
        bars = [row('2026-09-15T15:00:00', oi=179896),
                row('2026-09-30T10:00:00', oi=209248),
                row('2026-09-30T11:00:00'), row('2026-09-30T13:00:00'),
                row('2026-09-30T14:00:00')]
        q = self.quality({'60m': bars, '15m': [], '1d': []})
        self.assertIn('INSUFFICIENT_CONTIGUOUS_60M', q['blocking_reasons'])
        self.assertIsNone(bars[1]['open_interest_change'])
        self.assertEqual(bars[1]['open_interest_change_raw'], '29352')
        self.assertEqual(bars[1]['open_interest_comparison']['from'], '2026-09-15T15:00:00')

    def test_holiday_and_lunch_do_not_break_complete_natural_clock_window(self):
        bars = [row(f'2026-09-{day}T{hour}:00:00') for day in ('29','30')
                for hour in ('09','10','11','13','14')]
        q = self.quality({'60m': bars, '15m': [], '1d': []})
        self.assertNotIn('INSUFFICIENT_CONTIGUOUS_60M', q['blocking_reasons'])
        self.assertEqual(q['period_checks']['60m']['continuity_status'], 'UNVERIFIED')
        self.assertIn('TIME_LABEL_UNVERIFIED', q['warnings'])

    def test_historical_end_labels_are_checked_without_shifting_values(self):
        bars = [row(f'2026-09-{day}T{time}:00') for day in ('28','29','30')
                for time in ('10:00','11:15','14:15','15:00')]
        q = self.quality({'60m': bars, '15m': [], '1d': []})
        self.assertNotIn('INSUFFICIENT_CONTIGUOUS_60M', q['blocking_reasons'])
        self.assertEqual(bars[-1]['bar_time'], '2026-09-30T15:00:00')

    def test_candidate_adjacency_does_not_prove_single_period_oi(self):
        bars = [row('2026-09-30T13:00:00', oi=200), row('2026-09-30T14:00:00', oi=210)]
        self.quality({'60m': bars, '15m': [], '1d': []})
        self.assertIsNone(bars[1]['open_interest_change'])
        self.assertEqual(bars[1]['open_interest_change_raw'], '10')
        self.assertFalse(bars[1]['usage_limits']['single_period_oi'])

    def test_zero_volume_oi_change_is_warning_and_limited_use(self):
        bars = [row('2026-09-30T09:45:00', '15m', oi=207167),
                row('2026-09-30T10:00:00', '15m', oi=208219, volume=0)]
        q = self.quality({'15m': bars, '60m': [], '1d': []})
        self.assertIn('ZERO_VOLUME_OI_CHANGE', q['warnings'])
        self.assertEqual(bars[1]['open_interest_change_raw'], '1052')
        self.assertFalse(bars[1]['usage_limits']['volume_oi'])

    def test_range_candidate_only_warns_and_one_tick_is_tolerated(self):
        daily = row('2026-09-30T15:00:00', '1d')
        bars = [row('2026-09-30T14:00:00')]
        bars[0]['high'] = '103'
        q = self.quality({'60m': bars, '15m': [], '1d': [daily]})
        self.assertIn('CROSS_PERIOD_RANGE_UNVERIFIED', q['warnings'])
        self.assertFalse(bars[0]['usage_limits']['precise_price_levels'])
        self.assertFalse(daily['usage_limits']['precise_price_levels'])
        self.assertNotIn('CROSS_PERIOD_RANGE_UNVERIFIED', q['blocking_reasons'])
        bars[0]['high'] = '102'
        q = self.quality({'60m': bars, '15m': [], '1d': [daily]})
        self.assertNotIn('CROSS_PERIOD_RANGE_UNVERIFIED', q['warnings'])

    def test_illegal_ohlc_and_trading_day_mismatch_block(self):
        bad = row('2026-09-30T14:00:00', day='20260929')
        bad['high'] = '90'
        q = self.quality({'60m': [bad], '15m': [], '1d': []})
        self.assertIn('INVALID_OHLC', q['blocking_reasons'])
        self.assertIn('BAR_TRADING_DAY_MISMATCH', q['blocking_reasons'])

    def test_night_to_next_trading_day_is_valid(self):
        bars = [row('2026-09-29T22:00:00'), row('2026-09-29T23:00:00')]
        q = self.quality({'60m': bars, '15m': [], '1d': []}, 'FU2611')
        self.assertNotIn('BAR_TRADING_DAY_MISMATCH', q['blocking_reasons'])

    def test_nonfinite_ohlc_blocks_instead_of_failing_range_audit(self):
        daily = row('2026-09-30T15:00:00', '1d')
        daily['high'] = 'NaN'
        q = self.quality({'60m': [row('2026-09-30T14:00:00')], '15m': [], '1d': [daily]})
        self.assertIn('INVALID_OHLC', q['blocking_reasons'])

    def test_forming_bar_not_required_in_latest_window(self):
        bars = [row(f'2026-09-{day}T{hour}:00:00') for day in ('28','29')
                for hour in ('09','10','11','13','14')]
        q = self.quality({'60m': bars, '15m': [], '1d': []}, now=datetime(2026,9,30,9,20))
        self.assertNotIn('INSUFFICIENT_CONTIGUOUS_60M', q['blocking_reasons'])

    def test_snapshot_rejects_wrong_instrument_and_sets_review_policy(self):
        b = BarData(instrument='BU2611', trading_day='20260930', period='60m',
            bar_time=datetime(2026,9,30,14), open_price=Decimal('100'),
            high_price=Decimal('101'), low_price=Decimal('99'), close_price=Decimal('100'),
            volume=10, turnover=Decimal('0'), open_interest=Decimal('100'))
        with patch.object(snapshot, '_instrument_meta', return_value=('鸡蛋',10,Decimal('1'))), \
             patch.object(snapshot, '_load_future_bars', side_effect=lambda i,p,*a: [b] if p=='60m' else []):
            snap, _ = snapshot.build_future_snapshot('JD2612',
                {'trading_day':'20260930','timestamp':'2026-09-30T15:00:00','last_price':'100'},
                '', datetime(2026,10,6,10))
        self.assertIn('BAR_INSTRUMENT_MISMATCH', snap['quality']['blocking_reasons'])
        self.assertEqual(snap['review_policy_version'], '1')

