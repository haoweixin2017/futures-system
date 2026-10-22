"""从引擎检查点生成展示用实时 K 线，不改动收盘数据或信号计算。"""
import json
from datetime import datetime
from decimal import Decimal
from backend.common.volume_quality import inherit_volume_quality


def forming_bar(redis_client, feed, instrument, period):
    if period == "1d":
        quote = redis_client.hgetall(f"quote:{feed}:{instrument}")
        if not quote and feed == "sina":
            quote = redis_client.hgetall(f"quote:{instrument}")
        if feed == "sina" and quote and datetime.now() >= datetime.strptime(quote["trading_day"], "%Y%m%d").replace(hour=15):
            return None
        if quote and all(quote.get(k) and Decimal(quote[k]) > 0 for k in ("day_open", "day_high", "day_low", "last_price")):
            return dict(instrument=instrument, period=period, trading_day=quote["trading_day"],
                        bar_time=datetime.strptime(quote["trading_day"], "%Y%m%d").replace(hour=15).isoformat(),
                        open_price=quote["day_open"], high_price=quote["day_high"], low_price=quote["day_low"],
                        close_price=quote["last_price"], volume=int(quote["volume"]), turnover=quote["turnover"],
                        open_interest=quote["open_interest"], open_interest_change="0", is_closed=False)
    raw = redis_client.hget("bar-engine:forming:" + feed, instrument)
    if not raw:
        return None
    state = json.loads(raw)
    minute = state.get("minute")
    if period == "1m":
        result = dict(minute) if minute else None
    else:
        base = state.get("daily") if period == "1d" else state.get("higher", {}).get(period)
        if not minute:
            result = dict(base) if base else None
        else:
            timestamp = datetime.fromisoformat(minute["bar_time"])
            if period == "1d":
                start = datetime.strptime(minute["trading_day"], "%Y%m%d").replace(hour=15)
            else:
                duration = int(period[:-1])
                start = timestamp.replace(minute=timestamp.minute // duration * duration, second=0, microsecond=0)
            if base and base["bar_time"] == start.isoformat() and base["trading_day"] == minute["trading_day"]:
                result = dict(base)
                if feed == 'sina':
                    result['provenance_json']=inherit_volume_quality(result.get('provenance_json'),base,minute)
                result["high_price"] = str(max(Decimal(base["high_price"]), Decimal(minute["high_price"])))
                result["low_price"] = str(min(Decimal(base["low_price"]), Decimal(minute["low_price"])))
                result["close_price"] = minute["close_price"]
                result["volume"] = int(base["volume"]) + int(minute["volume"])
                result["turnover"] = str(Decimal(base["turnover"]) + Decimal(minute["turnover"]))
                result["open_interest"] = minute["open_interest"]
                result["open_interest_change"] = str(Decimal(base.get("open_interest_change", "0")) + Decimal(minute.get("open_interest_change", "0")))
            else:
                result = dict(minute, period=period, bar_time=start.isoformat())
    if result:
        if feed == "sina" and period == "1d" and datetime.now() >= datetime.fromisoformat(result["bar_time"]):
            return None
        result["is_closed"] = False
    return result


def merge_forming(bars, current, date=None, limit=1000):
    merged = {bar["bar_time"]: bar for bar in bars if not date or bar["trading_day"] == date}
    if current and (not date or current["trading_day"] == date):
        merged[current["bar_time"]] = current
    return [merged[key] for key in sorted(merged)][-limit:]
