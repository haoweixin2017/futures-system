"""CTP 只读连接诊断：TCP、行情登录、交易认证/登录。

不启动交易执行器，不订阅行情，不确认结算，不查询资金，不报撤单。
凭据仅从本机配置读取，不接受密码命令行参数；缺授权时绝不绕过认证。
用法: .venv/Scripts/python.exe scripts/ctp_connection_probe.py --profile simnow
"""
import argparse
import json
import os
import socket
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def probe_tcp(address):
    parsed = urlparse(address)
    if parsed.scheme != "tcp" or not parsed.hostname or not parsed.port:
        return {"ok": False, "error": "前置地址格式错误"}
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=5):
            return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def run_session(config, kind, timeout):
    from openctp_ctp import thostmduserapi as mdapi, thosttraderapi as tdapi
    module = mdapi if kind == "md" else tdapi
    base = module.CThostFtdcMdSpi if kind == "md" else module.CThostFtdcTraderSpi
    factory = module.CThostFtdcMdApi if kind == "md" else module.CThostFtdcTraderApi
    done = threading.Event()
    result = {"connected": False, "authenticated": False, "logged_in": False, "stage": "connecting"}

    def sanitize(message):
        text = str(message)
        for secret in (config.password, config.auth_code, config.user_id):
            if secret:
                text = text.replace(secret, "<redacted>")
        return text

    class Handler(base):
        def __init__(self, api):
            super().__init__()
            self.api = api
            self.attempted = False

        def fail(self, stage, code, message):
            result.update(stage=stage, error_id=code, error=sanitize(message))
            done.set()

        def login(self):
            result["stage"] = "login"
            req = module.CThostFtdcReqUserLoginField()
            req.BrokerID, req.UserID, req.Password = config.broker_id, config.user_id, config.password
            code = self.api.ReqUserLogin(req, 2)
            if code:
                self.fail("login_request_failed", code, "登录请求发送失败")

        def OnFrontConnected(self):
            result["connected"] = True
            if done.is_set() or self.attempted:
                return
            self.attempted = True
            if kind == "md":
                self.login()
                return
            if not config.app_id or not config.auth_code:
                self.fail("authorization_missing", None, "缺少券商分配的 AppID/AuthCode，未发起认证或交易登录")
                return
            result["stage"] = "authenticate"
            req = tdapi.CThostFtdcReqAuthenticateField()
            req.BrokerID, req.UserID = config.broker_id, config.user_id
            req.AppID, req.AuthCode = config.app_id, config.auth_code
            code = self.api.ReqAuthenticate(req, 1)
            if code:
                self.fail("authenticate_request_failed", code, "认证请求发送失败")

        def OnFrontDisconnected(self, reason):
            result["connected"] = False
            if not done.is_set():
                self.fail("disconnected", reason, "前置断开")

        def OnRspAuthenticate(self, row, info, request_id, last):
            if done.is_set():
                return
            if info and info.ErrorID:
                self.fail("authenticate_failed", info.ErrorID, info.ErrorMsg)
                return
            result["authenticated"] = True
            self.login()

        def OnRspUserLogin(self, row, info, request_id, last):
            if done.is_set():
                return
            if info and info.ErrorID:
                self.fail("login_failed", info.ErrorID, info.ErrorMsg)
                return
            if row is None:
                self.fail("login_failed", None, "登录响应缺失")
                return
            result.update(logged_in=True, stage="login_success", trading_day=row.TradingDay)
            done.set()

        def OnRspError(self, info, request_id, last):
            if info and info.ErrorID and not done.is_set():
                self.fail("server_error", info.ErrorID, info.ErrorMsg)

    # 当前用户临时目录用于独立流文件，不复用正式执行器流目录。
    with tempfile.TemporaryDirectory(prefix="ctp_readonly_") as flow:
        flow.encode("ascii")  # 原生库不接受中文流目录；不静默改用共享目录。
        api = factory.CreateFtdcMdApi(flow + os.sep) if kind == "md" else factory.CreateFtdcTraderApi(flow + os.sep)
        handler = Handler(api)
        try:
            api.RegisterSpi(handler)
            api.RegisterFront(config.md_address if kind == "md" else config.td_address)
            api.Init()
            if not done.wait(timeout):
                result.update(stage="timeout", error="未在限定时间内收到完整响应")
                done.set()
        finally:
            api.Release()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("local", "simnow", "prod"), default="simnow")
    parser.add_argument("--timeout", type=float, default=12)
    args = parser.parse_args()
    os.environ["FUTURES_PROFILE"] = args.profile
    from backend.common.config import cfg
    from openctp_ctp import thosttraderapi as tdapi
    report = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "profile": cfg.system.profile,
        "account": cfg.ctp.user_id[:3] + "****" + cfg.ctp.user_id[-2:],
        "api_version": tdapi.CThostFtdcTraderApi.GetApiVersion(),
        "broker_id": cfg.ctp.broker_id,
        "no_orders_no_settlement": True,
        "live_enabled": cfg.risk.live_enabled,
    }
    if not cfg.ctp.user_id or not cfg.ctp.password:
        report["error"] = "账号或密码未配置，未发起登录"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    for kind, address in (("td", cfg.ctp.td_address), ("md", cfg.ctp.md_address)):
        item = {"address": address, "tcp": probe_tcp(address)}
        if item["tcp"]["ok"]:
            item["ctp"] = run_session(cfg.ctp, kind, args.timeout)
        report[kind] = item
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(report[k].get("ctp", {}).get("logged_in") for k in ("td", "md")) else 1


if __name__ == "__main__":
    sys.exit(main())
