import logging
import uuid
import threading
from itertools import islice
from backend.trade_engine.state_store import atomic
from backend.common.product_specs import get_spec
from decimal import Decimal
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from backend.trade_engine.models import (
    Direction, OffsetFlag, OrderStatus, OrderRequest, OrderData, TradeData, PositionData, AccountData
)
from backend.trade_engine.risk_manager import PreTradeRiskManager
from backend.models.market_data import TickData
from backend.notifier.notifier import notifier
from backend.common.config import cfg

logger = logging.getLogger(__name__)

class PaperTrader:
    """
    国内期货高仿真虚拟撮合与模拟账户引擎 (Paper Trading Engine)
    1. 资金账户管理 (可用资金、保证金占用、挂单冻结、浮动盈亏、平仓盈亏)
    2. 双向持仓管理 (多头、空头独立开平仓核算)
    3. Tick 驱动撮合 (依据最新行情价格判断限价单成交)
    4. 联动风控硬校验 (事前拦截)
    """
    def __init__(self, initial_capital: Decimal = Decimal("1000000.0")):
        self._lock = threading.RLock()
        self._transaction = None
        self.last_prices = {}
        self.last_tick_times = {}
        self.account = AccountData(
            account_id="PAPER_001",
            balance=initial_capital,
            available=initial_capital,
            margin=Decimal("0.0"),
            frozen_margin=Decimal("0.0"),
            floating_pnl=Decimal("0.0"),
            realized_pnl=Decimal("0.0")
        )
        self.positions: Dict[str, PositionData] = {}  # "JM2701_BUY" -> PositionData
        self.orders: Dict[str, OrderData] = {}        # order_id -> OrderData
        self.active_orders: Dict[str, OrderData] = {} # order_id -> OrderData
        self.trades: List[TradeData] = []
        # ⚠️ 模拟盘与实盘共用 PreTradeRiskManager, 但**必须**关掉实盘专属开关:
        #   live_enabled / allowed_instruments / fat_finger_pct / daily_order_limit
        #   都是为实盘灰度放量引入的, 不应限制模拟盘 (否则模拟盘会被"实盘总开关未开启"拦死)。
        # 共用的部分: 单笔手数 / 持仓上限 / 保证金率 / 撤单上限 / 日亏熔断。
        self.risk_manager = PreTradeRiskManager(
            live_enabled=True,
            allowed_instruments=[],
            fat_finger_pct=Decimal("0"),
            daily_order_limit=0,
        )
        self.order_frozen_margins: Dict[str, Decimal] = {} # order_id -> frozen_margin

    def get_instrument_multiplier(self, instrument: str) -> int:
        for inst in cfg.instruments:
            if inst.symbol == instrument:
                return inst.volume_multiple
        return get_spec(instrument)[0]

    def get_pos_key(self, instrument: str, direction: Direction) -> str:
        return f"{instrument}_{direction.value}"

    @atomic
    def submit_order(self, req: OrderRequest) -> OrderData:
        order_id = f"ORD_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        
        # 1. 事前硬风控检查
        passed, reason = self.risk_manager.check_order(
            req=req,
            account=self.account,
            positions=self.positions,
            active_orders=self.active_orders
        )

        if not passed:
            order = OrderData(
                order_id=order_id,
                instrument=req.instrument,
                direction=req.direction,
                offset=req.offset,
                price=req.price,
                volume=req.volume,
                status=OrderStatus.REJECTED,
                reject_reason=reason
            )
            self.orders[order_id] = order
            logger.warning(f"Order {order_id} rejected by risk manager: {reason}")
            notifier.notify("ORDER_REJECTED", f"委托被风控拦截: {reason} ({req.instrument} {req.direction.value} {req.volume}手)", "WARNING", "paper_trader")
            return order

        # 2. 风控通过，冻结资金或持仓
        mult = self.get_instrument_multiplier(req.instrument)
        if req.offset == OffsetFlag.OPEN:
            req_margin = req.price * Decimal(str(req.volume * mult)) * self.risk_manager.margin_ratio
            self.account.available -= req_margin
            self.account.frozen_margin += req_margin
            self.order_frozen_margins[order_id] = req_margin
        else:
            pos_dir = Direction.SELL if req.direction == Direction.BUY else Direction.BUY
            pos_key = self.get_pos_key(req.instrument, pos_dir)
            pos = self.positions.get(pos_key)
            if pos:
                pos.frozen_volume += req.volume

        order = OrderData(
            order_id=order_id,
            instrument=req.instrument,
            direction=req.direction,
            offset=req.offset,
            price=req.price,
            volume=req.volume,
            status=OrderStatus.SUBMITTED
        )
        self.orders[order_id] = order
        self.active_orders[order_id] = order
        logger.info(f"Order submitted: {order_id} {req.instrument} {req.direction.value} {req.offset.value} {req.volume}@{req.price}")
        return order

    @atomic
    def cancel_order(self, order_id: str) -> bool:
        order = self.active_orders.get(order_id)
        if not order:
            logger.warning(f"Cannot cancel order {order_id}: not active")
            return False

        if self.risk_manager.cancel_counts.get(order.instrument, 0) >= self.risk_manager.max_cancel_count:
            return False

        # 解冻资金或持仓
        if order.offset == OffsetFlag.OPEN:
            frozen = self.order_frozen_margins.pop(order_id, Decimal("0.0"))
            self.account.frozen_margin -= frozen
            self.account.available += frozen
        else:
            pos_dir = Direction.SELL if order.direction == Direction.BUY else Direction.BUY
            pos_key = self.get_pos_key(order.instrument, pos_dir)
            pos = self.positions.get(pos_key)
            if pos:
                pos.frozen_volume = max(0, pos.frozen_volume - (order.volume - order.traded_volume))

        order.status = OrderStatus.CANCELLED
        del self.active_orders[order_id]
        self.risk_manager.record_cancel(order.instrument)
        logger.info(f"Order cancelled: {order_id}")
        return True

    @atomic
    def on_tick(self, tick: TickData):
        if not tick.last_price.is_finite() or tick.last_price <= 0:
            return
        previous = self.last_tick_times.get(tick.instrument)
        if previous and tick.timestamp <= previous:
            return
        if self.risk_manager.trading_day and tick.trading_day < self.risk_manager.trading_day:
            return
        self.risk_manager.roll_trading_day(tick.trading_day, self.account)
        self.last_tick_times[tick.instrument] = tick.timestamp
        self.last_prices[tick.instrument] = tick.last_price
        mult = self.get_instrument_multiplier(tick.instrument)

        # 1. 更新该合约持仓的浮动盈亏
        for dir_val in [Direction.BUY, Direction.SELL]:
            pos_key = self.get_pos_key(tick.instrument, dir_val)
            pos = self.positions.get(pos_key)
            if pos and pos.volume > 0:
                if dir_val == Direction.BUY:
                    pos.floating_pnl = (tick.last_price - pos.open_price_avg) * Decimal(str(pos.volume * mult))
                else:
                    pos.floating_pnl = (pos.open_price_avg - tick.last_price) * Decimal(str(pos.volume * mult))

        # 汇总全账户浮动盈亏
        self.account.floating_pnl = sum(p.floating_pnl for p in self.positions.values() if p.volume > 0)

        # 2. 撮合活跃挂单
        to_fill = []
        for order_id, order in list(self.active_orders.items()):
            if order.instrument != tick.instrument:
                continue

            can_fill = False
            fill_price = order.price

            if order.direction == Direction.BUY:
                # 买单：当最新价 <= 委托买价，成交
                if tick.last_price <= order.price:
                    can_fill = True
            elif order.direction == Direction.SELL:
                # 卖单：当最新价 >= 委托卖价，成交
                if tick.last_price >= order.price:
                    can_fill = True

            if can_fill:
                to_fill.append((order, self._resolve_fill_price(order, tick)))

        for order, fill_price in to_fill:
            self._execute_fill(order, fill_price)
        self._mark_account()

    def _resolve_fill_price(self, order: OrderData, tick: TickData) -> Decimal:
        """确定成交价。

        真实撮合下, 买单吃的是对手价 (卖一), 卖单砸的是买一, 且受委托价约束:
        - 买单成交价 = min(委托价, 卖一价/最新价取高者), 不可能优于你能拿到的报价
        - 卖单成交价 = max(委托价, 买一价/最新价取低者)

        旧实现一律取 tick.last_price, 会让限价单占到"最新价穿越"的便宜
        (如挂 1000 买、行情跌到 900 就以 900 成交), 导致盈亏被系统性高估。
        """
        use_opponent = getattr(cfg.system, "paper_fill_uses_opponent", True)
        if not use_opponent:
            return tick.last_price

        if order.direction == Direction.BUY:
            # 对手价: 有卖一用卖一, 否则退化为最新价; 绝不低于最新价 (避免虚假改善)
            opp = tick.ask_price_1 if tick.ask_price_1 is not None else tick.last_price
            candidate = max(opp, tick.last_price)
            # 成交价不会优于你的委托价
            price = min(order.price, candidate)
        else:
            opp = tick.bid_price_1 if tick.bid_price_1 is not None else tick.last_price
            candidate = min(opp, tick.last_price)
            price = max(order.price, candidate)

        # 额外滑点: 买单加价、卖单减价 (不利方向)
        slippage_ticks = Decimal(str(getattr(cfg.system, "paper_slippage_ticks", 0) or 0))
        if slippage_ticks > 0:
            tick_size = self._price_tick(order.instrument)
            adj = slippage_ticks * tick_size
            price = price + adj if order.direction == Direction.BUY else price - adj

        if price <= 0:
            price = tick.last_price
        return price

    @staticmethod
    def _price_tick(instrument: str) -> Decimal:
        for inst in cfg.instruments:
            if inst.symbol == instrument:
                return Decimal(str(inst.price_tick or 1))
        try:
            from backend.common.product_specs import get_spec
            return Decimal(str(get_spec(instrument)[1] or 1))
        except Exception:
            return Decimal("1")

    def _mark_account(self):
        for pos in self.positions.values():
            price = self.last_prices.get(pos.instrument, pos.open_price_avg)
            sign = 1 if pos.direction == Direction.BUY else -1
            pos.floating_pnl = (price - pos.open_price_avg) * pos.volume * self.get_instrument_multiplier(pos.instrument) * sign
        self.account.floating_pnl = sum((p.floating_pnl for p in self.positions.values()), Decimal("0"))
        self.account.available = self.account.dynamic_equity - self.account.margin - self.account.frozen_margin


    def _execute_fill(self, order: OrderData, fill_price: Decimal):
        fill_vol = order.volume - order.traded_volume
        mult = self.get_instrument_multiplier(order.instrument)
        trade_id = f"TRD_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

        trade = TradeData(
            trade_id=trade_id,
            order_id=order.order_id,
            instrument=order.instrument,
            direction=order.direction,
            offset=order.offset,
            price=fill_price,
            volume=fill_vol
        )
        self.trades.append(trade)

        # 手续费 (按手数计; 默认 0 表示不收取, 可通过 system.paper_commission_per_lot 配置)
        commission = self._calc_commission(fill_vol)
        if commission > 0:
            self.account.balance -= commission
            self.account.available -= commission
            self.account.realized_pnl -= commission

        order.traded_volume = order.volume
        order.status = OrderStatus.FILLED
        if order.order_id in self.active_orders:
            del self.active_orders[order.order_id]

        pos_key = self.get_pos_key(order.instrument, order.direction)

        # ----------------- 开仓成交处理 -----------------
        if order.offset == OffsetFlag.OPEN:
            # 释放挂单冻结资金
            frozen = self.order_frozen_margins.pop(order.order_id, Decimal("0.0"))
            self.account.frozen_margin -= frozen

            # 实际占用保证金
            actual_margin = fill_price * Decimal(str(fill_vol * mult)) * self.risk_manager.margin_ratio
            margin_diff = frozen - actual_margin
            self.account.available += margin_diff
            self.account.margin += actual_margin

            # 更新持仓
            pos = self.positions.get(pos_key)
            if not pos:
                pos = PositionData(
                    instrument=order.instrument,
                    direction=order.direction,
                    volume=fill_vol,
                    open_price_avg=fill_price,
                    margin=actual_margin
                )
                self.positions[pos_key] = pos
            else:
                total_cost = (pos.open_price_avg * Decimal(str(pos.volume))) + (fill_price * Decimal(str(fill_vol)))
                pos.volume += fill_vol
                pos.open_price_avg = (total_cost / Decimal(str(pos.volume))).quantize(Decimal("0.01"))
                pos.margin += actual_margin

        # ----------------- 平仓成交处理 -----------------
        else:
            # 平仓对应持仓方向相反
            opp_direction = Direction.SELL if order.direction == Direction.BUY else Direction.BUY
            opp_key = self.get_pos_key(order.instrument, opp_direction)
            pos = self.positions.get(opp_key)

            if pos and pos.volume > 0:
                pos.frozen_volume = max(0, pos.frozen_volume - fill_vol)
                
                # 计算平仓释放保证金比例
                released_margin = (pos.margin * Decimal(str(fill_vol)) / Decimal(str(pos.volume))).quantize(Decimal("0.01"))
                pos.margin -= released_margin
                self.account.margin -= released_margin

                # 计算平仓盈亏
                if opp_direction == Direction.BUY:
                    # 平多仓 (卖平)
                    pnl = (fill_price - pos.open_price_avg) * Decimal(str(fill_vol * mult))
                else:
                    # 平空仓 (买平)
                    pnl = (pos.open_price_avg - fill_price) * Decimal(str(fill_vol * mult))

                self.account.realized_pnl += pnl
                self.account.balance += pnl
                self.account.available += (released_margin + pnl)

                pos.volume -= fill_vol
                if pos.volume == 0:
                    # ⚠️ 持仓归零必须从字典中移除, 不能只把字段清零:
                    # 残留的"僵尸持仓"会被 _execute_fill 的开仓分支复用
                    # (pos = self.positions.get(pos_key) 命中旧对象), 导致
                    # open_price_avg / margin 带着旧值参与加权, 均价与保证金被污染。
                    pos.open_price_avg = Decimal("0.0")
                    pos.margin = Decimal("0.0")
                    pos.floating_pnl = Decimal("0.0")
                    pos.frozen_volume = 0
                    self.positions.pop(opp_key, None)

        logger.info(f"Fill executed: {trade_id} {order.instrument} {order.direction.value} {fill_vol}@{fill_price}")
        notifier.notify("TRADE_FILLED", f"模拟成交: {order.instrument} {order.direction.value} {order.offset.value} {fill_vol}手 @ {fill_price}", "INFO", "paper_trader")

    @staticmethod
    def _calc_commission(volume: int) -> Decimal:
        """按手数计算手续费。默认 0 (不收取), 由配置项控制。"""
        try:
            per_lot = Decimal(str(getattr(cfg.system, "paper_commission_per_lot", 0) or 0))
        except Exception:
            return Decimal("0")
        if per_lot <= 0:
            return Decimal("0")
        return (per_lot * Decimal(str(volume))).quantize(Decimal("0.01"))

    def get_account(self) -> AccountData:
        with self._lock:
            return self.account.model_copy(deep=True)

    def get_positions(self) -> List[PositionData]:
        with self._lock:
            return [p.model_copy(deep=True) for p in self.positions.values() if p.volume > 0]

    def get_orders(self, limit=None, offset=0, statuses=None) -> List[OrderData]:
        with self._lock:
            source = self.active_orders if statuses == {"SUBMITTED", "PARTIAL"} else self.orders
            orders = (o for o in reversed(source.values()) if not statuses or o.status in statuses)
            page = islice(orders, offset, offset + limit if limit is not None else None)
            return list(reversed([o.model_copy(deep=True) for o in page]))

    def get_trades(self, limit=None, offset=0) -> List[TradeData]:
        with self._lock:
            page = islice(reversed(self.trades), offset, offset + limit if limit is not None else None)
            return list(reversed([t.model_copy(deep=True) for t in page]))




    @atomic
    def close_all_positions(self) -> List[str]:
        """一键市价/最新价全平所有持仓 (紧急平仓)。

        紧急场景下必须"能平的先平": 某个品种缺行情或校验失败时**跳过该品种**,
        继续处理其余持仓, 绝不能因一个品种而中断整轮全平。
        返回实际提交的委托 id 列表; 被跳过的品种通过 skipped 记录并告警。
        """
        submitted_ids = []
        skipped = []
        for pos_key, pos in list(self.positions.items()):
            if pos.volume <= 0:
                continue
            avail = pos.volume - pos.frozen_volume
            if avail <= 0:
                skipped.append((pos.instrument, "持仓已全部冻结, 无可平手数"))
                continue

            # 取当前该合约价格
            close_price = self.last_prices.get(pos.instrument)
            if close_price is None:
                skipped.append((pos.instrument, "尚无有效行情"))
                continue

            # 多头持仓用卖平，空头持仓用买平
            close_dir = Direction.SELL if pos.direction == Direction.BUY else Direction.BUY
            # ⚠️ 拆单: 单笔手数仍受风控/交易所报单习惯约束。
            # 即使平仓已豁免 max_order_volume, 一次报 177 手也容易在实盘触发
            # "报单量过大" 类柜台限制, 因此按 max_order_volume 分批提交。
            batch_size = max(1, int(self.risk_manager.max_order_volume))
            remain = avail
            while remain > 0:
                this_vol = min(batch_size, remain)
                try:
                    req = OrderRequest(
                        instrument=pos.instrument,
                        direction=close_dir,
                        offset=OffsetFlag.CLOSE,
                        price=close_price,
                        volume=this_vol
                    )
                    order = self.submit_order(req)
                except Exception as e:
                    skipped.append((pos.instrument, str(e)))
                    logger.exception("紧急全平 %s 提交失败, 继续处理其余持仓", pos.instrument)
                    break

                if order.status == OrderStatus.REJECTED:
                    # 已在 submit_order 内部记入 self.orders (REJECTED), 前端可见
                    skipped.append((pos.instrument, order.reject_reason or "被风控拒绝"))
                    break

                submitted_ids.append(order.order_id)
                remain -= this_vol
                # 同一持仓本轮内已冻结 this_vol, 下一批需扣除刚冻结的部分
                if remain > 0:
                    pos.frozen_volume = max(0, pos.frozen_volume - this_vol)

        if skipped:
            detail = "; ".join(f"{sym}({why})" for sym, why in skipped)
            logger.warning("紧急全平: %d 个品种已提交, %d 个品种被跳过 -> %s",
                           len(submitted_ids), len(skipped), detail)
            notifier.notify(
                "EMERGENCY_CLOSE_PARTIAL",
                f"紧急全平已提交 {len(submitted_ids)} 笔, 但有 {len(skipped)} 个品种未能平仓: {detail}",
                "ERROR",
                "paper_trader",
            )
        return submitted_ids


paper_trader = PaperTrader()
