"""成交明细与大单监控引擎测试。"""

try:
    import test_bootstrap  # noqa: F401  直接发现模式(-s tests)下生效, 切换隔离环境
except ImportError:
    from tests import test_bootstrap  # noqa: F401  以包模式运行时生效

import unittest
from datetime import datetime
from decimal import Decimal

from backend.tape.tape_recorder import TapeRecorder, tape_recorder
from backend.models.market_data import TickData
from backend.storage.redis_client import RedisClient
from decimal import Decimal as _D  # noqa: F401


def make_tick(price, volume, oi, minute=0, second=0):
    return TickData(
        instrument="JM2701",
        exchange="DCE",
        trading_day="20260914",
        timestamp=datetime(2026, 9, 14, 21, minute, second),
        last_price=Decimal(str(price)),
        bid_price_1=Decimal(str(price)) - Decimal("0.5"),
        bid_volume_1=10,
        ask_price_1=Decimal(str(price)) + Decimal("0.5"),
        ask_volume_1=10,
        volume=volume,
        turnover=Decimal(str(volume * price * 60)),
        open_interest=Decimal(str(oi)),
    )


class TestTapeRecorder(unittest.TestCase):
    def setUp(self):
        self.recorder = TapeRecorder()
        self.redis = RedisClient.get_client()
        self.redis.delete("tape:JM2701", "bigorders:JM2701")

    def test_baseline_tick_no_record(self):
        """首个基线快照 (无累计量参照) 不产生成交明细"""
        self.recorder.on_tick(make_tick(1589.0, 10000, 487000, second=0))
        self.assertEqual(self.recorder.get_tape("JM2701"), [])

    def test_delta_volume_and_direction_long_open(self):
        """价涨+增仓 -> 多开; 现手 = 累计量差值"""
        self.recorder.on_tick(make_tick(1589.0, 10000, 487000, minute=0, second=0))
        self.recorder.on_tick(make_tick(1590.0, 10250, 487150, minute=0, second=15))
        tape = self.recorder.get_tape("JM2701")
        self.assertEqual(len(tape), 1)
        rec = tape[0]
        self.assertEqual(rec["volume"], 250)
        self.assertEqual(rec["direction"], "多开")
        self.assertEqual(rec["price"], 1590.0)
        self.assertEqual(rec["oi_change"], 150)
        # 成交额估算 = 现手 × 价格 × 乘数60
        self.assertAlmostEqual(rec["amount"], 250 * 1590.0 * 60, delta=1)

    def test_direction_short_open_and_flat(self):
        """价跌+增仓 -> 空开; 持仓不变 -> 换手"""
        self.recorder.on_tick(make_tick(1590.0, 10000, 487000, minute=1, second=0))
        self.recorder.on_tick(make_tick(1588.0, 10100, 487200, minute=1, second=15))
        self.assertEqual(self.recorder.get_tape("JM2701")[0]["direction"], "空开")
        self.recorder.on_tick(make_tick(1588.0, 10200, 487200, minute=2, second=0))
        self.assertEqual(self.recorder.get_tape("JM2701")[0]["direction"], "换手")

    def test_big_order_threshold(self):
        """现手达到阈值 (默认100手) 记入大单监控; 小单不记"""
        from backend.common.config import cfg
        threshold = cfg.system.big_order_min_volume
        self.recorder.on_tick(make_tick(1589.0, 10000, 487000, minute=3, second=0))
        # 小单
        self.recorder.on_tick(make_tick(1589.5, 10000 + threshold - 1, 487000, minute=3, second=15))
        self.assertEqual(self.recorder.get_big_orders("JM2701"), [])
        # 大单
        self.recorder.on_tick(make_tick(1590.0, 10000 + threshold - 1 + threshold, 487100, minute=3, second=30))
        big = self.recorder.get_big_orders("JM2701")
        self.assertEqual(len(big), 1)
        self.assertEqual(big[0]["volume"], threshold)
        self.assertEqual(big[0]["instrument"], "JM2701")
        self.assertEqual(big[0]["direction"], "多开")

    def test_volume_reset_across_session(self):
        """跨交易日累计量重置 (差值为负) 时重建基线, 不产生负现手"""
        self.recorder.on_tick(make_tick(1589.0, 50000, 487000, minute=4, second=0))
        self.recorder.on_tick(make_tick(1589.0, 100, 487000, minute=5, second=0))  # 重置
        self.assertEqual(self.recorder.get_tape("JM2701"), [])

    def test_global_singleton_available(self):
        self.assertIsNotNone(tape_recorder)

    def test_synthesize_from_bars(self):
        """历史日期分钟级明细: 由 1m K线推算现手与方向"""
        from datetime import datetime as dt
        from backend.storage.mysql_client import SessionLocal
        from backend.models.database import MarketBarModel

        session = SessionLocal()
        session.query(MarketBarModel).filter_by(instrument_id="TESTAPE").delete()
        bars = []
        # 三根 1m: 平 -> 涨+增仓(多开) -> 跌+减仓(多平)
        for i, (hhmm, close, vol, oi_chg) in enumerate([("09:01", 1500, 100, 0), ("09:02", 1510, 200, 50), ("09:03", 1500, 150, -80)]):
            base = dt(2026, 9, 10, 9, i + 1)
            prev_close = 1500 if i == 0 else bars[-1]["close_price"]
            bars.append({
                "instrument_id": "TESTAPE", "trading_day": "20260910", "period": "1m",
                "bar_time": base, "open_price": Decimal(str(prev_close)), "high_price": Decimal(str(close)),
                "low_price": Decimal(str(close)), "close_price": Decimal(str(close)),
                "volume": vol, "turnover": Decimal(str(close * vol * 10)),
                "open_interest": Decimal("50000"), "open_interest_change": Decimal(str(oi_chg)),
            })
        session.add_all([MarketBarModel(**b) for b in bars])
        session.commit()
        session.close()

        items = tape_recorder.synthesize_from_bars("TESTAPE", "20260910")
        self.assertEqual(len(items), 3)
        # 倒序: 最新一根在最上
        self.assertEqual(items[0]["direction"], "多平")
        self.assertEqual(items[1]["direction"], "多开")
        self.assertEqual(items[1]["volume"], 200)
        self.assertEqual(items[2]["direction"], "换手")
        self.assertEqual(items[0]["oi_change"], -80)
        # 历史路径成交额必须用合约乘数 (10), 不能误用最小变动价位 (1)
        self.assertEqual(items[1]["amount"], round(1510 * 200 * 10, 0))

        # 清理
        session = SessionLocal()
        session.query(MarketBarModel).filter_by(instrument_id="TESTAPE").delete()
        session.commit()
        session.close()


if __name__ == "__main__":
    unittest.main()
