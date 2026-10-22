"""Pure Sina observation evidence and explicitly identified history time grids.

An observation anchor is a recorded cumulative sample BEFORE a period, never
an estimate of exchange volume at the exact period boundary.
"""
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from functools import lru_cache

from backend.common.trading_time import FuturesTradingCalendar as Calendar


def _local(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if stamp.tzinfo is not None:
        from datetime import timezone
        stamp = stamp.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    return stamp


def _sample(record):
    stamp = _local(record['timestamp'])
    volume = Decimal(str(record['volume']))
    if not volume.is_finite() or volume < 0 or volume != int(volume):
        raise ValueError('invalid source cumulative volume')
    if (str(record['trading_day']) != Calendar.get_trading_day(stamp)
            or not Calendar.is_trading_time_for(stamp, record['instrument'])):
        raise ValueError('invalid source session')
    return stamp, int(volume)


def _digest(record):
    return 'sha256:' + hashlib.sha256(json.dumps(record, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()


def advance_sina_evidence(previous, current, anchor):
    """Capture original source fields; only a new period can establish an anchor."""
    try:
        stamp, volume = _sample(current)
    except (KeyError, ValueError, TypeError, AttributeError, ArithmeticError):
        return None, None
    latest = deepcopy(current)
    start = stamp.replace(minute=stamp.minute//15*15, second=0, microsecond=0)
    try:
        before, before_volume = _sample(previous)
        same_day = (previous['instrument'] == current['instrument']
                    and previous['trading_day'] == current['trading_day'])
        if before >= stamp:
            return deepcopy(previous), deepcopy(anchor)
        if not same_day or volume < before_volume:
            return latest, None
        if before < start:
            if start-before > timedelta(minutes=1) or stamp-start >= timedelta(minutes=1):
                return latest, None
            anchor = dict(schema_version=1, kind='LAST_SOURCE_OBSERVATION',
                boundary='BEFORE_PERIOD', at=start.isoformat(), value=before_volume,
                trading_day=current['trading_day'], source_at=previous['timestamp'],
                source_ref=_digest(previous), source_record=deepcopy(previous), coverage='PARTIAL')
        elif not anchor or _local(anchor['at']) != start:
            anchor = None
        return latest, deepcopy(anchor)
    except (KeyError, ValueError, TypeError, AttributeError, ArithmeticError):
        return latest, None


def valid_volume_anchor(anchor, start, quote):
    """Verify the stored raw sample and its hash; no derivation from bar sums."""
    try:
        record = anchor['source_record']
        before, volume = _sample(record)
        boundary = _local(start)
        quote_time, current_volume = _sample(quote)
        return (anchor['schema_version'] == 1 and anchor['kind'] == 'LAST_SOURCE_OBSERVATION'
            and anchor['boundary'] == 'BEFORE_PERIOD' and _local(anchor['at']) == boundary
            and record['instrument'] == quote['instrument']
            and record['trading_day'] == quote['trading_day'] == anchor['trading_day']
            and anchor['source_at'] == record['timestamp'] and anchor['source_ref'] == _digest(record)
            and Decimal(str(anchor['value'])) == volume <= current_volume
            and boundary-timedelta(minutes=1) <= before < boundary <= quote_time
            and anchor['coverage'] == 'PARTIAL')
    except (KeyError, ValueError, TypeError, AttributeError, ArithmeticError):
        return False


@lru_cache(maxsize=2048)
def sina_history_windows(instrument, trading_day, period):
    """END labels for Sina's explicitly identified trading-minute history series.

Day and night restart accumulation independently. Breaks contribute no minutes.
The result describes source intervals, and says nothing about source coverage.
"""
    minutes = int(period[:-1])
    if period not in ('1m', '5m', '15m', '30m', '60m'):
        raise ValueError('unsupported period')
    day = datetime.strptime(trading_day, '%Y%m%d')
    if not Calendar.is_trading_day(day.date()):
        raise ValueError('non-trading day')
    groups = {'DAY': [], 'NIGHT': []}
    for session in Calendar.analysis_session_options(day.replace(hour=8), instrument):
        cursor, end = _local(session['start_time']), _local(session['end_time'])
        while cursor < end:
            groups[session['session']].append(cursor)
            cursor += timedelta(minutes=1)
    windows = {}
    for slots in groups.values():
        for i in range(0, len(slots), minutes):
            chunk = slots[i:i+minutes]
            segments = []
            for stamp in chunk:
                end = stamp+timedelta(minutes=1)
                if segments and segments[-1][1] == stamp:
                    segments[-1] = (segments[-1][0], end)
                else:
                    segments.append((stamp, end))
            windows[segments[-1][1]] = dict(start_at=chunk[0], end_at=segments[-1][1], segments=segments)
    return windows
