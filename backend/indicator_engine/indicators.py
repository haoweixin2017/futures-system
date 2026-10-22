import math
from decimal import Decimal
from typing import List, Optional, Dict, Any
from backend.models.market_data import BarData, TickData
from backend.common.volume_quality import volume_usable

class IndicatorCalculator:
    """
    全量指标计算工具：
    EMA(5, 10, 20, 60), ATR(14), VWAP, 20周期均量, 日内高低, 20周期前高前低, 开盘区间, 波动率, 涨跌停距离
    """
    @staticmethod
    def calculate_ema(closes: List[Decimal], period: int) -> Optional[Decimal]:
        if not closes or len(closes) < period:
            return None
        k = Decimal("2") / Decimal(str(period + 1))
        ema = sum(closes[:period]) / Decimal(str(period))
        for price in closes[period:]:
            ema = (price * k) + (ema * (Decimal("1") - k))
        return ema.quantize(Decimal("0.01"))

    @staticmethod
    def calculate_atr(bars: List[BarData], period: int = 14) -> Optional[Decimal]:
        if len(bars) < period + 1:
            return None
        tr_list: List[Decimal] = []
        for i in range(1, len(bars)):
            high = bars[i].high_price
            low = bars[i].low_price
            prev_close = bars[i-1].close_price
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_list.append(tr)

        if len(tr_list) < period:
            return None
        atr = sum(tr_list[-period:]) / Decimal(str(period))
        return atr.quantize(Decimal("0.01"))

    @staticmethod
    def calculate_vwap(bars: List[BarData], volume_multiple: int = 10) -> Optional[Decimal]:
        if not bars:
            return None
        if any(not volume_usable(b) for b in bars):
            return None
        if any(b.volume > 0 and b.turnover <= 0 for b in bars):
            return None
        total_turnover = sum(b.turnover for b in bars)
        total_vol = sum(b.volume for b in bars)
        if total_vol == 0:
            return bars[-1].close_price
        mult = Decimal(str(volume_multiple if volume_multiple > 0 else 1))
        return (total_turnover / (Decimal(str(total_vol)) * mult)).quantize(Decimal("0.01"))

    @staticmethod
    def calculate_volatility(closes: List[Decimal], window: int = 20) -> Optional[float]:
        """计算最近窗口收益率年化波动率估计"""
        if len(closes) < window + 1:
            return None
        returns = []
        subset = closes[-window-1:]
        for i in range(1, len(subset)):
            if subset[i-1] > 0:
                ret = float((subset[i] - subset[i-1]) / subset[i-1])
                returns.append(ret)
        if not returns:
            return None
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance)
        # 年化估计 (按日内周期粗算)
        annualized_vol = std_dev * math.sqrt(240 * 250)
        return round(annualized_vol * 100, 2)

    @classmethod
    def compute_all_indicators(
        cls,
        bars: List[BarData],
        current_tick: Optional[Dict[str, Any]] = None,
        volume_multiple: int = 10
    ) -> Dict[str, Any]:
        if not bars:
            return {}

        closes = [b.close_price for b in bars]
        latest_bar = bars[-1]

        # 1. 均线系统
        ema5 = cls.calculate_ema(closes, 5)
        ema10 = cls.calculate_ema(closes, 10)
        ema20 = cls.calculate_ema(closes, 20)
        ema60 = cls.calculate_ema(closes, 60)

        # 2. 波动与均价
        atr14 = cls.calculate_atr(bars, 14)
        day_bars = [b for b in bars if b.trading_day == latest_bar.trading_day]
        vwap = cls.calculate_vwap(day_bars, volume_multiple=volume_multiple)
        volatility = cls.calculate_volatility(closes, 20)

        # 3. 统计与极值
        subset_20 = bars[-20:] if len(bars) >= 20 else bars
        prev_high_20 = max(b.high_price for b in subset_20)
        prev_low_20 = min(b.low_price for b in subset_20)
        avg_volume_20 = (sum(b.volume for b in subset_20) / len(subset_20)
                         if all(volume_usable(b) for b in subset_20) else None)

        # 4. 日内高低与开盘区间 (严格按当前交易日筛选)
        current_trading_day = latest_bar.trading_day
        day_bars = [b for b in bars if b.trading_day == current_trading_day]
        if not day_bars:
            day_bars = [latest_bar]

        day_high = max(b.high_price for b in day_bars)
        day_low = min(b.low_price for b in day_bars)
        opening_bar = day_bars[0]
        opening_range = {
            "open": str(opening_bar.open_price),
            "high": str(opening_bar.high_price),
            "low": str(opening_bar.low_price),
            "close": str(opening_bar.close_price)
        }

        # 5. 持仓与成交量异动
        prev_oi = bars[-2].open_interest if len(bars) >= 2 else latest_bar.open_interest
        oi_change = latest_bar.open_interest - prev_oi
        vol_ratio = ((latest_bar.volume / avg_volume_20) if avg_volume_20 > 0 else 1.0) if avg_volume_20 is not None else None

        return {
            "instrument": latest_bar.instrument,
            "period": latest_bar.period,
            "bar_time": latest_bar.bar_time.isoformat(),
            "close_price": str(latest_bar.close_price),
            "ema5": str(ema5) if ema5 else None,
            "ema10": str(ema10) if ema10 else None,
            "ema20": str(ema20) if ema20 else None,
            "ema60": str(ema60) if ema60 else None,
            "atr14": str(atr14) if atr14 else None,
            "vwap": str(vwap) if vwap is not None else None,
            "vwap_status": "available" if vwap is not None else "missing_turnover",
            "volatility_pct": volatility,
            "prev_high_20": str(prev_high_20),
            "prev_low_20": str(prev_low_20),
            "avg_volume_20": round(avg_volume_20, 2) if avg_volume_20 is not None else None,
            "volume_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "day_high": str(day_high),
            "day_low": str(day_low),
            "open_interest": str(latest_bar.open_interest),
            "open_interest_change": str(oi_change),
            "opening_range": opening_range
        }
