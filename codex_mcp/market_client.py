"""Read existing loopback market APIs without credentials or trading imports."""
import json
import re
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from backend.common.trading_time import FuturesTradingCalendar as Calendar

TZ = timezone(timedelta(hours=8))
PERIODS = ("1m", "5m", "15m", "30m", "60m", "1d")
MAX_BODY = 8 * 1024 * 1024


class MarketError(ValueError):
    """An actionable, bounded error safe to return through MCP."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]{1,6}[0-9]{1,4}", value.strip()):
        raise MarketError("INVALID_INSTRUMENT: 请先搜索确认具体合约代码，例如 FU2611 或 FU0；不能猜测月份")
    return value.strip().upper()


def _period(value):
    if value not in PERIODS:
        raise MarketError("INVALID_PERIOD: 支持 1m/5m/15m/30m/60m/1d")
    return value


def _integer(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise MarketError(f"INVALID_LIMIT: 根数必须为 1 至 {maximum} 的整数")
    return value


def _date(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
            raise ValueError()
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        raise MarketError("INVALID_TRADING_DAY: 请使用有效交易日 YYYYMMDD") from None
    return value


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)
    except (TypeError, ValueError):
        raise MarketError("INVALID_MARKET_TIMESTAMP: 行情时间字段无效，不能推断截止时间") from None


def _provenance(row):
    value = row.get("provenance_json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    return value if isinstance(value, dict) else {}


class MarketClient:
    def __init__(self, base_url="http://127.0.0.1:8000", clock=None):
        url = urlsplit(base_url)
        try:
            port = url.port
        except ValueError:
            raise MarketError("INVALID_BASE_URL: 本机接口地址端口无效") from None
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or url.username is not None or url.password is not None or url.path not in {"", "/"}
                or url.query or url.fragment or port is None):
            raise MarketError("INVALID_BASE_URL: 只允许带端口的本机 loopback HTTP 地址，不接受凭据或额外路径")
        self.base_url = base_url.rstrip("/")
        self.clock = clock or (lambda: datetime.now(TZ))
        self.http = build_opener(ProxyHandler({}), NoRedirect())

    def _get(self, path, **params):
        query = urlencode({k: v for k, v in params.items() if v is not None})
        url = self.base_url + path + ("?" + query if query else "")
        try:
            with self.http.open(Request(url, headers={"Accept": "application/json"}), timeout=15) as response:
                raw = response.read(MAX_BODY + 1)
        except HTTPError as exc:
            raise MarketError(f"HTTP_{exc.code}: 本机行情接口拒绝请求；404 表示暂无对应数据，401/403 需核对访问配置") from None
        except (URLError, OSError, TimeoutError):
            raise MarketError("SERVICE_UNAVAILABLE: 无法连接本机期货系统；请启动项目根目录 start.bat 并核查 8000 端口") from None
        if len(raw) > MAX_BODY:
            raise MarketError("RESPONSE_TOO_LARGE: 行情响应超过 8MB，请减少根数")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise MarketError("INVALID_JSON: 本机接口未返回有效 JSON") from None
        if not isinstance(data, dict):
            raise MarketError("INVALID_RESPONSE: 行情接口必须返回对象")
        if data.get("feed") not in {None, "sina"}:
            raise MarketError("UNEXPECTED_FEED: 本适配器仅读取新浪研究行情")
        return data

    def _meta(self):
        now = self.clock().astimezone(TZ)
        return {"retrieved_at": now.isoformat(), "timezone": "Asia/Shanghai", "read_only": True}

    @staticmethod
    def _identity(data, instrument, period=None):
        if (data.get("instrument") not in {None, instrument}
                or (period is not None and data.get("period") not in {None, period})):
            raise MarketError("IDENTITY_MISMATCH: 返回数据的合约或周期与请求不一致，不能用于分析")

    def status(self):
        return {**self._get("/api/v1/feed-status"), **self._meta(),
                "note": "采集线程运行不等于行情实时；分析还需查看指定合约的数据截止时间。"}

    def search(self, search="", limit=30):
        if not isinstance(search, str) or len(search) > 32:
            raise MarketError("INVALID_SEARCH: 搜索词最多 32 字符")
        _integer(limit, 200)
        return {**self._get("/api/v1/universe", search=search or None, limit=limit), **self._meta(),
                "note": "搜索不新增订阅、不触发回补；主连和月份合约的数据分别查询。"}

    def dates(self, instrument, period="1m"):
        instrument, period = _symbol(instrument), _period(period)
        data = self._get(f"/api/v1/bars/{instrument}/dates", period=period)
        self._identity(data, instrument, period)
        return {**data, **self._meta()}

    def quote(self, instrument):
        instrument = _symbol(instrument)
        quote = self._get(f"/api/v1/quotes/{instrument}")
        if quote.get("instrument") not in {None, instrument}:
            raise MarketError("QUOTE_IDENTITY_MISMATCH: 报价合约与请求不一致")
        now = self.clock().astimezone(TZ)
        warnings = []
        age = None
        if quote.get("timestamp"):
            age = round((now - _timestamp(quote["timestamp"])).total_seconds(), 1)
            if age < 0:
                warnings.append("FUTURE_QUOTE_TIMESTAMP")
        else:
            warnings.append("UNKNOWN_QUOTE_TIMESTAMP")
        opened = Calendar.is_trading_time_for(now.replace(tzinfo=None), instrument)
        freshness = ("UNKNOWN" if age is None or age < 0 else "MARKET_CLOSED" if not opened
                     else "STALE" if age > 120 else "RECENT")
        if freshness == "STALE":
            warnings.append("STALE_QUOTE_DURING_SESSION")
        return {"instrument": instrument, "quote": quote, **self._meta(), "age_seconds": age,
                "market_open_for_instrument": opened, "freshness": freshness, "warnings": warnings}

    def _context(self, instrument):
        try:
            quote = self.quote(instrument)
            return {"market_timestamp": quote["quote"].get("timestamp"),
                    "market_freshness": quote["freshness"], "market_warnings": quote["warnings"]}
        except MarketError:
            return {"market_timestamp": None, "market_freshness": "UNKNOWN",
                    "market_warnings": ["QUOTE_CONTEXT_UNAVAILABLE"]}

    def indicators(self, instrument, period="15m"):
        instrument, period = _symbol(instrument), _period(period)
        data = self._get(f"/api/v1/indicators/{instrument}", period=period)
        self._identity(data, instrument, period)
        return {"instrument": instrument, "period": period, "indicators": data,
                **self._meta(), **self._context(instrument),
                "note": "指标沿用系统算法和缓存；空 VWAP 不能当作 0；market_timestamp 是报价时间，不是指标计算时间。"}

    def signal(self, instrument):
        instrument = _symbol(instrument)
        data = self._get(f"/api/v1/signals/{instrument}")
        explanation = self._get(f"/api/v1/signals/{instrument}/explanation")
        self._identity(data, instrument)
        self._identity(explanation, instrument)
        return {"instrument": instrument, "signal": data, "explanation": explanation,
                **self._meta(), **self._context(instrument),
                "note": "客观规则观察信号，仅供研究；报价时间不等于信号触发时间。"}

    @staticmethod
    def _bar_end(row, label, semantics, period, instrument):
        if period == "1d":
            if semantics == "DAILY_CLOSE":
                return label
            day = datetime.strptime(_date(row.get("trading_day")), "%Y%m%d")
            return Calendar.session_end(datetime.combine(day.date(), time(15)), instrument).replace(tzinfo=TZ)
        if semantics == "END":
            return label
        end = label + timedelta(minutes=int(period[:-1]))
        session_end = Calendar.session_end(label.replace(tzinfo=None), instrument).replace(tzinfo=TZ)
        return min(end, session_end) if session_end >= label else end

    @staticmethod
    def _missing(rows, period, instrument):
        """Check comparable labels within a continuous session, not full source coverage."""
        missing = []
        for left, right in zip(rows, rows[1:]):
            if period == "1d":
                start = datetime.strptime(left["trading_day"], "%Y%m%d").date()
                stop = datetime.strptime(right["trading_day"], "%Y%m%d").date()
                if stop - start > timedelta(days=366):
                    continue
                day = start + timedelta(days=1)
                while day < stop:
                    if day.year == 2026 and Calendar.is_trading_day(day):
                        missing.append(day.strftime("%Y%m%d"))
                    day += timedelta(days=1)
            else:
                semantics = left["time_label_semantics"]
                if semantics not in {"START", "END"} or semantics != right["time_label_semantics"]:
                    continue
                start, stop = _timestamp(left["bar_time"]), _timestamp(right["bar_time"])
                offset = timedelta(seconds=1) if semantics == "END" else timedelta(0)
                left_pivot, right_pivot = (start - offset).replace(tzinfo=None), (stop - offset).replace(tzinfo=None)
                if (start.year != 2026 or stop.year != 2026
                        or not Calendar.is_trading_time_for(left_pivot, instrument)
                        or not Calendar.is_trading_time_for(right_pivot, instrument)
                        or Calendar.session_end(left_pivot, instrument) != Calendar.session_end(right_pivot, instrument)):
                    continue
                cursor = start + timedelta(minutes=int(period[:-1]))
                while cursor < stop:
                    missing.append(cursor.isoformat())
                    cursor += timedelta(minutes=int(period[:-1]))
            if len(missing) >= 50:
                return missing[:50]
        return missing

    def bars(self, instrument, period="15m", limit=200, trading_day=None, closed_only=True, include_provenance=False):
        instrument, period, limit = _symbol(instrument), _period(period), _integer(limit, 1000)
        if trading_day is not None:
            _date(trading_day)
        if type(closed_only) is not bool or type(include_provenance) is not bool:
            raise MarketError("INVALID_BOOLEAN: closed_only 和 include_provenance 必须为布尔值")
        payload = self._get(f"/api/v1/bars/{instrument}", period=period,
                            limit=min(limit + (1 if closed_only else 0), 1000), date=trading_day)
        if payload.get("instrument") != instrument or payload.get("period") != period:
            raise MarketError("BAR_IDENTITY_MISMATCH: K 线合约或周期与请求不一致")
        raw_rows = payload.get("bars")
        if not isinstance(raw_rows, list) or len(raw_rows) > 1000:
            raise MarketError("INVALID_BARS: 上游 K 线数组无效或超过上限")
        now = self.clock().astimezone(TZ)
        rows, warnings, excluded = [], set(), 0
        semantics_seen = set()
        for raw in raw_rows:
            if not isinstance(raw, dict) or raw.get("instrument") != instrument or raw.get("period") != period:
                raise MarketError("BAR_IDENTITY_MISMATCH: 返回了其他合约或周期的 K 线")
            _date(raw.get("trading_day"))
            if trading_day is not None and raw["trading_day"] != trading_day:
                raise MarketError("BAR_IDENTITY_MISMATCH: 返回了其他交易日的 K 线")
            try:
                prices = [Decimal(str(raw[key])) for key in ("open_price", "high_price", "low_price", "close_price")]
                quantities = [Decimal(str(raw[key])) for key in ("volume", "open_interest", "turnover")]
                oi_change = Decimal(str(raw.get("open_interest_change", "0")))
                if (not all(p.is_finite() and p > 0 for p in prices) or prices[1] < max(prices[0], prices[3])
                        or prices[2] > min(prices[0], prices[3])
                        or not all(q.is_finite() and q >= 0 for q in quantities) or not oi_change.is_finite()):
                    raise ValueError()
            except (KeyError, InvalidOperation, ValueError, TypeError):
                raise MarketError("INVALID_BAR_VALUES: OHLC 或成交量无效，不能用于分析") from None
            label = _timestamp(raw.get("bar_time"))
            provenance = _provenance(raw)
            semantics = provenance.get("label_semantics", "UNKNOWN")
            if semantics not in {"START", "END", "DAILY_CLOSE"}:
                semantics = "UNKNOWN"
            end = self._bar_end(raw, label, semantics, period, instrument)
            forming = (end > now or raw.get("is_closed") is False
                       or raw.get("is_forming") is True or raw.get("forming") is True)
            if closed_only and forming:
                excluded += 1
                continue
            semantics_seen.add(semantics)
            if semantics == "UNKNOWN":
                warnings.add("UNKNOWN_TIME_LABEL_SEMANTICS")
            if provenance.get("coverage") not in {"COMPLETE", "FULL"}:
                warnings.add("SOURCE_COVERAGE_UNVERIFIED")
            if provenance.get("unavailable_fields"):
                warnings.add("SOURCE_FIELDS_UNAVAILABLE")
            if label.year != 2026 or now.year != 2026:
                warnings.add("CALENDAR_YEAR_UNVERIFIED")
            if period == "1d" and semantics == "UNKNOWN":
                warnings.add("DAILY_CLOSE_TIME_ASSUMED")
            output = dict(raw)
            if not include_provenance:
                output.pop("provenance_json", None)
                output["provenance"] = {k: provenance[k] for k in
                                        ("source", "provider", "coverage", "unavailable_fields") if k in provenance}
            rows.append({**output, "time_label_semantics": semantics, "bar_end_time": end.isoformat(),
                         "closure_status": "FORMING" if forming else "CLOSED"})
        rows.sort(key=lambda row: _timestamp(row["bar_time"]))
        rows = rows[-limit:]
        if len(semantics_seen) > 1:
            warnings.add("MIXED_TIME_LABEL_SEMANTICS")
        if len(rows) < limit:
            warnings.add("LESS_THAN_REQUESTED")
        if not rows:
            warnings.add("NO_BARS")
        missing = self._missing(rows, period, instrument)
        if missing:
            warnings.add("INTRA_SESSION_GAPS" if period != "1d" else "TRADING_DAY_GAPS")
        closed = [row for row in rows if row["closure_status"] == "CLOSED"]
        cutoff = max((_timestamp(row["bar_end_time"]) for row in closed), default=None)
        age = round((now - cutoff).total_seconds(), 1) if cutoff else None
        opened = Calendar.is_trading_time_for(now.replace(tzinfo=None), instrument)
        stale_limit = 86400 if period == "1d" else int(period[:-1]) * 60 + 120
        freshness = ("NO_CLOSED_DATA" if cutoff is None else "MARKET_CLOSED" if not opened
                     else "STALE" if age > stale_limit else "RECENT")
        if freshness == "STALE":
            warnings.add("OLD_CLOSED_BARS_DURING_SESSION")
        return {"instrument": instrument, "period": period, "trading_day": trading_day,
                "requested_count": limit, "count": len(rows), "closed_only": closed_only,
                "forming_excluded": excluded, "source": payload.get("source"), "feed": "sina",
                **self._meta(), "latest_bar_time": rows[-1]["bar_time"] if rows else None,
                "latest_bar_end_time": cutoff.isoformat() if cutoff else None,
                "latest_closed_bar_age_seconds": age, "market_open_for_instrument": opened,
                "freshness": freshness, "bars": rows,
                "quality": {"warnings": sorted(warnings), "missing_labels": missing,
                            "gap_check_scope": "仅返回窗口内相同时间标签口径的连续小节；不证明跨小节或全部历史完整",
                            "coverage_status": "UNVERIFIED", "calendar_verified_year": 2026},
                "note": "保留原始价格精度；默认仅附来源摘要，include_provenance=true 返回完整来源字段。未知时间标签按起点保守估计闭合。缺失数据不补造，不自动订阅或回补。"}
