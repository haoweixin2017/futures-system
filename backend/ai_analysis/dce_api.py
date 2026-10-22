"""Authenticated DCE portal API; fixed official origin, never a general HTTP reader."""
import asyncio
import json
import os
import time
from urllib.parse import urlsplit

import httpx

from backend.ai_analysis import provider
from backend.common.config import ROOT_DIR, read_dotenv


BASE = 'http://www.dce.com.cn/dceapi'
LATEST_PATH = '/forward/publicweb/maxTradeDate'
WAREHOUSE_PATH = '/forward/publicweb/dailystat/wbillWeeklyQuotes'
_token_cache = None


def credentials():
    local = read_dotenv(ROOT_DIR / '.env')
    return tuple(os.environ.get(k, local.get(k, '')).strip() for k in ('DCE_API_KEY', 'DCE_API_SECRET'))


def warehouse_url(product, period):
    # Match the repository's canonical URL order so persisted evidence validates too.
    return BASE + WAREHOUSE_PATH + '?tradeDate=' + period.replace('-', '') + '&varietyId=' + product.lower()


def _wire(method, path, headers, body, budget):
    if (method, path) not in (('POST', '/cms/auth/accessToken'), ('GET', LATEST_PATH), ('POST', WAREHOUSE_PATH)):
        raise ValueError('DCE_API_ENDPOINT_REJECTED')
    budget.require_time()
    timeout = min(8, budget.time_left())

    async def request():
        parsed = urlsplit(BASE + path)
        addresses = await provider._resolve_addresses_async(parsed.hostname, 80)
        provider._enforce_address_policy(addresses, False)
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=timeout) as client:
            async with client.stream(method, provider._pinned_url(parsed, addresses[0]),
                                     headers=dict(headers, Host=parsed.hostname), json=body) as response:
                if response.status_code != 200:
                    raise ValueError('DCE_API_HTTP_' + str(response.status_code))
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 1024 * 1024:
                        raise ValueError('DCE_API_RESPONSE_TOO_LARGE')
                    chunks.append(chunk)
                return json.loads(b''.join(chunks))

    async def bounded():
        return await asyncio.wait_for(request(), timeout)
    try:
        return asyncio.run(bounded())
    except Exception as exc:
        # Never echo response text, request headers or secret-bearing exceptions.
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith('DCE_API_') else type(exc).__name__
        raise ValueError('DCE_API_REQUEST_FAILED ' + path + ' ' + reason) from None


def _data(response, stage):
    global _token_cache
    if not isinstance(response, dict) or response.get('success') is not True or response.get('code') != 200:
        code = response.get('code') if isinstance(response, dict) else None
        if code in (401, 402):
            _token_cache = None
        # Do not include the server's msg; it can echo credentials.
        safe_code = str(code) if isinstance(code, int) else 'INVALID'
        raise ValueError('DCE_API_' + stage + '_BUSINESS_' + safe_code)
    data = response.get('data')
    if not isinstance(data, dict):
        raise ValueError('DCE_API_' + stage + '_INVALID_DATA')
    return data


def warehouse(product, budget):
    """Caller reserves one latest-index check and one body read; auth uses another check."""
    global _token_cache
    if product not in ('JD', 'LH'):
        raise ValueError('DCE_API_PRODUCT_NOT_REGISTERED')
    key, secret = credentials()
    if not key or not secret:
        raise ValueError('DCE_API_CREDENTIALS_MISSING')
    budget.require_time()
    if not _token_cache or _token_cache[:2] != (key, secret) or _token_cache[3] <= time.monotonic():
        if not budget.try_consume('index_checks'):
            raise ValueError('DCE_API_AUTH_INDEX_BUDGET_EXHAUSTED')
        data = _data(_wire('POST', '/cms/auth/accessToken', {'apikey': key}, {'secret': secret}, budget), 'AUTH')
        token, expires = data.get('token'), data.get('expiresIn')
        if not isinstance(token, str) or not token or not isinstance(expires, (int, float)) or expires <= 30:
            raise ValueError('DCE_API_AUTH_INVALID_TOKEN')
        _token_cache = (key, secret, token, time.monotonic() + expires - 30)
    headers = {'apikey': key, 'Authorization': 'Bearer ' + _token_cache[2]}
    latest = _data(_wire('GET', LATEST_PATH, headers, None, budget), 'LATEST')
    date = latest.get('tradeDate')
    if not isinstance(date, str) or len(date) != 8 or not date.isdigit():
        raise ValueError('DCE_API_LATEST_INVALID_DATE')
    payload = _wire('POST', WAREHOUSE_PATH, headers, {'varietyId': product.lower(), 'tradeDate': date}, budget)
    _data(payload, 'WAREHOUSE')
    return date, payload
