import unittest
from datetime import datetime, timedelta
from decimal import Decimal

try:
    import test_bootstrap  # noqa: F401
except ImportError:
    from tests import test_bootstrap  # noqa: F401

from backend.indicator_engine.realtime_analyzer import RealtimeAnalyzer
from backend.models.market_data import BarData


def bar(day, at, close=100, *, open_=None, high=None, low=None, volume=100,
        turnover=None, oi=1000, period="5m"):
    close = Decimal(str(close))
    open_ = Decimal(str(open_ if open_ is not None else close))
    high = Decimal(str(high if high is not None else max(open_, close)))
    low = Decimal(str(low if low is not None else min(open_, close)))
    turnover = Decimal(str(turnover if turnover is not None else close * volume * 10))
    return BarData(
        instrument="RB2610", trading_day=day, period=period, bar_time=at,
        open_price=open_, high_price=high, low_price=low, close_price=close,
        volume=volume, turnover=turnover, open_interest=Decimal(str(oi)),
        open_interest_change=Decimal("0"),
    )


def daily_bars(current_day="20260916", include_current=False):
    start = datetime(2026, 8, 1, 15)
    result = []
    for i in range(30):
        close = Decimal("90") + Decimal(i)
        dt = start + timedelta(days=i)
        result.append(bar(dt.strftime("%Y%m%d"), dt, close, high=close + 1,
                          low=close - 1, period="1d"))
    if include_current:
        result.append(bar(current_day, datetime(2026, 9, 16, 15), 999,
                          high=1000, low=998, period="1d"))
    return result


def five_minute_bars(current_day="20260916", breakout=True):
    result = []
    for d in range(5):
        dt = datetime(2026, 9, 8 + d, 9)
        day = dt.strftime("%Y%m%d")
        for i in range(21):
            result.append(bar(day, dt + timedelta(minutes=5 * i), 100,
                              high=101, low=99, volume=100, oi=1000 + i))
    dt = datetime(2026, 9, 16, 9)
    for i in range(20):
        result.append(bar(current_day, dt + timedelta(minutes=5 * i), 100,
                          high=101, low=99, volume=100, oi=1100 + i))
    if breakout:
        result.append(bar(current_day, dt + timedelta(minutes=100), 104,
                          open_=100, high=105, low=99.5, volume=250, oi=1150))
    else:
        result.append(bar(current_day, dt + timedelta(minutes=100), 100,
                          high=101, low=99, volume=100, oi=1150))
    return result


def one_minute_history(current_day="20260916"):
    result = []
    for d in range(5):
        dt = datetime(2026, 9, 8 + d, 10, 40)
        result.append(bar(dt.strftime("%Y%m%d"), dt, 100, high=101, low=99,
                          volume=100, period="1m"))
    start = datetime(2026, 9, 16, 10, 20)
    for i in range(20):
        result.append(bar(current_day, start + timedelta(minutes=i), 100,
                          high=101, low=99, volume=100, period="1m"))
    return result


def quote(now, price=104):
    return {
        "instrument": "RB2610", "trading_day": "20260916",
        "timestamp": now.isoformat(), "last_price": str(price),
        "day_open": "100", "day_high": "105", "day_low": "99",
        "avg_price": "101", "volume": "10000", "open_interest": "1150",
        "bid_price_1": str(Decimal(str(price)) - Decimal("1")),
        "ask_price_1": str(Decimal(str(price)) + Decimal("1")),
    }


class RealtimeAnalyzerTests(unittest.TestCase):
    def analyze(self, *, now=None, daily=None, intraday=None, q=None,
                bars_1m=None, forming_1m=None, feed="sina"):
        now = now or datetime(2026, 9, 16, 10, 41)
        return RealtimeAnalyzer.analyze(
            "RB2610", "螺纹钢2610", q or quote(now),
            daily if daily is not None else daily_bars(),
            intraday if intraday is not None else five_minute_bars(),
            volume_multiple=10, price_tick=Decimal("1"), now=now, feed=feed,
            bars_1m=bars_1m, forming_1m=forming_1m,
        )

    def test_current_daily_bar_is_excluded_from_previous_high(self):
        result = self.analyze(daily=daily_bars(include_current=True))
        self.assertEqual(result["analysis_status"], "READY")
        self.assertLess(result["weekly"]["previous_5d_high"], 1000)

    def test_closed_breakout_uses_same_slot_volume_baseline(self):
        result = self.analyze()
        self.assertEqual(result["volume"]["baseline_quality"], "HIGH")
        self.assertEqual(result["volume"]["volume_ratio"], 2.5)
        self.assertEqual(result["signal"]["code"], "BUY_VOLUME_BREAK")
        self.assertEqual(result["signal"]["state"], "PENDING")

    def test_breakout_becomes_confirmed_on_next_closed_bar(self):
        bars = five_minute_bars()
        bars.append(bar("20260916", datetime(2026, 9, 16, 10, 45), 105,
                        open_=104, high=106, low=103, volume=100, oi=1160))
        now = datetime(2026, 9, 16, 10, 46)
        result = self.analyze(now=now, intraday=bars, q=quote(now, 105))
        self.assertEqual(result["signal"]["code"], "BUY_VOLUME_BREAK")
        self.assertEqual(result["signal"]["state"], "CONFIRMED")

    def test_pending_signal_is_marked_invalid_after_breaking_risk_level(self):
        bars = five_minute_bars()
        bars.append(bar("20260916", datetime(2026, 9, 16, 10, 45), 99,
                        open_=104, high=104.5, low=98, volume=100, oi=1160))
        now = datetime(2026, 9, 16, 10, 46)
        result = self.analyze(now=now, intraday=bars, q=quote(now, 99))
        self.assertEqual(result["signal"]["state"], "INVALIDATED")

    def test_unconfirmed_signal_expires_after_three_bars(self):
        bars = five_minute_bars()
        for i in range(1, 4):
            bars.append(bar("20260916", datetime(2026, 9, 16, 10, 40) + timedelta(minutes=5 * i),
                            103, open_=103, high=104.5, low=100, volume=100, oi=1150 + i))
        now = datetime(2026, 9, 16, 10, 56)
        result = self.analyze(now=now, intraday=bars, q=quote(now, 103))
        self.assertEqual(result["signal"]["state"], "EXPIRED")

    def test_quote_avg_price_is_authoritative_vwap(self):
        result = self.analyze()
        self.assertEqual(result["intraday"]["vwap"], 101.0)
        self.assertEqual(result["data_quality"]["vwap_source"], "QUOTE_AVG_PRICE")

    def test_forming_one_minute_bar_only_creates_preview(self):
        now = datetime(2026, 9, 16, 10, 40, 30)
        forming = bar("20260916", datetime(2026, 9, 16, 10, 40), 104,
                      open_=100, high=105, low=99.5, volume=125, oi=1160,
                      period="1m").to_redis_dict()
        result = self.analyze(
            now=now, q=quote(now), bars_1m=one_minute_history(), forming_1m=forming
        )
        self.assertEqual(result["preview_signal"]["state"], "PREVIEW")
        self.assertEqual(result["preview_signal"]["timeframe"], "1m")
        self.assertFalse(result["preview_signal"]["actionable"])

    def test_stale_quote_suppresses_signal(self):
        now = datetime(2026, 9, 16, 10, 41)
        old = now - timedelta(seconds=26)
        result = self.analyze(now=now, q=quote(old))
        self.assertEqual(result["analysis_status"], "STALE_DATA")
        self.assertIsNone(result["signal"])

    def test_closed_market_hides_stale_quote_and_suppresses_signal(self):
        now = datetime(2026, 9, 16, 10, 20)
        result = self.analyze(now=now, q=quote(now - timedelta(seconds=780)))
        self.assertEqual(result["analysis_status"], "MARKET_CLOSED")
        self.assertIsNone(result["signal"])
        self.assertIsNone(result["preview_signal"])
        self.assertNotIn("延迟", result["status_message"])

    def test_no_night_session_on_holiday_eve_hides_stale_quote(self):
        now = datetime(2026, 9, 24, 21, 15)
        result = self.analyze(now=now, q=quote(now - timedelta(seconds=780)))
        self.assertEqual(result["analysis_status"], "MARKET_CLOSED")

    def test_normal_sina_source_delay_keeps_simulated_analysis_available(self):
        now = datetime(2026, 9, 16, 10, 41)
        result = self.analyze(now=now, q=quote(now - timedelta(seconds=16)))
        self.assertEqual(result["analysis_status"], "READY")
        self.assertEqual(result["data_latency_seconds"], 16)

    def test_other_feed_keeps_strict_stale_limit(self):
        now = datetime(2026, 9, 16, 10, 41)
        result = self.analyze(now=now, q=quote(now - timedelta(seconds=11)), feed="ctp")
        self.assertEqual(result["analysis_status"], "STALE_DATA")

    def test_opening_warmup_suppresses_signal(self):
        bars = five_minute_bars()
        bars = [b for b in bars if b.trading_day != "20260916"] + [
            b for b in bars if b.trading_day == "20260916"
        ][:5]
        result = self.analyze(intraday=bars)
        self.assertEqual(result["analysis_status"], "WARMING_UP")
        self.assertIsNone(result["signal"])

    def test_no_trade_suppresses_signal(self):
        now = datetime(2026, 9, 16, 10, 41)
        q = quote(now)
        q["volume"] = "0"
        result = self.analyze(now=now, q=q)
        self.assertEqual(result["analysis_status"], "NO_TRADING")

    def test_missing_orderbook_suppresses_new_signal(self):
        now = datetime(2026, 9, 16, 10, 41)
        q = quote(now)
        q["ask_price_1"] = ""
        result = self.analyze(now=now, q=q)
        self.assertEqual(result["analysis_status"], "NO_TRADING")
        self.assertIsNone(result["signal"])


if __name__ == "__main__":
    unittest.main()
