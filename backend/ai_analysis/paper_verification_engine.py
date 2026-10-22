"""Pure one-lot sampled-quote execution; all amounts are RMB per contract lot.

This module never fetches quotes or places orders. Naive times mean Beijing;
aware times are converted to Beijing. Fill prices already include adverse
slippage, so trading_cost contains the two commissions only. Model costs are
charged separately, including analyses that did not produce a trade.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import isfinite


BEIJING = timezone(timedelta(hours=8))
TERMINAL = {"CLOSED", "SKIPPED", "UNVERIFIABLE"}
SIGNAL_TIMEOUT = 'SIGNAL_TIMEOUT:分析完成后10分钟未出现行情或交易结果，未成交信号作废'
SINA_QUOTE_POLICY = 'SINA_SOURCE_30S_RECEIPT_10S'


def _time(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(BEIJING).replace(tzinfo=None)
        return parsed
    except (ValueError, TypeError, OverflowError):
        return None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _positive(value):
    value = _number(value)
    return value if value is not None and value > 0 else None


def _cost_spec(costs):
    commission = _number(costs.get("commission_per_side"))
    ticks = _number(costs.get("slippage_ticks"))
    tick = _positive(costs.get("price_tick"))
    multiple = _positive(costs.get("volume_multiple"))
    if (commission is None or commission < 0 or ticks is None or ticks < 0 or not tick or not multiple
            or not isfinite(ticks * tick) or not isfinite(commission * 2)):
        return None
    return commission, ticks * tick, multiple


def _inspect_book(quote, now, available, after=None, policy=None):
    check = {'checked_at': now.isoformat()}
    def reject(reason):
        return None, dict(check, reason=reason)
    if not isinstance(quote, dict):
        return reject('MISSING_QUOTE')
    at = _time(quote.get("timestamp"))
    received = _time(quote.get('received_at'))
    bid, ask = _positive(quote.get("bid_price_1")), _positive(quote.get("ask_price_1"))
    last = _positive(quote.get("last_price"))
    check.update(source_time=at.isoformat() if at else None, received_at=received.isoformat() if received else None,
                 last_price=last, bid_price_1=bid, ask_price_1=ask)
    if at is None:
        return reject('INVALID_QUOTE_TIME')
    age = (now-at).total_seconds()
    check['source_age_seconds'] = round(age, 3)
    if at <= available:
        return reject('BEFORE_RESULT')
    if at > now:
        return reject('FUTURE_QUOTE')
    if age > (30 if policy == SINA_QUOTE_POLICY else 10):
        return reject('STALE_QUOTE')
    if policy == SINA_QUOTE_POLICY:
        # Older caches retain the strict ten-second source-time rule. A newly
        # received, bounded-delay Sina quote can use the thirty-second limit.
        if received is None and age > 10:
            return reject('MISSING_RECEIPT_TIME')
        if received is not None:
            check['receipt_age_seconds'] = round((now-received).total_seconds(), 3)
            if received > now or received < at:
                return reject('INVALID_RECEIPT_TIME')
            if (now-received).total_seconds() > 10:
                return reject('STALE_RECEIPT')
    if after is not None and at <= after:
        return reject('ALREADY_PROCESSED')
    if not bid or not ask or not last or bid > ask:
        return reject('INVALID_BOOK')
    return (at, last, bid, ask), dict(check, reason='ACCEPTED')


def _book(quote, now, available, after=None, policy=None):
    return _inspect_book(quote, now, available, after, policy)[0]


def _skip(state, reason):
    state.update(status="SKIPPED", reason=reason)
    return state


def _close(state, bid, ask, at, reason, spec):
    commission, slip, multiple = spec
    long = state["direction"] == "LONG"
    fill = bid - slip if long else ask + slip
    if fill <= 0 or not isfinite(fill):
        state.update(status="UNVERIFIABLE", reason="INVALID_EXIT_PRICE")
        return state
    gross = (fill - state["entry_price"]) * multiple * (1 if long else -1)
    if not isfinite(gross) or not isfinite(gross - commission * 2):
        state.update(status="UNVERIFIABLE", reason="INVALID_FINANCIAL_RESULT")
        return state
    state.update(status="CLOSED", settlement_pending=False, exit_price=round(fill, 8), exit_time=at.isoformat(),
                 exit_reason=reason, reason=reason, entry_commission=round(commission, 8),
                 exit_commission=round(commission, 8), gross_pnl=round(gross, 8),
                 trading_cost=round(commission * 2, 8),
                 trading_net_pnl=round(gross - commission * 2, 8))
    return state


def mark_position(state, quote, now, costs):
    """只读估值：按可平仓盘口、不利滑点及双边手续费计算，绝不产生退出记录。"""
    unavailable = {'status': 'STALE_OR_MISSING', 'quote_time': None, 'exit_price_estimate': None,
                   'incurred_commission': None, 'exit_commission_estimate': None, 'holding_net_pnl': None,
                   'gross_pnl': None, 'trading_cost': None, 'trading_net_pnl': None, 'total_net_pnl': None}
    current, available, entry_at = _time(now), _time(state.get('available_at')), _time(state.get('entry_time'))
    spec, entry = _cost_spec(costs), _positive(state.get('entry_price'))
    if (state.get('status') != 'OPEN' or current is None or available is None or entry_at is None
            or spec is None or entry is None or state.get('direction') not in ('LONG', 'SHORT')):
        return unavailable
    unavailable['incurred_commission'] = round(spec[0],8)
    unavailable['exit_commission_estimate'] = round(spec[0],8)
    book = _book(quote, current, available, policy=state.get('quote_policy'))
    if book is None or book[0] < entry_at:
        return unavailable
    at, _, bid, ask = book
    commission, slip, multiple = spec
    long = state['direction'] == 'LONG'
    fill = bid - slip if long else ask + slip
    gross = (fill-entry) * multiple * (1 if long else -1)
    net = gross - commission*2
    if fill <= 0 or not all(isfinite(value) for value in (fill, gross, net)):
        return unavailable
    fee = _number(state.get('model_cost'))
    total = net-fee if state.get('model_cost_known') and fee is not None and fee >= 0 else None
    return {'status': 'LIVE', 'quote_time': at.isoformat(), 'exit_price_estimate': round(fill,8),
            'incurred_commission': round(commission,8), 'exit_commission_estimate': round(commission,8),
            'holding_net_pnl': round(gross-commission,8),
            'gross_pnl': round(gross,8), 'trading_cost': round(commission*2,8),
            'trading_net_pnl': round(net,8), 'total_net_pnl': round(total,8) if total is not None else None}


def advance_trade(state: dict, quote: dict | None, now: datetime, costs: dict) -> dict:
    """Advance once from strictly newer fresh quotes, returning a deep copy.

    WAITING_TRIGGER uses last price to trigger and the executable opposite book
    to enter. OPEN uses its executable closing book to trigger either exit.
    At session_end, only a book observed on an earlier call may close a trade.
    """
    result = deepcopy(state)
    status = result.get("status")
    if status in TERMINAL or status not in {"WAITING_TRIGGER", "OPEN"}:
        return result
    if status == "WAITING_TRIGGER" and result.get("force_close"):
        return _skip(result, "STOPPED")
    current, available, end = _time(now), _time(result.get("available_at")), _time(result.get("session_end"))
    if current is None or available is None or end is None or available >= end:
        if status == "WAITING_TRIGGER":
            return _skip(result, "INVALID_WINDOW")
        result.update(status="UNVERIFIABLE", reason="INVALID_WINDOW")
        return result
    spec = _cost_spec(costs)
    levels = [_positive(result.get(key)) for key in ("trigger_price", "target_price", "invalidation_price")]
    if result.get("direction") not in {"LONG", "SHORT"} or any(value is None for value in levels) or spec is None:
        if status == "WAITING_TRIGGER":
            return _skip(result, "INVALID_TRADE_SPEC")
        result.update(status="UNVERIFIABLE", reason="INVALID_TRADE_SPEC")
        return result
    last_at = _time(result.get("last_quote_time"))
    entry_at = _time(result.get("entry_time"))
    deadline = (_time(result.get('entry_deadline_at' if status == 'WAITING_TRIGGER' else 'position_deadline_at'))
                or _time(result.get('signal_deadline_at')) or available + timedelta(minutes=10))
    timeout_minutes = max(0, int((deadline-available).total_seconds() / 60))
    timeout_reason = (f'SIGNAL_TIMEOUT:分析完成后{timeout_minutes}分钟未触发，未成交信号作废'
                      if result.get('entry_deadline_at') else SIGNAL_TIMEOUT)
    ten_minute = result.get('trade_outcome_driven') or result.get('window_policy') == 'RESULT_AVAILABLE_10M'
    if status == "OPEN":
        entry = _positive(result.get("entry_price"))
        if entry is None or entry_at is None or entry_at <= available or entry_at >= end:
            result.update(status="UNVERIFIABLE", reason="INVALID_ENTRY")
            return result
        result["entry_price"] = entry
        if ten_minute and current >= deadline:
            result.update(settlement_pending=True, settlement_pending_since=deadline.isoformat())
    if current >= end:
        if status == "WAITING_TRIGGER":
            return _skip(result, "NOT_TRIGGERED")
        bid, ask = _positive(result.get("last_bid")), _positive(result.get("last_ask"))
        if (last_at is None or last_at < end - timedelta(seconds=60) or last_at >= end
                or last_at < entry_at or not bid or not ask or bid > ask):
            result.update(status="UNVERIFIABLE", reason="MISSING_SESSION_END_QUOTE")
            return result
        reason = 'SESSION_END_EARLY' if ten_minute and end < deadline else 'SESSION_END'
        return _close(result, bid, ask, end, reason, spec)
    after = max(filter(None, (last_at, entry_at)), default=None)
    book, check = _inspect_book(quote, current, available, after, result.get('quote_policy'))
    if status == 'WAITING_TRIGGER':
        diagnostics = result.setdefault('quote_diagnostics', {'accepted_count': 0, 'rejection_counts': {}})
        diagnostics['last_check'] = check
        if book is not None:
            diagnostics['accepted_count'] += 1
        else:
            counts = diagnostics['rejection_counts']
            counts[check['reason']] = counts.get(check['reason'], 0) + 1
            touch_at, touch_price = _time(check.get('source_time')), check.get('last_price')
            if (touch_at and available < touch_at <= min(deadline, end)
                    and touch_price and (touch_price >= levels[0] if result['direction'] == 'LONG' else touch_price <= levels[0])
                    and check['reason'] != 'ALREADY_PROCESSED' and touch_at <= current):
                diagnostics['rejected_touch'] = dict(check)
        if diagnostics.get('rejected_touch'):
            timeout_reason = f'SIGNAL_TIMEOUT:分析完成后{timeout_minutes}分钟内触线盘口未通过校验，未成交信号作废'
        elif not diagnostics['accepted_count']:
            timeout_reason = f'SIGNAL_TIMEOUT:分析完成后{timeout_minutes}分钟有效盘口不足，无法确认触发成交，未成交信号作废'
    signal_expired = status == 'WAITING_TRIGGER' and ten_minute and current >= deadline
    if book is None:
        return _skip(result, timeout_reason) if signal_expired else result
    at, last, bid, ask = book
    if signal_expired and at > deadline:
        return _skip(result, timeout_reason)  # 截止后的报价不能补造成截止内进场。
    if at >= end:
        return result
    result.update(last_quote_time=at.isoformat(), last_bid=bid, last_ask=ask)
    long = result["direction"] == "LONG"
    trigger, target, invalid = levels
    if status == "WAITING_TRIGGER":
        if ten_minute and (last <= invalid if long else last >= invalid):
            return _skip(result, "PRE_ENTRY_INVALIDATED")
        if not (last >= trigger if long else last <= trigger):
            return _skip(result, timeout_reason) if signal_expired else result
        slip = spec[1]
        fill = ask + slip if long else bid - slip
        inside = invalid < fill < target if long else target < fill < invalid
        if not inside or fill <= 0 or not isfinite(fill):
            return _skip(result, "ENTRY_OUTSIDE_RANGE")
        result.update(status="OPEN", entry_price=round(fill, 8), entry_time=at.isoformat(),
                      entry_commission=round(spec[0], 8))
        if result.get('position_deadline_at'):
            result['signal_deadline_at'] = result['position_deadline_at']
        return result  # Entry and exit never occur on the same sampled quote.
    if result.get("force_close"):
        return _close(result, bid, ask, at, "STOPPED", spec)
    if ten_minute and current >= deadline and at > deadline:
        return _close(result, bid, ask, at, 'RESULT_TIMEOUT', spec)
    executable = bid if long else ask
    if executable <= invalid if long else executable >= invalid:
        return _close(result, bid, ask, at, "STOP_LOSS", spec)
    if executable >= target if long else executable <= target:
        return _close(result, bid, ask, at, "TAKE_PROFIT", spec)
    if ten_minute and current >= deadline and at == deadline:
        return _close(result, bid, ask, at, 'RESULT_TIMEOUT', spec)
    return result


def is_early_session_settlement(row):
    """Also classify legacy session-end exits without changing the ledger."""
    if row.get('exit_reason') == 'SESSION_END_EARLY':
        return True
    if row.get('exit_reason') != 'SESSION_END' or not (row.get('trade_outcome_driven') or row.get('window_policy') == 'RESULT_AVAILABLE_10M'):
        return False
    available, end = _time(row.get('available_at')), _time(row.get('session_end'))
    deadline = (_time(row.get('position_deadline_at')) or _time(row.get('signal_deadline_at'))
                or (available + timedelta(minutes=10) if available else None))
    return bool(end and deadline and end < deadline)


def _day(row):
    value = str(row.get("trading_day") or "")
    if len(value) == 8 and value.isdigit():
        value = value[:4] + "-" + value[4:6] + "-" + value[6:]
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        at = _time(row.get("result_available_at")) or _time(row.get("exit_time")) or _time(row.get("session_end"))
        return at.date().isoformat() if at else "UNKNOWN"


def summarize_trials(rows: list[dict]) -> dict:
    """Summarize realized one-lot P&L and all known analysis costs.

    Win rate excludes short-session settlements. Legacy rules include unfilled
    timeout/pre-entry invalidation losses; AUTO_PAPER counts executed trades only.
    Legacy RESULT_TIMEOUT counts as a loss; ACTUAL_NET uses trade net less model fee.
    consecutive losses follow exit/deadline/invalidation order.
    Analysis failures do not enter win/loss counts. Research fees
    enter equity at result_available_at and realized trading net at exit_time.
    Missing costs/financial values, open trials and empty samples are incomplete.
    average_total_net_pnl divides the net including all research fees by verified
    closed trades. This is a sampled simulation, never a profitability verdict.
    """
    result = {"trial_count": len(rows), "closed_trades": 0, "wins": 0, "losses": 0,
              "signal_timeout_losses": 0, "signal_timeout_count": 0, "result_timeout_losses": 0, "result_timeout_count": 0, "evaluated_outcomes": 0,
              "pre_entry_losses": 0, "early_session_settlements": 0, "settlement_pending_count": 0,
              "skipped_trials": 0, "skip_reasons": {}, "unverifiable_trials": 0,
              "open_count": 0, "pending_count": 0, "gross_pnl": 0.0,
              "trading_cost": 0.0, "trading_net_pnl": 0.0, "model_cost": 0.0,
              "model_cost_unknown": 0, "total_net_pnl": 0.0, "win_rate_pct": None,
              "average_net_pnl": None, "average_total_net_pnl": None,
              "max_drawdown": 0.0, "max_consecutive_losses": 0, "daily": [],
              "equity_curve": [], "is_complete": bool(rows)}
    days, events, verified, outcomes = {}, [], [], []
    for index, outer in enumerate(rows):
        row = {**outer, **outer.get("state", {})} if isinstance(outer.get("state"), dict) else outer
        status = row.get("status")
        day = _day(row)
        if day == "UNKNOWN":
            result["is_complete"] = False
        daily = days.setdefault(day, {"trading_day": day, "closed_trades": 0,
                                     "trading_net_pnl": 0.0, "model_cost": 0.0,
                                     "total_net_pnl": 0.0})
        if status == "CLOSED":
            result["closed_trades"] += 1
            daily["closed_trades"] += 1
        elif status == "SKIPPED":
            result["skipped_trials"] += 1
            reason = str(row.get("reason") or "UNKNOWN")
            result["skip_reasons"][reason] = result["skip_reasons"].get(reason, 0) + 1
            if reason.split(':', 1)[0] == 'SIGNAL_TIMEOUT':
                result['signal_timeout_count'] += 1
                deadline = _time(row.get('signal_deadline_at')) or _time(row.get('session_end'))
                if deadline is None:
                    result['is_complete'] = False
                if row.get('execution_mode') != 'AUTO_PAPER':
                    result['signal_timeout_losses'] += 1
                    outcomes.append((deadline or datetime.min, index, -1))
            elif reason == 'PRE_ENTRY_INVALIDATED':
                result['pre_entry_losses'] += 1
                at = _time(row.get('last_quote_time')) or _time(row.get('result_available_at'))
                if at is None:
                    result['is_complete'] = False
                if row.get('execution_mode') != 'AUTO_PAPER':
                    outcomes.append((at or datetime.min, index, -1))
        elif status == "UNVERIFIABLE":
            result["unverifiable_trials"] += 1
            result["is_complete"] = False
        elif status == "OPEN":
            result["open_count"] += 1
            if row.get('settlement_pending'):
                result['settlement_pending_count'] += 1
            result["is_complete"] = False
        else:
            result["pending_count"] += 1
            result["is_complete"] = False
        fee = _number(row.get("model_cost"))
        known = row.get("model_cost_known", fee is not None)
        if not known or fee is None or fee < 0:
            result["model_cost_unknown"] += 1
            result["is_complete"] = False
        else:
            result["model_cost"] += fee
            daily["model_cost"] += fee
            fee_at = (_time(row.get("model_cost_at")) or _time(row.get("result_available_at"))
                      or _time(row.get("created_at")) or _time(row.get("session_end")))
            if fee > 0:
                if fee_at is None:
                    result["is_complete"] = False
                events.append((fee_at or datetime.min, 0, index, -fee, "MODEL_COST", day))
        if status != "CLOSED":
            continue
        amounts = [_number(row.get(key)) for key in ("gross_pnl", "trading_cost", "trading_net_pnl")]
        exit_at = _time(row.get("exit_time"))
        if any(value is None for value in amounts) or amounts[1] < 0 or exit_at is None:
            result["is_complete"] = False
            continue
        gross, trade_cost, net = amounts
        result["gross_pnl"] += gross
        result["trading_cost"] += trade_cost
        result["trading_net_pnl"] += net
        daily["trading_net_pnl"] += net
        verified.append((exit_at, index, net))
        events.append((exit_at, 1, index, net, "TRADE_EXIT", day))
        timed_out = row.get('exit_reason') == 'RESULT_TIMEOUT'
        if timed_out:
            result['result_timeout_count'] += 1
        early = is_early_session_settlement(row)
        if early:
            result['early_session_settlements'] += 1
        actual = row.get('outcome_policy') == 'ACTUAL_NET'
        outcome_net = net
        if actual:
            if not known or fee is None or fee < 0:
                continue  # 未结费用不能假装已经知道实际净收益及胜负。
            outcome_net -= fee
        if timed_out and (not actual or outcome_net < 0):
            result['result_timeout_losses'] += 1
        if not early:
            outcomes.append((exit_at, index, -1 if timed_out and not actual else outcome_net))
    outcomes.sort()
    result['evaluated_outcomes'] = len(outcomes)
    streak = 0
    for _, _, net in outcomes:
        if net > 0:
            result["wins"] += 1
        if net < 0:
            result["losses"] += 1
            streak += 1
            result["max_consecutive_losses"] = max(result["max_consecutive_losses"], streak)
        else:
            streak = 0
    result["total_net_pnl"] = result["trading_net_pnl"] - result["model_cost"]
    if outcomes:
        result["win_rate_pct"] = result["wins"] / len(outcomes) * 100
    if verified:
        result["average_net_pnl"] = result["trading_net_pnl"] / len(verified)
        result["average_total_net_pnl"] = result["total_net_pnl"] / len(verified)
    equity = peak = 0.0
    for at, _, index, amount, kind, day in sorted(events):
        equity += amount
        peak = max(peak, equity)
        result["max_drawdown"] = max(result["max_drawdown"], peak - equity)
        result["equity_curve"].append({"timestamp": at.isoformat() if at != datetime.min else None,
                                       "trading_day": day, "kind": kind, "trial_index": index,
                                       "amount": round(amount, 8), "equity": round(equity, 8)})
    for day in sorted(days):
        daily = days[day]
        daily["total_net_pnl"] = daily["trading_net_pnl"] - daily["model_cost"]
        result["daily"].append({key: round(value, 8) if isinstance(value, float) else value
                                for key, value in daily.items()})
    return {key: round(value, 8) if isinstance(value, float) else value for key, value in result.items()}
