"""新浪财经实时行情采集器 (免费免鉴权, 批量轮询快照接口)。

- 覆盖国内六大交易所活跃合约 (沪/大/郑/能源/广/中金)。
- 商品合约解析: 名称,时间,开,高,低,?,买价,卖价,最新,结算,昨结,买量,卖量,持仓,成交量,...
- 中金所合约无名称/时间前缀, 采用独立数值布局解析。
- 快照含成交量为全日累计值, 与 CTP 口径一致, 可直接驱动现有 Bar 引擎与模拟撮合。
- 休市时快照静止, 依据 (时间,最新价,累计量,持仓) 变化判重, 避免生成垃圾 Tick。
"""

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.common.config import cfg
from backend.common.product_specs import get_exchange
from backend.common.trading_time import FuturesTradingCalendar
from backend.models.market_data import TickData
from backend.notifier.notifier import notifier
from backend.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

SINA_HQ_URL = "https://hq.sinajs.cn/list={batch}"
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
}
WATCHLIST_KEY = "watchlist:subscribed"
BATCH_SIZE = 40
# 中金所快照无时间戳, 休市时以本地时间打戳会长出幻影分钟线; 记录上一轮数值签名用于判重
_cffex_prev: Dict[str, Tuple[str, str, str, str, str, str]] = {}


def fetch_sina_quotes(symbols: List[str], timeout: float = 6.0) -> Dict[str, Dict[str, Any]]:
    """批量拉取并解析新浪实时快照, 返回 {symbol: quote_dict}"""
    quotes: Dict[str, Dict[str, Any]] = {}
    # 国内行情直连，避免继承系统代理后因代理出口被新浪拒绝。
    # 独立 opener 不影响 AI / 公开资料等其他请求的代理与 TLS 校验。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for i in range(0, len(symbols), BATCH_SIZE):
        chunk = symbols[i:i + BATCH_SIZE]
        batch = ",".join(f"nf_{s}" for s in chunk)
        req = urllib.request.Request(SINA_HQ_URL.format(batch=batch), headers=HTTP_HEADERS)
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("gbk", errors="replace")
        quotes.update(parse_sina_payload(raw))
        if i + BATCH_SIZE < len(symbols):
            time.sleep(0.15)
    return quotes


def parse_sina_payload(raw: str) -> Dict[str, Dict[str, Any]]:
    """解析 hq.sinajs.cn 响应文本 (纯函数, 便于单元测试)。

    商品布局 (逗号分隔, 0 起):
      0名称 1时间HHMMSS 2开 3高 4低 5? 6买价 7卖价 8最新 9结算 10昨结
      11买量 12卖量 13持仓 14成交量 15交易所 16品种 17日期YYYY-MM-DD
      27 当日均价(分时均线, VWAP) —— 源端权威值, 实测 FU2611=4507.188 与
         新浪网页显示的「均 4507.19」精确一致; 系统早期版本自己用 bar 收盘价
         加权重算 (4506.61), 与源端差 0.013%, 是「指标图与网页不一致」的根因。
    中金所布局 (无名称/时间前缀):
      0开 1高 2低 3最新 4成交量 5成交额 6持仓 ... 13昨结
    """
    quotes: Dict[str, Dict[str, Any]] = {}

    for line in raw.splitlines():
        line = line.strip()
        if "hq_str_nf_" not in line or '""' in line:
            continue
        try:
            key_part, _, data_part = line.partition('="')
            symbol = key_part.replace("var hq_str_nf_", "").strip()
            fields = data_part.rstrip('";').split(",")
            if len(fields) < 14:
                continue

            if fields[0] and fields[0][0].isdigit():
                # 中金所数值布局 (无时间戳字段, 采集器只能用本地时间打戳):
                # 休市时新浪返回的是静态快照, 若照单.emit 会以"当前时间"长出 volume=0 的幻影分钟线;
                # 因此数值签名与上一轮一致时跳过该品种 (开市后数值变化自动恢复推送)
                ts = datetime.now()
                sig = (fields[3], fields[0], fields[1], fields[2], fields[4], fields[6])
                if _cffex_prev.get(symbol) == sig:
                    continue
                _cffex_prev[symbol] = sig
                quotes[symbol] = {
                    "timestamp": ts,
                    "time_str": ts.strftime("%H%M%S"),
                    "last_price": Decimal(fields[3]),
                    "open_price": Decimal(fields[0]),
                    "high_price": Decimal(fields[1]),
                    "low_price": Decimal(fields[2]),
                    "bid_price_1": None,
                    "ask_price_1": None,
                    "bid_volume_1": 0,
                    "ask_volume_1": 0,
                    "volume": int(Decimal(fields[4])),
                    "open_interest": Decimal(fields[6]),
                    "prev_settle": Decimal(fields[13]) if len(fields) > 13 and fields[13] else None,
                }
            else:
                t = fields[1]
                ts = datetime.strptime(
                    f"{fields[17]} {t[0:2]}:{t[2:4]}:{t[4:6]}",
                    "%Y-%m-%d %H:%M:%S",
                )
                quotes[symbol] = {
                    "timestamp": ts,
                    "time_str": t,
                    "last_price": Decimal(fields[8]),
                    "open_price": Decimal(fields[2]),
                    "high_price": Decimal(fields[3]),
                    "low_price": Decimal(fields[4]),
                    "bid_price_1": Decimal(fields[6]) if Decimal(fields[6]) > 0 else None,
                    "ask_price_1": Decimal(fields[7]) if Decimal(fields[7]) > 0 else None,
                    "bid_volume_1": int(Decimal(fields[11])) if fields[11] else 0,
                    "ask_volume_1": int(Decimal(fields[12])) if fields[12] else 0,
                    "volume": int(Decimal(fields[14])),
                    "open_interest": Decimal(fields[13]),
                    "prev_settle": Decimal(fields[10]) if fields[10] else None,
                    # 字段 27 = 源端当日均价 (分时均线)。缺失/为 0 时置 None,
                    # 由下游回退到自算 VWAP, 避免用 0 画出穿底均线。
                    "avg_price": (Decimal(fields[27])
                                  if len(fields) > 27 and fields[27]
                                  and Decimal(fields[27]) > 0 else None),
                }
        except Exception as e:
            logger.debug(f"解析新浪行情行失败: {line[:60]}... ({e})")
            continue

    return quotes


def quote_to_tick(symbol: str, q: Dict[str, Any]) -> TickData:
    return TickData(
        instrument=symbol,
        exchange=get_exchange(symbol),
        trading_day=FuturesTradingCalendar.get_trading_day(q["timestamp"]),
        timestamp=q["timestamp"],
        last_price=q["last_price"],
        bid_price_1=q["bid_price_1"],
        bid_volume_1=q["bid_volume_1"],
        ask_price_1=q["ask_price_1"],
        ask_volume_1=q["ask_volume_1"],
        volume=q["volume"],
        turnover=Decimal("0.0"),
        open_interest=q["open_interest"],
        prev_settle=q.get("prev_settle"),
        day_open=q.get("open_price"),
        day_high=q.get("high_price"),
        day_low=q.get("low_price"),
        avg_price=q.get("avg_price"),
    )


def get_watchlist() -> List[str]:
    """读取订阅列表: 按持久化顺序返回, 为空时回落到配置文件并回种"""
    redis_client = RedisClient.get_client()
    try:
        members = sorted(redis_client.smembers(WATCHLIST_KEY))
    except Exception:
        members = []
    if not members:
        seeded = [inst.symbol for inst in cfg.instruments]
        try:
            if seeded:
                redis_client.sadd(WATCHLIST_KEY, *seeded)
        except Exception:
            pass
        return seeded
    try:
        order = json.loads(redis_client.get("watchlist:order") or "[]")
        if isinstance(order, list):
            # 持久化顺序优先, 新加入的合约追加在末尾
            ordered_known = [s for s in order if s in set(members)]
            extra = [s for s in members if s not in ordered_known]
            members = ordered_known + extra
    except Exception:
        pass
    return members


def get_market_subscriptions() -> List[str]:
    """自选决定展示；持仓、挂单和有效条件单独立保留后台行情。"""
    from backend.trade_engine.paper_accounts import paper_accounts
    members = get_watchlist()
    required = paper_accounts.required_instruments()
    return members + sorted(required.difference(members))


class SinaRealtimeCollector:
    """免费实时行情采集主控: 按订阅列表分批轮询新浪快照并驱动行情管道"""

    def __init__(self, on_tick_callback: Callable[[TickData], None], interval: Optional[float] = None):
        self.on_tick_callback = on_tick_callback
        self.interval = interval or float(cfg.system.poll_interval or 0.8)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_emitted: Dict[str, Tuple] = {}

    def start(self):
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, name="SinaFeedThread", daemon=True)
            self._thread.start()
            notifier.notify("FEED_SINA_STARTED", f"新浪实时行情采集已启动 (轮询 {self.interval}s)", "INFO", "sina_collector")

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        notifier.notify("FEED_SINA_STOPPED", "新浪实时行情采集已停止", "INFO", "sina_collector")

    def _mark_status(self, **fields):
        try:
            RedisClient.get_client().hset("feed:status:sina", mapping=fields)
            RedisClient.get_client().hset("feed:status", mapping=fields)
        except Exception:
            pass

    def _run_loop(self):
        while self._running:
            interval = self.interval
            try:
                symbols = get_market_subscriptions()
                if not symbols:
                    time.sleep(2.0)
                    continue

                quotes = fetch_sina_quotes(symbols)
                now_str = datetime.now().isoformat(timespec="seconds")
                emitted = 0
                for symbol in quotes:
                    if self._should_emit(symbol, quotes[symbol]):
                        try:
                            self.on_tick_callback(quote_to_tick(symbol, quotes[symbol]))
                            emitted += 1
                        except Exception as e:
                            logger.error(f"行情回调处理失败 {symbol}: {e}")

                # 休市时段快照静止, 放慢轮询频率
                if not FuturesTradingCalendar.is_trading_time(datetime.now()) and emitted == 0:
                    interval = max(interval, 3.0)

                status_fields = dict(
                    feed_type="sina",
                    running="1",
                    last_poll_at=now_str,
                    watchlist_count=str(len(symbols)),
                    last_error="",
                )
                # 没有新成交时保留最后一次有效 Tick 时间，避免短暂无变化或休市后丢失新鲜度基准。
                if emitted:
                    status_fields["last_tick_at"] = now_str
                self._mark_status(**status_fields)
            except Exception as e:
                logger.error(f"SinaRealtimeCollector 轮询异常: {e}")
                self._mark_status(feed_type="sina", running="1", last_error=str(e)[:200])
                interval = 3.0
            time.sleep(interval)

    def _should_emit(self, symbol: str, q: Dict[str, Any]) -> bool:
        """依据 (时间,最新价,累计成交量,持仓) 判重; 首次见到必发, 保证页面和引擎有基线数据"""
        # 中金所无源时间戳，休市时不能把旧快照标为当前行情。
        if get_exchange(symbol) == "CFFEX":
            now = datetime.now()
            if not FuturesTradingCalendar.is_trading_time_for(now, symbol):
                return False
        fingerprint = (q["time_str"], str(q["last_price"]), q["volume"], str(q["open_interest"]))
        if self._last_emitted.get(symbol) == fingerprint:
            return False
        self._last_emitted[symbol] = fingerprint
        return True
