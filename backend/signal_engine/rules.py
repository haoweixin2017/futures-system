"""15m numeric rules. No storage, account, notification or lifecycle imports."""
from decimal import Decimal
from backend.indicator_engine.indicators import IndicatorCalculator
from backend.common.volume_quality import volume_usable


def value(bar, key):
    return bar.get(key) if isinstance(bar, dict) else getattr(bar, key, None)


def number(bar, key):
    if key == 'volume' and not volume_usable(bar):
        return None
    v = value(bar, key)
    if v is None:
        return None
    d = Decimal(str(v))
    return d if d.is_finite() else None


def evaluate_rules(candidate, history_15m, history_60m, policy='STRICT'):
    if policy not in ('STRICT', 'LEGACY'):
        raise ValueError('Unknown rule policy')
    legacy = policy == 'LEGACY'
    history = history_15m[-20:]
    enough = len(history) == 20
    highs = [number(b, 'high_price') for b in history]
    lows = [number(b, 'low_price') for b in history]
    volumes = [number(b, 'volume') for b in history]
    high = max(highs) if enough and all(v is not None for v in highs) else None
    low = min(lows) if enough and all(v is not None for v in lows) else None
    avg = sum(volumes)/20 if enough and all(v is not None and v >= 0 for v in volumes) else None
    price, vol, oi = (number(candidate, k) for k in ('close_price', 'volume', 'open_interest'))
    previous_oi = number(history[-1], 'open_interest') if history else None
    oi_change = oi-previous_oi if oi is not None and previous_oi is not None else None
    ratio = vol/avg if vol is not None and avg is not None and avg > 0 else None
    # Original formal signal code uses Python float mean/ratio. Preserve its edge behavior.
    if legacy and enough and all(v is not None for v in volumes) and vol is not None:
        avg = sum(int(value(b, 'volume')) for b in history)/len(history)
        ratio = value(candidate, 'volume')/avg if avg > 0 else 0.0
    closes = [number(b, 'close_price') for b in history_60m]
    ema20 = ema60 = None
    if len(closes) >= 60 and all(c is not None for c in closes):
        ema20 = IndicatorCalculator.calculate_ema(closes, 20)
        ema60 = IndicatorCalculator.calculate_ema(closes, 60)
    passed_vol = ratio >= Decimal('1.5') if ratio is not None else None
    passed_oi = oi_change > 0 if oi_change is not None else None
    out = {'stats': dict(highest_prev=high, lowest_prev=low, avg_vol=avg,
                         vol_ratio=ratio, oi_change=oi_change, ema20=ema20, ema60=ema60)}
    for side, boundary in (('long', high), ('short', low)):
        breakout = None if price is None or boundary is None else (price > boundary if side == 'long' else price < boundary)
        trend = ((ema20 >= ema60 if side == 'long' else ema20 <= ema60)
                 if ema20 and ema60 else (True if legacy else None))
        conditions = []
        for code, passed, actual, threshold, weight in (
            ('BREAK_PREVIOUS_HIGH' if side == 'long' else 'BREAK_PREVIOUS_LOW', breakout, price, boundary, 35),
            ('VOLUME_EXPANSION', passed_vol, ratio, Decimal('1.5'), 30),
            ('OPEN_INTEREST_INCREASE', passed_oi, oi_change, Decimal('0'), 20),
            ('TREND_ALIGNMENT_60M', trend, ema20, ema60, 15),
        ):
            conditions.append(dict(code=code, direction=side.upper(), actual=actual, threshold=threshold,
                                   weight=weight, passed=passed, points=weight*int(passed) if passed is not None else None,
                                   reason_code='MISSING_DATA' if passed is None else ('PASSED' if passed else 'NOT_PASSED')))
        score = sum(c['points'] for c in conditions) if all(c['points'] is not None for c in conditions) else None
        out[side] = dict(score=score, conditions=conditions,
                         state='UNEVALUABLE' if score is None else ('MATCHED' if score >= 70 else 'NOT_MATCHED'))
    return out
