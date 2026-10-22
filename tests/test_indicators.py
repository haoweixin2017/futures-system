"""技术指标计算测试。

指标算错会直接污染下游信号, 因此对 EMA / ATR / VWAP / 波动率逐项用可手算的
小样本钉死期望值, 并覆盖边界条件 (样本不足 / 成交额缺失 / 全零成交量)。
"""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

import test_bootstrap  # noqa: F401  必须最先导入以完成测试环境隔离

from backend.indicator_engine.indicators import IndicatorCalculator
from backend.models.market_data import BarData


def make_bar(close, *, high=None, low=None, open_=None, volume=10, turnover=None,
             oi=100, day="20260914", minute=0, period="1m"):
    """构造测试K线; 未指定的字段按 close 派生, 便于构造常量价格序列。"""
    close = Decimal(str(close))
    high = Decimal(str(high)) if high is not None else close
    low = Decimal(str(low)) if low is not None else close
    open_ = Decimal(str(open_)) if open_ is not None else close
    # 默认成交额与价格一致 (乘数按 volume_multiple=1 计), 方便 VWAP 手算
    turnover = Decimal(str(turnover)) if turnover is not None else close * Decimal(str(volume))
    return BarData(
        instrument="TEST",
        trading_day=day,
        period=period,
        bar_time=datetime(2026, 9, 14, 9, 0) + timedelta(minutes=minute),
        open_price=open_,
        high_price=high,
        low_price=low,
        close_price=close,
        volume=volume,
        turnover=turnover,
        open_interest=Decimal(str(oi)),
        open_interest_change=Decimal("0"),
    )


class EmaTests(unittest.TestCase):
    def test_insufficient_samples_returns_none(self):
        closes = [Decimal(str(i)) for i in range(1, 5)]  # 只有 4 根, 需求 5
        self.assertIsNone(IndicatorCalculator.calculate_ema(closes, 5))

    def test_constant_series_equals_that_constant(self):
        """常量序列的 EMA 必然等于该常量 (各期加权和恒等)。"""
        closes = [Decimal("100")] * 10
        self.assertEqual(IndicatorCalculator.calculate_ema(closes, 5), Decimal("100.00"))

    def test_seed_is_sma_then_recursive(self):
        """首值取前 period 根的简单均值, 之后按 k=2/(period+1) 递推。

        period=3, closes=[1,2,3,4]; k=0.5
        seed = (1+2+3)/3 = 2
        第4根: 4*0.5 + 2*0.5 = 3
        """
        closes = [Decimal(str(v)) for v in (1, 2, 3, 4)]
        self.assertEqual(IndicatorCalculator.calculate_ema(closes, 3), Decimal("3.00"))

    def test_exactly_period_samples_returns_sma(self):
        closes = [Decimal(str(v)) for v in (10, 20, 30)]
        self.assertEqual(IndicatorCalculator.calculate_ema(closes, 3), Decimal("20.00"))

    def test_result_quantized_to_two_places(self):
        closes = [Decimal(str(v)) for v in (1, 2, 3, 4, 5, 6)]
        ema = IndicatorCalculator.calculate_ema(closes, 5)
        self.assertEqual(ema, ema.quantize(Decimal("0.01")))


class AtrTests(unittest.TestCase):
    def test_insufficient_samples_returns_none(self):
        bars = [make_bar(10, high=11, low=9) for _ in range(10)]  # 需求 period+1
        self.assertIsNone(IndicatorCalculator.calculate_atr(bars, 14))

    def test_true_range_uses_prev_close_when_gapping(self):
        """向上跳空时 TR 应取 |high - prev_close| 而非 high-low。

        bar1: high=10, low=9, close=9
        bar2: high=20, low=19, close=19  (向下? 不, 缺口向上)
          high-low      = 1
          |high-prevC|  = |20-9| = 11   <- 应取此值
          |low -prevC|  = |19-9| = 10
        """
        bars = [
            make_bar(9, high=10, low=9),
            make_bar(19, high=20, low=19),
        ]
        atr = IndicatorCalculator.calculate_atr(bars, 1)
        self.assertEqual(atr, Decimal("11.00"))

    def test_constant_range_series(self):
        """每根 TR 均为 2 (high-low=2, 无跳空) -> ATR=2。"""
        bars = [make_bar(10, high=11, low=9) for _ in range(6)]
        self.assertEqual(IndicatorCalculator.calculate_atr(bars, 5), Decimal("2.00"))

    def test_uses_only_last_period_true_ranges(self):
        """只用最近 period 个 TR: 早先的大波动不应影响结果。

        前2根 TR=100, 后3根 TR=2, period=3 -> ATR=2
        """
        bars = [
            make_bar(10, high=60, low=10),   # TR vs 下一根
            make_bar(10, high=110, low=10),
            make_bar(10, high=11, low=9),
            make_bar(10, high=11, low=9),
            make_bar(10, high=11, low=9),
        ]
        self.assertEqual(IndicatorCalculator.calculate_atr(bars, 3), Decimal("2.00"))


class VwapTests(unittest.TestCase):
    def test_empty_bars_returns_none(self):
        self.assertIsNone(IndicatorCalculator.calculate_vwap([]))

    def test_missing_turnover_returns_none(self):
        """有成交量但成交额缺失 -> 明确返回 None (页面显示'成交额数据不足')。"""
        bars = [make_bar(10, volume=5, turnover=0)]
        self.assertIsNone(IndicatorCalculator.calculate_vwap(bars, volume_multiple=1))

    def test_vwap_formula_with_multiple(self):
        """VWAP = 总成交额 / (总手数 × 合约乘数)。

        bar1: turnover=1000, volume=10
        bar2: turnover=2000, volume=10
        合计 = 3000 / (20 × 1) = 150
        """
        bars = [make_bar(10, volume=10, turnover=1000),
                make_bar(10, volume=10, turnover=2000)]
        self.assertEqual(IndicatorCalculator.calculate_vwap(bars, volume_multiple=1),
                         Decimal("150.00"))

    def test_zero_volume_falls_back_to_last_close(self):
        bars = [make_bar(123, volume=0, turnover=0)]
        self.assertEqual(IndicatorCalculator.calculate_vwap(bars, volume_multiple=1),
                         Decimal("123"))


class VolatilityTests(unittest.TestCase):
    def test_insufficient_samples_returns_none(self):
        closes = [Decimal(str(v)) for v in range(1, 21)]  # 需求 window+1
        self.assertIsNone(IndicatorCalculator.calculate_volatility(closes, 20))

    def test_constant_series_has_zero_volatility(self):
        closes = [Decimal("100")] * 25
        self.assertEqual(IndicatorCalculator.calculate_volatility(closes, 20), 0.0)

    def test_positive_for_moving_series(self):
        closes = [Decimal(str(100 + i)) for i in range(25)]
        vol = IndicatorCalculator.calculate_volatility(closes, 20)
        self.assertIsNotNone(vol)
        self.assertGreater(vol, 0)


class ComputeAllTests(unittest.TestCase):
    def test_empty_bars_returns_empty_dict(self):
        self.assertEqual(IndicatorCalculator.compute_all_indicators([]), {})

    def test_day_high_low_scope_to_current_trading_day(self):
        """日内高低必须只统计当前交易日, 不能被前一交易日的高点污染。"""
        prev_day = [make_bar(999, high=1000, low=900, day="20260911", minute=i) for i in range(3)]
        today = [make_bar(10, high=12, low=8, day="20260914", minute=i) for i in range(3)]
        result = IndicatorCalculator.compute_all_indicators(prev_day + today)
        self.assertEqual(result["day_high"], "12")
        self.assertEqual(result["day_low"], "8")

    def test_open_interest_change_uses_previous_bar(self):
        bars = [make_bar(10, oi=100), make_bar(10, oi=160)]
        result = IndicatorCalculator.compute_all_indicators(bars)
        self.assertEqual(result["open_interest_change"], "60")

    def test_volume_ratio_computed_against_20_bar_average(self):
        """volume_ratio = 最新一根成交量 / 20根均量。"""
        bars = [make_bar(10, volume=10, minute=i) for i in range(19)]
        bars.append(make_bar(10, volume=40, minute=19))
        result = IndicatorCalculator.compute_all_indicators(bars)
        # 均量 = (19*10 + 40)/20 = 11.5; ratio = 40/11.5 ≈ 3.48
        self.assertEqual(result["avg_volume_20"], 11.5)
        self.assertEqual(result["volume_ratio"], 3.48)

    def test_vwap_status_reflects_turnover_availability(self):
        with_turnover = [make_bar(10, volume=5, turnover=50, minute=i) for i in range(3)]
        self.assertEqual(
            IndicatorCalculator.compute_all_indicators(with_turnover)["vwap_status"], "available")
        without = [make_bar(10, volume=5, turnover=0, minute=i) for i in range(3)]
        self.assertEqual(
            IndicatorCalculator.compute_all_indicators(without)["vwap_status"], "missing_turnover")

    def test_short_series_does_not_crash_and_nulls_long_emas(self):
        """样本不足时不应抛异常, 长周期指标应为 None。"""
        bars = [make_bar(10, minute=i) for i in range(3)]
        result = IndicatorCalculator.compute_all_indicators(bars)
        self.assertIsNone(result["ema20"])
        self.assertIsNone(result["ema60"])
        self.assertIsNone(result["atr14"])
        self.assertEqual(result["close_price"], "10")

    def test_prev_high_low_from_last_20_bars(self):
        bars = [make_bar(10, high=10 + i, low=10 - i, minute=i) for i in range(25)]
        result = IndicatorCalculator.compute_all_indicators(bars)
        # 最后20根: high 最大 = 10+24 = 34, low 最小 = 10-24 = -14
        self.assertEqual(result["prev_high_20"], "34")
        self.assertEqual(result["prev_low_20"], "-14")


if __name__ == "__main__":
    unittest.main()
