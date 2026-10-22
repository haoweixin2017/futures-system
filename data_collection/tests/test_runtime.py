import json
from pathlib import Path

import httpx
import pytest
import yaml

from test_pipeline import ROOT


class Clock:
    def __init__(self): self.value = 0
    def now(self): return self.value
    def sleep(self, delay): self.value += delay


def network():
    net=yaml.safe_load((ROOT/'config.example.yaml').read_text(encoding='utf-8'))['network']
    net['rate_state']=None  # MockTransport only; persistent limiter tested separately.
    return net


@pytest.mark.parametrize('fault', ['offline', 'timeout', '429'])
def test_transport_retries_and_preserves_minimum_interval(fault):
    from data_collection.transport import HTTP
    calls, logs = [], []
    clock = Clock()
    def handle(request):
        calls.append((request.url.path, clock.now(), request.headers.get('User-Agent')))
        if request.url.path == '/robots.txt': return httpx.Response(200, text='User-agent: *\nAllow: /')
        if len([r for r in calls if r[0] == '/data']) < 3:
            if fault == 'offline': raise httpx.ConnectError('secret must not enter logs')
            if fault == 'timeout': raise httpx.ReadTimeout('secret must not enter logs')
            return httpx.Response(429, headers={'Retry-After': '30'})
        return httpx.Response(200, text='ok')
    net = network()
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        wire = HTTP(net, client=client, now=clock.now, sleep=clock.sleep)
        assert wire.request({'base_url': 'https://official.example', 'request': {'method': 'GET', 'path': '/data'}}, {}, logs.append) == 'ok'
    assert len(calls) == 4
    assert all(b[1] - a[1] >= 8 for a,b in zip(calls, calls[1:]))
    assert all(row[2] for row in calls)
    assert len([row for row in logs if row['status'] == 'RETRY']) == 2
    assert 'secret' not in json.dumps(logs)


def test_robots_denial_never_fetches_data():
    from data_collection.transport import HTTP
    seen=[]; clock=Clock()
    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(200, text='User-agent: *\nDisallow: /data')
    net=network()
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError, match='ROBOTS_DENIED'):
            HTTP(net, client=client, now=clock.now, sleep=clock.sleep).request({'base_url':'https://official.example','request':{'method':'GET','path':'/data'}}, {}, lambda row: None)
    assert seen == ['/robots.txt']


def config_file(tmp_path):
    raw=yaml.safe_load((ROOT/'config.example.yaml').read_text(encoding='utf-8'))
    raw['database']['url']='sqlite:///db.sqlite3'
    raw['network']['rate_state']=str(tmp_path/'rate.sqlite3')
    for item in raw['exchanges'].values():
        item['enabled']=True; item['mode']='fixture'
        for field in ('calendar','fixture'): item[field]=str(ROOT/item[field])
        item['market']['fixture']=str(ROOT/item['market']['fixture'])
    path=tmp_path/'config.yaml'; path.write_text(yaml.safe_dump(raw,allow_unicode=True),encoding='utf-8')
    return path,raw


def test_failed_receipt_does_not_block_market_and_resume_is_idempotent(tmp_path):
    from data_collection.config import load_config
    from data_collection.scheduler import run_dates
    from data_collection.storage import Store
    path,raw=config_file(tmp_path)
    raw['exchanges']['CZCE']['fixture']=str(tmp_path/'missing.html')
    raw['exchanges']['CZCE']['market']['enabled']=True
    path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    cfg=load_config(path)
    first=run_dates(cfg,'2026-09-30','2026-09-30')
    assert any(r['channel']=='receipt' and r['exchange']=='CZCE' and r['status']=='FAILED' for r in first)
    assert any(r['channel']=='market' and r['status']=='SUCCESS' for r in first)
    with Store(cfg['database']['path']) as db:
        assert any(r['exchange']=='DCE' and r['row_type']=='TOTAL' for r in db.rows())
        before=len(db.rows())
    second=run_dates(cfg,'2026-09-30','2026-09-30')
    assert any(r['status']=='SKIPPED_COMPLETE' for r in second)
    with Store(cfg['database']['path']) as db:
        assert len(db.rows())==before
        assert any(r['status']=='FAILED' for r in db.logs())


def test_dry_run_prints_validation_but_creates_no_database(tmp_path,capsys):
    from data_collection.cli import main
    path,_=config_file(tmp_path)
    assert main(['--config',str(path),'run','--date','2026-09-30','--dry-run'])==0
    output=capsys.readouterr().out
    assert 'row_count' in output and 'UNVERIFIED' in output
    assert not (tmp_path/'db.sqlite3').exists()


def test_nontrading_date_is_not_collected(tmp_path):
    from data_collection.config import load_config
    from data_collection.scheduler import run_dates
    path,_=config_file(tmp_path)
    assert run_dates(load_config(path),'2026-10-01','2026-10-01',dry_run=True)==[]


def test_self_check_reports_selector_failure(tmp_path,capsys):
    from data_collection.cli import main
    path,raw=config_file(tmp_path)
    raw['exchanges']['CZCE']['parser']['table_selector']='#missing'
    path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    assert main(['--config',str(path),'self-check'])==2
    assert 'SELECTOR_INVALID' in capsys.readouterr().out


@pytest.mark.parametrize('fault', ['offline', 'timeout', '429'])
def test_exhausted_request_is_logged_without_blocking_market(tmp_path, fault):
    from data_collection.config import load_config
    from data_collection.scheduler import run_dates
    from data_collection.storage import Store
    from data_collection.transport import HTTP
    path,raw=config_file(tmp_path)
    item=raw['exchanges']['CZCE']; item['mode']='html'; item['base_url']='https://official.example'
    item['request']['path']='/data'; item['market']['enabled']=True
    item['market']['base_url']='https://official.example'; item['market']['request']['path']='/market'
    cal=tmp_path/'calendar.yaml'; c=yaml.safe_load(Path(item['calendar']).read_text(encoding='utf-8'))
    c['fixture_only']=False; c['source']='fixture official-calendar test'; cal.write_text(yaml.safe_dump(c),encoding='utf-8'); item['calendar']=str(cal)
    path.write_text(yaml.safe_dump(raw),encoding='utf-8'); cfg=load_config(path)
    clock=Clock(); attempts=[]
    def handle(request):
        if request.url.path=='/robots.txt': return httpx.Response(200,text='User-agent: *\nAllow: /')
        if request.url.path=='/market': return httpx.Response(200,text=(ROOT/'tests/fixtures/market.json').read_text(encoding='utf-8'))
        attempts.append(clock.now())
        if fault=='offline': raise httpx.ConnectError('private password')
        if fault=='timeout': raise httpx.ReadTimeout('private password')
        return httpx.Response(429)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        wire=HTTP(cfg['network'],client=client,now=clock.now,sleep=clock.sleep)
        result=run_dates(cfg,'2026-09-30','2026-09-30',exchange='CZCE',wire=wire)
    assert len(attempts)==3
    assert [r['status'] for r in result]==['FAILED','SUCCESS']
    with Store(cfg['database']['path']) as db:
        assert len(db.rows())==1 and db.rows()[0]['row_type']=='MARKET'
        assert any(r['status']=='REQUEST_FAILED' for r in db.logs())
        assert any(r['status']=='FAILED' for r in db.logs())
        assert 'private password' not in json.dumps(db.logs())


def test_robots_crawl_delay_applies_to_every_retry_and_charset_is_configurable():
    from data_collection.transport import HTTP
    clock=Clock(); times=[]
    def handle(request):
        if request.url.path=='/robots.txt': return httpx.Response(200,text='User-agent: *\nAllow: /\nCrawl-delay: 40')
        times.append(clock.now())
        if len(times)<3: return httpx.Response(500)
        return httpx.Response(200,content='仓单'.encode('gb18030'))
    net=network()
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        text=HTTP(net,client=client,now=clock.now,sleep=clock.sleep).request({'base_url':'https://official.example','encoding':'gb18030','request':{'method':'GET','path':'/data'}},{},lambda r:None)
    assert text=='仓单'
    assert all(b-a>=40 for a,b in zip(times,times[1:]))


def test_scheduler_polls_only_failed_channels_and_stops_at_limit(tmp_path):
    from data_collection.config import load_config
    from data_collection.scheduler import poll
    from data_collection.storage import Store
    path,raw=config_file(tmp_path); raw['schedule']['max_polls']=2
    raw['exchanges']['CZCE']['fixture']=str(tmp_path/'missing.html')
    path.write_text(yaml.safe_dump(raw),encoding='utf-8'); cfg=load_config(path); sleeps=[]
    result=poll(cfg,'2026-09-30','2026-09-30',sleep=sleeps.append)
    assert len(sleeps)==1
    assert any(r['status']=='FAILED' for r in result)
    with Store(cfg['database']['path']) as db:
        successes=[r for r in db.logs() if r['status']=='SUCCESS']
        assert len(successes)==2  # DCE JD/LH only, no repeated successes.
        assert any(r['status']=='POLL_EXHAUSTED' for r in db.logs())


def test_live_mode_rejects_fixture_calendar(tmp_path):
    from data_collection.config import load_config
    path,raw=config_file(tmp_path)
    item=raw['exchanges']['CZCE']; item['mode']='html'; item['base_url']='https://official.example'; item['request']['path']='/data'
    path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    with pytest.raises(ValueError,match='LIVE_CALENDAR_NOT_VERIFIED'): load_config(path)


def test_dry_run_reads_history_without_updating_checkpoint(tmp_path):
    from data_collection.config import load_config
    from data_collection.scheduler import run_dates
    from data_collection.storage import Store
    path,_=config_file(tmp_path); cfg=load_config(path)
    run_dates(cfg,'2026-09-30','2026-09-30')
    with Store(cfg['database']['path']) as db: before=(db.rows(),db.logs())
    result=run_dates(cfg,'2026-09-30','2026-09-30',dry_run=True)
    assert all(r['status']=='SUCCESS' for r in result)
    with Store(cfg['database']['path']) as db: assert (db.rows(),db.logs())==before


def test_startup_selector_failure_never_requests_online(tmp_path):
    from data_collection.config import load_config
    from data_collection.scheduler import run_dates
    path,raw=config_file(tmp_path)
    raw['exchanges']['CZCE']['parser']['table_selector']='#missing'
    item=raw['exchanges']['CZCE']; item['mode']='html'; item['base_url']='https://official.example'; item['request']['path']='/data'
    cal=tmp_path/'calendar.yaml'; c=yaml.safe_load(Path(item['calendar']).read_text(encoding='utf-8')); c['fixture_only']=False
    cal.write_text(yaml.safe_dump(c),encoding='utf-8'); item['calendar']=str(cal)
    path.write_text(yaml.safe_dump(raw),encoding='utf-8'); cfg=load_config(path)
    class NoRequests:
        def request(self,*args): raise AssertionError('startup should reject bad fixture selector before fetching')
    result=run_dates(cfg,'2026-09-30','2026-09-30',wire=NoRequests())
    assert result[0]['status']=='FAILED' and result[0]['reason']=='SELECTOR_INVALID'
    assert any(r['exchange']=='DCE' and r['status']=='SUCCESS' for r in result)


def test_failed_reference_quarantines_report(tmp_path):
    from data_collection.config import load_config
    from data_collection.scheduler import run_dates
    from data_collection.storage import Store
    path,raw=config_file(tmp_path)
    ref=tmp_path/'reference.json'
    ref.write_text(json.dumps([{'trade_date':'2026-09-30','exchange':'CZCE','variety':'SR','unit':'吨','value':999,'source':'fixture:cfmmc'}]),encoding='utf-8')
    raw['validation']['reference_file']=str(ref)
    path.write_text(yaml.safe_dump(raw),encoding='utf-8'); cfg=load_config(path)
    result=run_dates(cfg,'2026-09-30','2026-09-30')
    assert result[0]['status']=='REJECTED'
    assert 'REFERENCE_MISMATCH' in result[0]['validation']['errors']
    with Store(cfg['database']['path']) as db:
        assert not any(r['exchange']=='CZCE' for r in db.rows())
        assert any(r['status']=='REJECTED' for r in db.logs())


def test_dce_auth_is_configured_and_token_is_not_logged(tmp_path, monkeypatch):
    from data_collection.config import load_config
    from data_collection.collectors.dce_receipt import DCEReceipt
    from data_collection.storage import Store
    from data_collection.transport import HTTP
    path,raw=config_file(tmp_path); cfg=load_config(path)
    item=cfg['exchanges']['DCE']; item['mode']='api'
    monkeypatch.setenv('DCE_API_KEY','fake-key'); monkeypatch.setenv('DCE_API_SECRET','private-secret')
    clock=Clock(); seen=[]; logs=[]
    def handle(request):
        seen.append(request)
        if request.url.path=='/robots.txt': return httpx.Response(404)
        if request.url.path.endswith('accessToken'):
            assert json.loads(request.content)=={'secret':'private-secret'}
            return httpx.Response(200,json={'success':True,'code':200,'data':{'token':'private-token'}})
        assert request.headers['Authorization']=='Bearer private-token'
        assert json.loads(request.content)=={'varietyId':'jd','tradeDate':'20260930'}
        return httpx.Response(200,text=(ROOT/'tests/fixtures/dce.json').read_text(encoding='utf-8'))
    with Store(':memory:') as db, httpx.Client(transport=httpx.MockTransport(handle)) as client:
        collector=DCEReceipt('DCE',item,cfg,db,HTTP(cfg['network'],client=client,now=clock.now,sleep=clock.sleep))
        rows=collector.parse(collector.fetch('2026-09-30','JD',logs.append),'2026-09-30','JD')
        assert rows[-1]['warr_yesterday']==204
    assert len(seen)==3
    assert 'private' not in json.dumps(logs)


def test_poll_waits_for_report_date_then_inserts_once(tmp_path,monkeypatch):
    from data_collection.config import load_config
    from data_collection.scheduler import poll
    from data_collection.storage import Store
    from data_collection.transport import HTTP
    path,raw=config_file(tmp_path); item=raw['exchanges']['CZCE']
    item['mode']='html'; item['base_url']='https://official.example'; item['request']['path']='/data'
    cal=tmp_path/'calendar.yaml'; data=yaml.safe_load(Path(item['calendar']).read_text(encoding='utf-8')); data['fixture_only']=False
    cal.write_text(yaml.safe_dump(data),encoding='utf-8'); item['calendar']=str(cal)
    path.write_text(yaml.safe_dump(raw),encoding='utf-8'); cfg=load_config(path)
    clock=Clock(); requests=[]
    def handle(request):
        if request.url.path=='/robots.txt': return httpx.Response(200,text='User-agent: *\nAllow: /')
        requests.append(clock.now())
        text=(ROOT/'tests/fixtures/czce.html').read_text(encoding='utf-8')
        if len(requests)==1: text=text.replace('2026-09-30','2026-09-29')
        return httpx.Response(200,text=text)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        wire=HTTP(cfg['network'],client=client,now=clock.now,sleep=clock.sleep)
        monkeypatch.setattr('data_collection.scheduler.HTTP',lambda settings:wire)
        result=poll(cfg,'2026-09-30','2026-09-30','CZCE',sleep=clock.sleep)
    assert len(requests)==2
    assert result[0]['status']=='SUCCESS'
    with Store(cfg['database']['path']) as db:
        assert len(db.rows())==3
        assert any(r['status']=='FAILED' and 'UPDATE_NOT_READY' in r['details'] for r in db.logs())


def test_response_limit_rejects_large_body():
    from data_collection.transport import HTTP
    net=network(); net['max_response_bytes']=5
    clock=Clock(); paths=[]
    def handle(request):
        paths.append(request.url.path)
        if request.url.path=='/robots.txt': return httpx.Response(404)
        return httpx.Response(200,text='too-long')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError,match='RESPONSE_TOO_LARGE'):
            HTTP(net,client=client,now=clock.now,sleep=clock.sleep).request({'base_url':'https://official.example','request':{'method':'GET','path':'/data'}},{},lambda r:None)
    assert paths==['/robots.txt','/data']


def test_missing_request_or_parser_configuration_fails_self_check(tmp_path,capsys):
    from data_collection.cli import main
    path,raw=config_file(tmp_path); del raw['exchanges']['CZCE']['parser']['row_selector']
    path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    assert main(['--config',str(path),'self-check'])==2


def test_rate_limit_survives_new_client_instance(tmp_path):
    from data_collection.transport import HTTP
    net=network(); net['rate_state']=str(tmp_path/'rate.sqlite3'); clock=Clock(); times=[]
    def handle(request):
        times.append(clock.now())
        return httpx.Response(404) if request.url.path=='/robots.txt' else httpx.Response(200,text='ok')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        for _ in range(2):
            HTTP(net,client=client,now=clock.now,sleep=clock.sleep).request({'base_url':'https://official.example','request':{'method':'GET','path':'/data'}},{},lambda r:None)
    assert len(times)==4
    assert all(b-a>=8 for a,b in zip(times,times[1:]))


@pytest.mark.parametrize('section,key,value', [('schedule','time','25:00'), ('schedule','max_polls',0), ('schedule','poll_seconds',0)])
def test_invalid_schedule_is_rejected_at_startup(tmp_path,section,key,value):
    from data_collection.config import load_config
    path,raw=config_file(tmp_path)
    raw[section][key]=value
    path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    with pytest.raises(ValueError,match='INVALID_SCHEDULE'): load_config(path)


def test_auth_business_failure_cannot_supply_a_token(tmp_path):
    from data_collection.config import load_config
    from data_collection.collectors.dce_receipt import DCEReceipt
    from data_collection.storage import Store
    path,_=config_file(tmp_path); cfg=load_config(path)
    item=cfg['exchanges']['DCE']; item['mode']='api'
    class FailedAuth:
        def request(self,*args):
            return json.dumps({'success':False,'code':401,'data':{'token':'stale-token'}})
    with Store(':memory:') as db:
        collector=DCEReceipt('DCE',item,cfg,db,FailedAuth())
        with pytest.raises(ValueError,match='AUTH_BUSINESS_ERROR'):
            collector.fetch('2026-09-30','JD',lambda row:None)
        assert collector.token is None


def test_redirect_is_rejected_without_following_location():
    from data_collection.transport import HTTP
    clock=Clock(); seen=[]
    def handle(request):
        seen.append(str(request.url))
        if request.url.path=='/robots.txt': return httpx.Response(404)
        return httpx.Response(302,headers={'Location':'https://other.example/data'})
    with httpx.Client(transport=httpx.MockTransport(handle),follow_redirects=True) as client:
        with pytest.raises(ValueError,match='HTTP_302'):
            HTTP(network(),client=client,now=clock.now,sleep=clock.sleep).request({'base_url':'https://official.example','request':{'method':'GET','path':'/data'}},{},lambda r:None)
    assert len(seen)==2 and all('other.example' not in url for url in seen)


def test_schedule_calendar_expiry_is_logged_and_other_exchange_runs(tmp_path,monkeypatch):
    from datetime import datetime, timezone, timedelta
    from data_collection.config import load_config
    from data_collection.scheduler import serve
    from data_collection.storage import Store
    path,raw=config_file(tmp_path)
    cal=tmp_path/'expired.yaml'
    cal.write_text(yaml.safe_dump({'start':'2026-09-29','end':'2026-09-30','dates':['2026-09-30']}),encoding='utf-8')
    raw['exchanges']['CZCE']['calendar']=str(cal)
    path.write_text(yaml.safe_dump(raw),encoding='utf-8'); cfg=load_config(path)
    seen=[]
    monkeypatch.setattr('data_collection.scheduler.poll',lambda cfg,start,end,name:seen.append(name) or [])
    class StopLoop(Exception): pass
    def stop(_): raise StopLoop()
    now=lambda:datetime(2026,10,8,19,0,tzinfo=timezone(timedelta(hours=8)))
    with pytest.raises(StopLoop): serve(cfg,sleep=stop,now=now)
    assert seen==['DCE']
    with Store(cfg['database']['path']) as db:
        assert any(r['status']=='SCHEDULE_FAILED' and 'CALENDAR_COVERAGE_MISSING' in r['details'] for r in db.logs())
