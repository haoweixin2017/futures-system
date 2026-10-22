from tests import test_bootstrap  # noqa: F401
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from tests.test_market_simulator import snapshot, NOW
from backend.api import market_simulator_routes as api


class SimulatorApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI(); self.app.include_router(api.router)
        self.client = TestClient(self.app)
        self.client.cookies.set('cfs_session', 'fixture-session')
        def user(request):
            role = request.headers.get('X-Test-Role', 'sim')
            if role == 'none':
                raise HTTPException(401)
            return SimpleNamespace(username=request.headers.get('X-Test-User', 'alice'), role=role)
        self.auth = patch.object(api, 'require_user', user); self.auth.start(); self.addCleanup(self.auth.stop)
        self.clock = patch.object(api, 'clock', return_value=NOW); self.now = self.clock.start(); self.addCleanup(self.clock.stop)
        api.store = api.SnapshotStore()
        self.reader = SimpleNamespace(freeze=lambda *a: snapshot())
        self.readpatch = patch.object(api, 'get_reader', return_value=self.reader); self.readpatch.start(); self.addCleanup(self.readpatch.stop)

    def create(self):
        r = self.client.post('/api/v1/market-simulator/snapshots', json={'instrument':'JM2701'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers['cache-control'], 'no-store')
        return r.json()['data']['snapshot_id']

    def test_auth_and_no_reference(self):
        for role, expected in [('none',401),('live',403),('admin',403)]:
            r = self.client.post('/api/v1/market-simulator/snapshots', json={'instrument':'JM2701'}, headers={'X-Test-Role':role})
            self.assertEqual(r.status_code, expected)
        sid = self.create()
        r = self.client.post('/api/v1/market-simulator/simulate', json={'snapshot_id':sid,'use_defaults':True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['data']['current'],r.json()['data']['simulated'])
        with patch.object(api, 'get_reader', side_effect=AssertionError('simulate read market')):
            self.assertEqual(self.client.post('/api/v1/market-simulator/simulate',json={'snapshot_id':sid,'use_defaults':True}).status_code,200)

    def test_ownership_release_and_unknown(self):
        sid = self.create()
        for headers in ({'X-Test-User':'bob'},):
            r = self.client.post('/api/v1/market-simulator/simulate',json={'snapshot_id':sid,'use_defaults':True},headers=headers)
            self.assertEqual(r.status_code,404)
            self.assertIsNone(r.json()['detail']['context'])
        r = self.client.delete('/api/v1/market-simulator/snapshots/'+sid)
        self.assertEqual(r.status_code,204)
        self.assertEqual(self.client.delete('/api/v1/market-simulator/snapshots/'+sid).status_code,404)

    def test_input_and_error_context(self):
        sid = self.create()
        for body in ({'snapshot_id':sid,'simulate_price':'100.1','volume_ratio':'3','open_interest_change':0},
                     {'snapshot_id':sid,'use_defaults':True,'history':[1]}):
            r = self.client.post('/api/v1/market-simulator/simulate',json=body)
            self.assertEqual(r.status_code,422)
            # Valid baseline numerical failures provide server-owned context.
            if 'history' not in body:
                self.assertEqual(r.json()['detail']['context']['instrument'],'JM2701')
        r = self.client.post('/api/v1/market-simulator/snapshots',content=b'x'*8193)
        self.assertEqual(r.status_code,413)
        r = self.client.post('/api/v1/market-simulator/snapshots',json={'instrument':'JM0'})
        self.assertEqual(r.status_code,422)

    def test_reference_ownership_and_whitelist(self):
        from backend.market_simulator.reader import MarketReader
        from unittest.mock import Mock
        from datetime import timezone, timedelta
        import json
        run=SimpleNamespace(run_id='run',batch_id='batch',status='SUCCEEDED',call_count=1,
            completed_at=(NOW-timedelta(minutes=1)).astimezone(timezone.utc).replace(tzinfo=None),
            direction='LONG',parsed_response_json=json.dumps(dict(publication_status='REVIEWED',direction='LONG',
                session_chart=dict(forecast_start_time=(NOW-timedelta(minutes=1)).isoformat(),
                    forecast_end_time=(NOW+timedelta(minutes=9)).isoformat(),
                    session_end_time=(NOW+timedelta(minutes=20)).isoformat(),target_price=1320,invalidation_price=1270))))
        batch=SimpleNamespace(instrument_id='JM2701',snapshot_json='{"analysis_target":"CURRENT_SESSION"}')
        session=Mock()
        a,b=Mock(),Mock();a.filter_by.return_value.first.return_value=run;b.filter_by.return_value.first.return_value=batch
        session.query.side_effect=lambda model: a if model.__name__=='AIAnalysisRunModel' else b
        reader=MarketReader(Mock(),lambda:session)
        plan,reason=reader.reference('run','alice','JM2701',NOW)
        self.assertIsNone(reason);self.assertEqual(plan['target'],1320)
        self.assertNotIn('config_json',plan)
        b.filter_by.return_value.first.return_value=None
        with self.assertRaises(api.SimulatorError) as err:
            reader.reference('run','bob','JM2701',NOW)
        self.assertEqual(err.exception.status,404)
        b.filter_by.return_value.first.return_value=batch;run.status='PENDING'
        self.assertIsNone(reader.reference('run','alice','JM2701',NOW)[0])
        session.commit.assert_not_called(); session.add.assert_not_called()

    def test_protocol_errors_sessions_and_restart(self):
        from datetime import timedelta
        from unittest.mock import Mock
        for status, code in [(409,'SNAPSHOT_RACE'), (422,'CURRENT_BAR_UNVERIFIED'), (429,'SNAPSHOT_LIMIT')]:
            with self.subTest(status=status):
                api.store = api.SnapshotStore()
                self.reader.freeze = Mock(side_effect=api.SimulatorError(code,'隔离错误',status))
                r = self.client.post('/api/v1/market-simulator/snapshots',json={'instrument':'JM2701'})
                self.assertEqual(r.status_code,status)
                self.assertIsNone(r.json()['detail']['context']['frozen_at'])
                self.assertEqual(len(api.store._items),0)
        self.reader.freeze = Mock(side_effect=ConnectionError('storage unavailable'))
        api.store = api.SnapshotStore()
        self.assertEqual(self.client.post('/api/v1/market-simulator/snapshots',json={'instrument':'JM2701'}).status_code,503)
        self.assertEqual(len(api.store._items),0)
        self.reader.freeze = lambda *a:snapshot()
        api.store = api.SnapshotStore()
        sid = self.create()
        self.client.cookies.set('cfs_session','different-session')
        for method,path,body in [('POST','/simulate',{'snapshot_id':sid,'use_defaults':True}),('DELETE','/snapshots/'+sid,None)]:
            r=self.client.request(method,'/api/v1/market-simulator'+path,json=body)
            self.assertEqual(r.status_code,404);self.assertIsNone(r.json()['detail']['context'])
        self.client.cookies.set('cfs_session','fixture-session')
        self.now.return_value=NOW+timedelta(minutes=30)
        self.assertEqual(self.client.delete('/api/v1/market-simulator/snapshots/'+sid).status_code,404)
        self.now.return_value=NOW;api.store=api.SnapshotStore();sid=self.create()
        api.store=api.SnapshotStore()  # New process has no snapshot identity records.
        self.assertEqual(self.client.post('/api/v1/market-simulator/simulate',json={'snapshot_id':sid,'use_defaults':True}).status_code,404)

    def test_real_reader_malformed_checkpoint_http(self):
        from backend.market_simulator.reader import MarketReader
        from unittest.mock import Mock
        import json
        from pathlib import Path
        f=json.loads((Path(__file__).parent/'fixtures/market_simulator/observations.json').read_text(encoding='utf-8'))
        redis=Mock(spec=['hgetall','hget']);redis.hgetall.return_value=f['quote']
        reader=MarketReader(redis,Mock())
        reader.metadata=lambda _:dict(tick=api.Decimal('.5'))
        reader.load_bars=lambda instrument,period,*a:f['closed_minutes'] if period=='1m' else []
        reader.reference=lambda *a:(None,'NO_REFERENCE_PLAN')
        self.readpatch.stop()
        with patch.object(api,'get_reader',return_value=reader):
            import copy
            negative=copy.deepcopy(f['checkpoint']);negative['minute']['open_interest']='-1'
            f['quote']['open_interest']='-1'
            for raw in ('{bad','null','[]',json.dumps({'minute':{'bar_time':None}}),json.dumps(negative)):
                api.store=api.SnapshotStore();redis.hget.return_value=raw
                r=self.client.post('/api/v1/market-simulator/snapshots',json={'instrument':'JM2701'})
                self.assertEqual(r.status_code,422,r.text)
                self.assertEqual(r.json()['detail']['code'],'CURRENT_BAR_UNVERIFIED')
                self.assertIsNone(r.json()['detail']['context']['frozen_at'])
                self.assertEqual(len(api.store._items),0)

    def test_quality_summary_preserves_unknown_and_partial(self):
        s=snapshot()
        s['observed_bar']['provenance_json']=dict(provider='sina',source='synthetic_fixture',label_semantics='START',coverage='PARTIAL')
        s['history_15m']=[];s['history_60m']=[];s['observed_bar']['open_interest']=None
        data=api.snapshot_response(s,NOW)
        quality=data['quality']
        self.assertEqual(quality['sequences']['current']['coverage'],['PARTIAL'])
        self.assertEqual(quality['sequences']['15m']['count'],0)
        self.assertIn('PREVIOUS_OI_UNKNOWN',{v['code'] for v in quality['evaluation_reasons']})
        self.assertIsNone(data['current']['long']['score'])
        self.assertNotIn('history_15m',data)

    def test_forbidden_side_effects_full_lifecycle(self):
        # Bootstrap/isolated imports happen before request counters. No production state.
        from contextlib import ExitStack
        from unittest.mock import Mock
        from backend.signal_engine.signal_engine import SignalEngine
        from backend.trade_engine.paper_accounts import paper_accounts
        from backend.notifier.notifier import notifier
        from backend.ai_analysis import provider
        from backend.collector.sina_collector import SinaRealtimeCollector
        from sqlalchemy.orm import Session
        import redis
        targets=[(SignalEngine,'_save_signal'),(paper_accounts,'get'),(paper_accounts,'on_tick'),
                 (paper_accounts,'on_signal_triggered'),(notifier,'notify'),
                 (provider,'chat_completion'),(provider,'gemini_generate_content'),
                 (SinaRealtimeCollector,'start')]
        with ExitStack() as stack:
            guards=[]
            def forbid(obj,name):
                guards.append(stack.enter_context(patch.object(obj,name,side_effect=AssertionError('forbidden '+name))))
            for obj,name in targets:forbid(obj,name)
            for name in ('add','delete','commit','flush'):forbid(Session,name)
            # Any real Redis/network command is forbidden after test initialization.
            forbid(redis.Redis,'execute_command')
            for target in ('backend.storage.history_backfill.ensure_symbol_backfilled',
                           'backend.storage.history_backfill.repair_minute_gaps',
                           'backend.ai_analysis.orchestrator.submit_batch',
                           'backend.ai_analysis.orchestrator._provider_completion'):
                guards.append(stack.enter_context(patch(target,side_effect=AssertionError('forbidden '+target))))
            # Real calculation/API paths, fixture reader only; no production dependencies.
            sid=self.create()
            for body in ({'snapshot_id':sid,'simulate_price':'102','volume_ratio':'3','open_interest_change':1},
                         {'snapshot_id':sid,'use_defaults':True}):
                self.assertEqual(self.client.post('/api/v1/market-simulator/simulate',json=body).status_code,200)
            r=self.client.post('/api/v1/market-simulator/simulate',json={'snapshot_id':sid,'simulate_price':'100.1','volume_ratio':'3','open_interest_change':0})
            self.assertEqual(r.status_code,422)
            self.assertEqual(self.client.delete('/api/v1/market-simulator/snapshots/'+sid).status_code,204)
            from datetime import timedelta
            self.now.return_value=NOW+timedelta(seconds=2)
            new_sid=self.create()
            self.assertNotEqual(sid,new_sid)
            self.assertEqual(self.client.delete('/api/v1/market-simulator/snapshots/'+new_sid).status_code,204)
            # Also run the actual reader against read-only Redis/SQL doubles, not
            # only freeze() fixtures: this catches writes in the create adapter.
            from backend.market_simulator.reader import MarketReader
            from pathlib import Path
            import json
            f=json.loads((Path(__file__).parent/'fixtures/market_simulator/observations.json').read_text(encoding='utf-8'))
            readredis=Mock(spec=['hgetall','hget','lrange','hset','set','delete','lpush','rpush','publish','expire'])
            readredis.hgetall.return_value=f['quote'];readredis.hget.return_value=json.dumps(f['checkpoint'])
            readredis.lrange.side_effect=lambda key,*args:[json.dumps(v) for v in f['closed_minutes']] if key.endswith(':1m') else []
            db=Mock()
            db.query.return_value.filter_by.return_value.first.return_value=SimpleNamespace(price_tick='.5',volume_multiple=100)
            db.query.return_value.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value=[]
            for obj,names in [(readredis,('hset','set','delete','lpush','rpush','publish','expire')),(db,('add','delete','commit','flush'))]:
                for name in names:
                    guard=getattr(obj,name);guard.side_effect=AssertionError('forbidden '+name);guards.append(guard)
            self.now.return_value=NOW+timedelta(seconds=4)
            from tests.test_simulator_source_evidence import source_history
            stack.enter_context(patch('backend.storage.history_backfill.fetch_sina_minute_bars',
                side_effect=lambda instrument,period: source_history(instrument,period,NOW)))
            with patch.object(api,'get_reader',return_value=MarketReader(readredis,lambda:db)):
                real_sid=self.create()
                self.assertEqual(self.client.post('/api/v1/market-simulator/simulate',json={'snapshot_id':real_sid,'use_defaults':True}).status_code,200)
                self.assertEqual(self.client.delete('/api/v1/market-simulator/snapshots/'+real_sid).status_code,204)
            self.assertGreater(db.query.call_count,0)
            for guard in guards:guard.assert_not_called()

