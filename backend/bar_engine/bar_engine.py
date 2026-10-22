from backend.trade_engine.paper_accounts import paper_accounts
from backend.tape.tape_recorder import tape_recorder
import json
import logging
import threading
from functools import wraps
from datetime import timedelta
from backend.common.trading_time import FuturesTradingCalendar
from backend.common.product_specs import get_exchange
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional, Callable, List
from backend.models.market_data import TickData, BarData, decode_bar_provenance
from backend.common.bar_evidence import advance_sina_evidence
from backend.common.volume_quality import missing_trading_minutes, inherit_volume_quality
from backend.storage.redis_client import RedisClient
from backend.storage.db_writer import db_writer
from backend.storage.mysql_client import SessionLocal
from backend.models.database import MarketBarModel
from backend.websocket.manager import ws_manager
from backend.common.config import cfg

logger = logging.getLogger(__name__)


def engine_locked(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


class BarGenerator:
    def __init__(self, instrument: str, on_bar_close: Callable[[BarData], None]):
        self.instrument = instrument
        self.on_bar_close = on_bar_close
        self.current_bar: Optional[BarData] = None
        self.last_tick_volume: int = 0
        self.last_tick_turnover: Decimal = Decimal("0")
        self.last_bar_close_oi: Optional[Decimal] = None
        self.last_tick_time = None
        self.closed_until = None

    def _starts_trading_day(self, bar_minute: datetime) -> bool:
        """仅在累计量从零起算的新交易日开盘分钟接纳首笔累计量。"""
        if self.last_tick_time and self.last_tick_time >= bar_minute:
            return False
        if bar_minute.hour == 21 and bar_minute.minute == 0:
            return FuturesTradingCalendar.is_trading_time_for(bar_minute, self.instrument)
        day_open = (9, 30) if get_exchange(self.instrument) == "CFFEX" else (9, 0)
        if (bar_minute.hour, bar_minute.minute) != day_open:
            return False
        previous_day = FuturesTradingCalendar.previous_trading_day(bar_minute.date())
        return (not FuturesTradingCalendar.has_night_session(self.instrument)
                or not FuturesTradingCalendar._night_session_day(previous_day))

    def update_tick(self, tick: TickData) -> Optional[BarData]:
        if self.last_tick_time and tick.timestamp <= self.last_tick_time:
            return self.current_bar
        if self.closed_until and tick.timestamp <= self.closed_until:
            return self.current_bar
        bar_minute = tick.timestamp.replace(second=0, microsecond=0)
        # 收盘边界: 属于 [session_end, session_end + 1min) 的 tick 是收盘静态快照,
        # 其成交量已计入最后一根有效分钟, 必须归到 session_end 前一分钟.
        # 用区间判定而非严格相等: 交易所快照常带非零毫秒或迟到 1 秒, 严格相等会漏判,
        # 进而以 15:00:00/23:00:00 新建一根 OHLCV 与前一分钟全同的"幽灵 bar"。
        session_end = FuturesTradingCalendar.session_end(tick.timestamp, tick.instrument)
        if session_end <= tick.timestamp < session_end + timedelta(minutes=1):
            bar_minute = session_end.replace(second=0, microsecond=0) - timedelta(minutes=1)
        elif not FuturesTradingCalendar.is_trading_time_for(tick.timestamp, tick.instrument):
            # 休市时段 (10:15~10:30 小休、11:30~13:30 午休、收盘后) 的静默快照不参与 K 线聚合,
            # 否则会凭空多出一根只有快照价、零成交量的假 bar。
            return self.current_bar

        opening_volume = self._starts_trading_day(bar_minute)
        same_trading_day = (self.last_tick_time is not None and
                            FuturesTradingCalendar.get_trading_day(self.last_tick_time) ==
                            FuturesTradingCalendar.get_trading_day(tick.timestamp))
        self.last_tick_time = tick.timestamp

        if self.current_bar is None:
            first_volume = (tick.volume if opening_volume else
                            max(0, tick.volume - self.last_tick_volume) if same_trading_day else 0)
            first_turnover = (tick.turnover if opening_volume else
                              max(Decimal("0.0"), tick.turnover - self.last_tick_turnover)
                              if same_trading_day else Decimal("0.0"))
            self.last_tick_volume = tick.volume
            self.last_tick_turnover = tick.turnover
            if self.last_bar_close_oi is None:
                self.last_bar_close_oi = tick.open_interest

            self.current_bar = BarData(
                instrument=self.instrument,
                trading_day=tick.trading_day,
                period="1m",
                bar_time=bar_minute,
                open_price=tick.last_price,
                high_price=tick.last_price,
                low_price=tick.last_price,
                close_price=tick.last_price,
                volume=first_volume,
                turnover=first_turnover,
                open_interest=tick.open_interest,
                open_interest_change=Decimal("0.0")
            )
            return self.current_bar

        if bar_minute > self.current_bar.bar_time:
            finished_bar = self.current_bar
            self.on_bar_close(finished_bar)

            delta_vol = tick.volume if opening_volume else max(0, tick.volume - self.last_tick_volume)
            delta_turnover = tick.turnover if opening_volume else max(Decimal("0.0"), tick.turnover - self.last_tick_turnover)
            oi_change = tick.open_interest - self.last_bar_close_oi
            self.last_bar_close_oi = tick.open_interest

            self.current_bar = BarData(
                instrument=self.instrument,
                trading_day=tick.trading_day,
                period="1m",
                bar_time=bar_minute,
                open_price=tick.last_price,
                high_price=tick.last_price,
                low_price=tick.last_price,
                close_price=tick.last_price,
                volume=delta_vol,
                turnover=delta_turnover,
                open_interest=tick.open_interest,
                open_interest_change=oi_change
            )
        else:
            delta_vol = max(0, tick.volume - self.last_tick_volume)
            delta_turnover = max(Decimal("0.0"), tick.turnover - self.last_tick_turnover)

            self.current_bar.high_price = max(self.current_bar.high_price, tick.last_price)
            self.current_bar.low_price = min(self.current_bar.low_price, tick.last_price)
            self.current_bar.close_price = tick.last_price
            self.current_bar.volume += delta_vol
            self.current_bar.turnover += delta_turnover
            self.current_bar.open_interest = tick.open_interest
            if self.last_bar_close_oi is not None:
                self.current_bar.open_interest_change = tick.open_interest - self.last_bar_close_oi

        self.last_tick_volume = tick.volume
        self.last_tick_turnover = tick.turnover
        return self.current_bar


class MultiPeriodBarEngine:
    def __init__(self, on_higher_bar_close: Optional[Callable[[BarData], None]] = None,
                 feed: Optional[str] = None, persist_db: bool = True,
                 drive_paper: bool = True):
        self._lock = threading.RLock()
        self.feed = (feed or cfg.system.feed or "sina").strip().lower()
        self.persist_db = persist_db
        self.drive_paper = drive_paper
        self.state_key = "bar-engine:forming:" + self.feed
        self.generators: Dict[str, BarGenerator] = {}
        self.synthetic_bars: Dict[str, Dict[str, Optional[BarData]]] = {}
        self.daily_bars: Dict[str, Optional[BarData]] = {}
        self.history_bars: Dict[str, Dict[str, List[BarData]]] = {}
        self.sina_observations = {}
        self.sina_volume_anchors = {}
        self.redis = RedisClient.get_client()
        self.on_higher_bar_close = on_higher_bar_close
        self.target_periods = ["5m", "15m", "30m", "60m"]
        self.all_periods = ["1m", "5m", "15m", "30m", "60m", "1d"]
        db_writer.start()
        # 执行热重启恢复
        self.warm_up_from_storage()
        self.restore_forming()

    def save_forming(self, instrument):
        generator = self.generators[instrument]
        encode = lambda bar: bar.model_dump(mode="json") if bar else None
        state = {
            "minute": encode(generator.current_bar),
            "volume": generator.last_tick_volume,
            "turnover": str(generator.last_tick_turnover),
            "oi": str(generator.last_bar_close_oi) if generator.last_bar_close_oi is not None else None,
            "time": generator.last_tick_time.isoformat() if generator.last_tick_time else None,
            "closed_until": generator.closed_until.isoformat() if generator.closed_until else None,
            "higher": {p: encode(b) for p, b in self.synthetic_bars[instrument].items()},
            "daily": encode(self.daily_bars[instrument]),
        }
        if self.feed == 'sina':
            state['source_observation'] = self.sina_observations.get(instrument)
            state['volume_anchor'] = self.sina_volume_anchors.get(instrument)
        self.redis.hset(self.state_key, instrument, json.dumps(state))

    def restore_forming(self):
        for instrument, raw in self.redis.hgetall(self.state_key).items():
            state = json.loads(raw)
            decode = lambda value: BarData.model_validate(value) if value else None
            generator = BarGenerator(instrument, self.on_1m_bar_close)
            generator.current_bar = decode(state["minute"])
            generator.last_tick_volume = state["volume"]
            generator.last_tick_turnover = Decimal(state["turnover"])
            generator.last_bar_close_oi = Decimal(state["oi"]) if state["oi"] is not None else None
            generator.last_tick_time = datetime.fromisoformat(state["time"]) if state["time"] else None
            generator.closed_until = datetime.fromisoformat(state["closed_until"]) if state.get("closed_until") else None
            self.generators[instrument] = generator
            self.synthetic_bars[instrument] = {p: decode(b) for p, b in state["higher"].items()}
            self.daily_bars[instrument] = decode(state["daily"])
            self.history_bars.setdefault(instrument, {p: [] for p in self.all_periods})
            if self.feed == 'sina':
                self.sina_observations[instrument] = state.get('source_observation')
                self.sina_volume_anchors[instrument] = state.get('volume_anchor')

    @engine_locked
    def close_sessions(self, now=None):
        now = now or datetime.now()
        for instrument, generator in self.generators.items():
            bar = generator.current_bar
            if bar is None or now < FuturesTradingCalendar.session_end(bar.bar_time, instrument):
                continue
            end = FuturesTradingCalendar.session_end(bar.bar_time, instrument)
            # 兜底防线: 收盘时刻(15:00/23:00/01:00 等)本身不是可交易的分钟。
            # 若 current_bar 恰好落在收盘整点, 说明它是收盘快照的残影, 直接丢弃不出库。
            if bar.bar_time.replace(second=0, microsecond=0) >= end:
                logger.warning("%s 丢弃收盘边界残影 bar %s (session_end=%s)", instrument, bar.bar_time, end)
                generator.current_bar = None
                generator.closed_until = end
                self.save_forming(instrument)
                continue
            self.on_1m_bar_close(bar)
            generator.current_bar = None
            generator.closed_until = end
            for period, current in self.synthetic_bars[instrument].items():
                if current is not None:
                    self._save_bar_to_redis(current)
                    if self.persist_db:
                        db_writer.put_bar(current)
                    self.history_bars[instrument][period].append(current)
                    if self.on_higher_bar_close:
                        self.on_higher_bar_close(current)
                    ws_manager.broadcast_threadsafe({"event": "BAR_CLOSE", "feed": self.feed, "period": period, "data": current.to_redis_dict()})
                    self.synthetic_bars[instrument][period] = None
            if end.hour == 15 and self.daily_bars[instrument] is not None:
                daily = self.daily_bars[instrument]
                self._save_bar_to_redis(daily)
                if self.persist_db:
                    db_writer.put_bar(daily)
                self.history_bars[instrument]["1d"].append(daily)
                ws_manager.broadcast_threadsafe({"event": "BAR_CLOSE", "feed": self.feed, "period": "1d", "data": daily.to_redis_dict()})
                self.daily_bars[instrument] = None
            self.save_forming(instrument)
            if end.hour == 15 and self.drive_paper:
                paper_accounts.snapshot_close(bar.trading_day)

    def _get_period_minutes(self, period: str) -> int:
        return int(period.replace("m", ""))

    def _generated_provenance(self, period: str, *observations):
        if self.feed != "sina":
            return None
        # Tick polling does not prove uninterrupted exchange coverage, even after closure.
        return inherit_volume_quality({
            "source": "sina_tick_aggregate", "provider": "sina",
            "label_semantics": "DAILY_CLOSE" if period == "1d" else "START",
            "coverage": "PARTIAL", "collected_at": datetime.now(timezone.utc).isoformat(),
            "trading_day_rule": "FuturesTradingCalendar.get_trading_day",
        }, *observations)

    def warm_up_from_storage(self):
        """从 MySQL/Redis 预热恢复历史 K 线数据，实现秒级热重启"""
        session = SessionLocal()
        try:
            symbols = {inst.symbol for inst in cfg.instruments}
            symbols.update(self.redis.smembers("watchlist:subscribed"))
            symbols.update(self.redis.hkeys(self.state_key))
            for sym in sorted(symbols):
                self.history_bars[sym] = {p: [] for p in self.all_periods}
                self.synthetic_bars[sym] = {p: None for p in self.target_periods}
                self.daily_bars[sym] = None

                for p in self.all_periods:
                    if not self.persist_db:
                        continue
                    rows = session.query(MarketBarModel).filter_by(
                        instrument_id=sym,
                        period=p
                    ).order_by(MarketBarModel.bar_time.desc()).limit(100).all()
                    rows.reverse()

                    for r in rows:
                        b = BarData(
                            instrument=r.instrument_id,
                            trading_day=r.trading_day,
                            period=r.period,
                            bar_time=r.bar_time,
                            open_price=r.open_price,
                            high_price=r.high_price,
                            low_price=r.low_price,
                            close_price=r.close_price,
                            volume=r.volume,
                            turnover=r.turnover,
                            open_interest=r.open_interest,
                            open_interest_change=r.open_interest_change,
                            provenance_json=(decode_bar_provenance(getattr(r, "provenance_json", None))
                                             if self.feed == "sina" else None)
                        )
                        self.history_bars[sym][p].append(b)

            if self.persist_db:
                logger.info("%s BarEngine warm-up completed from MySQL (including 1d bars).", self.feed)
            else:
                logger.info("%s BarEngine uses isolated Redis history; MySQL warm-up skipped.", self.feed)
        except Exception as e:
            logger.error(f"Error during warm_up: {e}")
        finally:
            session.close()

    @engine_locked
    def on_tick(self, tick: TickData) -> Optional[BarData]:
        if tick.instrument not in self.generators:
            self.generators[tick.instrument] = BarGenerator(
                tick.instrument,
                on_bar_close=self.on_1m_bar_close
            )
            if tick.instrument not in self.synthetic_bars:
                self.synthetic_bars[tick.instrument] = {p: None for p in self.target_periods}
                self.daily_bars[tick.instrument] = None
                self.history_bars[tick.instrument] = {p: [] for p in self.all_periods}

        generator = self.generators[tick.instrument]
        if (generator.last_tick_time and tick.timestamp <= generator.last_tick_time) or (generator.closed_until and tick.timestamp <= generator.closed_until):
            return generator.current_bar
        gap = None
        if (self.feed == 'sina' and generator.last_tick_time is not None
                and FuturesTradingCalendar.is_trading_time_for(tick.timestamp,tick.instrument)
                and FuturesTradingCalendar.get_trading_day(generator.last_tick_time) == tick.trading_day
                and missing_trading_minutes(generator.last_tick_time,tick.timestamp,tick.instrument)):
            gap = dict(from_source_time=generator.last_tick_time.isoformat(),
                       to_source_time=tick.timestamp.isoformat(),
                       previous_cumulative=generator.last_tick_volume,current_cumulative=tick.volume,
                       unallocated_volume=max(0,tick.volume-generator.last_tick_volume))
        cur_bar = generator.update_tick(tick)
        if self.feed == "sina" and cur_bar is not None:
            cur_bar.provenance_json = self._generated_provenance(cur_bar.period,cur_bar)
            if gap:
                # This difference covers missing minutes; it is not this minute's volume.
                cur_bar.volume=max(0,cur_bar.volume-gap['unallocated_volume'])
                cur_bar.provenance_json.update(volume_quality='UNALLOCATED_GAP',volume_gap=gap)
            if generator.last_tick_time == tick.timestamp:
                sample, anchor = advance_sina_evidence(self.sina_observations.get(tick.instrument),
                    tick.to_redis_dict(), self.sina_volume_anchors.get(tick.instrument))
                self.sina_observations[tick.instrument] = sample
                self.sina_volume_anchors[tick.instrument] = anchor

        try:
            quote_payload = tick.to_redis_dict()
            if self.feed == 'sina':
                # Local receipt of a new source tick; duplicate source ticks
                # return above and never refresh this freshness timestamp.
                quote_payload['received_at'] = datetime.now().isoformat()
            self.redis.hset(f"quote:{self.feed}:{tick.instrument}", mapping=quote_payload)
            # 新浪键保持兼容历史代码与已有缓存；CTP 数据绝不写入该公共键。
            if self.feed == "sina":
                self.redis.hset(f"quote:{tick.instrument}", mapping=quote_payload)
        except Exception as e:
            logger.error(f"Redis set quote error: {e}")

        self.save_forming(tick.instrument)
        ws_manager.broadcast_threadsafe({
            "event": "TICK",
            "feed": self.feed,
            "data": tick.to_redis_dict()
        })

        # 驱动模拟撮合交易引擎更新最新持仓盈亏及挂单撮合
        if self.drive_paper:
            try:
                paper_accounts.on_tick(tick)
            except Exception as e:
                logger.error(f"PaperTrader on_tick error: {e}")
        # 驱动成交明细与大单监控
            try:
                tape_recorder.on_tick(tick)
            except Exception as e:
                logger.error(f"TapeRecorder on_tick error: {e}")


        if cfg.system.save_raw_tick and self.persist_db:
            db_writer.put_tick(tick)

        return cur_bar

    def on_1m_bar_close(self, bar: BarData):
        if self.feed == "sina":
            bar.provenance_json = self._generated_provenance(bar.period,bar)
        self._save_bar_to_redis(bar)
        if self.persist_db:
            db_writer.put_bar(bar)
        if bar.instrument in self.history_bars:
            self.history_bars[bar.instrument]["1m"].append(bar)
            if len(self.history_bars[bar.instrument]["1m"]) > 200:
                self.history_bars[bar.instrument]["1m"].pop(0)

        ws_manager.broadcast_threadsafe({
            "event": "BAR_CLOSE",
            "feed": self.feed,
            "period": "1m",
            "data": bar.to_redis_dict()
        })

        for period in self.target_periods:
            self._update_higher_period(bar, period)

        self._update_daily_bar(bar)

    def _update_higher_period(self, bar_1m: BarData, period: str):
        minutes = self._get_period_minutes(period)
        bar_time = bar_1m.bar_time
        period_start_minute = (bar_time.minute // minutes) * minutes
        aligned_time = bar_time.replace(minute=period_start_minute, second=0, microsecond=0)

        current_synth = self.synthetic_bars[bar_1m.instrument][period]
        if self.feed == "sina" and current_synth is not None:
            current_synth.provenance_json = self._generated_provenance(period,current_synth,
                bar_1m if current_synth.bar_time == aligned_time else None)

        if current_synth is None:
            current_synth = BarData(
                instrument=bar_1m.instrument,
                trading_day=bar_1m.trading_day,
                period=period,
                bar_time=aligned_time,
                open_price=bar_1m.open_price,
                high_price=bar_1m.high_price,
                low_price=bar_1m.low_price,
                close_price=bar_1m.close_price,
                volume=bar_1m.volume,
                turnover=bar_1m.turnover,
                open_interest=bar_1m.open_interest,
                open_interest_change=bar_1m.open_interest_change,
                provenance_json=self._generated_provenance(period,bar_1m)
            )
            self.synthetic_bars[bar_1m.instrument][period] = current_synth
        else:
            if aligned_time > current_synth.bar_time:
                finished_bar = current_synth
                self._save_bar_to_redis(finished_bar)
                if self.persist_db:
                    db_writer.put_bar(finished_bar)

                if bar_1m.instrument in self.history_bars:
                    self.history_bars[bar_1m.instrument][period].append(finished_bar)
                    if len(self.history_bars[bar_1m.instrument][period]) > 200:
                        self.history_bars[bar_1m.instrument][period].pop(0)

                ws_manager.broadcast_threadsafe({
                    "event": "BAR_CLOSE",
                    "feed": self.feed,
                    "period": period,
                    "data": finished_bar.to_redis_dict()
                })

                if self.on_higher_bar_close:
                    self.on_higher_bar_close(finished_bar)

                current_synth = BarData(
                    instrument=bar_1m.instrument,
                    trading_day=bar_1m.trading_day,
                    period=period,
                    bar_time=aligned_time,
                    open_price=bar_1m.open_price,
                    high_price=bar_1m.high_price,
                    low_price=bar_1m.low_price,
                    close_price=bar_1m.close_price,
                    volume=bar_1m.volume,
                    turnover=bar_1m.turnover,
                    open_interest=bar_1m.open_interest,
                    open_interest_change=bar_1m.open_interest_change,
                    provenance_json=self._generated_provenance(period,bar_1m)
                )
                self.synthetic_bars[bar_1m.instrument][period] = current_synth
            else:
                current_synth.high_price = max(current_synth.high_price, bar_1m.high_price)
                current_synth.low_price = min(current_synth.low_price, bar_1m.low_price)
                current_synth.close_price = bar_1m.close_price
                current_synth.volume += bar_1m.volume
                current_synth.turnover += bar_1m.turnover
                current_synth.open_interest = bar_1m.open_interest
                current_synth.open_interest_change += bar_1m.open_interest_change

    def _update_daily_bar(self, bar_1m: BarData):
        """
        日线 (1d) 跨交易日合成机制：
        国内期货交易日由前一工作日夜盘 (21:00) 开始，至当日白盘收盘 (15:00) 结束。
        同一 trading_day 的全部 1m K线累积至一根 1d Bar。
        当遇到新的 trading_day 或者 15:00 收盘时触发日线结算。
        """
        inst = bar_1m.instrument
        cur_day = bar_1m.trading_day
        daily = self.daily_bars.get(inst)
        if self.feed == "sina" and daily is not None:
            daily.provenance_json = self._generated_provenance("1d",daily,
                bar_1m if daily.trading_day == cur_day else None)

        # 日线时间戳规范化为该交易日的 15:00
        try:
            day_dt = datetime.strptime(cur_day, "%Y%m%d").replace(hour=15, minute=0, second=0, microsecond=0)
        except Exception:
            day_dt = bar_1m.bar_time.replace(hour=15, minute=0, second=0, microsecond=0)

        if daily is None:
            self.daily_bars[inst] = BarData(
                instrument=inst,
                trading_day=cur_day,
                period="1d",
                bar_time=day_dt,
                open_price=bar_1m.open_price,
                high_price=bar_1m.high_price,
                low_price=bar_1m.low_price,
                close_price=bar_1m.close_price,
                volume=bar_1m.volume,
                turnover=bar_1m.turnover,
                open_interest=bar_1m.open_interest,
                open_interest_change=bar_1m.open_interest_change,
                provenance_json=self._generated_provenance("1d",bar_1m)
            )
        else:
            # 跨交易日：闭合上一交易日日线
            if cur_day != daily.trading_day:
                finished_daily = daily
                self._save_bar_to_redis(finished_daily)
                if self.persist_db:
                    db_writer.put_bar(finished_daily)

                if inst in self.history_bars:
                    self.history_bars[inst]["1d"].append(finished_daily)
                    if len(self.history_bars[inst]["1d"]) > 100:
                        self.history_bars[inst]["1d"].pop(0)

                ws_manager.broadcast_threadsafe({
                    "event": "BAR_CLOSE",
                    "feed": self.feed,
                    "period": "1d",
                    "data": finished_daily.to_redis_dict()
                })

                if self.on_higher_bar_close:
                    self.on_higher_bar_close(finished_daily)

                # 开启新交易日日线
                self.daily_bars[inst] = BarData(
                    instrument=inst,
                    trading_day=cur_day,
                    period="1d",
                    bar_time=day_dt,
                    open_price=bar_1m.open_price,
                    high_price=bar_1m.high_price,
                    low_price=bar_1m.low_price,
                    close_price=bar_1m.close_price,
                    volume=bar_1m.volume,
                    turnover=bar_1m.turnover,
                    open_interest=bar_1m.open_interest,
                    open_interest_change=bar_1m.open_interest_change,
                    provenance_json=self._generated_provenance("1d",bar_1m)
                )
            else:
                # 累积当前交易日数据
                daily.high_price = max(daily.high_price, bar_1m.high_price)
                daily.low_price = min(daily.low_price, bar_1m.low_price)
                daily.close_price = bar_1m.close_price
                daily.volume += bar_1m.volume
                daily.turnover += bar_1m.turnover
                daily.open_interest = bar_1m.open_interest
                daily.open_interest_change += bar_1m.open_interest_change

                # 如果到达白盘收盘最后一根 1m (14:59~15:00)，主动闭合该交易日日线
                t = bar_1m.bar_time.time()
                if (t.hour == 14 and t.minute == 59) or (t.hour == 15 and t.minute == 0):
                    finished_daily = daily
                    self._save_bar_to_redis(finished_daily)
                    if self.persist_db:
                        db_writer.put_bar(finished_daily)

                    if inst in self.history_bars:
                        self.history_bars[inst]["1d"].append(finished_daily)
                        if len(self.history_bars[inst]["1d"]) > 100:
                            self.history_bars[inst]["1d"].pop(0)

                    ws_manager.broadcast_threadsafe({
                        "event": "BAR_CLOSE",
                        "feed": self.feed,
                        "period": "1d",
                        "data": finished_daily.to_redis_dict()
                    })

                    if self.on_higher_bar_close:
                        self.on_higher_bar_close(finished_daily)

                    # 闭合后置空，等待下一交易日夜盘或开盘重新开启
                    self.daily_bars[inst] = None

    def _save_bar_to_redis(self, bar: BarData):
        key = (f"bars:{bar.instrument}:{bar.period}" if self.feed == "sina"
               else f"bars:{self.feed}:{bar.instrument}:{bar.period}")
        try:
            payload = json.dumps(bar.to_redis_dict())
            existing = [json.loads(raw) for raw in self.redis.lrange(key, -500, -1)]
            by_time = {item["bar_time"]: item for item in existing}
            by_time[bar.bar_time.isoformat()] = bar.to_redis_dict()
            ordered = [json.dumps(by_time[t]) for t in sorted(by_time)[-500:]]
            pipe = self.redis.pipeline()
            pipe.delete(key)
            pipe.rpush(key, *ordered)
            pipe.execute()
        except Exception as e:
            logger.error(f"Save bar to redis error: {e}")
