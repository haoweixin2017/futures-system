"""CTP 行情断线重连看门狗测试。

不依赖真实 CTP 接口: 通过替身 API 与直接驱动看门狗逻辑验证重连行为。
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import test_bootstrap  # noqa: F401  必须最先导入以完成测试环境隔离

from backend.collector import ctp_collector


class FakeMdApi:
    """替身: 记录 Release / Init 调用次数, 不建立真实网络连接。"""

    instances = []

    def __init__(self):
        self.released = 0
        self.registered = []
        self.fronts = []
        self.inited = 0
        FakeMdApi.instances.append(self)

    def RegisterSpi(self, spi):
        self.spi = spi

    def RegisterFront(self, addr):
        self.fronts.append(addr)

    def Init(self):
        self.inited += 1

    def Release(self):
        self.released += 1


def _make_collector(monkey_patched_api=True):
    collector = ctp_collector.CTPMarketCollector(lambda tick: None)
    collector._flow_path = "."
    return collector


class CTPReconnectTests(unittest.TestCase):
    def setUp(self):
        FakeMdApi.instances = []
        self._patches = [
            patch.object(ctp_collector.mdapi.CThostFtdcMdApi, "CreateFtdcMdApi",
                         side_effect=lambda path: FakeMdApi()),
            patch.object(ctp_collector.CTPMarketCollector, "_is_trading_time",
                         staticmethod(lambda: True)),
            patch.object(ctp_collector, "notifier", create=True),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_connect_creates_instance_and_init(self):
        c = _make_collector()
        c._connect()
        self.assertEqual(len(FakeMdApi.instances), 1)
        self.assertEqual(FakeMdApi.instances[0].inited, 1)
        self.assertIsNotNone(c.spi)

    def test_reconnect_releases_old_instance(self):
        c = _make_collector()
        c._connect()
        old_api = c.api
        c._reconnect()
        self.assertEqual(old_api.released, 1, "重连必须释放旧实例, 避免句柄泄漏")
        self.assertEqual(len(FakeMdApi.instances), 2, "重连应创建新实例")
        self.assertIsNot(c.api, old_api)

    def test_reconnect_survives_connect_failure(self):
        """建连失败不应抛出异常 (否则看门狗线程会死掉)。"""
        c = _make_collector()
        c._connect()
        with patch.object(ctp_collector.mdapi.CThostFtdcMdApi, "CreateFtdcMdApi",
                          side_effect=RuntimeError("模拟建连失败")):
            c._reconnect()  # 不应抛出
        self.assertIsNone(c.api)

    def test_disconnect_resets_flags_and_notifies(self):
        c = _make_collector()
        c._connect()
        spi = c.spi
        spi.is_connected = True
        spi.is_logged_in = True
        with patch.object(ctp_collector.notifier, "notify") as notify:
            spi.OnFrontDisconnected(-3)
        self.assertFalse(spi.is_connected)
        self.assertFalse(spi.is_logged_in)
        notify.assert_called_once()
        self.assertEqual(notify.call_args[0][0], "CTP_MD_DISCONNECTED")

    def test_watchdog_detects_stale_data(self):
        """连接正常但长时间无行情 -> 判定假死并触发重连。"""
        c = _make_collector()
        c._connect()
        c._running = True
        c.spi.is_connected = True
        c.spi.is_logged_in = True
        c.spi.last_tick_at = datetime.now() - timedelta(seconds=ctp_collector.STALE_RECONNECT_SECONDS + 1)

        # 看门狗每次循环先 sleep(5.0) 再检查; 退避重连时另有 sleep(backoff)。
        # 这里在第二次「循环顶部」休眠后停止, 确保重连分支已执行完。
        seen = {"loop": 0}

        def fast_sleep(seconds):
            if seconds >= 5.0:
                seen["loop"] += 1
                if seen["loop"] >= 2:
                    c._running = False

        with patch.object(ctp_collector.time, "sleep", side_effect=fast_sleep), \
             patch.object(c, "_reconnect") as reconnect:
            c._watchdog_loop()
        reconnect.assert_called_once()

    def test_watchdog_does_not_reconnect_when_healthy(self):
        """连接正常且行情新鲜 -> 不应触发重连。"""
        c = _make_collector()
        c._connect()
        c._running = True
        c.spi.is_connected = True
        c.spi.is_logged_in = True
        c.spi.last_tick_at = datetime.now()

        seen = {"loop": 0}

        def fast_sleep(seconds):
            if seconds >= 5.0:
                seen["loop"] += 1
                if seen["loop"] >= 3:
                    c._running = False

        with patch.object(ctp_collector.time, "sleep", side_effect=fast_sleep), \
             patch.object(c, "_reconnect") as reconnect:
            c._watchdog_loop()
        reconnect.assert_not_called()

    def test_watchdog_skips_check_outside_trading_hours(self):
        """休市静默不应被误判为故障。"""
        c = _make_collector()
        c._connect()
        c._running = True
        c.spi.is_connected = False
        c.spi.is_logged_in = False

        seen = {"loop": 0}

        def fast_sleep(seconds):
            if seconds >= 5.0:
                seen["loop"] += 1
                if seen["loop"] >= 3:
                    c._running = False

        with patch.object(ctp_collector.CTPMarketCollector, "_is_trading_time",
                          staticmethod(lambda: False)), \
             patch.object(ctp_collector.time, "sleep", side_effect=fast_sleep), \
             patch.object(c, "_reconnect") as reconnect:
            c._watchdog_loop()
        reconnect.assert_not_called()

    def test_stop_releases_and_halts_watchdog(self):
        c = _make_collector()
        c._connect()
        c._running = True
        api = c.api
        c.stop()
        self.assertFalse(c._running)
        self.assertEqual(api.released, 1)
        self.assertIsNone(c.api)


if __name__ == "__main__":
    unittest.main()
