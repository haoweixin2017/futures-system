import unittest
from datetime import datetime
from unittest.mock import patch
from tests.test_simulator_source_evidence import MemoryRedis, source_engine, source_tick


class VolumeQualityTests(unittest.TestCase):
    def test_cache_quarantine_keeps_its_original_source_and_hash(self):
        from scripts.quarantine_volume_allocation import sync_cached_quality
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        import json
        cached=dict(instrument='FU2701',period='1m',trading_day='20261021',bar_time='2026-10-21T11:06:00',volume=190474,
                    provenance_json=dict(provider='sina',source='sina_history_repair',volume_source='sina_tick_aggregate',
                                         label_semantics='START',response_sha256='CACHE_HASH'))
        row=SimpleNamespace(instrument_id='FU2701',period='1m',trading_day='20261021',bar_time=datetime(2026,10,21,11,6),
                            provenance_json=json.dumps(dict(volume_quality='UNVERIFIED_ALLOCATION',volume_gap={},response_sha256='DB_HASH')))
        redis=MagicMock();pipe=redis.pipeline.return_value.__enter__.return_value;pipe.lrange.return_value=[json.dumps(cached)]
        self.assertEqual(sync_cached_quality(redis,'bars:FU2701:1m',row),1)
        updated=json.loads(pipe.lset.call_args.args[2])
        self.assertEqual(updated['provenance_json']['response_sha256'],'CACHE_HASH')
        self.assertEqual(updated['provenance_json']['source'],'sina_history_repair')
        self.assertEqual(updated['volume'],190474)

    def test_remote_same_slot_baseline_cannot_use_old_quarantined_samples(self):
        from backend.indicator_engine.realtime_analyzer import RealtimeAnalyzer
        from backend.models.market_data import BarData
        from datetime import timedelta
        bars=[]
        for i in range(5):
            bars.append(BarData(instrument='FU2701',period='5m',trading_day=f'202610{12+i:02d}',
                bar_time=datetime(2026,10,12+i,9),open_price=100,high_price=101,low_price=99,close_price=100,
                volume=190474 if i<3 else 100,turnover=0,open_interest=200,
                provenance_json={'volume_quality':'UNVERIFIED_ALLOCATION'} if i<3 else None))
        for i in range(25):
            bars.append(bars[-1].model_copy(update={'bar_time':datetime(2026,10,19,10)+timedelta(minutes=i),
                'trading_day':'20261019','provenance_json':None,'volume':100}))
        bars.append(bars[-1].model_copy(update={'bar_time':datetime(2026,10,21,9),'trading_day':'20261021'}))
        ratio,baseline,quality=RealtimeAnalyzer._volume_ratio(bars,len(bars)-1)
        self.assertIsNone(ratio)
        self.assertEqual(quality,'UNVERIFIED_VOLUME')

    def test_unknown_volume_blocks_rules_and_indicators(self):
        from backend.models.market_data import BarData
        from backend.indicator_engine.indicators import IndicatorCalculator
        from backend.signal_engine.rules import evaluate_rules
        from backend.signal_engine.signal_engine import SignalEngine
        from backend.indicator_engine.realtime_analyzer import RealtimeAnalyzer
        from unittest.mock import Mock
        bars=[BarData(instrument='FU2701',period='15m',trading_day='20261021',bar_time=datetime(2026,10,21,11),
            open_price=100,high_price=101,low_price=99,close_price=100,volume=100,turnover=1000,open_interest=200) for _ in range(21)]
        bars[5].provenance_json={'volume_quality':'UNVERIFIED_ALLOCATION'}
        for policy in ('STRICT','LEGACY'):
            self.assertIsNone(evaluate_rules(bars[-1],bars[:-1],[],policy)['long']['score'])
        engine=SignalEngine.__new__(SignalEngine);engine._save_signal=Mock()
        self.assertIsNone(engine.evaluate_15m_signals('FU2701',bars,[]))
        engine._save_signal.assert_not_called()
        self.assertIsNone(IndicatorCalculator.compute_all_indicators(bars)['volume_ratio'])
        self.assertIsNone(IndicatorCalculator.calculate_vwap(bars))
        self.assertIsNone(RealtimeAnalyzer._volume_ratio(bars,20)[0])

    def test_bars_api_masks_invalid_volume_from_database(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from backend.api import routes
        row=SimpleNamespace(instrument_id='FU2701',period='1m',trading_day='20261021',bar_time=datetime(2026,10,21,11,6),
            open_price=100,high_price=101,low_price=99,close_price=100,volume=190474,turnover=0,
            open_interest=200,open_interest_change=0,provenance_json='{"volume_quality":"UNVERIFIED_ALLOCATION"}')
        db=Mock();db.query.return_value.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value=[row]
        redis=Mock();redis.lrange.return_value=[];redis.hget.return_value=None
        with patch.object(routes,'_market_feed',return_value='sina'),patch.object(routes,'SessionLocal',return_value=db),patch.object(routes,'redis_client',redis):
            result=routes.get_bars(Mock(),'FU2701','1m',1000,'20261021')
        self.assertIsNone(result['bars'][0]['volume'])
        self.assertEqual(result['bars'][0]['volume_raw'],190474)
        self.assertEqual(row.volume,190474)

    def test_resume_does_not_allocate_gap_to_first_minute(self):
        from backend.bar_engine.bar_engine import ws_manager
        from backend.common.volume_quality import volume_usable
        redis=MemoryRedis(); engine=source_engine(redis)
        with patch.object(ws_manager,'broadcast_threadsafe'):
            engine.on_tick(source_tick('2026-10-21T09:59:50',1000))
            current=engine.on_tick(source_tick('2026-10-21T11:06:10',191474))
            self.assertEqual(current.volume,0)
            self.assertFalse(volume_usable(current))
            self.assertEqual(current.provenance_json['volume_gap']['unallocated_volume'],190474)
            current=engine.on_tick(source_tick('2026-10-21T11:06:40',191484))
            self.assertEqual(current.volume,10)
            self.assertFalse(volume_usable(current),'new ticks must not erase unknown minute quality')
            engine.on_tick(source_tick('2026-10-21T11:07:10',191494))
            higher=engine.synthetic_bars['FU2701']['15m']
            self.assertFalse(volume_usable(higher),'derived period must preserve invalid allocation')

    def test_break_without_missing_trading_minutes_is_not_a_gap(self):
        from backend.common.volume_quality import missing_trading_minutes
        self.assertFalse(missing_trading_minutes(datetime(2026,10,21,11,29,59),datetime(2026,10,21,13,30,2),'FU2701'))
        self.assertTrue(missing_trading_minutes(datetime(2026,10,21,10,14,59),datetime(2026,10,21,11,6,2),'FU2701'))

    def test_bad_raw_value_is_audited_but_never_presented_as_known(self):
        from backend.common.volume_quality import public_volume_bar,volume_usable
        raw=dict(volume=190474,provenance_json=dict(volume_quality='UNALLOCATED_GAP',volume_gap={'unallocated_volume':190474}))
        public=public_volume_bar(raw)
        self.assertIsNone(public['volume'])
        self.assertEqual(public['volume_raw'],190474)
        self.assertFalse(volume_usable(raw))
        self.assertEqual(raw['volume'],190474)


if __name__=='__main__':unittest.main()
