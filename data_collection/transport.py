import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx


def render(value, context):
    if isinstance(value, dict): return {k: render(v, context) for k,v in value.items()}
    if isinstance(value, list): return [render(v, context) for v in value]
    if not isinstance(value, str): return value
    def substitute(match):
        key = match.group(1)
        if key.startswith('env:'):
            result = os.environ.get(key[4:])
        else:
            result = context.get(key)
        if result is None:
            raise ValueError('REQUEST_TEMPLATE_VALUE_MISSING')
        return str(result)
    return re.sub(r'\{([^{}]+)\}', substitute, value)


class HTTP:
    def __init__(self, settings, client=None, now=time.time, sleep=time.sleep):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings['timeout_seconds'], follow_redirects=False, trust_env=False)
        self.owned = client is None
        self.now, self.sleep = now, sleep
        self.last, self.robots = {}, {}
        self.robot_intervals = {}

    def close(self):
        if self.owned: self.client.close()

    def _wait(self, origin):
        interval = max(8, self.settings['interval_seconds'], 60/self.settings['max_per_minute'], self.robot_intervals.get(origin, 0))
        state = self.settings.get('rate_state')
        if state:
            Path(state).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(state, timeout=120) as db:
                db.execute('CREATE TABLE IF NOT EXISTS request_rate(host TEXT PRIMARY KEY,last_start REAL,interval REAL)')
                db.execute('BEGIN IMMEDIATE')
                previous = db.execute('SELECT last_start,interval FROM request_rate WHERE host=?', (origin,)).fetchone()
                if previous:
                    interval = max(interval, previous[1])
                    self.sleep(max(0, previous[0] + interval - self.now()))
                started = self.now()
                db.execute('INSERT INTO request_rate VALUES(?,?,?) ON CONFLICT(host) DO UPDATE SET last_start=excluded.last_start,interval=excluded.interval', (origin, started, interval))
                self.last[origin] = started
            return
        if origin in self.last:
            self.sleep(max(0, self.last[origin] + interval - self.now()))
        self.last[origin] = self.now()

    def _send(self, method, url, headers, body, log, robots=False, encoding='utf-8'):
        origin = urlsplit(url).netloc
        for attempt in range(1, self.settings['attempts'] + 1):
            self._wait(origin)
            retry_after = 0
            try:
                with self.client.stream(method, url, headers=headers, json=body,
                                        timeout=self.settings['timeout_seconds'], follow_redirects=False) as response:
                    if robots and response.status_code == 404:
                        return ''
                    if response.status_code == 429 or response.status_code >= 500:
                        value = response.headers.get('Retry-After', '')
                        try: retry_after = max(0, float(value))
                        except ValueError:
                            try: retry_after = max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
                            except (ValueError, TypeError): pass
                        reason = 'HTTP_' + str(response.status_code)
                    elif response.status_code != 200:
                        raise ValueError('HTTP_' + str(response.status_code))
                    else:
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > self.settings['max_response_bytes']:
                                raise ValueError('RESPONSE_TOO_LARGE')
                            chunks.append(chunk)
                        content = b''.join(chunks)
                        # Decoding is configured per source, including legacy GB18030 HTML.
                        return content.decode(encoding)
            except httpx.TransportError as exc:
                reason = type(exc).__name__
            log({'status': 'RETRY' if attempt < self.settings['attempts'] else 'REQUEST_FAILED', 'attempt': attempt, 'reason': reason})
            if attempt == self.settings['attempts']:
                raise ValueError('REQUEST_EXHAUSTED ' + reason) from None
            self.sleep(max(retry_after, self.settings['backoff_seconds'] * 2**(attempt-1)))

    def request(self, source, context, log):
        request = render(source['request'], context)
        base = source['base_url'].rstrip('/')
        parsed = urlsplit(base)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('INVALID_SOURCE_URL')
        if parsed.scheme == 'http' and not source.get('allow_http', self.settings.get('allow_http', False)):
            raise ValueError('HTTP_NOT_ALLOWED')
        url = base + '/' + request['path'].lstrip('/')
        if urlsplit(url).netloc != parsed.netloc:
            raise ValueError('SOURCE_ORIGIN_MISMATCH')
        origin = parsed.scheme + '://' + parsed.netloc
        agent = self.settings['user_agent']
        if origin not in self.robots:
            text = self._send('GET', urljoin(origin, '/robots.txt'), {'User-Agent': agent}, None, log, robots=True)
            rules = RobotFileParser()
            rules.parse(text.splitlines())
            self.robots[origin] = rules
        rules = self.robots[origin]
        if not rules.can_fetch(agent, url):
            raise ValueError('ROBOTS_DENIED')
        delay = rules.crawl_delay(agent)
        rate = rules.request_rate(agent)
        required = max(delay or 0, rate.seconds/rate.requests if rate else 0)
        self.robot_intervals[parsed.netloc] = required
        headers = dict(request.get('headers') or {}, **{'User-Agent': agent})
        return self._send(request['method'], url, headers, request.get('body'), log, encoding=source.get('encoding', 'utf-8'))


def notify(settings, event):
    endpoint = os.environ.get(settings['webhook_env'])
    if not endpoint:
        return 'NOT_CONFIGURED'
    try:
        with httpx.Client(timeout=settings['timeout_seconds'], follow_redirects=False, trust_env=False) as client:
            response = client.post(endpoint, json=event)
            response.raise_for_status()
        return 'SENT'
    except (httpx.HTTPError, ValueError):
        return 'FAILED'
