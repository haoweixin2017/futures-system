try:
    import test_bootstrap  # noqa: F401  直接发现模式(-s tests)下生效, 切换隔离环境
except ImportError:
    from tests import test_bootstrap  # noqa: F401  以包模式运行时生效

import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from backend.storage.mysql_client import SessionLocal, init_db
from backend.models.database import MarketTickModel
from backend.storage.retention import retention_manager

class TestDataRetention(unittest.TestCase):
    def setUp(self):
        init_db()
        self.session = SessionLocal()

    def tearDown(self):
        self.session.close()

    def test_clean_expired_ticks(self):
        now = datetime.utcnow()
        # 插入 2 条 10 天前的过期 Tick
        old_time = now - timedelta(days=10)
        t1 = MarketTickModel(
            instrument_id="TEST_OLD",
            exchange="DCE",
            trading_day="20260901",
            tick_time=old_time,
            last_price=Decimal("1200.0"),
            volume=100,
            turnover=Decimal("120000.0"),
            open_interest=Decimal("10000.0")
        )
        t2 = MarketTickModel(
            instrument_id="TEST_OLD",
            exchange="DCE",
            trading_day="20260901",
            tick_time=old_time + timedelta(seconds=1),
            last_price=Decimal("1200.5"),
            volume=105,
            turnover=Decimal("126000.0"),
            open_interest=Decimal("10005.0")
        )

        # 插入 1 条 1 天前的新 Tick (不应被删除)
        recent_time = now - timedelta(days=1)
        t3 = MarketTickModel(
            instrument_id="TEST_NEW",
            exchange="DCE",
            trading_day="20260910",
            tick_time=recent_time,
            last_price=Decimal("1250.0"),
            volume=200,
            turnover=Decimal("250000.0"),
            open_interest=Decimal("15000.0")
        )

        self.session.add_all([t1, t2, t3])
        self.session.commit()

        # 执行清理，保留 7 天
        deleted = retention_manager.clean_expired_ticks(days=7)
        self.assertGreaterEqual(deleted, 2)

        # 验证新数据仍然存在
        remaining = self.session.query(MarketTickModel).filter_by(instrument_id="TEST_NEW").first()
        self.assertIsNotNone(remaining)

        # 清理测试数据
        self.session.query(MarketTickModel).filter_by(instrument_id="TEST_NEW").delete()
        self.session.commit()

if __name__ == "__main__":
    unittest.main()
