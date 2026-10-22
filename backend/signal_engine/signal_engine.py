from backend.trade_engine.paper_accounts import paper_accounts
from backend.notifier.notifier import notifier
import json
import logging
from decimal import Decimal
from datetime import datetime
from typing import List, Optional, Dict
from backend.models.market_data import BarData, SignalData, SignalReason
from backend.indicator_engine.indicators import IndicatorCalculator
from backend.signal_engine.rules import evaluate_rules
from backend.common.volume_quality import volume_usable
from backend.storage.redis_client import RedisClient
from backend.storage.mysql_client import SessionLocal
from backend.models.database import SignalRecordModel

logger = logging.getLogger(__name__)

# 信号枚举 → 中文名 (系统事件消息展示用; 入库/Redis 仍存英文枚举)
SIGNAL_NAME_TEXT = {
    "LONG_WATCH": "多头观察",
    "SHORT_WATCH": "空头观察",
    "OI_SURGE": "持仓异动",
}

class SignalEngine:
    """
    客观规则信号引擎：
    - 多头观察 (LONG_WATCH, 15m)
    - 空头观察 (SHORT_WATCH, 15m)
    - 持仓异动 (OI_SURGE, 15m)
    所有规则均基于客观数值判断，杜绝主观臆测。
    """
    def __init__(self):
        self.redis = RedisClient.get_client()

    def evaluate_15m_signals(
        self,
        instrument: str,
        bars_15m: List[BarData],
        bars_60m: List[BarData]
    ) -> Optional[SignalData]:
        if len(bars_15m) < 21:
            return None
        if any(not volume_usable(bar) for bar in bars_15m[-21:]):
            return None

        current_bar = bars_15m[-1]
        prev_bars = bars_15m[-21:-1]
        prev_bar = bars_15m[-2]

        evaluation = evaluate_rules(current_bar, bars_15m[:-1], bars_60m, 'LEGACY')
        stats = evaluation['stats']
        highest_prev, lowest_prev = stats['highest_prev'], stats['lowest_prev']
        avg_vol, vol_ratio, oi_change = stats['avg_vol'], stats['vol_ratio'], stats['oi_change']
        long_conditions = evaluation['long']['conditions']
        passed_long_break, passed_vol, passed_oi, passed_long_trend = [c['passed'] for c in long_conditions]
        long_score = evaluation['long']['score']

        if long_score >= 70:
            atr = IndicatorCalculator.calculate_atr(bars_15m, 14) or Decimal("10.0")
            invalid_price = current_bar.close_price - (atr * Decimal("1.5"))
            reasons = [
                SignalReason(rule="BREAK_PREVIOUS_HIGH", current=float(current_bar.close_price), threshold=float(highest_prev), passed=passed_long_break),
                SignalReason(rule="VOLUME_EXPANSION", current=int(current_bar.volume), threshold=int(avg_vol), ratio=round(vol_ratio, 2), passed=passed_vol),
                SignalReason(rule="OPEN_INTEREST_INCREASE", current=float(current_bar.open_interest), threshold=float(prev_bar.open_interest), passed=passed_oi),
                SignalReason(rule="TREND_ALIGNMENT_60M", current="EMA20>=EMA60" if passed_long_trend else "EMA20<EMA60", passed=passed_long_trend)
            ]
            sig = SignalData(
                instrument=instrument,
                signal="LONG_WATCH",
                timeframe="15m",
                rule_score=long_score,
                trigger_price=current_bar.close_price,
                invalid_price=invalid_price.quantize(Decimal("0.01")),
                reasons=reasons,
                timestamp=current_bar.bar_time
            )
            self._save_signal(sig)
            return sig

        # ----------------- 2. 空头观察 (SHORT_WATCH) -----------------
        short_conditions = evaluation['short']['conditions']
        passed_short_break, _, _, passed_short_trend = [c['passed'] for c in short_conditions]
        short_score = evaluation['short']['score']

        if short_score >= 70:
            atr = IndicatorCalculator.calculate_atr(bars_15m, 14) or Decimal("10.0")
            invalid_price = current_bar.close_price + (atr * Decimal("1.5"))
            reasons = [
                SignalReason(rule="BREAK_PREVIOUS_LOW", current=float(current_bar.close_price), threshold=float(lowest_prev), passed=passed_short_break),
                SignalReason(rule="VOLUME_EXPANSION", current=int(current_bar.volume), threshold=int(avg_vol), ratio=round(vol_ratio, 2), passed=passed_vol),
                SignalReason(rule="OPEN_INTEREST_INCREASE", current=float(current_bar.open_interest), threshold=float(prev_bar.open_interest), passed=passed_oi),
                SignalReason(rule="TREND_ALIGNMENT_60M", current="EMA20<=EMA60" if passed_short_trend else "EMA20>EMA60", passed=passed_short_trend)
            ]
            sig = SignalData(
                instrument=instrument,
                signal="SHORT_WATCH",
                timeframe="15m",
                rule_score=short_score,
                trigger_price=current_bar.close_price,
                invalid_price=invalid_price.quantize(Decimal("0.01")),
                reasons=reasons,
                timestamp=current_bar.bar_time
            )
            self._save_signal(sig)
            return sig

        # ----------------- 3. 持仓异动 (OI_SURGE) -----------------
        # 单根增仓达到均量的 1.0 倍以上
        avg_vol_dec = Decimal(str(round(avg_vol, 2)))
        if oi_change > (avg_vol_dec * Decimal("1.0")) and current_bar.volume > avg_vol:
            reasons = [
                SignalReason(rule="OI_SURGE", current=float(oi_change), threshold=float(avg_vol), passed=True),
                SignalReason(rule="VOLUME_CONFIRM", current=int(current_bar.volume), threshold=int(avg_vol), passed=True)
            ]
            sig = SignalData(
                instrument=instrument,
                signal="OI_SURGE",
                timeframe="15m",
                rule_score=75,
                trigger_price=current_bar.close_price,
                invalid_price=None,
                reasons=reasons,
                timestamp=current_bar.bar_time
            )
            self._save_signal(sig)
            return sig

        return None

    def _save_signal(self, sig: SignalData):
        try:
            payload = {
                "instrument": sig.instrument,
                "signal": sig.signal,
                "timeframe": sig.timeframe,
                "rule_score": sig.rule_score,
                "trigger_price": str(sig.trigger_price),
                "invalid_price": str(sig.invalid_price) if sig.invalid_price else "",
                "timestamp": sig.timestamp.isoformat(),
                "reasons": json.dumps([r.model_dump() for r in sig.reasons])
            }
            self.redis.hset(f"signal:{sig.instrument}", mapping=payload)
        except Exception as e:
            logger.error(f"Save signal to redis error: {e}")

        session = SessionLocal()
        try:
            row = SignalRecordModel(
                instrument_id=sig.instrument,
                signal_type=sig.signal,
                timeframe=sig.timeframe,
                rule_score=sig.rule_score,
                trigger_price=sig.trigger_price,
                invalid_price=sig.invalid_price,
                reasons_json=json.dumps([r.model_dump() for r in sig.reasons], ensure_ascii=False),
                signal_time=sig.timestamp
            )
            session.add(row)
            session.commit()
                        # 触发信号与自动跟单交易桥接
            try:
                paper_accounts.on_signal_triggered(sig)
            except Exception as e:
                logger.error(f"SignalTradeBridge trigger error: {e}")

            notifier.notify(
                event_type="SIGNAL_TRIGGER",
                message=f"合约 {sig.instrument} 触发「{SIGNAL_NAME_TEXT.get(sig.signal, sig.signal)}」信号，客观评分: {sig.rule_score}/100",
                severity="WARNING",
                source="signal_engine",
                details={
                    "instrument": sig.instrument,
                    "signal": sig.signal,
                    "timeframe": sig.timeframe,
                    "rule_score": sig.rule_score,
                    "trigger_price": str(sig.trigger_price),
                    "invalid_price": str(sig.invalid_price) if sig.invalid_price else ""
                }
            )
        except Exception as e:
            session.rollback()
            logger.error(f"Save signal to mysql error: {e}")
        finally:
            session.close()


