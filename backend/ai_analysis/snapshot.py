import hashlib
import json
import math
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List

from backend.bar_engine.live_bars import forming_bar
from backend.common.config import cfg
from backend.common.product_specs import get_spec
from backend.common.trading_time import FuturesTradingCalendar
from backend.indicator_engine.realtime_analyzer import RealtimeAnalyzer
from backend.models.database import InstrumentModel, MarketBarModel
from backend.models.market_data import BarData
from backend.common.volume_quality import volume_usable
from backend.storage.mysql_client import SessionLocal
from backend.storage.redis_client import RedisClient

# 盘前展望: 未开盘小节附带的"开盘前最近已闭合 1 分钟 K 线"数量上限与日线根数
PRE_OPEN_CONTEXT_BARS = 360
PRE_OPEN_DAILY_BARS = 6


def _bar_provenance(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = None
    return dict(value) if isinstance(value, dict) else {
        'source': 'UNKNOWN', 'label_semantics': 'UNKNOWN', 'coverage': 'UNKNOWN'}


def _bar_end(bar: BarData, instrument: str) -> datetime:
    if bar.period == '1d':
        day = datetime.strptime(bar.trading_day, '%Y%m%d')
        return FuturesTradingCalendar.session_end(day.replace(hour=14), instrument)
    if _bar_provenance(getattr(bar, 'provenance_json', None)).get('label_semantics') == 'END':
        return bar.bar_time
    return min(bar.bar_time + timedelta(minutes=int(bar.period[:-1])),
               FuturesTradingCalendar.session_end(bar.bar_time, instrument))


def _bar_from_dict(data: Dict[str, Any]) -> BarData:
    return BarData(
        instrument=data["instrument"], trading_day=data["trading_day"], period=data["period"],
        bar_time=datetime.fromisoformat(data["bar_time"]),
        open_price=Decimal(str(data["open_price"])), high_price=Decimal(str(data["high_price"])),
        low_price=Decimal(str(data["low_price"])), close_price=Decimal(str(data["close_price"])),
        volume=int(data["volume"]), turnover=Decimal(str(data["turnover"])),
        open_interest=Decimal(str(data["open_interest"])),
        open_interest_change=Decimal(str(data.get("open_interest_change", "0"))),
        provenance_json=_bar_provenance(data.get('provenance_json')),
    )


def _load_bars(instrument: str, period: str, limit: int = 5000,
               trading_day: str | None = None, cutoff: datetime | None = None) -> List[BarData]:
    merged: Dict[datetime, BarData] = {}
    session = SessionLocal()
    try:
        query = session.query(MarketBarModel).filter_by(instrument_id=instrument, period=period)
        if trading_day:
            query = query.filter_by(trading_day=trading_day)
        if cutoff:
            query = query.filter(MarketBarModel.bar_time <= cutoff)
        rows = query.order_by(MarketBarModel.bar_time.desc()).limit(limit).all()
        for row in rows:
            bar = BarData(
                instrument=row.instrument_id, trading_day=row.trading_day, period=row.period,
                bar_time=row.bar_time, open_price=row.open_price, high_price=row.high_price,
                low_price=row.low_price, close_price=row.close_price, volume=row.volume,
                turnover=row.turnover, open_interest=row.open_interest,
                open_interest_change=row.open_interest_change,
                provenance_json=_bar_provenance(getattr(row, 'provenance_json', None)),
            )
            merged[bar.bar_time] = bar
    finally:
        session.close()
    redis_client = RedisClient.get_client()
    for raw in redis_client.lrange(f"bars:{instrument}:{period}", 0 if trading_day or cutoff else -limit, -1):
        try:
            bar = _bar_from_dict(json.loads(raw))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if (not trading_day or bar.trading_day == trading_day) and (not cutoff or bar.bar_time <= cutoff):
            merged[bar.bar_time] = bar
    bars = sorted(merged.values(), key=lambda item: item.bar_time)
    return bars[-limit:]


def _instrument_meta(instrument: str) -> tuple[str, int, Decimal]:
    volume_multiple, price_tick = get_spec(instrument)
    name = instrument
    for item in cfg.instruments:
        if item.symbol.upper() == instrument:
            return item.name, int(item.volume_multiple), Decimal(str(item.price_tick))
    session = SessionLocal()
    try:
        row = session.query(InstrumentModel).filter_by(symbol=instrument).first()
        if row:
            name = row.name or instrument
            volume_multiple = row.volume_multiple or volume_multiple
            price_tick = row.price_tick or price_tick
    finally:
        session.close()
    return name, int(volume_multiple), Decimal(str(price_tick))


def _closed_bar_json(bar: BarData) -> Dict[str, Any]:
    return {
        "bar_time": bar.bar_time.isoformat(), "open": str(bar.open_price),
        "high": str(bar.high_price), "low": str(bar.low_price), "close": str(bar.close_price),
        "volume": bar.volume if volume_usable(bar) else None, "turnover": str(bar.turnover),
        "open_interest": str(bar.open_interest),
        "open_interest_change": str(bar.open_interest_change), "is_closed": True,
        "provenance": _bar_provenance(getattr(bar, 'provenance_json', None)),
    }


def _forming_bar_json(bar: Dict[str, Any] | None, trading_day: str) -> Dict[str, Any] | None:
    if not bar or bar.get("trading_day") != trading_day:
        return None
    return {
        "bar_time": bar.get("bar_time"), "open": str(bar.get("open_price", "")),
        "high": str(bar.get("high_price", "")), "low": str(bar.get("low_price", "")),
        "close": str(bar.get("close_price", "")), "volume": int(bar.get("volume", 0)) if volume_usable(bar) else None,
        "turnover": str(bar.get("turnover", "0")),
        "open_interest": str(bar.get("open_interest", "0")),
        "open_interest_change": str(bar.get("open_interest_change", "0")), "is_closed": False,
    }


def _ema(values: List[float], period: int):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def _build_trend_context(daily_bars: List[BarData], bars_5m: List[BarData],
                         trading_day: str, quote: Dict[str, Any]) -> Dict[str, Any] | None:
    """盘时段/盘前分析的日线级趋势背景 (TODAY 模式已有 rule_analysis, 不需要)。"""
    daily = sorted({b.bar_time: b for b in daily_bars}.values(), key=lambda b: b.bar_time)
    closed = [b for b in daily if b.trading_day != trading_day]
    if len(closed) < 6:
        return None
    prev, prev2 = closed[-1], (closed[-2] if len(closed) >= 2 else closed[-1])
    closes = [float(b.close_price) for b in closed]
    prev_close = float(prev2.close_price)
    out: Dict[str, Any] = {
        "prev_daily": {
            "date": prev.bar_time.strftime("%Y-%m-%d"),
            "open": str(prev.open_price), "high": str(prev.high_price),
            "low": str(prev.low_price), "close": str(prev.close_price),
            "change_pct": (round((float(prev.close_price) / prev_close - 1) * 100, 2)
                           if prev_close else None),
        },
        "recent_daily": [{"date": b.bar_time.strftime("%Y-%m-%d"), "close": str(b.close_price)}
                         for b in closed[-5:]],
        "prev_settle": str(quote.get("prev_settle") or ""),
        "last_price": str(quote.get("last_price") or ""),
    }
    ema20, ema60 = _ema(closes, 20), _ema(closes, 60)
    daily_ema: Dict[str, Any] = {}
    if ema20 is not None:
        daily_ema["ema20"] = round(ema20, 2)
    if ema60 is not None:
        daily_ema["ema60"] = round(ema60, 2)
    if ema20 is not None and ema60 is not None:
        daily_ema["relation"] = "EMA20>=EMA60" if ema20 >= ema60 else "EMA20<EMA60"
    if daily_ema:
        out["daily_ema"] = daily_ema
    prev_day = str(prev.trading_day)
    b5 = [b for b in bars_5m if str(b.trading_day) == prev_day]
    if b5:
        out["prev_day_intraday"] = {
            "high": str(max(b.high_price for b in b5)),
            "low": str(min(b.low_price for b in b5)), "bars_5m": len(b5),
        }
    return out


_QUOTE_FIELDS = (
    "timestamp", "last_price", "day_open", "day_high", "day_low", "prev_settle",
    "bid_price_1", "bid_volume_1", "ask_price_1", "ask_volume_1", "avg_price",
    "volume", "turnover", "open_interest",
)


def _safe_quote(quote: Dict[str, Any]) -> Dict[str, Any]:
    return {key: quote.get(key) for key in _QUOTE_FIELDS if quote.get(key) not in (None, "")}


def market_quality(instrument, quote, bars, now=None):
    now = now or datetime.now()
    active = FuturesTradingCalendar.is_trading_time_for(now, instrument)
    warnings, blocking = [], []
    provenance = [_bar_provenance(bar.get('provenance', bar.get('provenance_json'))) for bar in bars]
    if any(p.get('provider') == 'eastmoney' or any(p.get(field) == 'eastmoney_history'
            for field in ('volume_source', 'turnover_source', 'open_interest_change_source')) for p in provenance):
        warnings.append('FALLBACK_1M_SOURCE_EASTMONEY')
    latency = None
    try:
        stamp = datetime.fromisoformat(quote['timestamp'])
        if stamp.tzinfo:
            stamp = stamp.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
        latency = (now - stamp).total_seconds()
        if latency < -5:
            blocking.append('FUTURE_QUOTE_TIME')
        elif active and latency > 10:
            warnings.append('STALE_QUOTE')
            if latency > 30:
                blocking.append('STALE_QUOTE')
    except (KeyError, TypeError, ValueError):
        blocking.append('INVALID_QUOTE_TIME')
    try:
        price = float(quote['last_price'])
        if not math.isfinite(price) or price <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        blocking.append('INVALID_REFERENCE_PRICE')
    if active:
        if quote.get('trading_day') != FuturesTradingCalendar.get_trading_day(now):
            blocking.append('TRADING_DAY_MISMATCH')
        if not bars:
            blocking.append('NO_CLOSED_1M_BARS')
        else:
            last = datetime.fromisoformat(bars[-1]['bar_time']).replace(tzinfo=None)
            # 1m 柱通常以起始分钟标记；新鲜度应从闭合时刻计算。
            # END 标签已经是闭合时刻；旧数据沿用 _bar_end 的 START 默认口径。
            semantics = _bar_provenance(bars[-1].get('provenance')).get('label_semantics')
            last_end = last if semantics == 'END' else last + timedelta(minutes=1)
            # 开盘/小节刚恢复时允许上一小节的最后一分钟；连续交易超过两分钟必须有新K线。
            recent = now - timedelta(minutes=2)
            if last_end < recent and FuturesTradingCalendar.is_trading_time_for(recent, instrument):
                blocking.append('STALE_1M_BARS')
    unverified_volume_bars = [
        {'instrument': instrument, 'period': '1m', 'bar_time': bar.get('bar_time'),
         'volume_quality': p.get('volume_quality', 'UNKNOWN'),
         'volume_gap': p.get('volume_gap', {})}
        for bar, p in zip(bars, provenance)
        if ('volume' in bar and bar['volume'] is None) or not volume_usable(bar)]
    if unverified_volume_bars:
        blocking.append('UNVERIFIED_MINUTE_VOLUME')
    return {'market_state': 'TRADING' if active else 'CLOSED',
            'can_judge': not blocking, 'blocking_reasons': blocking,
            'unverified_volume_bars': unverified_volume_bars,
            'quote_latency_seconds': latency, 'warnings': list(dict.fromkeys(warnings + blocking))}


def build_latest_context(instrument: str, bars_limit: int = 60) -> Dict[str, Any] | None:
    """追问附带的最新行情: 提问时刻的报价 + 最近已闭合 1 分钟 K 线 + 形成中K线。"""
    instrument = instrument.strip().upper()
    redis_client = RedisClient.get_client()
    quote = redis_client.hgetall(f"quote:sina:{instrument}")
    if not quote:
        quote = redis_client.hgetall(f"quote:{instrument}")
    if not quote:
        return None
    trading_day = str(quote.get("trading_day") or "")
    bars = _load_bars(instrument, "1m", bars_limit, trading_day or None)
    quality = market_quality(instrument, quote, [_closed_bar_json(bar) for bar in bars])
    missing = _missing_intervals(bars, instrument, trading_day)
    quality['missing_intervals'] = missing
    if missing:
        quality['warnings'].append('MISSING_1M_INTERVALS')
        if quality['market_state'] == 'TRADING':
            quality['can_judge'] = False
            quality['blocking_reasons'].append('MISSING_1M_INTERVALS')
    return {
        "instrument": {"symbol": instrument},
        "captured_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "trading_day": trading_day,
        "quote": _safe_quote(quote),
        "quality": quality,
        "bars_1m": [_closed_bar_json(bar) for bar in bars][-bars_limit:],
        "forming_bar_1m": _forming_bar_json(
            forming_bar(redis_client, "sina", instrument, "1m"), trading_day),
        "note": "提问时刻的最新行情快照, 其中的价格与K线属于新信息, 优先级高于 original_snapshot",
    }


def _missing_intervals(bars: List[BarData], instrument: str, trading_day: str) -> list[Dict[str, str]]:
    missing = []
    for previous, current in zip(bars, bars[1:]):
        cursor = previous.bar_time.replace(second=0, microsecond=0)
        end = current.bar_time.replace(second=0, microsecond=0)
        gap_start = gap_end = None
        from datetime import timedelta
        cursor += timedelta(minutes=1)
        while cursor < end:
            expected = (FuturesTradingCalendar.is_trading_time_for(cursor, instrument)
                        and FuturesTradingCalendar.get_trading_day(cursor) == trading_day)
            if expected:
                gap_start = gap_start or cursor
                gap_end = cursor
            elif gap_start:
                missing.append({"start": gap_start.isoformat(), "end": gap_end.isoformat()})
                gap_start = gap_end = None
            cursor += timedelta(minutes=1)
        if gap_start:
            missing.append({"start": gap_start.isoformat(), "end": gap_end.isoformat()})
    return missing


def build_related_market_snapshots(instruments: list[str], cutoff: str,
                                   contract_statuses: dict | list | None = None) -> list[Dict[str, Any]]:
    from backend.ai_analysis import future_trend
    local_cutoff = future_trend._timestamp(cutoff)
    if local_cutoff is None:
        raise ValueError("关联行情截点无效")
    if isinstance(contract_statuses, list):
        contract_statuses = {row['instrument']: row for row in contract_statuses}
    redis_client = RedisClient.get_client()
    snapshots = []
    for instrument in instruments:
        name, multiple, tick = _instrument_meta(instrument)
        lifecycle = (contract_statuses or {}).get(instrument) or future_trend.contract_lifecycle(
            instrument, [], local_cutoff)
        warnings = []
        market = {'feed': None, 'timezone': 'Asia/Shanghai', 'snapshot_time': cutoff,
                  'trading_day': None, 'quote': None, 'bars_60m': [],
                  'data_scope': '关联行情仅为参考，不代表目标合约自身事实或默认正相关。仅含截点前已闭合60分钟柱；时间标签口径未核实，持仓原差及跨度保留，单周期推理差为null。'}
        last_trading_day = lifecycle.get('last_trading_day') or lifecycle.get('last_trading_day_by_rule')
        expired = lifecycle.get('expired') or (last_trading_day and
                   last_trading_day < local_cutoff.strftime('%Y%m%d'))
        term_unknown = (lifecycle.get('status') == 'UNKNOWN' or
            (lifecycle.get('status') == 'RULE_BOUND' and
             (local_cutoff.strftime('%Y%m%d') >= lifecycle['last_trading_day_not_before'] or
              any(t['status'] == 'UNKNOWN' for t in lifecycle.get('targets', [])))))
        if expired:
            warnings.append('EXPIRED_CONTRACT')
        else:
            if term_unknown:
                warnings.append('CONTRACT_TERM_UNVERIFIED')
            quote = redis_client.hgetall(f'quote:sina:{instrument}')
            feed = 'sina'
            if not quote:
                quote = redis_client.hgetall(f'quote:{instrument}')
                feed = quote.get('feed') or 'local'
            stamp = future_trend._timestamp(quote.get('timestamp'))
            if not quote:
                warnings.append('QUOTE_MISSING')
            elif stamp is None:
                warnings.append('INVALID_QUOTE_TIME')
            elif stamp > local_cutoff:
                warnings.append('QUOTE_AFTER_CUTOFF')
            else:
                market.update(feed=feed, trading_day=quote.get('trading_day'), quote=_safe_quote(quote))
                if FuturesTradingCalendar.is_trading_time_for(local_cutoff, instrument):
                    if (local_cutoff - stamp).total_seconds() > 30:
                        warnings.append('STALE_QUOTE')
                    if quote.get('trading_day') != FuturesTradingCalendar.get_trading_day(local_cutoff):
                        warnings.append('TRADING_DAY_MISMATCH')
            closed = []
            for bar in _load_bars(instrument, '60m', 120, cutoff=local_cutoff):
                bar_time = future_trend._timestamp(bar.bar_time.isoformat())
                end = _bar_end(bar, instrument)
                if end <= local_cutoff and FuturesTradingCalendar.is_trading_day(
                        datetime.strptime(bar.trading_day, '%Y%m%d').date()):
                    closed.append(bar)
            closed = sorted(closed, key=lambda item: item.bar_time)[-61:]
            serialized = []
            previous = None
            previous_time = None
            for bar in closed:
                row = _closed_bar_json(bar)
                row['trading_day'] = bar.trading_day
                row['open_interest_change_raw'] = str(bar.open_interest - previous) if previous is not None else None
                row['open_interest_change'] = None
                row['open_interest_comparison'] = {'from': previous_time.isoformat() if previous_time else None,
                    'to': bar.bar_time.isoformat(),
                    'span_seconds': (bar.bar_time - previous_time).total_seconds() if previous_time else None,
                    'continuity': 'GAP_OR_UNVERIFIED'}
                previous = bar.open_interest
                previous_time = bar.bar_time
                serialized.append(row)
            market['bars_60m'] = serialized[-60:]
            if not market['bars_60m']:
                warnings.append('NO_CLOSED_60M_BARS')
        status = 'MISSING' if not market['quote'] and not market['bars_60m'] else 'PARTIAL' if warnings else 'AVAILABLE'
        availability = ('EXPIRED' if expired else 'TERM_UNKNOWN' if term_unknown
                        else 'DATA_MISSING' if status == 'MISSING' else status)
        snapshots.append({'instrument': {'symbol': instrument, 'name': name,
                          'price_tick': str(tick), 'volume_multiple': multiple},
                          'market': market, 'quality': {'status': status, 'availability':availability, 'warnings': warnings,
                          'market_state': 'TRADING' if FuturesTradingCalendar.is_trading_time_for(
                              local_cutoff, instrument) else 'CLOSED'}, 'contract_lifecycle': lifecycle})
    return snapshots


def _future_expected_labels(instrument: str, period: str, now: datetime) -> dict[str, list[str]]:
    """Two candidate grids; existing rows do not identify their source/label convention."""
    minutes = int(period[:-1])
    starts, ends = set(), set()
    for offset in range(31):
        day = now.date() - timedelta(days=offset)
        if not FuturesTradingCalendar.is_trading_day(day):
            continue
        sessions = FuturesTradingCalendar.analysis_session_options(
            datetime.combine(day, datetime.min.time()).replace(hour=8), instrument)
        groups = {'DAY': [], 'NIGHT': []}
        local_ends = {}
        for session in sessions:
            cursor = datetime.fromisoformat(session['start_time'])
            end = datetime.fromisoformat(session['end_time'])
            while cursor < end:
                aligned = cursor.replace(minute=(cursor.minute // minutes) * minutes,
                                         second=0, microsecond=0)
                local_ends[aligned] = min(aligned + timedelta(minutes=minutes), end)
                groups[session['session']].append(cursor + timedelta(minutes=1))
                cursor += timedelta(minutes=1)
        starts.update(label.isoformat() for label, end in local_ends.items() if end <= now)
        for trading_minutes in groups.values():
            for i in range(0, len(trading_minutes), minutes):
                end = trading_minutes[min(i + minutes, len(trading_minutes)) - 1]
                if end <= now:
                    ends.add(end.isoformat())
    return {'LOCAL_START': sorted(starts), 'HISTORY_END_CANDIDATE': sorted(ends)}


def _future_market_quality(instrument: str, periods: dict, tick: Decimal,
                           now: datetime) -> dict:
    checks = {'warnings': [], 'blocking_reasons': [],
              'period_checks': {}, 'range_checks': [],
              'source_coverage': 'UNVERIFIED',
              'usage_limits': {'cross_period_precision': False,
                               'unverified_time_labels': True}}
    for period in ('1d', '60m', '15m'):
        rows = periods.get(period, [])
        provenances = [_bar_provenance(bar.get('provenance')) for bar in rows]
        known_labels = bool(rows) and all(p.get('label_semantics') in
            (('DAILY_CLOSE',) if period == '1d' else ('START', 'END')) for p in provenances)
        if not known_labels:
            checks['warnings'].append('TIME_LABEL_UNVERIFIED')
        grids = _future_expected_labels(instrument, period, now) if period != '1d' else {}
        positions = [{label: i for i, label in enumerate(labels)} for labels in grids.values()]
        previous = None
        for bar in rows:
            provenance = _bar_provenance(bar.get('provenance'))
            if provenance.get('coverage') == 'PARTIAL':
                checks['blocking_reasons'].append(f'PARTIAL_COVERAGE_{period.upper()}')
            elif provenance.get('coverage') != 'COMPLETE':
                checks['warnings'].append('SOURCE_COVERAGE_UNVERIFIED')
            dt = datetime.fromisoformat(bar['bar_time'])
            if bar['trading_day'] != FuturesTradingCalendar.get_trading_day(dt):
                checks['blocking_reasons'].append('BAR_TRADING_DAY_MISMATCH')
            values = [Decimal(bar[key]) for key in ('open', 'high', 'low', 'close')]
            o, h, l, c = values
            if not all(value.is_finite() for value in values) or not (l <= min(o, c) <= max(o, c) <= h):
                checks['blocking_reasons'].append('INVALID_OHLC')
            bar['usage_limits'] = {'precise_price_levels': True, 'volume_oi': True}
            if provenance.get('coverage') == 'PARTIAL':
                bar['usage_limits'] = {'precise_price_levels': False, 'volume_oi': False}
            raw = str(Decimal(bar['open_interest']) - Decimal(previous['open_interest'])) if previous else None
            contiguous = False
            if previous:
                if period == '1d':
                    contiguous = FuturesTradingCalendar.previous_trading_day(dt.date()).strftime('%Y%m%d') == previous['trading_day']
                else:
                    contiguous = any(bar['bar_time'] in p and previous['bar_time'] in p
                                     and p[bar['bar_time']] == p[previous['bar_time']] + 1 for p in positions)
            bar['open_interest_change_raw'] = raw
            bar['usage_limits']['single_period_oi'] = contiguous and period == '1d'
            bar['open_interest_change'] = raw if bar['usage_limits']['single_period_oi'] else None
            bar['open_interest_comparison'] = {'from': previous['bar_time'] if previous else None,
                'to': bar['bar_time'], 'span_seconds': (dt - datetime.fromisoformat(previous['bar_time'])).total_seconds() if previous else None,
                'continuity': 'CANDIDATE_CONTIGUOUS' if contiguous and period != '1d' else 'CONTIGUOUS' if contiguous else 'GAP_OR_UNVERIFIED'}
            if previous and not contiguous:
                checks['warnings'].append('OI_COMPARISON_GAP_OR_UNVERIFIED')
                bar['usage_limits']['volume_oi'] = False
            if Decimal(str(bar.get('volume') or 0)) == 0 and raw is not None and Decimal(raw) != 0:
                checks['warnings'].append('ZERO_VOLUME_OI_CHANGE')
                bar['usage_limits']['volume_oi'] = False
            previous = bar
        if grids:
            required = 10 if period == '60m' else 20
            candidates = {}
            for kind, labels in grids.items():
                semantics = 'START' if kind == 'LOCAL_START' else 'END'
                observed = {bar['bar_time'] for bar in rows
                            if _bar_provenance(bar.get('provenance')).get('label_semantics')
                            in (None, 'UNKNOWN', semantics)}
                count = 0
                for label in reversed(labels):
                    if label not in observed:
                        break
                    count += 1
                candidates[kind] = {'recent_contiguous_count': count,
                    'missing_recent_labels': [label for label in labels[-required:] if label not in observed]}
            checks['period_checks'][period] = {'required': required,
                'label_status': 'VERIFIED' if known_labels else 'UNVERIFIED',
                'continuity_status': 'UNVERIFIED', 'candidates': candidates}
            if max(item['recent_contiguous_count'] for item in candidates.values()) < required:
                checks['blocking_reasons'].append(f'INSUFFICIENT_CONTIGUOUS_{period.upper()}')
    daily = {bar['trading_day']: bar for bar in periods.get('1d', [])}
    for period in ('60m', '15m'):
        for bar in periods.get(period, []):
            day = daily.get(bar['trading_day'])
            if not day or not all(Decimal(item[key]).is_finite()
                                  for item in (bar, day) for key in ('high', 'low')):
                continue
            if day and (Decimal(bar['high']) > Decimal(day['high']) + tick
                        or Decimal(bar['low']) < Decimal(day['low']) - tick):
                checks['warnings'].append('CROSS_PERIOD_RANGE_UNVERIFIED')
                bar['usage_limits']['precise_price_levels'] = False
                day['usage_limits']['precise_price_levels'] = False
                checks['range_checks'].append({'period': period, 'bar_time': bar['bar_time'],
                    'trading_day': bar['trading_day'], 'minute_high': bar['high'],
                    'minute_low': bar['low'], 'daily_high': day['high'], 'daily_low': day['low'],
                    'tick': str(tick), 'status': 'UNVERIFIED_COVERAGE'})
    for key in ('warnings', 'blocking_reasons'):
        checks[key] = list(dict.fromkeys(checks[key]))
    checks['usage_limits']['unverified_time_labels'] = 'TIME_LABEL_UNVERIFIED' in checks['warnings']
    return checks


def _load_future_bars(instrument: str, period: str) -> List[BarData]:
    """Read one historical series for a new forecast; never merge polling aggregates.

    Higher-period history uses END labels while local aggregates use START labels.
    Equal timestamps therefore do not identify interchangeable bars. Keep the
    provider's provenance and UNKNOWN coverage rather than claiming completeness.
    This fetch does not write MySQL/Redis or touch the running bar engine.
    """
    from backend.storage.history_backfill import fetch_sina_daily_bars, fetch_sina_minute_bars
    records = (fetch_sina_daily_bars(instrument) if period == '1d'
               else fetch_sina_minute_bars(instrument, period))
    return [_bar_from_dict(dict(record, instrument=record['instrument_id'],
                               bar_time=record['bar_time'].isoformat())) for record in records]


def build_future_snapshot(instrument: str, quote: dict, requirements: str,
                          now: datetime | None = None) -> tuple[Dict[str, Any], str]:
    from backend.ai_analysis import future_trend
    now = now or datetime.now(timezone(timedelta(hours=8)))
    local_now = now.replace(tzinfo=None)
    name, multiple, tick = _instrument_meta(instrument)
    market_day = str(quote['trading_day'])
    periods = {}
    row_issues = []
    for period, limit in [('1d', 100), ('60m', 100), ('15m', 120)]:
        closed = []
        try:
            history = _load_future_bars(instrument, period)
        except Exception:
            # Source failure remains explicit; stale/partial local bars cannot
            # turn a failed history request into an apparently valid forecast.
            history = []
            row_issues.append(f'HISTORY_FETCH_FAILED_{period.upper()}')
        for bar in sorted(history, key=lambda item: item.bar_time):
            if bar.instrument.upper() != instrument.upper():
                row_issues.append('BAR_INSTRUMENT_MISMATCH')
                continue
            if bar.period != period or bar.trading_day != FuturesTradingCalendar.get_trading_day(bar.bar_time):
                row_issues.append('BAR_TRADING_DAY_MISMATCH' if bar.period == period else 'BAR_PERIOD_MISMATCH')
                continue
            end = _bar_end(bar, instrument)
            if end <= local_now and FuturesTradingCalendar.is_trading_day(
                    datetime.strptime(bar.trading_day, '%Y%m%d').date()):
                row = _closed_bar_json(bar)
                row['trading_day'] = bar.trading_day
                row['open_interest_change'] = (str(Decimal(row['open_interest']) - Decimal(closed[-1]['open_interest']))
                                               if closed else None)
                closed.append(row)
        periods[period] = closed[-limit:]
    market_checks = _future_market_quality(instrument, periods, tick, local_now)
    # 沿用行情健康约束，但未来趋势不依赖当天的 1 分钟样本数。
    minute_bars = [_closed_bar_json(b) for b in _load_bars(instrument, '1m', 5)]
    quality = market_quality(instrument, quote, minute_bars, local_now)
    quality['blocking_reasons'].extend(row_issues + market_checks['blocking_reasons'])
    quality['warnings'].extend(market_checks['warnings'])
    quality['market_checks'] = market_checks
    quality['market_checks']['series_selection'] = 'SINA_HISTORY_ONLY'
    for period, count in [('1d', 20), ('60m', 10), ('15m', 20)]:
        if len(periods[period]) < count:
            quality['blocking_reasons'].append(f'INSUFFICIENT_{period.upper()}')
    quality['blocking_reasons'] = list(dict.fromkeys(quality['blocking_reasons']))
    quality['can_judge'] = not quality['blocking_reasons']
    quality['warnings'] = list(dict.fromkeys(quality['warnings'] + quality['blocking_reasons']))
    quality['bar_count'] = sum(map(len, periods.values()))
    quality['period_counts'] = {p:len(rows) for p,rows in periods.items()}
    public = {'status':'MISSING', 'cache_state':'NOT_FROZEN', 'sources':[], 'background_sources':[],
              'missing_dimensions':list(future_trend.EXTERNAL_DIMENSIONS), 'errors':[],
              'research':{'status':'NOT_REQUESTED','events':[]}}
    raw_quote = _safe_quote(quote)
    # Sina quote aggregates and historical K bars do not guarantee the same session scope.
    trend_quote = {key:value for key,value in raw_quote.items()
                   if key not in {'day_open','day_high','day_low','avg_price','volume','turnover'}}
    snap = {
        'schema_version':'1.1', 'analysis_target':'FUTURE_TREND', 'review_policy_version':'1',
        'analysis_requirements':requirements,
        'instrument':{'symbol':instrument,'name':name,'price_tick':str(tick),'volume_multiple':multiple},
        'market':{'feed':'sina','timezone':'Asia/Shanghai','trading_day':market_day,
                  'snapshot_time':now.isoformat(), 'quote':trend_quote, 'raw_quote':raw_quote, 'bars_1m':[],
                  'data_scope':'报价仅提供时点价格和持仓；K线成交量是来源所报周期量，不保证覆盖完整交易日，不能与报价累计量互证。每根柱的provenance记录来源、开始/结束标签及覆盖状态，旧行保留未知。持仓变化保留原差和比较跨度；分钟单周期差仍为null，日线仅相邻交易日保留单周期差。已知标签按对应网格检查，部分覆盖阻断判断，未知覆盖不显示检查通过；以quality.market_checks及各柱usage_limits限制精确价位和量仓推理。闭市期间没有新增国内K线属正常，外部资料截止至snapshot_time。',
                  'forming_bar_1m':None, 'daily_bars_1d':periods['1d'],
                  'bars_60m':periods['60m'], 'bars_15m':periods['15m']},
        'rule_analysis':None, 'quality':quality, 'public_materials':public,
        'future_targets':future_trend.targets(instrument, local_now, market_day,
                                             float(quote['last_price'])),
    }
    snap['contract_lifecycle'] = future_trend.contract_lifecycle(instrument, snap['future_targets'], local_now)
    canonical = json.dumps(snap, ensure_ascii=False, sort_keys=True, separators=(',',':'))
    return snap, hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def build_snapshot(instrument: str, analysis_target: str = "TODAY",
                   analysis_requirements: str = "",
                   session_start_time: str | None = None) -> tuple[Dict[str, Any], str]:
    instrument = instrument.strip().upper()
    if analysis_target not in {"TODAY", "CURRENT_SESSION", "NEXT_OPEN", "FUTURE_TREND"}:
        raise ValueError("analysis_target 仅支持 TODAY、CURRENT_SESSION 或 NEXT_OPEN")
    redis_client = RedisClient.get_client()
    quote = redis_client.hgetall(f"quote:sina:{instrument}")
    if not quote:
        quote = redis_client.hgetall(f"quote:{instrument}")
    if not quote:
        raise ValueError("未找到该合约的模拟行情")
    trading_day = str(quote.get("trading_day") or "")
    quote_time = str(quote.get("timestamp") or "")
    if not trading_day or not quote_time:
        raise ValueError("行情缺少 trading_day 或 timestamp")

    if analysis_target == 'FUTURE_TREND':
        return build_future_snapshot(instrument, quote, analysis_requirements)

    bars_1m = _load_bars(instrument, "1m", 5000, trading_day)
    session_target = None
    pre_open = False
    if analysis_target == "CURRENT_SESSION":
        options = FuturesTradingCalendar.analysis_session_options(datetime.now(), instrument)
        session_target = next(
            (item for item in options if item["start_time"] == session_start_time), None)
        if not session_target:
            raise ValueError("所选交易时段不属于当前合约")
        if session_target["status"] == "PASSED":
            raise ValueError("所选交易时段已经结束，不能再分析")
        session_start = datetime.fromisoformat(session_target["start_time"])
        session_end = datetime.fromisoformat(session_target["end_time"])
        if session_target["status"] == "UPCOMING":
            # 未开盘: 不再发空 K 线, 改用开盘前最近已闭合的 1 分钟 K 线做盘前展望
            # (可能属于同交易日夜盘, 也可能属于此前交易日)
            pre_open = True
            bars_1m = [bar for bar in _load_bars(instrument, "1m", 5000)
                       if bar.bar_time < session_start][-PRE_OPEN_CONTEXT_BARS:]
        else:
            bars_1m = [bar for bar in bars_1m if session_start <= bar.bar_time < session_end]
    daily_bars = _load_bars(instrument, "1d", 60)
    bars_5m = _load_bars(instrument, "5m", 2500)
    name, volume_multiple, price_tick = _instrument_meta(instrument)
    current = forming_bar(redis_client, "sina", instrument, "1m")
    rule_analysis = None if analysis_target == "CURRENT_SESSION" else RealtimeAnalyzer.analyze(
        instrument=instrument, instrument_name=name, quote=quote,
        daily_bars=daily_bars, bars_5m=bars_5m,
        volume_multiple=volume_multiple, price_tick=price_tick,
        feed="sina", bars_1m=bars_1m, forming_1m=current,
    )
    # Validate the exact evidence sent to the model. A morning collection gap
    # outside CURRENT_SESSION must not block an otherwise healthy afternoon.
    # TODAY/NEXT_OPEN retain their full-day evidence and strict volume checks.
    quality = market_quality(instrument, quote, [_closed_bar_json(bar) for bar in bars_1m])
    latency = quality['quote_latency_seconds']
    warnings = quality['warnings']
    if not bars_1m:
        warnings.append("NO_CLOSED_1M_BARS")
        quality["can_judge"] = False
        quality["blocking_reasons"].append("NO_CLOSED_1M_BARS")
    if pre_open:
        warnings.append("PRE_OPEN_OUTLOOK")
    daily_context = None
    if pre_open:
        # 只带此前交易日的已闭合日线; 当前交易日的日线此时必然未走完, 不得当作闭合数据
        daily_context = [_closed_bar_json(bar) for bar in daily_bars
                         if bar.trading_day != trading_day][-PRE_OPEN_DAILY_BARS:]
    # 盘时段/盘前缺少 rule_analysis, 补一份日线级趋势背景供模型参考
    trend_context = None
    if analysis_target == "CURRENT_SESSION":
        trend_context = _build_trend_context(daily_bars, bars_5m, trading_day, quote)
    snapshot_time = datetime.now(timezone.utc).astimezone()
    safe_quote = _safe_quote(quote)
    session_forecast = None
    if analysis_target == "CURRENT_SESSION":
        for key in ("day_open", "day_high", "day_low", "avg_price", "volume", "turnover"):
            safe_quote.pop(key, None)
        if current:
            current_time = datetime.fromisoformat(str(current.get("bar_time")))
            if not session_start <= current_time < session_end:
                current = None
        if session_target["status"] in {"ACTIVE", "UPCOMING"} and quality["can_judge"]:
            reference_price = safe_quote.get("last_price")
            if reference_price in (None, "") and bars_1m:
                reference_price = str(bars_1m[-1].close_price)
            try:
                if Decimal(str(reference_price)) <= 0:
                    raise ValueError
            except (ValueError, ArithmeticError):
                raise ValueError("盘时段预测缺少有效参考价")
            session_forecast = {
                "start_time": session_target["start_time"] if pre_open else snapshot_time.isoformat(),
                "end_time": session_target["end_time"],
                "reference_price": str(reference_price),
            }
            if pre_open:
                session_forecast.update({
                    "kind": "PRE_OPEN", "reference_time": quote_time,
                    "trading_day": FuturesTradingCalendar.get_trading_day(session_start),
                })
            else:
                session_forecast.update(window_policy='RESULT_AVAILABLE_10M', horizon_minutes=10)
    missing_intervals = _missing_intervals(bars_1m, instrument, trading_day)
    if missing_intervals:
        warnings.append("MISSING_1M_INTERVALS")
        if quality['market_state'] == 'TRADING' and not pre_open:
            quality['can_judge'] = False
            quality['blocking_reasons'].append('MISSING_1M_INTERVALS')
    snapshot = {
        "schema_version": "1.1",
        "analysis_target": analysis_target,
        "analysis_requirements": analysis_requirements,
        "instrument": {
            "symbol": instrument, "name": name, "price_tick": str(price_tick),
            "volume_multiple": volume_multiple,
        },
        "market": {
            "feed": "sina", "timezone": "Asia/Shanghai", "trading_day": trading_day,
            "snapshot_time": snapshot_time.isoformat(), "quote": safe_quote,
            "bars_1m": [_closed_bar_json(bar) for bar in bars_1m],
            "forming_bar_1m": _forming_bar_json(current, trading_day),
            **({"daily_bars_1d": daily_context} if daily_context is not None else {}),
        },
        "rule_analysis": rule_analysis,
        **({"trend_context": trend_context} if trend_context else {}),
        "quality": {
            **quality,
            "bar_count": len(bars_1m),
            "first_bar_time": bars_1m[0].bar_time.isoformat() if bars_1m else None,
            "last_closed_bar_time": bars_1m[-1].bar_time.isoformat() if bars_1m else None,
            "quote_latency_seconds": latency, "missing_intervals": missing_intervals,
            "warnings": warnings,
        },
    }
    if analysis_target == "NEXT_OPEN":
        snapshot["next_open_target"] = FuturesTradingCalendar.next_open_target(
            instrument, snapshot_time.replace(tzinfo=None), trading_day)
    elif analysis_target == "CURRENT_SESSION":
        snapshot["session_target"] = session_target
        if session_forecast:
            snapshot["session_forecast"] = session_forecast
    if analysis_target in {"TODAY", "NEXT_OPEN"}:
        if analysis_target == "NEXT_OPEN":
            target = snapshot["next_open_target"]
            bar_time = datetime.fromisoformat(target["start_time"])
            target_day, field = target["trading_day"], "open_price"
            due = bar_time + timedelta(minutes=2)
        else:
            target_day = trading_day
            day = datetime.strptime(trading_day, "%Y%m%d")
            close = FuturesTradingCalendar.session_end(day.replace(hour=14), instrument)
            bar_time, due, field = close - timedelta(minutes=1), close + timedelta(minutes=2), "close_price"
        snapshot["settlement_target"] = {
            "trading_day": target_day, "bar_time": bar_time.isoformat(),
            "due_time": due.isoformat(), "price_field": field,
            "reference_price": safe_quote.get("last_price"),
        }
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return snapshot, hashlib.sha256(canonical.encode("utf-8")).hexdigest()
