import unittest
from datetime import datetime
from backend.common.trading_time import FuturesTradingCalendar

class TestTradingTime(unittest.TestCase):
    def test_friday_night(self):
        # 2026-09-11 是星期五
        dt = datetime(2026, 9, 11, 21, 15, 0)
        self.assertEqual(dt.weekday(), 4)
        t_day = FuturesTradingCalendar.get_trading_day(dt)
        self.assertEqual(t_day, "20260914") # 归属下周一

    def test_normal_trading_hours(self):
        # 白盘早盘
        dt1 = datetime(2026, 9, 11, 9, 30, 0)
        self.assertTrue(FuturesTradingCalendar.is_trading_time(dt1))
        # 10:15~10:30 休市
        dt2 = datetime(2026, 9, 11, 10, 20, 0)
        self.assertFalse(FuturesTradingCalendar.is_trading_time(dt2))
        # 下午盘
        dt3 = datetime(2026, 9, 11, 14, 0, 0)
        self.assertTrue(FuturesTradingCalendar.is_trading_time(dt3))
        # 盘后
        dt4 = datetime(2026, 9, 11, 15, 30, 0)
        self.assertFalse(FuturesTradingCalendar.is_trading_time(dt4))

    def test_next_open_target_distinguishes_night_and_day_products(self):
        self.assertEqual(FuturesTradingCalendar.next_open_target("FU2611", datetime(2026, 9, 16, 10)), {
            "start_time": "2026-09-16T21:00:00", "trading_day": "20260917",
            "session": "NIGHT", "label": "下一交易日夜盘", "open_time": "21:00",
        })
        self.assertEqual(FuturesTradingCalendar.next_open_target("AP2701", datetime(2026, 9, 16, 10)), {
            "start_time": "2026-09-17T09:00:00", "trading_day": "20260917",
            "session": "DAY", "label": "下一交易日日盘", "open_time": "09:00",
        })
        self.assertEqual(FuturesTradingCalendar.next_open_target("IF2610", datetime(2026, 9, 16, 10)), {
            "start_time": "2026-09-17T09:30:00", "trading_day": "20260917",
            "session": "DAY", "label": "下一交易日日盘", "open_time": "09:30",
        })
        self.assertFalse(FuturesTradingCalendar.is_trading_time_for(
            datetime(2026, 9, 16, 21, 5), "AP2701"))

    def test_today_analysis_closes_at_day_end_and_reopens_for_next_night(self):
        available = FuturesTradingCalendar.today_analysis_available
        self.assertTrue(available(datetime(2026, 9, 24, 14, 59), "FU2611"))
        self.assertFalse(available(datetime(2026, 9, 24, 15, 0), "FU2611"))
        self.assertFalse(available(datetime(2026, 9, 24, 20, 59), "FU2611"))
        self.assertFalse(available(datetime(2026, 9, 24, 21, 0), "FU2611"))
        self.assertFalse(available(datetime(2026, 9, 25, 21, 0), "FU2611"))
        self.assertFalse(available(datetime(2026, 9, 24, 21, 0), "IF2610"))
        self.assertTrue(available(datetime(2026, 9, 24, 15, 14), "TF2612"))
        self.assertFalse(available(datetime(2026, 9, 24, 15, 15), "TF2612"))
        self.assertFalse(available(datetime(2026, 9, 24, 21, 0), "FU2611", "20260924"))
        self.assertFalse(available(datetime(2026, 9, 24, 21, 0), "FU2611", "20260925"))

    def test_mid_autumn_closure_and_reopen(self):
        calendar = FuturesTradingCalendar
        self.assertFalse(calendar.is_trading_time_for(datetime(2026, 9, 24, 21), "FU2611"))
        self.assertFalse(calendar.is_trading_time_for(datetime(2026, 9, 25, 9), "FU2611"))
        self.assertFalse(calendar.is_trading_time_for(datetime(2026, 9, 25, 0, 30), "SC2611"))
        self.assertFalse(calendar.is_trading_time(datetime(2026, 9, 24, 21)))
        self.assertEqual(calendar.get_trading_day(datetime(2026, 9, 24, 21)), "20260928")
        self.assertEqual(calendar.previous_trading_day(datetime(2026, 9, 28).date()).isoformat(),
                         "2026-09-24")
        self.assertTrue(calendar.is_trading_time_for(datetime(2026, 9, 28, 9, 30), "FU2611"))
        sessions = calendar.analysis_session_options(datetime(2026, 9, 24, 20), "FU2611")
        self.assertEqual({item["start_time"][:10] for item in sessions}, {"2026-09-28"})

if __name__ == "__main__":
    unittest.main()
