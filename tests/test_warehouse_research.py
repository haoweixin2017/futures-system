"""Official warehouse receipts: saved real pages, never live network in tests."""
import hashlib
import re
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tests import test_bootstrap
from backend.ai_analysis import fixed_research as fixed, web_research as web


FIXTURES = Path(__file__).parent / 'fixtures' / 'warehouse'
PRODUCTS = {'FU': ('shfe_fu', 550, '吨'), 'BU': ('shfe_bu', 37270, '吨'),
            'SC': ('ine_sc', 2961000, '桶'), 'LU': ('ine_lu', 35000, '吨')}


class WarehouseResearchTests(unittest.TestCase):
    def timeout_case(self, product, stage, failures=1, budget=None, error=None):
        source = next(s for s in fixed.FIXED_SOURCES if s['source_id'] == 'SHFE_WAREHOUSE_DAILY')
        index = (FIXTURES / 'index.json').read_text(encoding='utf-8')
        document = (FIXTURES / (PRODUCTS[product][0] + '.html')).read_text(encoding='utf-8')
        budget = budget or fixed.Budget()
        attempts = {'INDEX': 0, 'BODY': 0}
        self.last_attempts = attempts
        def get(url, timeout, professional_only=False):
            self.assertTrue(professional_only)
            self.assertLessEqual(timeout, budget.time_left() + 0.1)
            current = 'INDEX' if url == source['entry'] else 'BODY'
            attempts[current] += 1
            if current == stage and attempts[current] <= failures:
                raise error or TimeoutError()
            return (index if current == 'INDEX' else document), url
        # collect_fixed reserves the first index and body attempts before invoking the parser.
        budget.try_consume('index_checks')
        budget.try_consume('reads')
        with patch.object(web, 'public_get', side_effect=get):
            result = fixed._parse_shfe(source, 'product:' + product, product, budget, 'sim', {})
        return result, attempts, budget

    def test_body_timeout_retries_once_and_recovers_fu_and_sc(self):
        for product in ('FU', 'SC'):
            with self.subTest(product=product):
                result, attempts, budget = self.timeout_case(product, 'BODY')
                self.assertEqual(result['rows'][0]['warehouse_quantity'], PRODUCTS[product][1])
                self.assertEqual(attempts, {'INDEX': 1, 'BODY': 2})
                self.assertEqual(budget.used['reads'], 2)
                self.assertEqual([r['status'] for r in result['check']['requests']], ['COMPLETED', 'TIMEOUT', 'COMPLETED'])

    def test_index_timeout_retries_without_spending_another_body_read(self):
        result, attempts, budget = self.timeout_case('FU', 'INDEX')
        self.assertEqual(attempts, {'INDEX': 2, 'BODY': 1})
        self.assertEqual((budget.used['index_checks'], budget.used['reads']), (2, 1))
        self.assertEqual(result['rows'][0]['warehouse_quantity'], 550)

    def test_repeated_timeout_names_failed_stage_and_stops_after_two_requests(self):
        for stage in ('INDEX', 'BODY'):
            with self.subTest(stage=stage):
                with self.assertRaisesRegex(ValueError, 'WAREHOUSE_' + stage + '.*TimeoutError.*2'):
                    self.timeout_case('FU', stage, failures=3)
                self.assertEqual(self.last_attempts[stage], 2)

    def test_exhausted_body_read_budget_does_not_retry(self):
        with self.assertRaisesRegex(ValueError, 'WAREHOUSE_BODY.*reads.*预算'):
            self.timeout_case('FU', 'BODY', budget=fixed.Budget(reads=1))
        self.assertEqual(self.last_attempts['BODY'], 1)

    def test_exhausted_index_budget_does_not_retry(self):
        with self.assertRaisesRegex(ValueError, 'WAREHOUSE_INDEX.*index_checks.*预算'):
            self.timeout_case('FU', 'INDEX', budget=fixed.Budget(index_checks=1))
        self.assertEqual(self.last_attempts['INDEX'], 1)

    def test_httpx_connect_timeout_recovers(self):
        result, attempts, _ = self.timeout_case('SC', 'BODY', error=web.httpx.ConnectTimeout('connect'))
        self.assertEqual(attempts['BODY'], 2)
        self.assertEqual(result['check']['requests'][1]['error_type'], 'ConnectTimeout')

    def test_expired_time_budget_stops_network(self):
        with patch.object(web, 'public_get') as get:
            with self.assertRaisesRegex(ValueError, 'WAREHOUSE_INDEX.*时间预算'):
                fixed._warehouse_get('https://www.shfe.com.cn/', fixed.Budget(seconds=0), 'INDEX', [])
        get.assert_not_called()

    def test_http_rejection_is_not_retried(self):
        with self.assertRaisesRegex(ValueError, 'HTTP 412'):
            self.timeout_case('FU', 'BODY', error=ValueError('HTTP 412'))
        self.assertEqual(self.last_attempts['BODY'], 1)

    def parse(self, product, document=None):
        name = PRODUCTS[product][0]
        document = document if document is not None else (FIXTURES / (name + '.html')).read_text(encoding='utf-8')
        index = (FIXTURES / 'index.json').read_text(encoding='utf-8')
        b = fixed.Budget()
        def get(url, timeout, professional_only=False):
            self.assertTrue(professional_only)
            if 'eia.gov' in url:
                from tests.test_fixed_research import EIA_SUMMARY
                return EIA_SUMMARY, url
            return (index if url.endswith('stock_data_list.dat') else document), url
        with patch.object(web, 'public_get', side_effect=get):
            result = fixed.collect_fixed({'market': {'snapshot_time': '2026-10-05T12:00:00'}},
                                         'product:' + product, b)
        return result, b

    def test_real_product_pages_preserve_date_quantity_unit_and_exchange(self):
        for product, (_, quantity, unit) in PRODUCTS.items():
            with self.subTest(product=product):
                result, b = self.parse(product)
                rows = [r for r in result['sources'] if r.get('source_entry') == 'SHFE_WAREHOUSE_DAILY']
                self.assertEqual(len(rows), 1, result['errors'])
                row = rows[0]
                self.assertEqual(row['warehouse_quantity'], quantity)
                self.assertEqual(row['warehouse_unit'], unit)
                self.assertEqual(row['data_period'], '2026-09-30')
                self.assertEqual(row['published_at'], 'UNKNOWN')
                self.assertEqual(row['expected_update_by'], '2026-10-08T18:00:00')
                self.assertEqual(row['verification_ttl_hours'], 24)
                self.assertEqual(row['exchange'], 'INE' if product in ('SC', 'LU') else 'SHFE')
                self.assertEqual((row['dimension'], row['source_kind']), ('fundamentals', 'OFFICIAL'))
                self.assertIn('/' + product.lower() + '.html', row['url'])
                self.assertIn('不代表全部库存', row['body'])
                self.assertEqual(b.used['reads'], 2)  # EIA and warehouse body

    def test_release_check_hash_matches_persisted_body(self):
        result, _ = self.parse('FU')
        row = next(r for r in result['sources'] if r.get('source_entry') == 'SHFE_WAREHOUSE_DAILY')
        check = next(c for c in result['latest_release_checks'] if c['source_id'] == 'SHFE_WAREHOUSE_DAILY')
        self.assertEqual(check['content_hash'], hashlib.sha256(row['body'].encode('utf-8')).hexdigest())

    def test_normalization_preserves_warehouse_measurements_and_document_hash(self):
        result, _ = self.parse('SC')
        row = next(r for r in result['sources'] if r.get('source_entry') == 'SHFE_WAREHOUSE_DAILY')
        normalized = web.normalize_with_provenance([row], datetime(2026, 10, 5, 12),
                                                   allowed_hosts={'www.shfe.com.cn'})[0]
        for field in ('warehouse_quantity', 'warehouse_unit', 'warehouse_totals', 'exchange', 'document_hash'):
            self.assertEqual(normalized.get(field), row[field], field)

    def test_missing_product_table_is_failure_without_fabricated_row(self):
        for product, (name, _, _) in PRODUCTS.items():
            doc = (FIXTURES / (name + '.html')).read_text(encoding='utf-8')
            doc = re.sub(r'<table class="el-table_table">.*?</table>', '', doc, flags=re.S)
            result, _ = self.parse(product, doc)
            self.assertFalse(any(r.get('source_entry') == 'SHFE_WAREHOUSE_DAILY' for r in result['sources']))
            self.assertTrue(any(e['source_id'] == 'SHFE_WAREHOUSE_DAILY' for e in result['errors']))

    def test_changed_header_or_missing_date_rejects_each_real_fixture(self):
        for product, (name, _, _) in PRODUCTS.items():
            doc = (FIXTURES / (name + '.html')).read_text(encoding='utf-8')
            for changed in (doc.replace('期货</div>', '未知字段</div>').replace('本日数量</div>', '未知字段</div>'),
                            doc.replace('2026-09-30', '日期缺失')):
                with self.subTest(product=product):
                    result, _ = self.parse(product, changed)
                    self.assertFalse(any(r.get('source_entry') == 'SHFE_WAREHOUSE_DAILY' for r in result['sources']))
                    self.assertTrue(any(e['source_id'] == 'SHFE_WAREHOUSE_DAILY' for e in result['errors']))

    def test_zero_read_budget_prevents_warehouse_network(self):
        with patch.object(web, 'public_get') as get:
            result = fixed.collect_fixed({}, 'product:FU', fixed.Budget(reads=0))
        get.assert_not_called()
        check = next(s for s in result['sources_checked'] if s['source_id'] == 'SHFE_WAREHOUSE_DAILY')
        self.assertEqual(check['status'], 'SKIPPED_BUDGET')

    def test_warehouse_is_not_bu_general_inventory_coverage(self):
        result, _ = self.parse('BU')
        rows = [r for r in result['sources'] if r.get('source_entry') == 'SHFE_WAREHOUSE_DAILY']
        self.assertTrue(rows)
        self.assertTrue(all(item['status'] == 'MISSING' for item in web.fundamental_coverage(rows, 'BU2611')))

    def test_missing_or_invalid_aggregate_is_rejected(self):
        doc = (FIXTURES / 'shfe_fu.html').read_text(encoding='utf-8')
        for changed in (doc.replace('保税商品总计', '未核实总量'),
                        doc.replace('<td>550</td>', '<td>未知</td>')):
            result, _ = self.parse('FU', changed)
            self.assertFalse(any(r.get('source_entry') == 'SHFE_WAREHOUSE_DAILY' for r in result['sources']))

    def test_report_date_must_match_directory_date(self):
        doc = (FIXTURES / 'shfe_fu.html').read_text(encoding='utf-8').replace('2026-09-30', '2026-09-29')
        result, _ = self.parse('FU', doc)
        self.assertFalse(any(r.get('source_entry') == 'SHFE_WAREHOUSE_DAILY' for r in result['sources']))

    def test_future_only_index_is_not_fetched(self):
        source = next(s for s in fixed.FIXED_SOURCES if s['source_id'] == 'SHFE_WAREHOUSE_DAILY')
        with patch.object(web, 'public_get', return_value=('{"dataList":"[20261009]"}', source['entry'])) as get:
            with self.assertRaisesRegex(ValueError, 'no eligible'):
                fixed._parse_shfe(source, 'product:FU', 'FU', fixed.Budget(), '',
                                  {'market': {'snapshot_time': '2026-10-05T12:00:00'}})
        self.assertEqual(get.call_count, 1)

