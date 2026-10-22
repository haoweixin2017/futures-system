import copy
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def settings(exchange):
    return yaml.safe_load((ROOT / 'config.example.yaml').read_text(encoding='utf-8'))['exchanges'][exchange]


def test_config_rejects_short_interval_and_live_sample_calendar(tmp_path):
    from data_collection.config import load_config
    raw = yaml.safe_load((ROOT / 'config.example.yaml').read_text(encoding='utf-8'))
    raw['network']['interval_seconds'] = 7
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(raw), encoding='utf-8')
    with pytest.raises(ValueError, match='interval'):
        load_config(path)


def test_czce_fixture_preserves_grade_total_and_unit():
    from data_collection.parsers.czce import parse
    rows = parse((ROOT / 'tests/fixtures/czce.html').read_text(encoding='utf-8'), settings('CZCE'), '2026-09-30', 'SR')
    assert len(rows) == 3
    assert rows[0]['grade'] == '一级'
    assert rows[0]['warehouse_type'] == '厂库'
    assert rows[-1]['row_type'] == 'TOTAL'
    assert rows[-1]['warr_today'] == 120
    assert rows[0]['is_converted_warrant'] == 1


@pytest.mark.parametrize('change', ['selector', 'date', 'number', 'missing_total'])
def test_czce_structure_or_date_failure_is_not_empty_success(change):
    from data_collection.parsers.czce import parse
    cfg = settings('CZCE')
    html = (ROOT / 'tests/fixtures/czce.html').read_text(encoding='utf-8')
    if change == 'selector': cfg['parser']['table_selector'] = '#missing'
    if change == 'date': html = html.replace('2026-09-30', '2026-09-29')
    if change == 'number': html = html.replace('>70<', '>--<')
    if change == 'missing_total': html = html.replace('TOTAL', 'DETAIL')
    with pytest.raises(ValueError):
        parse(html, cfg, '2026-09-30', 'SR')


def test_dce_real_json_preserves_group_without_double_count():
    from data_collection.parsers.dce import parse
    payload = (ROOT / 'tests/fixtures/dce.json').read_text(encoding='utf-8')
    rows = parse(payload, settings('DCE'), '2026-09-30', 'LH')
    assert [r['row_type'] for r in rows] == ['GROUP', 'DETAIL', 'DETAIL', 'TOTAL']
    assert rows[-1]['warr_yesterday'] == 110
    assert rows[-1]['warr_chg'] == -110
    assert rows[1]['parent_id'] == '318'


def test_dce_html_fallback_keeps_explicit_zero():
    from data_collection.parsers.dce import parse
    cfg = settings('DCE'); cfg['parser']['format'] = 'html'
    rows = parse((ROOT / 'tests/fixtures/dce.html').read_text(encoding='utf-8'), cfg, '2026-09-30', 'JD')
    assert rows[-1]['warr_today'] == 0
    assert rows[-1]['warr_chg'] == -204


def test_dce_missing_product_rejected():
    from data_collection.parsers.dce import parse
    with pytest.raises(ValueError):
        parse((ROOT / 'tests/fixtures/dce.json').read_text(encoding='utf-8'), settings('DCE'), '2026-09-30', 'UNKNOWN')


def test_dce_unmapped_original_fields_are_preserved():
    from data_collection.parsers.dce import parse
    payload=json.loads((ROOT/'tests/fixtures/dce.json').read_text(encoding='utf-8'))
    next(r for r in payload['data']['entityList'] if r.get('varietyOrder')=='lh')['customGrade']='来源等级'
    rows=parse(json.dumps(payload),settings('DCE'),'2026-09-30','LH')
    assert json.loads(rows[0]['raw_record'])['customGrade']=='来源等级'
    assert rows[0]['is_converted_warrant'] is None


def test_unreliable_ton_conversion_keeps_raw_and_null():
    from data_collection.validators import normalize_units
    rows = [dict(variety='SC', unit_raw='桶', warr_yesterday=20, warr_today=30, warr_chg=10)]
    normalized = normalize_units(rows, {'SC': {'conversion_factor': None, 'conversion_basis': ''}})
    assert normalized[0]['warr_today'] == 30
    assert normalized[0]['warr_today_ton'] is None
    converted = normalize_units(rows, {'SC': {'conversion_factor': 0.1, 'conversion_basis': 'fixture only', 'unit_raw': '桶'}})
    assert converted[0]['warr_today_ton'] == 3


def test_validation_uses_total_and_checks_cross_day_reference_and_sigma():
    from data_collection.validators import validate
    rows = [dict(row_type='TOTAL', detail_id='TOTAL', warr_yesterday=110, warr_today=120, warr_chg=10, unit_raw='吨'),
            dict(row_type='DETAIL', detail_id='A', warr_yesterday=110, warr_today=120, warr_chg=10, unit_raw='吨')]
    config = dict(tolerance=0, min_history=3, sigma=3, require_previous=False, require_reference=False)
    result = validate(rows, config, previous={'warr_today': 110, 'unit_raw': '吨'}, reference={'value': 120, 'unit': '吨'}, history=[100, 110, 115])
    assert result['ok']
    assert result['total'] == 120
    result = validate(rows, config, previous={'warr_today': 100, 'unit_raw': '吨'}, reference={'value': 200, 'unit': '吨'}, history=[1, 2, 3])
    assert set(result['errors']) >= {'CROSS_DAY_MISMATCH', 'REFERENCE_MISMATCH', 'SIGMA_OUTLIER'}


def test_validation_missing_reference_is_unverified_and_negative_rejected():
    from data_collection.validators import validate
    rows = [dict(row_type='TOTAL', detail_id='TOTAL', warr_yesterday=0, warr_today=-1, warr_chg=-1, unit_raw='吨')]
    result = validate(rows, dict(tolerance=0, min_history=3, sigma=3))
    assert not result['ok']
    assert 'NEGATIVE_QUANTITY' in result['errors']
    assert result['checks']['reference'] == 'UNVERIFIED'


def test_storage_idempotent_market_and_receipt_arrival_orders(tmp_path):
    from data_collection.storage import Store
    for order in (0, 1):
        with Store(tmp_path / f'{order}.sqlite3') as db:
            receipt = dict(trade_date='2026-09-30', exchange='CZCE', variety='SR', detail_id='TOTAL', row_type='TOTAL',
                           warehouse='', warehouse_type='', grade='', unit_raw='吨', warr_yesterday=110, warr_today=120, warr_chg=10,
                           source_channel='fixture', receipt_source='fixture:czce', is_converted_warrant=0)
            market = dict(trade_date='2026-09-30', exchange='CZCE', variety='SR', main_contract='SR701', settle=5700, oi=800, volume=200, market_source='fixture:market')
            actions = [lambda: db.upsert_receipts([receipt]), lambda: db.upsert_market([market])]
            for action in actions[::1 if order == 0 else -1]: action()
            db.upsert_receipts([receipt]); db.upsert_market([market])
            rows = db.rows()
            assert len(rows) == 2
            r = next(r for r in rows if r['row_type'] == 'TOTAL')
            assert (r['warr_today'], r['settle'], r['main_contract']) == (120, 5700, 'SR701')
            assert r['receipt_source'] and r['market_source']


def test_contract_mapping_uses_context_and_validates_month():
    from data_collection.config import contract_identity
    assert contract_identity('SR601', 'CZCE', '2025-09-30') == ('SR', 2026, 1)
    assert contract_identity('m2601', 'DCE', '2025-09-30') == ('M', 2026, 1)
    with pytest.raises(ValueError): contract_identity('SR613', 'CZCE', '2025-09-30')


def test_calendar_never_silently_extrapolates():
    from data_collection.config import TradingCalendar
    cal = TradingCalendar({'start': '2026-09-29', 'end': '2026-10-08', 'dates': ['2026-09-29', '2026-09-30', '2026-10-08'], 'fixture_only': True})
    assert cal.days('2026-09-30', '2026-10-08') == ['2026-09-30', '2026-10-08']
    assert cal.previous('2026-09-30') == '2026-09-29'
    with pytest.raises(ValueError): cal.days('2026-01-01', '2026-09-30')
