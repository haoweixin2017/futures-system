"""AI 分析自动结算: 收盘后用实际行情为成功运行自动评分, 替代纯手工评分闭环。

规则:
- TODAY: 仅匹配冻结交易日的收盘前一分钟及冻结参考价;
- CURRENT_SESSION: 小节结束后, 取小节内最后一根收盘价对比;
- NEXT_OPEN: 目标时段开盘后, 取目标时段第一根 1 分钟开盘价对比 (UP/DOWN/FLAT 的真实方向);
- 噪声区间: max(3 × 最小变动价, 参考价 × 0.1%), 区间内视为未验证 (UNVERIFIABLE);
- 到期时目标行情暂缺: 宽限期 (_MISSING_BAR_GRACE) 内等待缺口回补后再评, 逾期才落 UNVERIFIABLE 终态;
- 人工评分保留，自动指标独立生成; 自动评分带固定前缀, 允许人工改判 (见 ai_routes.put_evaluation)。
"""
import json
import math
import logging
import uuid
from datetime import datetime, timedelta

from backend.models.database import (
    AIAnalysisBatchModel, AIAnalysisEvaluationModel, AIAnalysisRunModel, MarketBarModel,
)
from backend.storage.mysql_client import SessionLocal
from backend.ai_analysis.session_window import chart_for_completion, wall_time

logger = logging.getLogger(__name__)

AUTO_SETTLE_TAG = "【自动结算】"
AUTO_RATING_SCORES = {"CORRECT": 100, "WRONG": 0, "UNVERIFIABLE": None}
# 结算扫描 (300秒) 与行情缺口回补存在竞态, 到期后先等待回补, 逾期才落 UNVERIFIABLE 终态
_MISSING_BAR_GRACE = timedelta(days=3)


def _reference_price(snapshot: dict) -> float | None:
    quote_price = (snapshot.get("market", {}).get("quote") or {}).get("last_price")
    try:
        ref = float(quote_price)
        if math.isfinite(ref) and ref > 0:
            return ref
    except (TypeError, ValueError):
        pass
    bars = snapshot.get("market", {}).get("bars_1m") or []
    if bars:
        try:
            ref = float(bars[-1].get("close"))
            if math.isfinite(ref) and ref > 0:
                return ref
        except (TypeError, ValueError):
            pass
    return None


def _noise_threshold(reference: float, tick: float) -> float:
    return max(3 * tick, reference * 0.001)


def _judge(direction: str | None, delta: float, threshold: float):
    """返回 (rating, score) 或 (None, None) 表示无法判定。"""
    if direction in (None, "", "NO_JUDGMENT"):
        return "UNVERIFIABLE", None
    if direction in ("LONG", "UP"):
        if delta > threshold:
            return "CORRECT", 100
        if delta < -threshold:
            return "WRONG", 0
        return "UNVERIFIABLE", None
    if direction in ("SHORT", "DOWN"):
        if delta < -threshold:
            return "CORRECT", 100
        if delta > threshold:
            return "WRONG", 0
        return "UNVERIFIABLE", None
    if direction in ("NEUTRAL", "FLAT"):
        return ("CORRECT", 100) if abs(delta) <= threshold else ("WRONG", 0)
    return None, None


def _settle_frozen_price(session, batch, snapshot, now):
    target = snapshot.get("settlement_target") or {}
    ref = target.get("reference_price")
    try:
        ref = float(ref)
        bar_time = datetime.fromisoformat(target["bar_time"])
        due = datetime.fromisoformat(target["due_time"])
        day = target["trading_day"]
        field = target["price_field"]
        if not math.isfinite(ref) or ref <= 0 or field not in {"open_price", "close_price"}:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return {"reference": _reference_price(snapshot), "realized": None,
                "rating": "UNVERIFIABLE", "detail": "; 缺少有效冻结评分目标或基准，无法验证"}
    if now < due:
        return None
    bar = session.query(MarketBarModel).filter_by(
        instrument_id=batch.instrument_id, trading_day=day, period="1m", bar_time=bar_time).first()
    try:
        realized = float(getattr(bar, field))
        if not math.isfinite(realized) or realized <= 0:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        if now < due + _MISSING_BAR_GRACE:
            return None
        return {"reference": ref, "realized": None, "rating": "UNVERIFIABLE",
                "detail": f"; 缺少目标行情 {day} {bar_time.isoformat()} {field}"}
    return {"reference": ref, "realized": realized,
            "detail": f"; 冻结评分行情 {day} {bar_time.isoformat()} {field}"}


def _settle_today(session, run, batch, snapshot, now: datetime):
    return _settle_frozen_price(session, batch, snapshot, now)


def _settle_session(session, run, batch, snapshot, now: datetime):
    try:
        parsed = json.loads(run.parsed_response_json or '{}')
        chart = chart_for_completion(parsed.get('session_chart') or {}, getattr(run, 'completed_at', None))
    except (TypeError, ValueError):
        chart = {}
    if chart.get('window_policy') == 'RESULT_AVAILABLE_10M':
        return _settle_completed_window(session, run, batch, snapshot, chart, now)
    target = snapshot.get("session_target") or {}
    start, end = target.get("start_time"), target.get("end_time")
    if not start or not end:
        return None
    try:
        start_dt = datetime.fromisoformat(start).replace(tzinfo=None)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None
    if now < end_dt + timedelta(minutes=2):
        return None
    ref = _reference_price(snapshot)
    if ref is None:
        return {'reference': None, 'realized': None, 'rating': 'UNVERIFIABLE',
                'target_state': 'UNVERIFIABLE', 'detail': '; 缺少有效冻结基准价，无法验证'}
    forecast = snapshot.get("session_forecast") or {}
    forecast_start = batch.snapshot_time
    try:
        frozen_start = datetime.fromisoformat(str(forecast.get("start_time") or ""))
        forecast_start = frozen_start.replace(tzinfo=None)
    except (TypeError, ValueError):
        pass
    bars = session.query(MarketBarModel).filter(
        MarketBarModel.instrument_id == batch.instrument_id,
        MarketBarModel.period == "1m",
        MarketBarModel.bar_time >= max(start_dt, forecast_start),
        MarketBarModel.bar_time < end_dt,
    ).order_by(MarketBarModel.bar_time.asc()).all()
    if not bars:
        return {
            "reference": ref, "realized": None, "rating": "UNVERIFIABLE",
            "detail": "点击后无有效一分钟行情，不能使用点击前数据结算",
        }
    realized = (float(bars[-1].close_price)
                if bars[-1].bar_time == end_dt - timedelta(minutes=1) else None)
    high = max(float(bar.high_price) for bar in bars)
    low = min(float(bar.low_price) for bar in bars)
    detail = f"; 点击后最高 {high:.10g}, 最低 {low:.10g}"
    try:
        parsed = json.loads(run.parsed_response_json or "{}")
        chart = parsed.get("session_chart") or {}
        direction = chart.get("direction") or run.direction
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"reference": ref, "realized": realized, "detail": detail}

    state, touched_time = _session_line_state(bars, chart, direction, max(start_dt, forecast_start))
    if state == "UNVERIFIABLE":
        return {
            "reference": ref, "realized": realized, "rating": "UNVERIFIABLE",
            "target_state": state, "detail": detail + f"; {touched_time.isoformat()} 行情有缺口或同一分钟同时触及目标和失效，无法确认先后",
        }
    if state == "HIT":
        return {
            "reference": ref, "realized": realized, "rating": "CORRECT",
            "target_state": state, "detail": detail + f"; 目标先触及 {touched_time.isoformat()}",
        }
    if state == "INVALIDATED":
        return {
            "reference": ref, "realized": realized, "rating": "WRONG",
            "target_state": state, "detail": detail + f"; 失效价先触及 {touched_time.isoformat()}",
        }
    return {"reference": ref, "realized": realized,
            **({'rating': 'UNVERIFIABLE'} if realized is None else {}),
            "target_state": state if realized is not None else 'UNVERIFIABLE',
            "detail": detail + ("; 缺少小节收盘分钟行情" if realized is None else "; 目标和失效均未触及")}


def _settle_completed_window(session, run, batch, snapshot, chart, now):
    start, end = wall_time(chart['forecast_start_time']), wall_time(chart['forecast_end_time'])
    if start >= end:
        return {'reference': _reference_price(snapshot), 'realized': None, 'rating': 'UNVERIFIABLE',
                'target_state': 'UNVERIFIABLE', 'detail': '; 模型在小节结束后完成，没有可验证的盘中窗口'}
    if now < end + timedelta(minutes=2):
        return None
    ref = _reference_price(snapshot)
    if ref is None:
        return {'reference': None, 'realized': None, 'rating': 'UNVERIFIABLE',
                'target_state': 'UNVERIFIABLE', 'detail': '; 缺少有效冻结参考价，无法验证10分钟窗口'}
    bars = session.query(MarketBarModel).filter(
        MarketBarModel.instrument_id == batch.instrument_id, MarketBarModel.period == '1m',
        MarketBarModel.bar_time >= start, MarketBarModel.bar_time <= end + timedelta(minutes=1),
    ).order_by(MarketBarModel.bar_time.asc()).all()
    complete = [bar for bar in bars if bar.bar_time >= start and bar.bar_time + timedelta(minutes=1) <= end]
    state, touched = _session_line_state(complete, chart, chart.get('direction') or run.direction, start)
    first_full = start.replace(second=0, microsecond=0)
    if first_full < start:
        first_full += timedelta(minutes=1)
    last_full = end.replace(second=0, microsecond=0) - timedelta(minutes=1)
    if state not in ('HIT', 'INVALIDATED', 'UNVERIFIABLE') and (
            not complete or complete[0].bar_time != first_full or complete[-1].bar_time != last_full):
        if now < end + _MISSING_BAR_GRACE:
            return None
        state = 'UNVERIFIABLE'
    session_end = wall_time(chart['session_end_time'])
    if end == session_end:
        sample = next((bar for bar in complete if bar.bar_time == end-timedelta(minutes=1)), None)
        realized = float(sample.close_price) if sample else None
    else:
        sample = next((bar for bar in bars if bar.bar_time >= end), None)
        realized = float(sample.open_price) if sample else None
    if realized is not None and (not math.isfinite(realized) or realized <= 0):
        realized = None
    if realized is None and state not in ('HIT', 'INVALIDATED', 'UNVERIFIABLE') and now < end + _MISSING_BAR_GRACE:
        return None
    rating = {'HIT': 'CORRECT', 'INVALIDATED': 'WRONG', 'UNVERIFIABLE': 'UNVERIFIABLE',
              'WAITING_TRIGGER': 'UNVERIFIABLE'}.get(state)
    if ref is None or realized is None and rating is None:
        rating = 'UNVERIFIABLE'
    return {'reference': ref, 'realized': realized, **({'rating': rating} if rating else {}),
            'target_state': state,
            'detail': f'; 模型完成后10分钟窗口 {start.isoformat()} 至 {end.isoformat()}；'
                      f'完整分钟柱状态 {state}' + (f' {touched.isoformat()}' if touched else '') +
                      (f'；到期分钟采样 {sample.bar_time.isoformat()}，仅评估方向，不代表模拟成交盈亏' if sample else '；缺少到期分钟采样')}


def _session_line_state(bars, chart: dict, direction: str | None, start=None):
    """返回盘时段目标线状态和首个决定性一分钟时间。"""
    if direction not in ("LONG", "SHORT"):
        return "NO_DIRECTION", None
    try:
        target = float(chart["target_price"])
        invalidation = float(chart["invalidation_price"])
    except (KeyError, TypeError, ValueError):
        return "NO_DIRECTION", None
    if not all(math.isfinite(p) and p > 0 for p in (target, invalidation)):
        return 'NO_DIRECTION', None

    if chart.get('window_policy') == 'RESULT_AVAILABLE_10M':
        trigger = chart.get('trigger_price')
        try:
            trigger = float(trigger) if trigger is not None else None
            if trigger is not None and (not math.isfinite(trigger) or trigger <= 0):
                return 'NO_DIRECTION', None
        except (TypeError, ValueError):
            return 'NO_DIRECTION', None
        active = trigger is None
        expected = start.replace(second=0, microsecond=0) if start else None
        if expected and expected < start:
            expected += timedelta(minutes=1)
        for bar in bars:
            if expected and bar.bar_time != expected:
                return 'UNVERIFIABLE', bar.bar_time
            expected = bar.bar_time + timedelta(minutes=1)
            high, low = float(bar.high_price), float(bar.low_price)
            hit = high >= target if direction == 'LONG' else low <= target
            invalid = low <= invalidation if direction == 'LONG' else high >= invalidation
            if not active:
                triggered = high >= trigger if direction == 'LONG' else low <= trigger
                if not triggered:
                    if invalid:
                        return 'INVALIDATED', bar.bar_time
                    continue
                # OHLC 无法证明同一分钟内触发与退出价的先后。
                if hit or invalid:
                    return 'UNVERIFIABLE', bar.bar_time
                active = True
                continue
            if hit and invalid:
                return 'UNVERIFIABLE', bar.bar_time
            if hit or invalid:
                return ('HIT' if hit else 'INVALIDATED'), bar.bar_time
        return ('PENDING' if active else 'WAITING_TRIGGER'), None

    def touched(bar, kind: str) -> bool:
        if direction == "LONG":
            return (float(bar.high_price) >= target if kind == "target"
                    else float(bar.low_price) <= invalidation)
        if direction == "SHORT":
            return (float(bar.low_price) <= target if kind == "target"
                    else float(bar.high_price) >= invalidation)
        return False

    target_bar = next((bar for bar in bars if touched(bar, "target")), None)
    invalidation_bar = next((bar for bar in bars if touched(bar, "invalidation")), None)
    decisive = min((bar.bar_time for bar in (target_bar, invalidation_bar) if bar), default=None)
    if bars and start:
        expected = start.replace(second=0, microsecond=0)
        if expected < start:
            expected += timedelta(minutes=1)
        if bars[0].bar_time != expected:
            return 'UNVERIFIABLE', bars[0].bar_time
    for previous, current in zip(bars, bars[1:]):
        if decisive and current.bar_time > decisive:
            break
        if current.bar_time - previous.bar_time != timedelta(minutes=1):
            return 'UNVERIFIABLE', current.bar_time
    if target_bar and invalidation_bar and target_bar.bar_time == invalidation_bar.bar_time:
        return "UNVERIFIABLE", target_bar.bar_time
    if target_bar and (not invalidation_bar or target_bar.bar_time < invalidation_bar.bar_time):
        return "HIT", target_bar.bar_time
    if invalidation_bar and (not target_bar or invalidation_bar.bar_time < target_bar.bar_time):
        return "INVALIDATED", invalidation_bar.bar_time
    return "PENDING", None


def session_line_states(session, batch, runs, snapshot: dict):
    """按同一批冻结的预测起点和后续一分钟 K 线计算各模型状态。"""
    target = snapshot.get("session_target") or {}
    try:
        start = datetime.fromisoformat(target["start_time"]).replace(tzinfo=None)
        end = datetime.fromisoformat(target["end_time"]).replace(tzinfo=None)
        forecast = snapshot.get("session_forecast") or {}
        frozen_start = datetime.fromisoformat(
            forecast.get("start_time") or batch.snapshot_time.isoformat()).replace(tzinfo=None)
    except (KeyError, TypeError, ValueError):
        return {}
    if not any(run.status in ("SUCCEEDED", "UNSTRUCTURED") and run.parsed_response_json for run in runs):
        return {}
    bars = session.query(MarketBarModel).filter(
        MarketBarModel.instrument_id == batch.instrument_id,
        MarketBarModel.period == "1m",
        MarketBarModel.bar_time >= max(start, frozen_start),
        MarketBarModel.bar_time < end,
    ).order_by(MarketBarModel.bar_time.asc()).all()
    states = {}
    for run in runs:
        if run.status not in ("SUCCEEDED", "UNSTRUCTURED") or not run.parsed_response_json:
            continue
        try:
            parsed = json.loads(run.parsed_response_json) or {}
            chart = (parsed.get("session_chart") or {}) if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(chart, dict) or not chart:
            continue
        chart = chart_for_completion(chart, getattr(run, 'completed_at', None))
        run_start = max(start, frozen_start)
        run_bars = bars
        if chart.get('window_policy') == 'RESULT_AVAILABLE_10M':
            run_start, run_end = wall_time(chart['forecast_start_time']), wall_time(chart['forecast_end_time'])
            run_bars = [bar for bar in bars if bar.bar_time >= run_start and bar.bar_time + timedelta(minutes=1) <= run_end]
        state, touched_time = _session_line_state(run_bars, chart, chart.get("direction") or run.direction, run_start)
        states[run.run_id] = {"status": state, "decided_at": touched_time.isoformat() if touched_time else None}
    return states


def _settle_next_open(session, run, batch, snapshot, now: datetime):
    return _settle_frozen_price(session, batch, snapshot, now)


def _settle_future(session, batch, snapshot, parsed, now):
    """各期限独立到期，指标保存在原记录，整体评价等全部期限完成。"""
    metrics = parsed.setdefault('future_metrics', {})
    targets = snapshot.get('future_targets') or []
    forecasts = {f['horizon_days']:f for f in parsed.get('horizon_forecasts',[])}
    if [t.get('horizon_days') for t in targets] not in ([3,5], [3,5,7,15]):
        return {'reference':_reference_price(snapshot),'realized':None,
                'rating':'UNVERIFIABLE','detail':'; 缺少有效期限冻结目标'}
    for target in targets:
        key = str(target['horizon_days'])
        if key in metrics:
            continue
        outcome = _settle_frozen_price(session, batch, {'settlement_target':target}, now)
        if outcome is None:
            continue
        ref, realized = outcome['reference'], outcome['realized']
        if realized is None:
            # 终态: 冻结目标无效或宽限期后仍缺行情, 无法验证且不再重评
            metrics[key] = {'rating':'UNVERIFIABLE', 'trading_day':target.get('trading_day'),
                'reference_price':ref, 'realized_price':None, 'delta':None, 'threshold':None,
                'evaluated_at':now.isoformat(), 'max_rise_pct':None, 'max_fall_pct':None,
                'sample_count':0, 'sample_status':'NO_BARS', 'detail':outcome.get('detail','')}
            continue
        direction = forecasts.get(target['horizon_days'],{}).get('direction')
        threshold = _noise_threshold(ref, float(snapshot['instrument']['price_tick']))
        rating = _judge(direction, realized-ref, threshold)[0]
        start = datetime.fromisoformat(snapshot['market']['snapshot_time']).replace(tzinfo=None)
        # 不使用点击所在分钟的高低价，因为其中包含点击前的价格。
        if start.second or start.microsecond:
            start = start.replace(second=0,microsecond=0) + timedelta(minutes=1)
        end = datetime.fromisoformat(target['bar_time'])
        bars = session.query(MarketBarModel).filter(
            MarketBarModel.instrument_id == batch.instrument_id, MarketBarModel.period == '1m',
            MarketBarModel.bar_time >= start, MarketBarModel.bar_time <= end,
        ).order_by(MarketBarModel.bar_time.asc()).all()
        highs, lows = [], []
        for bar in bars:
            high, low = float(bar.high_price), float(bar.low_price)
            if math.isfinite(high) and math.isfinite(low) and 0 < low <= high:
                highs.append(high); lows.append(low)
        metrics[key] = {'rating':rating, 'trading_day':target['trading_day'],
            'reference_price':ref, 'realized_price':realized,
            'delta':realized-ref if realized is not None else None,
            'threshold':threshold, 'evaluated_at':now.isoformat(),
            'max_rise_pct':max(0,(max(highs)-ref)/ref*100) if highs else None,
            'max_fall_pct':min(0,(min(lows)-ref)/ref*100) if lows else None,
            'sample_count':len(highs), 'sample_status':'RECORDED_BARS',
            'detail':outcome.get('detail','')}
    keys = [str(t['horizon_days']) for t in targets]
    if not all(key in metrics for key in keys):
        return None
    ratings = [metrics[key]['rating'] for key in keys]
    # 任一期限缺少可验证结果时不生成整体分数。
    rating = ('UNVERIFIABLE' if 'UNVERIFIABLE' in ratings else
              ratings[0] if len(set(ratings)) == 1 else 'PARTIAL')
    return {'reference':metrics['5']['reference_price'], 'realized':metrics['5']['realized_price'],
            'rating':rating, 'detail':'; ' + '，'.join(f'第{key}日 {metrics[key]["rating"]}' for key in keys)}


def settle_due_runs(now: datetime | None = None) -> dict:
    """扫描所有成功且未评分的运行, 到期的按实际行情自动评分。返回统计。"""
    now = now or datetime.now()
    created = scanned = 0
    session = SessionLocal()
    try:
        evaluated = {row[0] for row in session.query(AIAnalysisEvaluationModel.run_id).all()}
        runs = session.query(AIAnalysisRunModel).filter(
            AIAnalysisRunModel.status.in_(["SUCCEEDED", "UNSTRUCTURED"])).all()
        for run in runs:
            if json.loads(run.parsed_response_json or '{}').get('settlement_metrics'):
                continue
            batch = session.query(AIAnalysisBatchModel).filter_by(batch_id=run.batch_id).first()
            if batch is None:
                continue
            try:
                snapshot = json.loads(batch.snapshot_json or "{}")
            except json.JSONDecodeError:
                continue
            if run.run_id in evaluated and snapshot.get('schema_version') != '1.1':
                continue
            target = snapshot.get("analysis_target", "TODAY")
            scanned += 1
            try:
                if target == "TODAY":
                    outcome = _settle_today(session, run, batch, snapshot, now)
                elif target == "CURRENT_SESSION":
                    outcome = _settle_session(session, run, batch, snapshot, now)
                elif target == "NEXT_OPEN":
                    outcome = _settle_next_open(session, run, batch, snapshot, now)
                elif target == 'FUTURE_TREND':
                    parsed = json.loads(run.parsed_response_json or '{}')
                    outcome = _settle_future(session, batch, snapshot, parsed, now)
                    run.parsed_response_json = json.dumps(parsed, ensure_ascii=False)
                else:
                    continue
            except Exception:
                logger.exception("自动结算计算失败: %s", run.run_id)
                continue
            if outcome is None:
                continue
            forced_rating = None
            detail = ""
            if isinstance(outcome, dict):
                ref = outcome["reference"]
                realized = outcome.get("realized")
                forced_rating = outcome.get("rating")
                detail = outcome.get("detail") or ""
            else:
                ref, realized = outcome
            try:
                tick = float(snapshot.get("instrument", {}).get("price_tick") or 1.0)
            except (TypeError, ValueError):
                tick = 1.0
            threshold = _noise_threshold(ref, tick) if ref is not None else 0
            if forced_rating:
                rating = forced_rating
                score = 50 if rating == 'PARTIAL' else AUTO_RATING_SCORES[rating]
                if realized is None:
                    comment = f"{AUTO_SETTLE_TAG} 参考价 {ref}{detail}"
                else:
                    delta = realized - ref
                    sign = "+" if delta >= 0 else ""
                    comment = (f"{AUTO_SETTLE_TAG} 参考价 {ref} → 结算价 {realized}"
                               f" (Δ{sign}{delta:.10g}){detail}"
                               "")
            elif realized is None:
                rating, score = "UNVERIFIABLE", None
                comment = f"{AUTO_SETTLE_TAG} 缺少有效目标行情，无法验证{detail}"
            else:
                direction = run.direction
                if direction is None:
                    try:
                        parsed = json.loads(run.parsed_response_json or "{}")
                        direction = (parsed.get("next_open_forecast") or parsed).get("direction")
                    except json.JSONDecodeError:
                        direction = None
                rating, score = _judge(direction, realized - ref, threshold)
                if rating is None:
                    continue
                delta = realized - ref
                sign = "+" if delta >= 0 else ""
                comment = (f"{AUTO_SETTLE_TAG} 参考价 {ref} → 结算价 {realized}"
                           f" (Δ{sign}{delta:.10g}, 阈值±{threshold:.10g})"
                           f"{detail}"
                           "")
            try:
                parsed = json.loads(run.parsed_response_json or '{}')
                direction = parsed.get('next_open_forecast', parsed).get('direction')
                direction_rating = (_judge(direction, realized - ref, threshold)[0]
                                    if realized is not None and ref is not None else 'UNVERIFIABLE')
                parsed['settlement_metrics'] = {
                    'direction_rating': direction_rating,
                    'target_state': outcome.get('target_state') if isinstance(outcome, dict) else None,
                    'evaluated_at': now.isoformat(),
                }
                run.parsed_response_json = json.dumps(parsed, ensure_ascii=False)
            except (ValueError, TypeError):
                pass
            if run.run_id in evaluated:
                continue  # 人工评分保留，自动指标独立保存。
            session.add(AIAnalysisEvaluationModel(
                evaluation_id=str(uuid.uuid4()), run_id=run.run_id,
                owner_username=batch.owner_username, analysis_target=target,
                rating=rating, score=score, comment=comment,
                evaluated_at=now, updated_at=now,
            ))
            created += 1
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("自动结算批量处理失败")
        from backend.ai_analysis import alerts
        alerts.notify_throttled('settle_due_runs', 'AI_SETTLEMENT_ERROR',
                                'AI 自动结算批量处理失败', 'ERROR', 'ai_settlement')
    finally:
        session.close()
    if created:
        logger.info("自动结算: 扫描 %s 个运行, 新增 %s 条自动评分", scanned, created)
    return {"scanned": scanned, "created": created}
