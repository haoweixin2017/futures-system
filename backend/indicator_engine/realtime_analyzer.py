from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from backend.indicator_engine.indicators import IndicatorCalculator
from backend.common.trading_time import FuturesTradingCalendar
from backend.models.market_data import BarData
from backend.common.volume_quality import volume_usable, metadata as volume_metadata


ZERO = Decimal("0")


def _decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _num(value: Optional[Decimal]) -> Optional[float]:
    return float(value) if value is not None else None


class RealtimeAnalyzer:
    """模拟盘只读量价分析器；输入显式数据，绝不触发账户或下单逻辑。"""

    SINA_STALE_SECONDS = 25

    WEEK_LABELS = (
        (75, "周度强势上涨"),
        (60, "周度震荡偏多"),
        (41, "周度箱体震荡"),
        (26, "周度震荡偏空"),
        (0, "周度弱势下跌"),
    )
    DAY_LABELS = (
        (80, "日内强势偏多"),
        (60, "日内偏多"),
        (41, "日内震荡"),
        (21, "日内偏空"),
        (0, "日内强势偏空"),
    )
    SIGNAL_NAMES = {
        "BUY_VOLUME_BREAK": "放量突破买点",
        "BUY_PULLBACK_DRY": "缩量回踩买点",
        "BUY_REVERSAL_VOL": "放量假跌破反转买点",
        "SELL_VOLUME_BREAK": "放量破位卖点",
        "SELL_PULLBACK_WEAK": "缩量反弹卖点",
        "SELL_HIGH_VOLUME_EXHAUST": "高量滞涨反转预警",
    }

    @classmethod
    def analyze(
        cls,
        instrument: str,
        instrument_name: str,
        quote: Dict[str, Any],
        daily_bars: List[BarData],
        bars_5m: List[BarData],
        volume_multiple: int,
        price_tick: Decimal,
        feed: str = "sina",
        now: Optional[datetime] = None,
        bars_1m: Optional[List[BarData]] = None,
        forming_1m: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = now or datetime.now()
        price = _decimal(quote.get("last_price"))
        quote_time = _iso(quote.get("timestamp"))
        result = cls._base_result(instrument, instrument_name, feed, price, quote_time, now)

        local_now = now.astimezone().replace(tzinfo=None) if now.tzinfo else now
        if not FuturesTradingCalendar.is_trading_time_for(local_now, instrument):
            return cls._unavailable(result, "MARKET_CLOSED", "当前合约未开盘")

        if price is None or price <= 0:
            return cls._unavailable(result, "INSUFFICIENT_DATA", "最新价格缺失或无效")

        if quote_time is not None:
            compare_now = now
            compare_quote = quote_time
            if compare_quote.tzinfo is not None and compare_now.tzinfo is None:
                compare_now = compare_now.astimezone()
            elif compare_quote.tzinfo is None and compare_now.tzinfo is not None:
                compare_quote = compare_quote.replace(tzinfo=compare_now.tzinfo)
            latency = max(0, int((compare_now - compare_quote).total_seconds()))
            result["data_latency_seconds"] = latency
            # 新浪商品快照实测常落后 11~19 秒；模拟盘的 5 分钟只读信号允许 25 秒，
            # 其他行情源仍按 10 秒暂停，超过门槛时绝不生成新信号。
            stale_after = cls.SINA_STALE_SECONDS if feed == "sina" else 10
            if latency > stale_after:
                return cls._unavailable(result, "STALE_DATA", f"行情已延迟 {latency} 秒")

        daily = sorted({b.bar_time: b for b in daily_bars}.values(), key=lambda b: b.bar_time)
        bars = sorted({b.bar_time: b for b in bars_5m}.values(), key=lambda b: b.bar_time)
        trading_day = str(quote.get("trading_day") or (bars[-1].trading_day if bars else ""))
        closed_daily = [b for b in daily if b.trading_day != trading_day]
        day_bars = [b for b in bars if b.trading_day == trading_day]

        result["data_quality"]["daily_closed_bars"] = len(closed_daily)
        result["data_quality"]["intraday_closed_bars"] = len(day_bars)
        if len(closed_daily) < 25:
            return cls._unavailable(result, "INSUFFICIENT_DATA", "已闭合日K不足25根")
        if len(day_bars) < 6:
            return cls._unavailable(result, "WARMING_UP", "当前交易日已闭合5分钟K不足6根")
        if _decimal(quote.get("volume")) == ZERO or all(b.volume <= 0 for b in day_bars[-3:]):
            return cls._unavailable(result, "NO_TRADING", "当前合约无有效成交")
        bid = _decimal(quote.get("bid_price_1"))
        ask = _decimal(quote.get("ask_price_1"))
        if bid is None or bid <= 0 or ask is None or ask <= 0:
            return cls._unavailable(result, "NO_TRADING", "买卖一档不完整，可能处于停板或无流动性状态")
        if quote_time is not None:
            quote_local = quote_time.replace(tzinfo=None)
            if (FuturesTradingCalendar.is_trading_time_for(quote_local, instrument)
                    and (quote_local - day_bars[-1].bar_time.replace(tzinfo=None)).total_seconds() > 600):
                return cls._unavailable(result, "STALE_DATA", "最新已闭合5分钟K线已落后超过一个周期")

        weekly = cls._weekly(price, closed_daily, price_tick)
        intraday = cls._intraday(price, quote, day_bars, volume_multiple, price_tick, weekly["score"])
        result["weekly"] = weekly
        result["intraday"] = intraday
        result["data_quality"]["vwap_source"] = intraday["vwap_source"]

        current_ratio, baseline, baseline_quality = cls._volume_ratio(bars, len(bars) - 1)
        latest = day_bars[-1]
        previous = day_bars[-2]
        result["volume"] = {
            "timeframe": "5m",
            "current_closed_bar_volume": latest.volume if volume_usable(latest) else None,
            "baseline_median_volume": _num(baseline),
            "volume_ratio": _num(current_ratio.quantize(Decimal("0.01"))) if current_ratio is not None else None,
            "baseline_quality": baseline_quality,
            "open_interest_change": _num(latest.open_interest - previous.open_interest),
        }
        if current_ratio is None:
            result["data_quality"]["warnings"].append("量能基准样本不足")

        signal = cls._latest_signal(
            instrument, bars, weekly["score"], intraday["score"],
            intraday.get("vwap"), volume_multiple, price_tick
        )
        result["signal"] = signal
        result["preview_signal"] = cls._preview_1m(
            instrument=instrument,
            closed_bars=bars_1m or [],
            forming=forming_1m,
            quote_time=quote_time or now,
            weekly_score=weekly["score"],
            intraday_score=intraday["score"],
            vwap_value=intraday.get("vwap"),
            volume_multiple=volume_multiple,
            tick=price_tick,
        )
        result["analysis_status"] = "READY"
        return result

    @staticmethod
    def _base_result(
        instrument: str,
        instrument_name: str,
        feed: str,
        price: Optional[Decimal],
        quote_time: Optional[datetime],
        now: datetime,
    ) -> Dict[str, Any]:
        return {
            "instrument": instrument,
            "instrument_name": instrument_name,
            "feed": feed,
            "analysis_status": "READY",
            "status_message": "",
            "latest_price": _num(price),
            "weekly": None,
            "intraday": None,
            "volume": None,
            "signal": None,
            "preview_signal": None,
            "data_quality": {
                "daily_closed_bars": 0,
                "intraday_closed_bars": 0,
                "vwap_source": "UNAVAILABLE",
                "warnings": [],
            },
            "quote_time": quote_time.isoformat() if quote_time else None,
            "computed_at": now.isoformat(),
            "data_latency_seconds": None,
        }

    @staticmethod
    def _unavailable(result: Dict[str, Any], status: str, message: str) -> Dict[str, Any]:
        result["analysis_status"] = status
        result["status_message"] = message
        result["signal"] = None
        result["preview_signal"] = None
        result["data_quality"]["warnings"].append(message)
        return result

    @classmethod
    def _weekly(cls, price: Decimal, bars: List[BarData], tick: Decimal) -> Dict[str, Any]:
        closes = [b.close_price for b in bars]
        ma5 = sum(closes[-5:]) / Decimal("5")
        ma20 = sum(closes[-20:]) / Decimal("20")
        ma5_before = sum(closes[-8:-3]) / Decimal("5")
        previous = bars[-5:]
        prev_high = max(b.high_price for b in previous)
        prev_low = min(b.low_price for b in previous)
        ret5 = price / closes[-6] - Decimal("1") if closes[-6] else ZERO
        score = 50
        reasons = []

        score += cls._compare_score(price, ma5, tick, 15, reasons, "PRICE_ABOVE_MA5", "PRICE_BELOW_MA5")
        score += cls._compare_score(ma5, ma20, tick, 15, reasons, "MA5_ABOVE_MA20", "MA5_BELOW_MA20")
        score += cls._compare_score(ma5, ma5_before, tick, 10, reasons, "MA5_RISING", "MA5_FALLING")
        if price > prev_high + tick:
            score += 20
            reasons.append("BREAK_PREVIOUS_5D_HIGH")
        elif price < prev_low - tick:
            score -= 20
            reasons.append("BREAK_PREVIOUS_5D_LOW")
        if ret5 > 0:
            score += 10
            reasons.append("POSITIVE_5D_RETURN")
        elif ret5 < 0:
            score -= 10
            reasons.append("NEGATIVE_5D_RETURN")
        score = max(0, min(100, score))
        return {
            "score": score,
            "label": cls._label(score, cls.WEEK_LABELS),
            "ma5": _num(ma5.quantize(tick)),
            "ma20": _num(ma20.quantize(tick)),
            "previous_5d_high": _num(prev_high),
            "previous_5d_low": _num(prev_low),
            "return_5d_pct": float((ret5 * 100).quantize(Decimal("0.01"))),
            "reason_codes": reasons,
        }

    @classmethod
    def _intraday(
        cls,
        price: Decimal,
        quote: Dict[str, Any],
        bars: List[BarData],
        volume_multiple: int,
        tick: Decimal,
        weekly_score: int,
    ) -> Dict[str, Any]:
        closes = [b.close_price for b in bars]
        atr = IndicatorCalculator.calculate_atr(bars, 14)
        deadband = max(tick * 2, (atr or ZERO) * Decimal("0.1"))
        open_price = _decimal(quote.get("day_open")) or bars[0].open_price
        day_high = max([b.high_price for b in bars] + [_decimal(quote.get("day_high")) or price, price])
        day_low = min([b.low_price for b in bars] + [_decimal(quote.get("day_low")) or price, price])
        vwap = _decimal(quote.get("avg_price"))
        vwap_source = "QUOTE_AVG_PRICE"
        if vwap is None or vwap <= 0:
            vwap = IndicatorCalculator.calculate_vwap(bars, volume_multiple)
            vwap_source = "TURNOVER_CALCULATED" if vwap is not None else "UNAVAILABLE"

        score = 50
        reasons: List[str] = []
        if vwap is not None:
            score += cls._compare_score(price, vwap, deadband, 15, reasons, "PRICE_ABOVE_VWAP", "PRICE_BELOW_VWAP")
        score += cls._compare_score(price, open_price, deadband, 10, reasons, "PRICE_ABOVE_OPEN", "PRICE_BELOW_OPEN")
        if day_high > day_low:
            clv = (price - day_low) / (day_high - day_low)
            clv_points = max(-15, min(15, int(((clv - Decimal("0.5")) * 30).to_integral_value())))
            score += clv_points
            reasons.append("PRICE_NEAR_DAY_HIGH" if clv_points > 5 else "PRICE_NEAR_DAY_LOW" if clv_points < -5 else "PRICE_MID_RANGE")

        ema5 = IndicatorCalculator.calculate_ema(closes, 5)
        ema20 = IndicatorCalculator.calculate_ema(closes, 20)
        if ema5 is not None and ema20 is not None:
            score += cls._compare_score(ema5, ema20, tick, 10, reasons, "EMA5_ABOVE_EMA20", "EMA5_BELOW_EMA20")
        if len(bars) >= 21:
            prev = bars[-21:-1]
            if price > max(b.high_price for b in prev) + tick:
                score += 10
                reasons.append("BREAK_PREVIOUS_20_HIGH_5M")
            elif price < min(b.low_price for b in prev) - tick:
                score -= 10
                reasons.append("BREAK_PREVIOUS_20_LOW_5M")
        score = max(0, min(100, score))

        confidence = "HIGH"
        crossings = 0
        if vwap is None:
            confidence = "LOW"
        else:
            recent = bars[-12:]
            sides = [b.close_price >= vwap for b in recent]
            crossings = sum(sides[i] != sides[i - 1] for i in range(1, len(sides)))
            if crossings >= 4:
                confidence = "MEDIUM"
        if (weekly_score >= 60 and score <= 40) or (weekly_score <= 40 and score >= 60):
            confidence = "MEDIUM" if confidence == "HIGH" else "LOW"
        return {
            "score": score,
            "label": cls._label(score, cls.DAY_LABELS),
            "confidence": confidence,
            "vwap": _num(vwap),
            "vwap_source": vwap_source,
            "day_open": _num(open_price),
            "day_high": _num(day_high),
            "day_low": _num(day_low),
            "vwap_crossings_12": crossings,
            "reason_codes": reasons,
        }

    @staticmethod
    def _compare_score(
        left: Decimal,
        right: Decimal,
        deadband: Decimal,
        points: int,
        reasons: List[str],
        above: str,
        below: str,
    ) -> int:
        if left > right + deadband:
            reasons.append(above)
            return points
        if left < right - deadband:
            reasons.append(below)
            return -points
        return 0

    @staticmethod
    def _label(score: int, labels: Tuple[Tuple[int, str], ...]) -> str:
        return next(label for threshold, label in labels if score >= threshold)

    @classmethod
    def _latest_signal(
        cls,
        instrument: str,
        bars: List[BarData],
        weekly_score: int,
        intraday_score: int,
        vwap_value: Optional[float],
        volume_multiple: int,
        tick: Decimal,
    ) -> Optional[Dict[str, Any]]:
        if len(bars) < 22:
            return None
        vwap = _decimal(vwap_value)
        found = []
        for index in range(max(20, len(bars) - 4), len(bars)):
            candidate = cls._candidate(
                bars, index, weekly_score, intraday_score, vwap,
                volume_multiple, tick
            )
            if candidate:
                found.append((index, candidate))
        if found:
            index, candidate = found[-1]
            state = "PENDING"
            if index + 1 < len(bars) and cls._confirmed(candidate, bars[index + 1], tick):
                state = "CONFIRMED"
            payload = cls._signal_payload(instrument, candidate, state, weekly_score, intraday_score, tick)
            later = bars[index + 1:]
            invalid = Decimal(str(payload["invalidation_price"]))
            if any((b.low_price <= invalid if candidate["side"] == "BUY" else b.high_price >= invalid) for b in later):
                payload["state"] = "INVALIDATED"
                payload["action_text"] = "价格已触及失效位，该信号不再有效"
            elif state == "PENDING" and len(later) >= 3:
                payload["state"] = "EXPIRED"
                payload["action_text"] = "三根5分钟K线内未获确认，该信号已过期"
            return payload
        return None

    @classmethod
    def _candidate(
        cls,
        bars: List[BarData],
        index: int,
        weekly_score: int,
        intraday_score: int,
        vwap: Optional[Decimal],
        volume_multiple: int,
        tick: Decimal,
    ) -> Optional[Dict[str, Any]]:
        if index < 20:
            return None
        bar = bars[index]
        previous = bars[index - 20:index]
        high = max(b.high_price for b in previous)
        low = min(b.low_price for b in previous)
        ratio, _, quality = cls._volume_ratio(bars, index)
        if ratio is None:
            return None
        width = bar.high_price - bar.low_price
        if width <= 0:
            return None
        body_ratio = abs(bar.close_price - bar.open_price) / width
        clv = (bar.close_price - bar.low_price) / width
        upper_shadow = bar.high_price - max(bar.open_price, bar.close_price)
        lower_shadow = min(bar.open_price, bar.close_price) - bar.low_price
        body = abs(bar.close_price - bar.open_price)
        tolerance = max(tick * 2, (IndicatorCalculator.calculate_atr(bars[:index + 1], 14) or ZERO) * Decimal("0.2"))
        ema20 = IndicatorCalculator.calculate_ema([b.close_price for b in bars[:index + 1]], 20)
        historical_vwap = IndicatorCalculator.calculate_vwap(
            [b for b in bars[:index + 1] if b.trading_day == bar.trading_day],
            volume_multiple,
        )
        effective_vwap = vwap if index == len(bars) - 1 else historical_vwap
        support_levels = [level for level in (ema20, effective_vwap) if level is not None]
        structure = bars[max(0, index - 60):index]
        higher_targets = [item.high_price for item in structure if item.high_price > bar.close_price]
        lower_targets = [item.low_price for item in structure if item.low_price < bar.close_price]
        long_target = min(higher_targets) if higher_targets else None
        short_target = max(lower_targets) if lower_targets else None

        if bar.close_price > high and ratio >= Decimal("2") and body_ratio >= Decimal("0.6") and clv >= Decimal("0.8"):
            return cls._make_candidate("BUY_VOLUME_BREAK", "BUY", bar, ratio, quality, high, ["VOLUME_EXPANSION", "BREAK_PREVIOUS_20_HIGH", "STRONG_BULL_BODY"], long_target)
        if bar.close_price < low and ratio >= Decimal("2") and body_ratio >= Decimal("0.6") and clv <= Decimal("0.2"):
            return cls._make_candidate("SELL_VOLUME_BREAK", "SELL", bar, ratio, quality, low, ["VOLUME_EXPANSION", "BREAK_PREVIOUS_20_LOW", "STRONG_BEAR_BODY"], short_target)
        if bar.low_price < low and bar.close_price > low and ratio >= Decimal("1.8") and lower_shadow >= body and clv >= Decimal("0.65"):
            return cls._make_candidate("BUY_REVERSAL_VOL", "BUY", bar, ratio, quality, low, ["FALSE_BREAK_PREVIOUS_LOW", "LOWER_SHADOW", "VOLUME_EXPANSION"], long_target)
        if bar.high_price >= high and ratio >= Decimal("2.5") and upper_shadow >= body and clv <= Decimal("0.55"):
            return cls._make_candidate("SELL_HIGH_VOLUME_EXHAUST", "SELL", bar, ratio, quality, high, ["TEST_PREVIOUS_HIGH", "UPPER_SHADOW", "EXTREME_VOLUME"], short_target)
        if ratio <= Decimal("0.7") and weekly_score >= 41 and intraday_score >= 41:
            support = next((level for level in support_levels if bar.low_price <= level + tolerance and bar.close_price >= level), None)
            if support is not None and bar.close_price > bar.open_price:
                return cls._make_candidate("BUY_PULLBACK_DRY", "BUY", bar, ratio, quality, support, ["LOW_VOLUME", "TOUCH_SUPPORT", "CLOSE_BACK_ABOVE_SUPPORT"], long_target)
        if ratio <= Decimal("0.7") and weekly_score <= 59 and intraday_score <= 59:
            resistance = next((level for level in support_levels if bar.high_price >= level - tolerance and bar.close_price <= level), None)
            if resistance is not None and bar.close_price < bar.open_price:
                return cls._make_candidate("SELL_PULLBACK_WEAK", "SELL", bar, ratio, quality, resistance, ["LOW_VOLUME", "TOUCH_RESISTANCE", "CLOSE_BACK_BELOW_RESISTANCE"], short_target)
        return None

    @staticmethod
    def _make_candidate(
        code: str,
        side: str,
        bar: BarData,
        ratio: Decimal,
        quality: str,
        level: Decimal,
        reasons: List[str],
        target: Optional[Decimal] = None,
    ) -> Dict[str, Any]:
        return {
            "code": code, "side": side, "bar": bar, "ratio": ratio,
            "quality": quality, "level": level, "reasons": reasons,
            "target": target, "timeframe": bar.period,
        }

    @staticmethod
    def _confirmed(candidate: Dict[str, Any], current: BarData, tick: Decimal) -> bool:
        code = candidate["code"]
        bar = candidate["bar"]
        level = candidate["level"]
        if code == "BUY_VOLUME_BREAK":
            return current.close_price >= level - tick and current.high_price > bar.high_price
        if code == "SELL_VOLUME_BREAK":
            return current.close_price <= level + tick and current.low_price < bar.low_price
        if code == "BUY_REVERSAL_VOL":
            return current.high_price > bar.high_price
        if code == "SELL_HIGH_VOLUME_EXHAUST":
            return current.low_price < bar.low_price
        if code == "BUY_PULLBACK_DRY":
            return current.close_price >= level - tick
        return current.close_price <= level + tick

    @classmethod
    def _signal_payload(
        cls,
        instrument: str,
        candidate: Dict[str, Any],
        state: str,
        weekly_score: int,
        intraday_score: int,
        tick: Decimal,
    ) -> Dict[str, Any]:
        bar = candidate["bar"]
        side = candidate["side"]
        atr = max(tick * 2, bar.high_price - bar.low_price)
        min_risk = max(tick * 2, atr * Decimal("0.5"))
        if side == "BUY":
            invalid = min(bar.low_price, bar.close_price - min_risk)
            invalid = cls._round_tick(invalid, tick, ROUND_FLOOR)
            risk = bar.close_price - invalid
            structural = candidate.get("target")
            target = (cls._round_tick(structural, tick, ROUND_FLOOR)
                      if structural is not None and structural > bar.close_price
                      else cls._round_tick(bar.close_price + risk * Decimal("1.5"), tick, ROUND_CEILING))
        else:
            invalid = max(bar.high_price, bar.close_price + min_risk)
            invalid = cls._round_tick(invalid, tick, ROUND_CEILING)
            risk = invalid - bar.close_price
            structural = candidate.get("target")
            target = (cls._round_tick(structural, tick, ROUND_CEILING)
                      if structural is not None and structural < bar.close_price
                      else cls._round_tick(bar.close_price - risk * Decimal("1.5"), tick, ROUND_FLOOR))
        reward = abs(target - bar.close_price)
        reward_risk = reward / risk if risk > 0 else ZERO
        aligned = (side == "BUY" and weekly_score >= 60 and intraday_score >= 60) or (
            side == "SELL" and weekly_score <= 40 and intraday_score <= 40
        )
        confidence = 30 if aligned else 15
        confidence += 25 if candidate["quality"] == "HIGH" else 10
        confidence += 25
        confidence += 20
        confidence = min(100, confidence)
        actionable = state == "CONFIRMED" and confidence >= 60 and reward_risk >= Decimal("1.5")
        if state == "PREVIEW":
            action = "仅作1分钟形成中预警，等待5分钟K线闭合确认"
        elif state == "PENDING":
            action = "等待下一根5分钟K线确认"
        elif reward_risk < Decimal("1.5"):
            action = "下一结构目标收益风险比不足1.5，仅作观察"
        elif confidence < 60:
            action = "多周期方向存在冲突，仅作观察"
        else:
            action = f"信号已确认；触及 {invalid} 时失效"
        return {
            "signal_id": f"{instrument}:{candidate.get('timeframe', '5m')}:{candidate['code']}:{bar.bar_time.strftime('%Y%m%dT%H%M%S')}",
            "timeframe": candidate.get("timeframe", "5m"),
            "side": side,
            "code": candidate["code"],
            "name": cls.SIGNAL_NAMES[candidate["code"]],
            "state": state,
            "trigger_bar_time": bar.bar_time.isoformat(),
            "trigger_price": _num(bar.close_price),
            "invalidation_price": _num(invalid),
            "target_price": _num(target),
            "reward_risk_ratio": float(reward_risk.quantize(Decimal("0.01"))),
            "confidence_score": confidence,
            "confidence_label": "高" if confidence >= 80 else "中" if confidence >= 60 else "低",
            "actionable": actionable,
            "reason_codes": candidate["reasons"],
            "action_text": action,
        }

    @staticmethod
    def _round_tick(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
        if tick <= 0:
            tick = Decimal("1")
        return (value / tick).to_integral_value(rounding=rounding) * tick

    @classmethod
    def _volume_ratio(cls, bars: List[BarData], index: int) -> Tuple[Optional[Decimal], Optional[Decimal], str]:
        if index < 1 or bars[index].volume < 0:
            return None, None, "UNAVAILABLE"
        current = bars[index]
        if not volume_usable(current):
            return None,None,'UNVERIFIED_VOLUME'
        earlier = bars[:index]
        same_slot_bars = [
            b for b in earlier
            if b.trading_day != current.trading_day
            and b.bar_time.time() == current.bar_time.time()
            and b.volume > 0
        ][-10:]
        if any(not volume_usable(b) for b in same_slot_bars):
            return None,None,'UNVERIFIED_VOLUME'
        same_slot=[Decimal(str(b.volume)) for b in same_slot_bars]
        quality = "HIGH"
        samples = same_slot
        if len(samples) < 5:
            segment = cls._session_segment(current.bar_time)
            sample_bars = [
                b for b in earlier
                if cls._session_segment(b.bar_time) == segment and b.volume > 0
            ][-20:]
            if any(not volume_usable(b) for b in sample_bars):
                return None,None,'UNVERIFIED_VOLUME'
            samples=[Decimal(str(b.volume)) for b in sample_bars]
            quality = "LOW"
        if len(samples) < 5:
            return None, None, "UNAVAILABLE"
        baseline = Decimal(str(median(samples)))
        if baseline <= 0:
            return None, None, "UNAVAILABLE"
        return Decimal(str(current.volume)) / baseline, baseline, quality

    @classmethod
    def _preview_1m(
        cls,
        instrument: str,
        closed_bars: List[BarData],
        forming: Optional[Dict[str, Any]],
        quote_time: datetime,
        weekly_score: int,
        intraday_score: int,
        vwap_value: Optional[float],
        volume_multiple: int,
        tick: Decimal,
    ) -> Optional[Dict[str, Any]]:
        if not forming or len(closed_bars) < 20:
            return None
        try:
            current = BarData(
                instrument=instrument,
                trading_day=str(forming["trading_day"]),
                period="1m",
                bar_time=datetime.fromisoformat(str(forming["bar_time"])),
                open_price=Decimal(str(forming["open_price"])),
                high_price=Decimal(str(forming["high_price"])),
                low_price=Decimal(str(forming["low_price"])),
                close_price=Decimal(str(forming["close_price"])),
                volume=int(forming["volume"]),
                provenance_json=volume_metadata(forming),
                turnover=Decimal(str(forming.get("turnover", "0"))),
                open_interest=Decimal(str(forming.get("open_interest", "0"))),
                open_interest_change=Decimal(str(forming.get("open_interest_change", "0"))),
            )
        except (KeyError, TypeError, ValueError):
            return None
        bars = sorted({b.bar_time: b for b in closed_bars}.values(), key=lambda b: b.bar_time)
        if bars[-1].bar_time >= current.bar_time:
            bars = [b for b in bars if b.bar_time < current.bar_time]
        if len(bars) < 20:
            return None
        actual_ratio, _, quality = cls._volume_ratio(bars + [current], len(bars))
        if actual_ratio is None:
            return None
        elapsed = (quote_time.replace(tzinfo=None) - current.bar_time.replace(tzinfo=None)).total_seconds()
        elapsed = max(5.0, min(60.0, elapsed))
        projected_ratio = actual_ratio * Decimal(str(60.0 / elapsed))
        previous = bars[-20:]
        previous_high = max(b.high_price for b in previous)
        previous_low = min(b.low_price for b in previous)
        width = current.high_price - current.low_price
        if width <= 0:
            return None
        body = abs(current.close_price - current.open_price)
        body_ratio = body / width
        clv = (current.close_price - current.low_price) / width
        lower_shadow = min(current.open_price, current.close_price) - current.low_price
        upper_shadow = current.high_price - max(current.open_price, current.close_price)
        code = side = None
        level = ZERO
        reasons: List[str] = ["FORMING_1M", "PROJECTED_VOLUME"]
        if current.close_price > previous_high and projected_ratio >= Decimal("2") and body_ratio >= Decimal("0.6") and clv >= Decimal("0.8"):
            code, side, level = "BUY_VOLUME_BREAK", "BUY", previous_high
            reasons += ["BREAK_PREVIOUS_20_HIGH", "STRONG_BULL_BODY"]
        elif current.close_price < previous_low and projected_ratio >= Decimal("2") and body_ratio >= Decimal("0.6") and clv <= Decimal("0.2"):
            code, side, level = "SELL_VOLUME_BREAK", "SELL", previous_low
            reasons += ["BREAK_PREVIOUS_20_LOW", "STRONG_BEAR_BODY"]
        elif current.low_price < previous_low and current.close_price > previous_low and projected_ratio >= Decimal("1.8") and lower_shadow >= body and clv >= Decimal("0.65"):
            code, side, level = "BUY_REVERSAL_VOL", "BUY", previous_low
            reasons += ["FALSE_BREAK_PREVIOUS_LOW", "LOWER_SHADOW"]
        elif current.high_price >= previous_high and projected_ratio >= Decimal("2.5") and upper_shadow >= body and clv <= Decimal("0.55"):
            code, side, level = "SELL_HIGH_VOLUME_EXHAUST", "SELL", previous_high
            reasons += ["TEST_PREVIOUS_HIGH", "UPPER_SHADOW"]
        else:
            ema20 = IndicatorCalculator.calculate_ema([b.close_price for b in bars] + [current.close_price], 20)
            vwap = _decimal(vwap_value)
            atr = IndicatorCalculator.calculate_atr(bars + [current], 14) or ZERO
            tolerance = max(tick * 2, atr * Decimal("0.2"))
            levels = [item for item in (ema20, vwap) if item is not None]
            if projected_ratio <= Decimal("0.7") and weekly_score >= 41 and intraday_score >= 41:
                support = next((item for item in levels if current.low_price <= item + tolerance and current.close_price >= item), None)
                if support is not None and current.close_price > current.open_price:
                    code, side, level = "BUY_PULLBACK_DRY", "BUY", support
                    reasons += ["LOW_VOLUME", "TOUCH_SUPPORT"]
            if code is None and projected_ratio <= Decimal("0.7") and weekly_score <= 59 and intraday_score <= 59:
                resistance = next((item for item in levels if current.high_price >= item - tolerance and current.close_price <= item), None)
                if resistance is not None and current.close_price < current.open_price:
                    code, side, level = "SELL_PULLBACK_WEAK", "SELL", resistance
                    reasons += ["LOW_VOLUME", "TOUCH_RESISTANCE"]
        if code is None or side is None:
            return None
        earlier = bars[-60:]
        if side == "BUY":
            targets = [b.high_price for b in earlier if b.high_price > current.close_price]
            target = min(targets) if targets else None
        else:
            targets = [b.low_price for b in earlier if b.low_price < current.close_price]
            target = max(targets) if targets else None
        candidate = cls._make_candidate(code, side, current, projected_ratio, quality, level, reasons, target)
        payload = cls._signal_payload(instrument, candidate, "PREVIEW", weekly_score, intraday_score, tick)
        payload["projected_volume_ratio"] = float(projected_ratio.quantize(Decimal("0.01")))
        payload["actionable"] = False
        return payload

    @staticmethod
    def _session_segment(value: datetime) -> str:
        hhmm = value.hour * 60 + value.minute
        if hhmm >= 20 * 60 or hhmm < 3 * 60:
            return "night"
        if hhmm < 10 * 60 + 15:
            return "morning1"
        if hhmm < 11 * 60 + 31:
            return "morning2"
        return "afternoon"
