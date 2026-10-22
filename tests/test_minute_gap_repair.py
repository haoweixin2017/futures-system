import test_bootstrap
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch
from backend.storage.history_backfill import _build_bar, repair_minute_gaps, save_bars_to_mysql
from backend.storage.mysql_client import SessionLocal
from backend.models.database import MarketBarModel


def setUpModule():
    # 本组只验证新浪备用路径，主源网络请求固定为空，绝不访问公网。
    stub = patch('backend.storage.eastmoney_history.fetch_minute_bars',return_value=[])
    stub.start()
    unittest.addModuleCleanup(stub.stop)


class MinuteGapRepairTests(unittest.TestCase):
    """注意: 时间戳 -1m 平移已收敛到 fetch_sina_minute_bars 内部。
    本组测试直接 patch 该函数, 因此 mock 返回的已是「分钟开始」口径,
    repair_minute_gaps 不再做二次平移。
    """

    def test_current_opening_gap_waits_for_history_to_settle(self):
        symbol = "FU9911"
        def source(minute, volume):
            return _build_bar(symbol, "1m", datetime(2026, 9, 28, 9, minute),
                              Decimal(4300), Decimal(4300), Decimal(4300), Decimal(4300),
                              volume, Decimal(10), Decimal(0))
        early = [source(0, 65), source(1, 331), source(2, 7799)]
        settled = [source(0, 20614), source(1, 9729), source(2, 7799)]
        with patch("backend.storage.history_backfill.fetch_sina_minute_bars", return_value=early):
            repair_minute_gaps(symbol, datetime(2026, 9, 28, 9, 10))
        with SessionLocal() as session:
            rows = session.query(MarketBarModel).filter_by(
                instrument_id=symbol, period="1m").order_by(MarketBarModel.bar_time).all()
            self.assertEqual([r.bar_time.minute for r in rows], [2])
        with patch("backend.storage.history_backfill.fetch_sina_minute_bars", return_value=settled):
            repair_minute_gaps(symbol, datetime(2026, 9, 28, 9, 20))
        with SessionLocal() as session:
            rows = session.query(MarketBarModel).filter_by(
                instrument_id=symbol, period="1m").order_by(MarketBarModel.bar_time).all()
            self.assertEqual([(r.bar_time.minute, r.volume) for r in rows[:2]],
                             [(0, 20614), (1, 9729)])

    def test_real_missing_minutes_only_and_idempotent(self):
        symbol = "GAPTEST"
        def source(minute, price):
            value = Decimal(str(price))
            return _build_bar(symbol, "1m", datetime(2026, 9, 14, 9, minute),
                              value, value, value, value, 5, Decimal(10), Decimal(0))
        existing = source(1, 999)
        save_bars_to_mysql([existing])
        upstream = [source(1, 740), source(2, 741), source(4, 743), source(9, 748)]
        with patch("backend.storage.history_backfill.fetch_sina_minute_bars", return_value=upstream):
            first = repair_minute_gaps(symbol, datetime(2026, 9, 14, 9, 10))
            second = repair_minute_gaps(symbol, datetime(2026, 9, 14, 9, 10))
        self.assertEqual(first["missing"], 2)
        self.assertEqual(second["missing"], 0)
        with SessionLocal() as session:
            rows = session.query(MarketBarModel).filter_by(instrument_id=symbol).order_by(MarketBarModel.bar_time).all()
            self.assertEqual([r.bar_time.minute for r in rows], [1, 2, 4])
            # 已收盘K线的行情字段以历史源为准: 本地 09:01 的 999 应被上游 740 覆写。
            # 本地引擎冷启动会吞掉当分钟已有成交量, 必须靠历史源纠偏。
            self.assertEqual([r.close_price for r in rows], [Decimal(740), Decimal(741), Decimal(743)])
            self.assertEqual(rows[0].trading_day, "20260914")

    def test_cutoff_bars_are_never_touched(self):
        """cutoff (近 2 分钟) 之内的K线属实时引擎属地, 历史源不得覆写或补入。

        mock 返回的已是平移后的「分钟开始」戳: minute=8 -> 09:08, minute=9 -> 09:09。
        """
        symbol = "CUTOFFTEST"
        def bar(minute, price):
            value = Decimal(str(price))
            return _build_bar(symbol, "1m", datetime(2026, 9, 14, 9, minute),
                              value, value, value, value, 5, Decimal(10), Decimal(0))
        # 本地已有 09:08 的K线 (close=888), cutoff=09:08 -> 该根不得被覆写
        save_bars_to_mysql([bar(8, 888)])
        # 上游: 09:07 为真实缺口(应补入); 09:08 落在 cutoff 内(应排除)
        upstream = [bar(7, 111), bar(8, 222)]
        with patch("backend.storage.history_backfill.fetch_sina_minute_bars", return_value=upstream):
            result = repair_minute_gaps(symbol, datetime(2026, 9, 14, 9, 10))
        self.assertEqual(result["missing"], 1, "09:07 是真实缺口, 应被补入")
        self.assertEqual(result["corrected_fields"], 0, "cutoff 内的K线不应被覆写")
        with SessionLocal() as session:
            rows = session.query(MarketBarModel).filter_by(instrument_id=symbol).order_by(MarketBarModel.bar_time).all()
            self.assertEqual([r.bar_time.minute for r in rows], [7, 8], "cutoff 之后的 09:09 不应被补入")
            self.assertEqual(rows[1].close_price, Decimal(888), "cutoff 之内的K线不应被覆写")

    def test_cross_midnight_belongs_to_monday(self):
        b = _build_bar("GAPNIGHT", "1m", datetime(2026, 9, 12, 1, 1),
                       Decimal(740), Decimal(741), Decimal(739), Decimal(740), 5, Decimal(10), Decimal(0))
        with patch("backend.storage.history_backfill.fetch_sina_minute_bars", return_value=[b]):
            repair_minute_gaps("GAPNIGHT", datetime(2026, 9, 14, 9, 10))
        with SessionLocal() as session:
            row = session.query(MarketBarModel).filter_by(instrument_id="GAPNIGHT").one()
            self.assertEqual(row.bar_time, datetime(2026, 9, 12, 1, 1))
            self.assertEqual(row.trading_day, "20260914")

    def test_subscription_backfill_uses_same_gap_path(self):
        from backend.storage.history_backfill import backfill_symbol
        with patch("backend.storage.history_backfill.repair_minute_gaps", return_value={"inserted": 3}) as repair:
            result = backfill_symbol("FU2611", periods=["1m"], sleep_between=0)
            repair.assert_called_once_with("FU2611")
            self.assertEqual(result["periods"]["1m"]["inserted"], 3)

    def test_existing_night_bars_and_cache_use_monday(self):
        from backend.api.routes import cached_bar
        import json
        b = _build_bar("NIGHTFIX", "1m", datetime(2026, 9, 12, 1, 0),
                       Decimal(740), Decimal(741), Decimal(739), Decimal(740), 5, Decimal(10), Decimal(0))
        b["trading_day"] = "20260911"
        save_bars_to_mysql([b])
        source = dict(b, bar_time=datetime(2026, 9, 12, 1, 0))
        with patch("backend.storage.history_backfill.fetch_sina_minute_bars", return_value=[source]):
            result = repair_minute_gaps("NIGHTFIX", datetime(2026, 9, 14, 9, 10))
        self.assertEqual(result["corrected_trading_days"], 1)
        self.assertEqual(result["missing"], 0)
        with SessionLocal() as session:
            row = session.query(MarketBarModel).filter_by(instrument_id="NIGHTFIX").one()
            self.assertEqual(row.trading_day, "20260914")
            self.assertEqual(row.close_price, Decimal(740))
        cached = cached_bar(json.dumps(b, default=str))
        self.assertEqual(cached["trading_day"], "20260914")


class OpenAuctionSanitizeTests(unittest.TestCase):
    """日盘 09:00 首根集合竞价异常量的源头净化。

    新浪源偶把开盘集合竞价的累计委托量写入首根, 放大 3~19 倍。
    净化必须: ① 只缩首根 ② 差额摊回后续分钟 ③ 日盘合计守恒
             ④ 幂等 (重复执行不再改动) ⑤ 不误伤正常日
    """

    SYMBOL = "AUCTIONFIX"

    def _day(self, trading_day, first_volume, other_volumes):
        """构造一天: 09:00 首根 + 09:01 起的其余分钟"""
        bars = []
        prices = [(4430, 4442, 4405, 4410)]
        prices += [(4400 + i, 4405 + i, 4395 + i, 4400 + i)
                   for i in range(len(other_volumes))]
        # 09:00 首根
        o, h, l, c = prices[0]
        bars.append(_build_bar(self.SYMBOL, "1m",
                               datetime.strptime(trading_day, "%Y%m%d").replace(hour=9, minute=0),
                               Decimal(o), Decimal(h), Decimal(l), Decimal(c),
                               first_volume, Decimal(1000), Decimal(0)))
        for i, vol in enumerate(other_volumes, start=1):
            o, h, l, c = prices[i]
            bars.append(_build_bar(self.SYMBOL, "1m",
                                   datetime.strptime(trading_day, "%Y%m%d").replace(hour=9, minute=i),
                                   Decimal(o), Decimal(h), Decimal(l), Decimal(c),
                                   vol, Decimal(1000), Decimal(0)))
        return bars

    def _normal_day(self, trading_day, ratio=1.4, other_max=10000, n=20):
        """构造一个「正常日」: 首根 = 次高 * ratio"""
        others = [other_max if i == 4 else max(1, other_max // 3) for i in range(n)]
        return self._day(trading_day, int(other_max * ratio), others)

    def _load(self, bars):
        save_bars_to_mysql([dict(b) for b in bars])

    def test_abnormal_first_bar_is_shrunk_and_total_preserved(self):
        # 两个正常日建立基准 (ratio 1.4)
        self._load(self._normal_day("20260910"))
        self._load(self._normal_day("20260911"))
        # 异常日: 首根 10 倍于次高
        bad_day = self._day("20260914", 162352,
                            [max(1, 15802 // 3) for _ in range(20)])
        bad_day[4]["volume"] = 15802        # 次高
        self._load(bad_day)

        before_total = sum(b["volume"] for b in bad_day)
        upstream = [dict(b) for b in bad_day]
        upstream += [dict(b) for b in self._normal_day("20260910")]
        upstream += [dict(b) for b in self._normal_day("20260911")]

        with patch("backend.storage.history_backfill.fetch_sina_minute_bars",
                   return_value=upstream):
            result = repair_minute_gaps(self.SYMBOL, datetime(2026, 9, 15, 9, 10))

        self.assertEqual(result["auction_fixed"], 1, "应检出并净化 1 个异常交易日")
        with SessionLocal() as session:
            rows = (session.query(MarketBarModel)
                    .filter_by(instrument_id=self.SYMBOL, trading_day="20260914")
                    .order_by(MarketBarModel.bar_time).all())
            first = rows[0]
            self.assertLess(first.volume, 162352, "首根应被缩小")
            # 首根/次高 恢复到接近基准倍数的量级
            second = max(r.volume for r in rows[1:])
            self.assertLess(first.volume / second, 3.0,
                            "净化后首根不应再是异常巨柱")
            # 日盘合计守恒
            self.assertEqual(sum(r.volume for r in rows), before_total,
                             "摊回后日盘合计必须不变")

    def test_idempotent(self):
        self._load(self._normal_day("20260910"))
        self._load(self._normal_day("20260911"))
        bad_day = self._day("20260914", 162352,
                            [max(1, 15802 // 3) for _ in range(20)])
        bad_day[4]["volume"] = 15802
        self._load(bad_day)
        upstream = [dict(b) for b in bad_day]
        upstream += [dict(b) for b in self._normal_day("20260910")]
        upstream += [dict(b) for b in self._normal_day("20260911")]

        with patch("backend.storage.history_backfill.fetch_sina_minute_bars",
                   return_value=upstream):
            repair_minute_gaps(self.SYMBOL, datetime(2026, 9, 15, 9, 10))
        with SessionLocal() as session:
            first_vol = (session.query(MarketBarModel)
                         .filter_by(instrument_id=self.SYMBOL, trading_day="20260914")
                         .order_by(MarketBarModel.bar_time).first().volume)

        # 第二轮: 源数据不变, 结果应完全一致 (不再触发修正)
        with patch("backend.storage.history_backfill.fetch_sina_minute_bars",
                   return_value=upstream):
            second = repair_minute_gaps(self.SYMBOL, datetime(2026, 9, 15, 9, 10))
        self.assertEqual(second["auction_fixed"], 0, "已净化的数据不应重复触发")
        with SessionLocal() as session:
            again = (session.query(MarketBarModel)
                     .filter_by(instrument_id=self.SYMBOL, trading_day="20260914")
                     .order_by(MarketBarModel.bar_time).first().volume)
        self.assertEqual(first_vol, again, "首根值必须稳定, 不随轮次漂移")

    def test_normal_days_are_not_touched(self):
        self._load(self._normal_day("20260910"))
        self._load(self._normal_day("20260911"))
        normal = self._normal_day("20260914")
        self._load(normal)
        before = {b["bar_time"]: b["volume"] for b in normal}
        upstream = [dict(b) for b in normal]
        upstream += [dict(b) for b in self._normal_day("20260910")]
        upstream += [dict(b) for b in self._normal_day("20260911")]

        with patch("backend.storage.history_backfill.fetch_sina_minute_bars",
                   return_value=upstream):
            result = repair_minute_gaps(self.SYMBOL, datetime(2026, 9, 15, 9, 10))

        self.assertEqual(result["auction_fixed"], 0, "正常日不应被误伤")
        with SessionLocal() as session:
            rows = (session.query(MarketBarModel)
                    .filter_by(instrument_id=self.SYMBOL, trading_day="20260914")
                    .order_by(MarketBarModel.bar_time).all())
            after = {r.bar_time: r.volume for r in rows}
        self.assertEqual(before, after, "正常日各分钟量必须原样保留")
