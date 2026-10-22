import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.exc import IntegrityError

from backend.ai_analysis import credentials, provider
from backend.models.database import AIAnalysisRunModel, AIModelConfigModel
from backend.storage.mysql_client import SessionLocal


ACTIVE_RUN_STATUSES = {"PENDING", "RUNNING"}


def normalize_and_validate_url(raw_url: str, is_local_service: bool) -> str:
    value = raw_url.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("API 地址必须是完整的 http/https 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("API 地址不能包含用户名、密码、查询参数或片段")
    try:
        addresses = provider.resolve_host_addresses(parsed.hostname, parsed.port)
    except provider.ProviderHTTPError as exc:
        raise ValueError("API 地址域名无法解析") from exc
    for address in addresses:
        if provider.address_forbidden(address):
            raise ValueError("API 地址解析到禁止访问的网络地址")
        private = provider.address_is_private(address)
        if is_local_service and not private:
            raise ValueError("勾选本地服务后，API 地址必须解析到本机或局域网")
        if not is_local_service and private:
            raise ValueError("外网模型地址不能解析到本机或局域网")
    netloc = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _credential_ref(owner: str, model_config_id: str) -> str:
    return credentials.credential_target(owner, model_config_id)


def _in_use(session, model_config_id: str) -> bool:
    return session.query(AIAnalysisRunModel.id).filter(
        AIAnalysisRunModel.model_config_id == model_config_id,
        AIAnalysisRunModel.status.in_(ACTIVE_RUN_STATUSES),
    ).first() is not None


def _public_model(row: AIModelConfigModel) -> Dict[str, Any]:
    has_key = False
    credential_error = None
    if row.requires_api_key and row.credential_ref:
        try:
            has_key = bool(credentials.credential_store.get(row.credential_ref))
        except credentials.CredentialError as exc:
            credential_error = str(exc)
    available = bool(row.enabled and (not row.requires_api_key or has_key) and not credential_error)
    reason = None
    if not row.enabled:
        reason = "模型已停用"
    elif credential_error:
        reason = "Windows 凭据管理器不可用"
    elif row.requires_api_key and not has_key:
        reason = "未配置 API Key"
    return {
        "model_config_id": row.model_config_id, "display_name": row.display_name,
        "provider": row.provider, "base_url": row.base_url, "model_name": row.model_name,
        "requires_api_key": row.requires_api_key, "is_local_service": row.is_local_service,
        "has_api_key": has_key, "max_context_tokens": row.max_context_tokens,
        "max_output_tokens": row.max_output_tokens, "timeout_seconds": row.timeout_seconds,
        "temperature": float(row.temperature), "enabled": row.enabled,
        "available": available, "unavailable_reason": reason,
        "last_test_status": row.last_test_status,
        "last_test_at": row.last_test_at.isoformat() if row.last_test_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_models(owner: str) -> list[Dict[str, Any]]:
    session = SessionLocal()
    try:
        rows = session.query(AIModelConfigModel).filter_by(owner_username=owner).order_by(
            AIModelConfigModel.created_at.asc()).all()
        return [_public_model(row) for row in rows]
    finally:
        session.close()


def get_model(session, owner: str, model_config_id: str) -> AIModelConfigModel:
    row = session.query(AIModelConfigModel).filter_by(
        owner_username=owner, model_config_id=model_config_id).first()
    if not row:
        raise LookupError("模型配置不存在")
    return row


def create_model(owner: str, data: Dict[str, Any]) -> Dict[str, Any]:
    model_config_id = str(uuid.uuid4())
    base_url = normalize_and_validate_url(data["base_url"], data["is_local_service"])
    requires_key = bool(data["requires_api_key"])
    api_key = (data.get("api_key") or "").strip()
    if requires_key and not api_key:
        raise ValueError("该模型需要 API Key")
    target = _credential_ref(owner, model_config_id) if requires_key else None
    if target:
        credentials.credential_store.set(target, api_key)
    session = SessionLocal()
    try:
        row = AIModelConfigModel(
            model_config_id=model_config_id, owner_username=owner,
            display_name=data["display_name"].strip(), provider=data["provider"],
            base_url=base_url, model_name=data["model_name"].strip(), credential_ref=target,
            requires_api_key=requires_key, is_local_service=bool(data["is_local_service"]),
            max_context_tokens=data["max_context_tokens"], max_output_tokens=data["max_output_tokens"],
            timeout_seconds=data["timeout_seconds"], temperature=Decimal(str(data["temperature"])),
            enabled=bool(data["enabled"]),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _public_model(row)
    except IntegrityError as exc:
        session.rollback()
        if target:
            credentials.credential_store.delete(target)
        raise ValueError("模型显示名称已存在") from exc
    except Exception:
        session.rollback()
        if target:
            credentials.credential_store.delete(target)
        raise
    finally:
        session.close()


def update_model(owner: str, model_config_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    session = SessionLocal()
    old_target = target = old_secret = None
    credential_changed = False
    try:
        row = get_model(session, owner, model_config_id)
        if _in_use(session, model_config_id):
            raise RuntimeError("模型正在分析中，暂不能编辑")
        base_url = normalize_and_validate_url(data["base_url"], data["is_local_service"])
        old_target, old_secret = row.credential_ref, None
        if old_target:
            old_secret = credentials.credential_store.get(old_target)
        requires_key = bool(data["requires_api_key"])
        api_key = (data.get("api_key") or "").strip()
        target = _credential_ref(owner, model_config_id) if requires_key else None
        if requires_key and not api_key and not old_secret:
            raise ValueError("该模型需要 API Key")
        if requires_key and api_key:
            credentials.credential_store.set(target, api_key)
            credential_changed = True
        elif not requires_key and old_target:
            credentials.credential_store.delete(old_target)
            credential_changed = True
        row.display_name = data["display_name"].strip()
        row.provider = data["provider"]
        row.base_url = base_url
        row.model_name = data["model_name"].strip()
        row.credential_ref = target
        row.requires_api_key = requires_key
        row.is_local_service = bool(data["is_local_service"])
        row.max_context_tokens = data["max_context_tokens"]
        row.max_output_tokens = data["max_output_tokens"]
        row.timeout_seconds = data["timeout_seconds"]
        row.temperature = Decimal(str(data["temperature"]))
        row.enabled = bool(data["enabled"])
        row.updated_at = datetime.utcnow()
        session.commit()
        session.refresh(row)
        return _public_model(row)
    except IntegrityError as exc:
        session.rollback()
        if credential_changed:
            if old_target and old_secret:
                credentials.credential_store.set(old_target, old_secret)
            elif target:
                credentials.credential_store.delete(target)
        raise ValueError("模型显示名称已存在") from exc
    except Exception:
        session.rollback()
        if credential_changed:
            if old_target and old_secret:
                credentials.credential_store.set(old_target, old_secret)
            elif target:
                credentials.credential_store.delete(target)
        raise
    finally:
        session.close()


def clear_key(owner: str, model_config_id: str) -> Dict[str, Any]:
    session = SessionLocal()
    try:
        row = get_model(session, owner, model_config_id)
        if _in_use(session, model_config_id):
            raise RuntimeError("模型正在分析中，暂不能清除密钥")
        if row.credential_ref:
            credentials.credential_store.delete(row.credential_ref)
        row.enabled = False
        row.updated_at = datetime.utcnow()
        session.commit()
        session.refresh(row)
        return _public_model(row)
    finally:
        session.close()


def set_model_enabled(owner: str, model_config_id: str, enabled: bool) -> Dict[str, Any]:
    session = SessionLocal()
    try:
        row = get_model(session, owner, model_config_id)
        if _in_use(session, model_config_id):
            raise RuntimeError("模型正在分析中，暂不能切换启用状态")
        row.enabled = enabled
        row.updated_at = datetime.utcnow()
        session.commit()
        session.refresh(row)
        return _public_model(row)
    finally:
        session.close()


def delete_model(owner: str, model_config_id: str) -> None:
    session = SessionLocal()
    try:
        row = get_model(session, owner, model_config_id)
        if _in_use(session, model_config_id):
            raise RuntimeError("模型正在分析中，暂不能删除")
        if row.credential_ref:
            credentials.credential_store.delete(row.credential_ref)
        session.delete(row)
        session.commit()
    finally:
        session.close()


def model_runtime_config(owner: str, model_config_id: str) -> Dict[str, Any]:
    session = SessionLocal()
    try:
        row = get_model(session, owner, model_config_id)
        if not row.enabled:
            raise ValueError(f"模型 {row.display_name} 已停用")
        api_key = credentials.credential_store.get(row.credential_ref) if row.credential_ref else None
        if row.requires_api_key and not api_key:
            raise ValueError(f"模型 {row.display_name} 未配置 API Key")
        base_url = normalize_and_validate_url(row.base_url, row.is_local_service)
        return {
            "model_config_id": row.model_config_id, "display_name": row.display_name,
            "provider": row.provider, "base_url": base_url, "model_name": row.model_name,
            "api_key": api_key, "is_local_service": bool(row.is_local_service),
            "max_context_tokens": row.max_context_tokens,
            "max_output_tokens": row.max_output_tokens, "timeout_seconds": row.timeout_seconds,
            "temperature": float(row.temperature),
        }
    finally:
        session.close()


def public_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in config.items() if key != "api_key"}


def config_hash(config: Dict[str, Any]) -> str:
    import hashlib
    body = json.dumps(public_snapshot(config), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
