import time
import random
import logging
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, Optional
from backend.models.market_data import TickData
from backend.bar_engine.bar_engine import MultiPeriodBarEngine
from backend.signal_engine.signal_engine import SignalEngine
from backend.notifier.notifier import notifier
from backend.common.config import cfg

logger = logging.getLogger(__name__)

class LiveMarketSimulator:
    """
    持续高保真实时行情模拟推送器（约 500ms 频率）：
    在中信期货实盘/仿真前置账号审批到位之前，提供符合国内商品期货真实波动特征的模拟流。
    同时驱动 1m/5m/15m/30m/60m/1d K 线生成、指标计算、信号触发与 WebSocket 页面展示。
    """
    def __init__(self, bar_engine: MultiPeriodBarEngine, interval: float = 0.5):
        self.bar_engine = bar_engine
        self.interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 焦煤、燃油、沥青 基础行情状态
        self.states: Dict[str, Dict[str, Any]] = {
            "JM2701": {
                "price": Decimal("1210.0"),
                "base_price": Decimal("1210.0"),
                "vol": 45000,
                "oi": Decimal("180000.0"),
                "tick_size": Decimal("0.5"),
                "exchange": "DCE",
                "trend_step": 0
            },
            "FU2610": {
                "price": Decimal("3140.0"),
                "base_price": Decimal("3140.0"),
                "vol": 82000,
                "oi": Decimal("295000.0"),
                "tick_size": Decimal("1.0"),
                "exchange": "SHFE",
                "trend_step": 0
            },
            "BU2610": {
                "price": Decimal("3550.0"),
                "base_price": Decimal("3550.0"),
                "vol": 61000,
                "oi": Decimal("210000.0"),
                "tick_size": Decimal("1.0"),
                "exchange": "SHFE",
                "trend_step": 0
            }
        }

    def start(self):
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, name="LiveSimThread", daemon=True)
            self._thread.start()
            logger.info("LiveMarketSimulator started (continuous 500ms market streaming).")
            notifier.notify("FEED_SIMULATOR_STARTED", "模拟实时行情推送服务已启动", "INFO", "simulator")

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("LiveMarketSimulator stopped.")
        notifier.notify("FEED_SIMULATOR_STOPPED", "模拟实时行情推送服务已停止", "INFO", "simulator")

    def _run_loop(self):
        tick_counter = 0
        while self._running:
            try:
                now = datetime.now()
                trading_day = now.strftime("%Y%m%d")

                for sym, state in self.states.items():
                    tick_counter += 1
                    step = (tick_counter // 20) % 40
                    tick_size = state["tick_size"]

                    # 模拟焦煤周期性出现多头放量突破特征
                    if sym == "JM2701" and 10 <= step <= 15:
                        delta = tick_size * Decimal(str(random.choice([1.0, 2.0])))
                        vol_add = random.randint(30, 80)
                        oi_add = Decimal(str(random.randint(20, 60)))
                    # 模拟燃油空头测试
                    elif sym == "FU2610" and 25 <= step <= 30:
                        delta = -tick_size * Decimal(str(random.choice([1.0, 2.0])))
                        vol_add = random.randint(35, 90)
                        oi_add = Decimal(str(random.randint(15, 50)))
                    else:
                        delta = tick_size * Decimal(str(random.choice([-1.0, 0.0, 1.0])))
                        vol_add = random.randint(5, 25)
                        oi_add = Decimal(str(random.randint(-10, 20)))

                    state["price"] = max(Decimal("10.0"), state["price"] + delta)
                    state["vol"] += vol_add
                    state["oi"] += oi_add
                    turnover_delta = state["price"] * Decimal(str(vol_add * 10))

                    tick = TickData(
                        instrument=sym,
                        exchange=state["exchange"],
                        trading_day=trading_day,
                        timestamp=now,
                        last_price=state["price"],
                        bid_price_1=state["price"] - tick_size,
                        bid_volume_1=random.randint(5, 60),
                        ask_price_1=state["price"] + tick_size,
                        ask_volume_1=random.randint(5, 60),
                        volume=state["vol"],
                        turnover=turnover_delta * Decimal("100"),
                        open_interest=state["oi"]
                    )

                    self.bar_engine.on_tick(tick)

                time.sleep(self.interval)
            except Exception as e:
                logger.error(f"Error in LiveMarketSimulator: {e}")
                time.sleep(1.0)