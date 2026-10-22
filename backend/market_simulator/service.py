"""Pure hypothetical calculations against one frozen snapshot."""
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, localcontext
from types import SimpleNamespace
from backend.signal_engine.rules import evaluate_rules, number
from backend.indicator_engine.indicators import IndicatorCalculator
from .models import SimulatorError

D = Decimal


def volume_baseline(snapshot):
    history = snapshot['history_15m'][-20:]
    values = [number(b, 'volume') for b in history]
    return sum(values)/20 if len(values) == 20 and all(v is not None and v >= 0 for v in values) and sum(values) > 0 else None


def validate_price(snapshot, price):
    tick = snapshot['tick']
    if not price.is_finite() or price <= 0 or price % tick != 0:
        raise SimulatorError('INVALID_PRICE_TICK', '价格须为正且对齐合约最小变动价位')
    for field, bad in [('lower_limit', lambda x: price < x), ('upper_limit', lambda x: price > x)]:
        if snapshot.get(field) is not None and bad(snapshot[field]):
            raise SimulatorError('OUTSIDE_PRICE_LIMIT', '假设价格超出已核实涨跌停')


def constraints(snapshot):
    tick = snapshot['tick']
    bars = snapshot['history_15m']
    try:
        atr = IndicatorCalculator.calculate_atr([SimpleNamespace(**{k: number(b, k) for k in ('high_price', 'low_price', 'close_price')}) for b in bars], 14)
    except (ValueError, TypeError, ArithmeticError):
        atr = None
    lo = hi = None
    reason = 'ATR14缺失或非正，使用手工输入'
    if atr is not None and atr > 0:
        price = D(str(snapshot['observed_bar']['close_price']))
        lo = max(tick, price-5*atr, snapshot.get('lower_limit') or tick)
        hi = min(price+5*atr, snapshot.get('upper_limit') or price+5*atr)
        lo = (lo/tick).to_integral_value(rounding=ROUND_CEILING)*tick
        hi = (hi/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
        if lo >= hi:
            lo = hi = None
            reason = '有效范围不足两个价位，使用手工输入'
        else:
            reason = None
    baseline = volume_baseline(snapshot)
    return dict(tick=tick, atr14=atr, slider_min=lo, slider_max=hi, slider_available=lo is not None,
                slider_reason=reason, lower_limit=snapshot.get('lower_limit'), upper_limit=snapshot.get('upper_limit'),
                min_effective_volume_ratio=D(snapshot['observed_bar']['volume'])/baseline if baseline else None)


def reference_ratio(plan, entry, direction, now):
    out = dict(status='UNAVAILABLE', ratio=None, reason_code='NO_REFERENCE_PLAN', gross=True, fees_included=False)
    if not plan:
        return out
    out.update(direction=plan['direction'], entry=entry, target=plan['target'], stop=plan['stop'])
    if now >= plan['valid_until'] or now < plan['valid_from']:
        out['reason_code'] = 'REFERENCE_EXPIRED'
    elif direction in ('NEUTRAL', 'UNKNOWN', 'CONFLICT'):
        out['reason_code'] = 'NO_DIRECTION'
    elif direction != plan['direction']:
        out['reason_code'] = 'DIRECTION_CONFLICT'
    else:
        target, stop = plan['target'], plan['stop']
        if direction == 'LONG' and stop < entry < target:
            ratio = (target-entry)/(entry-stop)
        elif direction == 'SHORT' and target < entry < stop:
            ratio = (entry-target)/(stop-entry)
        else:
            out['reason_code'] = 'INVALID_LEVELS'
            return out
        out.update(status='AVAILABLE', reason_code=None, ratio=ratio.quantize(D('.01'), rounding=ROUND_HALF_UP),
                   meets_threshold=ratio >= D('1.5'))
    return out


def direction_of(result):
    if any(result[s]['score'] is None for s in ('long', 'short')):
        return 'UNKNOWN'
    long, short = (result[s]['score'] >= 70 for s in ('long', 'short'))
    return 'CONFLICT' if long and short else 'LONG' if long else 'SHORT' if short else 'NEUTRAL'


def evaluate(snapshot, bar, now):
    r = evaluate_rules(bar, snapshot['history_15m'], snapshot['history_60m'])
    direction = direction_of(r)
    baseline = volume_baseline(snapshot)
    prev_oi = number(snapshot['history_15m'][-1], 'open_interest') if snapshot['history_15m'] else None
    oi = number(bar, 'open_interest')
    return dict(price=number(bar, 'close_price'), volume_ratio=D(bar['volume'])/baseline if baseline else None,
                oi_change=oi-prev_oi if oi is not None and prev_oi is not None else None,
                long=r['long'], short=r['short'], state=direction,
                reward_risk=reference_ratio(snapshot.get('reference_plan'), number(bar, 'close_price'), direction, now))


def compare(snapshot, request, now):
    # Inputs are limited to 32 digits; headroom avoids Decimal default precision failures.
    with localcontext() as ctx:
        ctx.prec = 80
        return _compare(snapshot, request, now)


def _compare(snapshot, request, now):
    observed = snapshot['observed_bar']
    candidate = deepcopy(observed)
    baseline = volume_baseline(snapshot)
    if not request.use_defaults:
        history = snapshot['history_15m']
        prev_oi = number(history[-1], 'open_interest') if history else None
        if prev_oi is None:
            raise SimulatorError('OI_BASELINE_UNAVAILABLE', '上一闭合柱持仓缺失，无法校验假设总持仓')
        if baseline is None:
            raise SimulatorError('VOLUME_BASELINE_UNAVAILABLE', '前20根闭合15分钟柱的有效均量不足')
        price, ratio = D(request.simulate_price), D(request.volume_ratio)
        validate_price(snapshot, price)
        volume = int((ratio*baseline).to_integral_value(rounding=ROUND_HALF_UP))
        if volume < observed['volume']:
            raise SimulatorError('BELOW_OBSERVED_VOLUME', '假设整周期成交量不能少于已发生成交量',
                                 fields=dict(observed_volume=observed['volume'], minimum_ratio=D(observed['volume'])/baseline))
        oi = prev_oi+request.open_interest_change
        if oi < 0:
            raise SimulatorError('NEGATIVE_OPEN_INTEREST', '假设总持仓不能为负')
        candidate.update(close_price=price, high_price=max(number(observed, 'high_price'), price),
                         low_price=min(number(observed, 'low_price'), price), volume=volume, open_interest=oi)
    candidate['hypothetical'] = True
    current, simulated = evaluate(snapshot, observed, now), evaluate(snapshot, candidate, now)
    changes, score_changes = [], {}
    for side in ('long', 'short'):
        left, right = current[side]['score'], simulated[side]['score']
        score_changes[side] = right-left if left is not None and right is not None else None
        for a, b in zip(current[side]['conditions'], simulated[side]['conditions']):
            delta = b['points']-a['points'] if a['points'] is not None and b['points'] is not None else None
            changes.append(dict(direction=side, condition_code=a['code'], current_points=a['points'],
                                simulated_points=b['points'], points_delta=delta))
    rr, direction = simulated['reward_risk'], simulated['state']
    action, text = 'OBSERVE', '仅为假设收盘情景，等待真实闭合行情验证；评分不是胜率。'
    if direction == 'UNKNOWN':
        action, text = 'COMPLETE_DATA', '数据不足，无法评估完整规则评分；保留已知条件。'
    elif direction == 'CONFLICT':
        text = '多空条件冲突，仅作观察。'
    elif rr['status'] == 'AVAILABLE':
        action = 'RR_ACCEPTABLE' if rr['meets_threshold'] else 'DO_NOT_CHASE'
        text = ('原计划毛收益风险比达到观察门槛，仍需真实收盘确认。' if rr['meets_threshold']
                else '不宜按原计划追价：毛收益风险比不足1.5。')
    elif rr['reason_code'] in ('INVALID_LEVELS', 'DIRECTION_CONFLICT', 'REFERENCE_EXPIRED'):
        action, text = 'PLAN_NOT_APPLICABLE', '原参考计划在此情景下不适用，不能判断追价性价比。'
    context = {k: snapshot[k] for k in ('instrument', 'timeframe', 'trading_day', 'frozen_at', 'quote_at', 'expires_at')}
    return dict(snapshot_id=snapshot['snapshot_id'], client_sequence=request.client_sequence,
                rules_version=snapshot['rules_version'], context=context, inputs=request.model_dump(exclude_none=True),
                effective_values=candidate, current=current, simulated=simulated, changes=changes, score_changes=score_changes,
                conclusion=dict(direction=direction, action=action, reason_codes=[rr['reason_code']] if rr['reason_code'] else [], text=text),
                baseline_stale=(now-snapshot['quote_at']).total_seconds() > 25 or now >= snapshot['session_end'],
                quality=snapshot['quality'])


def presets(snapshot):
    history, tick = snapshot['history_15m'][-20:], snapshot['tick']
    price = number(snapshot['observed_bar'], 'close_price')
    enough = len(history) == 20
    hi = max(number(b, 'high_price') for b in history) if enough else None
    lo = min(number(b, 'low_price') for b in history) if enough else None
    options = [('HIGH_BREAK', '前高上方一跳', hi+tick if hi is not None else None, None),
               ('LOW_BREAK', '前低下方一跳', lo-tick if lo is not None else None, None),
               ('VOLUME_BREAK', '放量突破', hi+tick if hi is not None else None, D('1.8')),
               ('LOW_VOLUME', '缩量0.7倍', None, D('.7')),
               ('UP_1', '上涨1%', (price*D('1.01')/tick).to_integral_value(rounding=ROUND_HALF_UP)*tick, None),
               ('DOWN_1', '下跌1%', (price*D('.99')/tick).to_integral_value(rounding=ROUND_HALF_UP)*tick, None)]
    result = []
    baseline = volume_baseline(snapshot)
    for code, label, p, ratio in options:
        reason = None
        try:
            if p is None and code != 'LOW_VOLUME':
                raise SimulatorError('MISSING_HISTORY', '前20根历史不足')
            validate_price(snapshot, p if p is not None else price)
            if ratio is not None and (baseline is None or int((ratio*baseline).to_integral_value(rounding=ROUND_HALF_UP)) < snapshot['observed_bar']['volume']):
                raise SimulatorError('VOLUME_PRESET_INVALID', '量能基准不足或低于已发生成交量')
        except SimulatorError as exc:
            reason = exc.message
        result.append(dict(code=code, label=label, price=p, volume_ratio=ratio, available=reason is None, reason=reason))
    return result
