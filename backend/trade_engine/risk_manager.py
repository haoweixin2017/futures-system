import logging
from decimal import Decimal
from typing import Optional, Dict, Tuple, Any, List
from backend.trade_engine.models import OrderRequest, Direction, OffsetFlag, AccountData, PositionData
from backend.common.config import cfg
from backend.common.product_specs import get_spec, get_exchange

logger = logging.getLogger(__name__)

class PreTradeRiskManager:
    """
    事前硬风控引擎 (Pre-trade Risk Management)
    杜绝违规、穿仓及异常操作：
    1. 单笔最大报单手数限制 (Max Order Volume)
    2. 最大持仓手数限制 (Max Position Volume)
    3. 账户资金充足性硬检查 (Margin Sufficiency Check)
    4. 平仓可用手数检查 (Close Position Sufficiency Check)
    5. 自成交防范 (Self-trade Prevention)
    6. 单日高频撤单限制 (Cancel Frequency Limit)
    7. 报单价格偏离保护 (Fat-finger Guard)          —— 新增
    8. 合约白名单 (Instrument Whitelist)            —— 新增
    9. 单日报单笔数上限 (Daily Order Limit)         —— 新增
    10. 实盘总开关 (Live Enable Switch)             —— 新增

    阈值全部从 config 的 risk 段读取 (支持灰度放量分档)。
    未配置时回退到下方的安全默认值, 保证旧调用方行为不变。
    """

    # 未配置 risk 段时的安全默认值 (与历史硬编码值保持一致)
    _DEFAULTS = {
        "max_order_volume": 50,
        "max_position_volume": 200,
        "margin_ratio": Decimal("0.12"),
        "max_cancel_count": 400,
        "max_daily_loss_ratio": Decimal("0.05"),
        "fat_finger_pct": Decimal("0"),
        "daily_order_limit": 0,
    }

    def __init__(
        self,
        max_order_volume: Optional[int] = None,
        max_position_volume: Optional[int] = None,
        margin_ratio: Optional[Decimal] = None,
        max_cancel_count: Optional[int] = None,
        max_daily_loss_ratio: Optional[Decimal] = None,
        fat_finger_pct: Optional[Decimal] = None,
        allowed_instruments: Optional[list] = None,
        daily_order_limit: Optional[int] = None,
        live_enabled: Optional[bool] = None,
    ):
        risk_cfg = getattr(cfg, "risk", None)

        def _pick(explicit, name):
            if explicit is not None:
                return explicit
            if risk_cfg is not None:
                value = getattr(risk_cfg, name, None)
                if value is not None:
                    return value
            return self._DEFAULTS.get(name)

        self.max_order_volume = int(_pick(max_order_volume, "max_order_volume"))
        self.max_position_volume = int(_pick(max_position_volume, "max_position_volume"))
        self.margin_ratio = Decimal(str(_pick(margin_ratio, "margin_ratio")))
        self.max_cancel_count = int(_pick(max_cancel_count, "max_cancel_count"))
        self.max_daily_loss_ratio = Decimal(str(_pick(max_daily_loss_ratio, "max_daily_loss_ratio")))
        self.fat_finger_pct = Decimal(str(_pick(fat_finger_pct, "fat_finger_pct") or 0))

        if allowed_instruments is not None:
            self.allowed_instruments = list(allowed_instruments)
        elif risk_cfg is not None:
            self.allowed_instruments = list(getattr(risk_cfg, "allowed_instruments", None) or [])
        else:
            self.allowed_instruments = []

        self.daily_order_limit = int(_pick(daily_order_limit, "daily_order_limit") or 0)
        self.live_enabled = bool(
            _pick(live_enabled, "live_enabled") if live_enabled is not None or risk_cfg is not None else False
        )
        # 白名单做规范化, 报单侧统一大写比较
        self.allowed_instruments = [s.strip().upper() for s in self.allowed_instruments if s and s.strip()]

        self.trading_day = ""
        self.day_start_equity = None
        self.cancel_counts: Dict[str, int] = {}   # instrument -> cancel_count
        self.order_counts: Dict[str, int] = {}    # trading_day -> order_count
        # 运行时软开关: 可在不重启服务的情况下禁止开仓 (紧急停机开关 1)
        self._open_blocked = False
        self._block_reason = ""

    # ---- 运行时软开关 (紧急停机开关 1) ----

    @property
    def open_blocked(self) -> bool:
        return self._open_blocked

    @property
    def block_reason(self) -> str:
        return self._block_reason

    def block_open(self, reason: str = "") -> None:
        """禁止新开仓 (平仓仍放行, 保证有持仓时能退出)。"""
        self._open_blocked = True
        self._block_reason = reason or "人工触发禁止开仓"
        logger.warning("实盘开仓已被禁止: %s", self._block_reason)

    def unblock_open(self) -> None:
        self._open_blocked = False
        self._block_reason = ""
        logger.warning("实盘开仓限制已解除")

    def get_instrument_multiplier(self, instrument: str) -> int:
        for inst in cfg.instruments:
            if inst.symbol == instrument:
                return inst.volume_multiple
        return get_spec(instrument)[0]

    def check_order(
        self,
        req: OrderRequest,
        account: AccountData,
        positions: Dict[str, PositionData],
        active_orders: Dict[str, Any],
        last_price: Optional[Decimal] = None,
    ) -> Tuple[bool, Optional[str]]:
        if not req.price.is_finite() or req.price <= 0:
            return False, "报单价格必须为正数"
        if not get_exchange(req.instrument):
            return False, "未知合约品种，无法校验交易规格"
        # 1. 单笔手数检查
        if req.volume <= 0:
            return False, "报单手数必须大于0"

        # 平仓判定: 平仓是减仓行为, 风险方向与开仓相反。
        is_closing = req.offset in (OffsetFlag.CLOSE, OffsetFlag.CLOSE_TODAY, OffsetFlag.CLOSE_YEST)

        # 10. 实盘总开关: 只拦开仓, 平仓豁免 (否则有持仓时会被锁死无法退出)
        if not is_closing and not self.live_enabled:
            return False, "实盘交易总开关未开启 (risk.live_enabled=false)，仅允许平仓"

        # 10b. 运行时软开关: 同样只拦开仓
        if not is_closing and self._open_blocked:
            return False, f"开仓已被人工禁止: {self._block_reason}"

        # 8. 合约白名单: 只限制开仓, 平仓豁免。
        # ⚠️ 平仓必须豁免: 否则会出现"能开的合约不让平", 是有意设计 (与 max_order_volume 同源考虑)。
        if not is_closing and self.allowed_instruments:
            if req.instrument.strip().upper() not in self.allowed_instruments:
                return False, f"合约 {req.instrument} 不在实盘白名单内 (仅限制开仓)"

        # ⚠️ max_order_volume 只约束「开仓」。
        # 平仓是减仓行为, 风险方向与开仓相反: 不放开会直接导致"持仓大于上限就永远平不掉"
        # (实测: 裸空 FU2611 177 手被单笔上限 50 卡死, 连续 4 次平仓全部被拒)。
        # 平仓的安全性由下方「可用持仓手数」检查兜底, 不会产生超额敞口。
        if not is_closing and req.volume > self.max_order_volume:
            return False, f"单笔手数 {req.volume} 超过风控上限 {self.max_order_volume}"

        mult = self.get_instrument_multiplier(req.instrument)

        # 2. 开仓风控检查
        if req.offset == OffsetFlag.OPEN:
            # 2.0 单日报单笔数上限
            if self.daily_order_limit > 0:
                used = self.order_counts.get(self.trading_day, 0)
                if used + 1 > self.daily_order_limit:
                    return False, f"单日报单笔数 {used} 已达上限 {self.daily_order_limit}"

            # 2.1 检查持仓上限
            pos_key = f"{req.instrument}_{req.direction.value}"
            current_pos = positions.get(pos_key)
            existing_vol = current_pos.volume if current_pos else 0
            existing_vol += sum(o.volume - o.traded_volume for o in active_orders.values()
                                if o.instrument == req.instrument and o.direction == req.direction
                                and o.offset == OffsetFlag.OPEN)
            if existing_vol + req.volume > self.max_position_volume:
                return False, f"开仓后总持仓 {existing_vol + req.volume} 超过单合约持仓上限 {self.max_position_volume}"

            # 2.2 检查资金充足性 (可用资金 >= 预估占用保证金)
            required_margin = req.price * Decimal(str(req.volume * mult)) * self.margin_ratio
            if account.available < required_margin:
                return False, f"可用资金不足: 需要保证金 {required_margin:.2f}, 当前可用 {account.available:.2f}"

            # 2.3 报单价格偏离保护 (防手滑/防程序 bug 报出天价)
            if self.fat_finger_pct > 0 and last_price and last_price > 0:
                deviation = abs(req.price - last_price) / last_price
                if deviation > self.fat_finger_pct:
                    return False, (
                        f"报单价 {req.price} 偏离最新价 {last_price} 达 {deviation * 100:.2f}%，"
                        f"超过保护阈值 {self.fat_finger_pct * 100:.1f}%"
                    )

            # 2.4 自成交防范检查 (同合约反向挂单若价格重叠则拦截)
            opp_direction = Direction.SELL if req.direction == Direction.BUY else Direction.BUY
            for o in active_orders.values():
                if o.instrument == req.instrument and o.direction == opp_direction:
                    # 如果买入价 >= 反向卖出挂单价，或卖出价 <= 反向买入挂单价
                    if req.direction == Direction.BUY and req.price >= o.price:
                        return False, f"触发自成交风控拦截: 买单价格 {req.price} 与己方卖挂单 {o.price} 重叠"
                    elif req.direction == Direction.SELL and req.price <= o.price:
                        return False, f"触发自成交风控拦截: 卖单价格 {req.price} 与己方买挂单 {o.price} 重叠"

        # 3. 平仓风控检查
        elif is_closing:
            # 平买仓看卖持仓，平卖仓看买持仓
            pos_direction = Direction.SELL if req.direction == Direction.BUY else Direction.BUY
            pos_key = f"{req.instrument}_{pos_direction.value}"
            current_pos = positions.get(pos_key)

            if not current_pos or current_pos.volume <= 0:
                return False, f"无对应 {pos_direction.value} 持仓，无法平仓"

            avail_close_vol = current_pos.volume - current_pos.frozen_volume
            if req.volume > avail_close_vol:
                return False, f"平仓手数 {req.volume} 超过可用持仓手数 {avail_close_vol} (总持仓 {current_pos.volume}, 冻结 {current_pos.frozen_volume})"

        # 4. 检查单日最大亏损熔断 (只拦开仓)
        if not is_closing:
            passed_loss, loss_reason = self.check_daily_loss_limit(account)
            if not passed_loss:
                return False, loss_reason

        return True, None

    def record_order(self) -> int:
        """记一次报单笔数, 返回当前交易日的累计笔数。"""
        cnt = self.order_counts.get(self.trading_day, 0) + 1
        self.order_counts[self.trading_day] = cnt
        # 只保留最近两个交易日, 避免字典无限增长
        for day in sorted(self.order_counts.keys(), reverse=True)[2:]:
            self.order_counts.pop(day, None)
        return cnt

    def check_daily_loss_limit(self, account: AccountData, max_loss_ratio: Optional[Decimal] = None) -> Tuple[bool, Optional[str]]:
        """
        单日累计亏损熔断检查
        计算当前动态权益相较初始/静态本金的亏损率，若超过阈值则触发熔断
        """
        ratio = max_loss_ratio if max_loss_ratio is not None else self.max_daily_loss_ratio
        baseline = self.day_start_equity
        if baseline is None:
            baseline = account.balance - account.realized_pnl
        if baseline <= 0 or account.dynamic_equity <= 0:
            return False, "账户权益不足，禁止开仓"
        loss_ratio = (baseline - account.dynamic_equity) / baseline
        if loss_ratio >= ratio:
            return False, f"触发单日最大亏损熔断线: 亏损比例 {loss_ratio*100:.2f}% >= {ratio*100:.1f}%"
        return True, None

    def roll_trading_day(self, day: str, account: AccountData):
        if day > self.trading_day:
            self.trading_day = day
            self.day_start_equity = account.dynamic_equity
            self.cancel_counts.clear()

    def record_cancel(self, instrument: str) -> bool:
        cnt = self.cancel_counts.get(instrument, 0) + 1
        self.cancel_counts[instrument] = cnt
        if cnt > self.max_cancel_count:
            logger.warning(f"合约 {instrument} 单日撤单次数 {cnt} 已超过异常交易警示线 {self.max_cancel_count}")
            return False
        return True
