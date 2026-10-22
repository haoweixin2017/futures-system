import os
import secrets
import logging
from decimal import Decimal
from pathlib import Path
import yaml
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = ROOT_DIR / "config"

# 环境变量名 -> (配置段, 字段)。用于把敏感值从 YAML 中解耦，避免明文入库。
_ENV_OVERRIDES = {
    "FUTURES_CTP_USER_ID": ("ctp", "user_id"),
    "FUTURES_CTP_PASSWORD": ("ctp", "password"),
    "FUTURES_CTP_APP_ID": ("ctp", "app_id"),
    "FUTURES_CTP_AUTH_CODE": ("ctp", "auth_code"),
    "FUTURES_CTP_BROKER_ID": ("ctp", "broker_id"),
    "FUTURES_CTP_MD_ADDRESS": ("ctp", "md_address"),
    "FUTURES_CTP_TD_ADDRESS": ("ctp", "td_address"),
    "FUTURES_MYSQL_HOST": ("mysql", "host"),
    "FUTURES_MYSQL_PORT": ("mysql", "port"),
    "FUTURES_MYSQL_USER": ("mysql", "user"),
    "FUTURES_MYSQL_PASSWORD": ("mysql", "password"),
    "FUTURES_MYSQL_DATABASE": ("mysql", "database"),
    "FUTURES_REDIS_HOST": ("redis", "host"),
    "FUTURES_REDIS_PORT": ("redis", "port"),
    "FUTURES_REDIS_PASSWORD": ("redis", "password"),
    "FUTURES_MAINTENANCE_TOKEN": ("system", "maintenance_token"),
    "FUTURES_SESSION_SECRET": ("system", "session_secret"),
    "FUTURES_CORS_ORIGINS": ("system", "cors_origins"),
}


def read_dotenv(path: Path) -> Dict[str, str]:
    """解析基础 KEY=VALUE 格式，不污染进程环境，支持 UTF-8 BOM。"""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                values[key] = value
    except Exception as exc:
        logger.warning("读取 .env 失败 (%s): %s", path, exc)
    return values


def load_dotenv(path: Path) -> None:
    """兼容入口：填充未配置的进程环境变量；load_config 使用独立合并字典。"""
    for key, value in read_dotenv(path).items():
        os.environ.setdefault(key, value)


def apply_env_overrides(data: Dict[str, Any], environ=None) -> Dict[str, Any]:
    """叠加已合并的环境配置；显式清空 CTP 字段不能回退到旧账号或默认授权。"""
    environ = os.environ if environ is None else environ
    for env_name, (section, field_name) in _ENV_OVERRIDES.items():
        raw = environ.get(env_name)
        if raw is None or (not raw.strip() and section != "ctp"):
            continue
        value: Any = raw.strip()
        if field_name in ("port", "db", "pool_size", "pool_recycle"):
            try:
                value = int(value)
            except ValueError:
                continue
        data.setdefault(section, {})
        if isinstance(data[section], dict):
            data[section][field_name] = value
        else:
            data[section] = {field_name: value}
    return data

class InstrumentConfig(BaseModel):
    symbol: str
    exchange: str
    name: str
    price_tick: float = 1.0
    volume_multiple: int = 10

class CTPConfig(BaseModel):
    md_address: str = "tcp://180.168.146.187:10131"
    td_address: str = "tcp://180.168.146.187:10130"
    broker_id: str = "9999"
    user_id: str = ""
    password: str = ""
    app_id: str = "simnow_client_test"
    auth_code: str = "0000000000000000"
    flow_path: str = "./ctp_flow/"

class RedisConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    password: Optional[str] = ""

class MySQLConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "citic_futures"
    pool_size: int = 10
    pool_recycle: int = 3600

class RiskConfig(BaseModel):
    """事前硬风控阈值 (外置配置)。

    灰度放量的本质就是逐步抬高这些阈值, 因此必须可配置, 不能硬编码。
    保留与 PreTradeRiskManager 构造函数一致的字段名与默认值。
    """
    # 单笔最大报单手数 (只约束开仓, 平仓豁免)
    max_order_volume: int = 50
    # 单合约最大持仓手数 (仅开仓方向校验)
    max_position_volume: int = 200
    # 保证金率: 用于资金充足性预估 (可略高于交易所实际值留安全垫)
    margin_ratio: Decimal = Decimal("0.12")
    # 单日最大撤单次数 (交易所"频繁报撤单"异常交易警示线)
    max_cancel_count: int = 400
    # 单日累计亏损熔断比例
    max_daily_loss_ratio: Decimal = Decimal("0.05")
    # 报单价格偏离最新价保护 (防手滑/防程序 bug 报出天价); 0 表示关闭
    fat_finger_pct: Decimal = Decimal("0.02")
    # 合约白名单: 空列表=不限制。仅限制开仓, 平仓一律豁免 (否则会出现"能开不能平")
    allowed_instruments: List[str] = Field(default_factory=list)
    # 单日总报单笔数上限; 0 表示不限制
    daily_order_limit: int = 0
    # 实盘总开关: false 时拒绝所有开仓 (平仓仍放行, 保证有持仓时能退出)
    live_enabled: bool = False


class SystemConfig(BaseModel):
    log_level: str = "INFO"
    periods: List[str] = ["1m", "5m", "15m", "30m", "60m"]
    save_raw_tick: bool = False
    tick_retention_days: int = 7
    # 维护接口令牌: 默认空值, 空值时维护类接口一律拒绝 (fail-closed)。
    # 通过环境变量 FUTURES_MAINTENANCE_TOKEN 或 settings.yaml 显式配置后才能使用。
    maintenance_token: str = ""
    # 会话 HMAC 签名密钥: 留空则自动生成随机密钥 (重启后旧会话失效)。
    session_secret: str = ""
    # 允许的跨域来源, 逗号分隔。默认仅本机, 禁止使用 "*" 携带凭据。
    cors_origins: str = "http://127.0.0.1:8000,http://localhost:8000"
    # 行情源: sina=新浪免费实时(默认) / simulator=内置模拟 / ctp=中信CTP
    feed: str = "sina"
    poll_interval: float = 0.8
    universe_refresh_seconds: int = 300
    backfill_on_subscribe: bool = True
    big_order_min_volume: int = 100  # 大单监控阈值 (现手手数)
    # ---- 模拟盘撮合参数 (高仿真的关键: 不加这些, 盈亏会被系统性高估) ----
    paper_fill_uses_opponent: bool = True   # 成交价考虑对手价(bid/ask), 而非只看最新价
    paper_slippage_ticks: float = 0.0       # 额外滑点(以最小变动价位为单位)
    paper_commission_per_lot: float = 0.0   # 每手手续费(元), 0 表示不收取
    # ---- 告警通道 ----
    # 公共告警 Webhook (企业微信/钉钉/飞书机器人), 空值表示关闭
    alert_webhook_url: str = ""
    # 私密告警 Webhook: 实盘成交/报单等账户类事件只能走这里, 绝不能进公共行情通道
    alert_private_webhook_url: str = ""
    # 有效配置档案名 (local / simnow / prod), 用于启动日志与页面环境标识
    profile: str = "local"

class AppConfig(BaseModel):
    ctp: CTPConfig = Field(default_factory=CTPConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    mysql: MySQLConfig = Field(default_factory=MySQLConfig)
    system: SystemConfig = Field(default_factory=SystemConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    instruments: List[InstrumentConfig] = Field(default_factory=list)

PROFILE_ENV = "FUTURES_PROFILE"
VALID_PROFILES = ("local", "simnow", "prod")


def resolve_profile(environ=None) -> str:
    """解析当前配置档案。非法值直接 fail-fast, 避免"以为在仿真、其实连了生产"。"""
    environ = os.environ if environ is None else environ
    raw = (environ.get(PROFILE_ENV) or "local").strip().lower()
    if raw not in VALID_PROFILES:
        raise RuntimeError(
            f"{PROFILE_ENV}='{raw}' 不是合法档案名, 可选: {', '.join(VALID_PROFILES)}"
        )
    return raw


def resolve_settings_file(profile: str) -> Path:
    """按档案选择配置文件, 缺失时回退到 settings.yaml (再回退示例)。"""
    if profile != "local":
        candidate = CONFIG_DIR / f"settings.{profile}.yaml"
        if candidate.exists():
            return candidate
        logger.warning(
            "档案 %s 的配置文件 %s 不存在, 已回退到 settings.yaml", profile, candidate.name
        )
    primary = CONFIG_DIR / "settings.yaml"
    if primary.exists():
        return primary
    return CONFIG_DIR / "settings.example.yaml"


def _config_sources(environ=None) -> Dict[str, str]:
    """记录关键字段的值来源 (env / yaml), 不记录敏感字段值。"""
    environ = os.environ if environ is None else environ
    sources: Dict[str, str] = {}
    for env_name, (section, field_name) in _ENV_OVERRIDES.items():
        raw = environ.get(env_name)
        sources[f"{section}.{field_name}"] = "env" if (
            raw is not None and (raw.strip() or section == "ctp")
        ) else "yaml"
    return sources


def load_config() -> AppConfig:
    # 优先级: 真实进程环境 > .env.<profile> > .env > YAML。
    # 不把 .env 写回 os.environ，否则重复加载/切换档案会把旧凭据误当成外部覆盖。
    base_env = read_dotenv(ROOT_DIR / ".env")
    profile = resolve_profile({**base_env, **os.environ})
    merged_env = {**base_env, **read_dotenv(ROOT_DIR / f".env.{profile}"), **os.environ}

    settings_file = resolve_settings_file(profile)

    data: Dict[str, Any] = {}
    if settings_file.exists():
        with open(settings_file, "r", encoding="utf-8") as f:
            content = yaml.safe_load(f) or {}
            data.update(content)

    instruments_file = CONFIG_DIR / "instruments.yaml"
    if instruments_file.exists():
        with open(instruments_file, "r", encoding="utf-8") as f:
            inst_data = yaml.safe_load(f) or {}
            data["instruments"] = inst_data.get("instruments", [])

    data = apply_env_overrides(data, merged_env)

    # 档案名注入 system 段, 供启动日志与页面环境标识使用
    data.setdefault("system", {})
    if isinstance(data["system"], dict):
        data["system"]["profile"] = profile

    config = AppConfig(**data)
    config.system.profile = profile

    # 启动时把关键事实钉在日志里, 避免"以为在仿真其实连了生产"
    logger.info("配置档案: %s (配置文件: %s)", profile, settings_file.name)
    logger.info(
        "运行环境: profile=%s feed=%s broker_id=%s investor=%s md=%s td=%s",
        profile,
        config.system.feed,
        config.ctp.broker_id,
        (config.ctp.user_id[:3] + "****") if len(config.ctp.user_id) > 3 else "(未配置)",
        config.ctp.md_address,
        config.ctp.td_address,
    )
    _sources = _config_sources(merged_env)
    for key in ("ctp.user_id", "ctp.broker_id", "ctp.td_address"):
        logger.info("  配置来源 %s <- %s", key, _sources.get(key, "yaml"))
    if profile == "prod" and config.risk.live_enabled:
        logger.warning("⚠️ 生产档案且实盘开关已打开, 报单将真实成交于真实资金账户!")
    return config


def resolve_cors_origins() -> List[str]:
    """解析允许的跨域来源。拒绝 '*' (与 allow_credentials 冲突且不安全)。"""
    raw = (cfg.system.cors_origins or "").strip()
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if not origins or "*" in origins:
        logger.warning("cors_origins 为空或含 '*', 已回退为仅本机地址")
        return ["http://127.0.0.1:8000", "http://localhost:8000"]
    return origins


def get_session_secret() -> str:
    """获取会话签名密钥; 未配置时生成随机密钥 (不落盘, 重启后旧会话失效)。"""
    configured = (cfg.system.session_secret or "").strip()
    if configured:
        return configured.encode("utf-8")
    key_file = CONFIG_DIR / "session_secret.key"
    if key_file.exists():
        content = key_file.read_text(encoding="utf-8").strip()
        if content:
            return content.encode("utf-8")
    generated = secrets.token_hex(32)
    logger.warning(
        "未配置 system.session_secret, 已生成临时会话密钥; "
        "重启后所有登录会话将失效。生产环境请设置 FUTURES_SESSION_SECRET。"
    )
    return generated.encode("utf-8")


cfg = load_config()

