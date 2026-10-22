"""结果可用后的十分钟窗口，不调用数据库、模型或交易接口。"""
import unittest
from datetime import datetime, timezone
from backend.ai_analysis.session_window import chart_for_completion


class SessionWindowTests(unittest.TestCase):
    def chart(self, **updates):
        return dict(direction='LONG', forecast_start_time='2026-10-13T09:15:00+08:00',
                    forecast_end_time='2026-10-13T10:15:00', reference_price=2800, **updates)

    def test_window_starts_at_actual_completion_and_retains_frozen_evidence(self):
        original = self.chart()
        chart = chart_for_completion(original, datetime(2026,10,13,1,16,23))
        self.assertEqual(chart['forecast_start_time'], '2026-10-13T09:16:23+08:00')
        self.assertEqual(chart['forecast_end_time'], '2026-10-13T09:26:23+08:00')
        self.assertEqual(chart['evidence_cutoff_time'], original['forecast_start_time'])
        self.assertEqual(chart['session_end_time'], original['forecast_end_time'])
        self.assertEqual(original, self.chart())

    def test_each_model_uses_its_own_completion_and_clips_to_session_end(self):
        chart = chart_for_completion(self.chart(), datetime(2026,10,13,2,10,tzinfo=timezone.utc))
        self.assertEqual(chart['forecast_end_time'], '2026-10-13T10:15:00+08:00')
        later = chart_for_completion(self.chart(), datetime(2026,10,13,1,18))
        self.assertEqual(later['forecast_start_time'], '2026-10-13T09:18:00+08:00')

    def test_pre_open_and_missing_completion_do_not_invent_a_window(self):
        pre_open = self.chart(kind='PRE_OPEN')
        self.assertEqual(chart_for_completion(pre_open, datetime(2026,10,13,1,18)), pre_open)
        self.assertEqual(chart_for_completion(self.chart(), None), self.chart())

    def test_existing_window_is_idempotent_and_bad_times_are_safe(self):
        chart = chart_for_completion(self.chart(), datetime(2026,10,13,1,16,23))
        self.assertEqual(chart_for_completion(chart, datetime(2026,10,13,1,16,23)), chart)
        chart['session_end_time'] = 'invalid'
        self.assertEqual(chart_for_completion(chart, datetime(2026,10,13,1,16,23)), chart)


class CompletedWindowSettlementTests(unittest.TestCase):
    def setUp(self):
        from tests import test_bootstrap  # noqa: F401
        from backend.ai_analysis import settlement
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        self.settlement = settlement
        self.ns = SimpleNamespace
        self.chart = chart_for_completion(SessionWindowTests().chart(), datetime(2026,10,13,1,16,23))
        self.chart.update(direction='LONG',trigger_price=100,target_price=110,invalidation_price=90)
        self.session = MagicMock()
        self.query = self.session.query.return_value.filter.return_value.order_by.return_value
        self.batch = SimpleNamespace(instrument_id='TEST10M')
        self.run = SimpleNamespace(direction='LONG')
        self.snapshot = {'market':{'quote':{'last_price':99}}}
        self.bars = [SimpleNamespace(bar_time=datetime(2026,10,13,9,i),high_price=105,low_price=99,
                                    open_price=101,close_price=102) for i in range(17,26)]
        self.sample = SimpleNamespace(bar_time=datetime(2026,10,13,9,27),high_price=200,low_price=1,
                                     open_price=103,close_price=150)
        self.query.all.return_value = self.bars + [self.sample]

    def settle(self, now=datetime(2026,10,13,9,29)):
        return self.settlement._settle_completed_window(self.session,self.run,self.batch,self.snapshot,self.chart,now)

    def test_deadline_sampling_does_not_count_late_high_low_as_target(self):
        self.assertIsNone(self.settle(datetime(2026,10,13,9,26)))
        result = self.settle()
        self.assertEqual(result['target_state'],'PENDING')
        self.assertEqual(result['realized'],103)
        self.assertIn('分钟采样',result['detail'])

    def test_missing_reference_returns_safe_unverifiable(self):
        self.snapshot = {}
        result = self.settle()
        self.assertEqual(result['rating'],'UNVERIFIABLE')
        self.assertIsNone(result['realized'])

    def test_tail_missing_waits_then_marks_unverifiable(self):
        self.query.all.return_value = self.bars[:1] + [self.sample]
        self.assertIsNone(self.settle())
        result = self.settle(datetime(2026,10,17,9,29))
        self.assertEqual(result['rating'],'UNVERIFIABLE')

    def test_same_minute_trigger_and_target_is_ambiguous(self):
        self.bars[0].high_price=111
        result = self.settle()
        self.assertEqual(result['rating'],'UNVERIFIABLE')

    def test_no_trigger_does_not_get_correct_direction_score(self):
        for bar in self.bars:
            bar.high_price = 99
            bar.low_price = 95
        result = self.settle()
        self.assertEqual(result['target_state'],'WAITING_TRIGGER')
        self.assertEqual(result['rating'],'UNVERIFIABLE')

    def test_pre_completion_and_partial_deadline_bars_do_not_count(self):
        partial = self.ns(bar_time=datetime(2026,10,13,9,26),high_price=200,low_price=1,open_price=100,close_price=100)
        before = self.ns(bar_time=datetime(2026,10,13,9,16),high_price=200,low_price=1,open_price=100,close_price=100)
        self.query.all.return_value = [before] + self.bars + [partial,self.sample]
        result = self.settle()
        self.assertEqual(result['target_state'],'PENDING')
        self.assertEqual(result['realized'],103)

    def test_completion_after_close_has_no_valid_window(self):
        self.chart['forecast_start_time'] = '2026-10-13T10:16:00+08:00'
        self.chart['forecast_end_time'] = '2026-10-13T10:15:00+08:00'
        result = self.settle(datetime(2026,10,13,10,16))
        self.assertEqual(result['rating'],'UNVERIFIABLE')
        self.assertIsNone(result['realized'])
        self.session.query.assert_not_called()
