"""东方财富只读分钟线备用源；不加载交易引擎，不订阅，不下单。"""
import hashlib
import http.client
import json
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.common.product_specs import get_exchange
from backend.common.trading_time import FuturesTradingCalendar
from backend.storage.universe_sync import MARKET_MAP, normalize_symbol

URL = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
logger = logging.getLogger(__name__)


def _create_ipv4_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """仅此请求筛选 IPv4，仍由 HTTPSConnection 按原域名校验证书和 SNI。"""
    last_error = None
    for _, _, _, _, sockaddr in socket.getaddrinfo(address[0], address[1], socket.AF_INET, socket.SOCK_STREAM):
        try:
            return socket.create_connection((sockaddr[0], sockaddr[1]), timeout, source_address)
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError('东方财富没有可用的 IPv4 地址')


def _ipv4_https_connection(host, **kwargs):
    connection = http.client.HTTPSConnection(host, **kwargs)
    connection._create_connection = _create_ipv4_connection
    return connection


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_ipv4_https_connection, request, context=self._context,
                            check_hostname=self._check_hostname)


def _secid(symbol):
    symbol = symbol.strip().upper()
    if not re.fullmatch(r'[A-Z]{1,3}\d{3,4}', symbol):
        raise ValueError('备用分钟线只支持具体月份合约')
    exchange = get_exchange(symbol)
    markets = {value:key for key,value in MARKET_MAP.items()}
    if exchange not in markets:
        raise ValueError('备用源不支持该交易所')
    code = symbol
    if exchange == 'CZCE':
        match = re.fullmatch(r'([A-Z]+)(\d{3,4})', symbol)
        code = match[1] + match[2][-3:]
    elif exchange != 'CFFEX':
        code = symbol.lower()
    return markets[exchange], code, exchange


def parse_minute_bars(symbol, raw, url='', requested_at=''):
    market, _, exchange = _secid(symbol)
    payload = json.loads(raw)
    data = payload.get('data') or {}
    if payload.get('rc') != 0 or data.get('market') != market or normalize_symbol(str(data.get('code','')),exchange) != normalize_symbol(symbol,exchange):
        raise ValueError('东方财富返回的具体合约或市场不匹配')
    rows = data.get('klines')
    if not isinstance(rows,list):
        raise ValueError('东方财富未返回分钟线列表')
    collected = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    result = []
    for row in rows:
        fields = row.split(',')
        if len(fields) != 14:
            raise ValueError('东方财富分钟线字段数量异常')
        stamp = datetime.strptime(fields[0],'%Y-%m-%d %H:%M') - timedelta(minutes=1)
        if not FuturesTradingCalendar.is_trading_time_for(stamp,symbol):
            continue
        o,c,h,l = [Decimal(value) for value in fields[1:5]]
        volume,turnover,oi,change = [Decimal(fields[index]) for index in (5,6,12,13)]
        if not all(value.is_finite() for value in (o,c,h,l,volume,turnover,oi,change)):
            raise ValueError('东方财富分钟线含非有限值')
        if (min(o,c,h,l) <= 0 or h < max(o,c,l) or l > min(o,c,h)
                or min(volume,turnover,oi) < 0
                or any(value != value.to_integral_value() for value in (volume,oi,change))):
            raise ValueError('东方财富分钟线价格或量仓无效')
        # 源端持仓增减采用32位无符号编码，负值须还原，不能当作巨量增仓。
        if 2**31 <= change < 2**32:
            change -= 2**32
        if abs(change) > oi:
            raise ValueError('东方财富持仓变化异常')
        provenance = {'source':'eastmoney_history','provider':'eastmoney','source_role':'FALLBACK',
            'label_semantics':'START','source_label_semantics':'END','source_time':fields[0],
            'coverage':'PARTIAL','requested_at':requested_at,'collected_at':collected,
            'request_url':url,'response_sha256':digest,'response_hash_basis':'utf8_decoded_text',
            'trading_day_rule':'FuturesTradingCalendar.get_trading_day',
            'volume_source':'eastmoney_history','turnover_source':'eastmoney_history',
            'open_interest_change_encoding':'signed_uint32'}
        result.append({'instrument_id':symbol.upper(),'period':'1m','bar_time':stamp,
            'trading_day':FuturesTradingCalendar.get_trading_day(stamp),
            'open_price':o,'high_price':h,'low_price':l,'close_price':c,
            'volume':int(volume),'turnover':turnover,'open_interest':oi,'open_interest_change':change,
            'provenance_json':json.dumps(provenance,ensure_ascii=False)})
    return result


def fetch_minute_bars(symbol):
    market,code,_ = _secid(symbol)
    params = {'secid':f'{market}.{code}','klt':1,'fqt':0,'end':'20500101','lmt':10000,
        'fields1':'f1,f2,f3,f4,f5,f6','fields2':','.join(f'f{i}' for i in range(51,65))}
    url = URL + '?' + urllib.parse.urlencode(params)
    requested = datetime.now(timezone.utc).isoformat()
    request = urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://qhweb.eastmoney.com/'})
    try:
        with urllib.request.urlopen(request,timeout=8) as response:
            raw = response.read().decode('utf-8')
    except urllib.error.HTTPError:
        raise
    except (OSError, http.client.HTTPException, urllib.error.URLError):
        # 部分网络的东财 IPv6 可完成 TLS，但读 HTTP 响应时直接断连。
        # 保留正常双栈路径，只在传输失败后独立重试一次 IPv4；不改全局解析。
        opener = urllib.request.build_opener(_IPv4HTTPSHandler())
        with opener.open(request, timeout=8) as response:
            raw = response.read().decode('utf-8')
        logger.info('%s 东方财富分钟线 IPv4 重试成功', symbol)
    return parse_minute_bars(symbol,raw,url,requested)
