import time
import json
import queue
import logging
import threading
from typing import List, Dict, Any, Union
from backend.storage.mysql_client import SessionLocal
from sqlalchemy.dialects.mysql import insert
from backend.models.database import MarketBarModel, MarketTickModel
from backend.models.market_data import BarData, TickData

logger = logging.getLogger(__name__)

class AsyncDBWriter:
    """
    异步数据库批量写入器：
    行情回调和K线计算线程只负责将数据塞入内存队列，
    由独立工作线程每隔固定时间（或达到批次大小）批量写入 MySQL，
    彻底避免数据库耗时阻塞行情引擎。
    """
    def __init__(self, batch_size: int = 50, flush_interval: float = 0.5):
        self.queue: queue.Queue = queue.Queue(maxsize=100000)
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._running = False
        self._worker_thread: Union[threading.Thread, None] = None

    def start(self):
        if not self._running:
            self._running = True
            self._worker_thread = threading.Thread(target=self._worker_loop, name="AsyncDBWriterThread", daemon=True)
            self._worker_thread.start()
            logger.info("AsyncDBWriter thread started.")

    def stop(self):
        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)
        logger.info("AsyncDBWriter thread stopped.")

    def put_bar(self, bar: BarData):
        try:
            self.queue.put_nowait(("BAR", bar))
        except queue.Full:
            logger.error("DBWriter queue full, dropping bar!")

    def put_tick(self, tick: TickData):
        try:
            self.queue.put_nowait(("TICK", tick))
        except queue.Full:
            logger.error("DBWriter queue full, dropping tick!")

    def _worker_loop(self):
        bars_batch: List[BarData] = []
        ticks_batch: List[TickData] = []
        last_flush_time = time.time()

        while self._running or not self.queue.empty():
            try:
                item_type, data = self.queue.get(timeout=0.2)
                if item_type == "BAR":
                    bars_batch.append(data)
                elif item_type == "TICK":
                    ticks_batch.append(data)
                self.queue.task_done()
            except queue.Empty:
                pass

            now = time.time()
            if (len(bars_batch) + len(ticks_batch)) >= self.batch_size or (now - last_flush_time) >= self.flush_interval:
                if bars_batch or ticks_batch:
                    self._flush_batch(bars_batch, ticks_batch)
                    bars_batch.clear()
                    ticks_batch.clear()
                last_flush_time = now

        # 退出前刷盘
        if bars_batch or ticks_batch:
            self._flush_batch(bars_batch, ticks_batch)

    def _flush_batch(self, bars: List[BarData], ticks: List[TickData]):
        session = SessionLocal()
        try:
            if bars:
                for b in bars:
                    values = b.model_dump(exclude={"instrument"})
                    values["provenance_json"] = (json.dumps(b.provenance_json, ensure_ascii=False)
                                                 if b.provenance_json is not None else None)
                    values["instrument_id"] = b.instrument
                    statement = insert(MarketBarModel).values(**values)
                    immutable = {"instrument_id", "period", "bar_time"}
                    statement = statement.on_duplicate_key_update(**{
                        key: statement.inserted[key] for key in values if key not in immutable})
                    session.execute(statement)

            if ticks:
                for t in ticks:
                    row = MarketTickModel(
                        instrument_id=t.instrument,
                        exchange=t.exchange,
                        trading_day=t.trading_day,
                        tick_time=t.timestamp,
                        last_price=t.last_price,
                        bid_price_1=t.bid_price_1,
                        bid_volume_1=t.bid_volume_1,
                        ask_price_1=t.ask_price_1,
                        ask_volume_1=t.ask_volume_1,
                        volume=t.volume,
                        turnover=t.turnover,
                        open_interest=t.open_interest
                    )
                    session.add(row)

            session.commit()
            logger.debug(f"Flushed batch to MySQL: {len(bars)} bars, {len(ticks)} ticks.")
        except Exception as e:
            session.rollback()
            logger.error(f"Error flushing batch to MySQL: {e}")
        finally:
            session.close()

db_writer = AsyncDBWriter()
