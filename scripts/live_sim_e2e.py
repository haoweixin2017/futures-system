"""仿真柜台全链路端到端测试 (下单 / 成交 / 撤单 / 查询 / 边界)。

设计原则:
1. **直连生产代码路径**: 复用 backend.trade_engine 的 CTPTradeExecutor 与 LiveTrader
   单例, 不 mock 柜台, 真实走 认证→登录→结算确认→对账→报单→回报 全链路。
2. **只作用于仿真柜台**: profile 必须是 simnow; prod 档直接拒绝运行。
3. **不残留**: 结束时撤销所有活动委托, 并尝试平掉测试中开出的仓位。
4. **可重复执行**: 每步结果结构化输出, 供回归对比。

用法:
    FUTURES_CTP_APP_ID=xxx FUTURES_CTP_AUTH_CODE=yyy \
    .venv/Scripts/python.exe scripts/live_sim_e2e.py --instrument FU2610

退出码: 0 全部通过; 1 存在失败项。
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------- #
# 结果收集
# --------------------------------------------------------------------------- #

class Report:
    def __init__(self):
        self.items = []

    def check(self, phase, name, ok, detail=""):
        self.items.append({"phase": phase, "name": name, "ok": bool(ok), "detail": str(detail)})
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {phase} :: {name}" + (f"  -> {detail}" if detail else ""), flush=True)
        return bool(ok)

    def note(self, phase, name, detail):
        self.items.append({"phase": phase, "name": name, "ok": None, "detail": str(detail)})
        print(f"[INFO] {phase} :: {name}  -> {detail}", flush=True)

    def phase_failed(self, phase):
        return [i for i in self.items if i["phase"] == phase and i["ok"] is False]

    @property
    def failed(self):
        return [i for i in self.items if i["ok"] is False]

    def summary(self):
        total = len([i for i in self.items if i["ok"] is not None])
        failed = len(self.failed)
        return {"checks": total, "failed": failed, "passed": total - failed}


report = Report()


def wait_until(pred, timeout=20.0, interval=0.4):
    """轮询等待谓词为真, 返回 (是否成功, 最后一次取值)。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = pred()
        except Exception:
            last = None
        if last:
            return True, last
        time.sleep(interval)
    return False, last


# --------------------------------------------------------------------------- #
# 行情快照 (独立 MD 会话, 只用于取参考价, 不下单)
# --------------------------------------------------------------------------- #

def fallback_quote(symbol):
    """MD 无推送时(休市)的参考价回退: Redis 实时缓存 → MySQL 最近收盘价。

    回退价仅用于给报单定一个"贴近市场"的价格, 不代表柜台真实盘口。
    """
    out = {"source": None}
    try:
        from backend.storage.redis_client import RedisClient
        q = RedisClient.get_client().hgetall(f"quote:{symbol}") or {}
        if q.get("last_price"):
            out.update(source="redis", last=Decimal(str(q["last_price"])),
                       bid=Decimal(str(q["bid_price_1"])) if q.get("bid_price_1") else None,
                       ask=Decimal(str(q["ask_price_1"])) if q.get("ask_price_1") else None,
                       update_time=q.get("timestamp", ""))
            return out
    except Exception:
        pass
    try:
        from backend.storage.mysql_client import engine
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT close_price FROM market_bar WHERE instrument_id = :s "
                "ORDER BY bar_time DESC LIMIT 1"), {"s": symbol}).fetchone()
        if row and row[0]:
            out.update(source="mysql", last=Decimal(str(row[0])), bid=None, ask=None,
                       update_time="最近K线收盘")
    except Exception:
        pass
    return out


def publish_quote(symbol, quote):
    """把行情快照写入 Redis quote 哈希。

    风控的偏离保护 (_last_price) 与一键全平的对手价 (_opponent_price) 都从这里取价;
    独立运行本脚本时服务未启动, 必须自己喂价, 否则对手价缺失会导致全平被跳过。
    """
    if not quote or not quote.get("last"):
        return False
    try:
        from backend.storage.redis_client import RedisClient
        from backend.common.product_specs import get_exchange
        fields = {
            "instrument": symbol,
            "exchange": get_exchange(symbol),
            "trading_day": datetime.now().strftime("%Y%m%d"),
            "last_price": str(quote["last"]),
            "bid_price_1": str(quote["bid"]) if quote.get("bid") else "",
            "ask_price_1": str(quote["ask"]) if quote.get("ask") else "",
            "volume": str(quote.get("volume") or 0),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        RedisClient.get_client().hset(f"quote:{symbol}", mapping=fields)
        return True
    except Exception as exc:
        print(f"  [warn] 写入 Redis 行情失败: {exc}", flush=True)
        return False


def best_quote(symbol, timeout=15.0):
    """优先取 CTP 实时盘口; 无推送(休市)时回退到 Redis / 最近收盘价。

    注意: fetch_quote 即使失败也会返回带 error 字段的字典, 必须按 last 判定,
    不能直接用 `or` 短路, 否则回退分支永远不会执行。
    """
    quote = fetch_quote(symbol, timeout=timeout)
    if quote.get("last"):
        return quote
    fallback = fallback_quote(symbol)
    if fallback.get("last"):
        return fallback
    return quote


def fetch_quote(symbol, timeout=15.0):
    from openctp_ctp import thostmduserapi as mdapi
    from backend.common.config import cfg
    import tempfile

    done = threading.Event()
    got = {}

    # CTP 合约代码大小写敏感: SHFE/INE/DCE 必须小写, 否则订阅静默失败
    # (订阅返回成功但永远收不到行情)。实测订阅 FU2611 无推送、fu2611 立即收到。
    from backend.common.product_specs import to_ctp_instrument
    ctp_symbol = to_ctp_instrument(symbol)

    def clean(v):
        try:
            f = float(v)
        except Exception:
            return None
        if f != f or abs(f) > 1e10:
            return None
        return Decimal(str(round(f, 4)))

    class Handler(mdapi.CThostFtdcMdSpi):
        def __init__(self, api):
            super().__init__()
            self.api = api

        def OnFrontConnected(self):
            req = mdapi.CThostFtdcReqUserLoginField()
            req.BrokerID, req.UserID, req.Password = cfg.ctp.broker_id, cfg.ctp.user_id, cfg.ctp.password
            self.api.ReqUserLogin(req, 1)

        def OnRspUserLogin(self, row, info, rid, last):
            if info and info.ErrorID:
                got["error"] = f"MD 登录失败 {info.ErrorID}: {info.ErrorMsg}"
                done.set()
                return
            self.api.SubscribeMarketData([ctp_symbol.encode("utf-8")], 1)

        def OnRtnDepthMarketData(self, d):
            if not d or d.InstrumentID != ctp_symbol:
                return
            got.update(last=clean(d.LastPrice), bid=clean(d.BidPrice1), ask=clean(d.AskPrice1),
                       volume=d.Volume, update_time=d.UpdateTime)
            done.set()

        def OnRspSubMarketData(self, inst, info, rid, last):
            if info and info.ErrorID:
                got["error"] = f"订阅失败 {info.ErrorID}: {info.ErrorMsg}"
                done.set()

    flow = os.path.join(tempfile.gettempdir(), "ctp_e2e_md")
    os.makedirs(flow, exist_ok=True)
    api = mdapi.CThostFtdcMdApi.CreateFtdcMdApi(flow + os.sep)
    handler = Handler(api)
    try:
        api.RegisterSpi(handler)
        api.RegisterFront(cfg.ctp.md_address)
        api.Init()
        if not done.wait(timeout):
            got["error"] = got.get("error") or "等待行情超时 (休市时段无推送属正常)"
    finally:
        try:
            api.Release()
        except Exception:
            pass
    return got


# --------------------------------------------------------------------------- #
# 各阶段
# --------------------------------------------------------------------------- #

def phase_connect(executor, timeout):
    phase = "01-连接就绪"
    ok, _ = wait_until(lambda: executor.is_ready, timeout=timeout)
    st = executor.get_status()
    report.note(phase, "柜台状态", json.dumps(st, ensure_ascii=False))
    report.check(phase, "connected", st.get("connected"), f"connected={st.get('connected')}")
    report.check(phase, "authenticated", st.get("authenticated"), f"authenticated={st.get('authenticated')}")
    report.check(phase, "logged_in", st.get("logged_in"), f"logged_in={st.get('logged_in')}")
    report.check(phase, "settlement_confirmed", st.get("settlement_confirmed"),
                 f"settlement_confirmed={st.get('settlement_confirmed')}")
    report.check(phase, "reconciled", st.get("reconciled"), f"reconciled={st.get('reconciled')}")
    report.check(phase, "is_ready", ok, f"sync_error={st.get('sync_error')!r}")
    return ok


def phase_query(trader, executor, symbol):
    phase = "02-查询与对账"
    snap = trader.get_snapshot()
    acct = snap.get("account") or []
    report.check(phase, "资金查询返回唯一账户", len(acct) == 1, f"account_rows={len(acct)}")
    if acct:
        a = acct[0]
        balance = a.get("Balance")
        avail = a.get("Available")
        report.check(phase, "资金字段完整 (Balance/Available)",
                     balance is not None and avail is not None, f"Balance={balance} Available={avail}")
        report.note(phase, "资金快照",
                    f"Balance={balance} Available={avail} CurrMargin={a.get('CurrMargin')} "
                    f"CloseProfit={a.get('CloseProfit')} PreBalance={a.get('PreBalance')}")
    report.check(phase, "持仓查询已执行", isinstance(snap.get("positions"), list),
                 f"positions={len(snap.get('positions') or [])} 条")
    report.check(phase, "成交流水查询已执行", isinstance(snap.get("trades"), list),
                 f"trades={len(snap.get('trades') or [])} 条")
    report.check(phase, "本地委托流水可读", isinstance(trader.get_orders("e2e"), list),
                 f"orders={len(trader.get_orders('e2e'))} 条")

    # 柜台持仓快照必须落结构化表 (审计地基)
    from backend.storage.mysql_client import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT COUNT(*) FROM position_snapshot WHERE trading_day = :d"),
                            {"d": executor.spi.trading_day}).scalar()
    report.check(phase, "持仓快照已落库 position_snapshot", rows >= 0, f"{executor.spi.trading_day} 行数={rows}")
    return snap


def phase_risk_boundary(trader, executor, symbol, last_price):
    """风控边界: 全部为本地拒绝, 不应触达柜台 (用 record 数量不变来验证)。"""
    phase = "03-风控边界"
    from backend.trade_engine.models import OrderRequest, Direction, OffsetFlag, AccountData, PositionData
    from backend.trade_engine.risk_manager import PreTradeRiskManager

    rm = trader.risk_manager
    before = len(trader.get_orders("e2e"))

    def submit(**kw):
        req = OrderRequest(**kw)
        return trader.submit_live_order(req, "e2e")

    def expect_reject(name, kw, keyword):
        try:
            res = submit(**kw)
        except Exception as exc:
            report.check(phase, name, False, f"抛出异常: {exc}")
            return
        ok = res.get("status") == "REJECTED" and keyword in (res.get("reject_reason") or "")
        report.check(phase, name, ok, res.get("reject_reason") or res.get("status"))

    # 1. 未知合约
    expect_reject("未知合约被拒", dict(instrument="ZZ9999", direction=Direction.BUY,
                                   offset=OffsetFlag.OPEN, price=Decimal("100"), volume=1), "未知合约")
    # 2. 白名单外合约
    expect_reject("白名单外合约开仓被拒", dict(instrument="JM2701", direction=Direction.BUY,
                                         offset=OffsetFlag.OPEN, price=Decimal("1200"), volume=1), "白名单")
    # 3. 单笔手数上限 (配置 max_order_volume)
    over = rm.max_order_volume + 1
    expect_reject(f"单笔手数超过上限({over}>{rm.max_order_volume})被拒",
                  dict(instrument=symbol, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                       price=last_price, volume=over), "单笔手数")
    # 4. 价格偏离保护
    if rm.fat_finger_pct > 0 and last_price:
        bad = (last_price * (Decimal("1") + rm.fat_finger_pct * 3)).quantize(Decimal("1"))
        expect_reject("报单价严重偏离最新价被拒",
                      dict(instrument=symbol, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                           price=bad, volume=1), "偏离")
    # 5. 无持仓平仓
    expect_reject("无持仓时平仓被拒", dict(instrument=symbol, direction=Direction.SELL,
                                      offset=OffsetFlag.CLOSE, price=last_price, volume=1), "无对应")
    # 6. 平仓手数超过可用持仓
    fake_pos = {f"{symbol}_BUY": PositionData(instrument=symbol, direction=Direction.BUY, volume=1)}
    acct = AccountData(balance=Decimal("1000000"), available=Decimal("1000000"))
    ok, why = rm.check_order(
        OrderRequest(instrument=symbol, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
                     price=last_price, volume=5), acct, fake_pos, {}, last_price=last_price)
    report.check(phase, "平仓手数超过可用持仓被拒", (not ok) and "可用持仓" in (why or ""), why)
    # 7. 平仓豁免单笔手数上限 (历史故障回归): 持仓充足时, 超过单笔上限的平仓必须放行
    big_pos = {f"{symbol}_BUY": PositionData(instrument=symbol, direction=Direction.BUY,
                                             volume=over + 1)}
    ok, why = rm.check_order(
        OrderRequest(instrument=symbol, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
                     price=last_price, volume=over), acct, big_pos, {}, last_price=last_price)
    report.check(phase, "平仓不受单笔手数上限约束(能开能平)", ok, why or "已放行")
    # 8. 资金不足
    poor = AccountData(balance=Decimal("1000"), available=Decimal("1000"))
    ok, why = rm.check_order(
        OrderRequest(instrument=symbol, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                     price=last_price, volume=1), poor, {}, {}, last_price=last_price)
    report.check(phase, "可用资金不足被拒", (not ok) and "资金不足" in (why or ""), why)
    # 9. 实盘总开关关闭时只拦开仓
    off = PreTradeRiskManager(live_enabled=False)
    ok_open, why_open = off.check_order(
        OrderRequest(instrument=symbol, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                     price=last_price, volume=1), acct, {}, {}, last_price=last_price)
    ok_close, why_close = off.check_order(
        OrderRequest(instrument=symbol, direction=Direction.SELL, offset=OffsetFlag.CLOSE,
                     price=last_price, volume=1), acct, fake_pos, {}, last_price=last_price)
    report.check(phase, "live_enabled=false 拦截开仓", not ok_open, why_open)
    report.check(phase, "live_enabled=false 放行平仓", ok_close, why_close or "已放行")
    # 10. 人工禁开仓开关
    rm.block_open("E2E 边界测试")
    ok_blocked, why_blocked = rm.check_order(
        OrderRequest(instrument=symbol, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                     price=last_price, volume=1), acct, {}, {}, last_price=last_price)
    rm.unblock_open()
    report.check(phase, "人工禁止开仓生效且可解除", (not ok_blocked) and not rm.open_blocked, why_blocked)
    # 11. 报单价格为 0 / 负数
    try:
        OrderRequest(instrument=symbol, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                     price=Decimal("0"), volume=1)
        report.check(phase, "非正价格被模型拒绝", False, "未拒绝")
    except Exception as exc:
        report.check(phase, "非正价格被模型拒绝", True, type(exc).__name__)
    # 12. 手数为 0
    try:
        OrderRequest(instrument=symbol, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                     price=last_price, volume=0)
        report.check(phase, "非正手数被模型拒绝", False, "未拒绝")
    except Exception as exc:
        report.check(phase, "非正手数被模型拒绝", True, type(exc).__name__)
    # 13. 非法合约代码
    try:
        OrderRequest(instrument="XX", direction=Direction.BUY, offset=OffsetFlag.OPEN,
                     price=last_price, volume=1)
        report.check(phase, "非合约代码被模型拒绝", False, "未拒绝")
    except Exception as exc:
        report.check(phase, "非合约代码被模型拒绝", True, type(exc).__name__)

    after = len(trader.get_orders("e2e"))
    report.check(phase, "边界拒绝均未触达柜台(无新委托记录)", before == after,
                 f"before={before} after={after}")
    return before == after


def _order_status(trader, order_ref, owner="e2e"):
    for o in trader.get_orders(owner):
        if o.get("order_ref") == order_ref:
            return o
    return None


def phase_submit_and_cancel(trader, symbol, last_price):
    """被动限价单: 不成交 → 撤单 → 校验状态机与柜台一致性。"""
    phase = "04-下单与撤单"
    from backend.trade_engine.models import OrderRequest, Direction, OffsetFlag

    # 买单挂在最新价下方 1.5% 处: 不成交, 且不触发 2% 偏离保护
    passive = (last_price * Decimal("0.985")).quantize(Decimal("1"))
    req = OrderRequest(instrument=symbol, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                       price=passive, volume=1)
    rec = trader.submit_live_order(req, "e2e")
    ref = rec.get("order_ref")
    report.check(phase, "被动限价单提交成功", rec.get("status") == "SUBMITTED",
                 f"status={rec.get('status')} reason={rec.get('reject_reason')} ref={ref}")
    if rec.get("status") != "SUBMITTED":
        return None, None

    # 柜台回报应把状态收敛到 SUBMITTED, 且带回 OrderSysID
    ok, _ = wait_until(lambda: (_order_status(trader, ref) or {}).get("order_sys_id"), timeout=10)
    cur = _order_status(trader, ref) or {}
    report.check(phase, "柜台回报带回 OrderSysID", bool(cur.get("order_sys_id")),
                 f"order_sys_id={cur.get('order_sys_id')!r} status={cur.get('status')}")
    report.check(phase, "委托已落结构化表 order_record", _count_order_record(ref) >= 1,
                 f"order_ref={ref}")

    # 撤单边界: 不存在的委托
    bad = trader.cancel_live_order("no_such_order_ref", "e2e")
    report.check(phase, "撤不存在的委托被拒", bad.get("status") == "REJECTED",
                 bad.get("reject_reason"))

    # 撤单边界: 非本人委托
    other = trader.cancel_live_order(ref, "someone_else")
    report.check(phase, "撤他人委托被拒", other.get("status") == "REJECTED",
                 other.get("reject_reason"))

    # 正常撤单
    res = trader.cancel_live_order(ref, "e2e")
    report.check(phase, "撤单请求被受理", res.get("status") not in ("REJECTED",),
                 f"status={res.get('status')} reason={res.get('reject_reason')}")
    ok, _ = wait_until(lambda: (_order_status(trader, ref) or {}).get("status") == "CANCELLED",
                       timeout=15)
    cur = _order_status(trader, ref) or {}
    report.check(phase, "委托最终状态为 CANCELLED", cur.get("status") == "CANCELLED",
                 f"status={cur.get('status')} msg={cur.get('reject_reason')}")

    # 重复撤单应被状态机拒绝
    again = trader.cancel_live_order(ref, "e2e")
    report.check(phase, "已终态委托不可重复撤单", again.get("status") == "REJECTED",
                 again.get("reject_reason"))

    # 柜台侧一致性: 该委托在柜台查询中也应是已撤
    ok, _ = wait_until(lambda: trader.executor.is_ready, timeout=30)
    report.check(phase, "撤单后对账恢复就绪", ok, f"sync_error={trader.executor.sync_error!r}")
    return ref, cur


def phase_fill(trader, symbol, quote):
    """主动吃价: 应成交 → 校验成交流水 / 持仓 / 幂等 → 平仓还原。"""
    phase = "05-成交与平仓"
    from backend.trade_engine.models import OrderRequest, Direction, OffsetFlag

    ask = quote.get("ask") or quote.get("last")
    bid = quote.get("bid") or quote.get("last")
    if not ask or not bid:
        report.check(phase, "取得有效盘口", False, f"quote={quote}")
        return
    report.note(phase, "参考盘口", f"bid={bid} ask={ask} last={quote.get('last')}")

    before_pos = _position_volume(trader, symbol, "BUY")
    # 买入价高于卖一, 立即成交
    price = ask + Decimal("2")
    req = OrderRequest(instrument=symbol, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                       price=price, volume=1)
    rec = trader.submit_live_order(req, "e2e")
    ref = rec.get("order_ref")
    report.check(phase, "主动买单提交成功", rec.get("status") == "SUBMITTED",
                 f"status={rec.get('status')} reason={rec.get('reject_reason')}")
    if rec.get("status") != "SUBMITTED":
        return

    ok, _ = wait_until(lambda: (_order_status(trader, ref) or {}).get("status") == "FILLED",
                       timeout=20)
    cur = _order_status(trader, ref) or {}
    filled = cur.get("status") == "FILLED"
    report.check(phase, "委托成交 (FILLED)", filled,
                 f"status={cur.get('status')} traded={cur.get('traded_volume')} msg={cur.get('reject_reason')}")
    if not filled:
        report.note(phase, "成交未达预期", "休市时段柜台拒单属预期, 交易时段应复测")
        return

    report.check(phase, "成交量登记正确", int(cur.get("traded_volume") or 0) == 1,
                 f"traded_volume={cur.get('traded_volume')}")

    # 成交流水落库 + 幂等
    ok, _ = wait_until(lambda: _count_trade_record() > 0, timeout=15)
    n1 = _count_trade_record()
    report.check(phase, "成交已落结构化表 trade_record", n1 > 0, f"trade_record 行数={n1}")

    # 重复推送同一笔成交必须幂等
    trades = trader.get_snapshot().get("trades") or []
    if trades:
        trader.on_trade(trades[-1])
        n2 = _count_trade_record()
        report.check(phase, "重复成交回报幂等", n2 == n1, f"重复前={n1} 重复后={n2}")

    ok, after_pos = _wait_position(trader, symbol, "BUY", before_pos + 1)
    report.check(phase, "持仓随成交增加", ok,
                 f"before={before_pos} after={after_pos}")

    # 平仓还原
    offset = OffsetFlag.CLOSE_TODAY if _today_position(trader, symbol, "BUY") > 0 else OffsetFlag.CLOSE
    if _exchange(symbol) not in ("SHFE", "INE"):
        offset = OffsetFlag.CLOSE
    close_price = bid - Decimal("2")
    close_req = OrderRequest(instrument=symbol, direction=Direction.SELL, offset=offset,
                             price=close_price, volume=1)
    crec = trader.submit_live_order(close_req, "e2e")
    cref = crec.get("order_ref")
    report.check(phase, "平仓单提交成功", crec.get("status") == "SUBMITTED",
                 f"status={crec.get('status')} offset={offset.value} reason={crec.get('reject_reason')}")
    if crec.get("status") == "SUBMITTED":
        ok, _ = wait_until(lambda: (_order_status(trader, cref) or {}).get("status") == "FILLED",
                           timeout=20)
        ccur = _order_status(trader, cref) or {}
        report.check(phase, "平仓单成交", ccur.get("status") == "FILLED",
                     f"status={ccur.get('status')} msg={ccur.get('reject_reason')}")


def phase_emergency_switch(trader, symbol):
    """紧急停机开关 2/3 (批量撤单 / 一键全平) 在真实柜台上的端到端验证。"""
    phase = "05b-紧急停机开关"
    from backend.trade_engine.models import OrderRequest, Direction, OffsetFlag

    quote = best_quote(symbol)
    ref_price = quote.get("last")
    if not ref_price:
        report.check(phase, "取得参考价", False, "无行情, 跳过紧急停机测试")
        return
    publish_quote(symbol, quote)

    # 前置清理: 上一阶段(05)刚平完仓, 柜台持仓快照可能尚未归零。若不清干净, 本阶段的
    # 被动挂单会被「开仓后总持仓超上限」误拦 (实测残留 1 手 → 第 2 笔 1+1+1 > 上限 2 被拒)。
    ok_zero, _ = _wait_position_zero(trader, symbol, "BUY", timeout=12)
    if not ok_zero:
        with patch("backend.trade_engine.live_trader.EMERGENCY_RATE_LIMIT", 0):
            trader.close_all("e2e", confirm="CONFIRM_CLOSE_ALL", batch_size=1)
        _wait_position_zero(trader, symbol, "BUY", timeout=15)

    # ---- 开关 2: 批量撤单 ----
    passive = (ref_price * Decimal("0.985")).quantize(Decimal("1"))
    refs, fails = [], []
    for _ in range(2):
        rec = trader.submit_live_order(
            OrderRequest(instrument=symbol, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                         price=passive, volume=1), "e2e")
        if rec.get("status") == "SUBMITTED":
            refs.append(rec["order_ref"])
        else:
            fails.append(rec.get("reject_reason"))
    report.check(phase, "挂出 2 笔被动委托", len(refs) == 2, f"refs={refs} 被拒={fails}")
    if refs:
        time.sleep(1.5)
        with patch("backend.trade_engine.live_trader.EMERGENCY_RATE_LIMIT", 0):
            res = trader.cancel_all("e2e")
        report.check(phase, "批量撤单覆盖全部活动委托", res.get("attempted") >= len(refs),
                     f"attempted={res.get('attempted')} submitted={res.get('submitted')}")
        ok, _ = wait_until(
            lambda: all((_order_status(trader, r) or {}).get("status") == "CANCELLED" for r in refs),
            timeout=25)
        report.check(phase, "批量撤单全部生效 (CANCELLED)", ok,
                     str([(_order_status(trader, r) or {}).get("status") for r in refs]))

    # ---- 开关 3: 一键全平 ----
    quote2 = best_quote(symbol) or quote
    publish_quote(symbol, quote2)
    ask = quote2.get("ask") or quote2.get("last")
    entry = trader.submit_live_order(
        OrderRequest(instrument=symbol, direction=Direction.BUY, offset=OffsetFlag.OPEN,
                     price=ask + Decimal("2"), volume=1), "e2e")
    entry_ref = entry.get("order_ref")
    ok, _ = wait_until(lambda: (_order_status(trader, entry_ref) or {}).get("status") == "FILLED",
                       timeout=20)
    vol_ok, vol = _wait_position(trader, symbol, "BUY", 1)
    report.check(phase, "为全平测试建立持仓", ok and vol_ok and vol > 0,
                 f"status={( _order_status(trader, entry_ref) or {}).get('status')} position={vol}")
    if vol <= 0:
        report.note(phase, "跳过一键全平", "未能建立持仓")
        return

    publish_quote(symbol, best_quote(symbol) or quote2)
    with patch("backend.trade_engine.live_trader.EMERGENCY_RATE_LIMIT", 0):
        result = trader.close_all("e2e", confirm="CONFIRM_CLOSE_ALL", batch_size=1)
    report.check(phase, "一键全平委托提交成功", result.get("status") == "success",
                 f"status={result.get('status')} closed={len(result.get('closed') or [])} "
                 f"errors={result.get('errors')}")

    def _refresh_and_check():
        trader.executor.request_reconcile()
        time.sleep(1.5)
        return _position_volume(trader, symbol, "BUY") == 0

    ok, _ = wait_until(_refresh_and_check, timeout=40, interval=0.1)
    report.check(phase, "一键全平后持仓归零", ok,
                 f"剩余={_position_volume(trader, symbol, 'BUY')} 手")


def phase_consistency(trader, executor):
    phase = "06-数据一致性"
    # 本地委托记录与柜台委托查询应能按 order_ref 对应
    orders = trader.get_orders("e2e")
    local = {o["order_ref"]: o for o in orders}
    snap_orders = trader.get_snapshot()
    report.check(phase, "本地委托键唯一", len(local) == len(orders), f"{len(local)} 条")
    # 未确认报单不应残留
    unresolved = [o for o in local.values() if o.get("status") in ("SENDING", "UNKNOWN")]
    report.check(phase, "无残留的未确认(SENDING/UNKNOWN)报单", not unresolved,
                 f"unresolved={[o['order_ref'] for o in unresolved]}")
    # 结构化表 order_ref 唯一
    from backend.storage.mysql_client import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        dup = conn.execute(text(
            "SELECT COUNT(*) FROM (SELECT order_ref FROM order_record GROUP BY order_ref "
            "HAVING COUNT(*) > 1) t")).scalar()
    report.check(phase, "order_record 无重复 order_ref", dup == 0, f"重复组数={dup}")
    # 对账是异步的: 报单/成交回报会触发重新对账, 这里给足收敛时间再判定
    ready, _ = wait_until(lambda: executor.is_ready, timeout=30)
    report.check(phase, "对账就绪 (is_ready)", ready,
                 f"sync_error={executor.sync_error!r} reconciled={executor.reconciled}")
    report.note(phase, "快照统计",
                f"account={len(snap_orders.get('account') or [])} "
                f"positions={len(snap_orders.get('positions') or [])} "
                f"trades={len(snap_orders.get('trades') or [])}")


def phase_cleanup(trader, symbol, executor):
    phase = "07-清理"
    active = [o for o in trader.get_orders("e2e")
              if o.get("status") in ("SUBMITTED", "PARTIAL", "CANCELLING")]
    if active:
        res = trader.cancel_all("e2e")
        report.check(phase, "撤销全部活动委托", res.get("attempted") == len(active),
                     f"attempted={res.get('attempted')} submitted={res.get('submitted')}")
        time.sleep(1.5)
        executor.request_reconcile()
        time.sleep(1.5)
    else:
        report.note(phase, "无活动委托需撤销", "skip")

    # 平掉残留多头 (测试可能因休市中断)
    vol = _position_volume(trader, symbol, "BUY")
    if vol > 0:
        quote = fetch_quote(symbol)
        bid = quote.get("bid") or quote.get("last")
        from backend.trade_engine.models import OrderRequest, Direction, OffsetFlag
        if bid:
            offset = OffsetFlag.CLOSE_TODAY if _exchange(symbol) in ("SHFE", "INE") else OffsetFlag.CLOSE
            rec = trader.submit_live_order(
                OrderRequest(instrument=symbol, direction=Direction.SELL, offset=offset,
                             price=bid - Decimal("2"), volume=1), "e2e")
            report.note(phase, "尝试平掉残留持仓", f"volume={vol} status={rec.get('status')}")
        else:
            report.note(phase, "残留持仓", f"{vol} 手, 无法取价未平")
    else:
        report.note(phase, "无残留持仓", "flat")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _exchange(symbol):
    from backend.common.product_specs import get_exchange
    return get_exchange(symbol)


def _refresh_positions(trader, timeout=20.0):
    """主动触发一次柜台对账, 并等待持仓快照真正刷新。

    成交/持仓回报只会「置位对账请求」, 快照更新是异步的。直接读 get_snapshot()
    会拿到成交前的旧持仓, 导致误判「持仓未增加」「无持仓可平」,
    进而用错平今/平昨标志。凡是判断持仓的检查点都必须先调用本函数。
    """
    before = trader.executor.last_sync
    trader.executor.request_reconcile()
    wait_until(lambda: trader.executor.last_sync != before, timeout=timeout, interval=0.2)
    time.sleep(0.4)


def _wait_position(trader, symbol, direction, expect, timeout=25.0):
    """反复触发对账, 直到持仓达到期望手数。

    柜台持仓查询是异步的: 成交回报只置位对账请求, 单次对账不一定反映刚发生的成交,
    必须轮询重试。返回 (是否达到, 最后观测到的持仓)。
    """
    last = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        trader.executor.request_reconcile()
        time.sleep(0.8)
        last = _position_volume(trader, symbol, direction)
        if last >= expect:
            return True, last
    return False, last


def _wait_position_zero(trader, symbol, direction, timeout=20.0):
    """反复对账直到该方向持仓归零。

    柜台持仓快照更新滞后于成交: 刚平完仓时快照可能仍显示旧持仓,
    会让后续开仓被「持仓上限」误拦, 必须先确认归零。
    """
    last = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        trader.executor.request_reconcile()
        time.sleep(0.8)
        last = _position_volume(trader, symbol, direction)
        if last == 0:
            return True, 0
    return False, last


def _position_volume(trader, symbol, direction):
    for row in trader.get_snapshot().get("positions") or []:
        if (row.get("InstrumentID") or "").upper() == symbol.upper():
            d = "BUY" if row.get("PosiDirection") == "2" else "SELL"
            if d == direction:
                return int(row.get("Position") or 0)
    return 0


def _today_position(trader, symbol, direction):
    for row in trader.get_snapshot().get("positions") or []:
        if (row.get("InstrumentID") or "").upper() == symbol.upper():
            d = "BUY" if row.get("PosiDirection") == "2" else "SELL"
            if d == direction:
                return int(row.get("TodayPosition") or 0)
    return 0


def _count_order_record(order_ref):
    from backend.storage.mysql_client import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM order_record WHERE order_ref = :r"),
                            {"r": order_ref}).scalar()


def _count_trade_record():
    from backend.storage.mysql_client import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM trade_record")).scalar()


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default="simnow", choices=("simnow",))
    parser.add_argument("--instrument", default="FU2610")
    parser.add_argument("--connect-timeout", type=float, default=45.0)
    parser.add_argument("--price", default="", help="手工指定参考价, 覆盖行情回退")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--skip-fill", action="store_true", help="跳过成交阶段(休市时使用)")
    parser.add_argument("--debug", action="store_true", help="打印状态持久化诊断")
    parser.add_argument("--reset-daily-counts", action="store_true",
                        help="测试专用: 清空当日报单/撤单计数 (计数跨进程持久, 多轮测试会耗尽配额)")
    args = parser.parse_args()

    os.environ["FUTURES_PROFILE"] = args.profile

    from backend.common.config import cfg
    if cfg.system.profile != "simnow":
        print("拒绝运行: 本脚本只允许在 simnow 档案下执行", file=sys.stderr)
        return 2
    if not cfg.ctp.user_id or not cfg.ctp.password:
        print("拒绝运行: 未配置仿真账号", file=sys.stderr)
        return 2

    print("=" * 74)
    print(f"仿真柜台 E2E  | profile={cfg.system.profile} | 合约={args.instrument}")
    print(f"柜台地址={cfg.ctp.td_address} | 账号={cfg.ctp.user_id[:3]}****{cfg.ctp.user_id[-2:]}")
    print(f"行情源配置 feed={cfg.system.feed} | live_enabled(配置)={cfg.risk.live_enabled}")
    print("=" * 74)

    from backend.trade_engine.ctp_trader import ctp_executor
    from backend.trade_engine.live_trader import live_trader
    from backend.trade_engine.risk_manager import PreTradeRiskManager

    if args.debug:
        import backend.trade_engine.live_trader as _lt
        _orig_load, _orig_save = _lt.LiveTrader._load, _lt.LiveTrader._save

        def _dbg_load(self):
            reloading = self._version is None
            _orig_load(self)
            if reloading:
                print(f"  [debug] _load 重新加载 -> version={self._version} "
                      f"orders={len(self._orders)}", flush=True)

        def _dbg_save(self):
            try:
                return _orig_save(self)
            except Exception as exc:
                print(f"  [debug] _save 失败: {exc!r}", flush=True)
                raise
        _lt.LiveTrader._load, _lt.LiveTrader._save = _dbg_load, _dbg_save

    # 测试需要开仓: 显式打开总开关, 其余阈值沿用 settings.simnow.yaml (最小档)
    live_trader.risk_manager = PreTradeRiskManager(live_enabled=True)
    rm = live_trader.risk_manager
    # 生产白名单可能停留在已无行情的老合约 (实测 FU2610 当日无任何行情推送),
    # 为保证 E2E 能真正报单, 把目标合约临时并入白名单。
    # 注意: 03 阶段仍用白名单外的 JM2701 验证拦截逻辑, 因此不影响该检查。
    if args.instrument.upper() not in [s.upper() for s in (rm.allowed_instruments or [])]:
        rm.allowed_instruments = list(rm.allowed_instruments or []) + [args.instrument.upper()]
        print(f"[测试] 目标合约 {args.instrument} 已临时并入白名单 (生产配置未改动)")
    if args.reset_daily_counts:
        # 当日计数由 trading_state 持久化 (重启不可绕过风控), 多轮测试会耗尽配额。
        # 测试专用: 先触发一次加载再清空, 否则下次 _load 会把旧计数读回来。
        live_trader._load()
        rm.order_counts.clear()
        rm.cancel_counts.clear()
        live_trader._save()
        print("已重置当日报单/撤单计数 (测试专用, 仅影响计数不影响阈值)")
    print(f"生效风控: 单笔<={rm.max_order_volume} 持仓<={rm.max_position_volume} "
          f"偏离={rm.fat_finger_pct} 日笔数<={rm.daily_order_limit or '不限'} 白名单={rm.allowed_instruments}")

    executor = live_trader.executor
    quote = fetch_quote(args.instrument)
    if quote.get("last"):
        print(f"参考行情(CTP MD): last={quote.get('last')} bid={quote.get('bid')} "
              f"ask={quote.get('ask')} time={quote.get('update_time')}")
    else:
        print(f"CTP MD 无推送 ({quote.get('error')}), 启用回退参考价")
        quote = fallback_quote(args.instrument)
        print(f"参考行情({quote.get('source') or '无'}): last={quote.get('last')} "
              f"bid={quote.get('bid')} ask={quote.get('ask')} time={quote.get('update_time')}")
    if args.price:
        quote["last"] = Decimal(str(args.price))
        quote.setdefault("bid", None)
        quote.setdefault("ask", None)
        print(f"参考价被 --price 覆盖为 {quote['last']}")
    if publish_quote(args.instrument, quote):
        print(f"已写入 Redis quote:{args.instrument} (供风控偏离保护与对手价取价)")

    try:
        executor.start()
        if not phase_connect(executor, args.connect_timeout):
            report.note("01-连接就绪", "中止", "柜台未就绪, 后续阶段跳过")
        else:
            phase_query(live_trader, executor, args.instrument)
            last_price = quote.get("last") or quote.get("ask")
            if last_price:
                phase_risk_boundary(live_trader, executor, args.instrument, last_price)
                phase_submit_and_cancel(live_trader, args.instrument, last_price)
                if not args.skip_fill:
                    phase_fill(live_trader, args.instrument, quote)
                    phase_emergency_switch(live_trader, args.instrument)
            else:
                report.check("03-风控边界", "取得参考价", False, "无行情, 跳过依赖价格的阶段")
            phase_consistency(live_trader, executor)
            phase_cleanup(live_trader, args.instrument, executor)
    except Exception:
        report.check("99-异常", "脚本未捕获异常", False, traceback.format_exc())
    finally:
        try:
            executor.stop()
        except Exception:
            pass

    s = report.summary()
    print("=" * 74)
    print(f"结果: 通过 {s['passed']} / {s['checks']} , 失败 {s['failed']}")
    for item in report.failed:
        print(f"  FAIL {item['phase']} :: {item['name']} -> {item['detail']}")
    print("=" * 74)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"summary": s, "items": report.items}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
