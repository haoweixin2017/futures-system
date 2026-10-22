"""Fixed public-source collection and rule-keyword search.

常规资料由程序按注册来源固定获取，不调用研究模型；只有后续 AI 辅助阶段
（由调度器触发）才会消耗模型调用。所有输出沿用 web_research 的资料行格式，
并附带 collection_method 标记实际发现方式。
"""
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from backend.ai_analysis import channel_policy, dce_api, future_trend, web_research
from backend.common.product_specs import extract_product


MAX_INDEX_CHECKS = 8
MAX_SEARCHES = 8
MAX_READS = 12
MAX_MODEL_CALLS = 3
TASK_SECONDS = 90

# 品种固定检索词（计划 7.1 表）；其他品种按名称以相同规则生成。
RULE_KEYWORDS = {
    'FU': {'fundamentals': '燃料油 库存 仓单 供应 船燃需求', 'events': '燃料油 炼厂 制裁 航运'},
    'LU': {'fundamentals': '低硫燃料油 库存 供应 船燃需求', 'events': '低硫燃料油 炼厂 航运 政策'},
    'SC': {'fundamentals': '原油 库存 产量 炼厂加工', 'events': '原油 OPEC 制裁 停产'},
    'BU': {'fundamentals': '沥青 库存 开工率 需求', 'events': '沥青 炼厂 停产 道路施工 政策'},
    'IH': {'fundamentals': '上证50 股指 贴水 升水 成交 持仓 两融', 'events': '上证50 A股 政策 流动性 外围市场 央行'},
    'IF': {'fundamentals': '沪深300 股指 贴水 升水 成交 持仓 两融', 'events': '沪深300 A股 政策 流动性 外围市场 央行'},
    'IC': {'fundamentals': '中证500 股指 贴水 升水 成交 持仓 两融', 'events': '中证500 A股 政策 流动性 外围市场 央行'},
    'IM': {'fundamentals': '中证1000 股指 贴水 升水 成交 持仓 两融', 'events': '中证1000 A股 政策 流动性 外围市场 央行'},
    'JD': {'fundamentals': '鸡蛋 库存 产蛋率 存栏 需求', 'events': '鸡蛋 疫病 养殖 饲料 政策'},
    'LH': {'fundamentals': '生猪 存栏 出栏 屠宰 猪价', 'events': '生猪 疫病 养殖 储备肉 政策'},
    'SA': {'fundamentals': '纯碱 库存 仓单 开工率 玻璃需求', 'events': '纯碱 检修 停产 产能 政策'},
    'AP': {'fundamentals': '苹果 库存 仓单 产量 产区需求', 'events': '苹果 天气 霜冻 采收 出口'},
}

# 小型来源注册表：source_id、入口、适用范围、周期、解析方式与启用状态。
# 不做通用爬虫框架；新增来源=新增一个带专用解析器的条目。
FIXED_SOURCES = (
    {'source_id': 'NBS_LATEST_RELEASE', 'entry': web_research.MACRO_INDEX, 'parser': 'nbs',
     'topics': ('macro',), 'products': (), 'extractable': False, 'enabled': True,
     'title': '国家统计局最新发布', 'freshness_policy': 'LATEST_VERIFIED_RELEASE'},
    {'source_id': 'PBC_NEWS_RELEASE',
     'entry': 'https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html', 'parser': 'pbc',
     'topics': ('macro',), 'products': (), 'extractable': True, 'enabled': True,
     'title': '中国人民银行新闻发布（货币政策/统计）', 'freshness_policy': 'LATEST_VERIFIED_RELEASE',
     'series': (
         ('MONETARY_POLICY', r'货币政策|公开市场业务|存款准备金|再贷款|再贴现|利率'),
         ('FINANCIAL_STATISTICS', r'金融统计数据|社会融资|金融市场运行情况'),
     )},
    {'source_id': 'EIA_WPSR_SUMMARY', 'entry': 'https://ir.eia.gov/wpsr/summary.txt', 'parser': 'eia',
     'topics': (), 'products': ('SC', 'FU', 'LU', 'BU'), 'extractable': False, 'enabled': True,
     'title': 'EIA Weekly Petroleum Status Report Summary', 'series_id': 'EIA:WPSR',
     'dimension': 'related_markets', 'freshness_policy': 'LATEST_VERIFIED_RELEASE',
     'note': '美国原油周度数据仅作相关市场参考，不代表 FU 自身库存'},
    {'source_id': 'SHFE_WAREHOUSE_DAILY',
     'entry': 'https://www.shfe.com.cn/data/tradedata/future/dailydata/stock_data_list.dat',
     'parser': 'shfe', 'topics': (), 'products': ('FU', 'BU', 'SC', 'LU'),
     'extractable': False, 'enabled': True,
     'title': '上期所/上海国际能源交易中心仓单日报', 'series_id': 'SHFE:WAREHOUSE_DAILY',
     'freshness_policy': 'VERIFIED_DATA_PERIOD', 'verified_on': '2026-10-05'},
    {'source_id': 'CZCE_WAREHOUSE_DAILY',
     'entry': 'https://www.czce.com.cn/cn/jysj/cdrb/H770319index_1.htm',
     'parser': None, 'topics': (), 'products': ('SA', 'AP'), 'extractable': False, 'enabled': False,
     'title': '郑商所每日仓单公告', 'series_id': 'CZCE:WAREHOUSE_DAILY', 'dimension': 'fundamentals',
     'block_evidence': '2026-10-05 官方首页、新旧入口、日报正文均 HTTP 412；带 Referer 仍返回脚本拦截页，未取得仓单数据。',
     'block_verified_on': '2026-10-05'},
    {'source_id': 'DCE_WAREHOUSE_DAILY',
     'entry': dce_api.BASE + dce_api.LATEST_PATH,
     'parser': 'dce', 'topics': (), 'products': ('JD', 'LH'), 'extractable': False, 'enabled': True,
     'title': '大商所每日仓单公告', 'series_id': 'DCE:WAREHOUSE_DAILY', 'dimension': 'fundamentals',
     'freshness_policy': 'VERIFIED_DATA_PERIOD', 'verified_on': '2026-10-06'},
    *({'source_id': source_id, 'entry': None, 'parser': None, 'topics': (),
       'products': (product,), 'enabled': False, 'title': title,
       'block_evidence': '尚未核实可稳定读取的公开数据入口；不得用宏观或原油数据替代'}
      for source_id, product, title in (
          ('FU_INVENTORY', 'FU', '燃料油库存'),
          ('FU_IMPORT_SUPPLY', 'FU', '燃料油进口与供应'),
          ('FU_BUNKER_DEMAND', 'FU', '船燃需求'),
          ('BU_REFINERY_OUTPUT', 'BU', '沥青炼厂开工与产量'),
          ('BU_INVENTORY', 'BU', '沥青厂库与社会库存'),
          ('BU_ROAD_DEMAND', 'BU', '道路施工需求'))),
)


class BudgetExhausted(ValueError):
    """Raised when a stage asks for an already spent shared budget item."""


class Budget:
    """One task's shared counters: deadline, index_checks, searches, reads, model_calls."""

    def __init__(self, seconds=TASK_SECONDS, index_checks=MAX_INDEX_CHECKS, searches=MAX_SEARCHES,
                 reads=MAX_READS, model_calls=MAX_MODEL_CALLS, deadline=None):
        self.deadline = deadline if deadline is not None else time.monotonic() + max(0, seconds)
        self.limits = {'index_checks': index_checks, 'searches': searches,
                       'reads': reads, 'model_calls': model_calls}
        self.used = {key: 0 for key in self.limits}

    def time_left(self):
        return self.deadline - time.monotonic()

    def require_time(self, minimum=1.0):
        if self.time_left() <= minimum:
            raise TimeoutError('Research time budget exhausted')

    def remaining(self, kind):
        return max(0, self.limits[kind] - self.used[kind])

    def try_consume(self, kind, amount=1):
        if self.remaining(kind) < amount:
            return False
        self.used[kind] += amount
        return True


def topic_product(topic):
    return topic.split(':', 1)[1] if isinstance(topic, str) and topic.startswith('product:') else None


def _source_applicable(source, topic):
    product = topic_product(topic)
    if topic == 'macro':
        return 'macro' in source['topics']
    if product is None:
        return False
    products = source.get('products')
    return products is None or product in products


def _stamp():
    return web_research._stamp()


# 每来源每轮读取上限：预算内优先最新发布，未读取的记录事件留待后续轮次。
SOURCE_READ_CAPS = {'nbs': 6, 'pbc': 4, 'eia': 1}


def _read_html_candidate(candidate, owner, budget, method):
    """Read one discovered candidate; returns a source row or a skip reason.

    同 URL 修订检测：每轮重新读取注册序列的最新候选，由正文 hash 判定是否产生
    新版本（§6.2 不做条件请求时检查目录及正文 hash 的口径）；相同 hash 由
    持久层去重，不刷新原版本获取时间。
    """
    timeout = min(10, max(1, budget.time_left()))
    page = web_research.read_public_page(dict(candidate, _instrument=candidate.get('_instrument')),
                                         timeout=timeout)
    row = dict(candidate, **page)
    row['body'] = page['body']
    row['content_hash'] = hashlib.sha256(row['body'].encode('utf-8')).hexdigest()
    if row.get('published_at') in (None, 'UNKNOWN') and candidate.get('published_at'):
        row['published_at'] = candidate['published_at']
    row['collection_method'] = method
    row['content_type'] = 'BODY'
    return row, None


def _parse_nbs(source, topic, product, budget, owner, context):
    """国家统计局最新发布目录：直接读取，无需模型发现候选。"""
    timeout = min(10, max(1, budget.time_left()))
    found = web_research.latest_official_macro(timeout=timeout)
    verified_at = _stamp()
    candidates, checks = [], []
    for row in found:
        series = row.get('macro_series')
        if not series:
            continue
        row = dict(row, series_id='NBS:' + series, latest_release=True,
                   latest_verified_at=row.get('latest_verified_at') or verified_at,
                   latest_release_index_url=source['entry'], collection_method='FIXED_SOURCE',
                   source_entry=source['source_id'], freshness_policy=source['freshness_policy'])
        published = future_trend._timestamp(row.get('published_at'))
        if published:
            # Conservative release-cycle guard, including a grace period, not a claimed release date.
            row['expected_update_by'] = (published + timedelta(days=110 if series == 'GDP' else 45)).isoformat()
        candidates.append(row)
        checks.append({'source_id': source['source_id'], 'series_id': row['series_id'],
                       'macro_series': series, 'url': row['url'], 'published_at': row.get('published_at'),
                       'verified_at': verified_at, 'index_url': source['entry'],
                       'latest_release': True, 'status': 'COMPLETED'})
    return {'candidates': candidates, 'series_checks': checks,
            'check': {'source_id': source['source_id'], 'entry': source['entry'],
                      'status': 'COMPLETED', 'checked_at': verified_at,
                      'series_count': len(candidates)}}


def _parse_pbc(source, topic, product, budget, owner, context):
    """央行新闻发布栏目：静态表格列表（newslist_style 链接 + hui12 日期）。"""
    timeout = min(10, max(1, budget.time_left()))
    document, final_url = web_research.public_get(source['entry'], timeout, professional_only=True)
    verified_at = _stamp()
    per_series = {}
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', document, re.S | re.I):
        anchor = re.search(r'<a\s+href="(/goutongjiaoliu/[^"]+)"[^>]*?\btitle="([^"]*)"', row)
        date = re.search(r'class="hui12">\s*(\d{4}-\d{2}-\d{2})\s*<', row)
        if not anchor or not date:
            continue
        title = future_trend._plain(anchor.group(2))
        url = urljoin(source['entry'], anchor.group(1))
        if not future_trend._timestamp(date.group(1)):
            continue
        series = next((name for name, pattern in source['series'] if re.search(pattern, title)), None)
        if not series:
            continue
        key = source['source_id'] + ':' + series
        candidate = {'url': url, 'title': title, 'published_at': date.group(1),
                     'excerpt': '官方发布目录条目', 'source_name': '中国人民银行',
                     'source_kind': 'OFFICIAL', 'dimension': 'macro', 'scope': 'macro',
                     'latest_release': True, 'series_id': key, 'latest_verified_at': verified_at,
                     'latest_release_index_url': source['entry'], 'content_type': 'SEARCH_EXCERPT',
                     'collection_method': 'FIXED_SOURCE', 'source_entry': source['source_id'],
                     'freshness_policy': source['freshness_policy'], 'extractable': True}
        if series == 'FINANCIAL_STATISTICS':
            candidate['expected_update_by'] = (datetime.fromisoformat(date.group(1)) + timedelta(days=45)).isoformat()
        if key not in per_series or candidate['published_at'] > per_series[key]['published_at']:
            per_series[key] = candidate
    candidates, checks = [], []
    for key, candidate in per_series.items():
        candidates.append(candidate)
        checks.append({'source_id': source['source_id'], 'series_id': key,
                       'url': candidate['url'], 'published_at': candidate['published_at'],
                       'verified_at': verified_at, 'index_url': source['entry'],
                       'latest_release': True, 'status': 'COMPLETED'})
    return {'candidates': candidates, 'series_checks': checks,
            'check': {'source_id': source['source_id'], 'entry': source['entry'],
                      'status': 'COMPLETED', 'checked_at': verified_at,
                      'series_count': len(candidates)}}


_MONTHS = {'January': 1, 'February': 2, 'March': 3, 'April': 4, 'May': 5, 'June': 6, 'July': 7,
           'August': 8, 'September': 9, 'October': 10, 'November': 11, 'December': 12}
_EIA_CYCLE_CACHE = {}


def _parse_eia(source, topic, product, budget, owner, context):
    """EIA 周报文本摘要：稳定数据文本由程序直接解析，不用 AI。"""
    timeout = min(10, max(1, budget.time_left()))
    cycle = context.get('collection_cycle_id')
    cached = _EIA_CYCLE_CACHE.get(cycle) if cycle else None
    if cached and time.monotonic() - cached[0] < 600:
        _, document, final_url, verified_at = cached
    else:
        document, final_url = web_research.public_get(source['entry'], timeout, professional_only=True)
        verified_at = _stamp()
        if cycle:
            if len(_EIA_CYCLE_CACHE) >= 64:
                _EIA_CYCLE_CACHE.pop(next(iter(_EIA_CYCLE_CACHE)))
            _EIA_CYCLE_CACHE[cycle] = (time.monotonic(), document, final_url, verified_at)
    match = re.search(r'For the week ending\s+([A-Za-z]+\s+\d{1,2},\s*\d{4})', document)
    if not match:
        raise ValueError('EIA weekly summary format not recognized (week ending missing)')
    parts = re.split(r'\s+|,\s*', match.group(1).strip())
    month = _MONTHS.get(parts[0])
    if not month:
        raise ValueError('EIA weekly summary month not recognized')
    week_ending = datetime(int(parts[2]), month, int(parts[1].rstrip(','))).date().isoformat()
    body = future_trend._plain(document)
    if not body:
        raise ValueError('EIA weekly summary body is empty')
    row = {'url': source['entry'], 'title': source['title'], 'body': body,
           'content_hash': hashlib.sha256(body.encode('utf-8')).hexdigest(),
           'excerpt': body[:4000], 'published_at': 'UNKNOWN',
           'fetched_at': verified_at, 'content_type': 'BODY', 'data_period': 'Week ending ' + week_ending,
           'dimension': source['dimension'], 'scope': 'product', 'product_code': product,
           'source_name': 'EIA', 'source_kind': 'OFFICIAL', 'latest_release': True,
           'series_id': source['series_id'], 'latest_verified_at': verified_at,
           'latest_release_index_url': source['entry'], 'collection_method': 'FIXED_SOURCE',
           'source_entry': source['source_id'], 'freshness_policy': source['freshness_policy'],
           'extractable': False, 'note': source['note'], 'applicable_contracts': []}
    return {'candidates': [], 'rows': [row],
            'series_checks': [{'source_id': source['source_id'], 'series_id': source['series_id'],
                               'url': source['entry'], 'published_at': 'UNKNOWN',
                               'data_period': row['data_period'], 'content_hash': row['content_hash'],
                               'verified_at': verified_at, 'index_url': source['entry'],
                               'latest_release': True, 'status': 'COMPLETED'}],
            'check': {'source_id': source['source_id'], 'entry': source['entry'],
                      'status': 'COMPLETED', 'checked_at': verified_at, 'series_count': 1}}


_WAREHOUSE_TABLES = {'FU': ('燃料油',), 'BU': ('石油沥青(仓库)', '石油沥青(厂库)'),
                     'SC': ('中质含硫原油',), 'LU': ('低硫燃料油(仓库)', '低硫燃料油(厂库)')}


def _warehouse_totals(document, product, date):
    """Read only the verified product tables, never sum subtotals with grand totals."""
    intro = re.search(r'<table\b[^>]*class="content_intro"[^>]*>(.*?)</table>', document, re.S)
    if not intro or not re.match(re.escape(date) + r'\b', future_trend._plain(intro.group(1))):
        raise ValueError('Warehouse report date missing or inconsistent with index')
    expected = _WAREHOUSE_TABLES[product]
    unit = '桶' if product == 'SC' else '吨'
    totals = {}
    for table in re.findall(r'<table\b[^>]*>.*?</table>', document, re.S):
        title = re.search(r'<tr[^>]*class="[^"]*\bspecial_row_type\b[^"]*"[^>]*>(.*?)</tr>', table, re.S)
        if not title:
            continue
        cells = [future_trend._plain(c) for c in re.findall(r'<td\b[^>]*>(.*?)</td>', title.group(1), re.S)]
        if not cells or cells[0] not in expected:
            continue
        name = cells[0]
        headers = [future_trend._plain(c) for c in re.findall(r'<th\b[^>]*>(.*?)</th>', table, re.S)]
        if (len(headers) != 4 or headers[2:] != ['本日数量' if product == 'SC' else '期货', '增减']
                or cells[-1] != '单位：' + unit or name in totals):
            raise ValueError('Warehouse table headers or unit changed')
        aggregate, subtotal = [], []
        for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>', table, re.S):
            values = [future_trend._plain(c) for c in re.findall(r'<td\b[^>]*>(.*?)</td>', tr, re.S)]
            if len(values) != 3 or values[0] not in ('总计', '保税商品总计', '合计'):
                continue
            if not re.fullmatch(r'\d+', values[1]) or not re.fullmatch(r'-?\d+', values[2]):
                raise ValueError('Warehouse aggregate quantity is invalid')
            item = {'table': name, 'quantity': int(values[1]), 'change': int(values[2]), 'unit': unit}
            (subtotal if values[0] == '合计' else aggregate).append(item)
        # Verified LU factory page has a single regional 合计 and no separate 总计.
        selected = aggregate or (subtotal if name == '低硫燃料油(厂库)' else [])
        if len(selected) != 1:
            raise ValueError('Warehouse grand total missing or ambiguous')
        totals[name] = selected[0]
    if set(totals) != set(expected):
        raise ValueError('Warehouse product table missing')
    return [totals[name] for name in expected]


def _warehouse_get(url, budget, stage, requests):
    """Retry one timeout within the caller's shared time and request budgets."""
    counter = 'index_checks' if stage == 'INDEX' else 'reads'
    for attempt in (1, 2):
        try:
            budget.require_time()
        except TimeoutError as exc:
            error = ValueError(f'WAREHOUSE_{stage}: 时间预算耗尽；{url}')
            error.warehouse_requests = requests
            raise error from exc
        if attempt == 2 and not budget.try_consume(counter):
            error = ValueError(f'WAREHOUSE_{stage}: {counter} 重试预算耗尽；{url}')
            error.warehouse_requests = requests
            raise error
        timeout = min(10, budget.time_left())
        started = time.monotonic()
        record = {'stage': 'WAREHOUSE_' + stage, 'url': url, 'attempt': attempt, 'timeout': timeout}
        try:
            result = web_research.public_get(url, timeout, professional_only=True)
        except (TimeoutError, web_research.httpx.TimeoutException) as exc:
            record.update(status='TIMEOUT', error_type=type(exc).__name__,
                          elapsed=round(time.monotonic() - started, 3))
            requests.append(record)
            if attempt == 2:
                error = ValueError(f'WAREHOUSE_{stage}: {type(exc).__name__} after 2 attempts；{url}')
                error.warehouse_requests = requests
                raise error from exc
        else:
            record.update(status='COMPLETED', elapsed=round(time.monotonic() - started, 3))
            requests.append(record)
            return result


def _parse_shfe(source, topic, product, budget, owner, context):
    """Official date index -> current per-product HTML table (verified 2026-10-05)."""
    requests = []
    document, _ = _warehouse_get(source['entry'], budget, 'INDEX', requests)
    index = json.loads(document)
    dates = json.loads(index['dataList'])
    cutoff = future_trend._timestamp((context.get('market') or {}).get('snapshot_time')) or future_trend._timestamp(_stamp())
    eligible = [str(d) for d in dates if re.fullmatch(r'\d{8}', str(d)) and str(d) <= cutoff.strftime('%Y%m%d')]
    if not eligible:
        raise ValueError('Warehouse index has no eligible report date')
    selected = max(eligible)
    date = datetime.strptime(selected, '%Y%m%d').date().isoformat()
    exchange = 'INE' if product in ('SC', 'LU') else 'SHFE'
    url = ('https://www.shfe.com.cn/data/tradedata/future/stockdata/dailystock_' + selected +
           '/ZH/' + exchange.lower() + '/' + product.lower() + '.html')
    document, final_url = _warehouse_get(url, budget, 'BODY', requests)
    totals = _warehouse_totals(document, product, date)
    verified_at = _stamp()
    name = future_trend.research_name(product)
    body = '\n'.join(f"{name} {item['table']} 仓单数量 {item['quantity']}{item['unit']}，增减 {item['change']}{item['unit']}；数据日期 {date}。"
                     for item in totals) + '\n仓单为已制成仓单的货物数量，不代表全部库存。'
    series = exchange + ':WAREHOUSE_DAILY:' + product
    from backend.common.trading_time import FuturesTradingCalendar as Calendar
    next_day = datetime.strptime(date, '%Y-%m-%d') + timedelta(days=1)
    while not Calendar.is_trading_day(next_day.date()):
        next_day += timedelta(days=1)
    expected = next_day.replace(hour=18).isoformat()
    row = {'url': final_url, 'title': name + '官方仓单日报 ' + date, 'body': body, 'excerpt': body,
           'content_hash': hashlib.sha256(body.encode('utf-8')).hexdigest(),
           'document_hash': hashlib.sha256(document.encode('utf-8')).hexdigest(),
           'content_type': 'BODY', 'published_at': 'UNKNOWN', 'data_period': date, 'fetched_at': verified_at,
           'dimension': 'fundamentals', 'scope': 'product', 'product_code': product, 'exchange': exchange,
           'source_name': '上海国际能源交易中心' if exchange == 'INE' else '上海期货交易所',
           'source_kind': 'OFFICIAL', 'series_id': series, 'latest_release': True,
           'latest_verified_at': verified_at, 'latest_release_index_url': source['entry'],
           'freshness_policy': source['freshness_policy'], 'collection_method': 'FIXED_SOURCE',
           'verification_ttl_hours': 24, 'expected_update_by': expected,
           'source_entry': source['source_id'], 'extractable': False, 'applicable_contracts': [],
           'warehouse_quantity': sum(t['quantity'] for t in totals), 'warehouse_unit': totals[0]['unit'],
           'warehouse_totals': totals}
    return {'candidates': [], 'rows': [row],
            'series_checks': [{'source_id': source['source_id'], 'series_id': series, 'url': final_url,
                               'published_at': 'UNKNOWN', 'data_period': date, 'content_hash': row['content_hash'],
                               'verified_at': verified_at, 'index_url': source['entry'],
                               'latest_release': True, 'status': 'COMPLETED'}],
            'check': {'checked_at': verified_at, 'series_count': 1, 'requests': requests}}


def _parse_dce(source, topic, product, budget, owner, snapshot):
    raw_date, payload = dce_api.warehouse(product, budget)
    date = datetime.strptime(raw_date, '%Y%m%d').strftime('%Y-%m-%d')
    entities = payload.get('data', {}).get('entityList')
    if not isinstance(entities, list):
        raise ValueError('DCE_WAREHOUSE_INVALID_ROWS')
    entities = [r for r in entities if isinstance(r, dict) and r.get('varietyOrder') == product.lower()]
    name = future_trend.research_name(product)
    subtotals = [r for r in entities if r.get('variety') == name + '小计'
                 and r.get('whCodeOrder') is None and r.get('groupCodeOrder') is None]
    if len(subtotals) != 1 or any(r.get('genDate') != raw_date for r in entities):
        raise ValueError('DCE_WAREHOUSE_MISSING_SUBTOTAL_OR_DATE_MISMATCH')
    total = subtotals[0]
    details = [r for r in entities if r.get('whCodeOrder') is not None]
    codes = [r['whCodeOrder'] for r in details]
    if not details or len(codes) != len(set(codes)):
        raise ValueError('DCE_WAREHOUSE_MISSING_OR_DUPLICATE_DETAILS')
    parents = {r['groupCodeOrder']: r for r in details if r.get('whType') == '1'
               and r.get('groupCodeOrder') == r['whCodeOrder']}
    if any(r.get('whType') not in ('1', '2') or
           (r.get('groupCodeOrder') and r.get('groupCodeOrder') not in parents) for r in details):
        raise ValueError('DCE_WAREHOUSE_UNKNOWN_HIERARCHY')
    top = list(parents.values()) + [r for r in details if not r.get('groupCodeOrder')]
    for field in ('lastWbillQty', 'wbillQty', 'diff'):
        if any(type(r.get(field)) is not int or (field != 'diff' and r[field] < 0) for r in entities):
            raise ValueError('DCE_WAREHOUSE_INVALID_QUANTITY')
        if sum(r[field] for r in top) != total[field]:
            raise ValueError('DCE_WAREHOUSE_SUBTOTAL_MISMATCH')
        for group, parent in parents.items():
            children = [r for r in details if r.get('groupCodeOrder') == group and r['whCodeOrder'] != group]
            if children and sum(r[field] for r in children) != parent[field]:
                raise ValueError('DCE_WAREHOUSE_GROUP_MISMATCH')
    if any(r['wbillQty'] - r['lastWbillQty'] != r['diff'] for r in entities):
        raise ValueError('DCE_WAREHOUSE_CHANGE_MISMATCH')
    verified_at = _stamp()
    next_day = datetime.strptime(date, '%Y-%m-%d') + timedelta(days=1)
    from backend.common.trading_time import FuturesTradingCalendar as Calendar
    while not Calendar.is_trading_day(next_day.date()):
        next_day += timedelta(days=1)
    body = (f"{name}官方仓单数量 {total['wbillQty']}手，昨日 {total['lastWbillQty']}手，增减 {total['diff']}手；"
            f"数据日期 {date}。按官方品种小计核对集团及分库明细，集团和分库不重复相加。\n"
            '仓单为已注册仓单数量，不代表全部库存；发布日期未提供。')
    url = dce_api.warehouse_url(product, date)
    series = 'DCE:WAREHOUSE_DAILY:' + product
    row = dict(url=url, title=name + '官方仓单日报 ' + date, body=body, excerpt=body,
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        document_hash=hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        content_type='BODY', published_at='UNKNOWN', data_period=date, fetched_at=verified_at,
        dimension='fundamentals', scope='product', product_code=product, exchange='DCE',
        source_name='大连商品交易所', source_kind='OFFICIAL', series_id=series, latest_release=True,
        latest_verified_at=verified_at, latest_release_index_url=source['entry'],
        freshness_policy=source['freshness_policy'], collection_method='FIXED_SOURCE',
        verification_ttl_hours=24, expected_update_by=next_day.replace(hour=18).isoformat(),
        source_entry=source['source_id'], extractable=False, applicable_contracts=[],
        warehouse_quantity=total['wbillQty'], warehouse_unit='手',
        warehouse_totals=[dict(table=name + '小计', quantity=total['wbillQty'], change=total['diff'], unit='手')])
    return {'candidates': [], 'rows': [row], 'series_checks': [dict(source_id=source['source_id'],
        series_id=series, url=url, published_at='UNKNOWN', data_period=date,
        content_hash=row['content_hash'], verified_at=verified_at, index_url=source['entry'],
        latest_release=True, status='COMPLETED')], 'check': dict(checked_at=verified_at, series_count=1)}


PARSERS = {'nbs': _parse_nbs, 'pbc': _parse_pbc, 'eia': _parse_eia, 'shfe': _parse_shfe, 'dce': _parse_dce}


def collect_fixed(snapshot, topic, budget):
    """固定来源检查与正文获取。预算耗尽时记录未检查来源并停止，不抛出。"""
    start_reads = budget.used['reads']
    owner = snapshot.get('owner') or ''
    product = topic_product(topic)
    rows, events, errors, sources_checked, series_checks = [], [], [], [], []
    completed = failed = 0
    for source in FIXED_SOURCES:
        entry = {'source_id': source['source_id'], 'entry': source['entry'],
                 'title': source.get('title'), 'status': 'NOT_APPLICABLE', 'checked_at': None,
                 'reason': None}
        if not _source_applicable(source, topic):
            entry['reason'] = 'SOURCE_NOT_APPLICABLE_TO_TOPIC'
            sources_checked.append(entry)
            continue
        blocked = channel_policy.blocked_reason(source['source_id'], source['entry'])
        if blocked or not source.get('enabled'):
            entry['status'] = 'UNAVAILABLE'
            entry['reason'] = 'SOURCE_BLOCKED: ' + (blocked or source.get('block_evidence', ''))
            entry['blocked_verified_on'] = source.get('block_verified_on')
            sources_checked.append(entry)
            events.append({'type': 'FIXED_CHECK', 'status': 'UNAVAILABLE', 'source_id': source['source_id'],
                           'message': entry['reason'], 'blocked_verified_on': entry['blocked_verified_on']})
            continue
        if source['parser'] == 'dce' and not all(dce_api.credentials()):
            entry.update(status='UNAVAILABLE', reason='DCE_API_CREDENTIALS_MISSING')
            sources_checked.append(entry)
            events.append({'type': 'FIXED_CHECK', 'status': 'UNAVAILABLE', 'source_id': source['source_id'],
                           'message': entry['reason']})
            continue
        # 直接返回正文的专用解析器也必须共享正文读取预算。
        if source['parser'] in ('eia', 'shfe', 'dce') and not budget.try_consume('reads'):
            entry['status'] = 'SKIPPED_BUDGET'
            entry['reason'] = '正文读取次数已用尽'
            sources_checked.append(entry)
            continue
        if not budget.try_consume('index_checks'):
            entry['status'] = 'SKIPPED_BUDGET'
            entry['reason'] = '列表检查次数已用尽'
            sources_checked.append(entry)
            events.append({'type': 'FIXED_CHECK', 'status': 'SKIPPED', 'source_id': source['source_id'],
                           'message': '列表检查预算已用尽，未检查该来源'})
            continue
        try:
            budget.require_time()
        except TimeoutError:
            entry['status'] = 'SKIPPED_BUDGET'
            entry['reason'] = '任务时间预算耗尽'
            sources_checked.append(entry)
            for rest in FIXED_SOURCES:
                if rest['source_id'] in {item['source_id'] for item in sources_checked}:
                    continue
                sources_checked.append({'source_id': rest['source_id'], 'entry': rest['entry'],
                                        'title': rest.get('title'), 'status': 'SKIPPED_BUDGET',
                                        'checked_at': None, 'reason': '任务时间预算耗尽'})
            break
        try:
            parsed = PARSERS[source['parser']](source, topic, product, budget, owner, snapshot)
        except Exception as exc:
            failed += 1
            message = web_research.provider.sanitize_error(exc)
            if hasattr(exc, 'warehouse_requests'):
                entry['requests'] = exc.warehouse_requests
            entry['status'] = 'FAILED'
            entry['reason'] = message
            entry['checked_at'] = _stamp()
            sources_checked.append(entry)
            errors.append({'tool': 'collect_fixed', 'source_id': source['source_id'], 'reason': message})
            events.append({'type': 'FIXED_CHECK', 'status': 'FAILED', 'source_id': source['source_id'],
                           'message': message})
            continue
        completed += 1
        entry['status'] = 'COMPLETED'
        entry['checked_at'] = parsed['check']['checked_at']
        entry['series_count'] = parsed['check'].get('series_count', 0)
        if 'requests' in parsed['check']:
            entry['requests'] = parsed['check']['requests']
        sources_checked.append(entry)
        series_checks.extend(parsed.get('series_checks', []))
        events.append({'type': 'FIXED_CHECK', 'status': 'COMPLETED', 'source_id': source['source_id'],
                       'message': f"检查完成，识别 {entry.get('series_count', 0)} 个序列最新发布"})
        already = {row['url'] for row in rows}
        candidates = sorted(parsed.get('candidates', []),
                            key=lambda item: future_trend._timestamp(item.get('published_at')) or datetime.min,
                            reverse=True)
        read_cap = SOURCE_READ_CAPS.get(source['parser'], 4)
        attempted = 0
        for candidate in candidates:
            if candidate['url'] in already:
                continue
            if attempted >= read_cap or not budget.try_consume('reads'):
                events.append({'type': 'READ', 'status': 'SKIPPED', 'source_id': source['source_id'],
                               'url': candidate['url'], 'title': candidate.get('title'),
                               'message': '读取额度优先用于更新的发布，该候选留待后续轮次'})
                continue
            attempted += 1
            try:
                budget.require_time()
                row, skip = _read_html_candidate(candidate, owner, budget, 'FIXED_SOURCE')
            except TimeoutError:
                events.append({'type': 'READ', 'status': 'SKIPPED', 'source_id': source['source_id'],
                               'url': candidate['url'], 'message': '任务时间预算耗尽'})
                break
            except Exception as exc:
                message = web_research.provider.sanitize_error(exc)
                errors.append({'tool': 'read_public_page', 'url': candidate['url'], 'reason': message})
                events.append({'type': 'ERROR', 'status': 'FAILED', 'tool': 'read_public_page',
                               'url': candidate['url'], 'message': message})
                continue
            rows.append(row)
            already.add(row['url'])
            events.append({'type': 'READ', 'status': 'COMPLETED', 'url': row['url'],
                           'title': row.get('title'), 'dimension': row.get('dimension')})
        for row in parsed.get('rows', []):
            if row['url'] not in already:
                rows.append(row)
                already.add(row['url'])
    status = 'COMPLETED'
    applicable_checks = [item for item in sources_checked if item['status'] != 'NOT_APPLICABLE']
    if applicable_checks and all(item['status'] == 'UNAVAILABLE' for item in applicable_checks):
        status = 'UNAVAILABLE'
    elif failed and not rows and not completed:
        status = 'FAILED'
    elif failed or any(item['status'] == 'SKIPPED_BUDGET' for item in sources_checked):
        status = 'PARTIAL'
    return {'status': status, 'sources': rows, 'candidates': [], 'events': events, 'errors': errors,
            'channel_policy': channel_policy.summary(),
            'sources_checked': sources_checked, 'latest_release_checks': series_checks,
            'checks_count': completed + failed, 'reads': budget.used['reads'] - start_reads}


def rule_keywords(topic):
    product = topic_product(topic)
    if not product:
        return {}
    known = RULE_KEYWORDS.get(product)
    if known:
        return dict(known)
    name = future_trend.research_name(product) or product
    return {'fundamentals': f'{name} 库存 仓单 供需', 'events': f'{name} 政策 制裁 停产'}


def _extractable_rows(rows):
    return [row for row in rows if row.get('extractable') and row.get('content_type') == 'BODY']


def collect_rule_search(snapshot, topic, budget):
    """按品种固定关键词搜索并读取候选正文；不调用模型。"""
    start_reads = budget.used['reads']
    owner = snapshot.get('owner') or ''
    cutoff = future_trend._timestamp(snapshot['market']['snapshot_time'])
    keywords = rule_keywords(topic)
    if not keywords:
        return {'status': 'NOT_APPLICABLE', 'sources': [], 'candidates': [], 'events': [],
                'errors': [], 'searches': 0, 'reads': 0}
    product = topic_product(topic)
    rows, candidates, events, errors = [], [], [], []
    searches = 0
    for dimension, query in keywords.items():
        if not budget.try_consume('searches'):
            events.append({'type': 'SEARCH', 'status': 'SKIPPED', 'tool': 'search_public_web',
                           'query': query, 'dimension': dimension, 'message': '搜索次数预算已用尽'})
            continue
        searches += 1
        events.append({'type': 'SEARCH', 'status': 'RUNNING', 'tool': 'rule_search',
                       'query': query, 'dimension': dimension})
        try:
            budget.require_time()
            timeout = min(10, max(1, budget.time_left()))
            found = web_research.search_public_web(query, dimension, timeout=timeout,
                                                   instrument=product, cutoff=cutoff, events=events)
        except TimeoutError:
            events.append({'type': 'SEARCH', 'status': 'SKIPPED', 'tool': 'rule_search',
                           'query': query, 'dimension': dimension, 'message': '任务时间预算耗尽'})
            break
        except Exception as exc:
            message = web_research.provider.sanitize_error(exc)
            errors.append({'tool': 'rule_search', 'reason': message})
            events.append({'type': 'ERROR', 'status': 'FAILED', 'tool': 'rule_search',
                           'query': query, 'message': message})
            continue
        kept = 0
        for candidate in found:
            if not web_research._relevant(candidate, product, dimension):
                events.append({'type': 'FILTER', 'status': 'SKIPPED', 'url': candidate.get('url'),
                               'title': candidate.get('title'), 'message': '与当前品种或维度不相关，已剔除'})
                continue
            candidate = dict(candidate, dimension=dimension, collection_method='RULE_SEARCH')
            if kept < 4:
                if not budget.try_consume('reads'):
                    events.append({'type': 'READ', 'status': 'SKIPPED', 'url': candidate['url'],
                                   'message': '正文读取预算已用尽，保留为候选'})
                    candidates.append(candidate)
                    continue
                try:
                    budget.require_time()
                    row, skip = _read_html_candidate(dict(candidate, _instrument=product),
                                                     owner, budget, 'RULE_SEARCH')
                except TimeoutError:
                    candidates.append(candidate)
                    events.append({'type': 'READ', 'status': 'SKIPPED', 'url': candidate['url'],
                                   'message': '任务时间预算耗尽，保留为候选'})
                    break
                except Exception as exc:
                    message = web_research.provider.sanitize_error(exc)
                    errors.append({'tool': 'read_public_page', 'url': candidate['url'], 'reason': message})
                    events.append({'type': 'ERROR', 'status': 'FAILED', 'tool': 'read_public_page',
                                   'url': candidate['url'], 'message': message})
                    continue
                scope = web_research._source_scope(row.get('title') or '', row['body'],
                                                   product, dimension)
                row.update(scope)
                if row['dimension'] in ('events', 'related_markets') and product in ('FU', 'LU', 'SC', 'BU', 'PG'):
                    row.setdefault('scope', 'product')
                row['published_at'] = row.get('published_at') or 'UNKNOWN'
                rows.append(row)
                kept += 1
                events.append({'type': 'READ', 'status': 'COMPLETED', 'url': row['url'],
                               'title': row.get('title'), 'dimension': dimension})
            else:
                candidates.append(candidate)
        events.append({'type': 'SEARCH', 'status': 'COMPLETED', 'tool': 'rule_search',
                       'query': query, 'dimension': dimension,
                       'message': f'{len(found)} 个候选，已读取 {kept} 篇正文'})
    return {'status': 'COMPLETED' if rows or candidates or not errors else 'FAILED',
            'sources': rows, 'candidates': candidates, 'events': events, 'errors': errors,
            'searches': searches, 'reads': budget.used['reads'] - start_reads}
