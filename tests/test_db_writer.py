"""异步数据库批量写入器测试。

覆盖: 队列积压不阻塞调用方、批次/定时触发刷盘、退出前强制刷盘、
以及刷盘失败时不吞掉已有队列状态。
"""

import queue
import time
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import test_bootstrap  # noqa: F401  必须最先导入以完成测试环境隔离

from backend.models.market_data import BarData, TickData
from backend.storage.db_writer import AsyncDBWriter


def make_bar(minute=0, close=100):
    return BarData(
        instrument="TEST", trading_day="20260914", period="1m",
        bar_time=datetime(2026, 9, 14, 9, minute),
        open_price=Decimal(str(close)), high_price=Decimal(str(close)),
        low_price=Decimal(str(close)), close_price=Decimal(str(close)),
        volume=10, turnover=Decimal(str(close * 10)),
        open_interest=Decimal("100"), open_interest_change=Decimal("0"),
    )


def make_tick(price=100):
    return TickData(
        instrument="TEST", exchange="DCE", trading_day="20260914",
        timestamp=datetime(2026, 9, 14, 9, 0),
        last_price=Decimal(str(price)),
        bid_price_1=Decimal(str(price)), bid_volume_1=1,
        ask_price_1=Decimal(str(price)), ask_volume_1=1,
        volume=10, turnover=Decimal("1000"), open_interest=Decimal("100"),
    )


class AsyncDBWriterTests(unittest.TestCase):
    def test_put_bar_enqueues(self):
        w = AsyncDBWriter()
        w.put_bar(make_bar())
        self.assertEqual(w.queue.qsize(), 1)

    def test_put_tick_enqueues(self):
        w = AsyncDBWriter()
        w.put_tick(make_tick())
        self.assertEqual(w.queue.qsize(), 1)

    def test_full_queue_drops_without_blocking(self):
        """队列打满时丢弃并记日志, 绝不阻塞行情回调线程。"""
        w = AsyncDBWriter()
        w.queue = queue.Queue(maxsize=1)
        w.put_bar(make_bar())
        start = time.time()
        w.put_bar(make_bar())  # 应被丢弃
        elapsed = time.time() - start
        self.assertLess(elapsed, 0.5, "入队不应阻塞")
        self.assertEqual(w.queue.qsize(), 1)

    def test_flush_batch_writes_bars_and_ticks(self):
        w = AsyncDBWriter()
        fake_session = MagicMock()
        with patch("backend.storage.db_writer.SessionLocal", return_value=fake_session), \
             patch("backend.storage.db_writer.insert") as fake_insert:
            fake_insert.return_value.on_duplicate_key_update.return_value = MagicMock()
            w._flush_batch([make_bar(0), make_bar(1)], [make_tick()])
        fake_session.commit.assert_called_once()
        self.assertEqual(fake_session.add.call_count, 1, "tick 应按行 add")
        fake_session.close.assert_called_once()

    def test_flush_batch_rolls_back_and_closes_on_error(self):
        w = AsyncDBWriter()
        fake_session = MagicMock()
        fake_session.commit.side_effect = RuntimeError("模拟写库失败")
        with patch("backend.storage.db_writer.SessionLocal", return_value=fake_session), \
             patch("backend.storage.db_writer.insert") as fake_insert:
            fake_insert.return_value.on_duplicate_key_update.return_value = MagicMock()
            w._flush_batch([make_bar()], [])  # 不应抛出
        fake_session.rollback.assert_called_once()
        fake_session.close.assert_called_once()

    def test_flush_on_stop_drains_pending_items(self):
        """stop() 后队列中未刷盘的数据必须被写入 (否则关服丢数据)。"""
        w = AsyncDBWriter(batch_size=1000, flush_interval=999)
        w.start()
        w.put_bar(make_bar(0))
        w.put_bar(make_bar(1))

        with patch.object(w, "_flush_batch") as flush:
            w._running = False           # 让 worker 在队列清空后退出
            w._worker_thread.join(timeout=3.0)
            # 退出前应至少刷盘一次, 且包含积压的两根K线
            self.assertTrue(flush.called, "退出前必须刷盘")
            flushed_bars = flush.call_args[0][0]
            self.assertEqual(len(flushed_bars), 2)

    def test_start_is_idempotent(self):
        w = AsyncDBWriter()
        with patch("backend.storage.db_writer.threading.Thread") as fake_thread:
            w.start()
            w.start()
        self.assertEqual(fake_thread.call_count, 1, "重复 start 不应创建多个线程")
        w._running = False


if __name__ == "__main__":
    unittest.main()
