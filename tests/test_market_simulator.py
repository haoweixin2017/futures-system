import unittest
from datetime import datetime, timedelta, timezone
from pydantic import ValidationError
from backend.market_simulator.models import ScenarioInput, SimulatorError
from backend.market_simulator.store import SnapshotStore
from backend.market_simulator.service import compare, constraints, presets, reference_ratio
from backend.market_simulator.reader import verify_observation, MarketReader, normalize_bar
from decimal import Decimal
from copy import deepcopy


def snapshot():
    history = [dict(open_price='100', high_price='101', low_price='99', close_price='100',
                    volume=3, open_interest='1000') for _ in range(21)]
    current = dict(open_price='100', high_price='101', low_price='99', close_price='100',
                   volume=7, open_interest='1001', turnover=None, hypothetical=False)
    return dict(snapshot_id='sample', instrument='JM2701', feed='sina', timeframe='15m',
                trading_day='20261019', frozen_at=NOW, quote_at=NOW,
                expires_at=NOW+timedelta(minutes=30), session_end=NOW+timedelta(minutes=58),
                rules_version='15m-v1', schema_version=1, history_15m=history,
                history_60m=deepcopy(history)*3, observed_bar=current,
                tick=Decimal('0.5'), upper_limit=None, lower_limit=None, reference_plan=None,
                quality=dict(warnings=[], blocking_reasons=[]))


NOW = datetime(2026, 10, 19, 9, 17, tzinfo=timezone(timedelta(hours=8)))


class StoreModelTests(unittest.TestCase):
    def test_invalid_inputs(self):
        for price in ('NaN', 'Infinity', '-1', '0', '1e2', '1.'+'1'*13, '9'*33, 100):
            with self.subTest(price=price), self.assertRaises(ValidationError):
                ScenarioInput(snapshot_id='x', simulate_price=price, volume_ratio='1', open_interest_change=0)
        for delta in (1.5, True, '3'):
            with self.assertRaises(ValidationError):
                ScenarioInput(snapshot_id='x', simulate_price='100', volume_ratio='1', open_interest_change=delta)
        with self.assertRaises(ValidationError):
            ScenarioInput(snapshot_id='x', use_defaults=True, simulate_price='100')
        with self.assertRaises(ValidationError):
            ScenarioInput(snapshot_id='x', use_defaults=True, arbitrary=1)

    def test_copy_ownership_expiry_release(self):
        store = SnapshotStore()
        value = {'frozen_at': NOW, 'data': [1]}
        sid = store.put('alice', 'session', value, NOW)
        value['data'].append(2)
        store.get('alice', 'session', sid, NOW)['data'].append(3)
        self.assertEqual(store.get('alice', 'session', sid, NOW)['data'], [1])
        for user, session in [('bob', 'session'), ('alice', 'other')]:
            with self.assertRaises(SimulatorError) as err:
                store.get(user, session, sid, NOW)
            self.assertEqual(err.exception.status, 404)
        store.delete('alice', 'session', sid, NOW)
        with self.assertRaises(SimulatorError):
            store.delete('alice', 'session', sid, NOW)
        sid = store.put('alice', 'session', value, NOW)
        for _ in range(2):
            with self.assertRaises(SimulatorError) as err:
                store.get('alice', 'session', sid, NOW+timedelta(minutes=30))
            self.assertEqual(err.exception.status, 404)
        self.assertEqual(len(store._items), 0)

    def test_limits_and_rate(self):
        store = SnapshotStore(max_total=5)
        for _ in range(4):
            store.put('a', 's', {}, NOW)
        with self.assertRaises(SimulatorError) as err:
            store.put('a', 's', {}, NOW)
        self.assertEqual(err.exception.status, 429)
        store.rate('a', 's', 'create', NOW)
        with self.assertRaises(SimulatorError):
            store.rate('a', 's', 'create', NOW)
        for _ in range(10):
            store.rate('a', 's', 'simulate', NOW)
        with self.assertRaises(SimulatorError):
            store.rate('a', 's', 'simulate', NOW)
        store.rate('a', 's', 'create', NOW+timedelta(seconds=2))

    def test_memory_concurrency_and_rate_reclamation(self):
        from concurrent.futures import ThreadPoolExecutor
        tiny=SnapshotStore(max_bytes=100)
        with self.assertRaises(SimulatorError):
            tiny.put('a','s',{'value':'x'*1000},NOW)
        self.assertEqual(len(tiny._items),0)
        store=SnapshotStore()
        def create(_):
            try:return store.put('a','s',{},NOW)
            except SimulatorError:return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            created=list(pool.map(create,range(12)))
        self.assertEqual(sum(v is not None for v in created),4)
        for i in range(30):store.rate(str(i),'s','create',NOW)
        store.rate('next','s','create',NOW+timedelta(seconds=2))
        self.assertEqual(len(store._rates),1)


class ScenarioTests(unittest.TestCase):
    def test_malformed_source_is_business_error(self):
        for stamp in (None, 'bad-time', 123):
            with self.subTest(stamp=stamp):
                with self.assertRaises(SimulatorError) as err:
                    normalize_bar(dict(bar_time=stamp, provenance_json=dict(provider='sina', label_semantics='START')), 'JM2701', '15m')
                self.assertEqual(err.exception.status, 422)

    def test_override_rounding_boundary(self):
        s = snapshot()
        for b in s['history_15m']:
            b['volume'] = 1000
        s['observed_bar']['volume'] = 1200
        request = dict(snapshot_id='s', simulate_price='102', open_interest_change=0)
        self.assertEqual(compare(s, ScenarioInput(**request, volume_ratio='1.1996'), NOW)['effective_values']['volume'], 1200)
        with self.assertRaises(SimulatorError):
            compare(s, ScenarioInput(**request, volume_ratio='1.1994'), NOW)

    def test_invalid_history_numbers_and_metadata(self):
        from unittest.mock import Mock
        import json
        bar=dict(instrument='JM2701',period='15m',trading_day='20261019',bar_time='2026-10-19T09:00:00+08:00',
            open_price='100',high_price='101',low_price='99',close_price='100',volume=3,open_interest=None,
            provenance_json=dict(provider='sina',label_semantics='START'))
        db=Mock();db.query.return_value.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value=[]
        redis=Mock();reader=MarketReader(redis,lambda:db)
        for field,value in [('high_price','bad'),('volume','bad'),('volume',1.5),('open_interest','bad'),('bar_time',None)]:
            redis.lrange.return_value=[json.dumps(dict(bar,**{field:value}))]
            with self.subTest(field=field,value=value),self.assertRaises(SimulatorError) as err:
                reader.load_storage_bars('JM2701','15m',256,NOW)
            self.assertEqual(err.exception.status,422)
        from types import SimpleNamespace
        db.query.return_value.filter_by.return_value.first.return_value=SimpleNamespace(price_tick='bad')
        with self.assertRaises(SimulatorError) as err:
            reader.metadata('JM2701')
        self.assertEqual(err.exception.code,'INVALID_PRICE_TICK')

    def test_reference_ratio_boundaries(self):
        plan = dict(direction='LONG', target=Decimal('1320'), stop=Decimal('1270'), valid_from=NOW-timedelta(minutes=1), valid_until=NOW+timedelta(minutes=1))
        for direction, target, stop, entry, expected in [
            ('LONG',1320,1270,1280,'4.00'),('LONG',1320,1270,1300,'0.67'),
            ('LONG',1320,1270,1270,None),('LONG',1320,1270,1320,None),
            ('LONG',1320,1270,1330,None),('LONG',1320,1270,1260,None),
            ('SHORT',1270,1320,1310,'4.00'),('SHORT',1270,1320,1290,'0.67'),
            ('SHORT',1270,1320,1270,None),('SHORT',1270,1320,1320,None),
            ('SHORT',1270,1320,1260,None),('SHORT',1270,1320,1330,None),
        ]:
            p=dict(plan, direction=direction,target=Decimal(target),stop=Decimal(stop))
            with self.subTest(direction=direction, entry=entry):
                r=reference_ratio(p,Decimal(entry),direction,NOW)
                self.assertEqual(str(r['ratio']) if r['ratio'] is not None else None, expected)
        self.assertEqual(reference_ratio(plan,Decimal(1280),'SHORT',NOW)['reason_code'],'DIRECTION_CONFLICT')
        self.assertEqual(reference_ratio(plan,Decimal(1280),'NEUTRAL',NOW)['reason_code'],'NO_DIRECTION')
        self.assertEqual(reference_ratio(plan,Decimal(1280),'LONG',NOW+timedelta(minutes=1))['reason_code'],'REFERENCE_EXPIRED')
        boundary=dict(plan,target=Decimal('125'),stop=Decimal('100'))
        self.assertTrue(reference_ratio(boundary,Decimal('110'),'LONG',NOW)['meets_threshold'])
        self.assertFalse(reference_ratio(boundary,Decimal('110.000001'),'LONG',NOW)['meets_threshold'])

    def test_slider_more_boundaries(self):
        for tick in ('0.5','0.02'):
            s=snapshot();s['tick']=Decimal(tick)
            c=constraints(s)
            self.assertEqual(c['slider_min']%s['tick'],0)
            self.assertEqual(c['slider_max']%s['tick'],0)
        for lower, upper in [('103','103'),('104','103')]:
            s=snapshot();s.update(lower_limit=Decimal(lower),upper_limit=Decimal(upper))
            self.assertFalse(constraints(s)['slider_available'])
        s=snapshot()
        for b in s['history_15m']:
            b.update(high_price='100',low_price='100')
        self.assertFalse(constraints(s)['slider_available'])

    def test_defaults_and_input_immutability(self):
        for price in (98,99,100,101,102):
            for qty in (0,1,7,9):
                s=snapshot();s['observed_bar'].update(close_price=str(price),volume=qty,
                    high_price=str(max(101,price)),low_price=str(min(99,price)))
                before = deepcopy(s)
                r = compare(s, ScenarioInput(snapshot_id='sample', use_defaults=True), NOW)
                self.assertEqual(r['current'], r['simulated'])
                self.assertEqual(r['score_changes'], {'long': 0, 'short': 0})
                self.assertEqual(r['effective_values']['volume'],qty)
                self.assertEqual(s,before)

    def test_overrides_and_unknown_oi(self):
        s = snapshot()
        req = ScenarioInput(snapshot_id='sample', simulate_price='102', volume_ratio='3', open_interest_change=5)
        r = compare(s, req, NOW)
        self.assertEqual(r['simulated']['long']['score'], 100)
        self.assertEqual(r['effective_values']['high_price'], Decimal('102'))
        for side in ('long', 'short'):
            self.assertEqual(sum(c['points_delta'] for c in r['changes'] if c['direction'] == side), r['score_changes'][side])
        s['observed_bar']['open_interest'] = None
        self.assertIsNone(compare(s, req, NOW)['current']['long']['score'])
        self.assertEqual(compare(s, req, NOW)['simulated']['long']['score'], 100)
        s['history_15m'][-1]['open_interest'] = None
        with self.assertRaises(SimulatorError) as err:
            compare(s, req, NOW)
        self.assertEqual(err.exception.code, 'OI_BASELINE_UNAVAILABLE')
        self.assertIsNone(compare(s, ScenarioInput(snapshot_id='s', use_defaults=True), NOW)['current']['long']['score'])

    def test_rejected_numbers_and_lower_volume(self):
        s = snapshot()
        for price, ratio, delta in [('100.1', '3', 0), ('100', '0.1', 0), ('100', '3', -2000)]:
            with self.assertRaises(SimulatorError):
                compare(s, ScenarioInput(snapshot_id='s', simulate_price=price, volume_ratio=ratio, open_interest_change=delta), NOW)

    def test_slider_and_presets(self):
        s = snapshot()
        c = constraints(s)
        self.assertEqual((c['slider_min'], c['slider_max']), (Decimal('90'), Decimal('110')))
        s.update(lower_limit=Decimal('98.1'), upper_limit=Decimal('103.2'))
        c = constraints(s)
        self.assertEqual((c['slider_min'], c['slider_max']), (Decimal('98.5'), Decimal('103')))
        self.assertEqual(len(presets(s)), 6)
        self.assertFalse(next(p for p in presets(s) if p['code'] == 'LOW_VOLUME')['available'])
        for count in (0, 14):
            s['history_15m'] = s['history_15m'][:count]
            self.assertFalse(constraints(s)['slider_available'])

    def test_observation_all_six_and_rejections(self):
        import json
        from pathlib import Path
        fixture = json.loads((Path(__file__).parent/'fixtures/market_simulator/observations.json').read_text(encoding='utf-8'))
        q, state, minutes = fixture['quote'], fixture['checkpoint'], fixture['closed_minutes']
        result, evidence = verify_observation(q, state, minutes, NOW, 'JM2701')
        self.assertEqual(result['volume'], 350)
        self.assertTrue(all(v['passed'] for v in evidence.values()))
        for quote_oi, minute_oi in [(None,'1001'),('1001',None),(None,None),('0','0')]:
            q2,s2=deepcopy(q),deepcopy(state)
            q2['open_interest']=quote_oi;s2['minute']['open_interest']=minute_oi
            allowed,proof=verify_observation(q2,s2,minutes,NOW,'JM2701')
            self.assertTrue(all(v['passed'] for v in proof.values()))
            self.assertEqual(allowed['open_interest'],Decimal(minute_oi) if minute_oi is not None else None)
        for quote_oi, minute_oi in [('-1','-1'),('-1',None),(None,'-1')]:
            q2,s2=deepcopy(q),deepcopy(state)
            q2['open_interest']=quote_oi;s2['minute']['open_interest']=minute_oi
            with self.subTest(quote_oi=quote_oi,minute_oi=minute_oi):
                with self.assertRaises(SimulatorError) as err:
                    verify_observation(q2,s2,minutes,NOW,'JM2701')
                self.assertEqual(err.exception.code,'CURRENT_BAR_UNVERIFIED')
        for mutation in ('anchor', 'first', 'volume', 'price', 'day', 'unknown'):
            q2, s2, m2 = deepcopy(q), deepcopy(state), deepcopy(minutes)
            if mutation == 'anchor': s2.pop('volume_anchor')
            if mutation == 'first': m2.pop(0)
            if mutation == 'volume': q2['volume'] += 1
            if mutation == 'price': q2['last_price'] = '105'
            if mutation == 'day': s2['minute']['trading_day'] = '20261016'
            if mutation == 'unknown': m2[0]['provenance_json'] = None
            with self.subTest(mutation=mutation), self.assertRaises(SimulatorError):
                verify_observation(q2, s2, m2, NOW, 'JM2701')

    def test_reader_freeze_race_and_source(self):
        import json
        from pathlib import Path
        from unittest.mock import Mock
        fixture=json.loads((Path(__file__).parent/'fixtures/market_simulator/observations.json').read_text(encoding='utf-8'))
        redis=Mock(spec=['hgetall','hget'])
        redis.hgetall.return_value=fixture['quote'];redis.hget.return_value=json.dumps(fixture['checkpoint'])
        reader=MarketReader(redis,Mock(side_effect=AssertionError('unexpected database')))
        reader.metadata=lambda i:dict(tick=Decimal('.5'),upper_limit=None,lower_limit=None)
        reader.reference=lambda *a:(None,'NO_REFERENCE_PLAN')
        reader.load_bars=lambda i,p,l,c:fixture['closed_minutes'] if p=='1m' else snapshot()['history_'+p]
        result=reader.freeze('JM2701','alice',None,NOW)
        self.assertEqual(result['observed_bar']['volume'],350)
        redis.hget.side_effect=['first','second']*3
        with self.assertRaises(SimulatorError) as err:
            reader.freeze('JM2701','alice',None,NOW)
        self.assertEqual(err.exception.code,'SNAPSHOT_RACE')
        redis.hget.side_effect=None
        for stamp in ('2026-10-19T09:16:00+08:00','2026-10-19T09:18:00+08:00'):
            redis.hgetall.return_value=dict(fixture['quote'],timestamp=stamp)
            with self.assertRaises(SimulatorError):
                reader.freeze('JM2701','alice',None,NOW)

    def test_history_unknown_labels_rejected(self):
        for p in (None,{},dict(provider='sina',label_semantics='UNKNOWN'),dict(provider='other',label_semantics='START')):
            with self.assertRaises(SimulatorError):
                normalize_bar(dict(bar_time=NOW.isoformat(),provenance_json=p),'JM2701','15m')

    def test_unknown_history_error_locates_record_without_inventing_source(self):
        from unittest.mock import Mock
        from types import SimpleNamespace
        bar = dict(bar_time=datetime(2026, 10, 12, 9), trading_day='20261012',
                   open_price=100, high_price=101, low_price=99, close_price=100,
                   volume=10, open_interest=1000, provenance_json=None)
        session = Mock()
        session.query.return_value.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value = [SimpleNamespace(**bar)]
        redis = Mock()
        redis.lrange.return_value = []
        reader = MarketReader(redis, lambda: session)
        with self.assertRaises(SimulatorError) as err:
            reader.load_storage_bars('FU2701', '15m', 256, NOW)
        self.assertEqual(err.exception.code, 'BAR_PROVENANCE_UNAVAILABLE')
        for value in ('FU2701', '15m', '2026-10-12 09:00:00', 'MySQL', '重新采集'):
            self.assertIn(value, err.exception.message)
        self.assertIsNone(bar['provenance_json'])
        redis.hset.assert_not_called()
        session.commit.assert_not_called()

    def test_end_bar_cannot_cross_break(self):
        with self.assertRaises(SimulatorError):
            normalize_bar(dict(bar_time='2026-10-19T10:30:00+08:00',trading_day='20261019',
                               provenance_json=dict(provider='sina',label_semantics='END')),'JM2701','60m')

    def test_missing_tail_bar_rejected(self):
        from unittest.mock import Mock
        import json
        b=dict(instrument='JM2701',period='15m',trading_day='20261019',bar_time='2026-10-16T22:45:00+08:00',
               open_price='100',high_price='101',low_price='99',close_price='100',volume=100,open_interest=1000,
               provenance_json=dict(provider='sina',label_semantics='START'))
        session=Mock();session.query.return_value.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value=[]
        redis=Mock();redis.lrange.return_value=[json.dumps(b)]
        reader=MarketReader(redis,lambda:session)
        with self.assertRaises(SimulatorError) as err:
            reader.load_storage_bars('JM2701','15m',256,NOW.replace(minute=15))
        self.assertEqual(err.exception.code,'HISTORY_GAP')

