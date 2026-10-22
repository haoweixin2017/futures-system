import os
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def contract_identity(contract, exchange, trade_date):
    match = re.fullmatch(r'([A-Za-z]+)(\d+)', contract)
    if not match:
        raise ValueError('INVALID_CONTRACT')
    product, digits = match.groups()
    width = 3 if exchange == 'CZCE' else 4
    if exchange not in ('CZCE', 'DCE') or len(digits) != width or not 1 <= int(digits[-2:]) <= 12:
        raise ValueError('INVALID_CONTRACT')
    context = date.fromisoformat(trade_date).year
    modulus = 10 if exchange == 'CZCE' else 100
    suffix = int(digits[:-2])
    years = [y for y in range(context - modulus // 2, context + modulus // 2 + 1) if y % modulus == suffix]
    if len(years) != 1:
        raise ValueError('AMBIGUOUS_CONTRACT_YEAR')
    return product.upper(), years[0], int(digits[-2:])


class TradingCalendar:
    def __init__(self, data):
        self.data = data
        self.start, self.end = data['start'], data['end']
        self.dates = sorted(set(data['dates']))
        if not self.dates or self.start > self.end:
            raise ValueError('INVALID_CALENDAR')
        for value in [self.start, self.end] + self.dates:
            date.fromisoformat(value)
        if any(value < self.start or value > self.end for value in self.dates):
            raise ValueError('CALENDAR_OUT_OF_RANGE')

    def days(self, start, end):
        date.fromisoformat(start); date.fromisoformat(end)
        if start < self.start or end > self.end or start > end:
            raise ValueError('CALENDAR_COVERAGE_MISSING')
        return [d for d in self.dates if start <= d <= end]

    def previous(self, current):
        return next((d for d in reversed(self.dates) if d < current), None)


def load_config(path):
    path = Path(path).resolve()
    cfg = yaml.safe_load(path.read_text(encoding='utf-8'))
    try:
        schedule = cfg['schedule']
        if (schedule['timezone'] != 'Asia/Shanghai' or
                not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', str(schedule['time'])) or
                not isinstance(schedule['max_polls'], int) or schedule['max_polls'] < 1 or
                schedule['poll_seconds'] < 8 or schedule['lookback_days'] < 1):
            raise ValueError('INVALID_SCHEDULE')
        net = cfg['network']
        if not net['rate_state']:
            raise ValueError('RATE_STATE_CONFIG_MISSING')
        net['rate_state'] = str((path.parent / net['rate_state']).resolve())
        if net['interval_seconds'] < 8 or not 1 <= net['max_per_minute'] <= 7:
            raise ValueError('network interval/rate must enforce >=8 seconds')
        if net['attempts'] < 1 or net['backoff_seconds'] < 8 or net['timeout_seconds'] <= 0:
            raise ValueError('INVALID_RETRY_CONFIG')
        if not net['user_agent'] or net['obey_robots'] is not True or net['max_response_bytes'] <= 0:
            raise ValueError('INVALID_NETWORK_POLICY')
        url = cfg['database']['url']
        if not url.startswith('sqlite:///'):
            raise ValueError('ONLY_SQLITE_SUPPORTED')
        cfg['database']['path'] = str((path.parent / url[10:]).resolve())
        for exchange, item in cfg['exchanges'].items():
            if exchange not in ('CZCE', 'DCE') or item['mode'] not in ('html', 'api', 'fixture'):
                raise ValueError('INVALID_EXCHANGE_MODE')
            for key in ('calendar', 'fixture'):
                item[key] = str((path.parent / item[key]).resolve())
            item['market']['fixture'] = str((path.parent / item['market']['fixture']).resolve())
            for channel in (item, item['market']):
                if channel['enabled'] and item['mode'] != 'fixture':
                    if not channel.get('base_url') or not channel['request'].get('path'):
                        raise ValueError('SOURCE_CONFIG_MISSING ' + exchange)
                    parsed = urlsplit(channel['base_url'])
                    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
                        raise ValueError('INVALID_SOURCE_URL')
                    for request in [channel['request']] + ([channel['auth']['request']] if channel.get('auth') else []):
                        if request['method'] not in ('GET', 'POST') or not request.get('path'):
                            raise ValueError('INVALID_REQUEST_CONFIG')
                    for text in (str(channel['request']), str(channel.get('auth', {}))):
                        for env in re.findall(r'\{env:([A-Za-z_][A-Za-z0-9_]*)\}', text):
                            if not os.environ.get(env):
                                raise ValueError('CREDENTIAL_ENV_MISSING ' + env)
            if item['enabled'] or item['market']['enabled']:
                calendar = TradingCalendar(yaml.safe_load(Path(item['calendar']).read_text(encoding='utf-8')))
                if item['mode'] != 'fixture' and (calendar.data.get('fixture_only') or not calendar.data.get('source')):
                    raise ValueError('LIVE_CALENDAR_NOT_VERIFIED')
                for product in item['varieties']:
                    if product not in item['mapping']:
                        raise ValueError('VARIETY_MAPPING_MISSING ' + product)
        ref = cfg['validation'].get('reference_file')
        if ref:
            cfg['validation']['reference_file'] = str((path.parent / ref).resolve())
        cfg['_path'] = str(path)
        return cfg
    except (KeyError, TypeError) as exc:
        raise ValueError('CONFIG_FIELD_MISSING ' + str(exc)) from None
