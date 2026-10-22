from backend.signal_engine.signal_engine import SignalEngine
import asyncio
from contextlib import suppress
import logging
import threading
from backend.api.routes import app
from backend.api.tools_api import router as tools_router
import backend.websocket.routes
from backend.websocket.manager import ws_manager
from backend.common.config import cfg
from backend.storage.mysql_client import init_db
from backend.storage.instrument_sync import sync_instruments_to_db
from backend.storage.db_writer import db_writer
from backend.storage.universe_sync import UniverseSync
from backend.storage import history_backfill
from backend.bar_engine.bar_engine import MultiPeriodBarEngine
from backend.collector.live_simulator import LiveMarketSimulator
from backend.collector.sina_collector import SinaRealtimeCollector, get_watchlist
from backend.notifier.notifier import notifier

logging.basicConfig(
    level=getattr(logging, cfg.system.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("citic_futures")

# 启动横幅: 把"当前连的是哪个环境"钉在日志最显眼处, 避免误操作
_PROFILE_BANNER = {
    "local": "[本地开发] 行情源 sina, 实盘通道默认关闭",
    "simnow": "[仿真环境] 仿真柜台, 报单不产生真实资金影响",
    "prod": "⚠️ [生产环境] 真实资金账户, 报单将真实成交 ⚠️",
}
logger.info("=" * 72)
logger.info("配置档案 PROFILE = %s  %s", cfg.system.profile.upper(),
            _PROFILE_BANNER.get(cfg.system.profile, ""))
logger.info("行情源: %s | 实盘开关 live_enabled=%s", cfg.system.feed, cfg.risk.live_enabled)
logger.info("=" * 72)


def _setup_alert_webhooks():
    """接线告警 Webhook。

    背景: set_webhook_url 此前**没有任何调用点**, _webhook_url 永远为 None,
    导致告警 Webhook 通道实际不可用、实盘异常无任何主动通知。
    """
    public_url = (cfg.system.alert_webhook_url or "").strip()
    private_url = (cfg.system.alert_private_webhook_url or "").strip()
    if public_url:
        notifier.set_webhook_url(public_url)
        logger.info("公共告警 Webhook 已配置: %s...", public_url[:36])
    else:
        logger.info("未配置公共告警 Webhook (system.alert_webhook_url), 非交易类告警仅落库与页面广播")
    if private_url:
        notifier.set_private_webhook_url(private_url)
        logger.info("私密告警 Webhook 已配置: %s...", private_url[:36])
    else:
        logger.warning(
            "未配置私密告警 Webhook (system.alert_private_webhook_url): "
            "实盘成交/报单/断线等交易类告警将**只落库**, 不会有任何主动通知"
        )


_setup_alert_webhooks()

app.include_router(tools_router)
# 页面路由 (/ 与 /paper /live /login) 已统一在 backend.api.routes 中定义

bar_engine: MultiPeriodBarEngine = None
ctp_bar_engine: MultiPeriodBarEngine = None
simulator: LiveMarketSimulator = None
ctp_collector = None
ctp_executor = None
sina_collector: SinaRealtimeCollector = None
universe_sync: UniverseSync = None
session_task = None
gap_repair_task = None
settlement_task = None
verification_task = None


async def verification_loop():
    """用户已启动的独立账本：扫描报价及结果驱动的分析/超时/重试状态。"""
    from backend.ai_analysis.paper_verification import tick
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception:
            logger.exception('AI 模拟验证扫描异常')
        await asyncio.sleep(2)

async def settlement_loop():
    """AI 分析自动结算: 周期扫描到期运行, 用实际行情自动评分 (人工评分优先)。"""
    from backend.ai_analysis.settlement import settle_due_runs
    while True:
        try:
            await asyncio.to_thread(settle_due_runs)
        except Exception:
            logger.exception("AI 分析自动结算循环异常")
            from backend.ai_analysis import alerts
            alerts.notify_throttled('settlement_loop', 'AI_SETTLEMENT_ERROR',
                                    'AI 自动结算循环异常', 'ERROR', 'ai_settlement')
        await asyncio.sleep(300)

async def close_market_sessions():
    while True:
        for engine_instance in (bar_engine, ctp_bar_engine):
            if engine_instance:
                try:
                    await asyncio.to_thread(engine_instance.close_sessions)
                except Exception:
                    logger.exception("%s 收盘检查失败", engine_instance.feed)
        await asyncio.sleep(1)

async def repair_market_gaps():
    while True:
        pending = set(bar_engine.redis.smembers("history:rebuild:symbols"))
        for symbol in sorted(pending):
            try:
                result = await asyncio.to_thread(history_backfill.backfill_symbol, symbol)
                errors = [p for p, item in result["periods"].items() if "error" in item]
                if not errors:
                    bar_engine.redis.srem("history:rebuild:symbols", symbol)
            except Exception:
                logger.exception("%s 全周期重建失败，下轮重试", symbol)
        for symbol in get_watchlist():
            if symbol in pending:
                continue
            try:
                await asyncio.to_thread(history_backfill.repair_minute_gaps, symbol)
            except Exception:
                logger.exception("%s 分钟缺口回补失败，下轮重试", symbol)
        await asyncio.sleep(300)

@app.on_event("startup")
async def on_startup():
    global bar_engine, ctp_bar_engine, simulator, ctp_collector, ctp_executor, sina_collector, universe_sync, session_task, gap_repair_task, settlement_task, verification_task
    logger.info("Initializing MySQL database schema...")
    init_db()
    from backend.ai_analysis.orchestrator import mark_interrupted
    mark_interrupted()
    from backend.ai_analysis import research_scheduler
    research_scheduler.start()
    sync_instruments_to_db()
    from backend.trade_engine.paper_accounts import paper_accounts
    paper_accounts.restore_all()
    db_writer.start()

    # 绑定运行 loop 到 ws_manager
    loop = asyncio.get_running_loop()
    ws_manager.set_loop(loop)

    # 启动每日 Tick 数据留存清理调度 (默认 02:30)
    from backend.storage.retention import retention_manager
    retention_manager.start_scheduler(loop)

    # 启动信号评估器与多周期 K 线引擎 (含秒级热重启预热)
    signal_engine = SignalEngine()

    def handle_higher_bar_close(bar):
        if bar.period == "15m":
            bars_15m = bar_engine.history_bars.get(bar.instrument, {}).get("15m", [])
            bars_60m = bar_engine.history_bars.get(bar.instrument, {}).get("60m", [])
            signal_engine.evaluate_15m_signals(bar.instrument, bars_15m, bars_60m)

    # 模拟用户始终使用 Sina；只有明确启用 ctp 档案时才创建并启动 CTP 链路。
    bar_engine = MultiPeriodBarEngine(on_higher_bar_close=handle_higher_bar_close,
                                      feed="sina", persist_db=True, drive_paper=True)
    ctp_enabled = (cfg.system.feed or "").strip().lower() == "ctp"
    ctp_bar_engine = (MultiPeriodBarEngine(feed="ctp", persist_db=False, drive_paper=False)
                      if ctp_enabled else None)

    session_task = asyncio.create_task(close_market_sessions())
    settlement_task = asyncio.create_task(settlement_loop())
    verification_task = asyncio.create_task(verification_loop())

    logger.info("模拟用户行情源=新浪，启动全市场合约同步、实时采集与历史回补...")
    universe_sync = UniverseSync()
    universe_sync.start()
    sina_collector = SinaRealtimeCollector(on_tick_callback=bar_engine.on_tick)
    sina_collector.start()
    gap_repair_task = asyncio.create_task(repair_market_gaps())

    def _backfill_task():
        try:
            pending_rebuild = set(bar_engine.redis.smembers("history:rebuild:symbols"))
            for sym in get_watchlist():
                if sym in pending_rebuild:
                    continue
                try:
                    history_backfill.ensure_symbol_backfilled(sym)
                except Exception:
                    logger.exception("%s 启动历史回填失败，继续处理其他合约", sym)
        except Exception as e:
            logger.error(f"启动历史回填失败: {e}")
    threading.Thread(target=_backfill_task, name="StartupBackfill", daemon=True).start()

    has_ctp_account = bool((cfg.ctp.user_id or "").strip())
    if ctp_enabled and has_ctp_account:
        from backend.collector.ctp_collector import CTPMarketCollector
        from backend.trade_engine.ctp_trader import ctp_executor as configured_ctp_executor

        ctp_executor = configured_ctp_executor
        logger.info("正式用户行情源=CTP，启动独立 CTP 行情采集...")
        ctp_collector = CTPMarketCollector(on_tick_callback=ctp_bar_engine.on_tick)
        ctp_collector.start()
        notifier.notify("FEED_CTP_STARTED", "正式用户 CTP 行情源已接入", "INFO", "main")
        try:
            ctp_executor.start()
        except Exception as e:
            logger.error(f"CTP Trader startup error: {e}")
    elif ctp_enabled:
        logger.warning("未配置 CTP 用户，正式用户 CTP 行情与交易柜台未启动。")
    else:
        bar_engine.redis.hset("feed:status:ctp", mapping={
            "running": "0", "connected": "0", "logged_in": "0",
            "last_error": "CTP 已停用：当前仅开放模拟用户",
        })
        logger.info("仅模拟用户模式：CTP 行情与交易柜台均不启动。")

    _log_effective_risk_config()
    feed_summary = "模拟=Sina, 正式=CTP" if ctp_enabled else "仅模拟用户, 行情=Sina"
    notifier.notify(
        "SYSTEM_STARTUP",
        f"期货交易系统启动就绪 (档案: {cfg.system.profile}, {feed_summary})",
        "INFO", "main",
    )
    logger.info(f"Citic Futures System ready: profile={cfg.system.profile}, feeds={feed_summary}, "
                f"Web UI, APIs, WebSocket, and Market Engine active.")


def _log_effective_risk_config():
    """启动时打印实际生效的风控档位, 灰度放量时用于核对是否真的切换成功。"""
    if (cfg.system.feed or "").strip().lower() == "ctp":
        from backend.trade_engine.live_trader import live_trader
        rm = live_trader.risk_manager
    else:
        rm = cfg.risk
    logger.info(
        "生效风控档位: live_enabled=%s | 单笔<=%s | 持仓<=%s | 保证金率=%s | "
        "日亏熔断=%s | 撤单<=%s | 偏离保护=%s | 日笔数<=%s | 白名单=%s",
        rm.live_enabled, rm.max_order_volume, rm.max_position_volume, rm.margin_ratio,
        rm.max_daily_loss_ratio, rm.max_cancel_count, rm.fat_finger_pct,
        rm.daily_order_limit or "不限", rm.allowed_instruments or "不限",
    )
    if cfg.system.feed == "ctp" and not rm.live_enabled:
        logger.warning(
            "行情源为 ctp 但 risk.live_enabled=false: 交易柜会连接, "
            "但所有开仓报单将被拒绝 (平仓仍放行)。如需交易请显式开启该开关。"
        )

@app.on_event("shutdown")
async def on_shutdown():
    global simulator, ctp_collector, sina_collector, universe_sync
    from backend.ai_analysis import research_scheduler
    research_scheduler.stop()
    if simulator:
        simulator.stop()
    if ctp_collector:
        ctp_collector.stop()
    if sina_collector:
        sina_collector.stop()
    if universe_sync:
        universe_sync.stop()
    if gap_repair_task:
        gap_repair_task.cancel()
        with suppress(asyncio.CancelledError):
            await gap_repair_task
    if session_task:
        session_task.cancel()
        with suppress(asyncio.CancelledError):
            await session_task
    if settlement_task:
        settlement_task.cancel()
        with suppress(asyncio.CancelledError):
            await settlement_task
    if verification_task:
        verification_task.cancel()
        with suppress(asyncio.CancelledError):
            await verification_task
    if bar_engine:
        bar_engine.close_sessions()
    from backend.storage.retention import retention_manager
    retention_manager.stop_scheduler()
    if ctp_executor:
        ctp_executor.stop()
    db_writer.stop()
    if bar_engine:
        bar_engine.redis.hset("feed:status:sina", "running", "0")
        bar_engine.redis.hset("feed:status:ctp", "running", "0")
    notifier.notify("SYSTEM_SHUTDOWN", "期货实时分析系统关闭", "INFO", "main")
