"""用户认证与角色权限路由。

角色:
- sim   模拟用户: 仅可模拟交易
- live  正式用户: 可模拟交易 + 实盘交易
- admin 管理员: live 权限 + 系统维护

用户清单存于 config/users.yaml (密码为 PBKDF2 哈希), 由 scripts/create_user.py 管理。
登录态通过 HttpOnly Cookie 携带 HMAC 签名令牌。
"""

import logging
import threading
import time
from typing import Optional

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.common.config import CONFIG_DIR, cfg
from backend.common.security import create_session_token, read_session_token, verify_password

logger = logging.getLogger(__name__)

USERS_FILE = CONFIG_DIR / "users.yaml"
SESSION_COOKIE = "cfs_session"
VALID_ROLES = {"sim", "live", "admin"}
# 各角色允许的交易范围: sim 只允许模拟盘, live/admin 可模拟盘 + 实盘
LIVE_ROLES = {"live", "admin"}
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 300
_login_failure_lock = threading.Lock()
_login_failures = {}


def _login_failure_key(request: Request, username: str) -> tuple[str, str]:
    client_host = request.client.host if request.client else "unknown"
    return client_host, username.lower()


def _check_login_rate_limit(request: Request, username: str) -> None:
    key = _login_failure_key(request, username)
    now = time.monotonic()
    with _login_failure_lock:
        recent = [stamp for stamp in _login_failures.get(key, [])
                  if now - stamp < LOGIN_FAILURE_WINDOW_SECONDS]
        if recent:
            _login_failures[key] = recent
        else:
            _login_failures.pop(key, None)
        if len(recent) >= LOGIN_FAILURE_LIMIT:
            raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试")


def _record_login_failure(request: Request, username: str) -> None:
    key = _login_failure_key(request, username)
    now = time.monotonic()
    with _login_failure_lock:
        recent = [stamp for stamp in _login_failures.get(key, [])
                  if now - stamp < LOGIN_FAILURE_WINDOW_SECONDS]
        recent.append(now)
        _login_failures[key] = recent


def _clear_login_failures(request: Request, username: str) -> None:
    with _login_failure_lock:
        _login_failures.pop(_login_failure_key(request, username), None)


def live_mode_enabled() -> bool:
    """仅 CTP 档案开放正式用户入口和实盘接口。"""
    return (cfg.system.feed or "").strip().lower() == "ctp"


class SessionUser:
    def __init__(self, username: str, role: str, display_name: str = ""):
        self.username = username
        self.role = role
        self.display_name = display_name or username

    @property
    def can_live_trade(self) -> bool:
        return live_mode_enabled() and self.role in LIVE_ROLES

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "role": self.role,
            "display_name": self.display_name,
            "can_live_trade": self.can_live_trade,
        }


def load_users() -> dict:
    """读取用户清单: {username: {password_hash, role, display_name}}"""
    if not USERS_FILE.exists():
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    users = {}
    for item in data.get("users", []):
        name = str(item.get("username", "")).strip()
        if not name:
            continue
        users[name] = {
            "password_hash": str(item.get("password_hash", "")),
            "role": str(item.get("role", "sim")),
            "display_name": str(item.get("display_name", "")),
        }
    return users


def get_current_user(request: Request) -> Optional[SessionUser]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    session = read_session_token(token)
    if not session:
        return None
    users = load_users()
    info = users.get(session["username"])
    if not info:
        return None
    role = info["role"]
    if role not in VALID_ROLES:
        role = "sim"
    if role in LIVE_ROLES and not live_mode_enabled():
        return None
    return SessionUser(
        username=session["username"],
        role=role,
        display_name=info["display_name"] if info else "",
    )


def require_user(request: Request) -> SessionUser:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def require_live(request: Request) -> SessionUser:
    if not live_mode_enabled():
        raise HTTPException(status_code=503, detail="正式交易功能已停用，当前仅开放模拟用户")
    user = require_user(request)
    if not user.can_live_trade:
        raise HTTPException(status_code=403, detail="模拟用户无实盘交易权限")
    return user


def require_strong_live_password(user):
    info = load_users().get(user.username)
    if not info or any(verify_password(value, info["password_hash"]) for value in ("123456", "12345678", "password", "admin")):
        raise HTTPException(status_code=403, detail="请先通过本机账号管理工具更换默认弱密码，再使用实盘报单")


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login")
def login(body: dict, request: Request):
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    # 登录入口类型: sim=模拟用户登录入口, live=实盘交易用户登录入口 (可选, 用于入口与账号角色一致性校验)
    expected_role = str(body.get("expected_role", "")).strip().lower()
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入用户名和密码")
    _check_login_rate_limit(request, username)
    if expected_role == "live" and not live_mode_enabled():
        raise HTTPException(status_code=503, detail="正式交易功能已停用，当前仅开放模拟用户")

    users = load_users()
    info = users.get(username)
    if not info or not verify_password(password, info["password_hash"]):
        _record_login_failure(request, username)
        logger.warning(f"登录失败: {username}")
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    role = info["role"] if info["role"] in VALID_ROLES else "sim"

    if role in LIVE_ROLES and not live_mode_enabled():
        raise HTTPException(status_code=503, detail="正式交易功能已停用，当前仅开放模拟用户")

    # 入口与账号角色一致性校验: 模拟账号不能走实盘登录入口, 反之亦然
    if expected_role in ("sim", "live"):
        if expected_role == "live" and role not in LIVE_ROLES:
            logger.warning(f"登录入口不匹配: {username} (角色 {role}) 尝试实盘登录入口")
            raise HTTPException(status_code=403, detail="该账号为模拟用户, 请使用模拟用户登录入口")
        if expected_role == "sim" and role in LIVE_ROLES:
            logger.warning(f"登录入口不匹配: {username} (角色 {role}) 尝试模拟登录入口")
            raise HTTPException(status_code=403, detail="该账号为正式用户, 请使用实盘交易登录入口")

    token = create_session_token(username, role)
    _clear_login_failures(request, username)
    response = JSONResponse({
        "status": "success",
        "user": {"username": username, "role": role, "display_name": info["display_name"] or username,
                 "can_live_trade": SessionUser(username, role).can_live_trade},
    })
    # 经 HTTPS 访问时附加 Secure 标记; 本地 HTTP 调试下不加, 否则浏览器不会回传 Cookie
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=7 * 24 * 3600,
        httponly=True,
        secure=is_https,
        samesite="lax",
        path="/",
    )
    logger.info(f"用户登录成功: {username} (角色: {role}, 入口: {expected_role or 'auto'})")
    return response


@router.post("/logout")
def logout():
    response = JSONResponse({"status": "success"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me")
def me(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return {"user": user.to_dict()}
