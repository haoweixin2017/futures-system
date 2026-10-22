"""成交明细与大单监控引擎。

基于快照级行情 (CTP/新浪均为全日累计成交量) 推算每笔现手:
- 现手 = 相邻 Tick 累计量差值; 成交额 = 现手 × 最新价 × 合约乘数 (估算)
- 方向推断 (国内期货通用规则, 结合持仓量增减与价格变动):
    价涨 + 增仓 -> 多开 | 价跌 + 增仓 -> 空开
    价涨 + 减仓 -> 空平 | 价跌 + 减仓 -> 多平
    持仓不变 -> 换手
- 大单: 现手 >= system.big_order_min_volume 时记入大单监控列表

存储: Redis 列表 tape:{instrument} (最近 600 条) 与 bigorders:{instrument} (最近 300 条),
页面通过 /api/v1/tape 与 /api/v1/bigorders 读取。
"""

import json
import logging
from typing import Dict, List, Optional

from backend.common.config import cfg
from backend.common.product_specs import get_spec
from backend.models.database import MarketBarModel
from backend.models.market_data import TickData
from backend.storage.mysql_client import SessionLocal
from backend.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

TAPE_KEY = "tape:{instrument}"
BIG_KEY = "bigorders:{instrument}"
TAPE_MAX = 600
BIG_MAX = 300


class TapeRecorder:
    def __init__(self):
        self._last: Dict[str, Dict] = {}

    def on_tick(self, tick: TickData):
        """每笔行情驱动: 计算现手/方向并写盘 (异常绝不影响行情主链路)"""
        try:
            last = self._last.get(tick.instrument)
            self._last[tick.instrument] = {
                "volume": tick.volume,
                "price": tick.last_price,
                "open_interest": tick.open_interest,
            }
            if last is None:
                return  # 首个基线快照, 无增量

            vol = int(tick.volume) - int(last["volume"])
            if vol <= 0:
                # 无成交或跨交易日累计量重置, 重新建立基线
                return

            oi_chg = float(tick.open_interest - last["open_interest"])
            px_chg = float(tick.last_price - last["price"])
            direction = self._infer_direction(px_chg, oi_chg)

            multiple, _ = get_spec(tick.instrument)
            amount = float(tick.last_price) * vol * multiple
            record = {
                "time": tick.timestamp.strftime("%H:%M:%S"),
                "ts": tick.timestamp.isoformat(timespec="seconds"),
                "trading_day": tick.trading_day,
                "price": float(tick.last_price),
                "volume": vol,
                "amount": round(amount, 0),
                "oi_change": round(oi_chg, 0),
                "direction": direction,
            }

            redis_client = RedisClient.get_client()
            payload = json.dumps(record, ensure_ascii=False)
            pipe = redis_client.pipeline(transaction=False)
            pipe.lpush(TAPE_KEY.format(instrument=tick.instrument), payload)
            pipe.ltrim(TAPE_KEY.format(instrument=tick.instrument), 0, TAPE_MAX - 1)
            if vol >= int(cfg.system.big_order_min_volume):
                big = dict(record)
                big["instrument"] = tick.instrument
                pipe.lpush(BIG_KEY.format(instrument=tick.instrument), json.dumps(big, ensure_ascii=False))
                pipe.ltrim(BIG_KEY.format(instrument=tick.instrument), 0, BIG_MAX - 1)
            pipe.execute()
        except Exception as e:
            logger.debug(f"成交明细记录失败 {tick.instrument}: {e}")

    @staticmethod
    def _infer_direction(px_chg: float, oi_chg: float) -> str:
        if oi_chg > 0:
            return "多开" if px_chg > 0 else ("空开" if px_chg < 0 else "增仓")
        if oi_chg < 0:
            return "空平" if px_chg > 0 else ("多平" if px_chg < 0 else "减仓")
        return "换手"

    def get_tape(self, instrument: str, limit: int = 100) -> list:
        try:
            raw = RedisClient.get_client().lrange(TAPE_KEY.format(instrument=instrument), 0, max(0, limit - 1))
            return [json.loads(x) for x in raw]
        except Exception as e:
            logger.warning(f"读取成交明细失败 {instrument}: {e}")
            return []

    def get_big_orders(self, instrument: Optional[str] = None, limit: int = 100) -> list:
        """大单列表: 指定合约或合并全部已订阅合约 (按时间倒序)"""
        try:
            redis_client = RedisClient.get_client()
            if instrument:
                keys = [BIG_KEY.format(instrument=instrument)]
            else:
                from backend.collector.sina_collector import get_watchlist
                keys = [BIG_KEY.format(instrument=s) for s in get_watchlist()]
            merged = []
            for k in keys:
                merged.extend(json.loads(x) for x in redis_client.lrange(k, 0, max(0, limit - 1)))
            merged.sort(key=lambda r: r.get("ts", ""), reverse=True)
            return merged[:limit]
        except Exception as e:
            logger.warning(f"读取大单失败: {e}")
            return []

    def latest_bar_trading_day(self, instrument: str) -> Optional[str]:
        """该合约 1m K线中最近的有数据的交易日"""
        session = SessionLocal()
        try:
            row = session.query(MarketBarModel.trading_day).filter_by(
                instrument_id=instrument, period="1m"
            ).order_by(MarketBarModel.trading_day.desc()).first()
            return row[0] if row else None
        finally:
            session.close()

    def synthesize_from_bars(self, instrument: str, date: str, limit: int = 500) -> List[Dict]:
        """历史日期成交明细: 由 1m K线推算分钟级现手与方向 (非逐笔, 分钟粒度)"""
        session = SessionLocal()
        try:
            rows = session.query(MarketBarModel).filter_by(
                instrument_id=instrument, period="1m", trading_day=date
            ).order_by(MarketBarModel.bar_time.asc()).limit(max(0, limit)).all()
        finally:
            session.close()
        if not rows:
            return []

        multiple, _ = get_spec(instrument)
        out: List[Dict] = []
        prev_close: Optional[float] = None
        for r in rows:
            close = float(r.close_price)
            vol = int(r.volume or 0)
            oi_chg = float(r.open_interest_change or 0)
            if prev_close is None:
                direction = "换手"
            elif oi_chg > 0:
                direction = "多开" if close > prev_close else ("空开" if close < prev_close else "增仓")
            elif oi_chg < 0:
                direction = "空平" if close > prev_close else ("多平" if close < prev_close else "减仓")
            else:
                direction = "换手"
            out.append({
                "time": r.bar_time.strftime("%H:%M:%S"),
                "ts": r.bar_time.isoformat(timespec="seconds"),
                "trading_day": r.trading_day,
                "price": close,
                "volume": vol,
                "amount": round(close * vol * multiple, 0),
                "oi_change": round(oi_chg, 0),
                "direction": direction,
            })
            prev_close = close
        # 倒序返回: 最新在最上 (与实时 Tick 明细口径一致)
        out.reverse()
        return out


tape_recorder = TapeRecorder()
