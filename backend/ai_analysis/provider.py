import ipaddress
import asyncio
import json
import math
from datetime import datetime
from email.utils import parsedate_to_datetime
import re
import socket
import time
import threading
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import httpx


# ipaddress.is_private 未收录、但应按"本机或局域网"对待的网段。
# 其中 100.64.0.0/10 (CGNAT) 是云厂商元数据服务所在段(如阿里云 100.100.200.200),
# 不拦截的话"外网模型地址"可以直连云元数据接口。
EXTENDED_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "100.64.0.0/10",
    "192.0.0.0/24",
    "fec0::/10",
))


def address_forbidden(ip: IPv4Address | IPv6Address) -> bool:
    """任何模式下都禁止模型请求触达的地址。"""
    return (ip.is_link_local or ip.is_multicast or ip.is_unspecified
            or ip.is_reserved)


def address_is_private(ip: IPv4Address | IPv6Address) -> bool:
    """本机或局域网地址; 仅 is_local_service 模式允许。"""
    return (ip.is_private or ip.is_loopback
            or any(ip in network for network in EXTENDED_PRIVATE_NETWORKS))


def resolve_host_addresses(hostname: str, port: Optional[int]) -> List[IPv4Address | IPv6Address]:
    """解析域名并去重; 解析结果在钉扎请求时直接使用, 不再二次解析。"""
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ProviderHTTPError("模型地址域名无法解析") from exc
    addresses: List[IPv4Address | IPv6Address] = []
    seen = set()
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if str(ip) not in seen:
            seen.add(str(ip))
            addresses.append(ip)
    if not addresses:
        raise ProviderHTTPError("模型地址域名无法解析")
    return addresses


def _enforce_address_policy(addresses: List[IPv4Address | IPv6Address],
                            allow_private: bool) -> None:
    if any(address_forbidden(ip) for ip in addresses):
        raise ProviderHTTPError("模型地址解析到禁止访问的网络地址")
    if allow_private:
        if not all(address_is_private(ip) for ip in addresses):
            raise ProviderHTTPError("勾选本地服务后，API 地址必须解析到本机或局域网")
    elif any(address_is_private(ip) for ip in addresses):
        raise ProviderHTTPError("外网模型地址不能解析到本机或局域网")


@dataclass
class ProviderResult:
    status_code: int
    raw_text: str
    content: str
    request_id: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    latency_ms: int
    tool_calls: Optional[List[Dict[str, Any]]] = None


class EvidenceValidationError(ValueError):
    """数值证据无法与冻结行情核对，可请求模型纠正但不可直接放行。"""


class ProviderHTTPError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None,
                 raw_text: Optional[str] = None, latency_ms: Optional[int] = None,
                 error_kind: str = 'UNKNOWN', retry_after: float | None = None,
                 request_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.raw_text = raw_text
        self.latency_ms = latency_ms
        self.error_kind = error_kind
        self.retry_after = retry_after
        self.request_id = request_id


def sanitize_error(value: object, secret: Optional[str] = None) -> str:
    text = str(value)[:2000]
    if secret:
        text = text.replace(secret, "***")
    text = re.sub(r"(?i)(authorization|api[-_ ]?key)(\s*[:=]\s*)([^\s,;]+)", r"\1\2***", text)
    return text


def _pinned_url(parsed, address) -> str:
    host = f"[{address}]" if isinstance(address, ipaddress.IPv6Address) else str(address)
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


async def _resolve_addresses_async(hostname, port):
    """A canceled research deadline must not wait for a stalled OS resolver."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    def deliver(value, error):
        if not future.done():
            if error:
                future.set_exception(error)
            else:
                future.set_result(value)
    def resolve():
        try:
            value, error = resolve_host_addresses(hostname, port), None
        except Exception as exc:
            value, error = None, exc
        try:
            loop.call_soon_threadsafe(deliver, value, error)
        except RuntimeError:
            pass  # Deadline closed this research loop; daemon resolver has no side effects.
    threading.Thread(target=resolve, daemon=True, name='research-dns').start()
    return await future


def _post_tool_json(url, headers, body, timeout_seconds, allow_private, secret, safe_mode=False):
    """Research calls have one total DNS/connection/response deadline."""
    started = time.perf_counter()
    async def request():
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ProviderHTTPError('模型 API 地址无效')
        addresses = await _resolve_addresses_async(parsed.hostname, parsed.port)
        _enforce_address_policy(addresses, allow_private)
        last_error = None
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=timeout_seconds) as client:
            for address in addresses:
                try:
                    async with client.stream('POST',_pinned_url(parsed,address),json=body,
                        headers=dict(headers,Host=parsed.netloc),
                        extensions={'sni_hostname':parsed.hostname} if parsed.scheme=='https' else {}) as response:
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 4 * 1024 * 1024:
                                raise ProviderHTTPError('研究模型响应超出大小限制')
                            chunks.append(chunk)
                        return httpx.Response(response.status_code,headers={k:v for k,v in response.headers.items() if k not in {'content-encoding','content-length'}},
                                              content=b''.join(chunks),request=response.request)
                except httpx.HTTPError as exc:
                    if safe_mode:
                        kind = 'CONNECT' if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)) else 'RESPONSE_UNKNOWN'
                        raise ProviderHTTPError(sanitize_error(exc,secret),
                            latency_ms=int((time.perf_counter()-started)*1000), error_kind=kind) from exc
                    last_error = exc
        raise ProviderHTTPError(sanitize_error(last_error,secret))
    async def bounded():
        return await asyncio.wait_for(request(),timeout_seconds)
    try:
        response = asyncio.run(bounded())
        return response, int((time.perf_counter()-started)*1000)
    except (TimeoutError,httpx.TimeoutException) as exc:
        raise ProviderHTTPError('模型请求超时',latency_ms=int((time.perf_counter()-started)*1000),
                                error_kind='RESPONSE_UNKNOWN' if safe_mode else 'UNKNOWN') from exc


def _post_json(url: str, headers: Dict[str, str], body: Dict[str, Any],
               timeout_seconds: int, allow_private: bool,
               secret: Optional[str], safe_mode=False) -> tuple[httpx.Response, int]:
    """校验地址策略后按解析出的 IP 直连请求。

    DNS 只解析一次并把连接钉在该 IP 上, 避免"校验时公网、请求时被重绑定到内网"
    的 TOCTOU 窗口; https 通过 sni_hostname 扩展保留原域名的 SNI 与证书校验。
    """
    if body.get('tools') or safe_mode:
        return _post_tool_json(url,headers,body,timeout_seconds,allow_private,secret,safe_mode=safe_mode)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderHTTPError("模型 API 地址无效")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderHTTPError("模型 API 地址无效")
    addresses = resolve_host_addresses(parsed.hostname, parsed.port)
    _enforce_address_policy(addresses, allow_private)
    extensions = {"sni_hostname": parsed.hostname} if parsed.scheme == "https" else {}
    started = time.perf_counter()
    last_error: Exception | None = None
    for address in addresses:
        request_headers = dict(headers)
        request_headers["Host"] = parsed.netloc
        try:
            with httpx.Client(follow_redirects=False, timeout=timeout_seconds) as client:
                response = client.post(_pinned_url(parsed, address), headers=request_headers,
                                       json=body, extensions=extensions)
            return response, int((time.perf_counter() - started) * 1000)
        except httpx.TimeoutException as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            raise ProviderHTTPError("模型请求超时", latency_ms=elapsed) from exc
        except httpx.HTTPError as exc:
            last_error = exc
    elapsed = int((time.perf_counter() - started) * 1000)
    raise ProviderHTTPError(sanitize_error(last_error, secret), latency_ms=elapsed)


def _retry_after(response):
    value = response.headers.get('retry-after')
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            due = parsedate_to_datetime(value)
            return max(0, due.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return None


def chat_completion(base_url: str, model_name: str, api_key: Optional[str],
                    body: Dict[str, Any], timeout_seconds: int,
                    allow_private: bool = False, safe_mode=False) -> ProviderResult:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = base_url.rstrip("/") + "/chat/completions"
    response, elapsed = _post_json(url, headers, body, timeout_seconds, allow_private, api_key, safe_mode=safe_mode)
    raw = response.text
    if response.status_code < 200 or response.status_code >= 300:
        raise ProviderHTTPError(
            f"模型服务返回 HTTP {response.status_code}", response.status_code,
            raw, elapsed, error_kind='HTTP_STATUS', retry_after=_retry_after(response),
            request_id=response.headers.get('x-request-id'),
        )
    try:
        payload = response.json()
        message = payload["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or None
        content = message.get("content") or ""
        if not content and not tool_calls:
            raise KeyError("empty content")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ProviderHTTPError("模型服务返回格式不兼容", response.status_code, raw, elapsed) from exc
    usage = payload.get("usage") or {}
    return ProviderResult(
        status_code=response.status_code,
        raw_text=raw,
        content=str(content),
        request_id=response.headers.get("x-request-id") or payload.get("id"),
        input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
        output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"),
        latency_ms=elapsed,
        tool_calls=tool_calls,
    )


def gemini_generate_content(base_url: str, model_name: str, api_key: Optional[str],
                            body: Dict[str, Any], timeout_seconds: int,
                            allow_private: bool = False, safe_mode=False) -> ProviderResult:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-goog-api-key"] = api_key
    url = f"{base_url.rstrip('/')}/models/{quote(model_name, safe='')}:generateContent"
    response, elapsed = _post_json(url, headers, body, timeout_seconds, allow_private, api_key, safe_mode=safe_mode)
    raw = response.text
    if response.status_code < 200 or response.status_code >= 300:
        raise ProviderHTTPError(
            f"模型服务返回 HTTP {response.status_code}", response.status_code, raw, elapsed,
            error_kind='HTTP_STATUS', retry_after=_retry_after(response),
            request_id=response.headers.get('x-request-id'),
        )
    try:
        payload = response.json()
        parts = payload["candidates"][0]["content"]["parts"]
        content = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
        tool_calls = [{'id': f'gemini_{i}', 'type': 'function',
                       'function': {'name': part['functionCall']['name'],
                                    'arguments': json.dumps(part['functionCall'].get('args') or {})},
                       'gemini_part': part}
                      for i, part in enumerate(parts) if isinstance(part, dict) and 'functionCall' in part]
        if not content and not tool_calls:
            raise KeyError("empty content")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ProviderHTTPError("Gemini 服务返回格式不兼容", response.status_code, raw, elapsed) from exc
    usage = payload.get("usageMetadata") or {}
    return ProviderResult(
        status_code=response.status_code,
        raw_text=raw,
        content=content,
        request_id=response.headers.get("x-request-id") or payload.get("responseId"),
        input_tokens=usage.get("promptTokenCount"),
        output_tokens=usage.get("candidatesTokenCount"),
        latency_ms=elapsed,
        tool_calls=tool_calls or None,
    )


def response_content(raw_text: str, provider_type: str) -> str:
    payload = json.loads(raw_text)
    if provider_type == "google_gemini":
        parts = payload["candidates"][0]["content"]["parts"]
        return "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
    return str(payload["choices"][0]["message"]["content"])


def analysis_instrument(data: Dict[str, Any]) -> str:
    value = data.get("instrument")
    if isinstance(value, dict):
        value = value.get("symbol")
    return str(value or "").strip().upper()


def _validate_answer(data, analysis_target, snapshot=None):
    instrument = data.get("instrument")
    if isinstance(instrument, dict) and isinstance(instrument.get("symbol"), str):
        data["instrument"] = instrument["symbol"]
    for key in ('schema_version', 'instrument', 'trading_day', 'snapshot_time', 'generated_at'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f'{key} 必须是非空字符串')
    for key in ('snapshot_time', 'generated_at'):
        try:
            datetime.fromisoformat(data[key])
        except ValueError:
            raise ValueError(f'{key} 时间格式无效')
    if snapshot and data['snapshot_time'] != snapshot['market']['snapshot_time']:
        raise ValueError('snapshot_time 与冻结快照不一致')
    for key in ('consensus', 'review_status', 'review_error', 'settlement_metrics',
                'model_called', 'decision_origin', 'publication_status'):
        data.pop(key, None)  # 这些状态只允许由本地程序生成。
    quality = data.get('data_quality')
    if not isinstance(quality, dict) or not isinstance(quality.get('warnings'), list) or not isinstance(quality.get('status'), str):
        raise ValueError('data_quality 必须包含 status 和 warnings')
    forecast = analysis_target == 'NEXT_OPEN'
    outcome = data['next_open_forecast'] if forecast else data
    for key in (('summary',) if forecast else ('summary', 'entry_condition', 'invalidation_condition')):
        if not isinstance(outcome.get(key), str) or not outcome[key].strip():
            raise ValueError(f'{key} 必须是非空字符串')
    risk_key, evidence_key = ('risk_factors', 'basis') if forecast else ('risks', 'evidence')
    risks = outcome.get(risk_key)
    if not isinstance(risks, list) or not risks or any(not isinstance(r, str) or not r.strip() for r in risks):
        raise ValueError(f'{risk_key} 必须是非空风险文本数组')
    directional = outcome['direction'] != 'NO_JUDGMENT'
    if not forecast:
        for key in ('support_levels', 'resistance_levels'):
            levels = outcome.get(key)
            if not isinstance(levels, list) or (directional and not levels):
                raise ValueError(f'{key} 必须是有效价位数组')
            if any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0 for x in levels):
                raise ValueError(f'{key} 价位必须是有限正数')
    evidence = outcome.get(evidence_key)
    if not isinstance(evidence, list):
        raise ValueError(f'{evidence_key} 必须是数组')
    for index, item in enumerate(evidence):
        if not isinstance(item, dict) or item.get('side') not in {'LONG', 'SHORT', 'CONTEXT'}:
            raise ValueError('证据必须是含 side/time/field/value/reason 的对象')
        if any(not isinstance(item.get(k), str) or not item[k].strip() for k in ('time', 'field', 'reason')):
            raise ValueError('证据缺少时间、字段或说明')
        try:
            value = float(item['value'])
            if isinstance(item['value'], bool) or not math.isfinite(value):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError('证据数值无效')
        if snapshot:
            market = snapshot['market']
            rows = ([market.get('quote') or {}] + (market.get('bars_1m') or [])
                    + (market.get('daily_bars_1d') or [])
                    + (market.get('bars_60m') or []) + (market.get('bars_15m') or []))
            matches = [r for r in rows if (r.get('timestamp') or r.get('bar_time')) == item['time']
                       and item['field'] in r]
            try:
                verified = any(math.isclose(float(r[item['field']]), value, rel_tol=1e-9, abs_tol=1e-9) for r in matches)
            except (TypeError, ValueError):
                verified = False
            if not verified:
                raise EvidenceValidationError(
                    f'证据时间、字段和值无法与行情快照核对（{evidence_key}[{index}]：'
                    f'time={item["time"]!r}, field={item["field"]!r}, value={item["value"]!r}）')
    if directional and any(len({(e['time'], e['field']) for e in evidence if e['side'] == side}) < 2
                           for side in ('LONG', 'SHORT')):
        raise ValueError('方向结论至少需要两条多头和两条空头可核对证据')


def parse_analysis_json(content: str, analysis_target: str = "TODAY",
                        require_forecast: bool | None = None, snapshot=None) -> Dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型返回中没有可解析的 JSON 对象")
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("模型返回必须是 JSON 对象")
    if analysis_target not in {"TODAY", "CURRENT_SESSION", "NEXT_OPEN", "FUTURE_TREND"}:
        raise ValueError("analysis_target 仅支持 TODAY、CURRENT_SESSION 或 NEXT_OPEN")
    if require_forecast is not None:
        direction = data.get("direction")
        if direction not in {"LONG", "SHORT", "NEUTRAL", "NO_JUDGMENT"}:
            raise ValueError("direction 枚举无效")
        score = data.get("confidence_score")
        if type(score) not in (int, float) or not 0 <= score <= 100:
            raise ValueError("confidence_score 必须在 0 到 100 之间")
        if not require_forecast:
            return data
    elif analysis_target in {"TODAY", "CURRENT_SESSION", "FUTURE_TREND"}:
        direction = data.get("direction")
        if direction not in {"LONG", "SHORT", "NEUTRAL", "NO_JUDGMENT"}:
            raise ValueError("direction 枚举无效")
        score = data.get("confidence_score")
        if type(score) not in (int, float) or not 0 <= score <= 100:
            raise ValueError("confidence_score 必须在 0 到 100 之间")
        if "next_open_forecast" in data:
            raise ValueError("今日走势结果不能包含 next_open_forecast")
        _validate_answer(data, analysis_target, snapshot)
        if analysis_target == 'FUTURE_TREND':
            from backend.ai_analysis.future_trend import validate_result
            validate_result(data, snapshot)
        return data
    else:
        mixed_fields = {
            "direction", "confidence_score", "summary", "evidence", "support_levels",
            "resistance_levels", "entry_condition", "invalidation_condition", "risks",
        }
        if mixed_fields.intersection(data):
            raise ValueError("明日走势结果不能包含今日走势字段")
    forecast = data.get("next_open_forecast")
    if not isinstance(forecast, dict):
        raise ValueError("next_open_forecast 必须是对象")
    if forecast.get("target_session") not in {"NIGHT", "DAY"}:
        raise ValueError("next_open_forecast.target_session 枚举无效")
    if forecast.get("direction") not in {"UP", "DOWN", "FLAT", "NO_JUDGMENT"}:
        raise ValueError("next_open_forecast.direction 枚举无效")
    forecast_score = forecast.get("confidence_score")
    if type(forecast_score) not in (int, float) or not 0 <= forecast_score <= 100:
        raise ValueError("next_open_forecast.confidence_score 必须在 0 到 100 之间")
    if not isinstance(forecast.get("open_time"), str):
        raise ValueError("next_open_forecast.open_time 必须是字符串")
    if require_forecast is None:
        _validate_answer(data, analysis_target, snapshot)
    return data
