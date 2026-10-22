"""实盘结构化流水落库 (order_record / trade_record / position_snapshot)。

设计约束:
1. **绝不抛异常**: 落库是旁路动作, 失败不能阻断交易主流程。失败时仅记日志,
   交易侧的完整性由 trading_state 的 JSON 存储与柜台对账保证。
2. **幂等**: 柜台会重复推送回报, 全部使用 ON DUPLICATE KEY UPDATE。
3. **字段缺失容错**: CTP 回报字段可能部分缺失 (如 OnRtnTrade 无 LimitPrice),
   缺字段一律写 NULL, 不猜值。
"""
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import text
from backend.storage.mysql_client import engine

logger = logging.getLogger(__name__)

_UPSERT_ORDER = text("""
INSERT INTO order_record
    (order_ref, gateway_ref, broker_id, investor_id, trading_day, front_id, session_id,
     order_sys_id, exchange, instrument_id, direction, offset_flag, price, volume,
     traded_volume, status, status_msg, owner, gateway_code, submit_time, update_time)
VALUES
    (:order_ref, :gateway_ref, :broker_id, :investor_id, :trading_day, :front_id, :session_id,
     :order_sys_id, :exchange, :instrument_id, :direction, :offset_flag, :price, :volume,
     :traded_volume, :status, :status_msg, :owner, :gateway_code, :submit_time, :update_time)
ON DUPLICATE KEY UPDATE
    gateway_ref=VALUES(gateway_ref), trading_day=VALUES(trading_day),
    front_id=VALUES(front_id), session_id=VALUES(session_id),
    order_sys_id=COALESCE(VALUES(order_sys_id), order_sys_id),
    exchange=COALESCE(VALUES(exchange), exchange),
    instrument_id=VALUES(instrument_id), direction=VALUES(direction),
    offset_flag=VALUES(offset_flag), price=VALUES(price), volume=VALUES(volume),
    traded_volume=GREATEST(VALUES(traded_volume), traded_volume),
    status=VALUES(status), status_msg=VALUES(status_msg),
    gateway_code=COALESCE(VALUES(gateway_code), gateway_code),
    update_time=VALUES(update_time)
""")

_UPSERT_TRADE = text("""
INSERT INTO trade_record
    (trade_key, trade_id, order_ref, order_sys_id, exchange, instrument_id,
     direction, offset_flag, price, volume, trade_time, trading_day)
VALUES
    (:trade_key, :trade_id, :order_ref, :order_sys_id, :exchange, :instrument_id,
     :direction, :offset_flag, :price, :volume, :trade_time, :trading_day)
ON DUPLICATE KEY UPDATE
    price=VALUES(price), volume=VALUES(volume), trade_time=VALUES(trade_time)
""")

_INSERT_POSITION = text("""
INSERT INTO position_snapshot
    (trading_day, snapshot_time, investor_id, instrument_id, posi_direction,
     position, today_position, yd_position, long_frozen, short_frozen,
     position_cost, use_margin, position_profit)
VALUES
    (:trading_day, :snapshot_time, :investor_id, :instrument_id, :posi_direction,
     :position, :today_position, :yd_position, :long_frozen, :short_frozen,
     :position_cost, :use_margin, :position_profit)
""")


def _dec(value) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def _dt(*candidates) -> Optional[datetime]:
    """从多个候选字符串里挑第一个能解析成 datetime 的 (容忍 'YYYY-MM-DD' / 'HH:MM:SS')。"""
    date_part, time_part = None, None
    for c in candidates:
        if not c:
            continue
        s = str(c).strip()
        if not s:
            continue
        if "-" in s and date_part is None:
            date_part = s
        elif ":" in s and time_part is None:
            time_part = s
        elif len(s) >= 14 and s.isdigit() and date_part is None:
            date_part = f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
            time_part = f"{s[8:10]}:{s[10:12]}:{s[12:14]}"
    if not date_part and not time_part:
        return None
    combined = f"{date_part or '1970-01-01'} {time_part or '00:00:00'}"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d %H:%M:%S"):
        try:
            return datetime.strptime(combined, fmt)
        except ValueError:
            continue
    return None


def record_order(record: Dict[str, Any]) -> None:
    """写/更新一条委托流水。record 为 live_trader 内部记录字典。"""
    try:
        with engine.begin() as conn:
            conn.execute(_UPSERT_ORDER, {
                "order_ref": record.get("order_ref"),
                "gateway_ref": record.get("gateway_ref"),
                "broker_id": record.get("broker_id"),
                "investor_id": record.get("investor_id"),
                "trading_day": record.get("trading_day"),
                "front_id": _int(record.get("front_id")),
                "session_id": _int(record.get("session_id")),
                "order_sys_id": record.get("order_sys_id") or None,
                "exchange": record.get("exchange") or None,
                "instrument_id": (record.get("instrument") or "").upper(),
                "direction": record.get("direction"),
                "offset_flag": record.get("offset"),
                "price": _dec(record.get("price")),
                "volume": _int(record.get("volume")),
                "traded_volume": _int(record.get("traded_volume")) or 0,
                "status": record.get("status"),
                "status_msg": (record.get("reject_reason") or "")[:255] or None,
                "owner": record.get("owner") or None,
                "gateway_code": _int(record.get("gateway_code")),
                "submit_time": _dt(record.get("submit_time")),
                "update_time": datetime.now(),
            })
    except Exception:
        logger.exception("委托流水落库失败 (不影响交易主流程) order_ref=%s", record.get("order_ref"))


def record_trade(row: Dict[str, Any]) -> None:
    """写一条成交流水。row 为柜台 Trade 回报的字段字典。"""
    try:
        day = row.get("TradingDay") or row.get("TradeDate") or ""
        trade_key = f"{day}:{row.get('ExchangeID', '')}:{row.get('TradeID', '')}"
        with engine.begin() as conn:
            conn.execute(_UPSERT_TRADE, {
                "trade_key": trade_key,
                "trade_id": row.get("TradeID") or None,
                "order_ref": row.get("OrderRef") or None,
                "order_sys_id": row.get("OrderSysID") or None,
                "exchange": row.get("ExchangeID") or None,
                "instrument_id": (row.get("InstrumentID") or "").upper(),
                "direction": row.get("Direction") or None,
                "offset_flag": row.get("OffsetFlag") or (row.get("CombOffsetFlag") or "")[:1] or None,
                "price": _dec(row.get("Price")),
                "volume": _int(row.get("Volume")),
                "trade_time": _dt(row.get("TradeDate"), row.get("TradeTime")),
                "trading_day": day or None,
            })
    except Exception:
        logger.exception("成交流水落库失败 (不影响交易主流程) TradeID=%s", row.get("TradeID"))


def record_positions(rows: Iterable[Dict[str, Any]], trading_day: str, investor_id: str = "") -> None:
    """落一份柜台持仓快照 (追加式, 支持日间归因)。"""
    rows = list(rows or [])
    if not rows:
        return
    try:
        now = datetime.now()
        with engine.begin() as conn:
            for row in rows:
                conn.execute(_INSERT_POSITION, {
                    "trading_day": trading_day or row.get("TradingDay") or "",
                    "snapshot_time": now,
                    "investor_id": investor_id or None,
                    "instrument_id": (row.get("InstrumentID") or "").upper(),
                    "posi_direction": row.get("PosiDirection") or None,
                    "position": _int(row.get("Position")) or 0,
                    "today_position": _int(row.get("TodayPosition")) or 0,
                    "yd_position": _int(row.get("YdPosition")) or 0,
                    "long_frozen": _int(row.get("LongFrozen")) or 0,
                    "short_frozen": _int(row.get("ShortFrozen")) or 0,
                    "position_cost": _dec(row.get("PositionCost")),
                    "use_margin": _dec(row.get("UseMargin")),
                    "position_profit": _dec(row.get("PositionProfit")),
                })
    except Exception:
        logger.exception("持仓快照落库失败 (不影响交易主流程)")
