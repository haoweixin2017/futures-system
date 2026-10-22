"""Exercise the real MCP stdio process against a local HTTP fixture.

Run with .venv-mcp/Scripts/python.exe; no database, CTP, or internet access.
"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from tests.test_codex_mcp import APIHandler, ThreadingHTTPServer, bar

ROOT = Path(__file__).resolve().parents[1]


class MCPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.api = ThreadingHTTPServer(("127.0.0.1", 0), APIHandler)
        self.api.calls, self.api.override, self.api.redirect, self.api.status = [], None, None, 200
        self.api.bars = [bar("2026-09-30T09:00:00"), bar("2026-09-30T09:30:00")]
        self.worker = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)
        env = {**os.environ, "FUTURES_MCP_BASE_URL": f"http://127.0.0.1:{self.api.server_port}",
               "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        self.process = subprocess.Popen([sys.executable, str(ROOT / "codex_mcp/server.py")],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", env=env, cwd=tempfile.gettempdir())
        self.addCleanup(self.close_process)
        self.responses = queue.Queue()
        self.reader = threading.Thread(target=self.read_messages, daemon=True)
        self.reader.start()
        self.counter = 0
        result = self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                             "clientInfo": {"name": "futures-test", "version": "1.0"}})
        self.assertEqual(result["result"]["serverInfo"]["name"], "futures-market")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def close_process(self):
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()
        self.reader.join(timeout=2)

    def read_messages(self):
        for line in self.process.stdout:
            self.responses.put(line)

    def send(self, payload):
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def request(self, method, params):
        self.counter += 1
        self.send({"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params})
        line = self.responses.get(timeout=15)
        response = json.loads(line)
        self.assertEqual(response.get("id"), self.counter)
        return response

    def call(self, name, arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"]

    def test_registered_tools_are_read_only_and_bounded(self):
        tools = self.request("tools/list", {})["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {
            "get_futures_bars", "get_futures_quote", "get_futures_indicators", "get_futures_signal",
            "search_futures_contracts", "get_futures_bar_dates", "get_futures_feed_status"})
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
        schema = next(t for t in tools if t["name"] == "get_futures_bars")["inputSchema"]
        self.assertEqual(schema["properties"]["limit"]["maximum"], 1000)
        self.assertEqual(schema["properties"]["closed_only"]["default"], True)

    def test_call_reads_real_http_fixture_and_returns_structured_market_data(self):
        result = self.call("get_futures_bars", {"instrument": "FU2611", "period": "15m", "limit": 2})
        self.assertFalse(result.get("isError", False))
        data = result["structuredContent"]
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["bars"][0]["close_price"], "101.000")
        self.assertIn("INTRA_SESSION_GAPS", data["quality"]["warnings"])
        self.assertEqual(self.api.calls[0][0], "/api/v1/bars/FU2611")

    def test_schema_rejects_invalid_period_and_boolean_limit_before_http(self):
        for arguments in [{"instrument": "FU2611", "period": "2m"}, {"instrument": "FU2611", "limit": True}]:
            result = self.call("get_futures_bars", arguments)
            self.assertTrue(result["isError"])
        self.assertEqual(self.api.calls, [])

    def test_invalid_symbol_is_a_tool_error_without_http(self):
        result = self.call("get_futures_quote", {"instrument": "../trade/live/order"})
        self.assertTrue(result["isError"])
        self.assertIn("INVALID_INSTRUMENT", result["content"][0]["text"])
        self.assertEqual(self.api.calls, [])

    def test_service_failure_is_a_tool_error(self):
        self.api.status = 503
        result = self.call("get_futures_bars", {"instrument": "FU2611"})
        self.assertTrue(result["isError"])
        self.assertIn("HTTP_503", result["content"][0]["text"])

    def test_chinese_contract_search_over_stdio(self):
        result = self.call("search_futures_contracts", {"search": "燃油"})
        self.assertFalse(result.get("isError", False))
        self.assertEqual(result["structuredContent"]["contracts"][0]["name"], "燃油2611")
        self.assertEqual(self.api.calls[0][1]["search"], ["燃油"])


if __name__ == "__main__":
    unittest.main()
