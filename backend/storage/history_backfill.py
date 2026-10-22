"""历史K线回填: 从新浪财经公开接口补全各周期历史数据并幂等写入 MySQL。

- 1m 约可回补最近 3 个交易日, 5m 约 3 周, 15m 约 2 个月, 30m 约 4 个月, 60m 约 8 个月
- 日线为合约自上市以来全历史
- 采用 ON DUPLICATE KEY UPDATE 幂等写入, 可随时重复执行
- Redis 仅在对应缓存键不存在时回灌, 不覆盖实时引擎维护的K线列表
"""

import json
import hashlib
import logging
import re
import time
import urllib.request
import urllib.error
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy.dialects.mysql import insert as mysql_insert

from backend.common.config import cfg
from backend.common.product_specs import get_exchange, get_spec
from backend.common.trading_time import FuturesTradingCalendar
from backend.models.database import MarketBarModel
from backend.models.market_data import decode_bar_provenance
from backend.storage.mysql_client import SessionLocal
from backend.storage.redis_client import RedisClient
from backend.storage.universe_sync import get_universe_symbols

logger = logging.getLogger(__name__)

_HISTORY_HTTP_LOCK = threading.Lock()
_HISTORY_LAST_REQUEST = 0.0
_HISTORY_BLOCKED_UNTIL = 0.0
_HISTORY_MIN_INTERVAL = 5.0
_HISTORY_456_COOLDOWN = 15 * 60

PERIOD_MAP = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}
ALL_PERIODS = ["1m", "5m", "15m", "30m", "60m", "1d"]
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
}
MINUTE_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/"
    "InnerFuturesNewService.getFewMinLine?symbol={symbol}&type={type}"
)
DAILY_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/"
    "InnerFuturesNewService.getDailyKLine?symbol={symbol}"
)


def _http_get(url: str, timeout: float = 12.0, retries: int = 2) -> str:
    global _HISTORY_LAST_REQUEST, _HISTORY_BLOCKED_UNTIL
    last_err: Optional[Exception] = None
    for i in range(retries + 1):
        try:
            with _HISTORY_HTTP_LOCK:
                now = time.monotonic()
                if now < _HISTORY_BLOCKED_UNTIL:
                    remaining = int(_HISTORY_BLOCKED_UNTIL - now)
                    raise RuntimeError(f"新浪历史接口限流冷却中，约 {remaining} 秒后重试")
                wait = _HISTORY_MIN_INTERVAL - (now - _HISTORY_LAST_REQUEST)
                if wait > 0:
                    time.sleep(wait)
                req = urllib.request.Request(url, headers=HTTP_HEADERS)
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        result = resp.read().decode("utf-8", errors="replace")
                    _HISTORY_LAST_REQUEST = time.monotonic()
                    return result
                except urllib.error.HTTPError as e:
                    _HISTORY_LAST_REQUEST = time.monotonic()
                    if e.code == 456:
                        _HISTORY_BLOCKED_UNTIL = _HISTORY_LAST_REQUEST + _HISTORY_456_COOLDOWN
                        raise RuntimeError("新浪历史接口触发 456 限流，已暂停请求 15 分钟") from e
                    raise
        except Exception as e:
            last_err = e
            if "限流" in str(e):
                break
            time.sleep(1.2 * (i + 1))
    raise last_err


def _parse_jsonp_items(raw_text: str) -> List[Dict[str, Any]]:
    match = re.search(r"=\((.*)\);", raw_text, re.DOTALL)
    if not match:
        return []
    return json.loads(match.group(1))


def _volume_multiple(symbol: str) -> int:
    mult, _ = get_spec(symbol)
    return mult


def _is_resolvable(symbol: str) -> bool:
    """该合约能否解析出所属交易所/品种 (决定交易时段是否可判定)。

    未知品种无法判定交易时段, 清洗类过滤一律放行, 避免误删合法数据。
    """
    try:
        from backend.common.product_specs import get_exchange
        return bool(get_exchange(symbol))
    except Exception:
        return False


def _build_bar(symbol: str, period: str, bar_time: datetime, o: Decimal, h: Decimal,
               l: Decimal, c: Decimal, vol: int, oi: Decimal, prev_oi: Decimal,
               provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # 免费历史源不提供真实成交额，不能用收盘价乘成交量伪造 VWAP。
    turnover = Decimal("0")
    return {
        "instrument_id": symbol,
        "trading_day": FuturesTradingCalendar.get_trading_day(bar_time),
        "period": period,
        "bar_time": bar_time,
        "open_price": o,
        "high_price": h,
        "low_price": l,
        "close_price": c,
        "volume": vol,
        "turnover": turnover,
        "open_interest": oi,
        "open_interest_change": (oi - prev_oi) if prev_oi > 0 else Decimal("0"),
        "provenance_json": json.dumps(provenance, ensure_ascii=False) if provenance is not None else None,
    }


def _history_provenance(raw: str, url: str, requested_at: str, label: str) -> Dict[str, Any]:
    return {
        "source": "sina_history", "provider": "sina",
        "label_semantics": label, "coverage": "UNKNOWN",
        "requested_at": requested_at, "collected_at": datetime.now(timezone.utc).isoformat(),
        "request_url": url, "response_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "response_hash_basis": "utf8_decoded_text",
        "trading_day_rule": "FuturesTradingCalendar.get_trading_day",
    }


def fetch_sina_minute_bars(symbol: str, period: str) -> List[Dict[str, Any]]:
    """拉取新浪分钟线, 并统一转换为「分钟开始」时间戳口径。

    新浪的 d 字段是分钟**结束**时刻 (夜盘 21:01~23:00), 本地 BarGenerator 用**开始**时刻
    (21:00~22:59)。这里对 1m 统一做 -1m 平移, 避免各调用方各自处理、
    漏平移就写入收盘整点幽灵 bar。高阶周期 (5m~60m) 保持新浪原样语义。

    另外: 上游源偶含非交易时刻的静态快照 (周六 09:00、周日 23:59 之类),
    属源端脏数据, 1m 下一律丢弃 —— 否则补缺任务每轮都会把它重新灌进库。
    """
    sina_type = PERIOD_MAP.get(period)
    if not sina_type:
        return []
    url = MINUTE_URL.format(symbol=symbol, type=sina_type)
    requested_at = datetime.now(timezone.utc).isoformat()
    raw = _http_get(url)
    provenance = _history_provenance(raw, url, requested_at, "START" if period == "1m" else "END")
    items = _parse_jsonp_items(raw)

    results: List[Dict[str, Any]] = []
    prev_oi = Decimal("0")
    for item in items:
        dt_str = item.get("d")
        if not dt_str:
            continue
        bar_time = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        if period == "1m":
            bar_time -= timedelta(minutes=1)
            # 源端非交易时刻快照 (收盘整点/周末) 不入库, 保持 1m 序列只含可交易分钟。
            # 未知品种放行 (无法判定时段)。
            if _is_resolvable(symbol) and not FuturesTradingCalendar.is_trading_time_for(
                    bar_time, symbol):
                prev_oi = Decimal(str(item.get("p", "0")))
                continue
        prev_oi_holder = prev_oi
        prev_oi = Decimal(str(item.get("p", "0")))
        results.append(_build_bar(
            symbol, period, bar_time,
            Decimal(str(item.get("o", "0"))), Decimal(str(item.get("h", "0"))),
            Decimal(str(item.get("l", "0"))), Decimal(str(item.get("c", "0"))),
            int(item.get("v", 0)), prev_oi, prev_oi_holder,
            dict(provenance, source_time=dt_str,
                 aggregation_clock='WALL_CLOCK' if period == '1m' else 'TRADING_MINUTES'),
        ))
    return results


def fetch_sina_daily_bars(symbol: str) -> List[Dict[str, Any]]:
    url = DAILY_URL.format(symbol=symbol)
    requested_at = datetime.now(timezone.utc).isoformat()
    raw = _http_get(url)
    provenance = _history_provenance(raw, url, requested_at, "DAILY_CLOSE")
    items = _parse_jsonp_items(raw)

    results: List[Dict[str, Any]] = []
    prev_oi = Decimal("0")
    for item in items:
        date_str = item.get("d")
        if not date_str:
            continue
        bar_time = datetime.strptime(f"{date_str} 15:00:00", "%Y-%m-%d %H:%M:%S")
        prev_oi_holder = prev_oi
        prev_oi = Decimal(str(item.get("p", "0")))
        results.append(_build_bar(
            symbol, "1d", bar_time,
            Decimal(str(item.get("o", "0"))), Decimal(str(item.get("h", "0"))),
            Decimal(str(item.get("l", "0"))), Decimal(str(item.get("c", "0"))),
            int(item.get("v", 0)), prev_oi, prev_oi_holder,
            dict(provenance, source_time=date_str),
        ))
    return results


def save_bars_to_mysql(bars: List[Dict[str, Any]]) -> int:
    """批量幂等保存 K 线至 MySQL (ON DUPLICATE KEY UPDATE)"""
    if not bars:
        return 0
    session = SessionLocal()
    saved = 0
    try:
        for i in range(0, len(bars), 500):
            chunk = [dict(bar, provenance_json=bar.get("provenance_json")) for bar in bars[i:i + 500]]
            stmt = mysql_insert(MarketBarModel).values(chunk)
            stmt = stmt.on_duplicate_key_update(
                trading_day=stmt.inserted.trading_day,
                open_price=stmt.inserted.open_price,
                high_price=stmt.inserted.high_price,
                low_price=stmt.inserted.low_price,
                close_price=stmt.inserted.close_price,
                volume=stmt.inserted.volume,
                turnover=stmt.inserted.turnover,
                open_interest=stmt.inserted.open_interest,
                open_interest_change=stmt.inserted.open_interest_change,
                provenance_json=stmt.inserted.provenance_json,
            )
            session.execute(stmt)
            saved += len(chunk)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"历史K线批量保存异常: {e}")
        raise
    finally:
        session.close()
    return saved


def get_stored_bar_count(symbol: str, period: str) -> int:
    session = SessionLocal()
    try:
        from sqlalchemy import func
        return int(session.query(func.count(MarketBarModel.id)).filter_by(
            instrument_id=symbol, period=period).scalar() or 0)
    finally:
        session.close()


def get_stored_bar_stats(symbol: str) -> Dict[str, Any]:
    session = SessionLocal()
    try:
        from sqlalchemy import func
        rows = session.query(MarketBarModel.period, func.count(MarketBarModel.id),
                             func.max(MarketBarModel.bar_time)).filter(
            MarketBarModel.instrument_id == symbol,
            MarketBarModel.period.in_(ALL_PERIODS)).group_by(MarketBarModel.period).all()
        return {period: (count, latest) for period, count, latest in rows}
    finally:
        session.close()


def sync_redis_if_empty(symbol: str, period: str, bars: List[Dict[str, Any]], max_keep: int = 500):
    """仅当 Redis 对应键为空时回灌最新K线, 避免覆盖实时引擎维护的列表"""
    if not bars:
        return
    redis_client = RedisClient.get_client()
    key = f"bars:{symbol}:{period}"
    try:
        if redis_client.exists(key):
            return
        payloads = []
        for b in bars[-max_keep:]:
            payloads.append(json.dumps({
                "instrument": b["instrument_id"],
                "trading_day": b["trading_day"],
                "period": b["period"],
                "bar_time": b["bar_time"].isoformat(),
                "open_price": str(b["open_price"]),
                "high_price": str(b["high_price"]),
                "low_price": str(b["low_price"]),
                "close_price": str(b["close_price"]),
                "volume": b["volume"],
                "turnover": str(b["turnover"]),
                "open_interest": str(b["open_interest"]),
                "open_interest_change": str(b["open_interest_change"]),
                "provenance_json": decode_bar_provenance(b.get("provenance_json")),
            }))
        if payloads:
            pipe = redis_client.pipeline(transaction=False)
            pipe.rpush(key, *payloads)
            pipe.ltrim(key, -max_keep, -1)
            pipe.execute()
    except Exception as e:
        logger.warning(f"回灌 Redis 缓存失败 {key}: {e}")


def backfill_symbol(symbol: str, periods: Optional[List[str]] = None, sleep_between: float = 0.5) -> Dict[str, Any]:
    """回补单个合约的全部/指定周期历史数据"""
    periods = periods or ALL_PERIODS
    summary: Dict[str, Any] = {"symbol": symbol, "periods": {}}
    for period in periods:
        try:
            if period == "1m":
                # 启动/新增订阅也走同一补缺路径，避免旧回填覆盖分钟起点口径。
                summary["periods"][period] = repair_minute_gaps(symbol)
                continue
            if period == "1d":
                bars = fetch_sina_daily_bars(symbol)
            else:
                bars = fetch_sina_minute_bars(symbol, period)
            # 高周期接口会提前给出形成中的下一根（如 09:06 返回 10:00 的 60m）。
            # 只保存已经结束且给源端留出两分钟落地时间的 Bar。
            cutoff = datetime.now() - timedelta(minutes=2)
            bars = [bar for bar in bars if bar["bar_time"] <= cutoff]
            saved = save_bars_to_mysql(bars)
            sync_redis_if_empty(symbol, period, bars)
            rng = f"{bars[0]['bar_time']:%Y-%m-%d} ~ {bars[-1]['bar_time']:%Y-%m-%d}" if bars else "-"
            summary["periods"][period] = {"fetched": len(bars), "saved": saved, "range": rng}
            logger.info(f"[{symbol}] {period}: 拉取 {len(bars)} 根, 保存/更新 {saved} 根 ({rng})")
        except Exception as e:
            summary["periods"][period] = {"error": str(e)}
            logger.error(f"[{symbol}] {period} 回填失败: {e}")
        time.sleep(sleep_between)
    return summary


def ensure_symbol_backfilled(symbol: str, min_bars: int = 30,
                             now: Optional[datetime] = None,
                             full_history: bool = False) -> Optional[Dict[str, Any]]:
    """启动时补不足/断档周期；订阅时可补全源端提供的全部历史。"""
    if full_history:
        return backfill_symbol(symbol)
    try:
        stats = get_stored_bar_stats(symbol)
    except Exception as e:
        logger.warning(f"查询历史K线数量失败 {symbol}: {e}")
        return None
    now = now or datetime.now()
    latest_minute = stats.get("1m", (0, None))[1]
    expected_daily = (now.date() if FuturesTradingCalendar.is_trading_day(now.date())
                      and now.hour >= 16 else FuturesTradingCalendar.previous_trading_day(now.date()))
    needed = []
    for period in ALL_PERIODS:
        count, latest = stats.get(period, (0, None))
        if count < (1 if period == "1d" else min_bars):
            needed.append(period)
        elif period == "1d" and latest and latest.date() < expected_daily:
            needed.append(period)
        elif period not in ("1m", "1d") and latest and latest_minute and \
                latest < latest_minute - timedelta(minutes=int(period[:-1]) + 10):
            needed.append(period)
    if not needed:
        return None
    logger.info("合约 %s 历史周期 %s 数量不足或已断档，自动回补...", symbol, needed)
    return backfill_symbol(symbol, periods=needed)


def backfill_watchlist(symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """回补关注合约列表 (默认取配置文件中的合约)"""
    if not symbols:
        symbols = [inst.symbol for inst in cfg.instruments]
    results = []
    for sym in symbols:
        results.append(backfill_symbol(sym))
    return results


def backfill_universe_daily(sleep_between: float = 1.2, only_when_empty: bool = True) -> Dict[str, Any]:
    """为全市场合约补日线历史 (合约数量多, 限速执行, 可随时中断重跑)"""
    symbols = get_universe_symbols()
    done, skipped = 0, 0
    for idx, sym in enumerate(symbols, 1):
        try:
            if only_when_empty and get_stored_bar_count(sym, "1d") > 0:
                skipped += 1
                continue
            bars = fetch_sina_daily_bars(sym)
            save_bars_to_mysql(bars)
            done += 1
            if idx % 20 == 0:
                logger.info(f"全市场日线回填进度: {idx}/{len(symbols)} (本次新增 {done}, 跳过 {skipped})")
        except Exception as e:
            logger.warning(f"[{sym}] 日线回填失败: {e}")
        time.sleep(sleep_between)
    logger.info(f"全市场日线回填完成: 共 {len(symbols)}, 新增 {done}, 跳过已有 {skipped}")
    return {"total": len(symbols), "filled": done, "skipped": skipped}


# 开盘首根异常判定参数 (与 scripts/fix_open_auction_bar.py 保持一致口径)
_AUCTION_ABS_RATIO_FLOOR = 3.0     # 首根/次高 低于此值视为正常
_AUCTION_MIN_OVERFLOW = 1000       # 溢出量下限, 低于此值视为噪声
_AUCTION_BASELINE_MIN = 1.0        # 基准倍数下限 (首根含竞价, 不应低于当次高)


def _auction_day_session(bars):
    """取日盘 (09:00~15:00) 的 bar, 按时间升序"""
    d = [b for b in bars if 9 <= b.bar_time.hour < 15]
    d.sort(key=lambda x: x.bar_time)
    return d


def _auction_first_ratio(day_bars):
    """返回 (日盘首根, 次高分钟量, 倍数); 不合格返回 None"""
    d = _auction_day_session(day_bars)
    if len(d) < 10:
        return None
    first = d[0]
    if first.bar_time.strftime("%H:%M") != "09:00":
        return None
    rest = [b.volume for b in d[1:] if b.volume > 0]
    if not rest:
        return None
    second = max(rest)
    if second < 100:          # 当日数据过于稀疏, 不足以判定
        return None
    return first, second, first.volume / second


def repair_open_auction_bar(symbol: str, session=None) -> int:
    """校正日盘 09:00 首根的集合竞价异常量, 返回校正的交易日数。

    上游 (新浪) 偶会把开盘集合竞价的累计委托量而非实际成交量写入首根,
    使该根量放大 3~19 倍, 在副图 VOL 上形成突兀巨柱。判定用「合约自适应基准」:
    以该合约历史正常日 (剔除异常日后) 首根/次高倍数的中位数为基准 k0,
    倍数 >= max(3.0, k0 + 1.5) 且溢出量 >= 1000 时判为异常。

    校正保持「日盘合计不变」: 首根目标量 = 次高 × k0, 差额按后续各分钟量占比
    加权摊回, 因此日线总量与日线 OHLC 均不受影响。
    """
    import statistics

    own_session = session is None
    if own_session:
        session = SessionLocal()
    try:
        rows = (session.query(MarketBarModel)
                .filter(MarketBarModel.instrument_id == symbol,
                        MarketBarModel.period == "1m")
                .order_by(MarketBarModel.bar_time).all())
        by_day = {}
        for r in rows:
            by_day.setdefault(r.trading_day, []).append(r)
        if not by_day:
            return 0

        # 基准: 先粗筛剔除异常日, 再用正常日中位数, 避免异常日抬高基准导致漏检。
        # 注意: 基准算一次即固定, 循环内不再重算 —— 否则「修正首根 -> 改变样本
        # -> 改变基准」会来回漂移, 每轮都触发一次修正, 永远不收敛。
        ratios = []
        for bars in by_day.values():
            got = _auction_first_ratio(bars)
            if got:
                ratios.append(got[2])
        if ratios:
            coarse = statistics.median(ratios)
            clean = [x for x in ratios
                     if x < max(_AUCTION_ABS_RATIO_FLOOR, coarse * 1.5)]
            base = statistics.median(clean) if clean else statistics.median(ratios)
        else:
            base = 1.5
        base = max(base, _AUCTION_BASELINE_MIN)
        threshold = min(max(_AUCTION_ABS_RATIO_FLOOR, base * 3.0),
                        max(_AUCTION_ABS_RATIO_FLOOR, base + 1.5))

        fixed_days = 0
        for td, bars in sorted(by_day.items()):
            got = _auction_first_ratio(bars)
            if not got:
                continue
            first, second, ratio = got
            if ratio < threshold:
                continue
            target = int(round(second * base))
            overflow = first.volume - target
            if overflow < _AUCTION_MIN_OVERFLOW:
                continue

            others = _auction_day_session(bars)[1:]
            weight_sum = sum(b.volume for b in others)
            if weight_sum <= 0:
                continue
            allocated = 0
            shares = []
            for b in others:
                share = int(overflow * b.volume // weight_sum)
                shares.append(share)
                allocated += share
            remainder = overflow - allocated
            if remainder > 0:
                max_idx = max(range(len(others)), key=lambda i: others[i].volume)
                shares[max_idx] += remainder

            original = first.volume
            first.volume = target
            _mark_volume_transformation(first)
            for b, share in zip(others, shares):
                if share:
                    b.volume += share
                    _mark_volume_transformation(b)
            fixed_days += 1
            logger.warning("%s 开盘首根集合竞价异常已校正: %s %d -> %d (倍数 %.1fx -> %.1fx, 摊回 %d)",
                           symbol, td, original, target,
                           ratio, target / second if second else 0, overflow)
        return fixed_days
    finally:
        if own_session:
            session.close()


def _sanitize_auction_in_candidates(symbol: str, candidates: dict, session=None) -> int:
    """在「写库前」净化候选集中的开盘首根异常量, 返回修正的交易日数。

    与 repair_open_auction_bar 的区别: 本函数只看源候选集 (candidates),
    不读库 —— 因为修复的正是「覆写前」这一刻, 库里可能仍是脏值。

    判定基准取自库中该合约的历史正常日 (库里已有的其它交易日), 因此需要
    session 才能读历史; 若传 None 则自建会话。
    """
    import statistics

    own_session = session is None
    if own_session:
        session = SessionLocal()
    try:
        # 按交易日归组候选
        by_day = {}
        for stamp, bar in candidates.items():
            by_day.setdefault(bar["trading_day"], {})[stamp] = bar

        # 基准: 从库里已有历史求「正常日首根/次高」中位数。
        # 关键: 必须排除「本次候选集覆盖的交易日」—— 那些日的首根正是待修正对象,
        # 若把它们算进基准样本, 每轮修正都会改变基准, 导致首根值来回漂移、不收敛。
        ratios = []
        hist_days = {}
        try:
            rows = (session.query(MarketBarModel)
                    .filter(MarketBarModel.instrument_id == symbol,
                            MarketBarModel.period == "1m")
                    .order_by(MarketBarModel.bar_time).all())
            for r in rows:
                hist_days.setdefault(r.trading_day, []).append(r)
        except Exception:
            hist_days = {}
        for td, bars in hist_days.items():
            if td in by_day:
                continue
            got = _auction_first_ratio(bars)
            if got:
                ratios.append(got[2])
        if ratios:
            coarse = statistics.median(ratios)
            clean = [x for x in ratios
                     if x < max(_AUCTION_ABS_RATIO_FLOOR, coarse * 1.5)]
            base = statistics.median(clean) if clean else statistics.median(ratios)
        else:
            base = 1.5
        base = max(base, _AUCTION_BASELINE_MIN)
        threshold = min(max(_AUCTION_ABS_RATIO_FLOOR, base * 3.0),
                        max(_AUCTION_ABS_RATIO_FLOOR, base + 1.5))

        # 读库中这批交易日的首根现值, 用于判断「是否真的需要变更」。
        # 上游源永远返回脏值, 若只看候选集, 每轮都会误报一次修正; 虽然写入值
        # 相同 (幂等), 但日志/返回值会让监控误以为数据在反复漂移。
        try:
            existing_first = {}
            for stamp, bar in candidates.items():
                if bar["bar_time"].strftime("%H:%M") != "09:00":
                    continue
                row = (session.query(MarketBarModel)
                       .filter_by(instrument_id=symbol, period="1m",
                                  bar_time=bar["bar_time"]).first())
                if row is not None:
                    existing_first[stamp] = row.volume
        except Exception:
            existing_first = {}

        fixed_days = 0
        for td, day_map in by_day.items():
            session_bars = [b for b in day_map.values()
                            if 9 <= b["bar_time"].hour < 15]
            session_bars.sort(key=lambda x: x["bar_time"])
            if len(session_bars) < 10:
                continue
            first = session_bars[0]
            if first["bar_time"].strftime("%H:%M") != "09:00":
                continue
            rest = [b["volume"] for b in session_bars[1:] if b["volume"] > 0]
            if not rest:
                continue
            second = max(rest)
            if second < 100:
                continue
            ratio = first["volume"] / second
            if ratio < threshold:
                continue
            target = int(round(second * base))
            overflow = first["volume"] - target
            if overflow < _AUCTION_MIN_OVERFLOW:
                continue

            already = existing_first.get(first["bar_time"]) == target

            others = session_bars[1:]
            weight_sum = sum(b["volume"] for b in others)
            if weight_sum <= 0:
                continue
            allocated = 0
            shares = []
            for b in others:
                share = int(overflow * b["volume"] // weight_sum)
                shares.append(share)
                allocated += share
            remainder = overflow - allocated
            if remainder > 0:
                max_idx = max(range(len(others)), key=lambda i: others[i]["volume"])
                shares[max_idx] += remainder

            original = first["volume"]
            first["volume"] = target
            _mark_volume_transformation(first)
            for b, share in zip(others, shares):
                if share:
                    b["volume"] += share
                    _mark_volume_transformation(b)

            # 幂等分支: 库里已是目标值, 不重复计数, 但仍要重放净化 ——
            # 候选集是「源端原始值」, 若不重放, 紧随其后的覆写循环会把首根脏值
            # 和未摊回的后续分钟一起写回库, 形成改了又回的循环。
            if already:
                continue

            fixed_days += 1
            logger.warning("%s 源头首根集合竞价异常已净化: %s %d -> %d "
                           "(倍数 %.1fx -> %.1fx, 摊回 %d)",
                           symbol, td, original, target,
                           ratio, target / second if second else 0, overflow)
        return fixed_days
    finally:
        if own_session:
            session.close()


def _mark_volume_transformation(bar):
    raw = bar.get("provenance_json") if isinstance(bar, dict) else getattr(bar, "provenance_json", None)
    provenance = decode_bar_provenance(raw)
    if provenance is None:
        return
    provenance["volume_transformation"] = "auction_volume_redistribution"
    encoded = json.dumps(provenance, ensure_ascii=False)
    if isinstance(bar, dict):
        bar["provenance_json"] = encoded
    else:
        bar.provenance_json = encoded


def _repair_provenance(row, source, preserve_volume):
    raw = source.get("provenance_json")
    provenance = decode_bar_provenance(raw)
    if provenance is None:
        return None
    prior = decode_bar_provenance(getattr(row, "provenance_json", None)) or {}
    provenance.update(source="sina_history_repair",
                      coverage="PARTIAL" if prior.get("coverage") == "PARTIAL" else "UNKNOWN",
                      retained_fields=["turnover", "open_interest_change"] + (["volume"] if preserve_volume else []))
    if preserve_volume:
        provenance["volume_source"] = prior.get("volume_source", prior.get("source", "UNKNOWN"))
        for key in ('volume_quality','volume_gap'):
            if key in prior:
                provenance[key]=prior[key]
    for field in ("turnover", "open_interest_change"):
        provenance[field + "_source"] = prior.get(field + "_source", prior.get("source", "UNKNOWN"))
    return json.dumps(provenance, ensure_ascii=False)


def _repair_eastmoney_minutes(symbol, now, force=False):
    """东方财富仅补缺失的已闭合1m；备用源不得覆盖既有分钟。"""
    from backend.storage.eastmoney_history import fetch_minute_bars
    if not _is_resolvable(symbol):
        return {'inserted':0,'corrected_fields':0,'available':False}
    cutoff = now.replace(second=0,microsecond=0) - timedelta(minutes=2)
    current_trading_day = FuturesTradingCalendar.get_trading_day(now)
    day_open = (9, 30) if get_exchange(symbol) == 'CFFEX' else (9, 0)

    def eligible(stamp):
        return (stamp < cutoff
                and FuturesTradingCalendar.is_trading_time_for(stamp, symbol)
                and not ((stamp.hour, stamp.minute) in
                         (day_open, (day_open[0], day_open[1] + 1), (21, 0), (21, 1))
                         and FuturesTradingCalendar.get_trading_day(stamp) == current_trading_day
                         and now - stamp < timedelta(minutes=15)))

    if not force:
        # 东财1m仅提供近端（通常最近5个交易日），lmt=10000不代表无限历史。
        # 从第五个交易日的夜盘起检查，并以本地历史起点裁剪，避免上市前假缺口。
        day = now.date()
        if not FuturesTradingCalendar.is_trading_day(day):
            day = FuturesTradingCalendar.previous_trading_day(day)
        for _ in range(4):
            day = FuturesTradingCalendar.previous_trading_day(day)
        start = datetime.combine(day, datetime.min.time())
        previous = FuturesTradingCalendar.previous_trading_day(day)
        if (FuturesTradingCalendar.has_night_session(symbol)
                and FuturesTradingCalendar._night_session_day(previous)):
            start = datetime.combine(previous, datetime.min.time()).replace(hour=21)
        with SessionLocal() as session:
            first = (session.query(MarketBarModel.bar_time).filter_by(
                instrument_id=symbol, period='1m').order_by(MarketBarModel.bar_time).first())
            if first is not None:
                start = max(start, first[0])
                existing = {row[0] for row in session.query(MarketBarModel.bar_time).filter_by(
                    instrument_id=symbol, period='1m').filter(
                        MarketBarModel.bar_time >= start, MarketBarModel.bar_time < cutoff).all()}
                stamp = start
                while stamp < cutoff:
                    if stamp not in existing and eligible(stamp):
                        break
                    stamp += timedelta(minutes=1)
                else:
                    return {'inserted':0,'corrected_fields':0,'available':False,
                            'skipped':'NO_MISSING_MINUTES'}
    candidates = {}
    for bar in fetch_minute_bars(symbol):
        stamp = bar['bar_time']
        if not eligible(stamp):
            continue
        candidates[stamp] = bar
    if not candidates:
        return {'inserted':0,'corrected_fields':0,'available':False}
    inserted = 0
    with SessionLocal() as session:
        existing = {row[0] for row in session.query(MarketBarModel.bar_time).filter_by(
            instrument_id=symbol,period='1m').filter(MarketBarModel.bar_time.in_(list(candidates))).all()}
        for stamp,bar in candidates.items():
            if stamp in existing:
                continue
            stmt = mysql_insert(MarketBarModel).values(bar)
            # 查询后若实时写库或另一回补已插入，同样保持原记录全部字段不变。
            stmt = stmt.on_duplicate_key_update(id=MarketBarModel.id)
            inserted += session.execute(stmt).rowcount
        session.commit()
    if inserted:
        _rebuild_minute_redis_cache(symbol,now)
        logger.info('%s 东方财富备用分钟补缺：补 %d 根',symbol,inserted)
    return {'inserted':inserted,'corrected_fields':0,'available':True}


def repair_minute_gaps(symbol: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """仅用新浪修复已闭合1m；东方财富备用已停用，失败保留真实缺口。"""
    primary = _repair_sina_minute_gaps(symbol, now or datetime.now())
    return {
        'symbol': symbol, 'inserted': primary['inserted'], 'missing': primary['missing'],
        'primary_source': 'sina', 'primary_available': primary['available'],
        'primary_inserted': primary['inserted'],
        'fallback_source': None, 'fallback_inserted': 0,
        'fallback_available': False, 'fallback_skipped': 'DISABLED',
        'corrected_trading_days': primary['corrected_trading_days'],
        'corrected_fields': primary['corrected_fields'], 'auction_fixed': primary['auction_fixed'],
    }


def _repair_sina_minute_gaps(symbol: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """补入历史源中存在而本地缺少的已结束分钟, 并按历史源修正已收盘K线的行情字段。

    绝不触碰 cutoff (近 2 分钟) 之后的K线: 那是实时引擎的属地 (形成中/刚落库)。
    盘中冷启动无法从首笔累计量还原之前的分钟分布，缺口仍需历史源补入。
    开盘最初两分钟的历史源可能延迟修订，当前交易日须等待 15 分钟才补入。
    """
    now = now or datetime.now()
    current_trading_day = FuturesTradingCalendar.get_trading_day(now)
    day_open = (9, 30) if get_exchange(symbol) == "CFFEX" else (9, 0)
    # 给上游两分钟落地时间，也避开本地异步写库和当前形成中的 K 线。
    cutoff = now.replace(second=0, microsecond=0) - timedelta(minutes=2)
    candidates = {}
    for source in fetch_sina_minute_bars(symbol, "1m"):
        # fetch_sina_minute_bars 已对 1m 统一做 -1m 平移, 此处直接用其时间戳。
        bar = dict(source)
        metadata = decode_bar_provenance(bar.get('provenance_json')) or {'provider':'sina','source':'sina_history_repair','label_semantics':'START'}
        metadata['source_role'] = 'PRIMARY'
        bar['provenance_json'] = json.dumps(metadata,ensure_ascii=False)
        if bar["bar_time"] >= cutoff:
            continue
        minute = bar["bar_time"]
        opening_minute = ((minute.hour, minute.minute) in
                          (day_open, (day_open[0], day_open[1] + 1), (21, 0), (21, 1)))
        if (opening_minute and _is_resolvable(symbol)
                and FuturesTradingCalendar.get_trading_day(minute) == current_trading_day
                and now - minute < timedelta(minutes=15)):
            continue
        # 上游源自身偶含非交易时刻的静态快照 (如周六 09:00、周日 23:59),
        # 属于源端脏数据, 直接丢弃, 否则每轮补缺都会把它重新灌进库、清不干净。
        # 未知品种 (无法解析交易时段) 一律放行, 避免误伤测试/指数类合称。
        if _is_resolvable(symbol) and not FuturesTradingCalendar.is_trading_time_for(
                bar["bar_time"], symbol):
            logger.debug("%s 跳过源端非交易时刻 bar %s", symbol, bar["bar_time"])
            continue
        bar["trading_day"] = FuturesTradingCalendar.get_trading_day(bar["bar_time"])
        candidates[bar["bar_time"]] = bar
    if not candidates:
        return {"symbol": symbol, "inserted": 0, "missing": 0,
                "corrected_trading_days": 0, "corrected_fields": 0,
                "auction_fixed": 0, "available": False}

    # 在写库前先净化首根: 上游 (新浪) 偶把开盘集合竞价的累计委托量写入
    # 日盘 09:00 首根, 放大 3~19 倍。若不在这一步拦下, 覆写循环会先把脏值
    # 写进库、再由事后校正改回, 既多一次写库, 也会让 Redis 重建读到中间态。
    auction_fixed = _sanitize_auction_in_candidates(symbol, candidates, session=None)
    pre_auction_fixed = auction_fixed

    session = SessionLocal()
    try:
        rows = session.query(MarketBarModel).filter(
            MarketBarModel.instrument_id == symbol, MarketBarModel.period == "1m",
            MarketBarModel.bar_time >= min(candidates), MarketBarModel.bar_time <= max(candidates)).all()
        existing = {row.bar_time for row in rows}
        corrected = 0
        field_fixes = 0
        # 当前交易日 (按 now 推算): 属于实时引擎属地, 其 volume 不得被源端覆写。
        for row in rows:
            trading_day = FuturesTradingCalendar.get_trading_day(row.bar_time)
            if row.trading_day != trading_day:
                row.trading_day = trading_day
                corrected += 1
            src = candidates.get(row.bar_time)
            if src is None:
                continue
            changed = False
            # ⚠️ 价格与持仓量始终以源端为准 (源端这两项可靠, 且能纠正错位/幻影K线)。
            for field in ("open_price", "high_price", "low_price", "close_price",
                          "open_interest"):
                if getattr(row, field) != src[field]:
                    setattr(row, field, src[field])
                    field_fixes += 1
                    changed = True
            # ⚠️ volume 只在「非当前交易日」才允许按源端纠偏。
            #
            # 原因: 上游新浪 getFewMinLine 的 v 字段分布严重扭曲 —— 实测同一交易日
            # 21:40=44 / 21:44=38 / 21:49=89, 而同期真实成交量是几千手 (源端按秒去重
            # 统计, 同秒多笔被合并、相邻分钟又重复计数)。而本地实时引擎按「累计量
            # 差分」聚合出的 volume 是准确的 (经影子引擎逐 tick 验证, 分毫不差)。
            # 曾因此把已闭合的正确值改小 (实测 21:55 由 2238 被改成 190, 砍掉 91.5%),
            # 再经 _rebuild_minute_redis_cache 回灌 Redis, 导致 VOL 副图只捕获约 16%。
            #
            # 判据用「交易日」而非「距 now 多久」: 当前交易日的K线是实时引擎属地,
            # 其 volume 不可信于源端; 已收盘的历史交易日源端才是唯一权威,
            # 且开盘集合竞价的净化结果必须经此通道落库 (_sanitize_auction_in_candidates
            # 改的是候选集, 靠这里的赋值写进库)。
            prior = decode_bar_provenance(row.provenance_json) or {}
            # 已有东财真实成交量不降级为新浪历史估算量；字段来源继续保留。
            preserve_volume = (row.trading_day == current_trading_day
                               or prior.get('volume_quality') in ('UNALLOCATED_GAP','UNVERIFIED_ALLOCATION')
                               or prior.get('provider') == 'eastmoney'
                               or prior.get('volume_source') == 'eastmoney_history')
            if preserve_volume:
                if changed:
                    row.provenance_json = _repair_provenance(row, src, preserve_volume=True)
                continue
            if row.volume != src["volume"]:
                row.volume = src["volume"]
                field_fixes += 1
                changed = True
            if changed:
                row.provenance_json = _repair_provenance(row, src, preserve_volume=False)
        missing = [bar for stamp, bar in candidates.items() if stamp not in existing]
        inserted = 0
        for offset in range(0, len(missing), 500):
            stmt = mysql_insert(MarketBarModel).values(missing[offset:offset + 500])
            # 并发实时写入优先；唯一键冲突时保持原记录全部字段不变。
            stmt = stmt.on_duplicate_key_update(id=MarketBarModel.id)
            inserted += session.execute(stmt).rowcount
        session.commit()
        if missing or corrected or field_fixes:
            logger.info("%s 分钟缺口回补: 补 %d 根, 修正交易日 %d, 修正字段 %d (保留实时属地)",
                        symbol, len(missing), corrected, field_fixes)
        # 首根净化已在上方「写库前」完成 (_sanitize_auction_in_candidates),
        # 此处不再重复调用 repair_open_auction_bar: 后者基于库内数据判定,
        # 与前置净化读到的候选集在同一事务里会前后打架 (一个按源值算、
        # 一个按库值算), 导致首根每轮被改写一次、永不收敛。
        auction_fixed = pre_auction_fixed
        # MySQL 已修正: 同步重建 Redis 近端缓存, 避免旧脏数据 (错位/幻影) 继续遮蔽历史
        if inserted or corrected or field_fixes or auction_fixed:
            _rebuild_minute_redis_cache(symbol, now)
        return {"symbol": symbol, "inserted": inserted, "missing": len(missing),
                "corrected_trading_days": corrected, "corrected_fields": field_fixes,
                "auction_fixed": auction_fixed, "available": True}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _rebuild_minute_redis_cache(symbol: str, now: datetime, max_keep: int = 500):
    """用 MySQL 已修正的 1m K线重建近端缓存列表。

    cutoff 之后的条目保留 Redis 现值 (实时引擎属地, 比历史源新鲜),
    其余一律以 MySQL 为准 —— 旧数据里可能存在错位/幻影K线。
    """
    redis_client = RedisClient.get_client()
    key = f"bars:{symbol}:1m"
    try:
        keep_recent = {}
        try:
            for raw in redis_client.lrange(key, -max_keep, -1):
                try:
                    d = json.loads(raw)
                    bt = datetime.fromisoformat(d["bar_time"])
                except Exception:
                    continue
                if bt >= now.replace(second=0, microsecond=0) - timedelta(minutes=2):
                    keep_recent[bt] = raw
        except Exception:
            keep_recent = {}
        session = SessionLocal()
        try:
            rows = session.query(MarketBarModel).filter_by(
                instrument_id=symbol, period="1m"
            ).order_by(MarketBarModel.bar_time.desc()).limit(max_keep).all()
        finally:
            session.close()
        by_time = dict(keep_recent)
        for r in rows:
            by_time[r.bar_time] = json.dumps({
                "instrument": r.instrument_id,
                "trading_day": r.trading_day,
                "period": "1m",
                "bar_time": r.bar_time.isoformat(),
                "open_price": str(r.open_price),
                "high_price": str(r.high_price),
                "low_price": str(r.low_price),
                "close_price": str(r.close_price),
                "volume": r.volume,
                "turnover": str(r.turnover),
                "open_interest": str(r.open_interest),
                "open_interest_change": str(r.open_interest_change),
                "provenance_json": decode_bar_provenance(getattr(r, "provenance_json", None)),
            })
        ordered = [by_time[t] for t in sorted(by_time)[-max_keep:]]
        pipe = redis_client.pipeline(transaction=False)
        pipe.delete(key)
        if ordered:
            pipe.rpush(key, *ordered)
        pipe.execute()
    except Exception as e:
        logger.warning(f"重建 1m 近端缓存失败 {key}: {e}")
