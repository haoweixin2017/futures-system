"""CTP 交易流水与柜台快照。发送前落库，不确定结果必须对账，不自动重发。"""
import hashlib
import logging
import threading
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional
from backend.common.config import cfg
from backend.common.product_specs import get_spec
from backend.trade_engine.ctp_trader import ctp_executor
from backend.trade_engine.models import OrderRequest, OrderData, AccountData, PositionData, Direction, OffsetFlag
from backend.trade_engine.risk_manager import PreTradeRiskManager
from backend.trade_engine.state_store import TradingStateStore
from backend.trade_engine import live_ledger

logger = logging.getLogger(__name__)

# 紧急全平/批量撤单的限速间隔 (秒): 过快会触发柜台流控与交易所异常交易认定
EMERGENCY_RATE_LIMIT = 0.25


class LiveTrader:
    def __init__(self, executor=None, store=None):
        self.executor = executor or ctp_executor
        self.store = store or TradingStateStore()
        self.key = "live:" + hashlib.sha256((cfg.ctp.broker_id + ":" + cfg.ctp.user_id).encode()).hexdigest()
        self.risk_manager = PreTradeRiskManager()
        self._orders = {}
        self._trades = {}
        self._snapshot = {}
        self._version = None
        self._lock = threading.RLock()

    def _load(self):
        if self._version is None:
            data, self._version = self.store.load(self.key)
            if not data:
                self._orders, self._trades, self._snapshot = {}, {}, {}
            if data:
                self._orders = data["orders"]
                self._trades = data["trades"]
                self._snapshot = data["snapshot"]
                risk = data.get("risk", {})
                self.risk_manager.trading_day = risk.get("day", "")
                self.risk_manager.day_start_equity = Decimal(risk["equity"]) if risk.get("equity") else None
                self.risk_manager.cancel_counts = risk.get("cancels", {})
                self.risk_manager.order_counts = risk.get("orders_count", {})
                # 人工紧急停机状态需跨重启保留, 避免重启后意外恢复报单
                if risk.get("open_blocked"):
                    self.risk_manager.block_open(risk.get("block_reason") or "重启前已禁止开仓")

    def _save(self):
        risk = self.risk_manager
        try:
            self._version = self.store.save(self.key, {
                "orders": self._orders, "trades": self._trades, "snapshot": self._snapshot,
                "risk": {"day": risk.trading_day, "equity": str(risk.day_start_equity) if risk.day_start_equity is not None else None,
                         "cancels": risk.cancel_counts, "orders_count": risk.order_counts,
                         "live_enabled": risk.live_enabled, "open_blocked": risk.open_blocked,
                         "block_reason": risk.block_reason}}, self._version)
        except Exception:
            self.executor.reconciled = False
            self._version = None
            raise

    def _sync_ledger(self, dirty_orders=None):
        """把内存状态结构化落库 (旁路, 失败不影响交易主流程)。

        与 _save() 的 JSON 存储**双写**: JSON 保证交易连续性, 结构化表供 SQL 对账。
        外层再兜一层 try: 落库是审计动作, 任何异常都不得中断报单。
        """
        try:
            orders = list(dirty_orders) if dirty_orders is not None else list(self._orders.values())
            for record in orders:
                live_ledger.record_order(record)
        except Exception:
            logger.exception("结构化委托流水同步失败 (已忽略, 不影响交易)")

    @staticmethod
    def order_key(day, front, session, ref):
        return f"{day}_{front}_{session}_{ref}"

    def status(self):
        with self._lock:
            self._load()
            return {**self.executor.get_status(), "ready": self.executor.is_ready,
                    "order_count": len(self._orders), "server_time": time.strftime("%Y-%m-%d %H:%M:%S")}

    def _risk_state(self):
        raw = self._snapshot.get("account", [])
        if not raw:
            raise RuntimeError("尚无柜台资金快照")
        a = raw[0]
        account = AccountData(balance=Decimal(str(a["Balance"])), available=Decimal(str(a["Available"])),
                              margin=Decimal(str(a["CurrMargin"])), frozen_margin=Decimal(str(a["FrozenMargin"])),
                              floating_pnl=0, realized_pnl=Decimal(str(a["CloseProfit"])))
        positions = {}
        for row in self._snapshot.get("positions", []):
            direction = "BUY" if row["PosiDirection"] == "2" else "SELL"
            symbol = row["InstrumentID"].upper()
            key = f"{symbol}_{direction}"
            pos = positions.setdefault(key, PositionData(instrument=symbol, direction=direction))
            pos.volume += int(row["Position"])
            pos.frozen_volume += int(row.get("ShortFrozen" if direction == "BUY" else "LongFrozen", 0))
            pos.margin += Decimal(str(row.get("UseMargin", 0)))
        active = {}
        for key, row in self._orders.items():
            if row.get("trading_day") != self.risk_manager.trading_day or row["status"] not in ("SUBMITTED", "PARTIAL", "CANCELLING", "SENDING", "UNKNOWN"):
                continue
            active[key] = OrderData(order_id=key, instrument=row["instrument"], direction=row["direction"],
                                    offset=row["offset"], price=row["price"], volume=row["volume"],
                                    traded_volume=row.get("traded_volume", 0))
        return account, positions, active

    def _last_price(self, instrument: str) -> Optional[Decimal]:
        """取合约最新价, 供报单价格偏离保护使用。取不到时返回 None (该项检查自动跳过)。"""
        try:
            from backend.storage.redis_client import RedisClient
            price = RedisClient.get_client().hget(f"quote:ctp:{instrument}", "last_price")
            if price in (None, "", b""):
                return None
            return Decimal(str(price))
        except Exception:
            return None

    def submit_live_order(self, req: OrderRequest, username=""):
        with self._lock:
            self._load()
            if not self.executor.is_ready:
                return {"status": "REJECTED", "reject_reason": "CTP 实盘柜台未连接或尚未完成结算确认与对账"}
            account, positions, active = self._risk_state()
            passed, reason = self.risk_manager.check_order(
                req, account, positions, active, last_price=self._last_price(req.instrument)
            )
            if not passed:
                return {"status": "REJECTED", "reject_reason": reason}
            self.risk_manager.record_order()
            ref = self.executor.reserve_order_ref()
            spi = self.executor.spi
            key = self.order_key(spi.trading_day, spi.front_id, spi.session_id, ref)
            record = {"order_ref": key, "gateway_ref": ref, "owner": username, "trading_day": spi.trading_day,
                      "front_id": spi.front_id, "session_id": spi.session_id,
                      "broker_id": cfg.ctp.broker_id, "investor_id": cfg.ctp.user_id,
                      "instrument": req.instrument, "direction": req.direction.value, "offset": req.offset.value,
                      "price": str(req.price), "volume": req.volume, "traded_volume": 0,
                      "status": "SENDING", "submit_time": time.strftime("%Y-%m-%d %H:%M:%S"), "reject_reason": ""}
            self._orders[key] = record
            self._save()
            self._sync_ledger([record])
            try:
                result = self.executor.submit_order(req, order_ref=ref)
            except Exception:
                # 调用可能已经到达柜台，不能标为确定拒单，更不能自动重发。
                if record["status"] == "SENDING":
                    record["status"] = "UNKNOWN"
                record["reject_reason"] = "发送结果不确定，等待柜台对账，禁止重复报单"
                # 结果未知属于"状态不确定": 必须立即阻断后续报单, 直到对账澄清。
                self.executor.mark_uncertain()
                self._save()
                self._sync_ledger([record])
                return record.copy()
            record["gateway_code"] = result["code"]
            if record["status"] == "SENDING":
                record["status"] = "SUBMITTED" if result["code"] == 0 else "REJECTED"
                if result["code"] != 0:
                    record["reject_reason"] = f"报单调用返回码 {result['code']}"
            self.executor.request_reconcile()
            self._save()
            self._sync_ledger([record])
            return record.copy()

    def cancel_live_order(self, order_ref, username=""):
        with self._lock:
            self._load()
            record = self._orders.get(order_ref)
            if not record or record.get("owner") != username:
                return {"status": "REJECTED", "reject_reason": "委托不存在或不属于当前用户"}
            # 对账时仍允许撤单；仅要求柜台连接、结算确认及完整委托标识。
            spi = self.executor.spi
            if not spi or not spi.is_logged_in or not spi.settlement_confirmed:
                return {"status": "REJECTED", "reject_reason": "CTP 柜台未就绪"}
            if record["status"] not in ("SUBMITTED", "PARTIAL"):
                return {"status": "REJECTED", "reject_reason": "当前委托状态不可撤"}
            if self.risk_manager.cancel_counts.get(record["instrument"], 0) >= self.risk_manager.max_cancel_count:
                return {"status": "REJECTED", "reject_reason": "已达到当日撤单上限"}
            previous = record["status"]
            record["status"] = "CANCELLING"
            self.risk_manager.record_cancel(record["instrument"])
            self._save()
            try:
                result = self.executor.cancel_order(record["gateway_ref"], record=record)
                if result["code"] != 0:
                    record["status"] = previous
                    record["reject_reason"] = f"撤单调用返回码 {result['code']}"
                    self._save()
                    self.executor.request_reconcile()
                    return {"status": "REJECTED", "reject_reason": record["reject_reason"]}
            except Exception:
                record["reject_reason"] = "撤单结果不确定，等待柜台回报"
                # 撤单调用异常同样属于状态未知, 立即阻断后续报单直至对账澄清。
                self.executor.mark_uncertain()
            self.executor.request_reconcile()
            self._save()
            self._sync_ledger([record])
            return record.copy()

    def _apply_order(self, row):
        day = row.get("TradingDay") or row.get("InsertDate") or getattr(self.executor.spi, "trading_day", "")
        key = self.order_key(day, row.get("FrontID", 0), row.get("SessionID", 0), row["OrderRef"])
        record = self._orders.get(key, {"order_ref": key, "gateway_ref": row["OrderRef"], "owner": "",
                                       "submit_time": row.get("InsertDate", "") + " " + row.get("InsertTime", "")})
        status = {"0": "FILLED", "1": "PARTIAL", "2": "CANCELLED", "3": "SUBMITTED", "4": "CANCELLED", "5": "CANCELLED", "a": "SUBMITTED"}.get(row.get("OrderStatus"), "UNKNOWN")
        if row.get("OrderSubmitStatus") == "4":
            status = "REJECTED"
        terminal = record.get("status") in ("FILLED", "CANCELLED", "REJECTED")
        if not terminal or status in ("FILLED", "CANCELLED", "REJECTED"):
            record["status"] = status
        record.update({"trading_day": day, "front_id": row.get("FrontID", 0), "session_id": row.get("SessionID", 0),
                       "instrument": row["InstrumentID"].upper(), "exchange": row.get("ExchangeID", ""),
                       "order_sys_id": row.get("OrderSysID", ""), "reject_reason": row.get("StatusMsg", "")})
        if "Direction" in row:
            record["direction"] = "BUY" if row["Direction"] == "0" else "SELL"
            record["offset"] = {"0": "OPEN", "3": "CLOSE_TODAY", "4": "CLOSE_YEST"}.get(row.get("CombOffsetFlag", "1")[:1], "CLOSE")
            record["price"] = str(row["LimitPrice"])
            record["volume"] = int(row["VolumeTotalOriginal"])
            record["traded_volume"] = max(record.get("traded_volume", 0), int(row["VolumeTraded"]))
        record.setdefault("broker_id", cfg.ctp.broker_id)
        record.setdefault("investor_id", cfg.ctp.user_id)
        self._orders[key] = record
        return key

    def on_order(self, row):
        with self._lock:
            self._load()
            key = self._apply_order(row)
            self._save()
            record = self._orders.get(key)
            if record:
                self._sync_ledger([record])

    def on_cancel_rejected(self, row, reason):
        """柜台拒绝撤单: 把本地记录从 CANCELLING 回退并记录原因。

        柜台回报比本地请求权威: 撤单被拒说明委托仍在交易所队列中,
        必须还原为 SUBMITTED, 否则界面会显示"撤单中"且无法再次撤单。
        """
        with self._lock:
            self._load()
            spi = getattr(self.executor, "spi", None)
            day = row.get("TradingDay") or getattr(spi, "trading_day", "") or self.risk_manager.trading_day
            front = row.get("FrontID", getattr(spi, "front_id", 0))
            session = row.get("SessionID", getattr(spi, "session_id", 0))
            key = self.order_key(day, front, session, row.get("OrderRef"))
            record = self._orders.get(key)
            if not record:
                return
            if record.get("status") == "CANCELLING":
                record["status"] = "SUBMITTED"
            record["reject_reason"] = f"撤单被柜台拒绝: {reason}"
            self._save()
            self._sync_ledger([record])

    def _apply_trade(self, row):
        key = f"{row.get('TradingDay', row.get('TradeDate', ''))}:{row['ExchangeID']}:{row['TradeID']}"
        self._trades[key] = row.copy()
        return key

    def on_trade(self, row):
        with self._lock:
            self._load()
            self._apply_trade(row)
            self._save()
            try:
                live_ledger.record_trade(row)
            except Exception:
                logger.exception("成交流水同步失败 (已忽略, 不影响交易)")

    def reconcile(self, snapshot, trading_day):
        with self._lock:
            self._load()
            if len(snapshot.get("account", [])) != 1:
                raise RuntimeError("柜台资金查询未返回唯一账户")
            for row in snapshot["orders"]:
                self._apply_order(row)
            for row in snapshot["trades"]:
                self._apply_trade(row)
            self._snapshot = snapshot
            a = snapshot["account"][0]
            if trading_day > self.risk_manager.trading_day:
                self.risk_manager.trading_day = trading_day
                self.risk_manager.cancel_counts.clear()
                self.risk_manager.day_start_equity = Decimal(str(a["PreBalance"])) + Decimal(str(a.get("Deposit", 0))) - Decimal(str(a.get("Withdraw", 0)))
            observed = {self.order_key(row.get("TradingDay") or row.get("InsertDate") or trading_day, row.get("FrontID", 0), row.get("SessionID", 0), row["OrderRef"]) for row in snapshot["orders"]}
            for key, record in self._orders.items():
                if record["status"] in ("SENDING", "SUBMITTED", "PARTIAL", "CANCELLING", "UNKNOWN") and key not in observed:
                    record["status"] = "UNKNOWN"
            unresolved = [o for o in self._orders.values() if o["status"] in ("SENDING", "UNKNOWN")]
            self._save()
            # 对账是全量动作, 把柜台全量委托/成交/持仓同步进结构化表 (失败仅记录, 不影响对账结果)
            self._sync_ledger()
            try:
                for row in snapshot["trades"]:
                    live_ledger.record_trade(row)
                live_ledger.record_positions(snapshot.get("positions", []), trading_day, cfg.ctp.user_id)
            except Exception:
                logger.exception("对账流水同步失败 (已忽略)")
            if unresolved:
                raise RuntimeError("存在未确认报单，必须核对柜台记录后再恢复交易")

    def get_orders(self, username=""):
        with self._lock:
            self._load()
            return sorted([o.copy() for o in self._orders.values() if o.get("owner") == username], key=lambda o: o["submit_time"], reverse=True)

    def get_snapshot(self):
        with self._lock:
            self._load()
            return {"ready": self.executor.is_ready, "account": self._snapshot.get("account", []),
                    "positions": self._snapshot.get("positions", []), "trades": list(self._trades.values())}

    # ------------------------------------------------------------------
    # 紧急停机三开关 (从软到硬)
    #
    # 注意: 本系统**没有**现成的一键全平接口 (/trade/close-all 是模拟盘的),
    # 实盘只具备单笔 order / cancel, 因此下面三个方法必须自建。
    # ------------------------------------------------------------------

    def block_open(self, reason: str = "") -> Dict[str, Any]:
        """开关 1 (软): 禁止新开仓, 保留平仓能力。立即生效, 无需重启。"""
        with self._lock:
            self._load()
            self.risk_manager.block_open(reason)
            self._save()
            return {"status": "success", "open_blocked": True, "reason": self.risk_manager.block_reason}

    def unblock_open(self) -> Dict[str, Any]:
        """解除软开关。"""
        with self._lock:
            self._load()
            self.risk_manager.unblock_open()
            self._save()
            return {"status": "success", "open_blocked": False}

    def cancel_all(self, username: str = "") -> Dict[str, Any]:
        """开关 2 (中): 撤销全部可撤委托。逐笔撤 + 限速, 避免触发柜台流控。"""
        with self._lock:
            self._load()
            targets = [o for o in self._orders.values()
                       if o.get("status") in ("SUBMITTED", "PARTIAL")
                       and (not username or o.get("owner") == username)]
            targets.sort(key=lambda o: o.get("submit_time", ""))

        details = []
        for record in targets:
            result = self.cancel_live_order(record["order_ref"], record.get("owner") or username)
            details.append({
                "order_ref": record["order_ref"],
                "instrument": record.get("instrument"),
                "status": result.get("status"),
                "reason": result.get("reject_reason", ""),
            })
            time.sleep(EMERGENCY_RATE_LIMIT)

        succeeded = sum(1 for d in details if d["status"] not in ("REJECTED",))
        return {"status": "success", "attempted": len(targets), "submitted": succeeded, "details": details}

    def close_all(self, username: str = "", confirm: str = "",
                  batch_size: Optional[int] = None) -> Dict[str, Any]:
        """开关 3 (硬): 一键全平。最危险的操作, 需要显式确认字符串。

        执行顺序 (不可颠倒):
          1. 先撤掉全部挂单 —— 否则挂单会继续成交, 导致"平完又有仓"
          2. 等待挂单撤销生效 (对账刷新快照)
          3. 按合约逐笔生成对手价平仓单, 按 batch_size 拆单
          4. 平今/平昨判定: SHFE/INE 必须区分, 依据持仓快照的今仓数量
        """
        if confirm != "CONFIRM_CLOSE_ALL":
            return {"status": "REJECTED",
                    "reason": "缺少确认字符串 confirm=CONFIRM_CLOSE_ALL (防止误触发全平)"}

        # 步骤 1: 撤掉全部挂单
        cancelled = self.cancel_all(username)
        if cancelled["attempted"]:
            # 步骤 2: 给柜台回报与对账留出时间, 让冻结量释放
            time.sleep(2.0)
            self.executor.request_reconcile()
            time.sleep(1.0)

        with self._lock:
            self._load()
            account, positions, _active = self._risk_state()
            snapshot_positions = list(self._snapshot.get("positions", []))

        # 今仓数量表: (合约, 方向) -> 今仓手数。用于 SHFE/INE 的平今/平昨判定。
        today_map: Dict[str, int] = {}
        for row in snapshot_positions:
            symbol = (row.get("InstrumentID") or "").upper()
            direction = "BUY" if row.get("PosiDirection") == "2" else "SELL"
            key = f"{symbol}_{direction}"
            today_map[key] = today_map.get(key, 0) + int(row.get("TodayPosition") or 0)

        limit = batch_size or max(1, self.risk_manager.max_order_volume)
        job_list: List[Dict[str, Any]] = []
        for key, pos in positions.items():
            available = pos.volume - pos.frozen_volume
            if available <= 0:
                continue
            job_list.append({
                "key": key, "instrument": pos.instrument, "direction": pos.direction,
                "available": available, "today": today_map.get(key, 0),
            })

        if not job_list:
            return {"status": "success", "message": "无可平持仓", "cancelled": cancelled["attempted"],
                    "closed": [], "errors": []}

        closed, errors = [], []
        remaining_today = {job["key"]: job["today"] for job in job_list}
        for job in job_list:
            instrument = job["instrument"]
            close_direction = Direction.SELL if job["direction"] == "BUY" else Direction.BUY
            remaining = job["available"]
            give_up = False
            while remaining > 0 and not give_up:
                vol = min(remaining, limit)
                # SHFE/INE 单条平仓指令的手数不得超过该方向仓位的可用量:
                # 今仓不足时若整笔报 CLOSE_TODAY, 柜台会直接拒单 (今仓不足),
                # 因此必须拆成「平今(今仓剩余) + 平昨(其余)」两条。
                for offset, leg_vol in self._plan_close(instrument, job["key"], vol, remaining_today):
                    price = self._opponent_price(instrument, close_direction)
                    if price is None:
                        errors.append({"instrument": instrument, "volume": leg_vol,
                                       "reason": "无法获取对手价, 已跳过 (禁止盲目报价)"})
                        give_up = True
                        break
                    req = OrderRequest(instrument=instrument, direction=close_direction,
                                       offset=offset, price=price, volume=leg_vol)
                    result = self.submit_live_order(req, username)
                    if result.get("status") in ("SUBMITTED", "SENDING"):
                        closed.append({"instrument": instrument, "direction": close_direction.value,
                                       "offset": offset.value, "volume": leg_vol, "price": str(price),
                                       "order_ref": result.get("order_ref")})
                        if offset == OffsetFlag.CLOSE_TODAY:
                            remaining_today[job["key"]] = max(
                                0, remaining_today.get(job["key"], 0) - leg_vol)
                    else:
                        errors.append({"instrument": instrument, "volume": leg_vol,
                                       "offset": offset.value,
                                       "reason": result.get("reject_reason", "报单被拒")})
                    time.sleep(EMERGENCY_RATE_LIMIT)
                remaining -= vol

        return {"status": "success" if not errors else "partial",
                "cancelled": cancelled["attempted"], "closed": closed, "errors": errors,
                "note": "全平仅表示委托已提交, 必须核对柜台持仓已归零后才能确认平仓完成"}

    def _plan_close(self, instrument: str, pos_key: str, volume: int,
                    remaining_today: Dict[str, int]) -> List[tuple]:
        """把一次平仓拆成 [(offset, volume), ...]。

        关键约束: SHFE/INE 的平今/平昨是两条不同的指令, 且**单条指令的手数
        不能超过对应仓位的可用量**, 否则柜台直接拒单 (今仓不足)。
        因此今仓不足时必须显式拆单, 而不是整笔报一个 CLOSE_TODAY。
        """
        from backend.common.product_specs import get_exchange
        if get_exchange(instrument) not in ("SHFE", "INE"):
            return [(OffsetFlag.CLOSE, volume)]
        today_left = max(0, int(remaining_today.get(pos_key, 0) or 0))
        if today_left <= 0:
            return [(OffsetFlag.CLOSE_YEST, volume)]
        if today_left >= volume:
            return [(OffsetFlag.CLOSE_TODAY, volume)]
        return [(OffsetFlag.CLOSE_TODAY, today_left),
                (OffsetFlag.CLOSE_YEST, volume - today_left)]

    def _decide_close_offset(self, instrument: str, pos_key: str, volume: int,
                             remaining_today: Dict[str, int]) -> OffsetFlag:
        """单笔平仓标志选择 (调用方须保证 volume 不超过对应仓位可用量)。

        SHFE/INE 平今与平昨的指令不同且手续费差异大, 必须显式区分;
        其余交易所统一用 CLOSE (由柜台自行处理)。

        ⚠️ 当今仓可用量 < volume 时, 单靠一个标志无法表达, 请改用 _plan_close() 拆单。
        """
        from backend.common.product_specs import get_exchange
        if get_exchange(instrument) not in ("SHFE", "INE"):
            return OffsetFlag.CLOSE
        return OffsetFlag.CLOSE_TODAY if remaining_today.get(pos_key, 0) > 0 else OffsetFlag.CLOSE_YEST

    def _opponent_price(self, instrument: str, direction: Direction) -> Optional[Decimal]:
        """取对手价作为平仓报价 (买单用卖一, 卖单用买一)。

        取不到盘口时返回 None —— 宁可不平, 也不盲目报一个可能立即成交在错误价位的限价单。
        """
        try:
            from backend.storage.redis_client import RedisClient
            q = RedisClient.get_client().hgetall(f"quote:ctp:{instrument}") or {}
            key = "ask_price_1" if direction == Direction.BUY else "bid_price_1"
            raw = q.get(key)
            if not raw:
                raw = q.get("last_price")
            if not raw:
                return None
            price = Decimal(str(raw))
            return price if price > 0 else None
        except Exception:
            logger.exception("获取 %s 对手价失败", instrument)
            return None


live_trader = LiveTrader()
