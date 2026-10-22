"""实盘交易 HTTP 接口层测试 (针对运行中的服务)。

覆盖: 鉴权与角色门禁、柜台状态查询、风控档位、账户/委托/流水查询、
      报单与撤单的失败路径、紧急停机三开关的二次确认门禁。

用法:
    .venv/Scripts/python.exe scripts/live_sim_api_test.py --base http://127.0.0.1:8000
"""
import argparse
import json
import sys
import time

import httpx

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--user", default="e2e_probe")
    ap.add_argument("--password", default="E2eProbe#2026")
    ap.add_argument("--sim-user", default="sim")
    ap.add_argument("--sim-password", default="123456")
    ap.add_argument("--admin-token", default="", help="system.maintenance_token, 用于验证紧急停机门禁")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    # trust_env=False: 本机调试直连, 不能被 HTTP_PROXY 环境变量劫持到代理上
    anon = httpx.Client(base_url=base, timeout=15.0, trust_env=False)
    sim = httpx.Client(base_url=base, timeout=15.0, trust_env=False)
    live = httpx.Client(base_url=base, timeout=15.0, trust_env=False)

    # ---- 健康检查 ----
    r = anon.get("/api/v1/health")
    body = r.json() if r.status_code == 200 else {}
    check("健康检查可用", r.status_code == 200, f"status={body.get('status')} feed={body.get('feed')}")
    check("健康检查回显 simnow 档案", body.get("profile") == "simnow", body.get("profile"))

    # ---- 未登录访问实盘接口 ----
    r = anon.get("/api/v1/trade/live/status")
    check("未登录访问实盘接口返回 401", r.status_code == 401, f"HTTP {r.status_code}")

    # ---- 模拟用户登录 ----
    r = sim.post("/api/v1/auth/login", json={"username": args.sim_user, "password": args.sim_password,
                                             "expected_role": "sim"})
    check("模拟用户登录成功", r.status_code == 200, f"HTTP {r.status_code}")

    # ---- 角色门禁: 模拟用户不得访问实盘接口 ----
    r = sim.get("/api/v1/trade/live/status")
    check("模拟用户访问实盘接口返回 403", r.status_code == 403,
          f"HTTP {r.status_code} {r.json().get('detail', '') if r.status_code != 200 else ''}")

    # ---- 入口与角色一致性: 模拟账号走实盘入口应被拒 ----
    r = anon.post("/api/v1/auth/login", json={"username": args.sim_user, "password": args.sim_password,
                                              "expected_role": "live"})
    check("模拟账号走实盘登录入口被拒", r.status_code == 403, f"HTTP {r.status_code}")

    # ---- 正式用户登录 ----
    r = live.post("/api/v1/auth/login", json={"username": args.user, "password": args.password,
                                              "expected_role": "live"})
    check("正式用户登录成功", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code != 200:
        return summarize()

    # ---- 柜台状态 ----
    r = live.get("/api/v1/trade/live/status")
    st = r.json() if r.status_code == 200 else {}
    check("实盘柜台状态可查询", r.status_code == 200, f"HTTP {r.status_code}")
    for key in ("connected", "authenticated", "logged_in", "settlement_confirmed", "reconciled"):
        check(f"柜台状态 {key}", bool(st.get(key)), st.get(key))
    check("柜台就绪 ready=true", bool(st.get("ready")), f"ready={st.get('ready')} "
          f"blocking={st.get('blocking_reason', '')}")

    # ---- 就绪状态稳定性: 周期性对账不得使 ready 反复翻转 ----
    # (历史缺陷: 对账心跳每轮清零 reconciled, ready 约一半时间不可用,
    #  导致单笔报单被随机拒绝、批量报单只有第一笔能通过)
    samples = []
    for _ in range(8):
        r = live.get("/api/v1/trade/live/status")
        samples.append(bool(r.json().get("ready")) if r.status_code == 200 else False)
        time.sleep(1.5)
    check("就绪状态在 12 秒内保持稳定", all(samples), f"samples={samples}")

    # ---- 风控档位 ----
    r = live.get("/api/v1/trade/live/risk")
    risk = r.json() if r.status_code == 200 else {}
    check("风控档位可查询", r.status_code == 200 and "max_order_volume" in risk,
          json.dumps({k: risk.get(k) for k in ("live_enabled", "max_order_volume", "allowed_instruments",
                                               "fat_finger_pct", "daily_order_limit")}, ensure_ascii=False))

    # ---- 账户 / 委托 / 流水查询 ----
    r = live.get("/api/v1/trade/live/account")
    acct = r.json() if r.status_code == 200 else {}
    check("实盘账户快照可查询", r.status_code == 200 and isinstance(acct.get("account"), list),
          f"account_rows={len(acct.get('account') or [])} positions={len(acct.get('positions') or [])}")

    r = live.get("/api/v1/trade/live/orders")
    check("实盘委托列表可查询", r.status_code == 200 and isinstance(r.json().get("orders"), list),
          f"orders={len(r.json().get('orders') or [])}")

    r = live.get("/api/v1/trade/live/ledger", params={"limit": 50})
    led = r.json() if r.status_code == 200 else {}
    check("实盘结构化流水可查询", r.status_code == 200 and "orders" in led and "trades" in led,
          f"summary={led.get('summary')}")

    # ---- 报单失败路径: 总开关关闭时开仓必须被拒 (fail-safe) ----
    r = live.post("/api/v1/trade/live/order", json={
        "instrument": "FU2610", "direction": "BUY", "offset": "OPEN", "price": 4800, "volume": 1})
    payload = r.json() if r.status_code == 200 else {}
    status = payload.get("order", {}).get("status") if isinstance(payload.get("order"), dict) else None
    reason = payload.get("order", {}).get("reject_reason", "") if isinstance(payload.get("order"), dict) else ""
    if risk.get("live_enabled"):
        check("开仓报单被受理", r.status_code == 200 and status in ("SUBMITTED", "SENDING"),
              f"status={status} reason={reason}")
    else:
        check("总开关关闭时开仓被拒", r.status_code == 200 and status == "REJECTED" and "总开关" in reason,
              f"status={status} reason={reason}")

    # ---- 非法合约代码: 模型层拦截 ----
    r = live.post("/api/v1/trade/live/order", json={
        "instrument": "XX", "direction": "BUY", "offset": "OPEN", "price": 100, "volume": 1})
    check("非法合约代码被模型拒绝 (422)", r.status_code == 422, f"HTTP {r.status_code}")

    # ---- 撤单失败路径 ----
    r = live.delete("/api/v1/trade/live/order/no_such_ref")
    check("撤不存在的委托返回 400", r.status_code == 400, f"HTTP {r.status_code}")

    # ---- 紧急停机门禁: 管理令牌 -> 确认字符串 (顺序固定, 认证优先) ----
    def detail(resp):
        try:
            return resp.json().get("detail", "")
        except Exception:
            return ""

    r = live.post("/api/v1/trade/live/close-all", json={"confirm": "CONFIRM_CLOSE_ALL"})
    if args.admin_token:
        check("一键全平缺管理令牌返回 403", r.status_code == 403, f"HTTP {r.status_code} {detail(r)}")
        hdr = {"X-Admin-Token": args.admin_token}
        r = live.post("/api/v1/trade/live/close-all", json={"confirm": ""}, headers=hdr)
        check("一键全平缺确认字符串返回 400", r.status_code == 400, f"HTTP {r.status_code} {detail(r)}")
        r = live.post("/api/v1/trade/live/close-all", json={"confirm": "WRONG"}, headers=hdr)
        check("一键全平确认字符串错误返回 400", r.status_code == 400, f"HTTP {r.status_code} {detail(r)}")
        r = live.post("/api/v1/trade/live/cancel-all", headers=hdr)
        ok = r.status_code == 200 and "attempted" in r.json()
        check("批量撤单带管理令牌可执行", ok,
              f"HTTP {r.status_code} attempted={r.json().get('attempted') if ok else detail(r)}")
    else:
        check("一键全平缺管理令牌被拒 (fail-closed)", r.status_code in (403, 503),
              f"HTTP {r.status_code} {detail(r)}")
        r = live.post("/api/v1/trade/live/cancel-all")
        check("批量撤单缺管理令牌被拒 (fail-closed)", r.status_code in (403, 503),
              f"HTTP {r.status_code} {detail(r)}")

    # ---- 软开关: 禁止开仓 / 解除 ----
    r = live.post("/api/v1/trade/live/block-open", json={"reason": "API 层测试"})
    check("禁止开仓开关生效", r.status_code == 200 and r.json().get("open_blocked") is True,
          f"HTTP {r.status_code}")
    r = live.get("/api/v1/trade/live/risk")
    check("禁止开仓状态可查询", r.json().get("open_blocked") is True, r.json().get("block_reason"))
    r = live.post("/api/v1/trade/live/unblock-open")
    check("解除禁止开仓开关", r.status_code == 200 and r.json().get("open_blocked") is False,
          f"HTTP {r.status_code}")

    return summarize()


def summarize():
    failed = [x for x in RESULTS if not x[1]]
    print("=" * 70)
    print(f"结果: 通过 {len(RESULTS) - len(failed)} / {len(RESULTS)}, 失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  FAIL {name} -> {detail}")
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
