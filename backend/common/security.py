"""认证安全模块: 密码哈希 (PBKDF2) 与 HMAC 签名的会话令牌。

- 密码存储格式: pbkdf2$迭代次数$salt_hex$hash_hex
- 会话令牌: base64url(JSON payload).base64url(HMAC-SHA256 签名), 载荷含用户/角色/过期时间
- 签名密钥来源优先级: 进程环境 > 合并配置(.env 档案/YAML) > config/session_secret.key > 随机生成
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Dict, Optional

from backend.common.config import CONFIG_DIR

logger = logging.getLogger(__name__)

SECRET_FILE = CONFIG_DIR / "session_secret.key"
PBKDF2_ITERATIONS = 120_000
SESSION_TTL_HOURS = 24 * 7


def _get_secret() -> bytes:
    """获取签名密钥：进程环境、合并配置、密钥文件、临时随机值依次回退。"""
    env_secret = (os.environ.get("FUTURES_SESSION_SECRET") or "").strip()
    if env_secret:
        return env_secret.encode()
    from backend.common.config import cfg
    if cfg.system.session_secret.strip():
        return cfg.system.session_secret.strip().encode()
    if SECRET_FILE.exists():
        content = SECRET_FILE.read_bytes().strip()
        if content:
            return content
    generated = secrets.token_hex(32).encode()
    logger.warning(
        "未配置 FUTURES_SESSION_SECRET 且不存在 %s, 已生成临时密钥; "
        "重启后所有登录会话将失效。", SECRET_FILE
    )
    return generated


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except Exception:
        return False


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_session_token(username: str, role: str, ttl_hours: int = SESSION_TTL_HOURS) -> str:
    payload = {
        "u": username,
        "r": role,
        "e": int(time.time()) + ttl_hours * 3600,
        "n": secrets.token_hex(8),
    }
    payload_b64 = _b64encode(json.dumps(payload).encode())
    sig = hmac.new(_get_secret(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64encode(sig)}"


def read_session_token(token: str) -> Optional[Dict]:
    """校验并解析会话令牌, 无效/过期返回 None"""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        expected_sig = hmac.new(_get_secret(), payload_b64.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected_sig, _b64decode(sig_b64)):
            return None
        payload = json.loads(_b64decode(payload_b64))
        if int(payload.get("e", 0)) < time.time():
            return None
        return {"username": payload["u"], "role": payload["r"]}
    except Exception:
        return None
