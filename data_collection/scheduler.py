import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .collectors.czce_receipt import CZCEReceipt
from .collectors.dce_receipt import DCEReceipt
from .collectors.market import Market
from .config import TradingCalendar
from .storage import Store
from .transport import HTTP, notify


def reason(exc):
    # Never expose response bodies, headers, credentials, or arbitrary exception messages.
    text = str(exc).split(' ', 1)[0]
    return text if isinstance(exc, ValueError) and text.replace('_', '').isalnum() and text.isupper() else type(exc).__name__


def run_dates(config, start, end, exchange=None, dry_run=False, force=False, wire=None, alert_failures=True):
    jobs = []
    for name, settings in config['exchanges'].items():
        if exchange and name != exchange: continue
        if not settings['enabled'] and not settings['market']['enabled']: continue
        calendar = TradingCalendar(yaml.safe_load(Path(settings['calendar']).read_text(encoding='utf-8')))
        for day in calendar.days(start, end):
            jobs.append((name, settings, calendar, day))
    if not jobs: return []
    readiness = {(r['exchange'], r['channel'], r.get('variety')): r for r in self_check(config)}
    owned = wire is None
    wire = wire or HTTP(config['network'])
    results = []
    try:
        with Store(':memory:' if dry_run else config['database']['path']) as store:
            if dry_run and Path(config['database']['path']).exists():
                with sqlite3.connect(Path(config['database']['path']).as_uri() + '?mode=ro', uri=True) as source:
                    source.backup(store.db)
            if not dry_run: store.mappings(config['exchanges'])
            collectors = {}
            for name, settings, calendar, day in jobs:
                if name not in collectors:
                    receipt = CZCEReceipt if name == 'CZCE' else DCEReceipt
                    collectors[name] = [cls(name, settings, config, store, wire) for cls, enabled in
                                        ((receipt, settings['enabled']), (Market, settings['market']['enabled'])) if enabled]
                for variety in settings['varieties']:
                    for collector in collectors[name]:
                        event = {'trade_date': day, 'exchange': name, 'variety': variety, 'channel': collector.channel}
                        if not force and not dry_run and store.complete(day, name, variety, collector.channel):
                            results.append(dict(event, status='SKIPPED_COMPLETE', row_count=0)); continue
                        def log(data):
                            if not dry_run:
                                store.log(day, name, variety, collector.channel, data['status'], attempt=data.get('attempt', 0), details=data)
                        try:
                            ready = readiness.get((name, collector.channel, variety))
                            if ready and ready['status'] == 'FAILED':
                                raise ValueError(ready['reason'])
                            rows = collector.parse(collector.fetch(day, variety, log), day, variety)
                            rows, validation = collector.validate(rows, day, variety, calendar)
                            status = 'SUCCESS' if validation['ok'] else 'REJECTED'
                            result = dict(event, status=status, row_count=len(rows), validation=validation)
                            if validation['ok'] and not dry_run: collector.upsert(rows)
                        except Exception as exc:
                            result = dict(event, status='FAILED', row_count=0, reason=reason(exc))
                            if isinstance(collector, DCEReceipt): collector.token = None
                        if not dry_run:
                            store.log(day, name, variety, collector.channel, result['status'], row_count=result['row_count'], details=result)
                            if result['status'] != 'SUCCESS' and alert_failures:
                                alert = notify(config['alerts'], result)
                                store.log(day, name, variety, collector.channel, 'ALERT_' + alert)
                        results.append(result)
    finally:
        if owned: wire.close()
    return results


def self_check(config):
    reports = []
    for name, settings in config['exchanges'].items():
        for cls, enabled in ((CZCEReceipt if name == 'CZCE' else DCEReceipt, settings['enabled']), (Market, settings['market']['enabled'])):
            if not enabled:
                reports.append({'exchange': name, 'channel': cls.channel, 'status': 'DISABLED'}); continue
            try:
                calendar = TradingCalendar(yaml.safe_load(Path(settings['calendar']).read_text(encoding='utf-8')))
                with Store(':memory:') as store:
                    collector = cls(name, settings, config, store, None)
                    fixture = settings['fixture'] if cls.channel == 'receipt' else settings['market']['fixture']
                    text = Path(fixture).read_text(encoding='utf-8')
                    check_date = settings['fixture_date']
                    if not calendar.days(check_date, check_date): raise ValueError('FIXTURE_CALENDAR_MISMATCH')
                    for variety in settings['varieties']:
                        try:
                            collector.parse(text, check_date, variety)
                            reports.append({'exchange': name, 'channel': cls.channel, 'variety': variety, 'status': 'PASS_OFFLINE'})
                        except Exception as exc:
                            reports.append({'exchange': name, 'channel': cls.channel, 'variety': variety, 'status': 'FAILED', 'reason': reason(exc)})
            except Exception as exc:
                reports.extend({'exchange': name, 'channel': cls.channel, 'variety': variety, 'status': 'FAILED', 'reason': reason(exc)} for variety in settings['varieties'])
    return reports


def poll(config, start, end, exchange=None, sleep=time.sleep):
    results = []
    wire = HTTP(config['network'])
    try:
        for attempt in range(config['schedule']['max_polls']):
            results = run_dates(config, start, end, exchange, wire=wire, alert_failures=False)
            if not any(r['status'] in ('FAILED', 'REJECTED') for r in results):
                break
            if attempt + 1 < config['schedule']['max_polls']:
                sleep(config['schedule']['poll_seconds'])
        failures = [r for r in results if r['status'] in ('FAILED', 'REJECTED')]
        if failures:
            with Store(config['database']['path']) as store:
                for result in failures:
                    store.log(result['trade_date'], result['exchange'], result['variety'], result['channel'], 'POLL_EXHAUSTED', details=result)
                    alert = notify(config['alerts'], result)
                    store.log(result['trade_date'], result['exchange'], result['variety'], result['channel'], 'ALERT_' + alert)
        return results
    finally:
        wire.close()


def serve(config, sleep=time.sleep, now=None):
    schedule = config['schedule']
    if schedule['timezone'] != 'Asia/Shanghai': raise ValueError('UNSUPPORTED_SCHEDULE_TIMEZONE')
    hour, minute = map(int, schedule['time'].split(':'))
    last_day = None
    clock = now or (lambda: datetime.now(timezone(timedelta(hours=8))))
    while True:
        current = clock()
        today = current.date().isoformat()
        if today != last_day and (current.hour, current.minute) >= (hour, minute):
            for name, settings in config['exchanges'].items():
                if not settings['enabled'] and not settings['market']['enabled']: continue
                try:
                    calendar = TradingCalendar(yaml.safe_load(Path(settings['calendar']).read_text(encoding='utf-8')))
                    start = max(calendar.start, (current.date() - timedelta(days=schedule['lookback_days'])).isoformat())
                    calendar.days(start, today)
                    # Holidays are excluded; unresolved earlier trading days are retried.
                    reports = poll(config, start, today, name)
                except Exception as exc:
                    event = dict(trade_date=today, exchange=name, variety='', channel='schedule', status='SCHEDULE_FAILED', reason=reason(exc))
                    with Store(config['database']['path']) as store:
                        store.log(today, name, '', 'schedule', 'SCHEDULE_FAILED', details=event)
                        store.log(today, name, '', 'schedule', 'ALERT_' + notify(config['alerts'], event))
                    reports = [event]
                print(json.dumps(reports, ensure_ascii=False), flush=True)
            last_day = today
        sleep(30)
