import unittest

from tests import test_bootstrap
from backend.ai_analysis import web_research as web


class ExtractionCredibilityTests(unittest.TestCase):
    def checked(self, **fields):
        fact = dict(metric='库存', value=100, unit='万吨', period='2026年9月',
                    region='华东', quote='2026年9月华东燃料油库存为100万吨。')
        fact.update(fields)
        return web.validate_extraction({'material_id': 'm1', 'facts': [fact]},
                                      {'material_id': 'm1', 'body': fact['quote']})

    def test_value_unit_period_region_must_match_quote(self):
        self.assertEqual(len(self.checked()['facts']), 1)
        for fields in ({'value': 999}, {'unit': '吨'}, {'period': '2026年10月'},
                       {'region': '华北'}, {'value': 10}):
            with self.subTest(fields=fields):
                result = self.checked(**fields)
                self.assertEqual(result['facts'], [])
                self.assertTrue(result['unverified'])

    def test_formatted_numbers_and_chinese_dates_match_without_conversion(self):
        result = self.checked(value='1,000.5', quote='2026年9月华东燃料油库存为1,000.5万吨。')
        self.assertEqual(len(result['facts']), 1)
        result = self.checked(value='2026-10-15', unit=None, period=None, region=None,
                              quote='自2026年10月15日起实施。')
        self.assertEqual(len(result['facts']), 1)

    def test_vague_fundamentals_do_not_close_coverage_gap(self):
        self.assertFalse(web._fundamental_detail({'body': '燃料油库存下降，供应减少。'}, 'FU2611'))
        self.assertTrue(web._fundamental_detail(
            {'body': '2026年9月燃料油库存为100万吨。'}, 'FU2611'))

    def test_positive_value_cannot_match_negative_quote(self):
        self.assertEqual(self.checked(quote='2026年9月华东燃料油库存变化为-100万吨。')['facts'], [])

    def test_other_product_sentence_cannot_fill_target_demand(self):
        rows = [{'body': '燃料油库存100万吨。沥青道路施工需求增长10%。', 'data_period': '2026年9月'}]
        coverage = web.fundamental_coverage(rows, 'FU2611')
        self.assertEqual([r['status'] for r in coverage], ['AVAILABLE', 'MISSING', 'MISSING'])
