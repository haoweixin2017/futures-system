"""按登录用户隔离的模拟账户，整笔操作成功落库后才返回。"""
import hashlib
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from backend.trade_engine.paper_trader import PaperTrader
from backend.trade_engine.stop_engine import StopOrderEngine
from backend.trade_engine.signal_bridge import SignalTradeBridge, AutoTradingConfig
from backend.trade_engine.models import AccountData, PositionData, OrderData, TradeData
from backend.trade_engine.conditional_models import StopOrderData
from backend.trade_engine.state_store import TradingStateStore


class PaperAccount:
    def __init__(self, username, store):
        self.key = "paper:" + username
        self.store = store
        self._lock = threading.RLock()
        self._depth = 0
        self.trader = PaperTrader()
        self.trader.account.account_id = "P_" + hashlib.sha256(username.encode()).hexdigest()[:30]
        self.stops = StopOrderEngine(self.trader)
        self.config = AutoTradingConfig()
        self.bridge = SignalTradeBridge(self.trader, self.stops, self.config)
        for component in (self.trader, self.stops, self.bridge):
            component._lock = self._lock
            component._transaction = self.transaction
        state, self.version = store.load(self.key)
        if state is not None:
            self.restore(state)

    def export(self, market_only=False):
        trader = self.trader
        risk = trader.risk_manager
        data = {
            "account": trader.account.model_dump(mode="json"),
            "positions": {k: v.model_dump(mode="json") for k, v in trader.positions.items()},
            "prices": {k: str(v) for k, v in trader.last_prices.items()},
            "times": {k: v.isoformat() for k, v in trader.last_tick_times.items()},
            "risk": {"day": risk.trading_day, "equity": str(risk.day_start_equity) if risk.day_start_equity is not None else None,
                     "cancels": risk.cancel_counts.copy()},
        }
        if market_only:
            return data
        data.update({
            "orders": {k: v.model_dump(mode="json") for k, v in trader.orders.items()},
            "trades": [v.model_dump(mode="json") for v in trader.trades],
            "frozen": {k: str(v) for k, v in trader.order_frozen_margins.items()},
            "stops": {k: v.model_dump(mode="json") for k, v in self.stops.stop_orders.items()},
            "config": vars(self.config).copy(),
            "cooldowns": self.bridge.last_trade_time.copy(),
        })
        return data

    def restore(self, data):
        trader = self.trader
        trader.account = AccountData.model_validate(data["account"])
        trader.positions = {k: PositionData.model_validate(v) for k, v in data["positions"].items()}
        if "orders" in data:
            trader.orders = {k: OrderData.model_validate(v) for k, v in data["orders"].items()}
            trader.active_orders = {k: v for k, v in trader.orders.items() if v.status in ("SUBMITTED", "PARTIAL")}
            trader.trades = [TradeData.model_validate(v) for v in data["trades"]]
            trader.order_frozen_margins = {k: Decimal(v) for k, v in data["frozen"].items()}
        trader.last_prices = {k: Decimal(v) for k, v in data["prices"].items()}
        trader.last_tick_times = {k: datetime.fromisoformat(v) for k, v in data["times"].items()}
        risk = data["risk"]
        trader.risk_manager.trading_day = risk["day"]
        trader.risk_manager.day_start_equity = Decimal(risk["equity"]) if risk["equity"] is not None else None
        trader.risk_manager.cancel_counts = risk["cancels"].copy()
        if "stops" in data:
            self.stops.stop_orders = {k: StopOrderData.model_validate(v) for k, v in data["stops"].items()}
            vars(self.config).update(data["config"])
            self.bridge.last_trade_time = data["cooldowns"].copy()

    @contextmanager
    def transaction(self, market_only=False):
        with self._lock:
            outer = self._depth == 0
            if outer:
                before = self.export(market_only)
            self._depth += 1
            try:
                yield
                if outer:
                    after = self.export(market_only)
                    if after != before:
                        save = self.store.save_market if market_only else self.store.save
                        self.version = save(self.key, after, self.version)
            except Exception:
                if outer:
                    self.restore(before)
                raise
            finally:
                self._depth -= 1

    def on_tick(self, tick):
        with self._lock:
            previous = self.trader.last_tick_times.get(tick.instrument)
            if previous and tick.timestamp <= previous:
                return
            if tick.trading_day < self.trader.risk_manager.trading_day:
                return
            market_only = (self.version > 0 and hasattr(self.store, "save_market")
                and not any(o.instrument == tick.instrument for o in self.trader.active_orders.values())
                and not any(o.instrument == tick.instrument and o.status == "ACTIVE"
                            for o in self.stops.stop_orders.values()))
            with self.transaction(market_only=market_only):
                self.trader.on_tick(tick)
                self.stops.on_tick(tick)


class PaperAccounts:
    def __init__(self, store=None):
        self.store = store or TradingStateStore()
        self.accounts = {}
        self._lock = threading.RLock()

    def get(self, username):
        with self._lock:
            if username not in self.accounts:
                self.accounts[username] = PaperAccount(username, self.store)
            return self.accounts[username]

    def restore_all(self):
        for key in self.store.keys("paper:"):
            self.get(key[len("paper:"):])

    def required_instruments(self):
        with self._lock:
            accounts = list(self.accounts.values())
        required = set()
        for account in accounts:
            with account._lock:
                required.update(p.instrument for p in account.trader.positions.values() if p.volume > 0)
                required.update(o.instrument for o in account.trader.active_orders.values())
                required.update(o.instrument for o in account.stops.stop_orders.values() if o.status == "ACTIVE")
        return required

    def on_tick(self, tick):
        with self._lock:
            accounts = list(self.accounts.values())
        for account in accounts:
            trader = account.trader
            with account._lock:
                needed = (any(p.instrument == tick.instrument and p.volume for p in trader.positions.values())
                    or any(o.instrument == tick.instrument for o in trader.active_orders.values())
                    or any(o.instrument == tick.instrument and o.status == "ACTIVE" for o in account.stops.stop_orders.values()))
            if needed:
                try:
                    account.on_tick(tick)
                except Exception:
                    logging.getLogger(__name__).exception("模拟账户处理失败: %s", account.key)

    def on_signal_triggered(self, signal):
        with self._lock:
            accounts = list(self.accounts.values())
        for account in accounts:
            try:
                account.bridge.on_signal_triggered(signal)
            except Exception:
                logging.getLogger(__name__).exception("模拟账户跟单失败: %s", account.key)

    def snapshot_close(self, trading_day):
        from backend.models.database import AccountSnapshotModel
        from backend.storage.mysql_client import SessionLocal
        close_time = datetime.strptime(trading_day, "%Y%m%d").replace(hour=15)
        with self._lock:
            accounts = list(self.accounts.values())
        for account in accounts:
            with account._lock, SessionLocal() as session:
                data = account.trader.get_account()
                exists = session.query(AccountSnapshotModel.id).filter_by(
                    account_id=data.account_id, trading_day=trading_day, snapshot_time=close_time).first()
                if not exists:
                    session.add(AccountSnapshotModel(account_id=data.account_id, trading_day=trading_day,
                        snapshot_time=close_time, balance=data.balance, dynamic_equity=data.dynamic_equity,
                        available=data.available, margin=data.margin, floating_pnl=data.floating_pnl,
                        realized_pnl=data.realized_pnl))
                    session.commit()


paper_accounts = PaperAccounts()
