"""Read-only MCP adapter contracts; local HTTP fixtures, no DB/CTP/public web."""
import importlib
import json
import threading
import unittest
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 30, 10, 10, tzinfo=TZ)


def bar(label, semantics="START", period="15m", day="20260930"):
    return {"instrument": "FU2611", "period": period, "trading_day": day,
            "bar_time": label, "open_price": "100.000", "high_price": "102.000",
            "low_price": "99.000", "close_price": "101.000", "volume": 10,
            "turnover": "0", "open_interest": "1000", "open_interest_change": "5",
            "provenance_json": {"label_semantics": semantics, "coverage": "UNKNOWN",
                                "unavailable_fields": ["turnover"]}}


class APIHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        self.server.calls.append((url.path, query))
        if self.server.redirect:
            self.send_response(302)
            self.send_header("Location", self.server.redirect)
            self.end_headers()
            return
        status = self.server.status
        if url.path.startswith("/api/v1/bars/") and not url.path.endswith("/dates"):
            rows = self.server.bars
            if "date" in query:
                rows = [b for b in rows if b["trading_day"] == query["date"][0]]
            rows = rows[-int(query.get("limit", [200])[0]):]
            data = {"instrument": "FU2611", "period": query.get("period", ["15m"])[0],
                    "source": "merged", "feed": "sina", "count": len(rows), "bars": rows}
        elif url.path.endswith("/dates"):
            data = {"instrument": "FU2611", "dates": ["20260929", "20260930"]}
        elif url.path == "/api/v1/feed-status":
            data = {"feed": "sina", "running": True, "market_open": True}
        elif url.path.startswith("/api/v1/quotes/"):
            data = {"instrument": "FU2611", "feed": "sina", "timestamp": "2026-09-30T09:00:00",
                    "last_price": "101.00", "volume": 1000, "open_interest": "2000"}
        elif url.path == "/api/v1/universe":
            data = {"total": 1, "contracts": [{"symbol": "FU2611", "name": "燃油2611"}]}
        elif url.path.startswith("/api/v1/indicators/"):
            data = {"ema20": "101", "vwap": None, "vwap_status": "missing_turnover"}
        elif url.path.startswith("/api/v1/signals/"):
            data = {"instrument": "FU2611", "signal": "NONE", "rule_score": 0}
        else:
            status, data = 404, {"detail": "not found"}
        if self.server.override is not None:
            data = self.server.override
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class MarketClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), APIHandler)
        cls.worker = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.api.server_close()
        cls.worker.join(timeout=3)

    def setUp(self):
        self.assertTrue((Path(__file__).resolve().parents[1] / "codex_mcp/market_client.py").is_file(),
                        "the approved read-only MCP client has not been implemented")
        self.module = importlib.import_module("codex_mcp.market_client")
        self.api.calls, self.api.override, self.api.redirect, self.api.status = [], None, None, 200
        self.api.bars = [bar("2026-09-30T09:00:00"), bar("2026-09-30T09:15:00"),
                         bar("2026-09-30T10:00:00", "END"), bar("2026-09-30T10:05:00")]
        self.client = self.module.MarketClient(f"http://127.0.0.1:{self.api.server_port}", clock=lambda: NOW)

    def test_closed_only_understands_end_labels_and_preserves_decimal_values(self):
        result = self.client.bars("fu2611", limit=2)
        self.assertEqual([b["bar_time"] for b in result["bars"]],
                         ["2026-09-30T09:15:00", "2026-09-30T10:00:00"])
        self.assertEqual(result["bars"][-1]["close_price"], "101.000")
        self.assertEqual(result["bars"][-1]["closure_status"], "CLOSED")
        self.assertEqual(result["latest_bar_end_time"], "2026-09-30T10:00:00+08:00")
        self.assertEqual(result["forming_excluded"], 1)
        self.assertIn("MIXED_TIME_LABEL_SEMANTICS", result["quality"]["warnings"])

    def test_forming_bar_is_explicit_when_requested(self):
        result = self.client.bars("FU2611", closed_only=False)
        self.assertEqual(result["bars"][-1]["closure_status"], "FORMING")
        self.assertEqual(result["bars"][-1]["bar_end_time"], "2026-09-30T10:15:00+08:00")

    def test_compact_default_keeps_source_caveats_and_full_provenance_is_optional(self):
        result = self.client.bars("FU2611")
        self.assertNotIn("provenance_json", result["bars"][0])
        self.assertEqual(result["bars"][0]["provenance"]["unavailable_fields"], ["turnover"])
        full = self.client.bars("FU2611", include_provenance=True)
        self.assertEqual(full["bars"][0]["provenance_json"]["label_semantics"], "START")

    def test_nonfinite_volume_is_rejected(self):
        self.api.bars[0]["volume"] = "NaN"
        with self.assertRaisesRegex(self.module.MarketError, "INVALID_BAR_VALUES"):
            self.client.bars("FU2611")

    def test_infinite_volume_and_invalid_open_interest_are_rejected(self):
        for key, value in [("volume", "Infinity"), ("volume", "-Infinity"),
                           ("open_interest", "Infinity"), ("open_interest", "-1"),
                           ("open_interest_change", "NaN"), ("turnover", "Infinity")]:
            with self.subTest(key=key, value=value):
                self.api.bars = [bar("2026-09-30T09:00:00")]
                self.api.bars[0][key] = value
                with self.assertRaisesRegex(self.module.MarketError, "INVALID_BAR_VALUES"):
                    self.client.bars("FU2611")

    def test_intrasection_gap_is_reported(self):
        self.api.bars = [bar("2026-09-30T09:00:00"), bar("2026-09-30T09:30:00")]
        result = self.client.bars("FU2611")
        self.assertEqual(result["quality"]["missing_labels"], ["2026-09-30T09:15:00+08:00"])
        self.assertIn("INTRA_SESSION_GAPS", result["quality"]["warnings"])

    def test_regular_break_is_not_a_missing_bar(self):
        self.api.bars = [bar("2026-09-30T10:00:00"), bar("2026-09-30T10:30:00")]
        self.client = self.module.MarketClient(f"http://127.0.0.1:{self.api.server_port}",
                                            clock=lambda: NOW.replace(hour=11))
        result = self.client.bars("FU2611")
        self.assertEqual(result["quality"]["missing_labels"], [])

    def test_unknown_provenance_is_not_declared_verified(self):
        self.api.bars = [bar("2026-09-30T09:00:00")]
        self.api.bars[0].pop("provenance_json")
        result = self.client.bars("FU2611")
        self.assertIn("UNKNOWN_TIME_LABEL_SEMANTICS", result["quality"]["warnings"])
        self.assertEqual(result["quality"]["coverage_status"], "UNVERIFIED")

    def test_daily_close_time_not_a_whole_extra_day(self):
        self.api.bars = [bar("2026-09-29T15:00:00", "DAILY_CLOSE", "1d", "20260929")]
        result = self.client.bars("FU2611", period="1d")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["latest_bar_end_time"], "2026-09-29T15:00:00+08:00")

    def test_request_for_trade_date_forwards_exact_date(self):
        result = self.client.bars("FU2611", trading_day="20260929")
        self.assertEqual(result["count"], 0)
        self.assertEqual(self.api.calls[0][1]["date"], ["20260929"])
        self.assertIn("NO_BARS", result["quality"]["warnings"])

    def test_wrong_contract_is_rejected_instead_of_mixed_into_analysis(self):
        self.api.bars[0]["instrument"] = "BU2611"
        with self.assertRaisesRegex(self.module.MarketError, "BAR_IDENTITY_MISMATCH"):
            self.client.bars("FU2611")

    def test_boolean_limit_invalid_date_period_and_extra_path_rejected_before_io(self):
        cases = [{"instrument": "FU2611", "limit": True},
                 {"instrument": "FU2611", "limit": 1001},
                 {"instrument": "FU2611", "period": "2m"},
                 {"instrument": "FU2611", "trading_day": "20260230"},
                 {"instrument": "../trade/live/order"}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(self.module.MarketError):
                self.client.bars(**kwargs)
        self.assertEqual(self.api.calls, [])

    def test_remote_or_credential_url_cannot_be_configured(self):
        for url in ["https://example.org", "http://192.168.1.2:8000", "http://user:pass@127.0.0.1:8000",
                    "http://127.0.0.1:8000/api", "http://127.0.0.1:8000?x=1"]:
            with self.subTest(url=url), self.assertRaises(self.module.MarketError):
                self.module.MarketClient(url)

    def test_http_redirect_is_not_followed(self):
        self.api.redirect = "http://192.168.1.2:8000/private"
        with self.assertRaisesRegex(self.module.MarketError, "HTTP_302"):
            self.client.quote("FU2611")
        self.assertEqual(len(self.api.calls), 1)

    def test_http_error_is_actionable(self):
        self.api.status = 404
        with self.assertRaisesRegex(self.module.MarketError, "HTTP_404"):
            self.client.bars("FU2611")

    def test_quote_old_timestamp_is_identified_in_open_session(self):
        result = self.client.quote("FU2611")
        self.assertEqual(result["freshness"], "STALE")
        self.assertEqual(result["age_seconds"], 4200)

    def test_indicators_and_signals_include_market_cutoff(self):
        indicators = self.client.indicators("FU2611")
        self.assertIsNone(indicators["indicators"]["vwap"])
        self.assertEqual(indicators["market_timestamp"], "2026-09-30T09:00:00")
        signal = self.client.signal("FU2611")
        self.assertEqual(signal["signal"]["signal"], "NONE")
        self.assertEqual(signal["market_timestamp"], "2026-09-30T09:00:00")

    def test_wrong_indicator_or_signal_identity_cannot_be_relabelled(self):
        self.api.override = {"instrument": "BU2611", "period": "60m", "signal": "NONE"}
        for method in [self.client.indicators, self.client.signal, self.client.dates]:
            with self.subTest(method=method.__name__), self.assertRaisesRegex(self.module.MarketError, "IDENTITY_MISMATCH"):
                method("FU2611")
        self.api.override = {"instrument": "FU2611", "period": "60m", "ema20": "100"}
        with self.assertRaisesRegex(self.module.MarketError, "IDENTITY_MISMATCH"):
            self.client.indicators("FU2611", "15m")

    def test_search_uses_get_and_does_not_subscribe(self):
        result = self.client.search("燃油")
        self.assertEqual(result["contracts"][0]["symbol"], "FU2611")
        self.assertEqual(self.api.calls[0][0], "/api/v1/universe")
        self.assertEqual(self.api.calls[0][1]["search"], ["燃油"])

    def test_bar_dates_use_existing_read_only_route(self):
        result = self.client.dates("FU2611", period="1m")
        self.assertEqual(result["dates"], ["20260929", "20260930"])

    def test_incorrect_feed_never_returns_ctp_data(self):
        self.api.override = {"instrument": "FU2611", "period": "15m", "feed": "ctp", "bars": []}
        with self.assertRaisesRegex(self.module.MarketError, "UNEXPECTED_FEED"):
            self.client.bars("FU2611")


if __name__ == "__main__":
    unittest.main()
