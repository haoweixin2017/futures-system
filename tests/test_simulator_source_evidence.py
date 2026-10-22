"""Source evidence tests: synthetic source observations, no production writes."""
import json
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from backend.market_simulator.models import SimulatorError, at
from backend.market_simulator.reader import MarketReader, normalize_bar


def quote(stamp, volume=1000, day='20261021'):
    return dict(instrument='FU2701', trading_day=day, timestamp=stamp,
                volume=str(volume), last_price='4800')


def source_history(instrument, period, cutoff):
    """Synthetic provider response using its declared time convention."""
    from backend.common.bar_evidence import sina_history_windows
    from backend.common.trading_time import FuturesTradingCalendar as Calendar
    rows = []
    for offset in range(35, -1, -1):
        day = cutoff.date()-timedelta(days=offset)
        if not Calendar.is_trading_day(day):
            continue
        trading_day = day.strftime('%Y%m%d')
        for end in sorted(sina_history_windows(instrument, trading_day, period)):
            if at(end) > cutoff:
                continue
            rows.append(dict(instrument_id=instrument, period=period, trading_day=trading_day,
                bar_time=end, open_price='4800', high_price='4801', low_price='4799',
                close_price='4800', volume=1000, open_interest='200000',
                provenance_json=dict(provider='sina', source='sina_history', label_semantics='END',
                    aggregation_clock='TRADING_MINUTES', source_time=end.isoformat(),
                    response_sha256='a'*64, request_url='https://stock2.finance.sina.com.cn/history', coverage='UNKNOWN')))
    return rows


class MemoryRedis:
    def __init__(self):
        self.hashes, self.lists, self.writes = {}, {}, 0

    def hset(self, key, field=None, value=None, mapping=None):
        self.writes += 1
        self.hashes.setdefault(key, {}).update(mapping if mapping is not None else {field: value})

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def lrange(self, key, start, end):
        return self.lists.get(key, [])[start:] if end == -1 else self.lists.get(key, [])[start:end+1]

    def pipeline(self):
        return self

    def delete(self, key):
        self.writes += 1
        self.lists.pop(key, None)

    def rpush(self, key, *values):
        self.writes += 1
        self.lists.setdefault(key, []).extend(values)

    def execute(self):
        pass


def source_engine(redis):
    import threading
    from backend.bar_engine.bar_engine import MultiPeriodBarEngine
    engine = MultiPeriodBarEngine.__new__(MultiPeriodBarEngine)
    engine._lock = threading.RLock()
    engine.feed, engine.persist_db, engine.drive_paper = 'sina', False, False
    engine.redis, engine.state_key = redis, 'bar-engine:forming:sina'
    engine.generators, engine.synthetic_bars, engine.daily_bars, engine.history_bars = {}, {}, {}, {}
    engine.sina_observations, engine.sina_volume_anchors = {}, {}
    engine.target_periods = ['5m', '15m', '30m', '60m']
    engine.all_periods = ['1m', *engine.target_periods, '1d']
    engine.on_higher_bar_close = None
    return engine


def source_tick(stamp, volume):
    from backend.models.market_data import TickData
    return TickData(instrument='FU2701', exchange='SHFE', trading_day='20261021',
        timestamp=datetime.fromisoformat(stamp), last_price='4800', volume=volume,
        turnover=0, open_interest='200000')


class SourceEvidenceTests(unittest.TestCase):
    def test_recorded_real_boundary_passes_without_rewriting_fields(self):
        from pathlib import Path
        from backend.market_simulator.reader import verify_observation
        fixture=json.loads((Path(__file__).parent/'fixtures/market_simulator/live_fu2701_20261021_verified.json').read_text(encoding='utf-8'))
        original=deepcopy(fixture)
        observed,evidence=verify_observation(fixture['quote'],fixture['checkpoint'],fixture['closed_minutes'],
                                           at(fixture['captured_at']),'FU2701')
        self.assertFalse(fixture['synthetic'])
        self.assertTrue(all(v['passed'] for v in evidence.values()))
        self.assertEqual(observed['volume'],2124)
        self.assertEqual(fixture,original)

    def test_first_minute_excludes_proven_closed_previous_period_checkpoint(self):
        from backend.market_simulator.reader import verify_observation
        from backend.bar_engine.bar_engine import ws_manager
        redis = MemoryRedis(); engine = source_engine(redis)
        with patch.object(ws_manager,'broadcast_threadsafe'):
            engine.on_tick(source_tick('2026-10-21T13:44:59',1000))
            engine.on_tick(source_tick('2026-10-21T13:45:04',1010))
        state=json.loads(redis.hget(engine.state_key,'FU2701'))
        self.assertEqual(state['higher']['15m']['bar_time'],'2026-10-21T13:30:00')
        observed,evidence=verify_observation(redis.hgetall('quote:sina:FU2701'),state,[],
                                           at('2026-10-21T13:45:05'),'FU2701')
        self.assertEqual(observed['volume'],10)
        self.assertTrue(all(v['passed'] for v in evidence.values()))

    def test_recorded_real_missing_boundary_stays_blocked(self):
        from pathlib import Path
        from backend.market_simulator.reader import verify_observation
        fixture = json.loads((Path(__file__).parent/'fixtures/market_simulator/live_fu2701_20261021_blocked.json').read_text(encoding='utf-8'))
        self.assertFalse(fixture['synthetic'])
        with self.assertRaises(SimulatorError) as err:
            verify_observation(fixture['quote'],fixture['checkpoint'],fixture['closed_minutes'],
                               at(fixture['captured_at']),'FU2701')
        self.assertEqual(err.exception.code,'CURRENT_BAR_UNVERIFIED')
        self.assertEqual({r['code'] for r in err.exception.reasons},{'VOLUME_ANCHOR'})

    def test_observation_cannot_remain_forming_after_period_end(self):
        from pathlib import Path
        from backend.market_simulator.reader import verify_observation
        fixture = json.loads((Path(__file__).parent/'fixtures/market_simulator/observations.json').read_text(encoding='utf-8'))
        with self.assertRaises(SimulatorError) as err:
            verify_observation(fixture['quote'],fixture['checkpoint'],fixture['closed_minutes'],
                               at('2026-10-19T09:30:00'),'JM2701')
        self.assertIn('PERIOD_IDENTITY',{r['code'] for r in err.exception.reasons})

    def test_collection_restore_boundary_and_http_lifecycle(self):
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.api import market_simulator_routes as api
        from backend.bar_engine.bar_engine import ws_manager
        redis = MemoryRedis()
        engine = source_engine(redis)
        cutoff = at('2026-10-21T13:47:05')
        with patch.object(ws_manager, 'broadcast_threadsafe'):
            engine.on_tick(source_tick('2026-10-21T13:44:59', 1000))
            restored = source_engine(redis)
            restored.restore_forming()
            for stamp, volume in [('13:45:04',1010), ('13:45:50',1020), ('13:46:04',1030), ('13:47:04',1040)]:
                restored.on_tick(source_tick('2026-10-21T'+stamp, volume))
            before = redis.writes
            restored.on_tick(source_tick('2026-10-21T13:47:04', 1040))
            self.assertEqual(redis.writes, before, 'duplicate source time must not refresh evidence')
        checkpoint = json.loads(redis.hget(restored.state_key, 'FU2701'))
        self.assertEqual(checkpoint['volume_anchor']['source_at'], '2026-10-21T13:44:59')
        self.assertEqual(checkpoint['volume_anchor']['source_record']['volume'], 1000)
        db = Mock()
        db.query.return_value.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value = []
        db.query.return_value.filter_by.return_value.first.return_value = SimpleNamespace(price_tick=1, volume_multiple=10)
        app = FastAPI(); app.include_router(api.router)
        client = TestClient(app); client.cookies.set(api.SESSION_COOKIE, 'synthetic-source-test')
        with patch.object(api, 'require_user', return_value=SimpleNamespace(username='source-test',role='sim')), \
             patch.object(api, 'clock', return_value=cutoff), \
             patch.object(api, 'store', api.SnapshotStore()), \
             patch.object(api, 'get_reader', side_effect=lambda: MarketReader(redis,lambda:db)), \
             patch('backend.storage.history_backfill.fetch_sina_minute_bars',
                   side_effect=lambda i,p: source_history(i,p,cutoff)) as fetch:
            response = client.post('/api/v1/market-simulator/snapshots',json={'instrument':'FU2701'})
            self.assertEqual(response.status_code,200,response.text)
            data = response.json()['data']; sid = data['snapshot_id']
            self.assertEqual(data['defaults']['observed_volume'],40)
            self.assertEqual(data['quality']['sequences']['60m']['label_semantics'], ['END'])
            self.assertEqual(data['quality']['sequences']['60m']['coverage'], ['UNKNOWN'])
            self.assertTrue(all(v['passed'] for v in data['observation_evidence'].values()))
            self.assertEqual(fetch.call_count,2)
            for body in ({'snapshot_id':sid,'use_defaults':True},
                         {'snapshot_id':sid,'simulate_price':'4801','volume_ratio':'1','open_interest_change':5}):
                self.assertEqual(client.post('/api/v1/market-simulator/simulate',json=body).status_code,200)
            self.assertEqual(fetch.call_count,2,'simulate must only use frozen history')
            self.assertEqual(client.delete('/api/v1/market-simulator/snapshots/'+sid).status_code,204)
            self.assertEqual(redis.writes,before,'snapshot and simulation must not write market storage')
            # Missing/corrupt old evidence must not be repaired from existing bar sums.
            for mode in ('missing','corrupt'):
                bad = deepcopy(checkpoint)
                if mode == 'missing':
                    bad.pop('volume_anchor')
                else:
                    bad['volume_anchor']['source_record']['volume'] += 1
                redis.hashes[restored.state_key]['FU2701'] = json.dumps(bad)
                api.store = api.SnapshotStore()
                failed = client.post('/api/v1/market-simulator/snapshots',json={'instrument':'FU2701'})
                self.assertEqual(failed.status_code,422,failed.text)
                self.assertEqual(failed.json()['detail']['code'],'CURRENT_BAR_UNVERIFIED')
                self.assertIn('VOLUME_ANCHOR',{r['code'] for r in failed.json()['detail']['reasons']})
                self.assertEqual(len(api.store._items),0)
        db.commit.assert_not_called(); db.add.assert_not_called()

    def test_history_missing_closed_bar_and_tail_are_rejected(self):
        cutoff = at('2026-10-21T14:00:00')
        for index in (-1,-3):
            rows = source_history('FU2701','15m',cutoff); rows.pop(index)
            with patch('backend.storage.history_backfill.fetch_sina_minute_bars',return_value=rows):
                with self.assertRaises(SimulatorError) as err:
                    MarketReader(Mock(),Mock()).load_bars('FU2701','15m',256,cutoff)
                self.assertEqual(err.exception.code,'HISTORY_GAP')

    def test_only_current_minute_window_reads_legacy_storage(self):
        redis = Mock(); db = Mock()
        redis.lrange.return_value = [json.dumps(dict(bar_time='2026-10-12T09:00:00',provenance_json=None))]
        db.query.return_value.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value=[]
        self.assertEqual(MarketReader(redis,lambda:db).load_bars('FU2701','1m',240,at('2026-10-21T13:47:00')),[])

    def test_original_quote_boundary_and_reset(self):
        from backend.common.bar_evidence import advance_sina_evidence, valid_volume_anchor
        previous = quote('2026-10-21T13:44:59', 1000)
        current = quote('2026-10-21T13:45:04', 1010)
        latest, anchor = advance_sina_evidence(previous, current, None)
        self.assertEqual(anchor['source_record'], previous)
        self.assertEqual(anchor['value'], 1000)
        self.assertEqual(anchor['source_at'], previous['timestamp'])
        self.assertTrue(valid_volume_anchor(anchor, at('2026-10-21T13:45:00'), current))
        current['volume'] = '999'
        current['timestamp'] = '2026-10-21T13:45:05'
        _, reset = advance_sina_evidence(latest, current, anchor)
        self.assertIsNone(reset)
        corrupted = deepcopy(anchor); corrupted['source_record']['volume'] = '999'
        self.assertFalse(valid_volume_anchor(corrupted, at('2026-10-21T13:45:00'), latest))

    def test_no_anchor_from_late_start_gap_or_previous_day(self):
        from backend.common.bar_evidence import advance_sina_evidence
        for previous, current in [
            (None, quote('2026-10-21T13:45:04')),
            (quote('2026-10-21T13:43:59'), quote('2026-10-21T13:45:04')),
            (quote('2026-10-21T13:44:59'), quote('2026-10-21T13:46:04')),
            (quote('2026-10-20T22:59:59', day='20261021'), quote('2026-10-21T13:30:04')),
            (quote('2026-10-21T14:59:59'), quote('2026-10-21T21:00:04', day='20261022')),
        ]:
            with self.subTest(previous=previous, current=current):
                self.assertIsNone(advance_sina_evidence(previous, current, None)[1])

    def test_trading_minute_history_spans_and_original_label(self):
        from backend.common.bar_evidence import sina_history_windows
        windows = sina_history_windows('FU2701', '20261021', '60m')
        span = windows[datetime(2026, 10, 21, 11, 15)]
        self.assertEqual(span['start_at'], datetime(2026, 10, 21, 10))
        self.assertEqual(len(span['segments']), 2)
        self.assertEqual(sum((b-a).total_seconds() for a,b in span['segments']), 3600)
        row = dict(bar_time='2026-10-21T11:15:00', trading_day='20261021',
                   provenance_json=dict(provider='sina', source='sina_history',
                       label_semantics='END', aggregation_clock='TRADING_MINUTES',
                       source_time='2026-10-21 11:15:00', response_sha256='a'*64,
                       request_url='https://stock2.finance.sina.com.cn/history', coverage='UNKNOWN'))
        original = deepcopy(row)
        normalized = normalize_bar(row, 'FU2701', '60m')
        self.assertEqual(normalized['end_at'], at('2026-10-21T11:15:00'))
        self.assertEqual(normalized['start_at'], at('2026-10-21T10:00:00'))
        self.assertEqual(normalized['source_bar_time'], at(row['bar_time']))
        self.assertEqual(normalized['provenance_json']['coverage'], 'UNKNOWN')
        self.assertEqual(row, original)
        row['bar_time'] = '2026-10-21T10:30:00'
        with self.assertRaises(SimulatorError):
            normalize_bar(row, 'FU2701', '60m')

    def test_night_midnight_and_partial_day_close_are_explicit(self):
        from backend.common.bar_evidence import sina_history_windows
        windows = sina_history_windows('FU2701','20261021','60m')
        afternoon = windows[datetime(2026,10,21,14,15)]
        self.assertEqual(afternoon['start_at'],datetime(2026,10,21,11,15))
        self.assertEqual(afternoon['segments'],[(datetime(2026,10,21,11,15),datetime(2026,10,21,11,30)),
                                              (datetime(2026,10,21,13,30),datetime(2026,10,21,14,15))])
        tail = windows[datetime(2026,10,21,15)]
        self.assertEqual((tail['end_at']-tail['start_at']).total_seconds(),45*60)
        midnight = sina_history_windows('AU2612','20261019','60m')[datetime(2026,10,17,0)]
        self.assertEqual(midnight['start_at'],datetime(2026,10,16,23))

    def test_higher_history_reads_source_without_legacy_storage(self):
        redis = Mock(); db = Mock(side_effect=AssertionError('legacy storage read'))
        reader = MarketReader(redis, db)
        with patch('backend.storage.history_backfill.fetch_sina_minute_bars', side_effect=RuntimeError('offline')):
            with self.assertRaises(SimulatorError) as err:
                reader.load_bars('FU2701', '15m', 256, at('2026-10-21T14:00:00'))
        self.assertEqual(err.exception.code, 'HISTORY_FETCH_FAILED_15M')
        db.assert_not_called(); redis.lrange.assert_not_called()


if __name__ == '__main__':
    unittest.main()
