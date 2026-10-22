"""WebSocket 连接管理器测试。

重点覆盖: 广播到多个客户端、发送失败自动摘除死连接、以及同步线程调用
broadcast_threadsafe 时不会阻塞或抛入调用方线程。
"""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

import test_bootstrap  # noqa: F401  必须最先导入以完成测试环境隔离

from backend.websocket.manager import ConnectionManager


class FakeWebSocket:
    def __init__(self, fail=False):
        self.accepted = False
        self.sent = []
        self.fail = fail

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        if self.fail:
            raise RuntimeError("模拟发送失败 (连接已断开)")
        self.sent.append(message)


class ConnectionManagerTests(unittest.TestCase):
    def test_connect_accepts_and_tracks(self):
        mgr = ConnectionManager()
        ws = FakeWebSocket()
        asyncio.run(mgr.connect(ws))
        self.assertTrue(ws.accepted)
        self.assertIn(ws, mgr.active_connections)

    def test_disconnect_is_idempotent(self):
        """重复断开不应报错 (连接可能已被广播清理)。"""
        mgr = ConnectionManager()
        ws = FakeWebSocket()
        asyncio.run(mgr.connect(ws))
        mgr.disconnect(ws)
        mgr.disconnect(ws)  # 不应抛异常
        self.assertEqual(len(mgr.active_connections), 0)

    def test_broadcast_delivers_to_all_clients(self):
        mgr = ConnectionManager()
        clients = [FakeWebSocket() for _ in range(3)]
        for ws in clients:
            asyncio.run(mgr.connect(ws))

        asyncio.run(mgr.broadcast({"type": "tick", "price": 100}))
        for ws in clients:
            self.assertEqual(len(ws.sent), 1)
            self.assertEqual(ws.sent[0]["price"], 100)

    def test_market_broadcast_isolated_by_feed(self):
        mgr = ConnectionManager()
        sina, ctp = FakeWebSocket(), FakeWebSocket()
        asyncio.run(mgr.connect(sina, feed="sina"))
        asyncio.run(mgr.connect(ctp, feed="ctp"))

        asyncio.run(mgr.broadcast({"event": "TICK", "feed": "ctp", "data": {"last_price": "202"}}))
        self.assertEqual(sina.sent, [])
        self.assertEqual(ctp.sent[0]["data"]["last_price"], "202")

    def test_broadcast_removes_dead_sockets(self):
        """单个客户端发送失败应被摘除, 且不影响其他客户端。"""
        mgr = ConnectionManager()
        alive = FakeWebSocket()
        dead = FakeWebSocket(fail=True)
        asyncio.run(mgr.connect(alive))
        asyncio.run(mgr.connect(dead))

        asyncio.run(mgr.broadcast({"type": "tick"}))

        self.assertEqual(len(alive.sent), 1, "健康客户端应正常收到消息")
        self.assertNotIn(dead, mgr.active_connections, "发送失败的连接应被摘除")
        self.assertIn(alive, mgr.active_connections)

    def test_broadcast_with_no_clients_is_noop(self):
        mgr = ConnectionManager()
        asyncio.run(mgr.broadcast({"type": "tick"}))  # 不应抛异常

    def test_threadsafe_broadcast_without_loop_does_not_raise(self):
        """loop 未设置时 (启动早期) 静默跳过, 不应抛异常。"""
        mgr = ConnectionManager()
        mgr.broadcast_threadsafe({"type": "tick"})  # 不应抛异常

    def test_threadsafe_broadcast_schedules_on_loop(self):
        """已设置 loop 时应把广播投递到该 loop 上执行。"""
        mgr = ConnectionManager()
        ws = FakeWebSocket()

        async def scenario():
            loop = asyncio.get_running_loop()
            mgr.set_loop(loop)
            await mgr.connect(ws)
            mgr.broadcast_threadsafe({"type": "tick", "v": 7})
            # 让投递的协程有机会执行
            await asyncio.sleep(0.05)
            return ws.sent

        sent = asyncio.run(scenario())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["v"], 7)


if __name__ == "__main__":
    unittest.main()
