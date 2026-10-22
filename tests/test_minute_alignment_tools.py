"""清洗工具 `scripts/fix_minute_alignment.py` 的关键判据单测。

覆盖历史数据两类脏口径的检测与清除逻辑:
  1. 幽灵 bar —— bar_time 恰为收盘整点 (15:00/23:00/09:00...) 且不是该合约可交易分钟,
     属收盘静态快照残影, 必须识别并丢弃。
  2. 整体滞后一格 (lag residue) —— DB[t] 存的是源 t-1 的值, 源于早期回填版本
     既未做 -1m 平移又错位累加。本文件钉死其「区间定位」与「按交易时段扩张删除」的行为,
     防止后续改动把正常整点 bar 误删、或漏判滞后区间。
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_bootstrap  # noqa: F401  切到隔离测试库

from backend.common.trading_time import FuturesTradingCalendar
from scripts.fix_minute_alignment import _is_ghost


class TestGhostDetection(unittest.TestCase):
    """幽灵判据必须「合约感知」: 同一整点在不同合约下结论可能相反。"""

    def test_commodity_close_ordinal_is_ghost(self):
        # 商品夜盘 23:00 收盘, 23:00:00 不是可交易分钟 -> 幽灵
        self.assertTrue(_is_ghost(datetime(2026, 9, 11, 23, 0), "FU2611"))
        # 商品日盘 15:00 收盘
        self.assertTrue(_is_ghost(datetime(2026, 9, 11, 15, 0), "SA2701"))

    def test_night_open_is_not_ghost(self):
        # 21:00 是夜盘开盘第一分钟, 可交易 -> 不是幽灵
        self.assertFalse(_is_ghost(datetime(2026, 9, 11, 21, 0), "FU2611"))

    def test_cffex_1300_is_not_ghost(self):
        # 股指 13:00 开盘, 整点但是可交易分钟 -> 不能误删
        self.assertFalse(_is_ghost(datetime(2026, 9, 11, 13, 0), "IF2609"))

    def test_sc_early_morning_boundary(self):
        # SC 夜盘到 02:30 收盘。02:00 是整点且可交易 -> 保留 (不能误删)。
        # 02:30 虽为收盘边界, 但分钟数非 0, 不落入「整点幽灵」判据 (另有区间判据兜底)。
        self.assertFalse(_is_ghost(datetime(2026, 9, 12, 2, 0), "SC2611"))
        # SC 夜盘 01:00 整点可交易 (未到 02:30 收盘) -> 非幽灵
        self.assertFalse(_is_ghost(datetime(2026, 9, 12, 1, 0), "SC2611"))
        # 但 CU 类夜盘 01:00 收盘, 该整点不可交易 -> 幽灵
        self.assertTrue(_is_ghost(datetime(2026, 9, 12, 1, 0), "CU2611"))

    def test_non_boundary_minute_never_ghost(self):
        # 非整点分钟一律不是幽灵, 与合约无关
        for sym in ("FU2611", "IF2609", "SC2611"):
            self.assertFalse(_is_ghost(datetime(2026, 9, 11, 10, 14), sym))

    def test_break_boundary_1015_is_not_trading(self):
        # 10:15~10:30 小休: 10:15 不可交易 (本地 -1m 口径下最后一分钟是 10:14)
        self.assertFalse(
            FuturesTradingCalendar.is_trading_time_for(datetime(2026, 9, 11, 10, 15), "FU2611")
        )
        self.assertTrue(
            FuturesTradingCalendar.is_trading_time_for(datetime(2026, 9, 11, 10, 14), "FU2611")
        )


class TestLagRangeExpansion(unittest.TestCase):
    """删除区间必须按交易时段向前/向后扩张, 不能只切中间一段留下半截夜盘。"""

    def test_purge_range_expands_to_session_bounds(self):
        from scripts.fix_minute_alignment import purge_range
        # 用假 session 捕获实际过滤条件 (不触库)
        captured = {}

        class FakeQuery:
            def filter(self, *conds):
                captured["conds"] = conds
                return self

            def all(self):
                return []

        class FakeSession:
            def query(self, model):
                return FakeQuery()

            def commit(self):
                captured["committed"] = True

        start = datetime(2026, 9, 11, 10, 0)
        end = datetime(2026, 9, 11, 11, 0)
        n = purge_range(FakeSession(), "FU2611", start, end)
        self.assertEqual(n, 0)
        self.assertTrue(captured.get("committed"))

        # 下界应扩到 start 当日 00:00 - 3h = 前一日 21:00 (含夜盘起点)
        lo, hi = self._bound_values(captured["conds"])
        self.assertEqual(lo, datetime(2026, 9, 10, 21, 0))
        # 上界扩到 end 当日 23:59:59 + 16h = 次日 15:59:59 (覆盖次日收盘)
        self.assertEqual(hi, datetime(2026, 9, 12, 15, 59, 59))

    @staticmethod
    def _bound_values(conds):
        lo = hi = None
        for c in conds:
            try:
                text = str(c)
            except Exception:
                continue
            if "bar_time >=" in text:
                lo = getattr(c, "right", None)
                lo = getattr(lo, "value", lo)
            elif "bar_time <=" in text:
                hi = getattr(c, "right", None)
                hi = getattr(hi, "value", hi)
        return lo, hi


if __name__ == "__main__":
    unittest.main(verbosity=2)
