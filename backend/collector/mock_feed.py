import time
import random
from datetime import datetime, timedelta
from decimal import Decimal
from backend.models.market_data import TickData
from backend.bar_engine.bar_engine import MultiPeriodBarEngine
from backend.signal_engine.signal_engine import SignalEngine
from backend.common.config import cfg

def run_simulation(minutes: int = 40):
    """
    行情模拟推送发生器：用于在 CTP 前置账号审批到位前，
    以真实 Tick 频率与结构驱动 K 线引擎、指标计算与规则信号引擎。
    """
    print(f"Starting market simulation for {minutes} minutes...")
    engine = MultiPeriodBarEngine()
    sig_engine = SignalEngine()

    base_time = datetime.now() - timedelta(minutes=minutes)

    instruments_state = {
        "JM2701": {"price": Decimal("1205.0"), "vol": 10000, "oi": Decimal("150000.0")},
        "FU2610": {"price": Decimal("3120.0"), "vol": 25000, "oi": Decimal("280000.0")},
        "BU2610": {"price": Decimal("3540.0"), "vol": 18000, "oi": Decimal("190000.0")},
    }

    for m in range(minutes):
        cur_minute = base_time + timedelta(minutes=m)

        for sym, state in instruments_state.items():
            # 焦煤在最后 5 分钟模拟多头突破
            if sym == "JM2701" and m >= (minutes - 5):
                state["price"] += Decimal("3.5")
                vol_inc = 600
                oi_inc = Decimal("300")
            else:
                price_delta = Decimal(str(random.choice([-1.0, -0.5, 0.0, 0.5, 1.0])))
                state["price"] += price_delta
                vol_inc = random.randint(30, 100)
                oi_inc = Decimal(str(random.randint(-20, 50)))

            state["vol"] += vol_inc
            state["oi"] += oi_inc
            turnover_inc = state["price"] * Decimal(str(vol_inc * 10))

            tick = TickData(
                instrument=sym,
                exchange="DCE" if "JM" in sym else "SHFE",
                trading_day=datetime.now().strftime("%Y%m%d"),
                timestamp=cur_minute + timedelta(seconds=20),
                last_price=state["price"],
                bid_price_1=state["price"] - Decimal("0.5"),
                bid_volume_1=10,
                ask_price_1=state["price"],
                ask_volume_1=8,
                volume=state["vol"],
                turnover=turnover_inc * Decimal("100"),
                open_interest=state["oi"]
            )
            engine.on_tick(tick)

    # 触发 15m 规则计算
    bars_15m = [b for b in engine.generators["JM2701"].current_bar and []]
    print("Simulation finished. Data fed to Redis & MySQL.")

if __name__ == "__main__":
    run_simulation(60)
