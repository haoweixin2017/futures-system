import json
from datetime import datetime

from .common import at, hierarchy, number, parse_html


def parse(text, config, trade_date, variety):
    parser = config['parser']
    if parser['format'] == 'html':
        return parse_html(text, config, trade_date, variety)
    try:
        payload = json.loads(text)
        if at(payload, parser['success_path']) != parser['success_value']:
            raise ValueError('DCE_BUSINESS_ERROR')
        entities = at(payload, parser['records_path'])
        if not isinstance(entities, list):
            raise ValueError('DCE_INVALID_RECORDS')
        fields = parser['fields']
        rows = []
        for raw in entities:
            if (raw[fields['variety']] or '').upper() != variety.upper():
                continue
            row = {key: raw.get(field) for key, field in fields.items()}
            if datetime.strptime(row['trade_date'], parser['date_format']).date().isoformat() != trade_date:
                raise ValueError('UPDATE_NOT_READY_DATE_MISMATCH')
            detail, parent = row['detail_id'], row['parent_id']
            hierarchy_type = row['warehouse_type']
            if detail is None and parent is None:
                kind, detail = 'TOTAL', 'TOTAL'
            elif hierarchy_type == parser['group_value'] and detail == parent:
                kind, parent = 'GROUP', ''
            elif hierarchy_type == parser['detail_value'] and detail:
                kind = 'DETAIL'
            else:
                raise ValueError('DCE_UNKNOWN_HIERARCHY')
            rows.append(dict(row, trade_date=trade_date, variety=variety, detail_id=detail, parent_id=parent or '', row_type=kind,
                             raw_record=json.dumps(raw, ensure_ascii=False),
                             warehouse=row['warehouse'] or '', grade=row.get('grade') or '',
                             warehouse_type=parser['warehouse_types'].get(hierarchy_type, ''), unit_raw=parser['unit_raw'],
                             is_converted_warrant=int(bool(raw[parser['converted_field']])) if parser.get('converted_field') in raw else None,
                             **{k: number(row[k]) for k in ('warr_yesterday', 'warr_today', 'warr_chg')}))
        return hierarchy(rows)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('DCE_SCHEMA_INVALID') from None
