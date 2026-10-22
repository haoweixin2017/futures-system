"""全市场合约同步: 从东方财富公开接口拉取六大交易所全部活跃合约。

- 交易所映射: 113=SHFE 114=DCE 115=CZCE 142=INE 220=CFFEX 225=GFEX
- 郑商所东财代码为 3 位年月 (AP610), 规范化为系统统一 4 位形式 (AP2610)
- 结果写入 Redis 哈希 universe:contracts (整体原子替换) 与 MySQL instrument 表 (幂等 upsert)
- 供前端全合约搜索/订阅与历史回填使用
"""

import json
import logging
import re
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.dialects.mysql import insert as mysql_insert

from backend.common.config import cfg
from backend.common.product_specs import get_spec
from backend.models.database import InstrumentModel
from backend.notifier.notifier import notifier
from backend.storage.mysql_client import SessionLocal
from backend.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

CLIST_PATH = (
    "/api/qt/clist/get?pn={page}&pz={page_size}&po=0&np=1"
    "&fltt=2&invt=2&fid=f12&fs={fs}&fields=f2,f5,f12,f13,f14,f17,f18"
)
# 东财推送接口有多组编号镜像, 单节点限流时轮换重试
CLIST_HOSTS = ["push2.eastmoney.com", "90.push2.eastmoney.com", "1.push2.eastmoney.com", "40.push2.eastmoney.com"]
# 期货专用接口 futsseapi (主源, 每交易所单请求即可取回全部活跃合约)
FUTSSE_URL = (
    "https://futsseapi.eastmoney.com/list/{market}?orderBy=dm&sort=asc&pageSize=1000"
    "&pageIndex=0&token=58b2fa8f53413d3913303bec3c3c2bec&field=dm,sc,name,p,o,h,l,cje,ccl"
)
MARKET_MAP = {113: "SHFE", 114: "DCE", 115: "CZCE", 142: "INE", 220: "CFFEX", 225: "GFEX"}
MARKET_FS = "+".join(f"m:{m}" for m in MARKET_MAP)
UNIVERSE_KEY = "universe:contracts"
UNIVERSE_TMP_KEY = "universe:contracts:tmp"
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://qh.eastmoney.com/",
}


def czce_to_canonical(symbol: str) -> str:
    """郑商所 3 位年月转 4 位规范形式: AP610 -> AP2610 (年份位 6->26, 0->30)"""
    letters = []
    for ch in symbol:
        if ch.isalpha():
            letters.append(ch)
        else:
            break
    product = "".join(letters)
    digits = symbol[len(product):]
    if len(digits) == 3:
        year_digit = int(digits[0])
        year2 = 20 + year_digit if year_digit >= 6 else 30 + year_digit
        return f"{product}{year2}{digits[1:]}"
    return symbol


def normalize_symbol(em_symbol: str, exchange: str) -> str:
    symbol = em_symbol.strip().upper()
    if exchange == "CZCE":
        symbol = czce_to_canonical(symbol)
    return symbol


def normalize_czce_name(name: str) -> str:
    """郑商所合约名称的 3 位年月展开为 4 位: 苹果701 -> 苹果2701"""
    m = re.match(r"^(.*?)(\d{3})$", name.strip())
    if not m:
        return name
    product, digits = m.group(1), m.group(2)
    year_digit = int(digits[0])
    year2 = 20 + year_digit if year_digit >= 6 else 30 + year_digit
    return f"{product}{year2}{digits[1:]}"


def _http_get_json(url: str, timeout: float = 12.0, retries: int = 2) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HTTP_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (i + 1))
    raise last_err


def _fetch_clist_page(market_id: int, page: int, page_size: int) -> Dict[str, Any]:
    """按镜像主机轮换拉取单页合约列表"""
    last_err: Optional[Exception] = None
    for host in CLIST_HOSTS:
        try:
            url = f"https://{host}{CLIST_PATH.format(page=page, page_size=page_size, fs=f'm:{market_id}')}"
            return _http_get_json(url)
        except Exception as e:
            last_err = e
    raise last_err


def _fetch_via_futsse() -> List[Dict[str, Any]]:
    """主源: 期货专用接口, 每交易所单请求取回全部合约 (含持仓量 ccl, 供主力合约判定)"""
    results: List[Dict[str, Any]] = []
    for market_id, exchange in MARKET_MAP.items():
        data = _http_get_json(FUTSSE_URL.format(market=market_id))
        for row in (data.get("list") or []):
            name = str(row.get("name", "")).strip()
            # 跳过主连/次主连等合成行, 只保留真实可交易合约
            if "主连" in name or "连续" in name:
                continue
            symbol = normalize_symbol(str(row.get("dm", "")), exchange)
            if not symbol:
                continue
            price = row.get("p")
            ccl = row.get("ccl")
            if exchange == "CZCE":
                name = normalize_czce_name(name)
            results.append({
                "symbol": symbol,
                "exchange": exchange,
                "name": name,
                "last_price": price if price not in ("-", None) else None,
                "volume": 0,
                "open_interest": int(ccl) if isinstance(ccl, (int, float)) else None,
                "open_price": row.get("o") if row.get("o") not in ("-", None) else None,
                "prev_settle": None,
            })
        time.sleep(0.3)
    return results


def _fetch_via_clist() -> List[Dict[str, Any]]:
    """备源: push2 clist 分页拉取 (带镜像轮换)"""
    results: List[Dict[str, Any]] = []
    for market_id, exchange in MARKET_MAP.items():
        page = 1
        while True:
            data = (_fetch_clist_page(market_id, page, 500).get("data") or {})
            diff = data.get("diff") or []
            for row in diff:
                symbol = normalize_symbol(str(row.get("f12", "")), exchange)
                if not symbol:
                    continue
                results.append({
                    "symbol": symbol,
                    "exchange": exchange,
                    "name": str(row.get("f14", "")).strip(),
                    "last_price": row.get("f2") if row.get("f2") != "-" else None,
                    "volume": row.get("f5") if row.get("f5") != "-" else 0,
                    "open_price": row.get("f17") if row.get("f17") != "-" else None,
                    "prev_settle": row.get("f18") if row.get("f18") != "-" else None,
                })
            total = int(data.get("total") or 0)
            if page * 500 >= total or not diff:
                break
            page += 1
            time.sleep(0.3)
        time.sleep(0.3)
    return results


def fetch_universe() -> List[Dict[str, Any]]:
    """拉取全部活跃合约: 优先 futsseapi, 失败时回退 push2 clist"""
    try:
        contracts = _fetch_via_futsse()
        if contracts:
            return contracts
    except Exception as e:
        logger.warning(f"futsseapi 合约列表拉取失败, 回退 push2 clist: {e}")
    return _fetch_via_clist()


def store_universe(contracts: List[Dict[str, Any]]):
    """写入 Redis (原子替换) 与 MySQL instrument 表 (upsert)"""
    redis_client = RedisClient.get_client()
    now_str = datetime.now().isoformat(timespec="seconds")

    pipe = redis_client.pipeline(transaction=False)
    pipe.delete(UNIVERSE_TMP_KEY)
    for c in contracts:
        pipe.hset(UNIVERSE_TMP_KEY, c["symbol"], json.dumps(c, ensure_ascii=False))
    pipe.hset(UNIVERSE_TMP_KEY, "__updated_at__", now_str)
    pipe.rename(UNIVERSE_TMP_KEY, UNIVERSE_KEY)
    pipe.execute()

    session = SessionLocal()
    try:
        for c in contracts:
            mult, tick = get_spec(c["symbol"])
            stmt = mysql_insert(InstrumentModel).values(
                symbol=c["symbol"],
                exchange=c["exchange"],
                name=c["name"] or c["symbol"],
                price_tick=tick,
                volume_multiple=mult,
            )
            stmt = stmt.on_duplicate_key_update(
                exchange=stmt.inserted.exchange,
                name=stmt.inserted.name,
            )
            session.execute(stmt)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"同步合约信息到 MySQL 失败: {e}")
    finally:
        session.close()


def get_universe_symbols() -> List[str]:
    """从 MySQL instrument 表读取全市场合约代码列表"""
    session = SessionLocal()
    try:
        rows = session.query(InstrumentModel.symbol).order_by(InstrumentModel.symbol.asc()).all()
        return [r[0] for r in rows]
    finally:
        session.close()


def get_universe_cache() -> Dict[str, Dict[str, Any]]:
    """读取 Redis 中的合约宇宙缓存 (含 __updated_at__ 元数据键)"""
    try:
        raw = RedisClient.get_client().hgetall(UNIVERSE_KEY)
    except Exception:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for symbol, payload in raw.items():
        if symbol == "__updated_at__":
            result["__updated_at__"] = payload
            continue
        try:
            result[symbol] = json.loads(payload)
        except Exception:
            continue
    return result


class UniverseSync:
    """定期刷新全市场合约列表的后台任务"""

    def __init__(self, refresh_seconds: Optional[int] = None):
        self.refresh_seconds = refresh_seconds or cfg.system.universe_refresh_seconds
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, name="UniverseSyncThread", daemon=True)
            self._thread.start()

    def stop(self):
        self._running = False

    def refresh_once(self) -> int:
        contracts = fetch_universe()
        store_universe(contracts)
        logger.info(f"全市场合约同步完成: {len(contracts)} 个合约")
        return len(contracts)

    def _run_loop(self):
        while self._running:
            try:
                count = self.refresh_once()
                self._mark_status(universe_count=str(count), universe_updated_at=datetime.now().isoformat(timespec="seconds"))
            except Exception as e:
                logger.error(f"全市场合约同步失败: {e}")
                notifier.notify("UNIVERSE_SYNC_FAILED", f"全市场合约同步失败: {e}", "WARNING", "universe_sync")
            time.sleep(self.refresh_seconds)

    def _mark_status(self, **fields):
        try:
            client = RedisClient.get_client()
            client.hset("feed:status:sina", mapping=fields)
            client.hset("feed:status", mapping=fields)
        except Exception:
            pass
