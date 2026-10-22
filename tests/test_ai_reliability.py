import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import test_bootstrap  # noqa: F401
except ImportError:
    from tests import test_bootstrap  # noqa: F401
from backend.ai_analysis import settlement, snapshot, provider, orchestrator
from backend.common.trading_time import FuturesTradingCalendar as Calendar


class SettlementReliabilityTests(unittest.TestCase):
    def test_due_session_without_reference_is_unverifiable(self):
        snap = {'session_target': {'start_time': '2026-09-28T13:30:00',
                                   'end_time': '2026-09-28T15:00:00'}}
        result = settlement._settle_session(Mock(), Mock(), Mock(), snap, datetime(2026, 9, 28, 16))
        self.assertEqual(result['rating'], 'UNVERIFIABLE')

    def test_next_open_is_frozen_across_holiday_and_financial_open(self):
        target = Calendar.next_open_target('RB2610', datetime(2026, 9, 30, 14), '20260930')
        self.assertEqual(target['start_time'], '2026-10-08T09:00:00')
        self.assertEqual(target['trading_day'], '20261008')
        target = Calendar.next_open_target('IF2610', datetime(2026, 9, 28, 10), '20260928')
        self.assertEqual(target['start_time'], '2026-09-29T09:30:00')

    def test_missing_exact_close_is_unverifiable(self):
        db = Mock()
        db.query.return_value.filter_by.return_value.order_by.return_value.first.return_value = SimpleNamespace(
            bar_time=datetime(2026, 9, 28, 14), close_price=110)
        db.query.return_value.filter_by.return_value.first.return_value = None
        snap = {'market': {'quote': {'last_price': 100}}, 'settlement_target': {
            'trading_day': '20260928', 'bar_time': '2026-09-28T14:59:00',
            'due_time': '2026-09-28T15:02:00', 'reference_price': 100, 'price_field': 'close_price'}}
        result = settlement._settle_today(db, None, SimpleNamespace(instrument_id='RB2610',
            trading_day='20260928'), snap, datetime(2026, 10, 1, 16))
        # now 取3天宽限期外; 宽限期内该场景返回 None 等待行情回补
        self.assertIsInstance(result, dict)
        self.assertEqual(result['rating'], 'UNVERIFIABLE')


class QualityReliabilityTests(unittest.TestCase):
    def test_session_quality_checks_only_the_frozen_evidence(self):
        from backend.models.market_data import BarData
        from datetime import timedelta
        now = datetime(2026, 10, 22, 14, 20, 20)
        morning = BarData(instrument='FU2701', trading_day='20261022', period='1m',
            bar_time=now.replace(hour=9, minute=20, second=0), open_price=100,
            high_price=101, low_price=99, close_price=100, volume=100,
            turnover=0, open_interest=200,
            provenance_json={'volume_quality': 'UNALLOCATED_GAP'})
        afternoon = [morning.model_copy(update={
            'bar_time': now.replace(hour=13, minute=30, second=0) + timedelta(minutes=i),
            'provenance_json': {'source': 'sina_tick_aggregate', 'label_semantics': 'START'}})
            for i in range(50)]
        target = dict(session='DAY', label='日盘第三节', status='ACTIVE',
                      start_time='2026-10-22T13:30:00', end_time='2026-10-22T15:00:00')
        quote = dict(timestamp=now.isoformat(), trading_day='20261022', last_price='100')
        redis = Mock(); redis.hgetall.return_value = quote
        def load(symbol, period, *args):
            return [morning, *afternoon] if period == '1m' else []
        with patch.object(snapshot, 'datetime', wraps=datetime) as clock, \
             patch.object(snapshot.RedisClient, 'get_client', return_value=redis), \
             patch.object(snapshot, '_load_bars', side_effect=load), \
             patch.object(snapshot, '_instrument_meta', return_value=('FU', 10, 1)), \
             patch.object(snapshot, 'forming_bar', return_value=None), \
             patch.object(Calendar, 'analysis_session_options', return_value=[target]), \
             patch.object(snapshot.RealtimeAnalyzer, 'analyze', return_value={}):
            clock.now.side_effect = lambda tz=None: now if tz is None else now.replace(tzinfo=tz)
            # An unrelated morning gap must not poison clean afternoon evidence.
            clean, _ = snapshot.build_snapshot('FU2701', 'CURRENT_SESSION',
                                               session_start_time=target['start_time'])
            self.assertTrue(clean['quality']['can_judge'], clean['quality'])
            self.assertEqual(len(clean['market']['bars_1m']), 50)
            # Day-wide requests still include and reject the unknown morning volume.
            day, _ = snapshot.build_snapshot('FU2701', 'TODAY')
            self.assertIn('UNVERIFIED_MINUTE_VOLUME', day['quality']['blocking_reasons'])
            # Unknown volume inside the selected session must still block.
            afternoon[0].provenance_json = morning.provenance_json
            bad, _ = snapshot.build_snapshot('FU2701', 'CURRENT_SESSION',
                                             session_start_time=target['start_time'])
            self.assertIn('UNVERIFIED_MINUTE_VOLUME', bad['quality']['blocking_reasons'])
            self.assertEqual(bad['quality']['unverified_volume_bars'][0]['bar_time'],
                             '2026-10-22T13:30:00')

    def test_lunch_close_is_not_a_missing_start_labelled_minute(self):
        for instrument in ('FU2611', 'IF2610', 'T2612'):
            with self.subTest(instrument=instrument):
                bars = [SimpleNamespace(bar_time=datetime(2026, 10, 8, 11, 29)),
                        SimpleNamespace(bar_time=datetime(2026, 10, 8, 13, 30))]
                # Financial contracts resume at 13:00.
                if instrument != 'FU2611':
                    bars[-1].bar_time = datetime(2026, 10, 8, 13)
                self.assertEqual(snapshot._missing_intervals(bars, instrument, '20261008'), [])
                self.assertFalse(Calendar.is_trading_time_for(datetime(2026, 10, 8, 11, 30), instrument))
                self.assertTrue(Calendar.is_trading_time_for(datetime(2026, 10, 8, 11, 29, 59), instrument))

    def test_real_missing_minute_before_lunch_is_still_detected(self):
        bars = [SimpleNamespace(bar_time=datetime(2026, 10, 8, 11, 28)),
                SimpleNamespace(bar_time=datetime(2026, 10, 8, 13, 30))]
        self.assertEqual(snapshot._missing_intervals(bars, 'FU2611', '20261008'),
                         [{'start': '2026-10-08T11:29:00', 'end': '2026-10-08T11:29:00'}])

    def test_forged_evidence_and_invalid_price_types_are_rejected(self):
        answer = {'schema_version': '1.1', 'instrument': 'RB2610', 'trading_day': '20260916',
                  'snapshot_time': '2026-09-16T10:40:00', 'generated_at': '2026-09-16T10:40:00',
                  'direction': 'LONG', 'confidence_score': 80, 'summary': '证据测试',
                  'support_levels': [99], 'resistance_levels': [101], 'entry_condition': '突破',
                  'invalidation_condition': '回落', 'risks': ['波动'],
                  'data_quality': {'status': 'GOOD', 'warnings': []},
                  'evidence': [{'side': side, 'time': '2026-09-16T10:20:00', 'field': field,
                                'value': value, 'reason': '证据'}
                               for side in ('LONG', 'SHORT') for field, value in [('high', 101), ('low', 99)]]}
        snap = {'market': {'snapshot_time': answer['snapshot_time'], 'bars_1m': [
            {'bar_time': '2026-09-16T10:20:00', 'high': 101, 'low': 99}]}}
        provider.parse_analysis_json(json.dumps(answer), snapshot=snap)
        for value in (True, -1, float('nan'), float('inf'), '101'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                provider.parse_analysis_json(json.dumps({**answer, 'support_levels': [value]}), snapshot=snap)
        answer['evidence'][0]['value'] = 999
        with self.assertRaisesRegex(ValueError, '核对'):
            provider.parse_analysis_json(json.dumps(answer), snapshot=snap)

    def test_missing_vote_does_not_turn_one_valid_vote_into_majority(self):
        result = orchestrator._majority_draft([{'direction': 'LONG', 'confidence_score': 90}], 'TODAY', 3)
        self.assertEqual(result['direction'], 'NO_JUDGMENT')

    def test_latest_quality_rejects_stale_quote_and_missing_bars(self):
        now = datetime(2026, 9, 28, 9, 5)
        data = {'timestamp': '2026-09-28T09:00:00', 'trading_day': '20260928', 'last_price': '3500'}
        self.assertFalse(snapshot.market_quality('RB2610', data, [], now)['can_judge'])

    def test_minute_rollover_checks_closed_bar_end_instead_of_start(self):
        # FU2611 的真实拦截：09:14:02 检查 09:12–09:13，闭合后仅约 62 秒。
        quote = {'timestamp': '2026-10-08T09:13:59', 'trading_day': '20261008', 'last_price': '4890'}
        for provenance in ({'label_semantics': 'START'}, {'label_semantics': 'UNKNOWN'}, None):
            with self.subTest(provenance=provenance):
                bars = [{'bar_time': '2026-10-08T09:12:00', 'is_closed': True,
                         'provenance': provenance}]
                quality = snapshot.market_quality('FU2611', quote, bars, datetime(2026, 10, 8, 9, 14, 2))
                self.assertTrue(quality['can_judge'], quality['blocking_reasons'])

    def test_bar_end_label_does_not_receive_an_extra_minute(self):
        quote = {'timestamp': '2026-10-08T09:14:00', 'trading_day': '20261008', 'last_price': '4890'}
        for label, can_judge in [('2026-10-08T09:13:00', True), ('2026-10-08T09:12:00', False)]:
            with self.subTest(label=label):
                bars = [{'bar_time': label, 'is_closed': True, 'provenance': {'label_semantics': 'END'}}]
                quality = snapshot.market_quality('FU2611', quote, bars, datetime(2026, 10, 8, 9, 14, 2))
                self.assertEqual(quality['can_judge'], can_judge)
                self.assertEqual('STALE_1M_BARS' in quality['blocking_reasons'], not can_judge)

    def test_fresh_quote_does_not_hide_bars_closed_over_two_minutes_ago(self):
        quote = {'timestamp': '2026-10-08T09:15:01', 'trading_day': '20261008', 'last_price': '4890'}
        bars = [{'bar_time': '2026-10-08T09:12:00', 'is_closed': True,
                 'provenance': {'label_semantics': 'START'}}]
        quality = snapshot.market_quality('FU2611', quote, bars, datetime(2026, 10, 8, 9, 15, 1))
        self.assertFalse(quality['can_judge'])
        self.assertIn('STALE_1M_BARS', quality['blocking_reasons'])

    def test_session_resume_grace_expires_after_two_trading_minutes(self):
        bars = [{'bar_time': '2026-10-08T10:14:00', 'is_closed': True,
                 'provenance': {'label_semantics': 'START'}}]
        for stamp, can_judge in [('2026-10-08T10:30:30', True), ('2026-10-08T10:32:01', False)]:
            with self.subTest(stamp=stamp):
                quote = {'timestamp': stamp, 'trading_day': '20261008', 'last_price': '4890'}
                quality = snapshot.market_quality('FU2611', quote, bars, datetime.fromisoformat(stamp))
                self.assertEqual(quality['can_judge'], can_judge)
                self.assertEqual('STALE_1M_BARS' in quality['blocking_reasons'], not can_judge)

    def test_gap_before_target_touch_is_not_a_verified_hit(self):
        bars = [SimpleNamespace(bar_time=datetime(2026, 9, 28, 10, 35), high_price=101, low_price=99),
                SimpleNamespace(bar_time=datetime(2026, 9, 28, 10, 37), high_price=111, low_price=99)]
        state, _ = settlement._session_line_state(bars, {'target_price': 110, 'invalidation_price': 90}, 'LONG')
        self.assertEqual(state, 'UNVERIFIABLE')

    def test_summary_usage_reads_sent_payload_not_planned_list(self):
        planned = {'enabled': True, 'summaries': [{'summary_id': 'one', 'summary': {'verdict': '经验'}}]}
        body = {'messages': [{'role': 'user', 'content': json.dumps({'review_context': planned})}]}
        self.assertEqual(orchestrator.sent_review_summaries(body)[0]['summary_id'], 'one')
        body['messages'][0]['content'] = json.dumps({'segment_summaries': []})
        self.assertEqual(orchestrator.sent_review_summaries(body), [])

    def test_stale_live_quote_blocks_but_closed_market_does_not(self):
        quote = {'timestamp': '2026-09-28T09:00:00', 'trading_day': '20260928', 'last_price': '3500'}
        bars = [{'bar_time': '2026-09-28T09:00:00'}]
        active = snapshot.market_quality('RB2610', quote, bars, datetime(2026, 9, 28, 9, 2))
        self.assertFalse(active['can_judge'])
        closed = snapshot.market_quality('RB2610', quote, bars, datetime(2026, 10, 1, 9, 2))
        self.assertTrue(closed['can_judge'])
        self.assertEqual(closed['market_state'], 'CLOSED')

    def test_current_quote_cannot_hide_wrong_trading_day_or_missing_bars(self):
        quote = {'timestamp': '2026-09-28T09:02:00', 'trading_day': '20260924', 'last_price': '3500'}
        quality = snapshot.market_quality('RB2610', quote, [], datetime(2026, 9, 28, 9, 2))
        self.assertFalse(quality['can_judge'])
        self.assertIn('TRADING_DAY_MISMATCH', quality['blocking_reasons'])

    def test_missing_required_answer_fields_rejected(self):
        with self.assertRaises(ValueError):
            provider.parse_analysis_json(json.dumps({'direction': 'LONG', 'confidence_score': 90}))

    def test_three_directions_must_not_select_highest_self_score(self):
        drafts = [{'direction': d, 'confidence_score': n} for d, n in [('LONG', 95), ('SHORT', 60), ('NEUTRAL', 40)]]
        result = orchestrator._majority_draft(drafts, 'TODAY')
        self.assertEqual(result['direction'], 'NO_JUDGMENT')

    def test_legacy_next_open_cannot_guess_a_target(self):
        db = Mock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        result = settlement._settle_next_open(db, None, SimpleNamespace(
            instrument_id='RB2610', trading_day='20260928', snapshot_time=datetime(2026, 9, 28)),
            {'market': {'quote': {'last_price': 100}}}, datetime(2026, 9, 29, 10))
        self.assertIsInstance(result, dict)
        self.assertEqual(result['rating'], 'UNVERIFIABLE')


if __name__ == '__main__':
    unittest.main()
