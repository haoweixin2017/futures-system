"""Verify actual MCP stdio calls and save compact, local read-only evidence."""
import asyncio
import importlib.metadata
import json
import os
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/evidence/2026-10-07-codex-mcp/acceptance.json"
TZ = timezone(timedelta(hours=8))


async def verify():
    configuration = tomllib.loads((Path.home() / ".codex/config.toml").read_text(encoding="utf-8"))
    config = configuration.get("mcp_servers", {}).get("futures_market", {})
    assert Path(config.get("command", "")).resolve() == Path(sys.executable).resolve(), "Codex MCP interpreter differs"
    assert config.get("args") == [str(ROOT / "codex_mcp/server.py")], "Codex MCP entry point differs"
    assert config.get("enabled", True), "Codex MCP disabled"
    env = {**os.environ, **config.get("env", {}), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    parameters = StdioServerParameters(command=config["command"], args=config["args"], env=env)
    report = {"verified_at": datetime.now(TZ).isoformat(), "mcp_sdk": importlib.metadata.version("mcp"),
              "codex_config_name": "futures_market", "stdio_handshake": False, "checks": []}
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            report["stdio_handshake"] = initialized.serverInfo.name == "futures-market"
            tools = (await session.list_tools()).tools
            report["tools"] = [t.name for t in tools]
            assert len(tools) == 7 and all(t.annotations and t.annotations.readOnlyHint for t in tools)
            cases = [("get_futures_bars", {"instrument": "FU2611", "period": period, "limit": 20})
                     for period in ["1m", "5m", "15m", "30m", "60m", "1d"]]
            cases += [("get_futures_bars", {"instrument": "FU2611", "period": "60m", "limit": 200}),
                      ("get_futures_bars", {"instrument": "BU2611", "period": "60m", "limit": 200}),
                      ("get_futures_bars", {"instrument": "FU2611", "period": "1m", "limit": 5, "trading_day": "20260930"}),
                      ("get_futures_quote", {"instrument": "FU2611"}),
                      ("get_futures_indicators", {"instrument": "FU2611", "period": "15m"}),
                      ("get_futures_signal", {"instrument": "FU2611"}),
                      ("search_futures_contracts", {"search": "FU", "limit": 5}),
                      ("get_futures_bar_dates", {"instrument": "FU2611", "period": "1m"}),
                      ("get_futures_feed_status", {})]
            for name, arguments in cases:
                result = await session.call_tool(name, arguments)
                if result.isError:
                    report["checks"].append({"tool": name, "arguments": arguments, "status": "ERROR",
                                             "error": result.content[0].text})
                    continue
                data = result.structuredContent
                if data is None:
                    data = json.loads(result.content[0].text)
                record = {"tool": name, "arguments": arguments, "status": "PASS", "retrieved_at": data.get("retrieved_at")}
                if name == "get_futures_bars":
                    assert data["count"] > 0 and data["count"] <= arguments["limit"]
                    assert data["instrument"] == arguments["instrument"] and data["period"] == arguments["period"]
                    assert all(b["closure_status"] == "CLOSED" and b["instrument"] == arguments["instrument"]
                               and b["period"] == arguments["period"] for b in data["bars"])
                    assert data["latest_bar_end_time"].endswith("+08:00")
                    record.update({k: data[k] for k in ["count", "source", "feed", "latest_bar_end_time", "freshness", "quality"]})
                    record["last_bar"] = data["bars"][-1]
                elif name == "get_futures_quote":
                    record.update({"market_timestamp": data["quote"].get("timestamp"), "freshness": data["freshness"]})
                elif name == "search_futures_contracts":
                    assert any(c["symbol"].startswith("FU") for c in data["contracts"])
                    record["contracts"] = [c["symbol"] for c in data["contracts"]]
                elif name == "get_futures_bar_dates":
                    assert "20260930" in data["dates"]
                    record["dates_count"] = len(data["dates"])
                else:
                    record["response_keys"] = sorted(data)
                report["checks"].append(record)
                print(json.dumps({k: record[k] for k in ["tool", "arguments", "status"]}, ensure_ascii=False))
    report["all_passed"] = report["stdio_handshake"] and all(c["status"] == "PASS" for c in report["checks"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Evidence saved:", OUT)
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(verify()))
