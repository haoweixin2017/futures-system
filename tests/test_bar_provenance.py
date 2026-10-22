"""Offline row provenance regression tests; never initialize production storage."""
import hashlib
import json
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from sqlalchemy.dialects import mysql
from backend.models.market_data import BarData
from backend.storage import history_backfill, db_writer
from backend.bar_engine import bar_engine


def make_bar(**extra):
    values = dict(instrument="ZZ9999", trading_day="20260930", period="1m",
                  bar_time=datetime(2026, 9, 30, 9), open_price=1, high_price=1,
                  low_price=1, close_price=1, volume=1, turnover=0, open_interest=1)
    values.update(extra)
    return BarData(**values)


class BarProvenanceTests(unittest.TestCase):
    def test_sina_quote_receipt_is_saved_without_refreshing_duplicate_source_ticks(self):
        source = datetime(2026, 10, 15, 14, 54, 2)
        received = datetime(2026, 10, 15, 14, 54, 25)
        generator = MagicMock(last_tick_time=None, closed_until=None)
        generator.update_tick.return_value = None
        engine = MagicMock(feed='sina', drive_paper=False, persist_db=False)
        engine.generators = {'FU2701': generator}
        tick = SimpleNamespace(instrument='FU2701', timestamp=source,
            to_redis_dict=lambda: {'timestamp':source.isoformat(),'last_price':'4820'})
        with patch.object(bar_engine, 'datetime') as clock, patch.object(bar_engine.ws_manager, 'broadcast_threadsafe'):
            clock.now.return_value = received
            bar_engine.MultiPeriodBarEngine.on_tick.__wrapped__(engine, tick)
            for call in engine.redis.hset.call_args_list:
                self.assertEqual(call.kwargs['mapping']['timestamp'], source.isoformat())
                self.assertEqual(call.kwargs['mapping']['received_at'], received.isoformat())
            self.assertEqual(engine.redis.hset.call_count, 2)
            generator.last_tick_time = source
            engine.redis.hset.reset_mock()
            clock.now.return_value = datetime(2026, 10, 15, 14, 55)
            bar_engine.MultiPeriodBarEngine.on_tick.__wrapped__(engine, tick)
            engine.redis.hset.assert_not_called()

    def test_malformed_metadata_is_unknown_without_losing_bar(self):
        for raw in ('{broken', '[]', 'null', 3):
            self.assertIsNone(history_backfill.decode_bar_provenance(raw))
            row = SimpleNamespace(provenance_json=raw)
            self.assertIsNone(history_backfill._repair_provenance(row, {'provenance_json': raw}, True))
            history_backfill._mark_volume_transformation(row)

    def test_legacy_row_unknown_and_redis_preserves_dict(self):
        self.assertIsNone(make_bar().provenance_json)
        metadata = {"source": "sina_history", "label_semantics": "START", "coverage": "UNKNOWN"}
        payload = json.loads(json.dumps(make_bar(provenance_json=metadata).to_redis_dict()))
        self.assertEqual(payload["provenance_json"], metadata)
        self.assertEqual(BarData.model_validate(payload).provenance_json, metadata)

    def test_history_keeps_factual_source_label_and_hash(self):
        raw = 'var _=([{"d":"2026-09-30 09:01:00","o":"1","h":"1","l":"1","c":"1","v":"2","p":"3"}]);'
        with patch.object(history_backfill, "_http_get", return_value=raw):
            minute = history_backfill.fetch_sina_minute_bars("ZZ9999", "1m")[0]
            higher = history_backfill.fetch_sina_minute_bars("ZZ9999", "60m")[0]
        self.assertEqual(minute["bar_time"], datetime(2026, 9, 30, 9))
        for row, label in ((minute, "START"), (higher, "END")):
            metadata = json.loads(row["provenance_json"])
            self.assertEqual(metadata["label_semantics"], label)
            self.assertEqual(metadata["coverage"], "UNKNOWN")
            self.assertEqual(metadata["source_time"], "2026-09-30 09:01:00")
            self.assertEqual(metadata["response_sha256"], hashlib.sha256(raw.encode()).hexdigest())
            self.assertIn("collected_at", metadata)

    def test_daily_has_close_label_unknown_coverage(self):
        raw = 'var _=([{"d":"2026-09-30","o":"1","h":"1","l":"1","c":"1","v":"2","p":"3"}]);'
        with patch.object(history_backfill, "_http_get", return_value=raw):
            row = history_backfill.fetch_sina_daily_bars("ZZ9999")[0]
        self.assertEqual(json.loads(row["provenance_json"])["label_semantics"], "DAILY_CLOSE")
        self.assertEqual(json.loads(row["provenance_json"])["coverage"], "UNKNOWN")

    def test_db_writer_serializes_and_unknown_overwrite_clears_metadata(self):
        metadata = {"source": "sina_tick_aggregate", "coverage": "PARTIAL"}
        for bar in (make_bar(provenance_json=metadata), make_bar()):
            session = MagicMock()
            with patch.object(db_writer, "SessionLocal", return_value=session):
                db_writer.AsyncDBWriter()._flush_batch([bar], [])
            statement = session.execute.call_args.args[0]
            compiled = statement.compile(dialect=mysql.dialect())
            value = compiled.params["provenance_json"]
            self.assertEqual(json.loads(value) if value else None, bar.provenance_json)
            self.assertIn("provenance_json = VALUES(provenance_json)", str(compiled))

    def test_history_unknown_overwrite_always_clears_metadata(self):
        row = history_backfill._build_bar("ZZ9999", "1m", datetime(2026, 9, 30, 9), 1, 1, 1, 1, 1, 1, 0)
        row.pop("provenance_json")  # Older callers provide no metadata field at all.
        session = MagicMock()
        with patch.object(history_backfill, "SessionLocal", return_value=session):
            history_backfill.save_bars_to_mysql([row])
        compiled = session.execute.call_args.args[0].compile(dialect=mysql.dialect())
        self.assertIn("provenance_json = VALUES(provenance_json)", str(compiled))
        self.assertIn(None, [v for k, v in compiled.params.items() if k.startswith("provenance_json")])

    def test_local_generated_bars_are_partial_including_daily_cold_start(self):
        engine = bar_engine.MultiPeriodBarEngine.__new__(bar_engine.MultiPeriodBarEngine)
        engine.feed = "sina"
        engine.synthetic_bars = {"ZZ9999": {"60m": None}}
        engine.daily_bars = {}
        engine._update_higher_period(make_bar(bar_time=datetime(2026, 9, 30, 9, 40)), "60m")
        engine._update_daily_bar(make_bar(bar_time=datetime(2026, 9, 30, 9, 40)))
        higher = engine.synthetic_bars["ZZ9999"]["60m"]
        daily = engine.daily_bars["ZZ9999"]
        self.assertEqual(higher.provenance_json["label_semantics"], "START")
        self.assertEqual(daily.provenance_json["label_semantics"], "DAILY_CLOSE")
        self.assertEqual(higher.provenance_json["coverage"], "PARTIAL")
        self.assertEqual(daily.provenance_json["coverage"], "PARTIAL")
        engine.feed = "other"
        self.assertIsNone(engine._generated_provenance("60m"))

    def test_mysql_warmup_preserves_source_attributes(self):
        engine = bar_engine.MultiPeriodBarEngine.__new__(bar_engine.MultiPeriodBarEngine)
        engine.feed = "sina"
        engine.persist_db = True
        engine.redis = MagicMock()
        engine.redis.smembers.return_value = []
        engine.redis.hkeys.return_value = []
        engine.state_key = "test"
        engine.history_bars, engine.synthetic_bars, engine.daily_bars = {}, {}, {}
        engine.target_periods, engine.all_periods = ["60m"], ["1m"]
        metadata = {"source": "sina_history", "label_semantics": "START", "coverage": "UNKNOWN"}
        values = make_bar().model_dump(exclude={"instrument", "provenance_json"})
        row = SimpleNamespace(instrument_id="ZZ9999", provenance_json=json.dumps(metadata), **values)
        bad = SimpleNamespace(instrument_id="ZZ9999", provenance_json='{broken', **values)
        session = MagicMock()
        session.query.return_value.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value = [row, bad]
        with patch.object(bar_engine, "SessionLocal", return_value=session), patch.object(bar_engine.cfg, "instruments", [SimpleNamespace(symbol="ZZ9999")]):
            engine.warm_up_from_storage()
        bars = engine.history_bars["ZZ9999"]["1m"]
        self.assertEqual(len(bars), 2)
        self.assertEqual([bar.provenance_json for bar in bars], [None, metadata])

    def test_repair_marks_mixed_fields_and_unknown_source_clears(self):
        row = SimpleNamespace(provenance_json=json.dumps({"source": "sina_tick_aggregate", "coverage": "PARTIAL"}))
        source = {"provenance_json": json.dumps({"source": "sina_history", "label_semantics": "START", "coverage": "UNKNOWN"})}
        metadata = json.loads(history_backfill._repair_provenance(row, source, True))
        self.assertEqual(metadata["source"], "sina_history_repair")
        self.assertEqual(metadata["coverage"], "PARTIAL")
        self.assertEqual(metadata["volume_source"], "sina_tick_aggregate")
        self.assertIn("volume", metadata["retained_fields"])
        self.assertIsNone(history_backfill._repair_provenance(row, {}, True))

    def test_history_redis_and_volume_transformation_keep_factual_metadata(self):
        metadata = {"source": "sina_history", "coverage": "UNKNOWN", "label_semantics": "START"}
        row = history_backfill._build_bar("ZZ9999", "1m", datetime(2026, 9, 30, 9), 1, 1, 1, 1, 1, 1, 0, metadata)
        history_backfill._mark_volume_transformation(row)
        redis = MagicMock()
        redis.exists.return_value = False
        with patch.object(history_backfill.RedisClient, "get_client", return_value=redis):
            history_backfill.sync_redis_if_empty("ZZ9999", "1m", [row])
        payload = json.loads(redis.pipeline.return_value.rpush.call_args.args[1])
        self.assertEqual(payload["provenance_json"]["coverage"], "UNKNOWN")
        self.assertEqual(payload["provenance_json"]["volume_transformation"], "auction_volume_redistribution")
        unknown = dict(row, provenance_json=None)
        history_backfill._mark_volume_transformation(unknown)
        self.assertIsNone(unknown["provenance_json"])


if __name__ == "__main__":
    unittest.main()
