import unittest
from unittest.mock import patch

try:
    import test_bootstrap
except ImportError:
    from tests import test_bootstrap

from backend.ai_analysis import alerts


class AIAlertTests(unittest.TestCase):
    def setUp(self):
        alerts._last_sent.clear()

    def test_repeat_within_interval_is_suppressed_and_resend_after_interval(self):
        with patch.object(alerts.time, 'monotonic', side_effect=[0, 10, 3700]), \
             patch.object(alerts.notifier, 'notify') as notify:
            self.assertTrue(alerts.notify_throttled('key', 'AI_SETTLEMENT_ERROR', '第一次'))
            self.assertFalse(alerts.notify_throttled('key', 'AI_SETTLEMENT_ERROR', '十秒后重复'))
            self.assertTrue(alerts.notify_throttled('key', 'AI_SETTLEMENT_ERROR', '超过间隔重发'))
            self.assertEqual(notify.call_count, 2)

    def test_keys_throttle_independently(self):
        with patch.object(alerts.time, 'monotonic', return_value=0), \
             patch.object(alerts.notifier, 'notify') as notify:
            self.assertTrue(alerts.notify_throttled('research:FU2611:FAILED', 'AI_RESEARCH_JOB_FAILED', 'm'))
            self.assertTrue(alerts.notify_throttled('research:SC2611:FAILED', 'AI_RESEARCH_JOB_FAILED', 'm'))
            self.assertEqual(notify.call_count, 2)

    def test_notifier_failure_does_not_propagate_or_consume_window(self):
        with patch.object(alerts.notifier, 'notify', side_effect=RuntimeError('webhook down')), \
             patch.object(alerts.time, 'monotonic', return_value=0):
            self.assertFalse(alerts.notify_throttled('key', 'AI_SETTLEMENT_ERROR', 'm'))
        with patch.object(alerts.time, 'monotonic', return_value=1), \
             patch.object(alerts.notifier, 'notify') as notify:
            # 发送失败不占用限频窗口, 立即重试可再次发送
            self.assertTrue(alerts.notify_throttled('key', 'AI_SETTLEMENT_ERROR', 'm'))
            self.assertEqual(notify.call_count, 1)
