"""Simulation endpoints. Only snapshot creation may read market storage."""
import hashlib
import json
from decimal import Decimal
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError
from backend.api.auth import require_user, SESSION_COOKIE
from backend.market_simulator.models import SnapshotRequest, ScenarioInput, SimulatorError, wire, now_beijing
from backend.market_simulator.store import SnapshotStore
from backend.market_simulator.service import compare, constraints, presets, volume_baseline

router = APIRouter(prefix='/api/v1/market-simulator', tags=['market-simulator'])
store = SnapshotStore()
clock = now_beijing


def get_reader():
    from backend.storage.redis_client import RedisClient
    from backend.storage.mysql_client import SessionLocal
    from backend.market_simulator.reader import MarketReader
    return MarketReader(RedisClient.get_client(), SessionLocal)


def identity(request):
    user = require_user(request)
    if user.role != 'sim':
        raise HTTPException(403, detail='行情模拟仅供模拟用户使用')
    token = request.cookies.get(SESSION_COOKIE, '')
    if not token:
        raise HTTPException(401, detail='请重新登录')
    return user.username, hashlib.sha256(token.encode()).hexdigest()


async def body_model(request, model):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise SimulatorError('BODY_TOO_LARGE', '请求不得超过8KiB', 413)
    try:
        return model.model_validate_json(bytes(raw))
    except ValidationError:
        # Pydantic may echo arbitrary input in its error details; do not expose it.
        raise SimulatorError('INVALID_INPUT', '输入格式无效：请检查必填字段、十进制格式和互斥模式')


def context(stage, snapshot=None, request=None, instrument=None):
    ctx = dict(stage=stage, instrument=instrument, timeframe='15m', hypothetical=True,
               snapshot_id=None, frozen_at=None, quote_at=None, expires_at=None,
               client_sequence=None, inputs=None)
    if snapshot:
        ctx.update({k:snapshot[k] for k in ('instrument','snapshot_id','frozen_at','quote_at','expires_at')})
    if request:
        ctx['inputs'] = request.model_dump(exclude_none=True)
        ctx['client_sequence'] = getattr(request, 'client_sequence', None)
    return ctx


def failure(error, ctx=None):
    return JSONResponse(status_code=error.status, content={'detail':error.detail(ctx)}, headers={'Cache-Control':'no-store'})


def success(data):
    return JSONResponse({'code':0, 'data':wire(data)}, headers={'Cache-Control':'no-store'})


def snapshot_response(snapshot, now):
    from backend.market_simulator.reader import quality_summary
    snapshot['quality'].update(quality_summary(snapshot['observed_bar'],
        {'15m': snapshot['history_15m'], '60m': snapshot['history_60m']}))
    baseline = volume_baseline(snapshot)
    observed = snapshot['observed_bar']
    history = snapshot['history_15m']
    previous = history[-1].get('open_interest') if history else None
    oi = observed.get('open_interest')
    defaults = dict(simulate_price=observed['close_price'], observed_volume=observed['volume'],
                    volume_ratio=Decimal(observed['volume'])/baseline if baseline else None,
                    open_interest_change=Decimal(str(oi))-Decimal(str(previous)) if oi is not None and previous is not None else None)
    initial = compare(snapshot, ScenarioInput(snapshot_id=snapshot['snapshot_id'],use_defaults=True), now)
    data = {k:v for k,v in snapshot.items() if k not in ('history_15m','history_60m','tick','observed_bar')}
    data.update(defaults=defaults, constraints=constraints(snapshot), presets=presets(snapshot),
                current=initial['current'], initial_comparison=initial,
                history_cutoffs={p:dict(count=len(snapshot['history_'+p]),
                                       closed_until=snapshot['history_'+p][-1].get('end_at') if snapshot['history_'+p] else None)
                                 for p in ('15m','60m')})
    return data


@router.post('/snapshots')
async def create_snapshot(request: Request):
    owner, session = identity(request)
    ctx = None
    sid = None
    try:
        req = await body_model(request, SnapshotRequest)
        instrument = req.instrument.upper()
        ctx = context('CREATE', request=req, instrument=instrument)
        now = clock()
        store.rate(owner, session, 'create', now)
        data = await run_in_threadpool(get_reader().freeze, instrument, owner, req.reference_run_id, now, clock)
        now = clock()
        sid = store.put(owner, session, data, now)
        snapshot = store.get(owner, session, sid, now)
        response = snapshot_response(snapshot, now)
        return success(response)
    except SimulatorError as error:
        if sid:
            store.delete(owner, session, sid, clock())
        return failure(error, None if error.status == 404 else ctx)
    except Exception:
        if sid:
            try:
                store.delete(owner, session, sid, clock())
            except SimulatorError:
                pass
        return failure(SimulatorError('MARKET_READ_FAILED','行情读取失败，请检查数据服务；未创建可用基准',503),ctx)


@router.post('/simulate')
async def simulate(request: Request):
    owner, session = identity(request)
    ctx = None
    try:
        req = await body_model(request, ScenarioInput)
        now = clock()
        snapshot = store.get(owner, session, req.snapshot_id, now)
        ctx = context('SIMULATE', snapshot, req)
        store.rate(owner, session, 'simulate', now)
        return success(compare(snapshot, req, now))
    except SimulatorError as error:
        return failure(error, None if error.status == 404 else ctx)


@router.delete('/snapshots/{snapshot_id}')
def release(snapshot_id: str, request: Request):
    owner, session = identity(request)
    try:
        store.delete(owner, session, snapshot_id, clock())
        return Response(status_code=204, headers={'Cache-Control':'no-store'})
    except SimulatorError as error:
        return failure(error)
