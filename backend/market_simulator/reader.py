"""Explicit Sina read adapter. Missing source evidence is never repaired here."""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from backend.common.trading_time import FuturesTradingCalendar as Calendar
from backend.models.market_data import decode_bar_provenance
from backend.common.bar_evidence import sina_history_windows, valid_volume_anchor
from backend.signal_engine.rules import number
from .models import SimulatorError, at

HISTORY_FIELDS = ('open_price', 'high_price', 'low_price', 'close_price', 'volume', 'open_interest')


def validate_history_values(bar):
    try:
        values = {key: number(bar, key) for key in HISTORY_FIELDS}
        o, h, l, c = (values[key] for key in HISTORY_FIELDS[:4])
        if any(value is None or value <= 0 for value in (o,h,l,c)) or not l <= min(o,c) <= max(o,c) <= h:
            raise ValueError('OHLC invalid')
        volume, oi = values['volume'], values['open_interest']
        if volume is None or volume < 0 or volume != int(volume):
            raise ValueError('volume invalid')
        if bar.get('open_interest') is not None and (oi is None or oi < 0):
            raise ValueError('OI invalid')
    except (ValueError, TypeError, ArithmeticError):
        raise SimulatorError('INVALID_HISTORY', '历史OHLC、成交量或持仓字段无效')


def source_object(raw, code, message):
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(value, dict):
            raise ValueError('expected object')
        return value
    except (ValueError, TypeError, UnicodeError):
        raise SimulatorError(code, message)


def quality_summary(observed, histories):
    """Summarize recorded provenance without promoting unknown coverage."""
    sequences = {}
    for name, bars in {'current': [observed], **histories}.items():
        records = [provenance(b) for b in bars]
        sequences[name] = dict(count=len(bars),
            providers=sorted({str(p.get('provider') or 'UNKNOWN') for p in records}),
            sources=sorted({str(p.get('source') or 'UNKNOWN') for p in records}),
            label_semantics=sorted({str(p.get('label_semantics') or 'UNKNOWN') for p in records}),
            coverage=sorted({str(p.get('coverage') or b.get('coverage') or 'UNKNOWN') for b, p in zip(bars, records)}))
    reasons = []
    def reason(code, message):
        reasons.append(dict(code=code, message=message))
    history, trend = histories['15m'], histories['60m']
    if len(history) < 20:
        reason('HISTORY_15M_INSUFFICIENT', '15分钟闭合历史不足20根，突破与均量不可评估')
    elif sum(number(b, 'volume') or Decimal(0) for b in history[-20:]) <= 0:
        reason('VOLUME_BASELINE_UNAVAILABLE', '前20根闭合柱均量非正，量比不可评估')
    if len(trend) < 60:
        reason('HISTORY_60M_INSUFFICIENT', '60分钟闭合历史不足60根，趋势不可评估')
    if number(observed, 'open_interest') is None:
        reason('CURRENT_OI_UNKNOWN', '当前持仓未知，当前评分不可评估')
    if not history or number(history[-1], 'open_interest') is None:
        reason('PREVIOUS_OI_UNKNOWN', '上一闭合柱持仓未知，覆盖情景不可校验')
    return dict(sequences=sequences, evaluation_reasons=reasons)


def session_for(now, instrument):
    local = at(now).replace(tzinfo=None)
    if not Calendar.is_trading_time_for(local, instrument):
        raise SimulatorError('MARKET_CLOSED', '当前合约未开盘，不能建立新基准')
    sessions = Calendar.analysis_session_options(local, instrument)
    active = next((s for s in sessions if s['status'] == 'ACTIVE'), None)
    if active is None:
        raise SimulatorError('MARKET_CLOSED', '找不到合法连续交易小节')
    return at(active['start_time']), at(active['end_time'])


def provenance(bar):
    return decode_bar_provenance(bar.get('provenance_json')) or {}


def normalize_bar(raw, instrument, period, storage=None):
    bar = deepcopy(source_object(raw, 'INVALID_HISTORY', '历史柱记录格式无效'))
    p = provenance(bar)
    if p.get('provider') != 'sina' or p.get('label_semantics') not in ('START', 'END'):
        try:
            stamp = at(bar['bar_time']).strftime('%Y-%m-%d %H:%M:%S')
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            stamp = '时间无效'
        missing = []
        if p.get('provider') != 'sina':
            missing.append('可核验的新浪来源')
        if p.get('label_semantics') not in ('START', 'END'):
            missing.append('START/END时间标签')
        location = f'{instrument} · {period} · {stamp}（北京时间）'
        if storage:
            location += f' · {storage}'
        raise SimulatorError('BAR_PROVENANCE_UNAVAILABLE',
            f'{location}：缺少{"、".join(missing)}，不能确定闭合范围；'
            '需要从可信来源重新采集并记录来源与时间语义，不能手工猜填标签或反推数据；本次未建立基准')
    try:
        label = at(bar['bar_time'])
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        raise SimulatorError('INVALID_HISTORY_TIME', '历史柱时间缺失或无效，无法核验闭合范围')
    duration = timedelta(minutes=int(period[:-1]))
    if p['label_semantics'] == 'START':
        start = label
        if not Calendar.is_trading_time_for(start.replace(tzinfo=None), instrument):
            raise SimulatorError('INVALID_BAR_SESSION', '柱开始时间不在合法交易小节')
        end = min(start+duration, at(Calendar.session_end(start.replace(tzinfo=None), instrument)))
    elif (p.get('source') == 'sina_history' and p.get('aggregation_clock') == 'TRADING_MINUTES'
          and p.get('response_sha256') and p.get('request_url') and p.get('source_time')):
        try:
            if at(p['source_time']) != label:
                raise ValueError('source label conflict')
            span = sina_history_windows(instrument, str(bar['trading_day']), period)[label.replace(tzinfo=None)]
            start, end = at(span['start_at']), at(span['end_at'])
            bar['segments'] = [(at(a), at(b)) for a,b in span['segments']]
        except (ValueError, KeyError, TypeError):
            raise SimulatorError('INVALID_BAR_SESSION', '新浪历史结束标签不在已核验的交易分钟网格')
    else:
        end = label
        # An END-labelled bar must have a determinable uninterrupted legal span.
        start = end-duration
        if not Calendar.is_trading_time_for(start.replace(tzinfo=None), instrument):
            raise SimulatorError('INVALID_BAR_SESSION', '结束标签无法确定合法开始范围')
        if end > at(Calendar.session_end(start.replace(tzinfo=None), instrument)):
            raise SimulatorError('INVALID_BAR_SESSION', '结束标签柱跨休市小节，无法确定真实连续范围')
    if Calendar.get_trading_day(start.replace(tzinfo=None)) != str(bar.get('trading_day')):
        raise SimulatorError('TRADING_DAY_CONFLICT', '历史柱交易日不一致')
    bar.update(source_bar_time=label, bar_time=start, start_at=start, end_at=end, provenance_json=p)
    return bar


def verify_observation(quote, state, closed_minutes, now, instrument):
    start_session, end_session = session_for(now, instrument)
    qtime = at(quote['timestamp'])
    start = qtime.replace(minute=qtime.minute//15*15, second=0, microsecond=0)
    start = max(start, start_session)
    end = min(start+timedelta(minutes=15), end_session)
    minute = deepcopy(state.get('minute') or {})
    base = deepcopy((state.get('higher') or {}).get('15m'))
    records = [deepcopy(b) for b in closed_minutes if start <= at(b['bar_time']) < qtime.replace(second=0, microsecond=0)]
    if minute:
        records.append(minute)
    evidence = {}

    def record(code, passed, reason):
        evidence[code] = dict(passed=passed, reason=reason, source_time=quote.get('timestamp'),
                              source_refs=[str(b.get('bar_time')) for b in records])

    try:
        identity = (start <= at(now) < end and qtime < end and start_session <= qtime < end_session
                    and str(quote.get('trading_day')) == Calendar.get_trading_day(qtime.replace(tzinfo=None))
                    and all(b.get('instrument') == instrument and str(b.get('trading_day')) == quote['trading_day'] for b in records))
        record('PERIOD_IDENTITY', bool(records) and identity, '合约、交易日、小节和周期必须一致')
        times = [at(b['bar_time']) for b in records]
        source_ok = all(provenance(b).get('provider') == 'sina' and provenance(b).get('label_semantics') == 'START' for b in records)
        record('FIRST_MINUTE', bool(records) and times[0] == start and source_ok,
               '必须有带可辨来源的周期首分钟观测')
        expected = []
        cursor = start
        while cursor <= qtime.replace(second=0, microsecond=0):
            expected.append(cursor); cursor += timedelta(minutes=1)
        record('MINUTE_SEQUENCE', times == expected and source_ok, '必须逐分钟齐备且不重复')
        anchor = state.get('volume_anchor')
        anchor_ok = valid_volume_anchor(anchor, start, quote)
        vols = [number(b, 'volume') for b in records]
        valid_vols = all(v is not None and v >= 0 and v == int(v) for v in vols)
        anchor_ok = anchor_ok and valid_vols and Decimal(str(quote['volume']))-Decimal(str(anchor['value'])) == sum(vols)
        record('VOLUME_ANCHOR', bool(anchor_ok), '必须有周期前一分钟内同交易日原始累计量观测及哈希；中途启动须等待新周期边界，不能反推')
        aggregate = dict(open_price=number(records[0], 'open_price'), high_price=max(number(b, 'high_price') for b in records),
                         low_price=min(number(b, 'low_price') for b in records), close_price=number(records[-1], 'close_price'),
                         volume=int(sum(vols)), open_interest=number(minute, 'open_interest'), turnover=None)
        shape_ok = all(all(number(b, k) is not None and number(b, k) > 0 for k in ('open_price', 'high_price', 'low_price', 'close_price'))
                       and number(b, 'high_price') >= max(number(b, 'open_price'), number(b, 'close_price'))
                       and number(b, 'low_price') <= min(number(b, 'open_price'), number(b, 'close_price')) for b in records)
        combined = deepcopy(minute)
        # During the new period's first minute, higher still contains the prior
        # period until a new minute closes. Exclude it only after proving closure.
        if base and at(base['bar_time']) < start:
            prior = normalize_bar(base, instrument, '15m')
            if prior['end_at'] <= start:
                base = None
        if base:
            shape_ok = shape_ok and at(base['bar_time']) == start and base['trading_day'] == quote['trading_day']
            combined.update(open_price=base['open_price'], high_price=max(number(base, 'high_price'), number(minute, 'high_price')),
                            low_price=min(number(base, 'low_price'), number(minute, 'low_price')), volume=int(base['volume'])+int(minute['volume']))
        match = shape_ok and valid_vols and all(number(combined, k) == Decimal(str(aggregate[k])) for k in ('open_price', 'high_price', 'low_price', 'close_price', 'volume'))
        record('OHLC_VOLUME_MATCH', bool(match), '分钟观测与检查点的OHLC和成交量须逐项相同')
        qo, so = number(quote, 'open_interest'), number(minute, 'open_interest')
        coherent = (at(state['time']) == qtime and number(minute, 'close_price') == number(quote, 'last_price')
                    and Decimal(str(state['volume'])) == Decimal(str(quote['volume']))
                    and all(v is None or v >= 0 for v in (qo, so))
                    and (qo is None or so is None or qo == so))
        record('SOURCE_COHERENCE', bool(coherent), '源时间、末价、累计量及已知持仓须一致，已知总持仓须非负')
    except (KeyError, TypeError, ValueError, ArithmeticError, IndexError, AttributeError):
        aggregate = None
    for code in ('PERIOD_IDENTITY', 'FIRST_MINUTE', 'MINUTE_SEQUENCE', 'VOLUME_ANCHOR', 'OHLC_VOLUME_MATCH', 'SOURCE_COHERENCE'):
        if code not in evidence:
            record(code, None, '必要观测字段缺失或无效')
    failed = [dict(code=k, **v) for k, v in evidence.items() if v['passed'] is not True]
    if failed:
        raise SimulatorError('CURRENT_BAR_UNVERIFIED', '当前15分钟观测证据不足，不能生成情景', reasons=failed)
    aggregate.update(instrument=instrument, period='15m', trading_day=quote['trading_day'], start_at=start, end_at=end,
                     provenance_json=provenance(minute), coverage=provenance(minute).get('coverage', 'UNKNOWN'), hypothetical=False)
    return aggregate, evidence


class MarketReader:
    def __init__(self, redis_client, session_factory):
        self.redis, self.session_factory = redis_client, session_factory
        self._source_history = {}

    def load_bars(self, instrument, period, limit, cutoff):
        if period in ('15m', '60m'):
            return self.load_source_history(instrument, period, limit, cutoff)
        return self.load_storage_bars(instrument, period, limit, cutoff)

    def load_source_history(self, instrument, period, limit, cutoff):
        """Only fresh Sina history, never merged with locally aggregated periods."""
        from backend.storage.history_backfill import fetch_sina_minute_bars
        key = (instrument, period)
        if key not in self._source_history:
            try:
                rows = fetch_sina_minute_bars(instrument, period)
                if not rows:
                    raise ValueError('empty source history')
                self._source_history[key] = rows
            except Exception:
                raise SimulatorError('HISTORY_FETCH_FAILED_'+period.upper(),
                    f'新浪{period}历史取数失败或为空；不回退到时间口径混杂的旧缓存', 503)
        closed = {}
        for raw in self._source_history[key]:
            if raw.get('instrument_id') != instrument or raw.get('period') != period:
                raise SimulatorError('INVALID_HISTORY', '新浪历史合约或周期不一致')
            b = normalize_bar(dict(raw, instrument=instrument), instrument, period, '新浪历史只读响应')
            if b['end_at'] > cutoff:
                continue
            validate_history_values(b)
            prior = closed.get(b['end_at'])
            if prior and any(number(prior, f) != number(b, f) for f in HISTORY_FIELDS):
                raise SimulatorError('HISTORY_CONFLICT', '新浪原始历史同一结束标签存在冲突')
            closed[b['end_at']] = b
        result = [closed[k] for k in sorted(closed)][-limit:]
        if not result:
            raise SimulatorError('HISTORY_UNAVAILABLE', f'新浪{period}历史没有截止前可用闭合柱')
        # Check the provider's own trading-minute grid, including closed tail slots.
        day = result[0]['start_at'].date()
        expected = set()
        last_day = datetime.strptime(Calendar.get_trading_day(cutoff.replace(tzinfo=None)), '%Y%m%d').date()
        while day <= max(cutoff.date(), last_day):
            if Calendar.is_trading_day(day):
                for end in sina_history_windows(instrument, day.strftime('%Y%m%d'), period):
                    end = at(end)
                    if result[0]['end_at'] <= end <= cutoff:
                        expected.add(end)
            day += timedelta(days=1)
        if expected != {b['end_at'] for b in result}:
            raise SimulatorError('HISTORY_GAP', f'新浪{period}原始历史存在真实缺柱或闭合尾柱缺失')
        return result

    def load_storage_bars(self, instrument, period, limit, cutoff):
        from backend.models.database import MarketBarModel
        session = self.session_factory()
        try:
            rows = session.query(MarketBarModel).filter_by(instrument_id=instrument, period=period).order_by(MarketBarModel.bar_time.desc()).limit(limit).all()
            data = [dict(instrument=instrument, period=period, trading_day=r.trading_day, bar_time=r.bar_time,
                         **{k: getattr(r, k) for k in ('open_price', 'high_price', 'low_price', 'close_price', 'volume', 'open_interest', 'provenance_json')}) for r in reversed(rows)]
        finally:
            session.close()
        cached = [source_object(v, 'INVALID_HISTORY', '历史柱记录格式无效') for v in self.redis.lrange(f'bars:{instrument}:{period}', -limit, -1)]
        merged = {}
        for raw, storage in [(b, 'MySQL') for b in data] + [(b, 'Redis') for b in cached]:
            # Only current-period minute observations are used to prove this bar.
            if period == '1m':
                try:
                    lower = cutoff.replace(minute=cutoff.minute//15*15, second=0, microsecond=0)
                    if at(raw['bar_time']) < lower:
                        continue
                except (KeyError, ValueError, TypeError, AttributeError):
                    raise SimulatorError('INVALID_HISTORY_TIME', '当前分钟观测时间无效')
            b = normalize_bar(raw, instrument, period, storage)
            if b['end_at'] <= cutoff:
                for field in ('open_price', 'high_price', 'low_price', 'close_price'):
                    try:
                        value = number(b, field)
                    except (ValueError, TypeError, ArithmeticError):
                        value = None
                    if value is None or value <= 0:
                        raise SimulatorError('INVALID_HISTORY', '历史价位无效')
                if number(b, 'high_price') < max(number(b, 'open_price'), number(b, 'close_price')) or number(b, 'low_price') > min(number(b, 'open_price'), number(b, 'close_price')):
                    raise SimulatorError('INVALID_HISTORY', '历史OHLC矛盾')
                try:
                    volume = number(b, 'volume')
                except (ValueError, TypeError, ArithmeticError):
                    volume = None
                if volume is None or volume < 0 or volume != int(volume):
                    raise SimulatorError('INVALID_HISTORY', '历史成交量未知或负值')
                try:
                    oi = number(b, 'open_interest')
                    if b.get('open_interest') is not None and (oi is None or oi < 0):
                        raise ValueError('invalid OI')
                except (ValueError, TypeError, ArithmeticError):
                    raise SimulatorError('INVALID_HISTORY', '历史持仓字段无效')
                key = b['start_at']
                prior = merged.get(key)
                if prior and any(number(prior, f) != number(b, f) for f in ('open_price', 'high_price', 'low_price', 'close_price', 'volume', 'open_interest')):
                    raise SimulatorError('HISTORY_CONFLICT', '历史同柱记录冲突，不能确定有效版本')
                merged[key] = b
        result = [merged[k] for k in sorted(merged)][-limit:]
        # Source gaps inside a continuous session remain visible; breaks are not filled.
        for a, b in zip(result, result[1:]):
            cursor = a['end_at']
            while cursor < b['start_at']:
                if Calendar.is_trading_time_for(cursor.replace(tzinfo=None), instrument):
                    raise SimulatorError('HISTORY_GAP', f'{period}历史存在真实交易时段缺口')
                cursor += timedelta(minutes=1)
        if result:
            # Detect missing *closed* tail periods, while leaving the current forming
            # period out. Non-trading breaks must not be mistaken for missing bars.
            cursor = max(result[-1]['end_at'], cutoff-timedelta(days=7))
            duration = int(period[:-1])
            while cursor < cutoff:
                if Calendar.is_trading_time_for(cursor.replace(tzinfo=None), instrument):
                    slot = cursor.replace(minute=cursor.minute//duration*duration, second=0, microsecond=0)
                    expected_end = min(slot+timedelta(minutes=duration), at(Calendar.session_end(cursor.replace(tzinfo=None), instrument)))
                    if expected_end <= cutoff:
                        raise SimulatorError('HISTORY_GAP', f'{period}历史尾部缺少截止前已闭合柱')
                cursor += timedelta(minutes=1)
        return result

    def metadata(self, instrument):
        from backend.common.product_specs import PRODUCT_SPECS, extract_product
        from backend.models.database import InstrumentModel
        product = extract_product(instrument)
        month = int(instrument[-2:])
        if month not in range(1, 13) or product not in PRODUCT_SPECS:
            raise SimulatorError('INVALID_CONTRACT', '请选择具有可核验规格的具体月份合约')
        session = self.session_factory()
        try:
            row = session.query(InstrumentModel).filter_by(symbol=instrument).first()
            if row is None:
                raise SimulatorError('INVALID_CONTRACT', '未找到该月份合约元数据')
            try:
                tick = Decimal(str(row.price_tick)) if row.price_tick else None
            except (ValueError, TypeError, ArithmeticError):
                tick = None
            if tick is None or not tick.is_finite() or tick <= 0:
                raise SimulatorError('INVALID_PRICE_TICK', '合约最小变动价位未核实')
            return dict(tick=tick, multiplier=row.volume_multiple, upper_limit=None, lower_limit=None)
        finally:
            session.close()

    def reference(self, run_id, owner, instrument, now):
        if not run_id:
            return None, 'NO_REFERENCE_PLAN'
        from backend.models.database import AIAnalysisRunModel, AIAnalysisBatchModel
        from backend.ai_analysis.session_window import chart_for_completion
        session = self.session_factory()
        try:
            run = session.query(AIAnalysisRunModel).filter_by(run_id=run_id).first()
            batch = session.query(AIAnalysisBatchModel).filter_by(batch_id=run.batch_id, owner_username=owner).first() if run else None
            if not run or not batch:
                raise SimulatorError('REFERENCE_NOT_FOUND', '参考结果不存在', 404)
            parsed = json.loads(run.parsed_response_json or '{}')
            if (batch.instrument_id != instrument or run.status != 'SUCCEEDED' or not run.completed_at
                    or parsed.get('publication_status') != 'REVIEWED' or not run.call_count):
                return None, 'REFERENCE_UNAVAILABLE'
            if json.loads(batch.snapshot_json or '{}').get('analysis_target') != 'CURRENT_SESSION':
                return None, 'REFERENCE_UNAVAILABLE'
            plan = chart_for_completion(parsed.get('session_chart') or {}, run.completed_at)
            completed = run.completed_at.replace(tzinfo=timezone.utc).astimezone(now.tzinfo)
            start, end = at(plan['forecast_start_time']), at(plan['forecast_end_time'])
            direction = parsed.get('direction', run.direction)
            target, stop = Decimal(str(plan['target_price'])), Decimal(str(plan['invalidation_price']))
            if not (completed <= now and start <= now < end and direction in ('LONG', 'SHORT') and target.is_finite() and stop.is_finite()
                    and target > 0 and stop > 0 and (target > stop if direction == 'LONG' else target < stop)):
                return None, 'REFERENCE_UNAVAILABLE'
            return dict(run_id=run_id, instrument=instrument, source='CURRENT_SESSION', source_timeframe='RESULT_AVAILABLE_10M',
                        completed_at=completed, valid_from=start, valid_until=end, direction=direction,
                        target=target, stop=stop, explicit_selection=True), None
        except (ValueError, KeyError, TypeError, ArithmeticError):
            return None, 'REFERENCE_UNAVAILABLE'
        finally:
            session.close()

    def freeze(self, instrument, owner, reference_run_id, now, refresh_clock=None):
        self._source_history.clear()
        for _ in range(3):
            if refresh_clock:
                now = refresh_clock()
            session_start, session_end = session_for(now, instrument)
            q1 = self.redis.hgetall(f'quote:sina:{instrument}')
            s1 = self.redis.hget('bar-engine:forming:sina', instrument)
            if not q1 or not s1:
                raise SimulatorError('CURRENT_BAR_MISSING', '缺少当前新浪报价或形成柱检查点')
            try:
                quote_time = at(q1['timestamp'])
            except (KeyError, ValueError, TypeError, AttributeError):
                raise SimulatorError('INVALID_QUOTE_TIME', '新浪源报价时间缺失或无效')
            age = (now-quote_time).total_seconds()
            if age < 0 or age > 25:
                raise SimulatorError('STALE_QUOTE', '新浪源报价超过25秒或时间在未来')
            meta = self.metadata(instrument)
            start = max(session_start, quote_time.replace(minute=quote_time.minute//15*15, second=0, microsecond=0))
            history = self.load_bars(instrument, '15m', 256, start)
            history60 = self.load_bars(instrument, '60m', 512, now)
            minutes = self.load_bars(instrument, '1m', 240, quote_time.replace(second=0, microsecond=0))
            plan, reference_reason = self.reference(reference_run_id, owner, instrument, now)
            s2 = self.redis.hget('bar-engine:forming:sina', instrument)
            q2 = self.redis.hgetall(f'quote:sina:{instrument}')
            if q1 != q2 or s1 != s2:
                continue
            if refresh_clock:
                now = refresh_clock()
                if not 0 <= (now-quote_time).total_seconds() <= 25:
                    continue
            state = source_object(s1, 'CURRENT_BAR_UNVERIFIED', '当前检查点记录格式无效，不能生成情景')
            try:
                observed, evidence = verify_observation(q1, state, minutes, now, instrument)
            except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
                raise SimulatorError('CURRENT_BAR_UNVERIFIED', '当前观测字段缺失或格式无效，不能生成情景')
            warnings = ['新浪轮询为已记录观测，非交易所完整成交覆盖',
                        '累计量起点为周期前最后一笔源观测，非交易所精确边界累计量',
                        '15m/60m历史只读新浪交易分钟结束标签序列，不混合本机高阶柱', '涨跌停未核实']
            if observed['open_interest'] is None:
                warnings.append('当前持仓未知')
            if number(q1, 'open_interest') is None:
                warnings.append('源报价持仓未知，未核对双方持仓一致性')
            return dict(instrument=instrument, feed='sina', timeframe='15m', trading_day=q1['trading_day'],
                        frozen_at=now, quote_at=quote_time, checkpoint_at=at(state['time']),
                        session_start=session_start, session_end=session_end,
                        period_start=observed['start_at'], period_end=observed['end_at'],
                        schema_version=1, rules_version='15m-v1', history_15m=history, history_60m=history60,
                        observed_bar=observed, observation_evidence=evidence, reference_plan=plan,
                        selected_reference_run_id=reference_run_id,
                        reference_reason=reference_reason,
                        quality=dict(warnings=warnings, blocking_reasons=[], **quality_summary(observed, {'15m': history, '60m': history60})), **meta)
        raise SimulatorError('SNAPSHOT_RACE', '行情正在更新，请重试', 409)
