import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional, Dict, Any
import httpx
from backend.storage.mysql_client import SessionLocal
from backend.models.database import SystemEventModel
from backend.websocket.manager import ws_manager

logger = logging.getLogger(__name__)
PRIVATE_TRADE_SOURCES = {"paper_trader", "stop_engine", "live_trader", "ctp_trader"}

# 严重级别的事件重复推送次数与间隔: 单次网络抖动不应导致关键告警漏报
CRITICAL_REPEAT = 3
CRITICAL_INTERVAL = 5.0


class EventNotifier:
    """
    系统异常日志与信号通知模块 (三通道):
    1. 异步将事件记录至 MySQL system_event 表
    2. WebSocket 实时向前端网页推送系统事件与告警
    3. Webhook 推送 (企业微信/钉钉/飞书), 不阻塞交易主线程

    通道隔离规则 (重要):
    - PRIVATE_TRADE_SOURCES (账户/成交类事件) **绝不**进入公共 WS 广播与公共 Webhook,
      否则会通过公共行情通道泄露他人成交信息。
    - 这类事件改走**私密 Webhook** (alert_private_webhook_url), 只有运维能看到。
      未配置私密通道时, 事件仅落库审计, 并打印一条 WARNING 提示通道缺失。
    """
    _webhook_url: Optional[str] = None
    _private_webhook_url: Optional[str] = None

    @classmethod
    def set_webhook_url(cls, url: Optional[str]):
        """设置公共告警 Webhook (非交易类事件)。"""
        cls._webhook_url = (url or "").strip() or None

    @classmethod
    def set_private_webhook_url(cls, url: Optional[str]):
        """设置私密告警 Webhook (实盘成交/报单等账户类事件)。"""
        cls._private_webhook_url = (url or "").strip() or None

    @classmethod
    def get_channel_status(cls) -> Dict[str, bool]:
        """告警通道配置状态, 供健康检查与上线前检查脚本使用。"""
        return {
            "public_webhook": bool(cls._webhook_url),
            "private_webhook": bool(cls._private_webhook_url),
        }

    @staticmethod
    def _build_payload(event_type: str, message: str, severity: str, source: str,
                       now: datetime, details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        lines = [
            f"**【期货系统-{severity}】{event_type}**",
            f"来源: {source}",
            f"内容: {message}",
        ]
        if details:
            try:
                lines.append("详情: " + json.dumps(details, ensure_ascii=False)[:800])
            except Exception:
                pass
        lines.append(f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
        return {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}

    @classmethod
    def _post_webhook(cls, url: str, event_type: str, message: str, severity: str,
                      source: str, now: datetime, details: Optional[Dict[str, Any]]) -> None:
        payload = cls._build_payload(event_type, message, severity, source, now, details)
        repeats = CRITICAL_REPEAT if severity in ("CRITICAL", "ERROR") else 1
        for attempt in range(repeats):
            try:
                resp = httpx.post(url, json=payload, timeout=3.0)
                if resp.status_code < 300:
                    return
                logger.warning("Webhook 返回非 2xx: %s", resp.status_code)
            except Exception as exc:
                logger.warning("Webhook 发送失败 (第 %s 次): %s", attempt + 1, exc)
            if attempt < repeats - 1:
                time.sleep(CRITICAL_INTERVAL)

    @classmethod
    def notify(
        cls,
        event_type: str,
        message: str,
        severity: str = "INFO",
        source: str = "system",
        details: Optional[Dict[str, Any]] = None
    ):
        now = datetime.utcnow()
        details_str = json.dumps(details, ensure_ascii=False) if details else None

        def _save_db():
            session = SessionLocal()
            try:
                evt = SystemEventModel(
                    event_type=event_type,
                    severity=severity,
                    source=source,
                    message=message,
                    details_json=details_str,
                    event_time=now
                )
                session.add(evt)
                session.commit()
            except Exception as e:
                session.rollback()
                logger.error(f"Failed to record system event in MySQL: {e}")
            finally:
                session.close()

        threading.Thread(target=_save_db, daemon=True).start()

        is_private = source in PRIVATE_TRADE_SOURCES

        # 账户事件只保留本地审计记录，禁止通过公共行情通道泄露给其他用户。
        if not is_private:
            ws_manager.broadcast_threadsafe({
                "event": "SYSTEM_EVENT",
                "data": {
                    "event_type": event_type,
                    "severity": severity,
                    "source": source,
                    "message": message,
                    "details": details,
                    "timestamp": now.isoformat()
                }
            })

        if is_private:
            # 交易类事件只能走私密通道
            if cls._private_webhook_url:
                threading.Thread(
                    target=cls._post_webhook,
                    args=(cls._private_webhook_url, event_type, message, severity, source, now, details),
                    daemon=True,
                ).start()
            elif severity in ("CRITICAL", "ERROR", "WARNING"):
                logger.warning(
                    "私密告警通道未配置 (system.alert_private_webhook_url), "
                    "事件 %s [%s] 仅落库: %s", event_type, severity, message
                )
            return

        if cls._webhook_url:
            threading.Thread(
                target=cls._post_webhook,
                args=(cls._webhook_url, event_type, message, severity, source, now, details),
                daemon=True,
            ).start()


notifier = EventNotifier
