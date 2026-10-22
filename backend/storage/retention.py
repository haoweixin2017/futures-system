import logging
import asyncio
from datetime import datetime, timedelta
from backend.storage.mysql_client import SessionLocal
from backend.models.database import MarketTickModel
from backend.notifier.notifier import notifier
from backend.common.config import cfg

logger = logging.getLogger(__name__)

class DataRetentionManager:
    """
    历史数据留存与清理服务：
    负责定期清理超过保留时限的原始 Tick 数据，确保 MySQL 磁盘占用可控。
    支持手动调用、API 触发以及后台定时调度任务。
    """
    _scheduler_task: asyncio.Task = None
    _scheduler_running: bool = False

    @classmethod
    def clean_expired_ticks(cls, days: int = None) -> int:
        if days is None:
            days = getattr(cfg.system, "tick_retention_days", 7)

        threshold_time = datetime.utcnow() - timedelta(days=days)
        session = SessionLocal()
        total_deleted = 0
        try:
            deleted = session.query(MarketTickModel).filter(
                MarketTickModel.tick_time < threshold_time
            ).delete(synchronize_session=False)

            session.commit()
            total_deleted = deleted
            logger.info(f"DataRetention: cleaned {total_deleted} ticks older than {days} days ({threshold_time.isoformat()}).")

            if total_deleted > 0:
                notifier.notify(
                    event_type="DATA_CLEANUP",
                    message=f"已成功清理超过 {days} 天的历史 Tick 行情，共删除 {total_deleted} 条记录",
                    severity="INFO",
                    source="data_retention",
                    details={"deleted_count": total_deleted, "retention_days": days}
                )
            return total_deleted
        except Exception as e:
            session.rollback()
            logger.error(f"DataRetention failed to clean expired ticks: {e}")
            notifier.notify(
                event_type="DATA_CLEANUP_ERROR",
                message=f"清理过期 Tick 数据失败: {e}",
                severity="ERROR",
                source="data_retention"
            )
            return 0
        finally:
            session.close()

    @classmethod
    async def run_daily_scheduler(cls, run_at_hour: int = 2, run_at_minute: int = 30):
        """
        后台定时调度协程：每日凌晨指定时间（默认 02:30）自动清理过期 Tick。
        """
        cls._scheduler_running = True
        logger.info(f"DataRetention scheduler started: daily at {run_at_hour:02d}:{run_at_minute:02d}.")
        while cls._scheduler_running:
            try:
                now = datetime.now()
                target = now.replace(hour=run_at_hour, minute=run_at_minute, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                wait_seconds = (target - now).total_seconds()

                while wait_seconds > 0 and cls._scheduler_running:
                    chunk = min(wait_seconds, 60.0)
                    await asyncio.sleep(chunk)
                    wait_seconds -= chunk

                if cls._scheduler_running:
                    logger.info("DataRetention: Triggering scheduled cleanup...")
                    cls.clean_expired_ticks()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in retention scheduler: {e}")
                await asyncio.sleep(60.0)

    @classmethod
    def start_scheduler(cls, loop: asyncio.AbstractEventLoop, run_at_hour: int = 2, run_at_minute: int = 30):
        if not cls._scheduler_running:
            cls._scheduler_task = loop.create_task(cls.run_daily_scheduler(run_at_hour, run_at_minute))

    @classmethod
    def stop_scheduler(cls):
        cls._scheduler_running = False
        if cls._scheduler_task and not cls._scheduler_task.done():
            cls._scheduler_task.cancel()
        logger.info("DataRetention scheduler stopped.")

retention_manager = DataRetentionManager
