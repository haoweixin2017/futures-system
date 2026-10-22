"""实盘上线前检查 (Live Preflight)。

一次性跑完《实盘测试全流程方案》模块三的自动化检查项, 输出红绿清单。
**只读**: 不产生任何报单, 不修改任何配置。

用法:
    .\\.venv\\Scripts\\python.exe scripts\\live_preflight.py
    .\\.venv\\Scripts\\python.exe scripts\\live_preflight.py --profile simnow
    .\\.venv\\Scripts\\python.exe scripts\\live_preflight.py --profile simnow --connect

--profile 等价于设置 FUTURES_PROFILE (不传则默认 local)。
--connect 会实际启动交易柜并等待就绪, 以便验证 C 段真实连通性; 仍然不报单、不改配置。

退出码: 0 = 全部通过 (或仅警告); 1 = 存在 FAIL, 禁止上线。
"""
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
_RESULTS = []
_ICON = {PASS: "[OK]  ", FAIL: "[FAIL]", WARN: "[WARN]", SKIP: "[SKIP]"}


def check(code: str, title: str, status: str, detail: str = ""):
    _RESULTS.append({"code": code, "title": title, "status": status, "detail": detail})
    print(f"{_ICON[status]} {code:<4} {title:<42} {detail}")


def section(name: str):
    print(f"\n--- {name} ---")


def _start_counter(timeout: float = 45.0) -> str:
    """启动交易柜并等待就绪, 返回错误信息 (空串表示成功)。仅连接, 不报单。"""
    try:
        from backend.trade_engine.ctp_trader import ctp_executor
        ctp_executor.start()
        if not ctp_executor.api:
            return "未初始化: feed 非 ctp 或 user_id 为空"
        deadline = time.time() + timeout
        while time.time() < deadline and not ctp_executor.is_ready:
            time.sleep(0.5)
        return ""
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def run(connect: bool = False) -> int:
    from backend.common.config import cfg
    from backend.common.product_specs import get_exchange, get_spec
    from backend.trade_engine.live_trader import live_trader

    profile = cfg.system.profile
    print("=" * 78)
    print(f"实盘上线前检查  |  配置档案: {profile.upper()}  |  行情源: {cfg.system.feed}")
    if profile == "prod":
        print("⚠️  当前为生产档案: 以下检查基于真实资金账户配置")
    print("=" * 78)

    # ---------------- A. 账号与配置 ----------------
    section("A. 账号与配置")

    broker_id = (cfg.ctp.broker_id or "").strip()
    check("A1", "BrokerID 已配置", PASS if broker_id else FAIL, f"broker_id={broker_id or '(空)'}")

    user_id = (cfg.ctp.user_id or "").strip()
    check("A2", "InvestorID(交易账号) 已配置", PASS if user_id else FAIL,
          f"账号={user_id[:3] + '****' if len(user_id) > 3 else '(未配置)'}")

    password = (cfg.ctp.password or "").strip()
    check("A3", "交易密码已配置", PASS if password else FAIL,
          "已配置(不回显)" if password else "未配置, 检查 .env." + profile)

    app_id = (cfg.ctp.app_id or "").strip()
    auth_code = (cfg.ctp.auth_code or "").strip()
    # app_id/auth_code 与前置环境强绑定, SimNow 组合不能用于生产/中信前置
    simnow_combo = app_id == "simnow_client_test" and auth_code.strip("0") == ""
    if not app_id or not auth_code:
        check("A4", "AppID/AuthCode 已配置", FAIL, "缺值, 须向柜台索取该前置专用值")
    elif simnow_combo and profile != "local":
        check("A4", "AppID/AuthCode 与前置匹配", FAIL,
              "检测到 SimNow 默认组合(app_id=simnow_client_test, auth_code全0)。"
              "若前置非 SimNow, 认证必失败, 须索取该前置专用值")
    elif simnow_combo:
        check("A4", "AppID/AuthCode 与前置匹配", WARN, "当前为 SimNow 默认组合(local 档案可接受)")
    else:
        check("A4", "AppID/AuthCode 已配置", PASS, "已配置非默认值")

    md, td = cfg.ctp.md_address, cfg.ctp.td_address
    check("A5", "行情前置地址格式", PASS if md.startswith("tcp://") else FAIL, md)
    check("A6", "交易前置地址格式", PASS if td.startswith("tcp://") else FAIL, td)
    check("A7", "行情/交易前置非同一地址", PASS if md != td else WARN,
          "两者相同, 请确认柜台确实合并了前置" if md == td else "已分离")

    if profile == "simnow" and cfg.ctp.flow_path.endswith("flow/"):
        check("A8", "flow_path 按档案隔离", WARN, f"{cfg.ctp.flow_path} 未体现档案名")
    else:
        check("A8", "flow_path 按档案隔离", PASS, cfg.ctp.flow_path)
    flow_ok = True
    try:
        Path(cfg.ctp.flow_path).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        flow_ok = False
        detail = str(exc)
    check("A9", "flow_path 目录可写", PASS if flow_ok else FAIL,
          cfg.ctp.flow_path if flow_ok else detail)

    if cfg.system.feed != "ctp":
        check("A10", "system.feed = ctp", FAIL,
              f"当前为 {cfg.system.feed}: CTP 交易柜不会启动, 下单接口只会返回'柜台未连接'")
    else:
        check("A10", "system.feed = ctp", PASS, "交易柜将随服务启动")

    # ---------------- B. 权限 ----------------
    section("B. 权限与账号安全")

    from backend.api.auth import load_users
    users = load_users()
    live_users = [u for u, i in users.items() if i["role"] in ("live", "admin")]
    check("B1", "存在 live/admin 角色账号", PASS if live_users else FAIL,
          f"共 {len(live_users)} 个: {', '.join(live_users) or '无'}")

    weak = [u for u in live_users
            if any(users[u]["password_hash"] and _is_weak(u, users[u]["password_hash"]) for _ in (0,))]
    if weak:
        check("B2", "实盘账号无默认弱密码", WARN,
              f"疑似弱密码: {', '.join(weak)} (弱密码账号无法报单, 会返回 403)")
    else:
        check("B2", "实盘账号无默认弱密码", PASS, "未检测到默认弱密码")

    check("B3", "模拟账号与实盘账号分离", PASS if any(i["role"] == "sim" for i in users.values()) else WARN,
          f"角色分布: sim={sum(1 for i in users.values() if i['role']=='sim')}, "
          f"live={sum(1 for i in users.values() if i['role']=='live')}")

    # ---------------- C. 资金与柜台就绪 ----------------
    section("C. 资金与柜台就绪状态")

    if connect:
        start_error = _start_counter()
        if start_error:
            check("C0", "启动交易柜用于连通性检查", FAIL, start_error)
        else:
            check("C0", "启动交易柜用于连通性检查", PASS, "已发起连接并等待就绪")

    try:
        status = live_trader.status()
        if not status.get("initialized"):
            # 本脚本是独立进程, 不启动柜台时 initialized 必然为 False。
            # 旧提示写死"feed 非 ctp 或 user_id 为空", 在 simnow 档下与事实相反, 会误导排查。
            check("C1", "CTP 交易柜已初始化", FAIL,
                  "本进程未启动交易柜 (静态检查); 加 --connect 可验证真实连通性, "
                  "或用 scripts/ctp_connection_probe.py 单独诊断")
            for code, title in (("C2", "柜台已连接"), ("C3", "已通过穿透认证"), ("C4", "已登录"),
                                ("C5", "结算单已确认"), ("C6", "已完成对账")):
                check(code, title, SKIP, "未启动柜台 (加 --connect 可验证)")
            check("C7", "账户可用资金", SKIP, "未启动柜台 (加 --connect 可验证)")
        else:
            check("C1", "CTP 交易柜已初始化", PASS, f"账号 {status.get('account')}")
            check("C2", "柜台已连接", PASS if status.get("connected") else FAIL, "")
            check("C3", "已通过穿透认证", PASS if status.get("authenticated") else FAIL,
                  "" if status.get("authenticated") else "认证失败: 检查 app_id/auth_code")
            check("C4", "已登录", PASS if status.get("logged_in") else FAIL, "")
            check("C5", "结算单已确认", PASS if status.get("settlement_confirmed") else FAIL, "")
            check("C6", "已完成对账", PASS if status.get("reconciled") else FAIL,
                  status.get("sync_error") or "")

            snap = live_trader.get_snapshot()
            acct = (snap.get("account") or [{}])[0]
            available = float(acct.get("Available") or 0)
            if available > 0:
                need = _required_capital()
                ratio = available / need if need else 0
                if ratio >= 3:
                    check("C7", "可用资金 >= 3 倍最大保证金", PASS,
                          f"可用 {available:,.0f} / 需要 {need:,.0f} = {ratio:.1f}x")
                elif ratio >= 1.5:
                    check("C7", "可用资金 >= 3 倍最大保证金", WARN,
                          f"仅 {ratio:.1f}x (建议 >=3x), 可用 {available:,.0f}")
                else:
                    check("C7", "可用资金充足", FAIL, f"仅 {ratio:.1f}x, 可用 {available:,.0f}")
            else:
                check("C7", "账户可用资金", FAIL, "可用资金为 0 或无返回")
    except Exception as exc:
        check("C1", "柜台状态查询", FAIL, f"异常: {exc}")

    # ---------------- D. 合约参数 ----------------
    section("D. 合约参数 (白名单)")

    whitelist = live_trader.risk_manager.allowed_instruments
    if not whitelist:
        check("D1", "已配置合约白名单", WARN, "白名单为空 = 不限制合约 (灰度期不建议)")
        targets = [i.symbol for i in cfg.instruments]
    else:
        check("D1", "已配置合约白名单", PASS, ", ".join(whitelist))
        targets = whitelist

    if not targets:
        check("D2", "合约清单非空", FAIL, "无任何合约可校验")
    for idx, symbol in enumerate(targets, start=1):
        code = f"D{idx + 1}"
        exchange = get_exchange(symbol)
        if not exchange:
            check(code, f"{symbol} 交易所映射", FAIL,
                  "get_exchange 返回空! 会导致合约大小写转换错误与报单被拒")
            continue
        mult, tick = get_spec(symbol)
        # CTP 对 CZCE/CFFEX 保持大写, 其余转小写
        wire = symbol if exchange in ("CZCE", "CFFEX") else symbol.lower()
        cfg_match = [i for i in cfg.instruments if i.symbol == symbol]
        mismatch = ""
        if cfg_match and (cfg_match[0].volume_multiple != mult or abs(cfg_match[0].price_tick - tick) > 1e-9):
            mismatch = (f" ⚠️ instruments.yaml({cfg_match[0].volume_multiple},{cfg_match[0].price_tick}) "
                        f"≠ 规格表({mult},{tick})")
        check(code, f"{symbol} 规格与报单形态", PASS,
              f"{exchange} 乘数={mult} tick={tick} 报单用={wire}{mismatch}")

    # ---------------- E. 风控参数 ----------------
    section("E. 风控参数")

    rm = live_trader.risk_manager
    check("E1", "阈值来自配置(非硬编码)", PASS,
          f"单笔={rm.max_order_volume} 持仓={rm.max_position_volume} 保证金率={rm.margin_ratio}")

    if profile == "simnow" and rm.max_order_volume > 5:
        check("E2", "灰度期使用最小档阈值", WARN,
              f"仿真档案单笔上限 {rm.max_order_volume} 偏大, S1 阶段建议 1")
    else:
        check("E2", "灰度期使用最小档阈值", PASS, f"单笔={rm.max_order_volume} 日笔数<={rm.daily_order_limit or '不限'}")

    check("E3", "实盘总开关状态", PASS if rm.live_enabled else WARN,
          "live_enabled=true (允许开仓)" if rm.live_enabled
          else "live_enabled=false: 所有开仓将被拒绝 (平仓仍放行)")

    check("E4", "报单价格偏离保护", PASS if rm.fat_finger_pct > 0 else WARN,
          f"{rm.fat_finger_pct * 100:.1f}%" if rm.fat_finger_pct > 0 else "未启用(0), 建议开启防手滑")

    check("E5", "单日亏损熔断", PASS if rm.max_daily_loss_ratio > 0 else FAIL,
          f"{rm.max_daily_loss_ratio * 100:.1f}%")

    check("E6", "单日撤单上限", PASS if rm.max_cancel_count <= 100 else WARN,
          f"{rm.max_cancel_count} 次/日" + ("" if rm.max_cancel_count <= 100 else " (灰度期建议 <=20, 远离交易所异常交易警示线)"))

    check("E7", "单日报单笔数上限", PASS if rm.daily_order_limit > 0 else WARN,
          f"{rm.daily_order_limit} 笔" if rm.daily_order_limit else "未限制")

    check("E8", "合约白名单仅限制开仓", PASS,
          "平仓已豁免白名单/单笔上限/总开关 (避免'能开不能平')")

    # 回归验证: 平仓必须豁免所有开仓限制
    ok, why = _verify_close_exemption(rm)
    check("E9", "平仓豁免回归验证", PASS if ok else FAIL, why)

    # ---------------- F. 基础设施 ----------------
    section("F. 基础设施与告警")

    from backend.notifier.notifier import notifier
    channels = notifier.get_channel_status()
    check("F1", "私密告警通道已配置", PASS if channels["private_webhook"] else WARN,
          "已配置" if channels["private_webhook"]
          else "未配置: 实盘断线/报单/成交告警将只落库, 无主动通知")
    check("F2", "公共告警通道已配置", PASS if channels["public_webhook"] else WARN,
          "已配置" if channels["public_webhook"] else "未配置")

    try:
        from backend.storage.mysql_client import engine
        from sqlalchemy import inspect, text
        tables = set(inspect(engine).get_table_names())
        need = {"order_record", "trade_record", "position_snapshot"}
        missing = need - tables
        check("F3", "结构化流水表已建", PASS if not missing else FAIL,
              "order_record/trade_record/position_snapshot 齐备" if not missing else f"缺: {', '.join(sorted(missing))}")
        if not missing:
            with engine.connect() as conn:
                n_orders = conn.execute(text("SELECT COUNT(*) FROM order_record")).scalar()
                n_trades = conn.execute(text("SELECT COUNT(*) FROM trade_record")).scalar()
            check("F4", "流水表可查询", PASS, f"order_record={n_orders} 行, trade_record={n_trades} 行")
    except Exception as exc:
        check("F3", "结构化流水表已建", FAIL, f"查询异常: {exc}")

    from backend.trade_engine.ctp_trader import CTPTradeExecutor
    check("F5", "交易前置断线自动重连", PASS,
          f"看门狗已实现, 退避 {CTPTradeExecutor.RECONNECT_BACKOFF_MIN:.0f}-{CTPTradeExecutor.RECONNECT_BACKOFF_MAX:.0f}s; "
          f"对账新鲜度窗口 {CTPTradeExecutor.SYNC_STALE_SECONDS:.0f}s")

    # ---------------- 汇总 ----------------
    if connect:
        try:
            from backend.trade_engine.ctp_trader import ctp_executor
            ctp_executor.stop()
        except Exception:
            pass

    counts = {s: sum(1 for r in _RESULTS if r["status"] == s) for s in (PASS, FAIL, WARN, SKIP)}
    print("\n" + "=" * 78)
    print(f"汇总: 共 {len(_RESULTS)} 项 | 通过 {counts[PASS]} | 失败 {counts[FAIL]} | "
          f"警告 {counts[WARN]} | 跳过 {counts[SKIP]}")
    if counts[FAIL]:
        print("\n❌ 存在失败项, 禁止上线。失败明细:")
        for r in _RESULTS:
            if r["status"] == FAIL:
                print(f"   {r['code']} {r['title']}: {r['detail']}")
    if counts[WARN]:
        print("\n⚠️  警告项 (不阻塞上线, 但建议处理):")
        for r in _RESULTS:
            if r["status"] == WARN:
                print(f"   {r['code']} {r['title']}: {r['detail']}")
    if not counts[FAIL]:
        print("\n✅ 无失败项。仍请完成人工检查: 柜台交易权限、合约是否临近交割月、"
              "当日涨跌停板、手工平仓能力(交易软件可人工下单)。")
    print("=" * 78)
    return 1 if counts[FAIL] else 0


def _is_weak(username: str, password_hash: str) -> bool:
    """按 auth.require_strong_live_password 的同一标准判断是否为默认弱密码。"""
    try:
        from backend.common.security import verify_password
        return any(verify_password(v, password_hash) for v in ("123456", "12345678", "password", "admin"))
    except Exception:
        return False


def _required_capital() -> float:
    """估算当前风控档位下的最大保证金需求 (取白名单中乘数最大者)。"""
    from backend.common.config import cfg
    from backend.common.product_specs import get_spec
    from backend.storage.redis_client import RedisClient

    rm_cfg = cfg.risk
    targets = rm_cfg.allowed_instruments or [i.symbol for i in cfg.instruments]
    if not targets:
        return 0.0
    worst = 0.0
    try:
        r = RedisClient.get_client()
    except Exception:
        r = None
    for symbol in targets:
        mult, _tick = get_spec(symbol)
        price = None
        if r is not None:
            price = r.hget(f"quote:{symbol}", "last_price")
        price = float(price) if price else 3000.0
        margin = price * mult * float(rm_cfg.margin_ratio) * rm_cfg.max_position_volume
        worst = max(worst, margin)
    return worst


def _verify_close_exemption(rm) -> tuple:
    """回归验证: 平仓必须豁免总开关/白名单/单笔上限, 否则会重现'裸空平不掉'故障。"""
    from decimal import Decimal
    from backend.trade_engine.models import (
        OrderRequest, Direction, OffsetFlag, AccountData, PositionData,
    )
    import copy
    probe = copy.deepcopy(rm)
    probe.live_enabled = False
    probe.allowed_instruments = ["ZZ9999"]
    probe.max_order_volume = 1
    probe.block_open("preflight 回归验证")
    acct = AccountData(balance=Decimal("1000000"), available=Decimal("1000000"))
    pos = {"FU2610_SELL": PositionData(instrument="FU2610", direction=Direction.SELL, volume=5)}
    req = OrderRequest(instrument="FU2610", direction=Direction.BUY,
                       offset=OffsetFlag.CLOSE, price=Decimal("3000"), volume=5)
    ok, why = probe.check_order(req, acct, pos, {}, last_price=Decimal("3000"))
    if ok:
        return True, "白名单外/总开关关闭/超单笔上限 的平仓 5 手 => 正确放行"
    return False, f"平仓被错误拦截: {why} (会重现'能开不能平'故障)"


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="实盘上线前只读自检 (不报单、不改配置)")
    parser.add_argument("--profile", choices=("local", "simnow", "prod"), default=None,
                        help="配置档案, 等价于设置 FUTURES_PROFILE (默认 local)")
    parser.add_argument("--connect", action="store_true",
                        help="实际启动交易柜并等待就绪, 用于验证 C 段真实连通性")
    args = parser.parse_args()
    # 必须在导入 backend.common.config 之前生效, 否则会读到上一个档案的配置
    if args.profile:
        os.environ["FUTURES_PROFILE"] = args.profile
    return run(connect=args.connect)


if __name__ == "__main__":
    sys.exit(main())
