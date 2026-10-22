"""分时按交易日回看: bars 日期过滤与交易日列表 API 测试。"""

try:
    import test_bootstrap  # noqa: F401  直接发现模式(-s tests)下生效, 切换隔离环境
except ImportError:
    from tests import test_bootstrap  # noqa: F401  以包模式运行时生效

import unittest
from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from backend.api.routes import app
from backend.storage.mysql_client import SessionLocal
from backend.models.database import MarketBarModel

client = TestClient(app)
INST = "TESTDATE"


def make_bar(trading_day, hhmm, close):
    return MarketBarModel(
        instrument_id=INST,
        trading_day=trading_day,
        period="1m",
        bar_time=datetime.strptime(f"{trading_day[:4]}-{trading_day[4:6]}-{trading_day[6:8]} {hhmm}", "%Y-%m-%d %H:%M"),
        open_price=Decimal(str(close)),
        high_price=Decimal(str(close)),
        low_price=Decimal(str(close)),
        close_price=Decimal(str(close)),
        volume=100,
        turnover=Decimal("100000"),
        open_interest=Decimal("50000"),
        open_interest_change=Decimal("0"),
    )


class TestBarDates(unittest.TestCase):
    def setUp(self):
        session = SessionLocal()
        session.query(MarketBarModel).filter_by(instrument_id=INST).delete()
        session.add_all([
            make_bar("20260910", "09:01", 1000),
            make_bar("20260910", "09:02", 1001),
            make_bar("20260911", "09:01", 1010),
        ])
        session.commit()
        session.close()

    def tearDown(self):
        session = SessionLocal()
        session.query(MarketBarModel).filter_by(instrument_id=INST).delete()
        session.commit()
        session.close()

    def test_dates_listed_ascending(self):
        d = client.get(f"/api/v1/bars/{INST}/dates?period=1m").json()
        self.assertEqual(d["dates"], ["20260910", "20260911"])

    def test_bars_filtered_by_date(self):
        d = client.get(f"/api/v1/bars/{INST}?period=1m&date=20260910").json()
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["source"], "mysql")
        self.assertEqual(d["bars"][0]["bar_time"], "2026-09-10T09:01:00")
        self.assertEqual([b["close_price"] for b in d["bars"]], ["1000.000000", "1001.000000"])

    def test_date_filter_empty_day(self):
        d = client.get(f"/api/v1/bars/{INST}?period=1m&date=20260101").json()
        self.assertEqual(d["count"], 0)

    def test_date_format_validated(self):
        res = client.get(f"/api/v1/bars/{INST}?period=1m&date=2026091")
        self.assertEqual(res.status_code, 422)


if __name__ == "__main__":
    unittest.main()
