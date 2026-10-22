"""清理休市期间生成的幻影分钟线 (中金所快照无时间戳, 采集器以本地时间打戳所致)。

删除条件: 指定为中金所股指品种, bar_time 在 2026-09-13 当天且 volume=0 (休市静态快照特征)。
覆盖 redis 各周期列表与 MySQL K 线表。执行后需重启后端以清空内存中的日内聚合状态。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.storage.redis_client import RedisClient
from backend.models.database import MarketBarModel
from backend.storage.mysql_client import SessionLocal

CFFEX = ["IF2609", "IF2610", "IC2609", "IH2610", "IM2610"]
PHANTOM_DAY = "2026-09-13"
PERIODS = ["1m", "5m", "15m", "30m", "60m", "1d"]


def is_phantom(payload: dict) -> bool:
    bt = str(payload.get("bar_time", ""))
    td = str(payload.get("trading_day", ""))
    vol = int(float(payload.get("volume", 0)) )
    return (bt.startswith(PHANTOM_DAY) or td == PHANTOM_DAY.replace("-", "")) and vol == 0


def main():
    r = RedisClient.get_client()
    removed_redis = 0
    for sym in CFFEX:
        for period in PERIODS:
            key = f"bars:{sym}:{period}"
            items = r.lrange(key, 0, -1)
            if not items:
                continue
            keep = []
            for it in items:
                try:
                    d = json.loads(it)
                except Exception:
                    keep.append(it)
                    continue
                if is_phantom(d):
                    removed_redis += 1
                    continue
                keep.append(it)
            if len(keep) != len(items):
                r.delete(key)
                if keep:
                    r.rpush(key, *keep)
    print(f"redis removed {removed_redis} phantom bars")

    session = SessionLocal()
    removed_mysql = 0
    try:
        for sym in CFFEX:
            rows = session.query(MarketBarModel).filter(
                MarketBarModel.instrument_id == sym,
                MarketBarModel.bar_time >= "2026-09-13 00:00:00",
            ).all()
            for row in rows:
                if int(row.volume or 0) == 0:
                    session.delete(row)
                    removed_mysql += 1
        session.commit()
    finally:
        session.close()
    print(f"mysql removed {removed_mysql} phantom rows")


if __name__ == "__main__":
    sys.exit(main())
