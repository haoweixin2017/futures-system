try:
    import test_bootstrap  # noqa: F401  直接发现模式(-s tests)下生效, 切换隔离环境
except ImportError:
    from tests import test_bootstrap  # noqa: F401  以包模式运行时生效

import unittest
import io
import urllib.request
from email.message import Message
from urllib.response import addinfourl
from datetime import datetime
from unittest.mock import patch

from backend.collector.sina_collector import SinaRealtimeCollector, fetch_sina_quotes, parse_sina_payload, quote_to_tick
from backend.storage.universe_sync import UniverseSync, czce_to_canonical, normalize_symbol


SAMPLE_COMMODITY = (
    'var hq_str_nf_JM2701="焦煤2701,230000,1585.000,1596.000,1573.000,0.000,'
    '1589.000,1589.500,1589.000,0.000,1612.500,4,73,487228.000,211318,连,焦煤,2026-09-11,1,,,'
    ',,,,1588.009,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0";'
)

SAMPLE_CZCE = (
    'var hq_str_nf_AP2610="苹果2610,150000,7593.000,7627.000,7550.000,7587.000,'
    '7587.000,7591.000,7587.000,7589.000,7633.000,2,2,14178.000,6563,郑,苹果,2026-09-11,0,,,'
    ',,,,7589.000,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0";'
)

SAMPLE_CFFEX = (
    'var hq_str_nf_IC2609="7626.000,7626.000,7418.800,7569.000,112997,849308918.400,'
    '120928.000,7569.000,0.000,8482.400,6940.400,0.000,0.000,7705.800,7711.400,116839.000,7567.800,1,0.000";'
)


class TestSinaParser(unittest.TestCase):
    def test_quote_fetch_ignores_system_proxy_without_changing_global_opener(self):
        global_opener = urllib.request._opener

        def transport(request):
            self.assertEqual(request.host, "hq.sinajs.cn")
            self.assertIsNone(request._tunnel_host)
            self.assertEqual(request.get_header("Referer"), "https://finance.sina.com.cn")
            self.assertEqual(request.timeout, 2.5)
            response = addinfourl(io.BytesIO(SAMPLE_COMMODITY.encode("gbk")),
                                  Message(), request.full_url, 200)
            response.msg = "OK"
            return response

        with patch("urllib.request.getproxies", return_value={"https": "http://127.0.0.1:10809"}), \
                patch("urllib.request.proxy_bypass", return_value=False), \
                patch("urllib.request.HTTPSHandler.https_open", side_effect=transport):
            quotes = fetch_sina_quotes(["JM2701"], timeout=2.5)

        self.assertIn("JM2701", quotes)
        self.assertIs(urllib.request._opener, global_opener)

    def test_commodity_layout(self):
        quotes = parse_sina_payload(SAMPLE_COMMODITY)
        self.assertIn("JM2701", quotes)
        q = quotes["JM2701"]
        self.assertEqual(str(q["last_price"]), "1589.000")
        self.assertEqual(str(q["bid_price_1"]), "1589.000")
        self.assertEqual(str(q["ask_price_1"]), "1589.500")
        self.assertEqual(q["bid_volume_1"], 4)
        self.assertEqual(q["ask_volume_1"], 73)
        self.assertEqual(str(q["open_interest"]), "487228.000")
        self.assertEqual(q["volume"], 211318)
        self.assertEqual(q["timestamp"], datetime(2026, 9, 11, 23, 0, 0))

    def test_czce_layout(self):
        quotes = parse_sina_payload(SAMPLE_CZCE)
        self.assertIn("AP2610", quotes)
        q = quotes["AP2610"]
        self.assertEqual(str(q["last_price"]), "7587.000")
        self.assertEqual(q["volume"], 6563)

    def test_cffex_layout(self):
        quotes = parse_sina_payload(SAMPLE_CFFEX)
        self.assertIn("IC2609", quotes)
        q = quotes["IC2609"]
        self.assertEqual(str(q["last_price"]), "7569.000")
        self.assertEqual(q["volume"], 112997)
        self.assertIsNone(q["bid_price_1"])

    def test_empty_and_garbage_lines_ignored(self):
        raw = 'var hq_str_nf_jm2701="";\nvar bad_line=1;\n' + SAMPLE_COMMODITY
        quotes = parse_sina_payload(raw)
        self.assertEqual(list(quotes.keys()), ["JM2701"])

    def test_quote_to_tick_night_trading_day(self):
        """周五夜盘 23:00 的 Tick 交易日应归属下周一 20260914"""
        quotes = parse_sina_payload(SAMPLE_COMMODITY)
        tick = quote_to_tick("JM2701", quotes["JM2701"])
        self.assertEqual(tick.trading_day, "20260914")
        self.assertEqual(tick.exchange, "DCE")
        self.assertEqual(tick.volume, 211318)

    def test_empty_poll_preserves_last_tick_and_success_clears_old_error(self):
        collector = SinaRealtimeCollector(on_tick_callback=lambda tick: None)
        collector._running = True
        states = []

        def stop_after_poll(_seconds):
            collector._running = False

        with patch("backend.collector.sina_collector.get_watchlist", return_value=["FU2611"]), \
                patch("backend.collector.sina_collector.fetch_sina_quotes", return_value={"FU2611": {}}), \
                patch.object(collector, "_should_emit", return_value=False), \
                patch.object(collector, "_mark_status", side_effect=lambda **fields: states.append(fields)), \
                patch("backend.collector.sina_collector.time.sleep", side_effect=stop_after_poll):
            collector._run_loop()

        self.assertEqual(len(states), 1)
        self.assertNotIn("last_tick_at", states[0])
        self.assertEqual(states[0]["last_error"], "")

    def test_universe_status_updates_scoped_and_legacy_keys(self):
        class FakeRedis:
            def __init__(self):
                self.keys = []

            def hset(self, key, mapping):
                self.keys.append((key, mapping))

        fake = FakeRedis()
        with patch("backend.storage.universe_sync.RedisClient.get_client", return_value=fake):
            UniverseSync()._mark_status(universe_count="870")
        self.assertEqual([key for key, _ in fake.keys], ["feed:status:sina", "feed:status"])


class TestSinaMinuteShift(unittest.TestCase):
    """fetch_sina_minute_bars 必须对 1m 统一做 -1m 平移 (新浪以分钟结束时刻标记)。

    这是防止收盘整点幽灵 bar 复发的关键契约: 平移收敛在 fetch 内部, 调用方无需关心。
    """

    SAMPLE = (
        'var _=([{"d":"2026-09-11 21:01:00","o":"4200.000","h":"4205.000","l":"4198.000",'
        '"c":"4202.000","v":"1000","p":"221000.000"},'
        '{"d":"2026-09-11 21:02:00","o":"4202.000","h":"4210.000","l":"4200.000",'
        '"c":"4208.000","v":"1200","p":"221500.000"},'
        '{"d":"2026-09-11 23:00:00","o":"4300.000","h":"4310.000","l":"4295.000",'
        '"c":"4307.000","v":"900","p":"222000.000"}]);'
    )

    def test_1m_timestamps_are_shifted_back_one_minute(self):
        from unittest.mock import patch
        from backend.storage.history_backfill import fetch_sina_minute_bars
        with patch("backend.storage.history_backfill._http_get", return_value=self.SAMPLE):
            bars = fetch_sina_minute_bars("FU2611", "1m")
        stamps = [b["bar_time"] for b in bars]
        self.assertEqual(stamps, [
            datetime(2026, 9, 11, 21, 0),
            datetime(2026, 9, 11, 21, 1),
            datetime(2026, 9, 11, 22, 59),
        ])

    def test_1m_never_emits_close_ordinal(self):
        from unittest.mock import patch
        from backend.storage.history_backfill import fetch_sina_minute_bars
        with patch("backend.storage.history_backfill._http_get", return_value=self.SAMPLE):
            bars = fetch_sina_minute_bars("FU2611", "1m")
        self.assertNotIn(datetime(2026, 9, 11, 23, 0), [b["bar_time"] for b in bars])

    def test_higher_periods_keep_sina_convention(self):
        from unittest.mock import patch
        from backend.storage.history_backfill import fetch_sina_minute_bars
        with patch("backend.storage.history_backfill._http_get", return_value=self.SAMPLE):
            bars = fetch_sina_minute_bars("FU2611", "5m")
        self.assertEqual(bars[0]["bar_time"], datetime(2026, 9, 11, 21, 1))


class TestTradingTimeFor(unittest.TestCase):
    """按合约判定的交易时段 (区分 CFFEX / 商品 / 各品种夜盘收盘)。"""

    def _check(self, sym, ts, expected):
        from backend.common.trading_time import FuturesTradingCalendar as C
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M")
        self.assertEqual(C.is_trading_time_for(dt, sym), expected,
                         f"{sym} {ts}")

    def test_commodity_day_sessions(self):
        self._check("FU2611", "2026-09-14 09:00", True)
        self._check("FU2611", "2026-09-14 10:14", True)
        self._check("FU2611", "2026-09-14 10:15", False)
        self._check("FU2611", "2026-09-14 13:29", False)
        self._check("FU2611", "2026-09-14 13:30", True)
        self._check("FU2611", "2026-09-14 14:59", True)
        self._check("FU2611", "2026-09-14 15:00", False)

    def test_fu_night_ends_2300(self):
        self._check("FU2611", "2026-09-14 22:59", True)
        self._check("FU2611", "2026-09-14 23:00", False)

    def test_cffex_afternoon_starts_1300(self):
        self._check("IF2610", "2026-09-14 13:00", True)
        self._check("IF2610", "2026-09-14 14:59", True)
        self._check("IF2610", "2026-09-14 15:00", False)
        self._check("IF2610", "2026-09-14 09:29", False)
        self._check("IF2610", "2026-09-14 09:30", True)

    def test_sc_night_runs_to_0230(self):
        self._check("SC2611", "2026-09-11 23:00", True)
        self._check("SC2611", "2026-09-12 02:29", True)
        self._check("SC2611", "2026-09-12 02:30", False)

    def test_cu_night_runs_to_0100(self):
        self._check("CU2610", "2026-09-11 00:59", True)
        self._check("CU2610", "2026-09-11 01:00", False)


class TestCzceSymbolNormalize(unittest.TestCase):
    def test_czce_3digit_to_canonical(self):
        self.assertEqual(czce_to_canonical("AP610"), "AP2610")
        self.assertEqual(czce_to_canonical("SR701"), "SR2701")
        self.assertEqual(czce_to_canonical("SA501"), "SA3501")

    def test_normalize_by_exchange(self):
        self.assertEqual(normalize_symbol("jm2701", "DCE"), "JM2701")
        self.assertEqual(normalize_symbol("AP610", "CZCE"), "AP2610")
        self.assertEqual(normalize_symbol("IC2609", "CFFEX"), "IC2609")


if __name__ == "__main__":
    unittest.main()
