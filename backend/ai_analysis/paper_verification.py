"""用户启动的盘时段模拟验证。只调用研究分析与读取新浪报价，独立账本不下单。"""
import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite

from backend.ai_analysis import registry
from backend.ai_analysis.paper_verification_models import VerificationPlan, VerificationTrial
from backend.ai_analysis.paper_verification_engine import SINA_QUOTE_POLICY
from backend.common.product_specs import PRODUCT_SPECS, extract_product, get_spec
from backend.common.trading_time import FuturesTradingCalendar
from backend.models.database import AIAnalysisBatchModel, AIAnalysisRunModel, AIAnalysisCallModel
from backend.storage.mysql_client import SessionLocal
from backend.storage.redis_client import RedisClient

_gate = threading.RLock()
TERMINAL = {'CLOSED', 'SKIPPED', 'UNVERIFIABLE'}
COST_FIELDS = ('commission_per_side', 'slippage_ticks', 'model_input_price_per_million',
               'model_output_price_per_million')
ANALYSIS_END_BUFFER = timedelta(minutes=5)
REQUIREMENTS = (
    '固定规则模拟研判，使用真实行情数据，仅用本次分析冻结时刻前、所选连续交易小节内的证据，'
    '预测模型实际完成后10分钟内的走势，先收盘则截止到小节收盘；证据截点与小节边界以冻结的 session_forecast 为准，'
    '不引用其他小节补足盘中证据。证据不足一律返回 NO_JUDGMENT，不强行给方向。'
    'LONG/SHORT 须给出数值触发价、目标价、失效价：偏多满足失效价＜触发价＜目标价，'
    '偏空满足目标价＜触发价＜失效价。触发价须为冻结时刻后尚待突破确认的价位；'
    '允许参考历史支撑压力，但冻结前已发生的突破不算触发。'
    '冻结前行情仅作证据，不算预测命中。输出契约其余不变。')


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


def _wall(value):
    result = datetime.fromisoformat(str(value)) if not isinstance(value, datetime) else value
    if result.tzinfo:
        result = result.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    return result


def _utc_to_wall(value):
    return (_wall(value) if value.tzinfo else value + timedelta(hours=8)) if value else None


def _analysis_windows(selected, config):
    start, end = _wall(selected['start_time']), _wall(selected['end_time'])
    due = start + timedelta(minutes=config.get('analysis_delay_minutes', 15))
    interval = 0 if _result_driven(config) else config.get('analysis_interval_minutes', 0)
    while due < end - ANALYSIS_END_BUFFER:
        yield start, end, due
        if not interval:
            break  # 已冻结的旧计划保持每小节一次。
        due += timedelta(minutes=interval)


def _result_driven(config):
    return config.get('analysis_schedule_mode') == 'RESULT_DRIVEN'


def _due_second(value):
    """数据库窗口键为 DATETIME(0)，向上取整避免重复或提前超时重试。"""
    return value.replace(microsecond=0) + (timedelta(seconds=1) if value.microsecond else timedelta())


def _plan_json(plan):
    return {'plan_id': plan.plan_id, 'instrument': plan.instrument, 'model_config_id': plan.model_config_id,
            'model_display_name': plan.model_display_name, 'status': plan.status,
            'config': json.loads(plan.config_json), 'start_day': plan.start_day, 'end_day': plan.end_day,
            'created_at': _utc_to_wall(plan.created_at).isoformat(),
            'stopped_at': plan.stopped_at.isoformat() if plan.stopped_at else None}


def _owned(db, owner, plan_id):
    plan = db.query(VerificationPlan).filter_by(plan_id=plan_id, owner_username=owner).first()
    if not plan:
        raise LookupError('模拟验证计划不存在')
    return plan


def _requirements(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError('请填写计划提示词，最多 2000 字')
    return value.strip()


def update_requirements(owner, plan_id, requirements):
    value = _requirements(requirements)
    with _gate, SessionLocal() as db:
        plan = _owned(db, owner, plan_id)
        if plan.status != 'STOPPED':
            raise ValueError('仅已停止并结算完成的计划可修改提示词')
        config = json.loads(plan.config_json)
        old = config.get('analysis_requirements', REQUIREMENTS)
        if old != value:
            config.setdefault('requirements_history', []).append({
                'changed_at': datetime.now().isoformat(), 'previous_requirements': old})
            config['analysis_requirements'] = value
            plan.config_json = _json(config)
            db.commit()
    return get_report(owner, plan_id)


def create_plan(owner, data, now=None, *, copied_from_plan_id=None):
    now = _wall(now or datetime.now())
    instrument = str(data.get('instrument') or '').strip().upper()
    if not re.fullmatch(r'[A-Z]{1,3}\d{3,4}', instrument) or extract_product(instrument) not in PRODUCT_SPECS:
        raise ValueError('请选择已知品种的具体交割月合约，不能使用主连')
    config = {}
    for field in COST_FIELDS:
        try:
            value = Decimal(str(data[field] if field == 'commission_per_side' else data.get(field, 0)))
            if not value.is_finite() or not 0 <= value <= 1000000:
                raise ValueError
            if field == 'slippage_ticks' and (value != value.to_integral_value() or value > 100):
                raise ValueError
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ValueError('请明确填写有效的手续费、整数滑点及模型人民币价格（允许零）')
        config[field] = float(value)
    config['analysis_requirements'] = _requirements(data.get('analysis_requirements', REQUIREMENTS))
    model_id = str(data.get('model_config_id') or '')
    runtime = registry.model_runtime_config(owner, model_id)
    multiplier, tick_size = get_spec(instrument)
    config.update(volume_multiple=multiplier, price_tick=tick_size, analysis_delay_minutes=15,
                  analysis_schedule_mode='RESULT_DRIVEN', analysis_timeout_minutes=10,
                  analysis_result_trigger='TRADE_OUTCOME', signal_timeout_minutes=5, position_timeout_minutes=10,
                  no_direction_retry_minutes=5, volume=1, target_days=20,
                  execution_mode='AUTO_PAPER', rule_version='1.5', outcome_policy='ACTUAL_NET',
                  quote_policy=SINA_QUOTE_POLICY, quote_max_age_seconds=30, quote_receipt_max_age_seconds=10)
    if copied_from_plan_id:
        config['copied_from_plan_id'] = copied_from_plan_id
    options = FuturesTradingCalendar.analysis_session_options(now, instrument)
    eligible = [start for item in options for start, end, due in _analysis_windows(item, config)
                if (_result_driven(config) and now < end - ANALYSIS_END_BUFFER) or due >= now]
    if not eligible and options:
        # During the final afternoon part the calendar still returns today's
        # cycle, even though its last analysis due has passed. Probe beyond its
        # end to reach the next actual period (including the next night).
        next_period = max(_wall(item['end_time']) for item in options) + timedelta(seconds=1)
        options = FuturesTradingCalendar.analysis_session_options(next_period, instrument)
        eligible = [start for item in options for start, end, due in _analysis_windows(item, config) if due >= now]
    if not eligible:
        raise ValueError('未找到启动之后的可分析小节')
    first_start = min(eligible)
    start_day = FuturesTradingCalendar.get_trading_day(first_start)
    end = datetime.strptime(start_day, '%Y%m%d').date()
    remaining = 19
    while remaining:
        end += timedelta(days=1)
        if FuturesTradingCalendar.is_trading_day(end):
            remaining -= 1
    with _gate, SessionLocal() as db:
        if db.query(VerificationPlan).filter(VerificationPlan.owner_username == owner,
                VerificationPlan.status.in_(['RUNNING', 'STOPPING'])).first():
            raise ValueError('已有运行或停止中的模拟验证计划，请先停止并完成结算')
        plan = VerificationPlan(plan_id=str(uuid.uuid4()), owner_username=owner, instrument=instrument,
            model_config_id=model_id, model_display_name=runtime['display_name'],
            model_config_hash=registry.config_hash(runtime), config_json=_json(config),
            start_day=start_day, end_day=end.strftime('%Y%m%d'), status='RUNNING',
            created_at=now - timedelta(hours=8))
        db.add(plan)
        db.commit()
        result_id = plan.plan_id
    return get_report(owner, result_id, now=now)


def copy_restart_plan(owner, plan_id, now=None):
    """Copy saved inputs into a fresh campaign; never mutate the source ledger."""
    with _gate:
        with SessionLocal() as db:
            plan = _owned(db, owner, plan_id)
            if plan.status not in ('STOPPED', 'COMPLETED'):
                raise ValueError('请先停止原计划并完成结算，再复制并重新开始')
            rows = db.query(VerificationTrial).filter_by(plan_id=plan_id).all()
            if any(row.status not in TERMINAL or json.loads(row.state_json).get('cost_pending') for row in rows):
                raise ValueError('原计划仍有未结清的交易或模型费用')
            config = json.loads(plan.config_json)
            data = {field:config[field] for field in COST_FIELDS}
            data.update(instrument=plan.instrument, model_config_id=plan.model_config_id,
                        analysis_requirements=config.get('analysis_requirements', REQUIREMENTS))
        return create_plan(owner, data, now=now, copied_from_plan_id=plan_id)


def list_plans(owner):
    with SessionLocal() as db:
        return {'items': [_plan_json(row) for row in db.query(VerificationPlan).filter_by(
            owner_username=owner).order_by(VerificationPlan.created_at.desc()).limit(100)]}


def upgrade_trade_outcome_schedule(owner, plan_id, now=None):
    """显式切换当前计划至1.3；保留历史账本、费用及原有持仓。"""
    now = _due_second(_wall(now or datetime.now()))
    with _gate, SessionLocal() as db:
        plan = _owned(db, owner, plan_id)
        config = json.loads(plan.config_json)
        if config.get('analysis_result_trigger') == 'TRADE_OUTCOME':
            return _plan_json(plan)
        if plan.status != 'RUNNING':
            raise ValueError('仅运行中的计划可切换调度规则')
        previous = {key: value for key, value in config.items() if key != 'schedule_history'}
        history = list(config.get('schedule_history', []))
        history.append({'config': previous,
            'start': config.get('analysis_rule_effective_at', _utc_to_wall(plan.created_at).isoformat()),
            'end': now.isoformat()})
        config.update(analysis_schedule_mode='RESULT_DRIVEN', analysis_result_trigger='TRADE_OUTCOME',
            signal_timeout_minutes=10, analysis_timeout_minutes=10, no_direction_retry_minutes=5,
            rule_version='1.3', analysis_rule_effective_at=now.isoformat(), schedule_history=history)
        for trial in db.query(VerificationTrial).filter_by(plan_id=plan_id):
            state = json.loads(trial.state_json)
            if state.get('next_analysis_due_at'):
                state['superseded_next_analysis_due_at'] = state.pop('next_analysis_due_at')
            state['schedule_upgraded_at'] = now.isoformat()
            if trial.status in ('SUBMITTED', 'SUBMITTING'):
                state.update(analysis_expired=True, cost_pending=True, next_result_scheduled=True)
                _skip(trial, state, 'RULE_SWITCH:旧规则待返回分析作废，迟到结果仅结算费用')
            elif trial.status == 'WAITING_TRIGGER':
                _skip(trial, state, 'RULE_SWITCH:切换行情结果驱动规则，旧未成交信号作废')
            elif trial.status == 'OPEN':
                state.update(result_driven=True, trade_outcome_driven=True,
                    analysis_direction_clear=True, next_result_scheduled=False)
            trial.state_json = _json(state)
        plan.config_json = _json(config)
        db.commit()
        return _plan_json(plan)


def _campaign_end(plan):
    last_day = datetime.strptime(plan.end_day, '%Y%m%d').replace(hour=14)
    return FuturesTradingCalendar.session_end(last_day, plan.instrument)


def _eligible_sessions(plan, cutoff):
    """Enumerate all elapsed campaign windows, including entire offline days."""
    cutoff = min(cutoff, plan.stopped_at or cutoff, _campaign_end(plan))
    created = _utc_to_wall(plan.created_at)
    config = json.loads(plan.config_json)
    segments = [(config, max(created, _wall(config.get('analysis_rule_effective_at', created))), cutoff)]
    for history in config.get('schedule_history', []):
        segments.append((history['config'], max(created, _wall(history['start'])),
                         min(cutoff, _wall(history['end']) - timedelta(microseconds=1))))
    day = datetime.strptime(plan.start_day, '%Y%m%d')
    final = datetime.strptime(plan.end_day, '%Y%m%d')
    seen = set()
    while day <= final:
        if FuturesTradingCalendar.is_trading_day(day.date()):
            # A morning probe yields that trading day's previous-night + day parts.
            for selected in FuturesTradingCalendar.analysis_session_options(day.replace(hour=10), plan.instrument):
                for schedule, begin, until in segments:
                    for start, end, due in _analysis_windows(selected, schedule):
                        if _result_driven(schedule):
                            due = _due_second(max(due, begin))
                        trading_day = FuturesTradingCalendar.get_trading_day(start)
                        if (due in seen or due >= end or not plan.start_day <= trading_day <= plan.end_day
                                or due < begin or due > until):
                            continue
                        seen.add(due)
                        yield selected, start, end, due, trading_day
        day += timedelta(days=1)


def _retry_active(config, row, due):
    if not config.get('resumed_at'):
        return True
    resumed = _wall(config['resumed_at'])
    if row.analysis_due_at < resumed <= due:
        return False
    return due >= _wall(config['analysis_rule_effective_at']) or any(
        _wall(part['start']) <= due < _wall(part['end']) for part in config.get('schedule_history', []))


def _expected_windows(db, plan, cutoff):
    windows = {due: (selected, start, end, due, day)
               for selected, start, end, due, day in _eligible_sessions(plan, cutoff)}
    cutoff = min(cutoff, plan.stopped_at or cutoff, _campaign_end(plan))
    config = json.loads(plan.config_json)
    for row in db.query(VerificationTrial).filter_by(plan_id=plan.plan_id):
        state = json.loads(row.state_json)
        if row.analysis_due_at <= cutoff:
            windows.setdefault(row.analysis_due_at, ({'label': row.session_label}, row.session_start,
                row.session_end, row.analysis_due_at, row.trading_day))
        next_at = state.get('next_analysis_due_at')
        if next_at:
            due = _due_second(_wall(next_at))
            if not _retry_active(config, row, due):
                continue
            if due < row.session_end - ANALYSIS_END_BUFFER and due <= cutoff:
                windows.setdefault(due, ({'label': row.session_label}, row.session_start,
                    row.session_end, due, row.trading_day))
    return [windows[due] for due in sorted(windows)]


def _plan_activity(plan, rows, now):
    """Read-only explanation of recorded work and the next eligible action."""
    config = json.loads(plan.config_json)
    states = [(row, json.loads(row.state_json)) for row in reversed(rows)]
    cost_pending = sum(bool(state.get('cost_pending')) for _, state in states)
    def result(phase, current, next_action, at=None):
        return {'phase':phase, 'current_action':current, 'next_action':next_action,
                'next_at':at.isoformat() if at else None, 'observed_at':now.isoformat(),
                'pending_cost_count':cost_pending}
    if plan.status in ('STOPPED','COMPLETED'):
        return result(plan.status, '计划已停止' if plan.status == 'STOPPED' else '计划已完成', '没有新的分析安排')
    open_trial = next(((row,state) for row,state in states if row.status == 'OPEN'), None)
    if open_trial:
        row, state = open_trial
        at = _wall(state['signal_deadline_at']) if state.get('signal_deadline_at') else None
        if plan.status == 'STOPPING':
            return result('STOPPING', '计划停止中，等待有效盘口平仓', '平仓并结清已发起分析的费用；不再分析')
        if state.get('settlement_pending') or ((state.get('trade_outcome_driven') or
                state.get('window_policy') == 'RESULT_AVAILABLE_10M') and at and now >= at):
            return result('WAITING_SETTLEMENT_QUOTE', '持仓已超时，等待新鲜报价结算',
                          '收到有效盘口后结算；小节结束前仍可分析时再开始下一次')
        limit = min(at, row.session_end) if at else row.session_end
        return result('HOLDING', '持仓中，观察止盈线和止损线',
                      '触及止盈/止损先结算；否则到截止时间按盘口结算，小节先结束则收盘结算', limit)
    if plan.status == 'STOPPING':
        return result('STOPPING', '计划停止中，等待已发起分析和费用结清', '结清后结束计划；不再分析')
    pending = next(((row,state) for row,state in states if row.status in ('SUBMITTED','SUBMITTING')), None)
    if pending:
        _, state = pending
        at = _wall(state['analysis_deadline_at']) if state.get('analysis_deadline_at') else None
        if at and now >= at:
            return result('ANALYSIS_OVERDUE', '模型未按时返回，等待后台作废处理',
                          '作废后符合时段规则时重试；迟到结果只记录费用')
        return result('ANALYZING', '已发起分析，等待模型返回结果',
                      '有效方向返回后观察行情；超过截止时间仍未返回则作废并按规则重试' if at else
                      '结果返回后按冻结规则继续处理', at)
    waiting = next(((row,state) for row,state in states if row.status == 'WAITING_TRIGGER'), None)
    if waiting:
        row, state = waiting
        at = _wall(state['signal_deadline_at']) if state.get('signal_deadline_at') else None
        if at and now >= at and now < row.session_end:
            return result('SIGNAL_OVERDUE', '未成交信号已到期，等待后台记录超时结果',
                          '记录未触发结果及费用；符合时段规则时再分析')
        return result('WAITING_TRIGGER', '分析已完成，等待新鲜行情触发进场',
                      '触发则进入持仓观察；失效或到期未成交则记录结果', min(at,row.session_end) if at else row.session_end)
    recorded = {row.analysis_due_at for row in rows}
    candidates = [(due,selected['label']) for selected,_,end,due,_ in
                  _eligible_sessions(plan,now+timedelta(days=7))
                  if due not in recorded and due >= now and due < end-ANALYSIS_END_BUFFER]
    for row,state in states:
        if state.get('next_analysis_due_at'):
            due = _wall(state['next_analysis_due_at'])
            if (_retry_active(config, row, due) and due >= now and due not in recorded
                    and due < row.session_end-ANALYSIS_END_BUFFER):
                candidates.append((due,row.session_label))
    next_at, label = min(candidates) if candidates else (None,None)
    sessions = FuturesTradingCalendar.analysis_session_options(now,plan.instrument)
    in_buffer = any(_wall(s['end_time'])-ANALYSIS_END_BUFFER <= now < _wall(s['end_time']) for s in sessions)
    if in_buffer:
        return result('END_BUFFER', '小节结束前最后5分钟，不再发起新分析',
                      f'等待{label}的下一次分析' if next_at else '等待下一可分析小节', next_at)
    if next_at:
        return result('WAITING_NEXT_ANALYSIS', '等待下一次分析时间', f'预计开始{label}分析', next_at)
    return result('WAITING_SCAN', '等待后台扫描确认下一步', '按交易时段和剩余计划日期确定是否可继续分析')


def _ai_result(run):
    """只读展示持久化输出；不返回模型配置、请求或原始响应。"""
    if not run:
        return None
    from backend.ai_analysis.provider import sanitize_error
    result = {'status':run.status, 'publication_status':None, 'review_status':None,
              'error':sanitize_error(run.sanitized_error) if run.sanitized_error else None}
    try:
        parsed = json.loads(run.parsed_response_json or '{}')
        if not isinstance(parsed, dict):
            raise ValueError
    except (TypeError, ValueError):
        result['error'] = '持久化 AI 输出格式无效，无法展示判断依据'
        return result
    for key in ('direction', 'summary', 'entry_condition', 'invalidation_condition',
                'publication_status', 'review_status'):
        if isinstance(parsed.get(key), str):
            result[key] = parsed[key]
    score = parsed.get('confidence_score')
    if type(score) in (int, float) and 0 <= score <= 100 and isfinite(score):
        result['confidence_score'] = score
    result['risks'] = [item for item in parsed.get('risks', []) if isinstance(item, str)] if isinstance(parsed.get('risks'), list) else []
    result['evidence'] = []
    for item in parsed.get('evidence', []) if isinstance(parsed.get('evidence'), list) else []:
        if isinstance(item, dict):
            evidence = {key:item[key] for key in ('side', 'time', 'field', 'reason') if isinstance(item.get(key), str)}
            value = item.get('value')
            if type(value) in (int, float):
                try:
                    if isfinite(value):
                        evidence['value'] = value
                except OverflowError:
                    pass  # 损坏历史输出不能使整份报告读取失败。
            result['evidence'].append(evidence)
    chart = parsed.get('session_chart')
    if isinstance(chart, dict) and isinstance(chart.get('reason'), str):
        result['chart_reason'] = chart['reason']
    quality = parsed.get('data_quality')
    if isinstance(quality, dict):
        result['data_quality'] = {'status':quality.get('status') if isinstance(quality.get('status'), str) else None,
            'warnings':[item for item in quality.get('warnings', []) if isinstance(item, str)] if isinstance(quality.get('warnings'), list) else []}
    return result


def get_report(owner, plan_id, now=None):
    from backend.ai_analysis.paper_verification_engine import summarize_trials, is_early_session_settlement, mark_position
    now = _wall(now or datetime.now())
    with SessionLocal() as db:
        plan = _owned(db, owner, plan_id)
        rows = db.query(VerificationTrial).filter_by(plan_id=plan_id).order_by(
            VerificationTrial.analysis_due_at).all()
        batch_ids = {row.batch_id for row in rows if row.batch_id}
        runs = {run.batch_id:run for run in db.query(AIAnalysisRunModel).join(AIAnalysisBatchModel,
            AIAnalysisRunModel.batch_id == AIAnalysisBatchModel.batch_id).filter(
                AIAnalysisBatchModel.batch_id.in_(batch_ids), AIAnalysisBatchModel.owner_username == owner,
                AIAnalysisBatchModel.instrument_id == plan.instrument,
                AIAnalysisRunModel.model_config_id == plan.model_config_id).all()} if batch_ids else {}
        trials, states = [], []
        config = json.loads(plan.config_json)
        quote = None
        if any(row.status == 'OPEN' for row in rows):
            try:
                quote = RedisClient.get_client().hgetall('quote:sina:'+plan.instrument)
            except Exception:
                pass  # 估值行情不可用不影响已实现账本读取。
        for row in rows:
            state = json.loads(row.state_json)
            state['early_session_settlement'] = is_early_session_settlement(state)
            state.update(status=row.status, reason=row.reason, trading_day=row.trading_day,
                session_end=row.session_end.isoformat(), result_available_at=(
                    row.result_available_at.isoformat() if row.result_available_at else None))
            states.append(state)
            trials.append({'trial_id':row.trial_id,'trading_day':row.trading_day,'session_label':row.session_label,
                'session_start':row.session_start.isoformat(),'session_end':row.session_end.isoformat(),
                'analysis_due_at':row.analysis_due_at.isoformat(),'batch_id':row.batch_id,'run_id':row.run_id,
                'status':row.status,'reason':row.reason,'result_available_at':state['result_available_at'],
                'model_cost':state.get('model_cost'),'model_cost_known':state.get('model_cost_known',False),
                'state':state, 'ai_result':_ai_result(runs.get(row.batch_id))})
            if row.status == 'OPEN':
                trials[-1]['live_pnl'] = mark_position(state,
                    quote if quote and quote.get('trading_day') == row.trading_day else None, now, config)
        report = summarize_trials(states)
        positions = [{'trial_id':row['trial_id'], 'direction':row['state'].get('direction'),
                      'entry_price':row['state'].get('entry_price'), **row['live_pnl']}
                     for row in trials if 'live_pnl' in row]
        live_known = all(row['status'] == 'LIVE' for row in positions)
        floating = round(sum(row['trading_net_pnl'] for row in positions), 8) if live_known else None
        incurred_open = round(sum(row['incurred_commission'] or 0 for row in positions),8)
        # Closed commissions are already included in the realized ledger. Keep
        # recorded entry fees for failed/unverifiable exits even without quotes.
        unsettled_commission = incurred_open
        for state in states:
            if state.get('status') in ('OPEN', 'CLOSED'):
                continue
            fee = state.get('entry_commission')
            if (isinstance(fee, (int, float)) and not isinstance(fee, bool)
                    and isfinite(fee) and fee >= 0 and state.get('entry_time') and state.get('entry_price')):
                unsettled_commission += fee
        report['incurred_trading_cost'] = round(report['trading_cost']+unsettled_commission,8)
        report['cash_net_pnl'] = round(report['total_net_pnl']-unsettled_commission,8)
        holding = round(sum(row['holding_net_pnl'] for row in positions),8) if live_known else None
        live = {'holding_net_pnl':holding, 'incurred_open_commission':incurred_open, 'open_count':len(positions), 'positions':positions, 'observed_at':now.isoformat(),
                'trading_net_pnl':floating,
                'estimated_total_net_pnl':round(report['cash_net_pnl']+floating+incurred_open,8)
                    if floating is not None and not report['model_cost_unknown'] else None}
        expected = len(_expected_windows(db, plan, _wall(now or datetime.now())))
        monitoring = {'expected_trials': expected, 'recorded_trials': len(rows),
            'missed_windows': sum(row.reason.startswith('MISSED_WINDOW:') for row in rows),
            'observed_trading_days': len({row.trading_day for row in rows}),
            'target_trading_days': 20}
        monitoring['complete'] = (plan.status == 'COMPLETED' and expected == len(rows)
            and monitoring['observed_trading_days'] == 20
            and not monitoring['missed_windows'] and report['is_complete'])
        report['is_complete'] = monitoring['complete']
        return {'plan':_plan_json(plan),'report':report,'trials':trials,'monitoring':monitoring,
                'live':live, 'activity':_plan_activity(plan,rows,now)}


def upgrade_auto_paper_rules(owner, plan_id, now=None):
    """仅升级当前计划规则，不派发分析，不修改历史终态或既有持仓价位。"""
    now = _wall(now or datetime.now())
    with _gate, SessionLocal() as db:
        plan = _owned(db, owner, plan_id)
        config = json.loads(plan.config_json)
        if plan.status != 'RUNNING' or config.get('analysis_result_trigger') != 'TRADE_OUTCOME':
            raise ValueError('仅运行中的交易结果驱动计划可升级自动模拟盘规则')
        if config.get('execution_mode') != 'AUTO_PAPER':
            config.setdefault('trade_rule_history', []).append({'changed_at':now.isoformat(),
                'previous_rule_version':config.get('rule_version'),
                'previous_signal_timeout_minutes':config.get('signal_timeout_minutes',10)})
            config.update(execution_mode='AUTO_PAPER', signal_timeout_minutes=5, position_timeout_minutes=10,
                          no_direction_retry_minutes=5, outcome_policy='ACTUAL_NET', rule_version='1.5',
                          trade_rule_effective_at=now.isoformat())
            plan.config_json = _json(config)
            for trial in db.query(VerificationTrial).filter(
                    VerificationTrial.plan_id == plan_id, VerificationTrial.status.in_(['WAITING_TRIGGER','OPEN'])).all():
                state = json.loads(trial.state_json)
                state['status'] = trial.status
                _apply_auto_trade_window(state, config)
                if trial.status == 'WAITING_TRIGGER':
                    state.pop('next_result_scheduled', None)
                    _next_after_result(trial, state, now, config)
                else:
                    state.pop('next_analysis_due_at', None)
                trial.state_json = _json(state)
            db.commit()
        return _plan_json(plan)


def stop_plan(owner, plan_id, now=None):
    with _gate, SessionLocal() as db:
        plan = _owned(db, owner, plan_id)
        if plan.status == 'RUNNING':
            stop_at = _wall(now or datetime.now())
            _schedule(db, plan, stop_at, allow_dispatch=False)
            plan.status = 'STOPPING'
            plan.stopped_at = stop_at
            db.commit()
    return get_report(owner, plan_id)


def resume_plan(owner, plan_id, now=None):
    now = _due_second(_wall(now or datetime.now()))
    with _gate, SessionLocal() as db:
        plan = _owned(db, owner, plan_id)
        if plan.status != 'STOPPED':
            raise ValueError('仅结算完成的已停止计划可重新启用')
        if now >= _campaign_end(plan):
            raise ValueError('原计划20个交易日周期已结束，请新建计划')
        if db.query(VerificationPlan).filter(VerificationPlan.owner_username == owner,
                VerificationPlan.status.in_(['RUNNING','STOPPING'])).first():
            raise ValueError('已有运行或停止中的计划，请先停止并完成结算')
        rows = db.query(VerificationTrial).filter_by(plan_id=plan_id).all()
        if any(row.status not in TERMINAL or json.loads(row.state_json).get('cost_pending') for row in rows):
            raise ValueError('计划仍有未结清的交易或模型费用，暂不能重新启用')
        runtime = registry.model_runtime_config(owner, plan.model_config_id)
        if registry.config_hash(runtime) != plan.model_config_hash:
            raise ValueError('模型配置已改变，请按新配置建立计划')
        config = json.loads(plan.config_json)
        previous = {key:value for key,value in config.items() if key != 'schedule_history'}
        stopped = plan.stopped_at or now
        config.setdefault('schedule_history', []).append({
            'config':previous,
            'start':config.get('analysis_rule_effective_at', _utc_to_wall(plan.created_at).isoformat()),
            'end':stopped.isoformat()})
        config.setdefault('resume_history', []).append({'stopped_at':stopped.isoformat(),'resumed_at':now.isoformat()})
        config.update(analysis_rule_effective_at=now.isoformat(), resumed_at=now.isoformat(),
                      outcome_policy='ACTUAL_NET', outcome_rule_effective_at=now.isoformat())
        plan.config_json = _json(config)
        plan.status, plan.stopped_at = 'RUNNING', None
        db.commit()
    return get_report(owner, plan_id, now=now)


def dispatch_analysis(plan, trial):
    from backend.api.ai_routes import AnalysisCreateBody, create_analysis_for_owner
    runtime = registry.model_runtime_config(plan.owner_username, plan.model_config_id)
    if registry.config_hash(runtime) != plan.model_config_hash:
        raise ValueError('模型配置已改变，请停止后按新配置建立计划')
    body = AnalysisCreateBody(client_request_id=uuid.UUID(trial.trial_id),
        model_config_ids=[uuid.UUID(plan.model_config_id)], analysis_target='CURRENT_SESSION',
        analysis_requirements=json.loads(plan.config_json).get('analysis_requirements', REQUIREMENTS),
        session_start_time=trial.session_start.isoformat(),
        use_review_summaries=False, allow_insufficient_data=False, self_consistency_votes=1)
    return create_analysis_for_owner(plan.owner_username, plan.instrument, body)


def _skip(trial, state, reason):
    trial.status = 'SKIPPED'
    trial.reason = reason[:512]
    state.update(status='SKIPPED', reason=trial.reason)


def _model_cost(db, run, config):
    rates = (Decimal(str(config['model_input_price_per_million'])),
             Decimal(str(config['model_output_price_per_million'])))
    calls = db.query(AIAnalysisCallModel).filter_by(run_id=run.run_id).all()
    if not any(rates):
        return 0.0
    if len(calls) < (run.call_count or 0):
        return None
    cost = Decimal('0')
    for call in calls:
        for rate, usage in zip(rates, (call.input_tokens, call.output_tokens)):
            if rate and (usage is None or usage < 0):
                return None
            cost += rate * Decimal(usage or 0) / Decimal('1000000')
    return float(cost.quantize(Decimal('0.000001')))


def _expire_analysis(trial, state, now):
    state.update(analysis_expired=True, cost_pending=True, model_cost_known=False)
    _skip(trial, state, 'TIMEOUT:分析发起后10分钟没有结果，已作废；迟到结果只结算费用')
    state['next_analysis_due_at'] = _due_second(_wall(state['analysis_deadline_at'])).isoformat()


def _apply_auto_trade_window(state, config):
    if config.get('execution_mode') != 'AUTO_PAPER' or not state.get('available_at'):
        return
    available = _wall(state['available_at'])
    state.update(execution_mode='AUTO_PAPER',
        entry_deadline_at=(available+timedelta(minutes=5)).isoformat(),
        position_deadline_at=(available+timedelta(minutes=10)).isoformat())
    state['signal_deadline_at'] = state['position_deadline_at'] if state.get('status') == 'OPEN' else state['entry_deadline_at']


def _next_after_result(trial, state, now, config):
    if not state.get('result_driven') or state.get('next_result_scheduled') or state.get('analysis_expired'):
        return
    clear = state.get('analysis_direction_clear', False)
    if config.get('analysis_result_trigger') == 'TRADE_OUTCOME' and clear:
        state['trade_outcome_driven'] = True
        if trial.status in ('WAITING_TRIGGER', 'OPEN'):
            _apply_auto_trade_window(state, config)
            deadline = (_wall(state['signal_deadline_at']) if config.get('execution_mode') == 'AUTO_PAPER'
                        else _wall(state['available_at']) + timedelta(minutes=config.get('signal_timeout_minutes', 10)))
            state['signal_deadline_at'] = deadline.isoformat()
            if trial.status == 'WAITING_TRIGGER':
                state['next_analysis_due_at'] = _due_second(deadline).isoformat()
            else:
                state.pop('next_analysis_due_at', None)
            state['analysis_outcome'] = 'WAITING_TRADE_OUTCOME'
            return
    due = now if clear else now + timedelta(minutes=config.get('no_direction_retry_minutes', 5))
    due = _due_second(max(due, trial.analysis_due_at + timedelta(seconds=1)))
    state.update(next_analysis_due_at=due.isoformat(), next_result_scheduled=True,
                 analysis_outcome=('TRADE_OUTCOME' if state.get('trade_outcome_driven') else 'DIRECTION') if clear else 'NO_VALID_DIRECTION')


def _receive_result(db, plan, trial, state, now, config):
    # 发起前先持久化的唯一 request_id 可在进程中断后恢复已提交批次，绝不重发。
    if not trial.batch_id:
        batch = db.query(AIAnalysisBatchModel).filter_by(owner_username=plan.owner_username,
            client_request_id=trial.trial_id).first()
        if batch:
            trial.batch_id = batch.batch_id
        else:
            state.update(model_cost=0, model_cost_known=True, cost_pending=False)
            _skip(trial, state, '提交中断，未找到已创建的分析批次；不补发')
            return
    batch = db.query(AIAnalysisBatchModel).filter_by(batch_id=trial.batch_id,
        owner_username=plan.owner_username, instrument_id=plan.instrument).first()
    run = db.query(AIAnalysisRunModel).filter_by(batch_id=trial.batch_id,
        model_config_id=plan.model_config_id).first()
    if not batch or not run:
        state.update(model_cost=None, model_cost_known=False, cost_pending=False)
        _skip(trial, state, '分析批次或模型记录遗失，费用无法核实')
        return
    expired = state.get('analysis_expired', False)
    if not expired:
        trial.status = 'SUBMITTED'
    if run.status in ('PENDING', 'RUNNING'):
        return
    available = _utc_to_wall(run.completed_at)
    if not available:
        state.update(model_cost=_model_cost(db, run, config), model_cost_known=False)
        state['model_cost_known'] = state['model_cost'] is not None
        state['cost_pending'] = False
        if not expired:
            _skip(trial, state, '模型终态缺少实际完成时间，结果不能用于成交')
        return
    if available > now:
        return
    trial.run_id, trial.result_available_at = run.run_id, available
    state.update(model_cost=_model_cost(db,run,config), model_cost_known=False,
                 available_at=available.isoformat(), result_available_at=available.isoformat(),
                 model_cost_at=available.isoformat(),
                 session_end=trial.session_end.isoformat(), snapshot_hash=batch.snapshot_hash)
    state['model_cost_known'] = state['model_cost'] is not None
    state['cost_pending'] = False
    if expired:
        return  # 超时批次迟到后仅补齐费用，不能恢复方向或改变下一次调度。
    if state.get('analysis_deadline_at') and available >= _wall(state['analysis_deadline_at']):
        known = state['model_cost_known']
        _expire_analysis(trial, state, now)
        state.update(cost_pending=False, model_cost_known=known)
        return
    # 较新的实际分析结果可用后，旧的未成交信号不再入场。已开仓仍按原规则退出。
    for old in db.query(VerificationTrial).filter(
            VerificationTrial.plan_id == plan.plan_id,
            VerificationTrial.status == 'WAITING_TRIGGER',
            VerificationTrial.analysis_due_at < trial.analysis_due_at).all():
        old_state = json.loads(old.state_json)
        _skip(old, old_state, '新分析结果已可用，旧的未成交信号失效')
        old.state_json = _json(old_state)
    newer = db.query(VerificationTrial).filter(
        VerificationTrial.plan_id == plan.plan_id,
        VerificationTrial.analysis_due_at > trial.analysis_due_at,
        VerificationTrial.result_available_at.isnot(None),
        VerificationTrial.result_available_at <= now).all()
    if any(not json.loads(row.state_json).get('analysis_expired') for row in newer):
        _skip(trial, state, '新分析结果已可用，较早返回的信号不再采用')
        return
    try:
        parsed = json.loads(run.parsed_response_json or '{}')
        if not isinstance(parsed, dict):
            raise ValueError
    except (TypeError, ValueError):
        _skip(trial, state, '模型结果JSON损坏或格式无效；已记录模型费用')
        return
    if plan.status != 'RUNNING':
        _skip(trial,state,'计划已停止，不采用后返回的方向；模型费用仍记录')
        return
    if available >= trial.session_end:
        _skip(trial,state,'模型结果在所选小节结束后返回')
        return
    if run.status != 'SUCCEEDED' or not run.call_count or parsed.get('publication_status') != 'REVIEWED':
        _skip(trial,state,'没有通过有效复核的实际模型结果')
        return
    if run.config_hash != plan.model_config_hash:
        _skip(trial,state,'实际模型参数与冻结计划不一致')
        return
    chart = parsed.get('session_chart') or {}
    if not isinstance(chart, dict):
        _skip(trial, state, '模型小节图表格式无效；已记录模型费用')
        return
    direction = parsed.get('direction')
    if direction not in ('LONG','SHORT') or chart.get('direction') != direction:
        _skip(trial,state,'模型无法判断、无明确方向或方向不一致')
        return
    try:
        snapshot = json.loads(batch.snapshot_json)
        target = snapshot['session_target']
        forecast = snapshot['session_forecast']
        if not isinstance(target, dict) or not isinstance(forecast, dict):
            raise ValueError
        start = _wall(forecast['start_time'])
        completed_window = chart.get('window_policy') == 'RESULT_AVAILABLE_10M'
        valid_window = (
            _wall(chart['evidence_cutoff_time']) == start
            and _wall(chart['session_end_time']) == trial.session_end
            and _wall(chart['forecast_start_time']) == available
            and _wall(chart['forecast_end_time']) == min(available + timedelta(minutes=10), trial.session_end)
        ) if completed_window else (
            _wall(chart['forecast_start_time']) == start
            and _wall(chart['forecast_end_time']) == trial.session_end)
        if (target.get('status') != 'ACTIVE' or _wall(target['start_time']) != trial.session_start
                or _wall(forecast['end_time']) != trial.session_end
                or not trial.analysis_due_at <= start < trial.analysis_due_at+timedelta(seconds=60)
                or not valid_window):
            raise ValueError
        trigger,target_price,stop = [Decimal(str(chart[field])) for field in
                                    ('trigger_price','target_price','invalidation_price')]
        if not all(price.is_finite() and price > 0 for price in (trigger,target_price,stop)):
            raise ValueError
        if not (stop < trigger < target_price if direction == 'LONG' else target_price < trigger < stop):
            raise ValueError
        reference = Decimal(str(forecast['reference_price']))
        if (not reference.is_finite() or reference <= 0
                or not (trigger > reference if direction == 'LONG' else trigger < reference)):
            raise ValueError
        if not all(isfinite(float(price)) for price in (trigger,target_price,stop)):
            raise ValueError
    except (KeyError,TypeError,ValueError,InvalidOperation,OverflowError):
        _skip(trial,state,'预测窗口或触发/目标/失效价不符合固定规则')
        return
    state['analysis_direction_clear'] = True
    if db.query(VerificationTrial).filter_by(plan_id=plan.plan_id,status='OPEN').first():
        _skip(trial,state,'已有模拟持仓，按最多一仓规则跳过')
        return
    state.update(status='WAITING_TRIGGER',direction=direction,trigger_price=float(trigger),
                 outcome_policy='ACTUAL_NET', window_policy='RESULT_AVAILABLE_10M',
                 signal_deadline_at=(available + timedelta(minutes=10)).isoformat(),
                 target_price=float(target_price),invalidation_price=float(stop),
                 reference_price=chart.get('reference_price'),reason='等待结果公布后的有效报价触发')
    trial.status, trial.reason = 'WAITING_TRIGGER', state['reason']


def _schedule(db, plan, now, allow_dispatch=True):
    existing = {row.analysis_due_at for row in db.query(VerificationTrial).filter_by(plan_id=plan.plan_id)}
    config = json.loads(plan.config_json)
    if (config.get('analysis_result_trigger') == 'TRADE_OUTCOME'
            and db.query(VerificationTrial).filter_by(plan_id=plan.plan_id, status='OPEN').first()):
        return  # 一仓尚未退出时等待其交易结果，不反复分析并因持仓占用跳过。
    for selected, start, end, due, trading_day in _expected_windows(db, plan, now):
        if due in existing:
            continue
        trial = VerificationTrial(trial_id=str(uuid.uuid4()),plan_id=plan.plan_id,trading_day=trading_day,
            session_label=selected['label'],session_start=start,session_end=end,analysis_due_at=due,
            status='SUBMITTING',state_json=_json({'model_cost':None,'model_cost_known':False}))
        db.add(trial)
        db.commit()  # 先固定唯一分析时间与request_id，再产生模型调用副作用。
        state = json.loads(trial.state_json)
        driven = _result_driven(config) and due >= _wall(config.get(
            'analysis_rule_effective_at', _utc_to_wall(plan.created_at)))
        state['result_driven'] = driven
        if now >= due+timedelta(seconds=60) or now >= end:
            state.update(model_cost=0, model_cost_known=True)
            _skip(trial,state,'MISSED_WINDOW:错过计划分析窗口，未补发')
            if driven and now < end and allow_dispatch:
                state['next_analysis_due_at'] = _due_second(now + timedelta(minutes=5)).isoformat()
        elif now >= end - ANALYSIS_END_BUFFER:
            state.update(model_cost=0, model_cost_known=True)
            _skip(trial,state,'SESSION_CUTOFF:距小节结束不足或等于5分钟，不再发起分析')
        elif not allow_dispatch:
            state.update(model_cost=0, model_cost_known=True)
            _skip(trial,state,'STOPPED:计划停止时尚未发起分析')
        else:
            if driven:
                deadline = now + timedelta(minutes=config.get('analysis_timeout_minutes', 10))
                state.update(submitted_at=now.isoformat(), analysis_deadline_at=deadline.isoformat(),
                             next_analysis_due_at=_due_second(deadline).isoformat())
                trial.state_json = _json(state)
                db.commit()  # 真实提交时间及超时边界同样须在外部调用前持久化。
            try:
                result = dispatch_analysis(plan,trial)
                trial.batch_id = result['batch_id']
                trial.status = 'SUBMITTED'
                state.update(model_cost=None,model_cost_known=False)
            except Exception as exc:
                # A batch commit may precede a response/dispatch exception. Recover
                # by the durable request ID before deciding there was no expense.
                # End the old repeatable-read snapshot: dispatch uses another DB
                # session and its committed batch must become visible here.
                db.rollback()
                batch = db.query(AIAnalysisBatchModel).filter_by(owner_username=plan.owner_username,
                    client_request_id=trial.trial_id, instrument_id=plan.instrument).first()
                if batch:
                    trial.batch_id, trial.status = batch.batch_id, 'SUBMITTED'
                    state.update(model_cost=None, model_cost_known=False)
                else:
                    from backend.ai_analysis.provider import sanitize_error
                    state.update(model_cost=0, model_cost_known=True)
                    _skip(trial,state,'分析未成功提交：'+sanitize_error(str(getattr(exc,'detail',exc))))
                    if driven:
                        _next_after_result(trial, state, now, config)
        trial.state_json = _json(state)
        db.commit()


def tick(now=None):
    from backend.ai_analysis.paper_verification_engine import advance_trade
    live_clock = now is None
    now = _wall(now or datetime.now())
    with _gate, SessionLocal() as db:
        plans = db.query(VerificationPlan).filter(VerificationPlan.status.in_(['RUNNING','STOPPING'])).all()
        for plan in plans:
            config = json.loads(plan.config_json)
            if config.get('outcome_policy') != 'ACTUAL_NET':
                config.update(outcome_policy='ACTUAL_NET', outcome_rule_effective_at=now.isoformat(),
                              previous_outcome_policy='LEGACY_TIMEOUT_LOSS')
                plan.config_json = _json(config)
                db.commit()
            rows = db.query(VerificationTrial).filter_by(plan_id=plan.plan_id).order_by(
                VerificationTrial.analysis_due_at.desc()).all()
            # 覆盖目标日最后一个小节；20日完成后不再发新分析。
            campaign_end = _campaign_end(plan)
            finishing = now >= campaign_end
            for trial in rows:
                state = json.loads(trial.state_json)
                if trial.status in TERMINAL and not state.get('cost_pending'):
                    continue
                if trial.status in ('SUBMITTED','SUBMITTING') or state.get('cost_pending'):
                    _receive_result(db,plan,trial,state,now,config)
                    if (trial.status in ('SUBMITTED','SUBMITTING') and state.get('analysis_deadline_at')
                            and now >= _wall(state['analysis_deadline_at'])):
                        _expire_analysis(trial, state, now)
                    elif trial.status not in ('SUBMITTED','SUBMITTING'):
                        _next_after_result(trial, state, now, config)
                if trial.status in ('WAITING_TRIGGER','OPEN'):
                    if config.get('execution_mode') == 'AUTO_PAPER':
                        state['quote_policy'] = SINA_QUOTE_POLICY
                    state['outcome_policy'] = 'ACTUAL_NET'
                    state['window_policy'] = 'RESULT_AVAILABLE_10M'
                    if state.get('available_at'):
                        state['signal_deadline_at'] = (_wall(state['available_at']) + timedelta(minutes=10)).isoformat()
                    _apply_auto_trade_window(state, config)
                    state['force_close'] = plan.status == 'STOPPING'
                    occupied = (trial.status == 'WAITING_TRIGGER' and plan.status == 'RUNNING'
                        and db.query(VerificationTrial).filter_by(plan_id=plan.plan_id,status='OPEN').first())
                    if occupied:
                        _skip(trial, state, '已有模拟持仓，按最多一仓规则跳过')
                        trial.state_json = _json(state)
                        db.commit()
                        continue
                    quote = RedisClient.get_client().hgetall('quote:sina:'+plan.instrument)
                    if quote and quote.get('trading_day') != trial.trading_day:
                        quote = None
                    # Database/lock work precedes the Redis read. A quote
                    # received meanwhile must not look newer than the scan.
                    execution_now = _wall(datetime.now()) if live_clock else now
                    state = advance_trade(state,quote,execution_now,config)
                    trial.status = state['status']
                    trial.reason = str(state.get('reason') or state.get('exit_reason') or '')[:512]
                    if config.get('analysis_result_trigger') == 'TRADE_OUTCOME':
                        _next_after_result(trial, state, execution_now, config)
                trial.state_json = _json(state)
                db.commit()
            if plan.status == 'RUNNING':
                _schedule(db,plan,now)
                if finishing:
                    plan.status,plan.stopped_at = 'STOPPING',campaign_end
                    db.commit()
            elif plan.status == 'STOPPING':
                _schedule(db,plan,now,allow_dispatch=False)
            if plan.status == 'STOPPING':
                pending = any(row.status not in TERMINAL or json.loads(row.state_json).get('cost_pending')
                    for row in db.query(VerificationTrial).filter_by(plan_id=plan.plan_id))
                if not pending:
                    expired_normally = plan.stopped_at is not None and plan.stopped_at >= campaign_end
                    plan.status = 'COMPLETED' if expired_normally else 'STOPPED'
                    db.commit()
