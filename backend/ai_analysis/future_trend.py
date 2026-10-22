"""Frozen trading-day horizons and attributable public research materials."""
import concurrent.futures
import html
import hashlib
import json
import math
import re
from datetime import datetime, time, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from backend.common.product_specs import extract_product, get_exchange
from backend.common.trading_time import FuturesTradingCalendar as Calendar
from backend.ai_analysis import dce_api


EXTERNAL_DIMENSIONS = ('fundamentals', 'macro', 'events', 'related_markets')
DIMENSIONS = ('technical', 'volume_oi') + EXTERNAL_DIMENSIONS
_ARTICLE_HOSTS = {'www.stats.gov.cn', 'futures.eastmoney.com', 'finance.eastmoney.com',
                  'stock.eastmoney.com', 'wap.eastmoney.com'}


def research_name(instrument, name=''):
    names = {'FU': '燃料油', 'LU': '低硫燃料油', 'BU': '沥青', 'SC': '原油',
             'IH': '上证50股指', 'IF': '沪深300股指', 'IC': '中证500股指', 'IM': '中证1000股指',
             'RB': '螺纹钢', 'HC': '热卷', 'I': '铁矿石', 'J': '焦炭', 'JM': '焦煤',
             'RU': '天然橡胶', 'NR': '天然橡胶', 'BR': '合成橡胶', 'SP': '纸浆',
             'AU': '黄金', 'AG': '白银', 'CU': '铜', 'AL': '铝', 'ZN': '锌',
             'PB': '铅', 'NI': '镍', 'SN': '锡', 'SS': '不锈钢', 'AO': '氧化铝',
             'M': '豆粕', 'Y': '豆油', 'P': '棕榈油', 'C': '玉米', 'CS': '玉米淀粉',
             'A': '大豆', 'B': '大豆', 'L': '聚乙烯', 'V': 'PVC', 'PP': '聚丙烯',
             'EG': '乙二醇', 'EB': '苯乙烯', 'PG': '液化石油气', 'JD': '鸡蛋',
             'LH': '生猪', 'SR': '白糖', 'CF': '棉花', 'CY': '棉纱', 'OI': '菜籽油',
             'RM': '菜粕', 'TA': 'PTA', 'MA': '甲醇', 'FG': '玻璃', 'SA': '纯碱',
             'UR': '尿素', 'PF': '涤纶短纤', 'AP': '苹果', 'CJ': '红枣', 'PK': '花生',
             'SI': '工业硅', 'LC': '碳酸锂', 'PS': '多晶硅', 'EC': '集运指数'}
    return names.get(extract_product(instrument), re.sub(r'\d+', '', name or instrument).strip())


def targets(instrument, now, reference_day, reference_price):
    reference = datetime.strptime(reference_day, '%Y%m%d').date()
    if not Calendar.is_trading_day(reference):
        reference = Calendar.previous_trading_day(reference + timedelta(days=1))
    # A trading day starts with its actual night/day opening. Before opening,
    # holidays and instruments without a preceding night use the completed day.
    assigned = now.date()
    if Calendar.is_trading_day(assigned):
        day_open = time(9, 30) if get_exchange(instrument) == 'CFFEX' else time(9)
        previous = Calendar.previous_trading_day(assigned)
        if (now.time() < day_open
                and not (Calendar.has_night_session(instrument)
                         and Calendar._night_session_day(previous))):
            assigned = previous
        if (now.time() >= time(21) and Calendar.has_night_session(instrument)
                and Calendar._night_session_day(assigned)):
            assigned = datetime.strptime(Calendar.get_trading_day(now), '%Y%m%d').date()
    elif now.time() < time(3) and Calendar.is_trading_time_for(now, instrument):
        assigned = datetime.strptime(Calendar.get_trading_day(now), '%Y%m%d').date()
    else:
        assigned = Calendar.previous_trading_day(assigned + timedelta(days=1))
    day = max(reference, assigned)
    close = time(15, 15) if extract_product(instrument) in {'T', 'TF', 'TS', 'TL'} and get_exchange(instrument) == 'CFFEX' else time(15)
    result = []
    for count in range(1, 6):
        day += timedelta(days=1)
        while not Calendar.is_trading_day(day):
            day += timedelta(days=1)
        if count in (3, 5):
            due = datetime.combine(day, close)
            result.append({'horizon_days': count, 'trading_day': day.strftime('%Y%m%d'),
                           'bar_time': (due - timedelta(minutes=1)).isoformat(),
                           'due_time': (due + timedelta(minutes=2)).isoformat(), 'price_field': 'close_price',
                           'reference_price': reference_price})
    return result


def _contract_notice(rules, instrument, now, calendar_years):
    notices = [n for n in rules.get('special_notices', [])
               if str(n.get('instrument', '')).upper() == instrument.upper()]
    if not notices:
        return None
    if len(notices) != 1:
        raise ValueError('合约专项公告配置冲突')
    notice = notices[0]
    verified = _timestamp(notice.get('verified_on'))
    announced = _timestamp(notice.get('published_on'))
    adjusted = _timestamp(notice.get('last_trading_day'))
    if (not verified or not announced or not adjusted or verified > now or announced > now
            or not notice.get('source_url') or not notice.get('source_title')
            or adjusted.year not in calendar_years or not Calendar.is_trading_day(adjusted.date())):
        raise ValueError('合约专项公告来源、日期或日历未核实')
    return notice


def _czce_rule_bound(rule, rules, instrument, frozen_targets, now, year, month):
    """A captured official rule proves pre-delivery validity, not an exact expiry."""
    name = {'SA':'纯碱', 'AP':'苹果'}[rule['product']]
    body = (Path(__file__).resolve().parents[2] / rule['evidence_file']).read_bytes()
    capture = json.loads(body.decode('utf-8-sig'))
    text = re.sub(r'\s+', '', '\n'.join(capture['text']))
    verified = _timestamp(rule['verified_at'])
    effective = _timestamp(rule['effective_from'])
    published = _timestamp(rule['source_published_on'])
    delivery = datetime(year, month, 1)
    applies = datetime.strptime(rule['applies_from_delivery_month'], '%Y-%m')
    months = rule['delivery_months']
    month_clause = ('1至12月' if months == list(range(1,13)) else
                    '、'.join(str(m) for m in months) + '月')
    clause = f'第九条{name}期货合约最后交易日为合约交割月份的第10个交易日。'
    effective_clause = f'自{effective.year}年{effective.month}月{effective.day}日起施行' if effective else ''
    applies_clause = (f'适用于苹果期货{applies.strftime("%y%m")}及后续合约'
                     if rule['product'] == 'AP' else effective_clause)
    valid = (rule['instrument'] == instrument.upper() and verified and effective and published
        and verified <= now and published <= verified and effective <= now
        and _timestamp(capture['captured_at']) == verified
        and capture.get('capture_method') == 'desktop_edge_uia_document_text'
        and urlsplit(rule['source_url']).hostname == 'www.czce.com.cn'
        and urlsplit(rule['source_url']).path.startswith('/cn/flfg/zcjywgz/pzxz/webinfo/')
        and capture['url'] == rule['source_url']
        and hashlib.sha256(body).hexdigest() == rule['sha256']
        and rule['source_title'] in text and clause in text
        and re.sub(r'\s+', '', rule['rule']) == clause
        and f'发布日期：{rule["source_published_on"]}' in text
        and effective_clause in text and applies_clause in text
        and f'第六条{name}期货合约交割月份为{month_clause}。' in text
        and f'第十条{name}期货合约交易代码为{rule["product"]}。' in text
        and month in months and delivery >= applies
        and (rule['product'] == 'AP' or applies.strftime('%Y-%m') == effective.strftime('%Y-%m')))
    if not valid:
        raise ValueError('官方期限下界证据、适用版本或核验时间无效')
    notice = _contract_notice(rules, instrument, now, rule['calendar_years'])
    last = _timestamp(notice['last_trading_day']) if notice else None
    assessed = []
    for target in frozen_targets:
        day = datetime.strptime(target['trading_day'], '%Y%m%d')
        status = 'UNKNOWN'
        if day.year in rule['calendar_years'] and Calendar.is_trading_day(day.date()):
            if last:
                status = 'WITHIN_RULE_TERM' if day <= last else 'AFTER_RULE_TERM'
            elif now < delivery and now.date() <= day.date() < delivery.date():
                status = 'WITHIN_RULE_TERM'
        assessed.append(dict(target, status=status))
    result = {'status':'VERIFIED' if notice else 'RULE_BOUND',
        'term_basis':'OFFICIAL_NOTICE' if notice else 'OFFICIAL_RULE_LOWER_BOUND',
        'delivery_month':delivery.strftime('%Y-%m'), 'source_url':rule['source_url'],
        'source_title':rule['source_title'], 'rule':rule['rule'], 'verified_at':rule['verified_at'],
        'evidence_sha256':rule['sha256'], 'evidence_file':rule['evidence_file'],
        'source_published_on':rule['source_published_on'],
        'special_notice':notice, 'special_notice_status':'VERIFIED_OVERRIDE' if notice else 'NOT_CHECKED',
        'limitations':'期限使用已核验专项公告调整。' if notice else
            '仅确认交割月之前未越过标准期限；未推算精确最后交易日，2027年日历及合约专项调整公告未核实。',
        'targets':assessed}
    if last:
        result['last_trading_day'] = last.strftime('%Y%m%d')
    else:
        result['last_trading_day_not_before'] = delivery.strftime('%Y%m%d')
    return result


def contract_lifecycle(instrument, frozen_targets, now):
    """Only verified official dates/rules and calendar years may establish a term."""
    result = {'status':'UNKNOWN', 'targets':[
        {'horizon_days':t['horizon_days'], 'trading_day':t['trading_day'], 'status':'UNKNOWN'}
        for t in frozen_targets], 'limitations':'未核实该品种的合约期限规则',
        'special_notice_status':'NOT_CHECKED'}
    match = re.fullmatch(r'([A-Z]+)(\d{2})(\d{2})', instrument.upper())
    if not match:
        return result
    rules = json.loads((Path(__file__).resolve().parents[2] / 'config' /
                        'future_contract_rules.json').read_text(encoding='utf-8'))
    exact = [r for r in rules.get('verified_contracts', []) if r['instrument'] == instrument.upper()]
    if exact:
        record = exact[0]
        try:
            verified = _timestamp(record['verified_at'])
            last = datetime.strptime(record['last_trading_day'], '%Y%m%d').date()
            start = datetime.strptime(record['start_trading_day'], '%Y%m%d').date()
            body = (Path(__file__).resolve().parents[2] / record['evidence_file']).read_bytes()
            data = json.loads(body)
            matching = [r for r in data['data'] if str(r.get('contractId', '')).upper() == instrument.upper()]
            valid = (len(exact) == 1 and verified is not None and verified <= now and start <= now.date()
                and urlsplit(record['source_url']).hostname == 'www.dce.com.cn'
                and hashlib.sha256(body).hexdigest() == record['sha256']
                and data.get('success') is True and data.get('code') == 200 and len(matching) == 1
                and matching[0].get('endTradeDate') == record['last_trading_day']
                and matching[0].get('startTradeDate') == record['start_trading_day']
                and last.year in record['calendar_years'] and Calendar.is_trading_day(last)
                and all(int(t['trading_day'][:4]) in record['calendar_years'] for t in frozen_targets))
        except (KeyError, TypeError, ValueError, OSError):
            valid = False
        if not valid:
            result['limitations'] = '官方具体合约元数据未可用、冲突或原响应核验失败'
            return result
        try:
            notice = _contract_notice(rules, instrument, now, record['calendar_years'])
        except ValueError as exc:
            result['limitations'] = str(exc)
            return result
        if notice:
            last = _timestamp(notice['last_trading_day']).date()
        return {'status':'VERIFIED', 'term_basis':'OFFICIAL_CONTRACT_METADATA',
            'last_trading_day':last.strftime('%Y%m%d'), 'start_trading_day':record['start_trading_day'],
            'last_trading_day_from_metadata':record['last_trading_day'], 'special_notice':notice,
            'source_url':record['source_url'], 'source_title':record['source_title'],
            'verified_at':record['verified_at'], 'evidence_sha256':record['sha256'],
            'special_notice_status':'VERIFIED_OVERRIDE' if notice else 'NOT_CHECKED',
            'limitations':'期限使用已核验专项公告调整。' if notice else
                '官方接口直接返回该具体合约日期；未泛化其他合约，专项公告仍未单独核验。',
            'targets':[dict(t, status='WITHIN_RULE_TERM' if
                record['start_trading_day'] <= t['trading_day'] <= last.strftime('%Y%m%d') else 'AFTER_RULE_TERM')
                for t in frozen_targets]}
    matching_rules = [r for r in rules['rules'] if r['product'] == match[1]]
    if len(matching_rules) > 1:
        result['limitations'] = '品种期限规则配置冲突'
        return result
    rule = matching_rules[0] if matching_rules else {}
    if rule.get('status') != 'VERIFIED_STANDARD_RULE':
        result['limitations'] = rule.get('reason', result['limitations'])
        return result
    if now.date().isoformat() < rule['available_from']:
        result['limitations'] = '快照时间早于本地规则核验可用日期'
        return result
    year, month = 2000 + int(match[2]), int(match[3])
    if not 1 <= month <= 12:
        return result
    kind = rule['rule_kind']
    if kind == 'DELIVERY_MONTH_TENTH_TRADING_DAY_BOUND':
        try:
            return _czce_rule_bound(rule, rules, instrument, frozen_targets, now, year, month)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            result['limitations'] = '官方期限下界未核实：' + str(exc)
            return result
    if kind == 'PREVIOUS_MONTH_LAST_TRADING_DAY':
        candidate = datetime(year, month, 1).date() - timedelta(days=1)
    elif kind == 'DELIVERY_MONTH_15_ROLL_FORWARD':
        candidate = datetime(year, month, 15).date()
    elif kind == 'DELIVERY_MONTH_THIRD_FRIDAY_ROLL_FORWARD':
        first = datetime(year, month, 1).date()
        candidate = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
    else:
        return result
    if candidate.year not in rule['calendar_years'] or any(
            int(str(t['trading_day'])[:4]) not in rule['calendar_years'] for t in frozen_targets):
        result['limitations'] = '推导日期或预测目标超出已核验日历年份'
        return result
    if kind == 'PREVIOUS_MONTH_LAST_TRADING_DAY':
        last = Calendar.previous_trading_day(candidate + timedelta(days=1))
    else:
        last = candidate
        while not Calendar.is_trading_day(last):
            last += timedelta(days=1)
    try:
        notice = _contract_notice(rules, instrument, now, rule['calendar_years'])
    except ValueError as exc:
        result['limitations'] = str(exc)
        return result
    if notice:
        last = _timestamp(notice['last_trading_day']).date()
    return {'status':'RULE_BASED', 'delivery_month':f'{year:04d}-{month:02d}',
            'last_trading_day_by_rule':last.strftime('%Y%m%d'),
            'special_notice':notice,
            'source_url':rule['source_url'], 'source_title':rule['source_title'],
            'rule':rule['rule'], 'rule_verified_on':rule['rule_verified_on'],
            'source_published_on':rule.get('source_published_on'),
            'calendar_evidence_scope':rules['calendar_evidence_scope'],
            'special_notice_status':'VERIFIED_OVERRIDE' if notice else 'NOT_CHECKED',
            'limitations':'期限使用已核验专项公告调整。' if notice else '按标准合约及2026年日历推导；临近到期需另查交易所调整公告，不是合约专项公告核验。',
            'targets':[{'horizon_days':t['horizon_days'], 'trading_day':t['trading_day'],
                        'status':'WITHIN_RULE_TERM' if t['trading_day'] <= last.strftime('%Y%m%d') else 'AFTER_RULE_TERM'}
                       for t in frozen_targets]}


def horizon_term_status(snapshot, target):
    """Require one matching frozen term assessment; missing/conflicting means unknown."""
    lifecycle = snapshot.get('contract_lifecycle') or {}
    if lifecycle.get('status') not in ('RULE_BASED', 'VERIFIED', 'RULE_BOUND'):
        return 'UNKNOWN'
    matches = [row for row in lifecycle.get('targets', [])
               if row.get('horizon_days') == target['horizon_days']
               and row.get('trading_day') == target['trading_day']]
    if len(matches) != 1:
        return 'UNKNOWN'
    return matches[0].get('status', 'UNKNOWN')


def _timestamp(value):
    if not value or value == 'UNKNOWN':
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError):
        return None


def _plain(value):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', str(value or '')))).strip()


def _registered_dce_warehouse(row):
    product = row.get('product_code')
    period = _timestamp(row.get('data_period'))
    return (product in ('JD', 'LH') and period is not None
        and row.get('source_entry') == 'DCE_WAREHOUSE_DAILY'
        and row.get('exchange') == 'DCE' and row.get('source_kind') == 'OFFICIAL'
        and row.get('content_type') == 'BODY' and row.get('collection_method') == 'FIXED_SOURCE'
        and row.get('series_id') == 'DCE:WAREHOUSE_DAILY:' + product
        and row.get('url') == dce_api.warehouse_url(product, period.strftime('%Y-%m-%d'))
        and row.get('latest_release_index_url') == dce_api.BASE + dce_api.LATEST_PATH)


def _verified_data_period(row, now):
    """Narrow exception for the registered official warehouse daily series."""
    product = row.get('product_code')
    if product not in ('FU', 'BU', 'SC', 'LU', 'JD', 'LH'):
        return False
    exchange = 'INE' if product in ('SC', 'LU') else 'SHFE'
    period = _timestamp(row.get('data_period'))
    checked = _timestamp(row.get('latest_verified_at'))
    expected = _timestamp(row.get('expected_update_by'))
    if not period or not checked or not expected:
        return False
    if product in ('JD', 'LH'):
        return (_registered_dce_warehouse(row) and row.get('freshness_policy') == 'VERIFIED_DATA_PERIOD'
            and row.get('latest_release') is True and period <= checked <= now <= expected
            and now - checked <= timedelta(hours=24))
    url = ('https://www.shfe.com.cn/data/tradedata/future/stockdata/dailystock_' +
           period.strftime('%Y%m%d') + '/ZH/' + exchange.lower() + '/' + product.lower() + '.html')
    return (row.get('freshness_policy') == 'VERIFIED_DATA_PERIOD'
        and row.get('source_entry') == 'SHFE_WAREHOUSE_DAILY'
        and row.get('source_kind') == 'OFFICIAL' and row.get('content_type') == 'BODY'
        and row.get('collection_method') == 'FIXED_SOURCE' and row.get('latest_release') is True
        and row.get('series_id') == exchange + ':WAREHOUSE_DAILY:' + product
        and row.get('url') == url
        and row.get('latest_release_index_url') == 'https://www.shfe.com.cn/data/tradedata/future/dailydata/stock_data_list.dat'
        and period <= checked <= now <= expected and now - checked <= timedelta(hours=24))


def _is_stale(row, now):
    if row.get('freshness_policy') == 'VERIFIED_DATA_PERIOD':
        return not _verified_data_period(row, now)
    published = _timestamp(row.get('published_at'))
    if published is None:
        return True
    # 明确注册的官方周期性序列（如 EIA 周报）使用最新已核验版本，不按天数过期；
    # 核验有效期独立于轮询间隔；周期报告另有包含宽限期的更新上限。
    expected = _timestamp(row.get('expected_update_by'))
    if expected and _timestamp(now.isoformat()) > expected:
        return True
    if (row.get('source_kind') == 'OFFICIAL' and row.get('latest_release')
            and (row.get('macro_series') or row.get('series_id')) and row.get('latest_verified_at')):
        return False
    if row.get('dimension') == 'macro' and row.get('source_kind') == 'OFFICIAL' and row.get('latest_release'):
        return False
    days = {'fundamentals': 14, 'events': 7, 'related_markets': 7}.get(row.get('dimension'), 30)
    return (now.date() - published.date()).days > days


def normalize_materials(rows, now, allowed_hosts=None):
    result, urls, contents = [], set(), set()
    local_now = _timestamp(now.isoformat())
    for row in rows:
        if not isinstance(row, dict):
            continue
        url, title, excerpt = str(row.get('url') or ''), _plain(row.get('title')), _plain(row.get('excerpt'))
        published = _timestamp(row.get('published_at'))
        if (not title or not excerpt or (not url.startswith('https://') and not _registered_dce_warehouse(row))
                or (urlsplit(url).hostname not in (_ARTICLE_HOSTS | set(allowed_hosts or ()))
                    and not _registered_dce_warehouse(row)) or (published and published > local_now)):
            continue
        key = (title, excerpt)
        if url in urls or key in contents or row.get('dimension') not in EXTERNAL_DIMENSIONS:
            continue
        urls.add(url)
        contents.add(key)
        result.append({
            'source_id': 'S' + str(len(result) + 1), 'title': title, 'url': url,
            'source_name': row.get('source_name') or urlsplit(url).hostname,
            'source_kind': row.get('source_kind') or 'NEWS',
            'published_at': (str(row['published_at']) if len(str(row['published_at'])) == 10
                             else published.isoformat()) if published else 'UNKNOWN',
            'fetched_at': row.get('fetched_at') or datetime.now(timezone(timedelta(hours=8))).isoformat(),
            'data_period': row.get('data_period') or 'UNKNOWN',
            'scope': row.get('scope') or 'product', 'dimension': row['dimension'],
            'excerpt': excerpt, 'content_type': row.get('content_type') or 'SEARCH_EXCERPT',
            'stale': _is_stale(row, local_now),
            'warnings': ['UNKNOWN_PUBLICATION_TIME'] if published is None else [],
        })
    return result


class _BodyParser(HTMLParser):
    """Extract only an article container; omit scripts, navigation and styles."""
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.active_depth = None
        self.skip = 0
        self.parts = []
        self.finished = False

    def handle_starttag(self, tag, attrs):
        if tag in {'br', 'img', 'meta', 'link', 'input', 'hr', 'source'}:
            return
        self.depth += 1
        attributes = dict(attrs)
        marker = attributes.get('id', '') + ' ' + attributes.get('class', '')
        if (not self.finished and self.active_depth is None
                and re.search(r'\b(ContentBody|article-content|TRS_Editor|trs_editor|TRS_UEDITOR)\b', marker)):
            self.active_depth = self.depth
        if self.active_depth is not None and tag in {'script', 'style', 'nav'}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {'br', 'img', 'meta', 'link', 'input', 'hr', 'source'}:
            return
        if self.active_depth is not None and tag in {'script', 'style', 'nav'} and self.skip:
            self.skip -= 1
        if self.depth == self.active_depth:
            self.active_depth = None
            self.finished = True
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.active_depth is not None and not self.skip:
            self.parts.append(data)


def _get(url, **kwargs):
    with httpx.Client(timeout=3.5, follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0'}) as client:
        response = client.get(url, **kwargs)
        response.raise_for_status()
        return response.content.decode('utf-8', errors='replace')


def _article(url):
    if urlsplit(url).scheme != 'https' or urlsplit(url).hostname not in _ARTICLE_HOSTS:
        return ''
    parser = _BodyParser()
    parser.feed(_get(url))
    return _plain(' '.join(parser.parts))[:4000]


def _search(name, dimension='fundamentals', now=None):
    now = _timestamp((now or datetime.now()).isoformat())
    suffix = {'fundamentals': '库存', 'events': '政策', 'related_markets': '价格'}[dimension]
    param = {'uid': '', 'keyword': name + suffix, 'type': ['cmsArticleWebOld'],
             'client': 'web', 'clientType': 'web', 'clientVersion': '1',
             'param': {'cmsArticleWebOld': {'searchScope': 'default', 'sort': 'date',
                                           'pageIndex': 1, 'pageSize': 20}}}
    def query(keyword):
        param['keyword'] = keyword
        body = _get('https://search-api-web.eastmoney.com/search/jsonp',
                    params={'cb': 'jQuery', 'param': json.dumps(param, ensure_ascii=False)},
                    headers={'Referer': 'https://so.eastmoney.com/'})
        match = re.search(r'^\s*jQuery\((.*)\)\s*;?\s*$', body, re.S)
        payload = json.loads(match.group(1) if match else body)
        entries = payload.get('result', {}).get('cmsArticleWebOld', [])
        if not isinstance(entries, list):
            raise ValueError('Search response articles must be a list')
        return entries
    keywords = {'fundamentals': r'库存|供需|供应|需求|产量|开工|进口|出口',
                'events': r'政策|制裁|关税|战争|冲突|会议|袭击',
                'related_markets': r'原油|美元|汇率|海外|国际|相关市场'}
    def relevant(entries):
        return [entry for entry in entries if isinstance(entry, dict)
                and re.search(keywords[dimension], _plain(entry.get('title')) + _plain(entry.get('content')))]
    entries = relevant(query(name + suffix))
    if not any(not _is_stale({'published_at': e.get('date'), 'dimension': dimension}, now)
               and _timestamp(e.get('date')) <= now for e in entries):
        entries.extend(relevant(query(name)))
    entries.sort(key=lambda entry: _timestamp(entry.get('date')) or datetime.min, reverse=True)
    entries = [e for e in entries if not _timestamp(e.get('date')) or _timestamp(e.get('date')) <= now][:4]
    result = []
    for entry in entries:
        url = str(entry.get('url') or '')
        if url.startswith('http://') and urlsplit(url).hostname in _ARTICLE_HOSTS:
            url = 'https://' + url[len('http://'):]
        result.append({'title': entry.get('title'), 'url': url,
                       'published_at': entry.get('date'), 'excerpt': entry.get('content'),
                       'source_name': entry.get('mediaName') or '东方财富',
                       'source_kind': 'NEWS', 'scope': 'product', 'dimension': dimension,
                       'content_type': 'SEARCH_EXCERPT'})
    # A search excerpt is explicitly labeled; one bounded full-body attempt per query.
    if result:
        try:
            body = _article(result[0]['url'])
            if body:
                result[0].update(excerpt=body, content_type='BODY')
        except (httpx.HTTPError, ValueError):
            pass
    fetched_at = datetime.now(timezone(timedelta(hours=8))).isoformat()
    for row in result:
        row['fetched_at'] = fetched_at
    return result


def _official_macro():
    index = 'https://www.stats.gov.cn/sj/zxfb/'
    document = _get(index)
    result, attempts = [], 0
    for item in re.findall(r'<li\b[^>]*>(.*?)</li>', document, re.S | re.I):
        anchor = re.search(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', item, re.S | re.I)
        date = re.search(r'<span\b[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</span>', item, re.I)
        if not anchor or not date:
            continue
        url = urljoin(index, anchor.group(1))
        if urlsplit(url).hostname != 'www.stats.gov.cn':
            continue
        attempts += 1
        try:
            body = _article(url)
        except (httpx.HTTPError, ValueError):
            body = ''
        if body:
            result.append({'title': _plain(anchor.group(2)), 'url': url,
                           'published_at': date.group(1), 'excerpt': body,
                           'source_name': '国家统计局', 'source_kind': 'OFFICIAL',
                           'scope': 'macro', 'dimension': 'macro', 'content_type': 'BODY', 'latest_release': True,
                           'fetched_at': datetime.now(timezone(timedelta(hours=8))).isoformat()})
        if attempts == 2:
            break
    return result


def collect_materials(name, now):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    jobs = {executor.submit(_search, name, dimension, now): dimension
            for dimension in EXTERNAL_DIMENSIONS if dimension != 'macro'}
    jobs[executor.submit(_official_macro)] = 'macro'
    _, pending = concurrent.futures.wait(jobs, timeout=15)
    rows, errors = [], []
    # Submission order fixes source IDs independently of network completion order.
    for job, dimension in jobs.items():
        if job in pending:
            job.cancel()
            errors.append({'dimension': dimension, 'reason': 'TIMEOUT'})
        else:
            try:
                rows.extend(job.result())
            except Exception as exc:
                errors.append({'dimension': dimension, 'reason': type(exc).__name__})
    executor.shutdown(wait=False, cancel_futures=True)
    normalized = normalize_materials(rows, now)
    sources = [row for row in normalized if not row['stale']]
    background = [row for row in normalized if row['stale']]
    missing = [dimension for dimension in EXTERNAL_DIMENSIONS
               if not any(row['dimension'] == dimension for row in sources)]
    return {'sources': sources, 'background_sources': background,
            'freshness_policy': {'fundamentals_days': 14, 'events_days': 7,
                                 'related_markets_days': 7, 'macro': 'LATEST_OFFICIAL_RELEASE'},
            'missing_dimensions': missing, 'errors': errors,
            'status': 'MISSING' if not sources else 'PARTIAL' if missing or errors else 'AVAILABLE'}


class PublicCitationError(ValueError):
    """A public citation requires correction against the frozen sources."""


class CitationDimensionError(PublicCitationError):
    """An exact public quote was used outside its frozen evidence dimension."""


def validate_citations(citations, sources):
    if not isinstance(citations, list):
        raise ValueError('citations must be a list')
    by_id = {row['source_id']: row for row in sources if not row.get('stale')}
    for citation in citations:
        if not isinstance(citation, dict):
            raise ValueError('citation must be an object')
        if not isinstance(citation.get('source_id'), str):
            raise ValueError('source_id must be a string')
        source = by_id.get(citation.get('source_id'))
        quote = citation.get('quote')
        if source is None or not isinstance(quote, str) or len(quote.strip()) < 4 or quote not in source.get('excerpt', ''):
            raise PublicCitationError('Unverifiable public source citation: source_id=' + str(citation.get('source_id')))


def _matches_market_evidence(evidence, market):
    try:
        value = float(evidence['value'])
        if isinstance(evidence['value'], bool) or not math.isfinite(value):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    rows = [market.get('quote') or {}]
    for key in ('bars_1m', 'daily_bars_1d', 'bars_60m', 'bars_15m'):
        rows += market.get(key) or []
    for row in rows:
        if (row.get('timestamp') or row.get('bar_time')) != evidence.get('time'):
            continue
        try:
            if math.isclose(float(row[evidence['field']]), value, rel_tol=1e-9, abs_tol=1e-9):
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def validate_result(data, snapshot):
    if not isinstance(data, dict):
        raise ValueError('Future trend result must be an object')
    if any(key in data for key in ('session_chart', 'next_open_forecast')):
        raise ValueError('Future trend cannot contain intraday or next-open outputs')
    data.pop('future_metrics', None)
    if not isinstance(data.get('alternative_scenario'), str) or not data['alternative_scenario'].strip():
        raise ValueError('Expected a nonempty alternative scenario')
    forecasts = data.get('horizon_forecasts')
    frozen = snapshot.get('future_targets', [])
    horizons = [row.get('horizon_days') for row in frozen]
    if (horizons not in ([3, 5], [3, 5, 7, 15])
            or not isinstance(forecasts, list) or len(forecasts) != len(frozen)
            or [row.get('horizon_days') for row in forecasts if isinstance(row, dict)] != horizons):
        raise ValueError('Expected forecasts matching all frozen trading-day horizons')
    for forecast, target in zip(forecasts, frozen):
        if forecast.get('direction') not in {'LONG', 'SHORT', 'NEUTRAL', 'NO_JUDGMENT'}:
            raise ValueError('Invalid horizon direction')
        if forecast.get('trading_day') != target['trading_day']:
            raise ValueError('Forecast trading day differs from frozen target')
        if snapshot.get('quality', {}).get('can_judge') is False and forecast['direction'] != 'NO_JUDGMENT':
            raise ValueError('Insufficient market evidence requires NO_JUDGMENT')
        term_status = horizon_term_status(snapshot, target)
        if term_status != 'WITHIN_RULE_TERM':
            if forecast['direction'] != 'NO_JUDGMENT' or any(
                    forecast.get(key) is not None for key in
                    ('target_price', 'price_range', 'target_range', 'support_levels', 'resistance_levels')):
                raise ValueError('Unverified or expired contract term requires NO_JUDGMENT without price predictions')
            forecast['blocking_reason'] = 'CONTRACT_TERM_' + term_status
        confidence = forecast.get('confidence_score')
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 100:
            raise ValueError('Invalid evidence confidence')
        for field in ('summary', 'entry_condition', 'invalidation_condition'):
            if not isinstance(forecast.get(field), str) or not forecast[field].strip():
                raise ValueError('Missing horizon ' + field)
    if data.get('direction') != forecasts[1]['direction']:
        raise ValueError('Main direction must equal the 5-day forecast')
    if any(row['direction'] != 'NO_JUDGMENT' for row in forecasts):
        evidence = data.get('evidence', [])
        for side in ('LONG', 'SHORT'):
            if len({(row.get('time'), row.get('field')) for row in evidence
                    if isinstance(row, dict) and row.get('side') == side}) < 2:
                raise ValueError('Directional horizon requires balanced verifiable evidence')
        for field in ('support_levels', 'resistance_levels'):
            if (not data.get(field) and 'CROSS_PERIOD_RANGE_UNVERIFIED' not in
                    snapshot.get('quality', {}).get('market_checks', {}).get('warnings', [])):
                raise ValueError('Directional horizon requires price levels')
    dimensions = data.get('dimensions')
    if not isinstance(dimensions, dict) or set(dimensions) != set(DIMENSIONS):
        raise ValueError('Expected all six evidence dimensions')
    materials = snapshot.get('public_materials', {})
    sources = materials.get('sources', [])
    missing = materials.get('missing_dimensions', list(EXTERNAL_DIMENSIONS))
    related = snapshot.get('related_contracts', [])
    related_snapshots = {row['instrument']['symbol']: row for row in snapshot.get('related_market_snapshots', [])}
    market_evidence = dimensions.get('related_markets', {}).get('market_evidence', [])
    if not isinstance(market_evidence, list):
        raise ValueError('关联行情证据必须是数组')
    used_related = set()
    for item in market_evidence:
        if (not isinstance(item, dict) or item.get('instrument') not in related
                or any(not isinstance(item.get(key), str) or not item[key].strip()
                       for key in ('instrument', 'time', 'field', 'reason'))):
            raise ValueError('关联行情证据缺少具体合约、时间、字段或说明')
        reference = related_snapshots.get(item['instrument'])
        if (not reference or reference.get('contract_lifecycle', {}).get('status') == 'UNKNOWN'
                or 'EXPIRED_CONTRACT' in reference.get('quality', {}).get('warnings', [])
                or not _matches_market_evidence(item, reference.get('market', {}))):
            raise ValueError('关联行情证据无法与对应合约快照核对')
        used_related.add(item['instrument'])
    target_symbol = snapshot.get('instrument', {}).get('symbol')
    for item in data.get('evidence', []):
        if item.get('instrument') and target_symbol and item['instrument'] != target_symbol:
            raise ValueError('目标行情证据不能引用关联或其他合约')
    used_dimensions = set()
    for dimension, evidence in dimensions.items():
        if (not isinstance(evidence, dict) or evidence.get('status') not in {'AVAILABLE', 'PARTIAL', 'MISSING'}
                or not isinstance(evidence.get('summary'), str) or not evidence['summary'].strip()):
            raise ValueError('Invalid dimension evidence')
        citations = evidence.get('citations')
        if dimension in EXTERNAL_DIMENSIONS:
            validate_citations(citations, sources)
        elif not isinstance(citations, list):
            raise ValueError('citations must be a list')
        related_numeric = dimension == 'related_markets' and bool(market_evidence)
        if dimension in missing and evidence['status'] != 'MISSING' and not related_numeric:
            raise ValueError('Missing public dimension cannot be available')
        if dimension in EXTERNAL_DIMENSIONS:
            if evidence['status'] != 'MISSING' and not citations and not related_numeric:
                raise ValueError('Public evidence requires source citations')
            cited = {row['source_id'] for row in citations}
            mismatches = [f"{row['source_id']} 属于 {row['dimension']}" for row in sources
                          if row['source_id'] in cited and row['dimension'] != dimension]
            if mismatches:
                raise CitationDimensionError(f"引用维度不匹配：{dimension} 引用了 " + '、'.join(mismatches))
            cited_sources = [row for row in sources if row['source_id'] in cited]
            if dimension != 'related_markets' and any(row.get('reference_contracts') for row in cited_sources):
                raise CitationDimensionError('关联公开资料不能作为目标合约自身的' + dimension + '证据')
            if citations or related_numeric:
                used_dimensions.add(dimension)
            for row in cited_sources:
                used_related.update(code for code in row.get('reference_contracts', []) if code in related)
    technical_fields = {'last_price', 'bid_price', 'ask_price', 'open', 'high', 'low', 'close',
                        'open_price', 'high_price', 'low_price', 'close_price'}
    volume_fields = {'volume', 'turnover', 'open_interest', 'open_interest_change'}
    checks = snapshot.get('quality', {}).get('market_checks', {})
    if 'CROSS_PERIOD_RANGE_UNVERIFIED' in checks.get('warnings', []):
        for outcome in [data] + forecasts:
            if any(outcome.get(key) not in (None, []) for key in
                   ('support_levels', 'resistance_levels', 'target_price', 'price_range', 'target_range')):
                raise ValueError('行情跨周期范围尚未核实，禁止输出精确价位')
    for item in data.get('evidence', []):
        for key in ('daily_bars_1d', 'bars_60m', 'bars_15m'):
            bar = next((b for b in snapshot.get('market', {}).get(key, [])
                        if b.get('bar_time') == item.get('time')), None)
            if bar and item.get('field') in volume_fields and not bar.get('usage_limits', {}).get('volume_oi', True):
                raise ValueError('受限行情不能作为量仓推理证据')
        if not _matches_market_evidence(item, snapshot.get('market', {})):
            continue
        if item.get('field') in technical_fields:
            used_dimensions.add('technical')
        elif item.get('field') in volume_fields:
            used_dimensions.add('volume_oi')
    if market_evidence:
        used_dimensions.add('related_markets')
    data['used_dimensions'] = [value for value in DIMENSIONS if value in used_dimensions]
    data['used_related_contracts'] = [value for value in related if value in used_related]
    # The main score is a compatibility field, derived from the validated 5-day result.
    data['confidence_score'] = forecasts[1]['confidence_score']
    return data
