import os
import time
import logging
import threading
from typing import Optional, Dict, Any, Callable
from decimal import Decimal
from datetime import datetime
from openctp_ctp import thosttraderapi as tdapi
from backend.common.config import cfg
from backend.common.product_specs import get_exchange, to_ctp_instrument
from backend.trade_engine.models import (
    OrderRequest, OrderData, TradeData, Direction, OffsetFlag, OrderStatus
)
from backend.notifier.notifier import notifier

logger = logging.getLogger(__name__)

class CTPTraderHandler(tdapi.CThostFtdcTraderSpi):
    """
    中信期货/CTP 实盘交易 Spi 回调处理器
    """
    def __init__(self, api: tdapi.CThostFtdcTraderApi, on_connected_cb: Optional[Callable] = None):
        super().__init__()
        self.api = api
        self.on_connected_cb = on_connected_cb
        self.is_connected = False
        self.is_authenticated = False
        self.is_logged_in = False
        self.settlement_confirmed = False
        self.executor = None
        self.front_id: int = 0
        self.session_id: int = 0
        self.order_ref: int = 1000

    def get_next_order_ref(self) -> str:
        self.order_ref += 1
        return str(self.order_ref)

    def OnFrontConnected(self) -> None:
        logger.info("CTP 交易前置连接成功，准备客户端穿透式认证...")
        self.is_connected = True
        if self.executor:
            self.executor.last_connected_at = time.monotonic()
        # 1. 穿透式认证 (Authenticate)
        req = tdapi.CThostFtdcReqAuthenticateField()
        req.BrokerID = cfg.ctp.broker_id
        req.UserID = cfg.ctp.user_id
        req.AppID = cfg.ctp.app_id
        req.AuthCode = cfg.ctp.auth_code
        self.api.ReqAuthenticate(req, 1)

    def OnFrontDisconnected(self, nReason: int) -> None:
        logger.warning(f"CTP 交易前置断开，原因代码: {nReason}")
        self.is_connected = False
        self.is_authenticated = False
        self.is_logged_in = False
        self.settlement_confirmed = False
        if self.executor:
            self.executor.reconciled = False
            self.executor.last_disconnected_at = time.monotonic()
            self.executor.last_disconnect_reason = str(nReason)
        notifier.notify("CTP_TRADER_DISCONNECTED", f"CTP 交易柜台断开连接 (代码 {nReason})", "ERROR", "ctp_trader")

    def OnRspAuthenticate(self, pRspAuthenticateField: tdapi.CThostFtdcRspAuthenticateField, pRspInfo: tdapi.CThostFtdcRspInfoField, nRequestID: int, bIsLast: bool) -> None:
        if pRspInfo and pRspInfo.ErrorID != 0:
            logger.error(f"CTP 穿透式认证失败: {pRspInfo.ErrorMsg} (ErrorID: {pRspInfo.ErrorID})")
            notifier.notify("CTP_AUTH_FAILED", f"CTP 穿透认证失败: {pRspInfo.ErrorMsg}", "ERROR", "ctp_trader")
            return

        logger.info("CTP 穿透式认证通过，发起交易资金账号登录...")
        self.is_authenticated = True
        login_req = tdapi.CThostFtdcReqUserLoginField()
        login_req.BrokerID = cfg.ctp.broker_id
        login_req.UserID = cfg.ctp.user_id
        login_req.Password = cfg.ctp.password
        self.api.ReqUserLogin(login_req, 2)

    def OnRspUserLogin(self, pRspUserLogin: tdapi.CThostFtdcRspUserLoginField, pRspInfo: tdapi.CThostFtdcRspInfoField, nRequestID: int, bIsLast: bool) -> None:
        if pRspInfo and pRspInfo.ErrorID != 0:
            logger.error(f"CTP 交易登录失败: {pRspInfo.ErrorMsg} (ErrorID: {pRspInfo.ErrorID})")
            notifier.notify("CTP_LOGIN_FAILED", f"CTP 交易登录失败: {pRspInfo.ErrorMsg}", "ERROR", "ctp_trader")
            return

        self.front_id = pRspUserLogin.FrontID
        self.session_id = pRspUserLogin.SessionID
        self.order_ref = int(pRspUserLogin.MaxOrderRef or "1000")
        self.is_logged_in = True
        self.settlement_confirmed = False
        self.trading_day = pRspUserLogin.TradingDay
        logger.info(f"CTP 交易登录成功！FrontID: {self.front_id}, SessionID: {self.session_id}, 交易日: {pRspUserLogin.TradingDay}")
        notifier.notify("CTP_TRADER_LOGGED_IN", f"CTP 交易账户 {cfg.ctp.user_id} 登录就绪", "INFO", "ctp_trader")

        # 确认结算单
        confirm_req = tdapi.CThostFtdcSettlementInfoConfirmField()
        confirm_req.BrokerID = cfg.ctp.broker_id
        confirm_req.InvestorID = cfg.ctp.user_id
        self.api.ReqSettlementInfoConfirm(confirm_req, 3)

    def OnRspSettlementInfoConfirm(self, field, info, request_id, last):
        if info and info.ErrorID:
            return
        self.settlement_confirmed = True
        if self.executor:
            self.executor.request_reconcile()

    def OnRspQryTradingAccount(self, row, info, request_id, last):
        self.executor.query_result("account", row, info, request_id, last)

    def OnRspQryInvestorPosition(self, row, info, request_id, last):
        self.executor.query_result("positions", row, info, request_id, last)

    def OnRspQryOrder(self, row, info, request_id, last):
        self.executor.query_result("orders", row, info, request_id, last)

    def OnRspQryTrade(self, row, info, request_id, last):
        self.executor.query_result("trades", row, info, request_id, last)

    def OnErrRtnOrderInsert(self, order, info):
        self.OnRspOrderInsert(order, info, 0, True)

    def OnRspOrderAction(self, action, info, request_id, last):
        # 撤单失败必须显式暴露: 柜台拒绝意味着委托仍在交易所队列中,
        # 静默吞掉会让操作人误以为已撤单而重复下单。
        if info and info.ErrorID != 0:
            logger.error("CTP 撤单被柜台拒绝: %s (ErrorID: %s)", info.ErrorMsg, info.ErrorID)
            notifier.notify("CTP_CANCEL_REJECTED", f"撤单被柜台拒绝: {info.ErrorMsg}",
                            "WARNING", "ctp_trader")
            if self.executor:
                self.executor.cancel_rejected(action, info.ErrorMsg)
        if self.executor:
            self.executor.request_reconcile()

    def OnErrRtnOrderAction(self, action, info):
        self.OnRspOrderAction(action, info, 0, True)


    def OnRspOrderInsert(self, pInputOrder: tdapi.CThostFtdcInputOrderField, pRspInfo: tdapi.CThostFtdcRspInfoField, nRequestID: int, bIsLast: bool) -> None:
        if pRspInfo and pRspInfo.ErrorID != 0:
            if self.executor:
                self.executor.order_rejected(pInputOrder, pRspInfo.ErrorMsg)
            logger.error(f"CTP 报单错误回报: {pRspInfo.ErrorMsg}")
            notifier.notify("CTP_ORDER_ERROR", f"CTP报单被柜台拒绝: {pRspInfo.ErrorMsg}", "WARNING", "ctp_trader")

    def OnRtnOrder(self, pOrder: tdapi.CThostFtdcOrderField) -> None:
        if not pOrder:
            return
        if self.executor:
            self.executor.order_event(pOrder)
        logger.info(f"CTP 报单状态回报: 合约={pOrder.InstrumentID}, 状态={pOrder.OrderStatus}, 状态信息={pOrder.StatusMsg}")

    def OnRtnTrade(self, pTrade: tdapi.CThostFtdcTradeField) -> None:
        if not pTrade:
            return
        if self.executor:
            self.executor.trade_event(pTrade)
        logger.info(f"CTP 实盘成交流水: 成交编号={pTrade.TradeID}, 合约={pTrade.InstrumentID}, 方向={pTrade.Direction}, 价格={pTrade.Price}, 手数={pTrade.Volume}")
        notifier.notify(
            "CTP_TRADE_FILLED",
            f"实盘成交: {pTrade.InstrumentID} {pTrade.Direction} {pTrade.Volume}手 @ {pTrade.Price}",
            "INFO",
            "ctp_trader"
        )


class CTPTradeExecutor:
    """
    中信期货/CTP 实盘交易网关主控类
    对外暴露与 PaperTrader 保持对齐的 submit_order 与 cancel_order 标准接口
    """

    # 对账新鲜度上限: is_ready 要求 last_sync 在此秒数内。
    # 一次完整对账最坏耗时 = 4 类查询 × 单次超时 10s = 40s, 因此该值必须
    # 显著大于最坏耗时, 否则网络一慢就会导致 is_ready 抖动、交易被误中断。
    SYNC_STALE_SECONDS = 90.0
    QUERY_TIMEOUT_SECONDS = 10.0
    # 交易前置看门狗: 断开后的重连退避区间
    WATCHDOG_IDLE_SECONDS = 2.0
    RECONNECT_BACKOFF_MIN = 1.0
    RECONNECT_BACKOFF_MAX = 30.0

    def __init__(self):
        self.api: Optional[tdapi.CThostFtdcTraderApi] = None
        self.spi: Optional[CTPTraderHandler] = None
        self.reconciled = False
        self.last_sync = 0.0
        self.sync_error = "尚未完成结算确认和账户对账"
        self._sync_requested = threading.Event()
        self._stopped = threading.Event()
        self._query_done = threading.Event()
        self._query_id = 10000
        self._query_rows = []
        self._query_error = None
        # 「结果不确定」计数: 每次出现发送/撤单结果不明的报单时递增。
        # 用于区分"请刷新快照"(不阻断报单) 与"状态未知"(必须阻断报单)。
        self._uncertain_seq = 0
        self._ref_lock = threading.Lock()
        self._worker = None
        self._watchdog = None
        # 连接健康指标 (供监控与上线前检查使用)
        self.last_connected_at = 0.0
        self.last_disconnected_at = 0.0
        self.reconnect_count = 0
        self.last_disconnect_reason = ""

    @property
    def is_ready(self) -> bool:
        return bool(self.spi and self.spi.is_connected and self.spi.is_logged_in
                    and self.spi.settlement_confirmed and self.reconciled
                    and time.monotonic() - self.last_sync < self.SYNC_STALE_SECONDS)

    def get_status(self) -> dict:
        spi = self.spi
        return {
            "initialized": bool(self.api),
            "connected": bool(spi and spi.is_connected),
            "authenticated": bool(spi and spi.is_authenticated),
            "logged_in": bool(spi and spi.is_logged_in),
            "settlement_confirmed": bool(spi and spi.settlement_confirmed),
            "reconciled": self.reconciled,
            "sync_error": self.sync_error,
            "account": (cfg.ctp.user_id[:3] + "****" + cfg.ctp.user_id[-2:]) if len(cfg.ctp.user_id) > 5 else "***",
            "reconnect_count": self.reconnect_count,
            "last_disconnect_reason": self.last_disconnect_reason,
            "sync_age_seconds": round(time.monotonic() - self.last_sync, 1) if self.last_sync else None,
            "profile": cfg.system.profile,
        }

    def start(self):
        if not cfg.ctp.user_id or not cfg.ctp.user_id.strip():
            logger.info("未配置 CTP 交易账号，跳过实盘 CTP Trader 初始化。")
            return

        import tempfile
        raw_dir = os.path.abspath(os.path.join(cfg.ctp.flow_path or "./ctp_flow", "td"))
        try:
            raw_dir.encode("ascii")
            flow_dir = raw_dir
        except UnicodeEncodeError:
            flow_dir = os.path.join(tempfile.gettempdir(), "ctp_flow", "td")
        os.makedirs(flow_dir, exist_ok=True)
        if not flow_dir.endswith(os.sep) and not flow_dir.endswith("/"): 
            flow_dir += os.sep

        logger.info(f"初始化 CTP Trader API，流目录: {flow_dir}")
        self.api = tdapi.CThostFtdcTraderApi.CreateFtdcTraderApi(flow_dir)
        self.spi = CTPTraderHandler(self.api)
        self.spi.executor = self
        self._stopped.clear()
        self._worker = threading.Thread(target=self._sync_loop, name="CTPReconciliation", daemon=True)
        self._worker.start()
        self.api.RegisterSpi(self.spi)
        self.api.RegisterFront(cfg.ctp.td_address)
        self.api.SubscribePublicTopic(tdapi.THOST_TERT_QUICK)
        self.api.SubscribePrivateTopic(tdapi.THOST_TERT_QUICK)
        logger.info(f"注册交易前置地址: {cfg.ctp.td_address}")
        self.api.Init()
        self._ensure_watchdog()

    def _ensure_watchdog(self):
        """确保交易前置看门狗线程在运行 (只启动一次)。"""
        if self._watchdog and self._watchdog.is_alive():
            return
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="CTPTraderWatchdog", daemon=True)
        self._watchdog.start()

    def _watchdog_loop(self):
        """交易前置看门狗: 断开且处于交易时段时按退避重连。

        背景: OnFrontDisconnected 只把就绪标志位置 false, **不会重连**。
        没有这一段, 交易柜断开后会一直保持断开直到人工重启服务。
        """
        from backend.common.trading_time import FuturesTradingCalendar
        backoff = self.RECONNECT_BACKOFF_MIN
        while not self._stopped.is_set():
            if self._stopped.wait(self.WATCHDOG_IDLE_SECONDS):
                break
            spi = self.spi
            if spi is not None and spi.is_connected:
                backoff = self.RECONNECT_BACKOFF_MIN
                continue
            if spi is None:
                continue
            if not FuturesTradingCalendar.is_trading_time(datetime.now()):
                # 非交易时段不重连, 避免无意义刷日志与反复认证
                continue
            logger.warning("CTP 交易前置断开, %.0f 秒后尝试重连 (已重连 %s 次)", backoff, self.reconnect_count)
            if self._stopped.wait(backoff):
                break
            backoff = min(backoff * 2, self.RECONNECT_BACKOFF_MAX)
            try:
                self._reconnect()
            except Exception:
                logger.exception("CTP 交易前置重连失败")

    def _reconnect(self):
        """重建 API 并重新 Init。重连后必须重新走完 认证→登录→结算确认→对账。"""
        self.reconnect_count += 1
        notifier.notify(
            "CTP_TRADER_RECONNECTING",
            f"CTP 交易前置重连中 (第 {self.reconnect_count} 次)",
            "WARNING", "ctp_trader",
        )
        # 先释放旧资源, 避免句柄与回调重复注册
        self.reconciled = False
        self.last_sync = 0.0
        self._query_done.set()
        old_api = self.api
        self.api = None
        self.spi = None
        if old_api is not None:
            try:
                old_api.Release()
            except Exception:
                logger.exception("释放旧 CTP Trader API 失败")
        # 重建 (start() 内部会重新 Init 并复用已存在的看门狗线程)
        self.start()
        logger.info("CTP 交易前置已重新初始化, 等待认证/登录/结算确认/对账完成")

    def submit_order(self, req: OrderRequest, order_ref=None) -> Dict[str, Any]:
        if not self.is_ready:
            raise RuntimeError("CTP 未完成结算确认与对账")

        input_order = tdapi.CThostFtdcInputOrderField()
        input_order.BrokerID = cfg.ctp.broker_id
        input_order.InvestorID = cfg.ctp.user_id
        input_order.InstrumentID = to_ctp_instrument(req.instrument)
        input_order.OrderRef = order_ref or self.reserve_order_ref()
        input_order.UserID = cfg.ctp.user_id

        # 方向映射
        input_order.Direction = tdapi.THOST_FTDC_D_Buy if req.direction == Direction.BUY else tdapi.THOST_FTDC_D_Sell
        
        # 开平映射
        if req.offset == OffsetFlag.OPEN:
            input_order.CombOffsetFlag = tdapi.THOST_FTDC_OF_Open
        elif req.offset == OffsetFlag.CLOSE_TODAY:
            input_order.CombOffsetFlag = tdapi.THOST_FTDC_OF_CloseToday
        elif req.offset == OffsetFlag.CLOSE_YEST:
            input_order.CombOffsetFlag = tdapi.THOST_FTDC_OF_CloseYesterday
        else:
            input_order.CombOffsetFlag = tdapi.THOST_FTDC_OF_Close

        input_order.CombHedgeFlag = tdapi.THOST_FTDC_HF_Speculation
        input_order.LimitPrice = float(req.price)
        input_order.VolumeTotalOriginal = int(req.volume)
        input_order.OrderPriceType = tdapi.THOST_FTDC_OPT_LimitPrice
        input_order.TimeCondition = tdapi.THOST_FTDC_TC_GFD
        input_order.VolumeCondition = tdapi.THOST_FTDC_VC_AV
        input_order.ContingentCondition = tdapi.THOST_FTDC_CC_Immediately
        input_order.ForceCloseReason = tdapi.THOST_FTDC_FCC_NotForceClose

        ret = self.api.ReqOrderInsert(input_order, int(input_order.OrderRef))
        return {
            "order_ref": input_order.OrderRef,
            "code": ret,
            "status": "submitted" if ret == 0 else "failed"
        }

    def cancel_order(self, order_ref: str, record=None) -> Dict[str, Any]:
        """撤销未成交的实盘委托 (依据 OrderRef + 本地报单标识)"""
        if not self.spi or not self.spi.is_logged_in:
            raise RuntimeError("CTP 交易前置未登录或账号未就绪")

        cancel_req = tdapi.CThostFtdcInputOrderActionField()
        cancel_req.BrokerID = cfg.ctp.broker_id
        cancel_req.InvestorID = cfg.ctp.user_id
        cancel_req.OrderRef = str(order_ref)
        record = record or {}
        cancel_req.FrontID = record.get("front_id", self.spi.front_id)
        cancel_req.SessionID = record.get("session_id", self.spi.session_id)
        symbol = record.get("instrument", "")
        cancel_req.InstrumentID = to_ctp_instrument(symbol)
        cancel_req.ExchangeID = record.get("exchange", "")
        cancel_req.OrderSysID = record.get("order_sys_id", "")
        cancel_req.ActionFlag = tdapi.THOST_FTDC_AF_Delete
        ret = self.api.ReqOrderAction(cancel_req, int(order_ref))
        return {
            "order_ref": str(order_ref),
            "code": ret,
            "status": "cancel_submitted" if ret == 0 else "cancel_failed"
        }

    def reserve_order_ref(self):
        with self._ref_lock:
            return self.spi.get_next_order_ref()

    @staticmethod
    def row_dict(row):
        fields = ("TradingDay", "OrderRef", "FrontID", "SessionID", "InstrumentID", "ExchangeID", "OrderSysID",
                  "Direction", "CombOffsetFlag", "OffsetFlag", "LimitPrice", "VolumeTotalOriginal", "VolumeTraded",
                  "OrderStatus", "OrderSubmitStatus", "StatusMsg", "InsertDate", "InsertTime", "TradeID", "TradeDate", "TradeTime", "Price", "Volume",
                  "Balance", "Available", "CurrMargin", "FrozenMargin", "PositionProfit", "CloseProfit", "PreBalance",
                  "Deposit", "Withdraw", "PosiDirection", "Position", "TodayPosition", "YdPosition", "LongFrozen", "ShortFrozen", "PositionCost", "UseMargin")
        return {key: getattr(row, key) for key in fields if hasattr(row, key)}

    def request_reconcile(self):
        """请求一次柜台对账: 只刷新快照, **不**改变就绪状态。

        ⚠️ 这里绝不能把 reconciled 置为 False。该标志是 is_ready 的必要条件,
        而周期性对账心跳与每次报单/成交回报都会调用本方法。若在此清零:
        1. is_ready 会随 5 秒心跳反复翻转 (实测约一半时间不可用), 单笔报单被随机拒绝;
        2. 批量报单 (一键全平 / 批量撤单) 只有第一笔能通过, 后续全部以
           "柜台未就绪"被拒 —— 这正是紧急退出最不能失效的场景。
        真正"结果不确定"的场景请改用 mark_uncertain()。
        """
        self._sync_requested.set()

    def mark_uncertain(self):
        """本地出现结果不确定的报单 (发送/撤单调用异常): 立即置为未就绪, 直到对账澄清。

        这是对账门禁的安全侧: 只有在柜台状态确实未知时才阻断报单。
        """
        self._uncertain_seq += 1
        self.reconciled = False
        self._sync_requested.set()

    def query_result(self, kind, row, info, request_id, last):
        if request_id != self._query_id:
            return
        if info and info.ErrorID:
            self._query_error = str(info.ErrorMsg)
        if row:
            self._query_rows.append(self.row_dict(row))
        if last:
            self._query_done.set()

    def _sync_loop(self):
        while not self._stopped.is_set():
            self._sync_requested.wait(5)
            self._sync_requested.clear()
            if self._stopped.is_set():
                break
            if not self.spi or not self.spi.is_logged_in or not self.spi.settlement_confirmed:
                continue
            # 本轮开始前的不确定计数: 只有本轮查询期间没有新产生不确定报单,
            # 对账成功才可以恢复就绪 (避免把期间新出现的未知状态误判为已澄清)。
            uncertain_at_start = self._uncertain_seq
            try:
                snapshot = {}
                for kind, name in (("account", "TradingAccount"), ("positions", "InvestorPosition"), ("orders", "Order"), ("trades", "Trade")):
                    if self._stopped.wait(1.1):
                        return
                    self._query_id += 1
                    self._query_rows, self._query_error = [], None
                    self._query_done.clear()
                    request = getattr(tdapi, "CThostFtdcQry" + name + "Field")()
                    request.BrokerID = cfg.ctp.broker_id
                    request.InvestorID = cfg.ctp.user_id
                    result = getattr(self.api, "ReqQry" + name)(request, self._query_id)
                    if result != 0 or not self._query_done.wait(self.QUERY_TIMEOUT_SECONDS) or self._query_error:
                        raise RuntimeError(self._query_error or f"{kind} 查询失败或超时 ({result})")
                    if self._stopped.is_set() or not self.spi.is_logged_in:
                        raise RuntimeError("对账期间柜台断开")
                    snapshot[kind] = self._query_rows[:]
                from backend.trade_engine.live_trader import live_trader
                live_trader.reconcile(snapshot, self.spi.trading_day)
                self.last_sync = time.monotonic()
                self.sync_error = ""
                # 对账成功即证明不存在未确认报单; 期间若又出现新的不确定报单则保持未就绪。
                self.reconciled = self._uncertain_seq == uncertain_at_start
            except Exception as exc:
                self.sync_error = str(exc)
                self.reconciled = False
                logger.exception("CTP 对账失败，禁止新报单")

    def order_event(self, order):
        self.request_reconcile()
        try:
            from backend.trade_engine.live_trader import live_trader
            live_trader.on_order(self.row_dict(order))
        except Exception:
            self.sync_error = "委托回报持久化失败"
            logger.exception(self.sync_error)

    def trade_event(self, trade):
        self.request_reconcile()
        try:
            from backend.trade_engine.live_trader import live_trader
            live_trader.on_trade(self.row_dict(trade))
        except Exception:
            self.sync_error = "成交回报持久化失败"
            logger.exception(self.sync_error)

    def cancel_rejected(self, action, reason):
        """撤单被柜台拒绝的回报: 仅回退本地记录状态, 不触碰柜台。"""
        try:
            from backend.trade_engine.live_trader import live_trader
            live_trader.on_cancel_rejected(self.row_dict(action), reason)
        except Exception:
            self.sync_error = "撤单失败回报处理异常"
            logger.exception(self.sync_error)

    def order_rejected(self, order, reason):
        self.order_event(type("RejectedOrder", (), {
            "OrderRef": order.OrderRef, "TradingDay": self.spi.trading_day,
            "FrontID": self.spi.front_id, "SessionID": self.spi.session_id,
            "InstrumentID": order.InstrumentID, "OrderStatus": "5", "OrderSubmitStatus": "4", "StatusMsg": reason})())

    def stop(self):
        self.reconciled = False
        self._stopped.set()
        self._sync_requested.set()
        self._query_done.set()
        if self._worker:
            self._worker.join(timeout=3)
        if self._watchdog:
            self._watchdog.join(timeout=3)
        if self.spi:
            self.spi.is_logged_in = False
        if self.api:
            self.api.Release()
            self.api = None

    def shutdown(self):
        """对外部调用的停止入口别名 (main.py 在 shutdown 事件中调用)。"""
        self.stop()

ctp_executor = CTPTradeExecutor()
