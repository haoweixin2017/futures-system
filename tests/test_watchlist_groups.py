"""订阅分组 API 测试。"""

try:
    import test_bootstrap  # noqa: F401  直接发现模式(-s tests)下生效, 切换隔离环境
except ImportError:
    from tests import test_bootstrap  # noqa: F401  以包模式运行时生效

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api.routes import app
from backend.common.security import hash_password

client = TestClient(app)


class TestWatchlistGroups(unittest.TestCase):
    def setUp(self):
        client.cookies.clear()
        users = {
            "sim": {
                "role": "sim",
                "display_name": "sim",
                "password_hash": hash_password("test-only-strong-password"),
            }
        }
        self.users_patch = patch("backend.api.auth.load_users", return_value=users)
        self.users_patch.start()
        self.addCleanup(self.users_patch.stop)
        self._login()

    def _login(self):
        response = client.post("/api/v1/auth/login", json={
            "username": "sim",
            "password": "test-only-strong-password",
        })
        self.assertEqual(response.status_code, 200)

    def test_anonymous_watchlist_writes_are_blocked(self):
        client.cookies.clear()
        requests = [
            client.post("/api/v1/watchlist/rename", json={"symbol": "JM2701", "name": "焦煤"}),
            client.post("/api/v1/watchlist/group", json={"name": "黑色系"}),
            client.post("/api/v1/watchlist/assign-groups", json={"symbol": "JM2701", "groups": ["黑色系"]}),
            client.post("/api/v1/watchlist/assign", json={"symbol": "JM2701", "group": "黑色系"}),
            client.post("/api/v1/watchlist/group/rename", json={"old": "黑色系", "new": "商品"}),
            client.delete("/api/v1/watchlist/group/黑色系"),
            client.post("/api/v1/watchlist/reorder", json={"symbols": ["JM2701"]}),
            client.post("/api/v1/watchlist/group/reorder", json={"groups": ["默认"]}),
            client.post("/api/v1/watchlist", json={"symbol": "JM2701"}),
            client.delete("/api/v1/watchlist/JM2701"),
        ]
        self.assertTrue(all(response.status_code == 401 for response in requests))
        self._login()

    def test_subscription_requests_full_history_for_new_and_existing_contract(self):
        from unittest.mock import Mock
        def immediate_thread(*args, **kwargs):
            thread = Mock()
            thread.start.side_effect = kwargs["target"]
            return thread
        with patch("backend.api.routes.threading.Thread", side_effect=immediate_thread), \
                patch("backend.api.routes.history_backfill.ensure_symbol_backfilled") as backfill, \
                patch("backend.api.routes.cfg.system.backfill_on_subscribe", True):
            for symbol in ("FU2611", "FU2611", "FU0"):
                response = client.post("/api/v1/watchlist", json={"symbol": symbol})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["backfill"]["triggered"])
                backfill.assert_called_with(symbol, full_history=True)
            self.assertEqual(backfill.call_count, 3)

    def test_group_create_assign_and_fallback(self):
        # 订阅两个合约
        client.post("/api/v1/watchlist", json={"symbol": "JM2701"})
        client.post("/api/v1/watchlist", json={"symbol": "SA2701"})

        # 新建分组
        res = client.post("/api/v1/watchlist/group", json={"name": "黑色系"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("黑色系", res.json()["groups"])

        # 分配 JM2701 到黑色系
        res = client.post("/api/v1/watchlist/assign", json={"symbol": "JM2701", "group": "黑色系"})
        self.assertEqual(res.status_code, 200)

        items = client.get("/api/v1/watchlist").json()["watchlist"]
        g = {x["symbol"]: x["group"] for x in items}
        self.assertEqual(g["JM2701"], "黑色系")
        self.assertEqual(g["SA2701"], "默认")

        # 分配到不存在的分组 -> 隐式创建
        client.post("/api/v1/watchlist/assign", json={"symbol": "SA2701", "group": "农产品"})
        items = client.get("/api/v1/watchlist").json()["watchlist"]
        g = {x["symbol"]: x["group"] for x in items}
        self.assertEqual(g["SA2701"], "农产品")

        # 删除黑色系分组 -> 组内合约回到默认
        res = client.delete("/api/v1/watchlist/group/黑色系")
        self.assertEqual(res.status_code, 200)
        items = client.get("/api/v1/watchlist").json()["watchlist"]
        g = {x["symbol"]: x["group"] for x in items}
        self.assertEqual(g["JM2701"], "默认")

    def test_group_validation(self):
        res = client.post("/api/v1/watchlist/group", json={"name": ""})
        self.assertEqual(res.status_code, 400)
        res = client.post("/api/v1/watchlist/group", json={"name": "<img/onerror=x>"})
        self.assertEqual(res.status_code, 400)
        client.post("/api/v1/watchlist", json={"symbol": "JM2701"})
        res = client.post("/api/v1/watchlist/rename", json={
            "symbol": "JM2701", "name": "<img src=x onerror=x>"
        })
        self.assertEqual(res.status_code, 400)
        res = client.delete("/api/v1/watchlist/group/默认")
        self.assertEqual(res.status_code, 400)
        res = client.delete("/api/v1/watchlist/group/不存在")
        self.assertEqual(res.status_code, 404)

    def test_assign_requires_watched_symbol(self):
        res = client.post("/api/v1/watchlist/assign", json={"symbol": "ZZ9999", "group": "黑色系"})
        self.assertEqual(res.status_code, 404)

    def test_reorder_symbols_and_groups(self):
        # 订阅三个合约 (当前列表可能含其他成员, 断言与成员集无关)
        for s in ["FU2610", "JM2701", "SA2701"]:
            client.post("/api/v1/watchlist", json={"symbol": s})
        members = [x["symbol"] for x in client.get("/api/v1/watchlist").json()["watchlist"]]
        self.assertGreaterEqual(len(members), 3)

        # 拖动排序持久化: 全体成员反序
        target = list(reversed(members))
        client.post("/api/v1/watchlist/reorder", json={"symbols": target})
        items = client.get("/api/v1/watchlist").json()["watchlist"]
        self.assertEqual([x["symbol"] for x in items], target)

        # reorder 中的无效符号被过滤, 缺失成员自动补齐到末尾
        client.post("/api/v1/watchlist/reorder", json={"symbols": [target[0], "ZZ9999"]})
        items = client.get("/api/v1/watchlist").json()["watchlist"]
        self.assertEqual(items[0]["symbol"], target[0])
        self.assertEqual(len(items), len(members))

        # 分组顺序持久化
        client.post("/api/v1/watchlist/group", json={"name": "G1"})
        client.post("/api/v1/watchlist/group", json={"name": "G2"})
        client.post("/api/v1/watchlist/group/reorder", json={"groups": ["G2", "G1"]})
        self.assertEqual(client.get("/api/v1/watchlist").json()["groups"], ["G2", "G1"])

    def tearDown(self):
        # 清理测试分组与测试订阅
        client.delete("/api/v1/watchlist/group/黑色系")
        client.delete("/api/v1/watchlist/group/农产品")
        client.delete("/api/v1/watchlist/group/G1")
        client.delete("/api/v1/watchlist/group/G2")
        for s in ["JM2701", "SA2701", "FU2610"]:
            client.delete("/api/v1/watchlist/" + s)
        # 恢复默认订阅
        for s in ["JM2701", "FU2610", "BU2610"]:
            client.post("/api/v1/watchlist", json={"symbol": s})


if __name__ == "__main__":
    unittest.main()
