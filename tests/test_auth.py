"""登录认证与角色权限测试 (不依赖外部服务)。"""

try:
    import test_bootstrap  # noqa: F401  直接发现模式(-s tests)下生效, 切换隔离环境
except ImportError:
    from tests import test_bootstrap  # noqa: F401  以包模式运行时生效

import unittest
import os
import subprocess
import sys
from unittest.mock import patch
from backend.common.security import hash_password

from fastapi.testclient import TestClient

from backend.api.auth import get_current_user, _login_failures
from backend.api.routes import app
from backend.common.config import cfg
from backend.storage.redis_client import RedisClient
from scripts.create_user import validate_password

client = TestClient(app)


class TestAuth(unittest.TestCase):
    def setUp(self):
        client.cookies.clear()
        _login_failures.clear()
        old_feed = cfg.system.feed
        cfg.system.feed = "ctp"
        self.addCleanup(setattr, cfg.system, "feed", old_feed)
        users = {name: {"role": name, "display_name": name, "password_hash": hash_password("test-only-strong-password")} for name in ("sim", "live")}
        self.users_patch = patch("backend.api.auth.load_users", return_value=users)
        self.users_patch.start()
        self.addCleanup(self.users_patch.stop)

    def test_login_wrong_password(self):
        res = client.post("/api/v1/auth/login", json={"username": "sim", "password": "wrong"})
        self.assertEqual(res.status_code, 401)

    def test_local_startup_import_does_not_load_ctp_native_module(self):
        env = os.environ.copy()
        env["FUTURES_PROFILE"] = "local"
        result = subprocess.run(
            [sys.executable, "-c", (
                "import sys; import backend.main; "
                "assert 'openctp_ctp' not in sys.modules"
            )],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_user_tool_rejects_weak_passwords(self):
        for password in ("123456", "password", "short"):
            with self.assertRaises(ValueError):
                validate_password(password)
        self.assertEqual(validate_password("local-only-strong-password"), "local-only-strong-password")

    def test_repeated_login_failures_are_rate_limited_and_success_clears_counter(self):
        for _ in range(5):
            res = client.post("/api/v1/auth/login", json={"username": "sim", "password": "wrong"})
            self.assertEqual(res.status_code, 401)
        blocked = client.post("/api/v1/auth/login", json={
            "username": "sim", "password": "test-only-strong-password"
        })
        self.assertEqual(blocked.status_code, 429)

        _login_failures.clear()
        ok = client.post("/api/v1/auth/login", json={
            "username": "sim", "password": "test-only-strong-password"
        })
        self.assertEqual(ok.status_code, 200)
        self.assertFalse(_login_failures)

    def test_login_entry_role_mismatch(self):
        """登录入口与账号角色不匹配时拒绝: 模拟账号走实盘入口 / 正式账号走模拟入口"""
        res = client.post("/api/v1/auth/login", json={
            "username": "sim", "password": "test-only-strong-password", "expected_role": "live"
        })
        self.assertEqual(res.status_code, 403)
        self.assertIn("模拟用户", res.json()["detail"])

        res = client.post("/api/v1/auth/login", json={
            "username": "live", "password": "test-only-strong-password", "expected_role": "sim"
        })
        self.assertEqual(res.status_code, 403)
        self.assertIn("正式用户", res.json()["detail"])

        # 入口匹配时正常登录
        res = client.post("/api/v1/auth/login", json={
            "username": "sim", "password": "test-only-strong-password", "expected_role": "sim"
        })
        self.assertEqual(res.status_code, 200)
        client.post("/api/v1/auth/logout")
        res = client.post("/api/v1/auth/login", json={
            "username": "live", "password": "test-only-strong-password", "expected_role": "live"
        })
        self.assertEqual(res.status_code, 200)
        client.post("/api/v1/auth/logout")

    def test_login_unknown_user(self):
        res = client.post("/api/v1/auth/login", json={"username": "ghost", "password": "x"})
        self.assertEqual(res.status_code, 401)

    def test_anonymous_trade_blocked(self):
        res = client.get("/api/v1/trade/account")
        self.assertEqual(res.status_code, 401)
        res = client.post("/api/v1/trade/order", json={
            "instrument": "JM2701", "direction": "BUY", "offset": "OPEN", "price": "1500", "volume": 1
        })
        self.assertEqual(res.status_code, 401)

    def test_sim_user_cannot_access_live(self):
        res = client.post("/api/v1/auth/login", json={"username": "sim", "password": "test-only-strong-password"})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["user"]["can_live_trade"])

        me = client.get("/api/v1/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["role"], "sim")

        # 模拟用户访问实盘接口 -> 403
        res = client.get("/api/v1/trade/live/status")
        self.assertEqual(res.status_code, 403)
        res = client.post("/api/v1/trade/live/order", json={
            "instrument": "JM2701", "direction": "BUY", "offset": "OPEN", "price": "1500", "volume": 1
        })
        self.assertEqual(res.status_code, 403)

        # 模拟用户可以正常使用模拟交易接口
        res = client.get("/api/v1/trade/account")
        self.assertEqual(res.status_code, 200)
        client.post("/api/v1/auth/logout")

    def test_live_user_can_access_live_and_rejected_when_ctp_not_ready(self):
        res = client.post("/api/v1/auth/login", json={"username": "live", "password": "test-only-strong-password"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["user"]["can_live_trade"])

        status = client.get("/api/v1/trade/live/status")
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["ready"])

        # CTP 未就绪时实盘报单被网关拒绝 (绝不误发), 而非 403
        res = client.post("/api/v1/trade/live/order", json={
            "instrument": "JM2701", "direction": "BUY", "offset": "OPEN", "price": "1500", "volume": 1
        })
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "rejected")
        self.assertIn("未连接", body["order"]["reject_reason"])

        # 正式用户也可以使用模拟交易
        res = client.get("/api/v1/trade/account")
        self.assertEqual(res.status_code, 200)
        client.post("/api/v1/auth/logout")

    def test_logout_clears_session(self):
        client.post("/api/v1/auth/login", json={"username": "sim", "password": "test-only-strong-password"})
        self.assertEqual(client.get("/api/v1/auth/me").status_code, 200)
        client.post("/api/v1/auth/logout")
        self.assertEqual(client.get("/api/v1/auth/me").status_code, 401)

    def test_market_data_source_follows_login_role(self):
        """模拟用户只能读新浪缓存，正式用户只能读 CTP 缓存。"""
        redis_client = RedisClient.get_client()
        redis_client.hset("quote:sina:ROLETEST", mapping={"last_price": "101", "timestamp": "2026-09-15T09:00:00"})
        redis_client.hset("quote:ctp:ROLETEST", mapping={"last_price": "202", "timestamp": "2026-09-15T09:00:01"})

        client.post("/api/v1/auth/login", json={"username": "sim", "password": "test-only-strong-password"})
        paper = client.get("/api/v1/quotes/ROLETEST")
        self.assertEqual((paper.json()["feed"], paper.json()["last_price"]), ("sina", "101"))

        client.post("/api/v1/auth/logout")
        client.post("/api/v1/auth/login", json={"username": "live", "password": "test-only-strong-password"})
        live = client.get("/api/v1/quotes/ROLETEST")
        self.assertEqual((live.json()["feed"], live.json()["last_price"]), ("ctp", "202"))

    def test_tampered_cookie_rejected(self):
        client.cookies.set("cfs_session", "abc.def")
        res = client.get("/api/v1/auth/me")
        self.assertEqual(res.status_code, 401)
        self.assertIsNone(get_current_user.__wrapped__ if hasattr(get_current_user, "__wrapped__") else None)
        client.cookies.clear()

    def test_page_routes_role_separation(self):
        # 未登录: 所有交易页跳登录页
        res = client.get("/", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/login")
        res = client.get("/paper", follow_redirects=False)
        self.assertEqual(res.headers["location"], "/login")
        res = client.get("/live", follow_redirects=False)
        self.assertEqual(res.headers["location"], "/login")
        # 登录页本身可访问
        self.assertEqual(client.get("/login").status_code, 200)
        live_login = client.get("/live-login")
        self.assertEqual(live_login.status_code, 200)
        self.assertIn("实盘交易账号登录", live_login.text)

        # 模拟用户: / 与 /paper 进模拟页, /live 弹回模拟页
        client.post("/api/v1/auth/login", json={"username": "sim", "password": "test-only-strong-password"})
        res = client.get("/", follow_redirects=False)
        self.assertEqual(res.headers["location"], "/paper")
        self.assertEqual(client.get("/paper").status_code, 200)
        res = client.get("/live", follow_redirects=False)
        self.assertEqual(res.headers["location"], "/paper")
        client.post("/api/v1/auth/logout")

        # 正式用户: / 直进实盘页, 模拟页也可访问
        client.post("/api/v1/auth/login", json={"username": "live", "password": "test-only-strong-password"})
        res = client.get("/", follow_redirects=False)
        self.assertEqual(res.headers["location"], "/live")
        live_page = client.get("/live")
        self.assertEqual(live_page.status_code, 200)
        self.assertIn("搜索全市场合约", live_page.text)
        # 交易面板标题由拆分后的 JS 模块运行时写入, 校验 /js 模块路由可访问且含实盘文案
        core_js = client.get("/js/01-core.js")
        self.assertEqual(core_js.status_code, 200)
        self.assertIn("实盘账户与闪电交易", core_js.text)
        self.assertEqual(client.get("/js/../secret.js").status_code, 404)
        self.assertEqual(client.get("/paper").status_code, 200)
        # 已登录访问登录页按角色回交易页
        res = client.get("/login", follow_redirects=False)
        self.assertEqual(res.headers["location"], "/live")
        client.post("/api/v1/auth/logout")

    def test_live_mode_disabled_blocks_login_api_pages_and_old_session(self):
        # 先在 CTP 模式建立正式用户会话，再切回仅模拟模式，覆盖旧 Cookie 场景。
        res = client.post("/api/v1/auth/login", json={
            "username": "live", "password": "test-only-strong-password", "expected_role": "live"
        })
        self.assertEqual(res.status_code, 200)

        cfg.system.feed = "sina"
        me = client.get("/api/v1/auth/me")
        self.assertEqual(me.status_code, 401)
        self.assertEqual(client.get("/api/v1/trade/live/status").status_code, 503)
        self.assertEqual(client.get("/live").status_code, 503)
        self.assertEqual(client.get("/live-login").status_code, 503)
        self.assertEqual(client.get("/paper", follow_redirects=False).headers["location"], "/login")

        redis_client = RedisClient.get_client()
        redis_client.hset("quote:sina:DISABLEDTEST", mapping={"last_price": "101"})
        redis_client.hset("quote:ctp:DISABLEDTEST", mapping={"last_price": "202"})
        quote = client.get("/api/v1/quotes/DISABLEDTEST")
        self.assertEqual((quote.json()["feed"], quote.json()["last_price"]), ("sina", "101"))

        client.post("/api/v1/auth/logout")
        blocked = client.post("/api/v1/auth/login", json={
            "username": "live", "password": "test-only-strong-password", "expected_role": "live"
        })
        self.assertEqual(blocked.status_code, 503)

        health = client.get("/api/v1/health").json()
        self.assertEqual(health["feed"], "sina")
        self.assertEqual(health["feeds"], {"sim": "sina"})


if __name__ == "__main__":
    unittest.main()
