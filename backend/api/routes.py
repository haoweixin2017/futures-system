from sqlalchemy import text
import hmac
import json
import logging
import threading
from decimal import Decimal
from datetime import datetime
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, Query, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from backend.common.config import cfg, resolve_cors_origins
from backend.storage.redis_client import RedisClient
from backend.storage.mysql_client import SessionLocal, engine
from backend.models.database import MarketBarModel, SignalRecordModel, InstrumentModel
from backend.models.market_data import BarData, decode_bar_provenance
from backend.common.volume_quality import public_volume_bar
from backend.indicator_engine.indicators import IndicatorCalculator
from backend.indicator_engine.realtime_analyzer import RealtimeAnalyzer
from backend.notifier.notifier import notifier

logger = logging.getLogger(__name__)

app = FastAPI(
    title="国内期货实时分析系统 API",
    version="1.0.0",
    description="中信期货CTP实时行情、多周期K线与客观规则信号接口（只读）"
)

# 跨域: 显式列出来源。allow_origins=["*"] 与 allow_credentials=True 互斥且不安全。
app.add_middleware(
    CORSMiddleware,
    allow_origins=resolve_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)

redis_client = RedisClient.get_client()

# 认证路由随 API 应用一同挂载 (主页服务的 app 与测试用 app 保持一致)
from backend.api.auth import router as auth_router, require_user, require_live
app.include_router(auth_router)
from backend.api.ai_routes import router as ai_router
app.include_router(ai_router)
from backend.api.paper_verification_routes import router as verification_router
app.include_router(verification_router)
from backend.api.market_simulator_routes import router as market_simulator_router
app.include_router(market_simulator_router)


def _market_feed(request: Request) -> str:
    """登录角色决定行情源：模拟用户=Sina，正式用户/管理员=CTP。"""
    from backend.api.auth import get_current_user, live_mode_enabled
    user = get_current_user(request)
    return "ctp" if live_mode_enabled() and user and user.can_live_trade else "sina"


def _quote_key(feed: str, instrument: str) -> str:
    return f"quote:{feed}:{instrument}"


def _bars_key(feed: str, instrument: str, period: str) -> str:
    # 新浪历史回填一直使用 legacy 键；它本身就是模拟用户专属数据。
    # CTP 必须独立命名，避免覆盖新浪历史。
    return f"bars:{instrument}:{period}" if feed == "sina" else f"bars:{feed}:{instrument}:{period}"


def _analysis_bar(data: Dict[str, Any]) -> BarData:
    return BarData(
        instrument=data["instrument"],
        trading_day=data["trading_day"],
        period=data["period"],
        bar_time=datetime.fromisoformat(data["bar_time"]),
        open_price=Decimal(str(data["open_price"])),
        high_price=Decimal(str(data["high_price"])),
        low_price=Decimal(str(data["low_price"])),
        close_price=Decimal(str(data["close_price"])),
        volume=int(data["volume"]),
        turnover=Decimal(str(data["turnover"])),
        open_interest=Decimal(str(data["open_interest"])),
        open_interest_change=Decimal(str(data.get("open_interest_change", "0"))),
        provenance_json=decode_bar_provenance(data.get('provenance_json')),
    )


def _load_analysis_bars(instrument: str, period: str, limit: int) -> List[BarData]:
    """读取模拟盘已闭合 Bar；不合并 forming_bar，避免未闭合量能触发正式信号。"""
    merged: Dict[datetime, BarData] = {}
    session = SessionLocal()
    try:
        rows = session.query(MarketBarModel).filter_by(
            instrument_id=instrument, period=period
        ).order_by(MarketBarModel.bar_time.desc()).limit(limit).all()
        for row in reversed(rows):
            bar = BarData(
                instrument=row.instrument_id, trading_day=row.trading_day, period=row.period,
                bar_time=row.bar_time, open_price=row.open_price, high_price=row.high_price,
                low_price=row.low_price, close_price=row.close_price, volume=row.volume,
                turnover=row.turnover, open_interest=row.open_interest,
                open_interest_change=row.open_interest_change,
                provenance_json=decode_bar_provenance(getattr(row,'provenance_json',None)),
            )
            merged[bar.bar_time] = bar
    finally:
        session.close()
    try:
        raw_bars = redis_client.lrange(_bars_key("sina", instrument, period), -limit, -1)
    except Exception:
        raw_bars = []
    for raw in raw_bars:
        try:
            bar = _analysis_bar(json.loads(raw))
            merged[bar.bar_time] = bar
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("忽略无法解析的分析K线: %s %s", instrument, period)
    return sorted(merged.values(), key=lambda item: item.bar_time)[-limit:]

@app.get("/api/v1/health")
def health_check():
    redis_ok = False
    mysql_ok = False
    try:
        redis_ok = redis_client.ping()
    except Exception:
        pass

    try:
        session = SessionLocal()
        session.execute(text("SELECT 1"))
        session.close()
        mysql_ok = True
    except Exception:
        pass

    ctp_enabled = (cfg.system.feed or "").strip().lower() == "ctp"
    return {
        "status": "healthy" if (redis_ok and mysql_ok) else "degraded",
        "redis": "connected" if redis_ok else "disconnected",
        "mysql": "connected" if mysql_ok else "disconnected",
        "profile": cfg.system.profile,
        "feed": "ctp" if ctp_enabled else "sina",
        "feeds": {"sim": "sina", "live": "ctp"} if ctp_enabled else {"sim": "sina"},
        "alerts": notifier.get_channel_status(),
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/api/v1/instruments")
def get_instruments():
    return {
        "instruments": [inst.model_dump() for inst in cfg.instruments]
    }


def verify_admin_token(token: Optional[str]) -> None:
    """校验维护接口令牌。未配置令牌时一律拒绝 (fail-closed), 并使用恒定时间比较。"""
    expected = (getattr(cfg.system, "maintenance_token", "") or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="维护接口未启用: 请先配置 system.maintenance_token 或环境变量 FUTURES_MAINTENANCE_TOKEN"
        )
    supplied = (token or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Forbidden: Invalid or missing X-Admin-Token")

@app.get("/api/v1/quotes/{instrument}")
def get_quote(instrument: str, request: Request):
    feed = _market_feed(request)
    data = redis_client.hgetall(_quote_key(feed, instrument))
    if not data and feed == "sina":
        data = redis_client.hgetall(f"quote:{instrument}")
    if not data:
        raise HTTPException(status_code=404, detail=f"{feed.upper()} Quote not found for instrument")
    return {**data, "feed": feed}

def cached_bar(raw):
    bar = json.loads(raw)
    if bar["period"] == "1m":
        from backend.common.trading_time import FuturesTradingCalendar
        bar["trading_day"] = FuturesTradingCalendar.get_trading_day(datetime.fromisoformat(bar["bar_time"]))
    return bar


@app.get("/api/v1/bars/{instrument}")
def get_bars(
    request: Request,
    instrument: str,
    period: str = Query("15m", pattern="^(1m|5m|15m|30m|60m|1d)$"),
    limit: int = Query(200, ge=1, le=1000),
    date: Optional[str] = Query(None, pattern="^\\d{8}$", description="按交易日过滤 (YYYYMMDD), 用于分时回看历史")
):
    from backend.bar_engine.live_bars import forming_bar, merge_forming
    feed = _market_feed(request)
    current = forming_bar(redis_client, feed, instrument, period)
    # 指定交易日: 直接从历史库按 trading_day 精确取当日全部分钟线
    if date:
        session = SessionLocal()
        try:
            rows = [] if feed == "ctp" else session.query(MarketBarModel).filter_by(
                instrument_id=instrument, period=period, trading_day=date
            ).order_by(MarketBarModel.bar_time.asc()).limit(1000).all()
            bars = [
                {
                    "instrument": r.instrument_id,
                    "trading_day": r.trading_day,
                    "period": r.period,
                    "bar_time": r.bar_time.isoformat(),
                    "open_price": str(r.open_price),
                    "high_price": str(r.high_price),
                    "low_price": str(r.low_price),
                    "close_price": str(r.close_price),
                    "volume": r.volume,
                    "turnover": str(r.turnover),
                    "open_interest": str(r.open_interest),
                    "open_interest_change": str(r.open_interest_change),
                    "provenance_json": r.provenance_json,
                }
                for r in rows
            ]
            cache_key = _bars_key(feed, instrument, period)
            raw_cached = redis_client.lrange(cache_key, -1000, -1)
            if not raw_cached and feed == "sina":
                raw_cached = redis_client.lrange(f"bars:{instrument}:{period}", -1000, -1)
            cached = [cached_bar(raw) for raw in raw_cached]
            bars = merge_forming(bars + cached, current, date=date, limit=limit)
            if feed == 'sina':
                bars = [public_volume_bar(b) for b in bars]
            return {
                "instrument": instrument, "period": period, "count": len(bars),
                "source": "merged" if any(b["trading_day"] == date for b in cached) or (current and current["trading_day"] == date) else "mysql",
                "date": date, "feed": feed, "bars": bars
            }
        finally:
            session.close()

    """合并查询: MySQL 历史全量 + Redis 实时近端, 按时间去重后返回最近 limit 根"""
    merged: Dict[str, Dict[str, Any]] = {}
    mysql_count = 0

    session = SessionLocal()
    try:
        rows = [] if feed == "ctp" else session.query(MarketBarModel).filter_by(
            instrument_id=instrument,
            period=period
        ).order_by(MarketBarModel.bar_time.desc()).limit(limit).all()
        for r in rows:
            mysql_count += 1
            merged[r.bar_time.isoformat()] = {
                "instrument": r.instrument_id,
                "trading_day": r.trading_day,
                "period": r.period,
                "bar_time": r.bar_time.isoformat(),
                "open_price": str(r.open_price),
                "high_price": str(r.high_price),
                "low_price": str(r.low_price),
                "close_price": str(r.close_price),
                "volume": r.volume,
                "turnover": str(r.turnover),
                "open_interest": str(r.open_interest),
                "open_interest_change": str(r.open_interest_change),
                "provenance_json": r.provenance_json,
            }
    finally:
        session.close()

    # Redis 中较新的实时K线优先 (可能与 MySQL 部分重叠, 以 Redis 为准)
    try:
        raw_bars = redis_client.lrange(_bars_key(feed, instrument, period), -limit, -1)
        if not raw_bars and feed == "sina":
            raw_bars = redis_client.lrange(f"bars:{instrument}:{period}", -limit, -1)
    except Exception:
        raw_bars = []
    redis_count = 0
    for b in raw_bars:
        d = cached_bar(b)
        merged[d["bar_time"]] = d
        redis_count += 1

    bars = merge_forming(list(merged.values()), current, limit=limit)
    if feed == 'sina':
        bars = [public_volume_bar(b) for b in bars]
    if mysql_count and redis_count:
        source = "merged"
    elif redis_count:
        source = "redis"
    else:
        source = "mysql"
    return {
        "instrument": instrument,
        "period": period,
        "count": len(bars),
        "source": source,
        "feed": feed,
        "bars": bars
    }

@app.get("/api/v1/bars/{instrument}/dates")
def get_bar_dates(
    request: Request,
    instrument: str,
    period: str = Query("1m", pattern="^(1m|5m|15m|30m|60m|1d)$")
):
    """该合约某周期可用的交易日列表 (升序, 供分时按日期切换)"""
    feed = _market_feed(request)
    if feed == "ctp":
        raw = redis_client.lrange(_bars_key(feed, instrument, period), 0, -1)
        dates = sorted({cached_bar(item)["trading_day"] for item in raw})
        return {"instrument": instrument, "period": period, "feed": feed, "dates": dates}
    session = SessionLocal()
    try:
        rows = session.query(MarketBarModel.trading_day).filter_by(
            instrument_id=instrument, period=period
        ).distinct().order_by(MarketBarModel.trading_day.asc()).all()
        return {"instrument": instrument, "period": period, "dates": [r[0] for r in rows]}
    finally:
        session.close()

@app.get("/api/v1/indicators/{instrument}")
def get_indicators(
    request: Request,
    instrument: str,
    period: str = Query("15m", pattern="^(1m|5m|15m|30m|60m|1d)$")
):
    # 优先读取已计算好的缓存
    feed = _market_feed(request)
    cached = redis_client.get(f"indicators:v2:{feed}:{instrument}:{period}")
    if not cached and feed == "sina":
        cached = redis_client.get(f"indicators:v2:{instrument}:{period}")
    if cached:
        return json.loads(cached)

    # 若无缓存则优先从 Redis 获取近 200 根 Bar，若 Redis 为空则从 MySQL 回退加载
    key = _bars_key(feed, instrument, period)
    raw_bars = redis_client.lrange(key, -200, -1)
    if not raw_bars and feed == "sina":
        raw_bars = redis_client.lrange(f"bars:{instrument}:{period}", -200, -1)
    bars_obj = []
    if raw_bars:
        for b in raw_bars:
            d = json.loads(b)
            bars_obj.append(BarData(
                instrument=d["instrument"],
                trading_day=d["trading_day"],
                period=d["period"],
                bar_time=datetime.fromisoformat(d["bar_time"]),
                open_price=Decimal(d["open_price"]),
                high_price=Decimal(d["high_price"]),
                low_price=Decimal(d["low_price"]),
                close_price=Decimal(d["close_price"]),
                volume=int(d["volume"]),
                turnover=Decimal(d["turnover"]),
                open_interest=Decimal(d["open_interest"]),
                open_interest_change=Decimal(d.get("open_interest_change", "0")),
                provenance_json=decode_bar_provenance(d.get('provenance_json')),
            ))
    elif feed == "sina":
        session = SessionLocal()
        try:
            rows = session.query(MarketBarModel).filter_by(
                instrument_id=instrument,
                period=period
            ).order_by(MarketBarModel.bar_time.desc()).limit(200).all()
            rows.reverse()
            for r in rows:
                bars_obj.append(BarData(
                    instrument=r.instrument_id,
                    trading_day=r.trading_day,
                    period=r.period,
                    bar_time=r.bar_time,
                    open_price=r.open_price,
                    high_price=r.high_price,
                    low_price=r.low_price,
                    close_price=r.close_price,
                    volume=r.volume,
                    turnover=r.turnover,
                    open_interest=r.open_interest,
                    open_interest_change=r.open_interest_change,
                    provenance_json=decode_bar_provenance(getattr(r,'provenance_json',None)),
                ))
        finally:
            session.close()

    if not bars_obj:
        raise HTTPException(status_code=404, detail="No bars available for indicator calculation")

    from backend.common.product_specs import get_spec
    vol_mult = get_spec(instrument)[0]
    bars_obj = sorted({b.bar_time: b for b in bars_obj}.values(), key=lambda b: b.bar_time)
    for inst in cfg.instruments:
        if inst.symbol == instrument:
            vol_mult = inst.volume_multiple
            break
    indicators = IndicatorCalculator.compute_all_indicators(bars_obj, volume_multiple=vol_mult)
    # 新浪历史库含旧版本估算成交额，来源无法核实时不展示为精确 VWAP。
    if cfg.system.feed == "sina":
        indicators["vwap"] = None
        indicators["vwap_status"] = "missing_turnover"
    # 写入缓存
    redis_client.setex(f"indicators:v2:{instrument}:{period}", 60, json.dumps(indicators))
    return indicators


@app.get("/api/v1/analysis/{instrument}")
def get_realtime_analysis(instrument: str, request: Request):
    """模拟用户量价分析；只读计算，不写信号表、不调用自动跟单。"""
    from backend.api.auth import require_user
    from backend.common.product_specs import get_spec
    from backend.common.trading_time import FuturesTradingCalendar

    user = require_user(request)
    if user.role != "sim":
        raise HTTPException(status_code=403, detail="该分析接口仅供模拟用户使用")
    instrument = instrument.strip().upper()
    quote = redis_client.hgetall(_quote_key("sina", instrument))
    if not quote:
        quote = redis_client.hgetall(f"quote:{instrument}")
    if not quote:
        raise HTTPException(status_code=404, detail="未找到该合约的模拟行情")

    cache_key = f"analysis:v1:sina:{instrument}:5m"
    cached = redis_client.get(cache_key)
    if cached:
        try:
            body = json.loads(cached)
            quote_at = datetime.fromisoformat(str(quote.get("timestamp")))
            if (body.get("data", {}).get("quote_time") == quote.get("timestamp")
                    and (datetime.now() - quote_at).total_seconds() <= RealtimeAnalyzer.SINA_STALE_SECONDS
                    and FuturesTradingCalendar.is_trading_time_for(datetime.now(), instrument)):
                return body
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    daily_bars = _load_analysis_bars(instrument, "1d", 60)
    bars_5m = _load_analysis_bars(instrument, "5m", 2500)
    bars_1m = _load_analysis_bars(instrument, "1m", 4000)
    from backend.bar_engine.live_bars import forming_bar
    forming_1m = forming_bar(redis_client, "sina", instrument, "1m")
    volume_multiple, tick_value = get_spec(instrument)
    instrument_name = instrument
    for configured in cfg.instruments:
        if configured.symbol.upper() == instrument:
            instrument_name = configured.name
            volume_multiple = configured.volume_multiple
            tick_value = configured.price_tick
            break
    else:
        session = SessionLocal()
        try:
            row = session.query(InstrumentModel).filter_by(symbol=instrument).first()
            if row:
                instrument_name = row.name or instrument
                volume_multiple = row.volume_multiple or volume_multiple
                tick_value = row.price_tick or tick_value
        finally:
            session.close()

    analysis = RealtimeAnalyzer.analyze(
        instrument=instrument,
        instrument_name=instrument_name,
        quote=quote,
        daily_bars=daily_bars,
        bars_5m=bars_5m,
        volume_multiple=int(volume_multiple),
        price_tick=Decimal(str(tick_value)),
        feed="sina",
        bars_1m=bars_1m,
        forming_1m=forming_1m,
    )
    body = {"code": 0, "data": analysis}
    redis_client.set(cache_key, json.dumps(body, ensure_ascii=False), ex=3)
    return body

@app.get("/api/v1/signals/{instrument}")
def get_signal(instrument: str):
    sig = redis_client.hgetall(f"signal:{instrument}")
    if not sig:
        return {"instrument": instrument, "signal": "NONE", "rule_score": 0}
    if "reasons" in sig:
        sig["reasons"] = json.loads(sig["reasons"])
    return sig

@app.get("/api/v1/signals/{instrument}/explanation")
def explain_signal(instrument: str):
    sig = redis_client.hgetall(f"signal:{instrument}")
    if not sig:
        return {"instrument": instrument, "has_signal": False, "summary": "当前无有效规则信号"}

    reasons = json.loads(sig.get("reasons", "[]"))
    passed_rules = [r["rule"] for r in reasons if r.get("passed")]
    score = int(sig.get("rule_score", 0))

    explanation = (
        f"合约 {instrument} 在 {sig.get('timeframe')} 周期触发 {sig.get('signal')} 观察。"
        f"综合客观评分: {score}/100。"
        f"触发价: {sig.get('trigger_price')}，失效价: {sig.get('invalid_price')}。"
        f"满足的核心条件: {', '.join(passed_rules)}。"
    )

    return {
        "instrument": instrument,
        "signal": sig.get("signal"),
        "rule_score": score,
        "explanation": explanation,
        "reasons": reasons
    }

@app.get("/api/v1/events")
def get_system_events(limit: int = Query(50, ge=1, le=200)):
    from backend.models.database import SystemEventModel
    session = SessionLocal()
    try:
        from backend.notifier.notifier import PRIVATE_TRADE_SOURCES
        rows = session.query(SystemEventModel).filter(~SystemEventModel.source.in_(PRIVATE_TRADE_SOURCES)).order_by(SystemEventModel.event_time.desc()).limit(limit).all()
        return {
            "events": [
                {
                    "id": r.id,
                    "event_type": r.event_type,
                    "severity": r.severity,
                    "source": r.source,
                    "message": r.message,
                    "details": json.loads(r.details_json) if r.details_json else None,
                    "event_time": r.event_time.isoformat()
                }
                for r in rows
            ]
        }
    finally:
        session.close()

@app.get("/api/v1/market/summary")
def get_market_summary():
    result = []
    for inst in cfg.instruments:
        symbol = inst.symbol
        q = redis_client.hgetall(f"quote:{symbol}")
        sig = redis_client.hgetall(f"signal:{symbol}")
        result.append({
            "symbol": symbol,
            "name": inst.name,
            "exchange": inst.exchange,
            "last_price": q.get("last_price", "--"),
            "volume": q.get("volume", "--"),
            "open_interest": q.get("open_interest", "--"),
            "signal": sig.get("signal", "NONE"),
            "rule_score": sig.get("rule_score", 0)
        })
    return {"market_summary": result}

@app.post("/api/v1/maintenance/clean-ticks")
def trigger_clean_ticks(
    days: Optional[int] = Query(None, ge=1, le=365, description="保留天数"),
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token")
):
    verify_admin_token(x_admin_token)

    from backend.storage.retention import retention_manager
    deleted = retention_manager.clean_expired_ticks(days=days)
    return {
        "status": "success",
        "deleted_ticks": deleted,
        "retention_days": days or getattr(cfg.system, "tick_retention_days", 7),
        "timestamp": datetime.utcnow().isoformat()
    }


# ----------------- 全市场合约、订阅列表与行情源状态 -----------------
from backend.storage.universe_sync import get_universe_cache, get_universe_symbols
from backend.collector.sina_collector import WATCHLIST_KEY, get_watchlist
from backend.storage import history_backfill

# 订阅分组: Redis 单键 JSON {分组名: [合约...]}, 未归组的合约显示在"默认"组
GROUPS_KEY = "watchlist:groups"
DEFAULT_GROUP = "默认"
WATCHLIST_NAME_MAX_LENGTH = 32
GROUP_NAME_MAX_LENGTH = 16


def _safe_watchlist_label(value: Any, field: str, max_length: int) -> str:
    name = str(value or "").strip()
    if not name or len(name) > max_length or any(ord(ch) < 32 or ch in "<>&\"'" for ch in name):
        raise HTTPException(status_code=400, detail=f"{field}无效 (1-{max_length} 字符，不能包含 HTML 特殊字符)")
    return name
# 排序持久化: watchlist:order (合约全局顺序), watchlist:group_order (分组顺序)
ORDER_KEY = "watchlist:order"
GROUP_ORDER_KEY = "watchlist:group_order"


def _load_groups() -> Dict[str, list]:
    raw = redis_client.get(GROUPS_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _save_groups(groups: Dict[str, list]):
    redis_client.set(GROUPS_KEY, json.dumps(groups, ensure_ascii=False))


def _group_of(symbol: str, groups: Dict[str, list]) -> str:
    for name, members in groups.items():
        if symbol in members:
            return name
    return DEFAULT_GROUP


def _load_order() -> list:
    raw = redis_client.get(ORDER_KEY)
    try:
        data = json.loads(raw) if raw else []
    except Exception:
        data = []
    return data if isinstance(data, list) else []


def _save_order(order: list):
    redis_client.set(ORDER_KEY, json.dumps(order, ensure_ascii=False))


def _ordered_symbols() -> list:
    """订阅合约按持久化顺序返回 (新加入的排在末尾)"""
    members = get_watchlist()
    order = [s for s in _load_order() if s in set(members)]
    return order + [s for s in members if s not in order]


def _load_group_order() -> list:
    raw = redis_client.get(GROUP_ORDER_KEY)
    try:
        data = json.loads(raw) if raw else []
    except Exception:
        data = []
    return data if isinstance(data, list) else []


def _save_group_order(order: list):
    redis_client.set(GROUP_ORDER_KEY, json.dumps(order, ensure_ascii=False))

@app.get("/api/v1/universe/categories")
def get_universe_categories():
    """品种类别列表 (按品种前缀聚合全市场快照, 供搜索框无输入时展示)"""
    from backend.common.product_specs import extract_product
    universe = {k: v for k, v in get_universe_cache().items() if k != "__updated_at__"}
    cats: Dict[str, dict] = {}
    for c in universe.values():
        sym = str(c.get("symbol", ""))
        name = str(c.get("name", ""))
        if not sym or "主连" in name or "连续" in name:
            continue
        product = extract_product(sym)
        if not product or product == sym:
            continue
        if product not in cats:
            import re as _re
            clean_name = _re.sub(r"\d+$", "", name).strip() or product
            cats[product] = {"product": product, "name": clean_name, "exchange": c.get("exchange", ""), "count": 0}
        cats[product]["count"] += 1
    items = sorted(cats.values(), key=lambda x: (x["exchange"], x["product"]))
    return {"total": len(items), "categories": items}


@app.get("/api/v1/universe")
def get_universe(
    search: Optional[str] = Query(None, max_length=32, description="按代码或名称模糊搜索"),
    product: Optional[str] = Query(None, max_length=8, description="按品种前缀过滤, 如 JM / AP"),
    exchange: Optional[str] = Query(None, pattern="^(SHFE|DCE|CZCE|INE|CFFEX|GFEX)$"),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0)
):
    """全市场活跃合约浏览/搜索 (数据来自定期同步的快照)"""
    universe = {k: v for k, v in get_universe_cache().items() if k != "__updated_at__"}
    items = list(universe.values())
    if exchange:
        items = [c for c in items if c.get("exchange") == exchange]
    if product:
        p = product.strip().upper()
        items = [c for c in items if str(c.get("symbol", "")).upper().startswith(p) and c.get("symbol", "").upper() != p]
    if search:
        kw = search.strip().upper()
        items = [c for c in items if kw in c.get("symbol", "").upper() or kw in c.get("name", "")]
    items.sort(key=lambda c: (c.get("exchange", ""), c.get("symbol", "")))
    total = len(items)
    page = items[offset:offset + limit]
    watch = set(get_watchlist())
    for c in page:
        c["subscribed"] = c["symbol"] in watch
    return {"total": total, "updated_at": get_universe_cache().get("__updated_at__"), "contracts": page}

def _find_dominant_contract(product: str) -> Optional[str]:
    """从全市场快照中判定品种主力合约 (持仓量最大者, 排除主连等合成符号)"""
    universe = get_universe_cache()
    best, best_oi = None, -1
    for c in universe.values():
        if not isinstance(c, dict):
            continue
        sym = str(c.get("symbol", ""))
        name = str(c.get("name", ""))
        if not sym.startswith(product) or sym == product:
            continue
        # 剩余部分必须是纯数字月份 (排除 SAM/JM0 这类合成符号), 且排除主连名称行
        suffix = sym[len(product):]
        if not suffix.isdigit() or suffix == "0" or "主连" in name or "连续" in name:
            continue
        oi = c.get("open_interest")
        try:
            oi = int(oi) if oi not in (None, "-") else -1
        except Exception:
            oi = -1
        if oi > best_oi:
            best, best_oi = sym, oi
    return best if best_oi > 0 else None


@app.get("/api/v1/watchlist")
def get_watchlist_api(request: Request):
    """当前实时订阅的合约列表 (按持久化顺序; 含最新快照价、主力判定与所属分组)"""
    symbols = _ordered_symbols()
    groups = _load_groups()
    gorder = _load_group_order()
    groups = {g: groups[g] for g in [g for g in gorder if g in groups] + [g for g in groups if g not in gorder]}
    universe = {k: v for k, v in get_universe_cache().items() if k != "__updated_at__"}
    custom_names = redis_client.hgetall("watchlist:names") or {}
    dominant_cache: Dict[str, Optional[str]] = {}
    feed = _market_feed(request)
    result = []
    for s in symbols:
        q = redis_client.hgetall(_quote_key(feed, s)) or {}
        if not q and feed == "sina":
            q = redis_client.hgetall(f"quote:{s}") or {}
        meta = universe.get(s, {})
        last_price = q.get("last_price") or meta.get("last_price")
        from backend.common.product_specs import extract_product
        product = extract_product(s)
        if product not in dominant_cache:
            dominant_cache[product] = _find_dominant_contract(product)
        display_name = custom_names.get(s) or meta.get("name", "")
        result.append({
            "symbol": s,
            "name": display_name,
            "exchange": meta.get("exchange", q.get("exchange", "")),
            "last_price": last_price,
            "prev_settle": q.get("prev_settle"),
            "open_interest": q.get("open_interest") or meta.get("open_interest"),
            "timestamp": q.get("timestamp"),
            "dominant_contract": dominant_cache[product],
            "group": _group_of(s, groups),
            "groups": [g for g, members in groups.items() if s in members] or [_group_of(s, groups)],
        })
    return {"watchlist": result, "groups": list(groups.keys()), "feed": feed}


@app.post("/api/v1/watchlist/rename", dependencies=[Depends(require_user)])
def rename_watchlist_symbol(body: dict):
    """重命名订阅合约的显示名称 (留空恢复默认名称)"""
    symbol = str(body.get("symbol", "")).strip().upper()
    raw_name = str(body.get("name", "")).strip()
    name = _safe_watchlist_label(raw_name, "合约名称", WATCHLIST_NAME_MAX_LENGTH) if raw_name else ""
    if symbol not in get_watchlist():
        raise HTTPException(status_code=404, detail=f"合约 {symbol} 不在订阅列表中")
    if name:
        redis_client.hset("watchlist:names", symbol, name)
    else:
        redis_client.hdel("watchlist:names", symbol)
    return {"status": "success", "symbol": symbol, "name": name}


@app.post("/api/v1/watchlist/group", dependencies=[Depends(require_user)])
def create_watchlist_group(body: dict):
    """新建订阅分组"""
    name = _safe_watchlist_label(body.get("name"), "分组名称", GROUP_NAME_MAX_LENGTH)
    groups = _load_groups()
    if name not in groups:
        groups[name] = []
        _save_groups(groups)
    gorder = _load_group_order()
    if name not in gorder:
        gorder.append(name)
        _save_group_order(gorder)
    return {"status": "success", "groups": list(groups.keys())}


@app.post("/api/v1/watchlist/assign-groups", dependencies=[Depends(require_user)])
def assign_watchlist_groups(body: dict):
    """合约分配到多个分组 (集合式: 以提交的分组列表为准, 空列表 = 默认分组)"""
    symbol = str(body.get("symbol", "")).strip().upper()
    wanted = []
    for g in body.get("groups", []):
        name = _safe_watchlist_label(g, "分组名称", GROUP_NAME_MAX_LENGTH)
        if name and name not in wanted:
            wanted.append(name)
    if not wanted:
        wanted = [DEFAULT_GROUP]
    if symbol not in get_watchlist():
        raise HTTPException(status_code=404, detail=f"合约 {symbol} 不在订阅列表中")
    groups = _load_groups()
    # 从现有分组中移除
    for g, members in groups.items():
        if symbol in members:
            members.remove(symbol)
    # 加入目标分组 (不存在的分组自动创建)
    for g in wanted:
        groups.setdefault(g, [])
        if symbol not in groups[g]:
            groups[g].append(symbol)
    _save_groups(groups)
    # 同步分组顺序 (新分组排在最后)
    gorder = _load_group_order()
    if any(g not in gorder for g in wanted):
        gorder.extend([g for g in wanted if g not in gorder])
        _save_group_order(gorder)
    return {"status": "success", "symbol": symbol, "groups": wanted}


@app.post("/api/v1/watchlist/assign", dependencies=[Depends(require_user)])
def assign_watchlist_group(body: dict):
    """将合约分配到指定分组 (合约需已在订阅列表中)"""
    symbol = str(body.get("symbol", "")).strip().upper()
    raw_group = str(body.get("group", "")).strip()
    group = _safe_watchlist_label(raw_group, "分组名称", GROUP_NAME_MAX_LENGTH) if raw_group else DEFAULT_GROUP
    if symbol not in get_watchlist():
        raise HTTPException(status_code=404, detail=f"合约 {symbol} 不在订阅列表中")
    groups = _load_groups()
    for g, members in groups.items():
        if symbol in members:
            members.remove(symbol)
    groups.setdefault(group, [])
    if symbol not in groups[group]:
        groups[group].append(symbol)
    # 清理空的自定义分组 (默认组除外)
    for g in [g for g, m in groups.items() if g != DEFAULT_GROUP and not m]:
        groups.pop(g)
    _save_groups(groups)
    return {"status": "success", "symbol": symbol, "group": group, "groups": list(groups.keys())}


@app.post("/api/v1/watchlist/group/rename", dependencies=[Depends(require_user)])
def rename_watchlist_group(body: dict):
    """重命名分组 (组内合约与分组顺序保持不变)"""
    old = str(body.get("old", "")).strip()
    raw_new = str(body.get("new", "")).strip()
    new = _safe_watchlist_label(raw_new, "分组名称", GROUP_NAME_MAX_LENGTH) if raw_new else ""
    if old == DEFAULT_GROUP:
        raise HTTPException(status_code=400, detail="默认分组不可重命名")
    if not old or not new:
        raise HTTPException(status_code=400, detail="分组名称无效")
    groups = _load_groups()
    if old not in groups:
        raise HTTPException(status_code=404, detail=f"分组 {old} 不存在")
    if new in groups:
        raise HTTPException(status_code=400, detail=f"分组 {new} 已存在")
    members = groups.pop(old)
    groups[new] = members
    _save_groups(groups)
    order = [new if g == old else g for g in _load_group_order()]
    _save_group_order(order)
    return {"status": "success", "old": old, "new": new}


@app.delete("/api/v1/watchlist/group/{name}", dependencies=[Depends(require_user)])
def delete_watchlist_group(name: str):
    """删除分组 (组内合约回到默认分组)"""
    if name == DEFAULT_GROUP:
        raise HTTPException(status_code=400, detail="默认分组不可删除")
    groups = _load_groups()
    if name not in groups:
        raise HTTPException(status_code=404, detail=f"分组 {name} 不存在")
    groups.pop(name)
    _save_groups(groups)
    _save_group_order([g for g in _load_group_order() if g != name])
    return {"status": "success", "groups": list(groups.keys())}


@app.post("/api/v1/watchlist/reorder", dependencies=[Depends(require_user)])
def reorder_watchlist(body: dict):
    """拖动排序: 按前端提交的顺序持久化订阅合约顺序"""
    symbols = [str(s).strip().upper() for s in body.get("symbols", [])]
    members = set(get_watchlist())
    cleaned: list = []
    for s in symbols:
        if s in members and s not in cleaned:
            cleaned.append(s)
    for s in get_watchlist():
        if s not in cleaned:
            cleaned.append(s)
    _save_order(cleaned)
    return {"status": "success", "order": cleaned}


@app.post("/api/v1/watchlist/group/reorder", dependencies=[Depends(require_user)])
def reorder_watchlist_groups(body: dict):
    """拖动排序: 按前端提交的顺序持久化分组顺序"""
    names = [str(g).strip() for g in body.get("groups", [])]
    groups = _load_groups()
    cleaned = [g for g in names if g in groups]
    for g in groups:
        if g not in cleaned:
            cleaned.append(g)
    _save_group_order(cleaned)
    return {"status": "success", "groups": cleaned}

@app.post("/api/v1/watchlist", dependencies=[Depends(require_user)])
def add_watchlist(body: dict):
    """订阅新合约: 加入实时采集列表并自动回补历史K线"""
    symbol = str(body.get("symbol", "")).strip().upper()
    if not symbol or not symbol.isalnum():
        raise HTTPException(status_code=400, detail="无效合约代码")
    redis_client.sadd(WATCHLIST_KEY, symbol)
    order = _load_order()
    if symbol not in order:
        order.append(symbol)
        _save_order(order)

    # 主连等未在全市场列表中的符号: 登记名称与交易所, 供订阅列表展示
    if not redis_client.hexists("universe:contracts", symbol):
        from backend.common.product_specs import extract_product, get_exchange
        product = extract_product(symbol)
        is_main = symbol.endswith("0") and bool(product) and symbol != product
        name = f"{product}主连" if is_main else symbol
        redis_client.hset(
            "universe:contracts", symbol,
            json.dumps({
                "symbol": symbol,
                "exchange": get_exchange(symbol) or "",
                "name": name,
                "last_price": None,
                "volume": 0,
                "open_price": None,
                "prev_settle": None,
            }, ensure_ascii=False),
        )

    backfilled = {"triggered": False}
    if cfg.system.backfill_on_subscribe:
        def _task():
            try:
                history_backfill.ensure_symbol_backfilled(symbol, full_history=True)
            except Exception as e:
                logger.warning(f"订阅回填失败 {symbol}: {e}")
        threading.Thread(target=_task, daemon=True).start()
        backfilled["triggered"] = True
    # CTP 订阅接口不是轮询式，新增关注合约后需要显式追加订阅。
    try:
        import backend.main as runtime
        if runtime.ctp_collector:
            runtime.ctp_collector.subscribe(symbol)
    except Exception as e:
        logger.warning("CTP 追加订阅失败 %s: %s", symbol, e)
    return {"status": "success", "symbol": symbol, "backfill": backfilled}

@app.delete("/api/v1/watchlist/{symbol}", dependencies=[Depends(require_user)])
def remove_watchlist(symbol: str):
    """取消订阅合约 (移出实时采集列表, 历史数据保留)"""
    symbol = symbol.upper()
    redis_client.srem(WATCHLIST_KEY, symbol)
    _save_order([s for s in _load_order() if s != symbol])
    return {"status": "success", "symbol": symbol}

@app.get("/api/v1/feed-status")
def get_feed_status(request: Request):
    """行情源运行状态: 类型、最近轮询/成交时间、休市判定与数据新鲜度"""
    import time as _time
    feed = _market_feed(request)
    status = redis_client.hgetall(f"feed:status:{feed}") or {}
    if feed == "sina":
        # 兼容旧版本写入的全市场同步字段；同名字段以新浪专用状态为准。
        status = {**(redis_client.hgetall("feed:status") or {}), **status}
    now = _time.time()

    last_tick_raw = status.get("last_tick_at") or ""
    tick_age = None
    if last_tick_raw:
        try:
            from datetime import datetime as _dt
            tick_age = round(now - _dt.fromisoformat(last_tick_raw).timestamp(), 1)
        except Exception:
            tick_age = None

    market_open = False
    try:
        from backend.common.trading_time import FuturesTradingCalendar
        market_open = FuturesTradingCalendar.is_trading_time(datetime.now())
    except Exception:
        pass

    return {
        "feed": feed,
        "running": status.get("running") == "1",
        "connected": status.get("connected") == "1" if feed == "ctp" else None,
        "logged_in": status.get("logged_in") == "1" if feed == "ctp" else None,
        "market_open": market_open,
        "poll_interval": cfg.system.poll_interval,
        "last_poll_at": status.get("last_poll_at"),
        "last_tick_at": last_tick_raw or None,
        "last_tick_age_seconds": tick_age,
        "watchlist_count": int(status.get("watchlist_count") or 0),
        "universe_count": int(status.get("universe_count") or 0),
        "universe_updated_at": status.get("universe_updated_at"),
        "last_error": status.get("last_error")
    }

@app.post("/api/v1/maintenance/backfill")
def trigger_backfill(
    body: dict,
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token")
):
    """手动触发历史K线回填: {"symbol": "JM2701"} 单合约全周期 / {"all_daily": true} 全市场日线"""
    verify_admin_token(x_admin_token)

    symbol = str(body.get("symbol", "")).strip().upper()
    if symbol:
        def _task():
            try:
                history_backfill.backfill_symbol(symbol)
            except Exception:
                logger.exception("后台回补 %s 失败", symbol)
        threading.Thread(target=_task, name=f"Backfill-{symbol}", daemon=True).start()
        return {"status": "success", "message": f"已后台回补 {symbol} 全周期历史K线"}

    if body.get("all_daily"):
        def _task():
            try:
                history_backfill.backfill_universe_daily()
            except Exception:
                logger.exception("后台全市场日线回填失败")
        threading.Thread(target=_task, name="Backfill-UniverseDaily", daemon=True).start()
        return {"status": "success", "message": "已后台启动全市场日线历史回填 (限速执行)"}

    raise HTTPException(status_code=400, detail="请提供 symbol 或 all_daily=true")


# ----------------- 交易执行与风控接口 (Paper Trading API, 需登录) -----------------
from backend.trade_engine.models import OrderRequest, Direction, OrderStatus
from backend.trade_engine.paper_accounts import paper_accounts


def current_paper_account(user=Depends(require_user)):
    return paper_accounts.get(user.username)


@app.post("/api/v1/trade/order", dependencies=[Depends(require_user)])
def submit_order(order_req: OrderRequest, account=Depends(current_paper_account)):
    """提交买卖报单 (支持限价买开、卖平、卖开、买平等，内置事前硬风控拦截)"""
    order = account.trader.submit_order(order_req)
    return {
        "status": "success" if order.status != "REJECTED" else "rejected",
        "order": order.to_dict()
    }

@app.delete("/api/v1/trade/order/{order_id}", dependencies=[Depends(require_user)])
def cancel_order(order_id: str, account=Depends(current_paper_account)):
    """撤销未成交的挂单并解冻资金或持仓"""
    success = account.trader.cancel_order(order_id)
    if not success:
        raise HTTPException(status_code=400, detail=f"无法撤销订单 {order_id}，订单不存在或已结束撮合")
    return {
        "status": "success",
        "message": f"订单 {order_id} 撤销成功"
    }

@app.get("/api/v1/trade/account", dependencies=[Depends(require_user)])
def get_trade_account(account=Depends(current_paper_account)):
    """获取当前模拟账户的资金状况 (动态权益、可用资金、占用保证金、冻结资金、浮动盈亏、平仓累计盈亏)"""
    return account.trader.get_account().to_dict()

@app.get("/api/v1/trade/positions", dependencies=[Depends(require_user)])
def get_trade_positions(account=Depends(current_paper_account)):
    """获取当前所有合约持仓明细 (多空双向独立统计、持仓均价、占用保证金、实时浮动盈亏)"""
    positions = account.trader.get_positions()
    return {
        "positions": [p.to_dict() for p in positions]
    }

@app.get("/api/v1/trade/orders", dependencies=[Depends(require_user)])
def get_trade_orders(limit: Optional[int] = Query(None, ge=1, le=1000),
                     offset: int = Query(0, ge=0), statuses: Optional[str] = None,
                     account=Depends(current_paper_account)):
    """获取所有报单历史与状态"""
    selected = set(statuses.split(",")) if statuses is not None else None
    if selected is not None and not selected.issubset({item.value for item in OrderStatus}):
        raise HTTPException(status_code=422, detail="订单状态无效")
    orders = account.trader.get_orders(limit=limit, offset=offset, statuses=selected)
    return {
        "orders": [o.to_dict() for o in orders]
    }

@app.get("/api/v1/trade/trades", dependencies=[Depends(require_user)])
def get_trade_records(limit: Optional[int] = Query(None, ge=1, le=1000),
                      offset: int = Query(0, ge=0), account=Depends(current_paper_account)):
    """获取所有成交记录流水"""
    trades = account.trader.get_trades(limit=limit, offset=offset)
    return {
        "trades": [t.to_dict() for t in trades]
    }


# ----------------- 自动止损止盈与条件委托 API (需登录) -----------------
from backend.trade_engine.conditional_models import StopOrderRequest
from backend.trade_engine.stop_engine import stop_order_engine

@app.post("/api/v1/trade/stop-order", dependencies=[Depends(require_user)])
def create_stop_order(stop_req: StopOrderRequest, account=Depends(current_paper_account)):
    """提交自动止损单或止盈条件委托"""
    order = account.stops.submit_stop_order(stop_req)
    return {
        "status": "success",
        "stop_order": order.to_dict()
    }

@app.delete("/api/v1/trade/stop-order/{stop_id}", dependencies=[Depends(require_user)])
def cancel_stop_order(stop_id: str, account=Depends(current_paper_account)):
    """撤销正在监控的止损止盈条件单"""
    success = account.stops.cancel_stop_order(stop_id)
    if not success:
        raise HTTPException(status_code=400, detail=f"无法撤销条件单 {stop_id}，不存在或已触发/撤销")
    return {
        "status": "success",
        "message": f"条件单 {stop_id} 已撤销"
    }

@app.get("/api/v1/trade/stop-orders", dependencies=[Depends(require_user)])
def get_stop_orders(instrument: Optional[str] = Query(None), account=Depends(current_paper_account)):
    """获取当前条件单与止损止盈单列表"""
    orders = account.stops.get_stop_orders(instrument=instrument)
    return {
        "stop_orders": [o.to_dict() for o in orders]
    }


# ----------------- 自动跟单策略控制 API -----------------
from pydantic import BaseModel, Field
from backend.trade_engine.signal_bridge import auto_config

class AutoConfigReq(BaseModel):
    enabled: bool
    min_score: Optional[int] = Field(default=80, ge=0, le=100)
    default_volume: Optional[int] = Field(default=1, ge=1, le=50)
    auto_stop_loss_enabled: Optional[bool] = True

@app.get("/api/v1/trade/auto-config", dependencies=[Depends(require_user)])
def get_auto_config(account=Depends(current_paper_account)):
    """获取当前信号跟单交易配置"""
    return {
        "enabled": account.config.enabled,
        "min_score": account.config.min_score,
        "default_volume": account.config.default_volume,
        "auto_stop_loss_enabled": account.config.auto_stop_loss_enabled,
        "cooldown_seconds": account.config.cooldown_seconds
    }

@app.post("/api/v1/trade/auto-config", dependencies=[Depends(require_user)])
def update_auto_config(req: AutoConfigReq, account=Depends(current_paper_account)):
    """更新信号自动跟单开关与策略参数"""
    with account.transaction():
        account.config.enabled = req.enabled
        if req.min_score is not None:
            account.config.min_score = req.min_score
        if req.default_volume is not None:
            account.config.default_volume = req.default_volume
        if req.auto_stop_loss_enabled is not None:
            account.config.auto_stop_loss_enabled = req.auto_stop_loss_enabled
    return {
        "status": "success",
        "config": {
            "enabled": account.config.enabled,
            "min_score": account.config.min_score,
            "default_volume": account.config.default_volume,
            "auto_stop_loss_enabled": account.config.auto_stop_loss_enabled
        }
    }


# ----------------- 账户结算快照、资金曲线与紧急全平 API -----------------
from backend.models.database import AccountSnapshotModel

@app.post("/api/v1/trade/close-all", dependencies=[Depends(require_user)])
def emergency_close_all(account=Depends(current_paper_account)):
    """一键紧急全平当前所有持仓。

    逐个品种提交, 缺行情/被风控拒绝的品种会被跳过而非中断整轮;
    返回值明确列出未平掉的品种, 避免前端误以为已全部平仓。
    """
    order_ids = account.trader.close_all_positions()
    # ⚠️ 这里只能报告"确实没被提交平仓"的持仓, 不能把 frozen_volume 当成未平仓:
    # close_all_positions 拆单提交后, 已报出的手数会被立即冻结, 于是
    # volume - frozen_volume 在成交前读出来是 0 (或部分残余)。若据此判断
    # "未能平仓", 会在每一轮正常全平后都误报 partial (实测: 177 手拆 4 笔
    # 全部 SUBMITTED, 却报 unclosed 150 手)。
    # 判据改为: 该持仓是否已有本轮提交的、处于活动状态的 CLOSE 委托。
    closing_keys = set()
    for oid in order_ids:
        o = account.trader.orders.get(oid)
        if o is not None:
            closing_keys.add(f"{o.instrument}_{o.direction.value}")
    remaining = []
    for p in account.trader.get_positions():
        if p.volume <= 0:
            continue
        close_dir = Direction.SELL if p.direction == Direction.BUY else Direction.BUY
        if f"{p.instrument}_{close_dir.value}" in closing_keys:
            continue          # 已有平仓委托在途, 不算未平仓
        avail = p.volume - p.frozen_volume
        remaining.append({
            "instrument": p.instrument,
            "direction": p.direction.value,
            "volume": avail if avail > 0 else p.volume,
        })
    result = {
        "status": "success" if not remaining else "partial",
        "message": (f"已提交 {len(order_ids)} 笔平仓委托"
                    if not remaining
                    else f"已提交 {len(order_ids)} 笔平仓委托, 仍有 {len(remaining)} 个品种未能平仓 (缺行情或被风控拒绝)"),
        "order_ids": order_ids,
    }
    if remaining:
        result["unclosed_positions"] = remaining
    return result

@app.post("/api/v1/trade/snapshot", dependencies=[Depends(require_user)])
def save_account_snapshot(trading_day: Optional[str] = Query(None), account=Depends(current_paper_account)):
    """手动/定时记录当前账户资产快照至 MySQL"""
    acc = account.trader.get_account()
    day = trading_day or datetime.utcnow().strftime("%Y%m%d")
    now = datetime.utcnow()
    
    session = SessionLocal()
    try:
        snap = AccountSnapshotModel(
            account_id=acc.account_id,
            trading_day=day,
            snapshot_time=now,
            balance=acc.balance,
            dynamic_equity=acc.dynamic_equity,
            available=acc.available,
            margin=acc.margin,
            floating_pnl=acc.floating_pnl,
            realized_pnl=acc.realized_pnl
        )
        session.add(snap)
        session.commit()
        return {"status": "success", "snapshot_id": snap.id, "dynamic_equity": str(acc.dynamic_equity)}
    finally:
        session.close()

@app.get("/api/v1/trade/equity-curve", dependencies=[Depends(require_user)])
def get_equity_curve(limit: int = Query(100, ge=10, le=1000), account=Depends(current_paper_account)):
    """获取历史账户动态权益曲线与回撤统计"""
    session = SessionLocal()
    try:
        rows = session.query(AccountSnapshotModel).filter_by(account_id=account.trader.account.account_id).order_by(AccountSnapshotModel.snapshot_time.asc()).limit(limit).all()
        data = []
        max_equity = Decimal("0.0")
        for r in rows:
            eq = Decimal(str(r.dynamic_equity))
            if eq > max_equity:
                max_equity = eq
            drawdown = (max_equity - eq) / max_equity if max_equity > 0 else Decimal("0.0")
            data.append({
                "time": r.snapshot_time.strftime("%Y-%m-%d %H:%M"),
                "dynamic_equity": float(eq),
                "balance": float(r.balance),
                "drawdown": round(float(drawdown) * 100, 2)
            })
        return {"curve": data}
    finally:
        session.close()

@app.get("/api/v1/market/spreads")
def get_spreads_matrix():
    """获取焦煤跨期价差或跨品种强相关比价矩阵"""
    symbols = [inst.symbol for inst in cfg.instruments]
    quotes = {}
    for s in symbols:
        q = redis_client.hgetall(f"quote:{s}")
        p = q.get("last_price")
        if p:
            quotes[s] = float(p)
    
    spreads = []
    # 如果有 FU2610 和 BU2610，计算燃油沥青比价与价差 (裂解/相关性套利参考)
    if "FU2610" in quotes and "BU2610" in quotes:
        fu_p = quotes["FU2610"]
        bu_p = quotes["BU2610"]
        spreads.append({
            "pair": "FU2610 - BU2610",
            "name": "燃油-沥青跨品种价差",
            "spread": round(fu_p - bu_p, 2),
            "ratio": round(fu_p / bu_p, 4) if bu_p > 0 else 0
        })
    return {"spreads": spreads}


# ----------------- 实盘交易接口 (仅正式用户 live/admin 角色) -----------------
def _live_trader():
    """仅在实盘接口通过权限检查后加载 CTP 原生交易模块。"""
    from backend.trade_engine.live_trader import live_trader
    return live_trader

@app.get("/api/v1/trade/live/status", dependencies=[Depends(require_live)])
def get_live_status(user=Depends(require_live)):
    """CTP 实盘柜台连接状态与就绪判定"""
    from backend.api.auth import require_strong_live_password
    status = _live_trader().status()
    try:
        require_strong_live_password(user)
    except HTTPException as exc:
        status["ready"] = False
        status["blocking_reason"] = exc.detail
    return status

@app.post("/api/v1/trade/live/order", dependencies=[Depends(require_live)])
def submit_live_order(order_req: OrderRequest, user=Depends(require_live)):
    from backend.api.auth import require_strong_live_password
    require_strong_live_password(user)
    """实盘限价报单: CTP 柜台未就绪时一律拒绝 (绝不误发)"""
    record = _live_trader().submit_live_order(order_req, user.username)
    return {
        "status": "success" if record.get("status") == "SUBMITTED" else "rejected",
        "order": record
    }

@app.delete("/api/v1/trade/live/order/{order_ref}", dependencies=[Depends(require_live)])
def cancel_live_order(order_ref: str, user=Depends(require_live)):
    """撤销实盘未成交委托"""
    result = _live_trader().cancel_live_order(order_ref, user.username)
    if result.get("status") == "REJECTED":
        raise HTTPException(status_code=400, detail=result.get("reject_reason", "撤单失败"))
    return {"status": "success", **result}

@app.get("/api/v1/trade/live/orders", dependencies=[Depends(require_live)])
def get_live_orders(user=Depends(require_live)):
    """实盘委托流水 (本地登记)"""
    return {"orders": _live_trader().get_orders(user.username)}


@app.get("/api/v1/trade/live/account", dependencies=[Depends(require_live)])
def get_live_account():
    return _live_trader().get_snapshot()


# ----------------- 实盘紧急停机三开关 (从软到硬) -----------------
# 开关 1: 禁止开仓 (保留平仓)      → 软开关, 立即生效, 无需重启
# 开关 2: 撤销全部挂单              → 中开关, 限速逐笔撤
# 开关 3: 一键全平                  → 硬开关, 需确认字符串 + X-Admin-Token
#
# 注意: /api/v1/trade/close-all (模拟盘一键全平) 对实盘不适用, 故单独实现。

class EmergencyConfirm(BaseModel):
    confirm: str = ""
    reason: str = ""


@app.get("/api/v1/trade/live/risk", dependencies=[Depends(require_live)])
def get_live_risk_config(user=Depends(require_live)):
    """当前实盘生效的风控阈值与开关状态 (灰度放量时用于核对档位)"""
    rm = _live_trader().risk_manager
    return {
        "profile": cfg.system.profile,
        "feed": cfg.system.feed,
        "live_enabled": rm.live_enabled,
        "open_blocked": rm.open_blocked,
        "block_reason": rm.block_reason,
        "max_order_volume": rm.max_order_volume,
        "max_position_volume": rm.max_position_volume,
        "margin_ratio": str(rm.margin_ratio),
        "max_cancel_count": rm.max_cancel_count,
        "max_daily_loss_ratio": str(rm.max_daily_loss_ratio),
        "fat_finger_pct": str(rm.fat_finger_pct),
        "allowed_instruments": rm.allowed_instruments,
        "daily_order_limit": rm.daily_order_limit,
        "trading_day": rm.trading_day,
        "orders_today": rm.order_counts.get(rm.trading_day, 0),
        "cancels_today": dict(rm.cancel_counts),
    }


@app.post("/api/v1/trade/live/block-open", dependencies=[Depends(require_live)])
def block_live_open(body: EmergencyConfirm = EmergencyConfirm(), user=Depends(require_live)):
    """开关 1 (软): 禁止新开仓, 平仓不受影响。用于策略异常时的人工干预。"""
    result = _live_trader().block_open(body.reason or f"人工触发 (操作人: {user.username})")
    notifier.notify("LIVE_OPEN_BLOCKED", f"实盘开仓已被禁止: {result['reason']}", "WARNING", "live_trader")
    return result


@app.post("/api/v1/trade/live/unblock-open", dependencies=[Depends(require_live)])
def unblock_live_open(user=Depends(require_live)):
    """解除开关 1。"""
    result = _live_trader().unblock_open()
    notifier.notify("LIVE_OPEN_UNBLOCKED", f"实盘开仓限制已解除 (操作人: {user.username})", "WARNING", "live_trader")
    return result


@app.post("/api/v1/trade/live/cancel-all", dependencies=[Depends(require_live)])
def cancel_all_live_orders(
    user=Depends(require_live),
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
):
    """开关 2 (中): 撤销全部活动委托。限速逐笔撤, 避免触发柜台流控与异常交易认定。"""
    from backend.api.auth import require_strong_live_password
    require_strong_live_password(user)
    verify_admin_token(x_admin_token)
    result = _live_trader().cancel_all(user.username)
    notifier.notify(
        "LIVE_CANCEL_ALL",
        f"实盘批量撤单: 尝试 {result['attempted']} 笔, 已提交 {result['submitted']} 笔 (操作人: {user.username})",
        "WARNING", "live_trader",
        details={"attempted": result["attempted"], "submitted": result["submitted"]},
    )
    return result


@app.post("/api/v1/trade/live/close-all", dependencies=[Depends(require_live)])
def close_all_live_positions(
    body: EmergencyConfirm,
    user=Depends(require_live),
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
):
    """开关 3 (硬): 一键全平。先撤全部挂单, 再按合约逐笔对手价平仓。

    需要 body.confirm == "CONFIRM_CLOSE_ALL" 且携带 X-Admin-Token (二次确认)。
    """
    from backend.api.auth import require_strong_live_password
    require_strong_live_password(user)
    verify_admin_token(x_admin_token)
    if body.confirm != "CONFIRM_CLOSE_ALL":
        raise HTTPException(
            status_code=400,
            detail="缺少确认字符串: 请在请求体传入 {\"confirm\": \"CONFIRM_CLOSE_ALL\"}",
        )
    result = _live_trader().close_all(user.username, confirm=body.confirm)
    notifier.notify(
        "LIVE_CLOSE_ALL",
        f"实盘一键全平触发: 撤单 {result.get('cancelled', 0)} 笔, "
        f"平仓委托 {len(result.get('closed', []))} 笔, 失败 {len(result.get('errors', []))} 笔 "
        f"(操作人: {user.username}, 原因: {body.reason or '未填写'})",
        "CRITICAL", "live_trader",
        details={"closed": result.get("closed"), "errors": result.get("errors")},
    )
    return result


@app.get("/api/v1/trade/live/ledger")
def get_live_ledger(
    trading_day: Optional[str] = Query(None, pattern="^\\d{8}$"),
    instrument: Optional[str] = Query(None, max_length=16),
    limit: int = Query(200, ge=1, le=2000),
    user=Depends(require_live),
):
    """实盘结构化流水查询 (供柜台逐笔对账使用)。

    这是对账的地基: order_record / trade_record 可用 SQL 直接与柜台对账单比对。
    """
    where_orders, where_trades, params = [], [], {"limit": limit}
    if trading_day:
        where_orders.append("trading_day = :day")
        where_trades.append("trading_day = :day")
        params["day"] = trading_day
    if instrument:
        where_orders.append("instrument_id = :inst")
        where_trades.append("instrument_id = :inst")
        params["inst"] = instrument.upper()
    sql_o = "SELECT * FROM order_record"
    sql_t = "SELECT * FROM trade_record"
    if where_orders:
        sql_o += " WHERE " + " AND ".join(where_orders)
        sql_t += " WHERE " + " AND ".join(where_trades)
    sql_o += " ORDER BY id DESC LIMIT :limit"
    sql_t += " ORDER BY id DESC LIMIT :limit"

    with engine.connect() as conn:
        orders = [dict(r._mapping) for r in conn.execute(text(sql_o), params)]
        trades = [dict(r._mapping) for r in conn.execute(text(sql_t), params)]

    for rows in (orders, trades):
        for row in rows:
            for key, value in list(row.items()):
                if isinstance(value, Decimal):
                    row[key] = str(value)
                elif isinstance(value, datetime):
                    row[key] = value.strftime("%Y-%m-%d %H:%M:%S")

    return {"orders": orders, "trades": trades,
            "summary": {"order_count": len(orders), "trade_count": len(trades),
                        "trade_volume": sum(int(t.get("volume") or 0) for t in trades)}}


# ----------------- 成交明细与大单监控 (基于快照级行情推算现手) -----------------
from backend.tape.tape_recorder import tape_recorder

@app.get("/api/v1/tape/{instrument}")
def get_tape(
    instrument: str,
    limit: int = Query(60, ge=1, le=500),
    date: Optional[str] = Query(None, pattern="^\\d{8}$", description="历史交易日 (YYYYMMDD), 由 1m K线推算分钟级明细")
):
    """成交明细: 实时为 Tick 快照现手; 当日无 Tick 时自动回退到最近有数据交易日的分钟级明细"""
    items = tape_recorder.get_tape(instrument, limit)
    if items:
        return {
            "instrument": instrument,
            "items": items,
            "big_order_threshold": cfg.system.big_order_min_volume,
            "granularity": "tick",
        }
    # 当日无实时 Tick (休市/刚启动): 回退到最近有数据交易日的分钟级明细
    fallback_day = tape_recorder.latest_bar_trading_day(instrument)
    if fallback_day:
        items = tape_recorder.synthesize_from_bars(instrument, fallback_day, max(limit, 500))
        return {
            "instrument": instrument,
            "items": items,
            "big_order_threshold": cfg.system.big_order_min_volume,
            "granularity": "1m",
            "date": fallback_day,
            "fallback": True,
        }
    return {
        "instrument": instrument,
        "items": [],
        "big_order_threshold": cfg.system.big_order_min_volume,
        "granularity": "tick",
    }

@app.get("/api/v1/bigorders")
def get_big_orders(
    instrument: Optional[str] = Query(None, max_length=16),
    limit: int = Query(60, ge=1, le=300),
    date: Optional[str] = Query(None, pattern="^\\d{8}$", description="历史交易日 (YYYYMMDD)")
):
    """大单监控: 实时为 Tick 快照现手; 当日无 Tick 或指定历史日期时由 1m K线推算分钟级明细"""
    threshold = cfg.system.big_order_min_volume
    live = tape_recorder.get_big_orders(instrument, limit)
    if not date and live:
        return {"items": live}

    # 历史日期或当日无实时大单: 推算分钟级大单
    days: list = []
    if date:
        days = [date]
    else:
        syms = [instrument] if instrument else get_watchlist()
        for s in syms:
            d = tape_recorder.latest_bar_trading_day(s)
            if d:
                days.append(d)
    days = sorted(set(days))
    if not days:
        return {"items": []}

    merged: list = []
    for d in days:
        syms = [instrument] if instrument else [s for s in get_watchlist()]
        for s in syms:
            for x in tape_recorder.synthesize_from_bars(s, d, 1000):
                if x["volume"] >= threshold:
                    x["instrument"] = s
                    merged.append(x)
    merged.sort(key=lambda r: (r.get("ts", ""), r.get("instrument", "")), reverse=True)
    return {"items": merged[:limit], "granularity": "1m", "date": ",".join(days)}


# ----------------- 页面路由 (登录门禁与角色分流) -----------------
from pathlib import Path
from fastapi import Request
from fastapi.responses import RedirectResponse, FileResponse
from backend.api.auth import get_current_user

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


def _page_or_login(request: Request, filename: str, require_live: bool = False):
    """页面统一入口: 未登录跳登录页; 实盘页对模拟用户弹回模拟页"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if require_live and not user.can_live_trade:
        return RedirectResponse(url="/paper", status_code=302)
    # no-store: HTML 完全不缓存, 发版后任何浏览器(含微信内置内核)重开页面必取最新代码
    return FileResponse(FRONTEND_DIR / filename, headers={"Cache-Control": "no-store"})


if FRONTEND_DIR.exists():
    @app.get("/js/{filename}")
    async def serve_frontend_js(filename: str):
        """前端拆分后的模块脚本 (index.html 内联脚本迁移至 frontend/js/, 仅允许单级 .js 文件名)"""
        import re as _re
        if not _re.fullmatch(r"[A-Za-z0-9_\-]+\.js", filename) or not (FRONTEND_DIR / "js" / filename).exists():
            raise HTTPException(status_code=404)
        # 与 HTML 同策略 no-store: 避免 HTML 新引用与浏览器缓存的旧 js 混搭
        return FileResponse(FRONTEND_DIR / "js" / filename, headers={"Cache-Control": "no-store"})

    @app.get("/")
    async def serve_root(request: Request):
        # 按角色分流: 模拟用户进模拟交易页, 正式用户进实盘交易页
        user = get_current_user(request)
        if not user:
            return RedirectResponse(url="/login", status_code=302)
        return RedirectResponse(url="/live" if user.can_live_trade else "/paper", status_code=302)

    @app.get("/paper")
    async def serve_paper(request: Request):
        """模拟交易页面 (全部已登录角色可用)"""
        return _page_or_login(request, "index.html")

    @app.get("/live")
    async def serve_live_page(request: Request):
        """完整实盘看盘与交易工作台 (仅 live/admin 角色)"""
        from backend.api.auth import live_mode_enabled
        if not live_mode_enabled():
            raise HTTPException(status_code=503, detail="正式交易功能已停用，当前仅开放模拟用户")
        return _page_or_login(request, "index.html", require_live=True)

    @app.get("/live-login")
    async def serve_live_login(request: Request):
        """实盘账号登录交易一体页 (无需预登录, 允许直接访问并在页内登录)"""
        from backend.api.auth import live_mode_enabled
        if not live_mode_enabled():
            raise HTTPException(status_code=503, detail="正式交易功能已停用，当前仅开放模拟用户")
        return FileResponse(FRONTEND_DIR / "live.html", headers={"Cache-Control": "no-store"})

    @app.get("/login")
    async def serve_login(request: Request):
        # 已登录用户访问登录页时按角色进入对应交易页
        user = get_current_user(request)
        if user:
            return RedirectResponse(url="/live" if user.can_live_trade else "/paper", status_code=302)
        return FileResponse(FRONTEND_DIR / "login.html", headers={"Cache-Control": "no-store"})
