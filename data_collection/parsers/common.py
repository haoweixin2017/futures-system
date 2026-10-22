import json
import math
from datetime import datetime

from bs4 import BeautifulSoup


def at(value, path):
    for key in path.split('.'):
        value = value[key]
    return value


def number(value):
    if value is None or isinstance(value, bool) or str(value).strip() == '':
        raise ValueError('QUANTITY_MISSING')
    try:
        result = float(str(value).replace(',', '').strip())
    except (ValueError, TypeError):
        raise ValueError('INVALID_NUMBER') from None
    if not math.isfinite(result):
        raise ValueError('INVALID_NUMBER')
    return result


def hierarchy(rows):
    if not rows or len({r['detail_id'] for r in rows}) != len(rows):
        raise ValueError('MISSING_OR_DUPLICATE_DETAILS')
    totals = [r for r in rows if r['row_type'] == 'TOTAL']
    if len(totals) != 1:
        raise ValueError('MISSING_OR_DUPLICATE_TOTAL')
    groups = {r['detail_id']: r for r in rows if r['row_type'] == 'GROUP'}
    details = [r for r in rows if r['row_type'] == 'DETAIL']
    if not details or any(r.get('parent_id') and r['parent_id'] not in groups for r in details):
        raise ValueError('INVALID_DETAIL_HIERARCHY')
    top = list(groups.values()) + [r for r in details if not r.get('parent_id')]
    for field in ('warr_yesterday', 'warr_today', 'warr_chg'):
        if abs(sum(r[field] for r in top) - totals[0][field]) > 1e-8:
            raise ValueError('SUBTOTAL_MISMATCH')
        for identity, group in groups.items():
            children = [r for r in details if r.get('parent_id') == identity]
            if children and abs(sum(r[field] for r in children) - group[field]) > 1e-8:
                raise ValueError('GROUP_MISMATCH')
    for row in rows:
        if abs(row['warr_today'] - row['warr_yesterday'] - row['warr_chg']) > 1e-8:
            raise ValueError('ROW_CHANGE_MISMATCH')
    return rows


def parse_html(text, config, trade_date, variety):
    parser = config['parser']
    soup = BeautifulSoup(text, 'html.parser')
    date_node = soup.select_one(parser['date_selector'])
    table = soup.select_one(parser['table_selector'])
    if not date_node or not table:
        raise ValueError('SELECTOR_INVALID')
    fmt = parser.get('html_date_format', parser['date_format'])
    if datetime.strptime(date_node.get_text(strip=True), fmt).date().isoformat() != trade_date:
        raise ValueError('UPDATE_NOT_READY_DATE_MISMATCH')
    fields = parser.get('html_fields', parser['fields'])
    required = ('variety', 'detail_id', 'warehouse', 'warehouse_type', 'grade', 'warr_yesterday', 'warr_today', 'warr_chg', 'unit_raw', 'row_type', 'is_converted_warrant')
    if any(key not in fields for key in required):
        raise ValueError('PARSER_FIELDS_MISSING')
    rows = []
    for element in table.select(parser['row_selector']):
        cells = element.select(parser['cell_selector'])
        try:
            raw = {key: cells[index].get_text(' ', strip=True) for key, index in fields.items()}
        except IndexError:
            raise ValueError('COLUMN_MISSING') from None
        if raw['variety'].upper() != variety.upper():
            continue
        kind = 'TOTAL' if raw['row_type'] == parser['total_value'] else 'DETAIL' if raw['row_type'] == parser.get('html_detail_value', parser['detail_value']) else None
        if not kind or not raw['detail_id'] or not raw['unit_raw']:
            raise ValueError('UNKNOWN_ROW_TYPE_OR_ID')
        warehouse_type = parser['warehouse_types'].get(raw['warehouse_type'], '')
        if kind == 'DETAIL' and raw['warehouse_type'] and not warehouse_type:
            raise ValueError('UNKNOWN_WAREHOUSE_TYPE')
        rows.append(dict(raw, trade_date=trade_date, variety=variety, row_type=kind, parent_id='', warehouse_type=warehouse_type,
                         raw_record=json.dumps(raw, ensure_ascii=False),
                         is_converted_warrant=int(raw['is_converted_warrant'] in parser['converted_values']),
                         **{key: number(raw[key]) for key in ('warr_yesterday', 'warr_today', 'warr_chg')}))
    if len({r['unit_raw'] for r in rows}) > 1:
        raise ValueError('MIXED_UNITS')
    return hierarchy(rows)
