"""Quarantine a verified bad minute allocation without rewriting its raw values.

Default is read-only. --apply requires an explicit evidence note and audit file.
This is maintenance, never invoked by charts, simulator, or background collection.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.common.trading_time import FuturesTradingCalendar as Calendar
from backend.models.market_data import decode_bar_provenance
from backend.models.database import MarketBarModel
from backend.storage.mysql_client import SessionLocal
from backend.storage.redis_client import RedisClient
from redis.exceptions import WatchError


def sync_cached_quality(redis,key,row):
    for _ in range(3):
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                updates=[]
                for index,raw in enumerate(pipe.lrange(key,0,-1)):
                    bar=json.loads(raw)
                    p=decode_bar_provenance(bar.get('provenance_json')) or {}
                    if (datetime.fromisoformat(bar['bar_time'])==row.bar_time
                            and bar.get('instrument')==row.instrument_id and bar.get('period')==row.period
                            and bar.get('trading_day')==row.trading_day
                            and p.get('provider')=='sina' and p.get('label_semantics')=='START'
                            and (p.get('source')=='sina_tick_aggregate' or p.get('volume_source')=='sina_tick_aggregate')):
                        dbp=decode_bar_provenance(row.provenance_json)
                        p.update(volume_quality=dbp['volume_quality'],
                                 volume_gap=dict(dbp['volume_gap'],raw_cached_volume=bar.get('volume')))
                        bar['provenance_json']=p
                        updates.append((index,json.dumps(bar,ensure_ascii=False)))
                pipe.multi()
                for index,payload in updates:
                    pipe.lset(key,index,payload)
                pipe.execute()
                return len(updates)
        except WatchError:
            continue
    raise RuntimeError('Cache changed during repair; database audit already saved, cache sync incomplete')


def affected(row, stamp):
    if row.period not in ('1m','5m','15m','30m','60m'):
        return False
    p=decode_bar_provenance(row.provenance_json) or {}
    if p.get('provider')!='sina' or p.get('label_semantics')!='START':
        return False
    if p.get('source')!='sina_tick_aggregate' and p.get('volume_source')!='sina_tick_aggregate':
        return False
    return row.bar_time <= stamp < row.bar_time+timedelta(minutes=int(row.period[:-1]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instrument',required=True)
    parser.add_argument('--bar-time',required=True)
    parser.add_argument('--evidence-note',required=True)
    parser.add_argument('--audit-file',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    stamp=datetime.fromisoformat(args.bar_time)
    if stamp.tzinfo is not None or stamp.second or stamp.microsecond:
        parser.error('--bar-time must be a local Beijing minute start')
    audit_path=Path(args.audit_file)
    if args.apply and audit_path.exists():
        parser.error('audit file already exists; refusing to overwrite evidence')
    evidence=Path(args.evidence_note)
    if not evidence.is_file():
        parser.error('evidence note must exist')
    session=SessionLocal()
    try:
        query=session.query(MarketBarModel).filter_by(instrument_id=args.instrument,
            trading_day=Calendar.get_trading_day(stamp))
        rows=query.with_for_update().all() if args.apply else query.all()
        targets=[row for row in rows if affected(row,stamp)]
        if not any(row.period=='1m' and row.bar_time==stamp for row in targets):
            raise RuntimeError('No matching original Sina minute allocation; no changes')
        originals=[{column.name:getattr(row,column.name) for column in MarketBarModel.__table__.columns} for row in targets]
        print(json.dumps({'apply':args.apply,'instrument':args.instrument,'targets':[
            {'period':row.period,'bar_time':row.bar_time.isoformat(),'raw_volume':row.volume} for row in targets]},ensure_ascii=False))
        if not args.apply:
            return
        audit_path.parent.mkdir(parents=True,exist_ok=True)
        audit=dict(status='PREPARED',instrument=args.instrument,minute=stamp.isoformat(),
                   evidence_note=str(evidence.resolve()),before=originals,
                   reason='Verified restart/collection gap; original exact minute volume unavailable. Raw values preserved.')
        audit_path.write_text(json.dumps(audit,default=str,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        for row in targets:
            p=decode_bar_provenance(row.provenance_json) or {}
            p.update(volume_quality='UNVERIFIED_ALLOCATION',volume_gap=dict(
                affected_minute=stamp.isoformat(),raw_stored_volume=row.volume,
                source_interval='UNKNOWN',evidence_note=str(evidence.resolve()),audit_file=str(audit_path.resolve())))
            row.provenance_json=json.dumps(p,ensure_ascii=False)
        session.commit()
        audit['status']='DB_APPLIED'
        audit_path.write_text(json.dumps(audit,default=str,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        # Update matching cached records in place; never replace values or sources.
        redis=RedisClient.get_client();cache_updates=0
        for row in targets:
            key=f'bars:{args.instrument}:{row.period}'
            cache_updates+=sync_cached_quality(redis,key,row)
            redis.delete(f'indicators:v2:sina:{args.instrument}:{row.period}',
                         f'indicators:v2:{args.instrument}:{row.period}')
        redis.delete(f'analysis:v1:sina:{args.instrument}:5m')
        audit.update(status='APPLIED',cache_updates=cache_updates,
            after=[dict(period=row.period,bar_time=row.bar_time.isoformat(),raw_volume=row.volume,
                        provenance=decode_bar_provenance(row.provenance_json)) for row in targets])
        audit_path.write_text(json.dumps(audit,default=str,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__=='__main__':main()
