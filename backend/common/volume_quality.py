"""Explicit unknown minute-volume allocation; raw values remain available for audit."""
from copy import deepcopy
from datetime import timedelta
from backend.models.market_data import decode_bar_provenance
from backend.common.trading_time import FuturesTradingCalendar as Calendar


def metadata(bar):
    raw = bar.get('provenance_json', bar.get('provenance')) if isinstance(bar, dict) else getattr(bar,'provenance_json',None)
    return decode_bar_provenance(raw) or {}


def volume_usable(bar):
    return metadata(bar).get('volume_quality') not in ('UNALLOCATED_GAP','UNVERIFIED_ALLOCATION')


def inherit_volume_quality(provenance, *bars):
    result = dict(provenance or {})
    for bar in bars:
        if bar is not None and not volume_usable(bar):
            p=metadata(bar)
            result['volume_quality']=p['volume_quality']
            result['volume_gap']=deepcopy(p.get('volume_gap',{}))
            break
    return result


def missing_trading_minutes(before, current, instrument):
    cursor=before.replace(second=0,microsecond=0)+timedelta(minutes=1)
    stop=current.replace(second=0,microsecond=0)
    while cursor < stop:
        if Calendar.is_trading_time_for(cursor,instrument):
            return True
        cursor+=timedelta(minutes=1)
    return False


def public_volume_bar(bar):
    result=dict(bar)
    if not volume_usable(bar):
        result.update(volume=None,volume_raw=bar.get('volume'),
            volume_quality=metadata(bar)['volume_quality'],
            volume_warning='成交量跨采集缺口，分钟归属未知；原始值仅供审计，不参与量能计算')
    return result
