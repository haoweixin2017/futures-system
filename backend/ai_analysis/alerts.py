"""AI 侧告警: 复用系统 notifier 三通道 (落库/WS广播/Webhook), 按告警键限频。

定时研究采集与结算循环按小时/5分钟周期运行, 连续失败若每次都提醒会刷屏;
这里按 key 限频, 同一故障每小时最多提醒一次, 恢复后再次失败会重新提醒。
"""
import logging
import threading
import time

from backend.notifier.notifier import notifier

logger = logging.getLogger(__name__)

# 同一告警键的最小重发间隔 (秒): 与定时采集的最小运行间隔同量级
MIN_INTERVAL_SECONDS = 3600

_last_sent: dict = {}
_lock = threading.Lock()


def notify_throttled(key: str, event_type: str, message: str, severity: str = "ERROR",
                     source: str = "ai", details: dict | None = None) -> bool:
    """按 key 限频发送告警, 返回是否真正发送; 告警失败不影响调用方流程。"""
    now = time.monotonic()
    with _lock:
        last = _last_sent.get(key)
        if last is not None and now - last < MIN_INTERVAL_SECONDS:
            return False
        _last_sent[key] = now
    try:
        notifier.notify(event_type, message, severity, source, details)
        return True
    except Exception:
        # 发送失败不占用限频窗口, 下个周期重试
        with _lock:
            _last_sent.pop(key, None)
        logger.exception("AI 告警发送失败: %s", event_type)
        return False
