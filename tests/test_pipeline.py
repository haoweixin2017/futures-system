try:
    import test_bootstrap  # noqa: F401  直接发现模式(-s tests)下生效, 切换隔离环境
except ImportError:
    from tests import test_bootstrap  # noqa: F401  以包模式运行时生效

import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from backend.models.market_data import TickData
from backend.bar_engine.bar_engine import MultiPeriodBarEngine
from backend.common.trading_time import FuturesTradingCalendar
from backend.signal_engine.signal_engine import SignalEngine
from backend.storage.redis_client import RedisClient
from backend.storage.mysql_client import SessionLocal, init_db
from backend.models.database import MarketBarModel, SignalRecordModel

class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 隔离环境已由 tests/__init__.py 统一切换 (MySQL 测试库 + Redis DB1)
        init_db()
        cls.redis = RedisClient.get_client()
        cls.redis.flushdb()

    @classmethod
    def tearDownClass(cls):
        cls.redis.flushdb()

    def test_tick_to_bars_and_signal(self):
        engine = MultiPeriodBarEngine()
        sig_engine = SignalEngine()

        base_time = datetime(2026, 9, 10, 9, 0, 0)
        instrument = "JM2701"

        # 模拟生成 25 根 15 分钟 K 线所需要的 1 分钟 Ticks
        # 前 20 根 15m (300 个交易分钟): 震荡在 1200 ~ 1250, 成交量平缓
        # 第 21 根 15m: 强势突破到 1285，成交量放大, 持仓量增加
        base_price = Decimal("1200.0")
        total_vol = 1000
        total_turnover = Decimal("12000000.0")
        total_oi = Decimal("200000.0")

        # 按"真实交易分钟"推进: 引擎会丢弃休市静默快照 (10:15~10:30 小休 /
        # 11:30~13:30 午休), 若按墙上时钟凭空跨越休市, 产出的 K 线会严重偏少。
        trading_minutes = []
        cursor = base_time
        while len(trading_minutes) < 316:
            if FuturesTradingCalendar.is_trading_time(cursor):
                trading_minutes.append(cursor)
            cursor += timedelta(minutes=1)

        for m, cur_time in enumerate(trading_minutes):
            if m < 300:
                # 震荡期
                price = base_price + Decimal(str(m % 30))
                vol_inc = 100
                oi_inc = Decimal("10")
            else:
                # 突破期 (第 21 根 15m)
                price = Decimal("1285.0")
                vol_inc = 500  # 大幅放量
                oi_inc = Decimal("500") # 持仓大增

            total_vol += vol_inc
            total_turnover += price * Decimal(str(vol_inc * 60))
            total_oi += oi_inc

            # 每分钟模拟 2 个 Tick
            t1 = TickData(
                instrument=instrument,
                exchange="DCE",
                trading_day="20260910",
                timestamp=cur_time,
                last_price=price,
                bid_price_1=price - Decimal("0.5"),
                bid_volume_1=20,
                ask_price_1=price,
                ask_volume_1=15,
                volume=total_vol,
                turnover=total_turnover,
                open_interest=total_oi
            )
            engine.on_tick(t1)

            t2 = TickData(
                instrument=instrument,
                exchange="DCE",
                trading_day="20260910",
                timestamp=cur_time + timedelta(seconds=30),
                last_price=price,
                bid_price_1=price - Decimal("0.5"),
                bid_volume_1=25,
                ask_price_1=price,
                ask_volume_1=10,
                volume=total_vol + 5,
                turnover=total_turnover + price * Decimal("300"),
                open_interest=total_oi
            )
            engine.on_tick(t2)

        # 验证 Redis 中的 quote
        quote = self.redis.hgetall(f"quote:{instrument}")
        self.assertTrue(quote)
        self.assertEqual(quote["instrument"], instrument)
        print("Verified Redis quote:", quote["last_price"], "volume:", quote["volume"])

        # 验证 Redis 中的 15m K线
        bars_15m_raw = self.redis.lrange(f"bars:{instrument}:15m", 0, -1)
        self.assertGreater(len(bars_15m_raw), 15)
        print(f"Verified Redis 15m bars count: {len(bars_15m_raw)}")

        # 验证 MySQL 写入
        session = SessionLocal()
        mysql_bars = session.query(MarketBarModel).filter_by(
            instrument_id=instrument,
            period="15m"
        ).all()
        self.assertGreater(len(mysql_bars), 15)
        print(f"Verified MySQL 15m bars count: {len(mysql_bars)}")
        session.close()

if __name__ == "__main__":
    unittest.main()
