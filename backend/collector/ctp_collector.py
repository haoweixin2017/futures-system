import os
import time
import logging
import threading
from typing import List, Callable, Optional
from openctp_ctp import thostmduserapi as mdapi
from backend.common.config import cfg
from backend.common.product_specs import to_ctp_instrument
from backend.models.market_data import TickData
from backend.notifier.notifier import notifier
from backend.storage.redis_client import RedisClient
from datetime import datetime
from decimal import Decimal

logger = logging.getLogger(__name__)

# 断线重连参数
RECONNECT_MIN_INTERVAL = 1.0    # 首次重试等待(秒)
RECONNECT_MAX_INTERVAL = 30.0   # 退避上限(秒)
STALE_DATA_SECONDS = 30.0       # 超过该时长无行情视为异常并触发重连
STALE_RECONNECT_SECONDS = 15 * 60.0  # 仿真行情可能极稀疏，避免两分钟一次的误重连

class CTPMdHandler(mdapi.CThostFtdcMdSpi):
    """
    CTP 行情回调处理器
    """
    def __init__(self, api: mdapi.CThostFtdcMdApi, symbols: List[str], on_tick_callback: Callable[[TickData], None]):
        super().__init__()
        self.api = api
        self.symbols = symbols
        self.on_tick_callback = on_tick_callback
        self.is_connected = False
        self.is_logged_in = False
        self.last_tick_at: Optional[datetime] = None
        self.last_market_at: Optional[datetime] = None

    def _mark_status(self, **fields):
        try:
            RedisClient.get_client().hset("feed:status:ctp", mapping=fields)
        except Exception:
            pass

    def OnFrontConnected(self) -> None:
        logger.info("CTP 行情前置连接成功，发起登录请求...")
        self.is_connected = True
        self._mark_status(feed_type="ctp", running="1", connected="1", logged_in="0")
        req = mdapi.CThostFtdcReqUserLoginField()
        req.BrokerID = cfg.ctp.broker_id
        req.UserID = cfg.ctp.user_id
        req.Password = cfg.ctp.password
        self.api.ReqUserLogin(req, 1)

    def OnFrontDisconnected(self, nReason: int) -> None:
        logger.warning(f"CTP 行情前置断开连接，原因代码: {nReason}，等待自动重连...")
        self.is_connected = False
        self.is_logged_in = False
        self._mark_status(feed_type="ctp", running="1", connected="0", logged_in="0",
                          last_error=f"disconnect:{nReason}")
        notifier.notify(
            "CTP_MD_DISCONNECTED",
            f"CTP 行情前置断开连接 (代码 {nReason})",
            "ERROR",
            "ctp_collector",
        )

    def OnRspUserLogin(self, pRspUserLogin: mdapi.CThostFtdcRspUserLoginField, pRspInfo: mdapi.CThostFtdcRspInfoField, nRequestID: int, bIsLast: bool) -> None:
        if pRspInfo and pRspInfo.ErrorID != 0:
            logger.error(f"CTP 行情登录失败: {pRspInfo.ErrorMsg} (ErrorID: {pRspInfo.ErrorID})")
            notifier.notify(
                "CTP_MD_LOGIN_FAILED",
                f"CTP 行情登录失败: {pRspInfo.ErrorMsg}",
                "ERROR",
                "ctp_collector",
            )
            self._mark_status(feed_type="ctp", running="1", connected="1", logged_in="0",
                              last_error=f"login:{pRspInfo.ErrorID}:{pRspInfo.ErrorMsg}")
            return

        logger.info(f"CTP 行情登录成功！交易日: {pRspUserLogin.TradingDay if pRspUserLogin else '未知'}")
        self.is_logged_in = True
        self._mark_status(feed_type="ctp", running="1", connected="1", logged_in="1", last_error="")
        notifier.notify("CTP_MD_LOGGED_IN", "CTP 行情已登录就绪", "INFO", "ctp_collector")
        # 订阅合约
        if self.symbols:
            # ⚠️ CTP 合约代码大小写敏感: SHFE/INE/DCE/GFEX 必须小写, CZCE/CFFEX 大写。
            # 用错大小写不会报错, 只会「订阅成功但永远收不到行情」——静默失败。
            ctp_symbols = [to_ctp_instrument(s) for s in self.symbols]
            logger.info(f"正在订阅合约列表: {ctp_symbols}")
            self.api.SubscribeMarketData([s.encode('utf-8') for s in ctp_symbols], len(ctp_symbols))

    def OnRspSubMarketData(self, pSpecificInstrument: mdapi.CThostFtdcSpecificInstrumentField, pRspInfo: mdapi.CThostFtdcRspInfoField, nRequestID: int, bIsLast: bool) -> None:
        if pRspInfo and pRspInfo.ErrorID != 0:
            logger.error(f"订阅合约失败: {pRspInfo.ErrorMsg}")
        elif pSpecificInstrument:
            logger.info(f"合约订阅成功: {pSpecificInstrument.InstrumentID}")

    def OnRtnDepthMarketData(self, pDepthMarketData: mdapi.CThostFtdcDepthMarketDataField) -> None:
        if not pDepthMarketData:
            return
        self.last_tick_at = datetime.now()
        self._mark_status(feed_type="ctp", running="1", connected="1", logged_in="1",
                          received_at=self.last_tick_at.isoformat(timespec="seconds"))

        # 过滤无效价格 (CTP 无买卖报价时常以 DBL_MAX 占位)
        def clean_price(val: float) -> Optional[Decimal]:
            if val is None or val > 1e10 or val < -1e10:
                return None
            return Decimal(str(round(val, 4)))

        # 毫秒与时间戳拼接
        time_str = f"{pDepthMarketData.ActionDay or pDepthMarketData.TradingDay} {pDepthMarketData.UpdateTime}.{pDepthMarketData.UpdateMillisec:03d}"
        try:
            ts = datetime.strptime(time_str, "%Y%m%d %H:%M:%S.%f")
        except Exception:
            ts = datetime.utcnow()

        tick = TickData(
            # 柜台对 SHFE/INE/DCE/GFEX 常回传小写代码；页面与关注列表统一使用大写。
            instrument=str(pDepthMarketData.InstrumentID).upper(),
            exchange=pDepthMarketData.ExchangeID or "",
            trading_day=pDepthMarketData.TradingDay,
            timestamp=ts,
            last_price=clean_price(pDepthMarketData.LastPrice) or Decimal("0.0"),
            bid_price_1=clean_price(pDepthMarketData.BidPrice1),
            bid_volume_1=pDepthMarketData.BidVolume1,
            ask_price_1=clean_price(pDepthMarketData.AskPrice1),
            ask_volume_1=pDepthMarketData.AskVolume1,
            volume=int(pDepthMarketData.Volume),
            turnover=Decimal(str(round(pDepthMarketData.Turnover, 2))),
            open_interest=Decimal(str(round(pDepthMarketData.OpenInterest, 2)))
        )
        if self.last_market_at is None or tick.timestamp > self.last_market_at:
            self.last_market_at = tick.timestamp
            self._mark_status(last_tick_at=tick.timestamp.isoformat(timespec="seconds"))

        # 传递给管道回调
        self.on_tick_callback(tick)


class CTPMarketCollector:
    """
    CTP 行情采集主控制类

    除初始化 CTP 接口外, 还负责监督连接健康度:
    CTP 库自带的重连在部分网络故障下会失效, 且登录失败不会自动重试,
    因此由看门狗线程按退避策略主动重建连接。
    """
    def __init__(self, on_tick_callback: Callable[[TickData], None]):
        self.on_tick_callback = on_tick_callback
        self.api: Optional[mdapi.CThostFtdcMdApi] = None
        self.spi: Optional[CTPMdHandler] = None
        self._flow_path: Optional[str] = None
        self._running = False
        self._watchdog: Optional[threading.Thread] = None

    def _resolve_flow_path(self) -> str:
        import tempfile
        raw_path = os.path.abspath(os.path.join(cfg.ctp.flow_path or "./ctp_flow", "md"))
        try:
            raw_path.encode("ascii")
            flow_path = raw_path
        except UnicodeEncodeError:
            flow_path = os.path.join(tempfile.gettempdir(), "ctp_flow", "md")
        os.makedirs(flow_path, exist_ok=True)
        if not flow_path.endswith(os.sep) and not flow_path.endswith("/"):
            flow_path += os.sep
        return flow_path

    def _connect(self):
        """创建 CTP 行情实例并注册回调 (含 Release 旧实例, 避免句柄泄漏)"""
        from backend.collector.sina_collector import get_watchlist
        symbols = get_watchlist()
        logger.info(f"初始化 CTP 行情接口，流动文件目录: {self._flow_path}")
        self.api = mdapi.CThostFtdcMdApi.CreateFtdcMdApi(self._flow_path)
        self.spi = CTPMdHandler(self.api, symbols, self.on_tick_callback)
        self.api.RegisterSpi(self.spi)
        self.api.RegisterFront(cfg.ctp.md_address)
        logger.info(f"注册行情前置地址: {cfg.ctp.md_address}")
        self.api.Init()

    def start(self):
        self._flow_path = self._resolve_flow_path()
        self._connect()
        self._running = True
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name="CTPMdWatchdog", daemon=True
        )
        self._watchdog.start()

    def subscribe(self, symbol: str):
        """运行中追加订阅，供两套行情并行时共享关注列表。"""
        normalized = symbol.strip().upper()
        if not normalized:
            return False
        if self.spi and normalized not in self.spi.symbols:
            self.spi.symbols.append(normalized)
        if not self.api or not self.spi or not self.spi.is_logged_in:
            return False
        ctp_symbol = to_ctp_instrument(normalized)
        self.api.SubscribeMarketData([ctp_symbol.encode("utf-8")], 1)
        return True

    def _watchdog_loop(self):
        """监督行情连接: 断线或长时间无行情时按退避策略重连。"""
        backoff = RECONNECT_MIN_INTERVAL
        while self._running:
            time.sleep(5.0)
            if not self._running:
                break
            try:
                spi = self.spi
                if spi is None:
                    continue

                # 仅在交易时段判定, 避免休市静默被误判为故障
                if not self._is_trading_time():
                    backoff = RECONNECT_MIN_INTERVAL
                    continue

                healthy = spi.is_connected and spi.is_logged_in
                if healthy and spi.last_tick_at is not None:
                    idle = (datetime.now() - spi.last_tick_at).total_seconds()
                    # 长时间无行情 (超过阈值) 视为连接假死
                    if idle > STALE_RECONNECT_SECONDS:
                        logger.warning(f"CTP 行情连续 {idle:.0f}s 无数据, 判定连接假死, 触发重连")
                        healthy = False
                elif healthy and spi.last_tick_at is None:
                    # 已登录但从未收到行情: 仅在长时间后判定异常, 给足订阅生效时间
                    healthy = True

                if healthy:
                    backoff = RECONNECT_MIN_INTERVAL
                    continue

                logger.warning(f"CTP 行情连接异常 (connected={spi.is_connected}, "
                               f"logged_in={spi.is_logged_in}), {backoff:.0f}s 后重连...")
                time.sleep(backoff)
                if not self._running:
                    break
                self._reconnect()
                backoff = min(backoff * 2, RECONNECT_MAX_INTERVAL)
            except Exception:
                logger.exception("CTP 行情看门狗异常, 将在下轮重试")

    @staticmethod
    def _is_trading_time() -> bool:
        try:
            from backend.common.trading_time import FuturesTradingCalendar
            return FuturesTradingCalendar.is_trading_time(datetime.now())
        except Exception:
            # 无法判定时按交易时段处理, 宁可多检查也不漏故障
            return True

    def _reconnect(self):
        """释放旧实例并重建连接。任何异常都不应终止看门狗循环。"""
        try:
            old = self.api
            self.api = None
            self.spi = None
            if old is not None:
                try:
                    old.Release()
                except Exception:
                    logger.warning("释放旧 CTP 行情实例失败, 继续重建", exc_info=True)
            self._connect()
            notifier.notify("CTP_MD_RECONNECTED", "CTP 行情已发起重连", "INFO", "ctp_collector")
        except Exception:
            logger.exception("CTP 行情重连失败, 将由看门狗继续重试")

    def stop(self):
        self._running = False
        try:
            RedisClient.get_client().hset("feed:status:ctp", mapping={"running": "0", "connected": "0", "logged_in": "0"})
        except Exception:
            pass
        if self._watchdog and self._watchdog.is_alive():
            self._watchdog.join(timeout=3.0)
        if self.api:
            try:
                self.api.Release()
            except Exception:
                logger.warning("释放 CTP 行情实例失败", exc_info=True)
            self.api = None
        self.spi = None
