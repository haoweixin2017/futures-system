import time
import unittest
from unittest.mock import patch

from tests import test_bootstrap
from backend.ai_analysis import web_research as web, research_cache as cache


class SourceDistributionTests(unittest.TestCase):
    def test_product_index_is_used_without_claiming_keyword_search(self):
        for product in ('FU', 'BU'):
            with patch.object(web, 'public_get', return_value=('', 'https://finance.sina.com.cn/')) as get:
                web._search_sina_index('库存', 'fundamentals', time.monotonic()+5, product+'2611')
            self.assertEqual(get.call_args.args[0], 'https://finance.sina.com.cn/money/future/'+product+'/')

    def test_price_and_qualitative_demand_do_not_close_metric_gap(self):
        rows = [dict(body='2026年9月燃料油船燃价格4500元，需求旺盛。燃料油供应偏紧，价格4500元。', data_period='2026年9月')]
        self.assertEqual([x['status'] for x in web.fundamental_coverage(rows, 'FU2611')], ['MISSING']*3)
        rows[0]['body'] = '燃料油船燃需求在价格上涨10%的背景下减弱。'
        self.assertEqual([x['status'] for x in web.fundamental_coverage(rows, 'FU2611')], ['MISSING']*3)
        warehouse = dict(body='燃料油仓单数量550吨。', data_period='2026-09-30', source_entry='SHFE_WAREHOUSE_DAILY')
        self.assertEqual([x['status'] for x in web.fundamental_coverage([warehouse], 'FU2611')], ['MISSING']*3)

    def test_explicit_period_is_local_to_metric_sentence(self):
        row = dict(body='沥青库存20万吨。本期（20260918-0924）改性沥青对沥青周度消费量5.46万吨，道路施工需求增长10%。', data_period='UNKNOWN')
        self.assertEqual([x['status'] for x in web.fundamental_coverage([row], 'BU2611')], ['MISSING', 'MISSING', 'AVAILABLE'])
        row['body'] = row['body'].replace('20260918-0924', '20260931-0932')
        self.assertEqual([x['status'] for x in web.fundamental_coverage([row], 'BU2611')], ['MISSING']*3)

    def test_source_share_aggregates_eastmoney_and_empty_is_unknown(self):
        rows = [{'url': 'https://finance.eastmoney.com/a/1'}, {'url': 'https://futures.eastmoney.com/a/2'}, {'url': 'https://finance.sina.com.cn/a/3'}]
        stats = cache.source_distribution(rows)
        self.assertEqual(stats['eastmoney_percent'], 66.67)
        self.assertEqual(stats['total'], 3)
        self.assertIsNone(cache.source_distribution([])['eastmoney_percent'])

    def test_cross_domain_identical_body_is_one_reprint_candidate(self):
        body = '燃料油市场供应库存报告' * 20
        rows = [{'url':'https://finance.eastmoney.com/a/1','body':body},
                {'url':'https://finance.sina.com.cn/a/2','body':body+'\n'},
                {'url':'https://example.com/a/3'}]
        result = cache.source_distribution(rows)['content_clusters']
        self.assertEqual(result['known_content_clusters'],1)
        self.assertEqual(result['unknown_content_documents'],1)
        self.assertEqual(result['cross_domain_reprint_candidates'],1)
        self.assertEqual(result['origin_status'],'UNVERIFIED')

    def test_real_report_periods_are_preserved_without_inventing_year(self):
        rows = [dict(body='截至9月23日当周，新加坡燃料油库存下降36.6万桶至1977.8万桶。9月以来富查伊拉燃料油进口量平均为每日12.6万桶。', data_period='UNKNOWN')]
        coverage = web.fundamental_coverage(rows, 'FU2611')
        self.assertEqual([x['status'] for x in coverage], ['AVAILABLE', 'MISSING', 'MISSING'])
        self.assertEqual(coverage[0]['evidence'][0]['data_period'], '9月23日当周')
        self.assertEqual(coverage[1]['candidate_evidence'][0]['data_period'], '9月以来')
        self.assertEqual(coverage[1]['detail_status'], 'SCOPE_MISMATCH')
        self.assertEqual([x['status'] for x in web.fundamental_coverage([dict(body='本期（20260918-0924）改性沥青企业产能利用率为10.2%。', data_period='UNKNOWN')], 'BU2611')], ['MISSING', 'MISSING', 'MISSING'])
        self.assertEqual([x['status'] for x in web.fundamental_coverage([dict(body='本期（20260918-0924）沥青炼厂产能利用率为23.4%。', data_period='UNKNOWN')], 'BU2611')], ['AVAILABLE', 'MISSING', 'MISSING'])

    def test_metric_unknown_period_and_scope_are_not_available(self):
        rows = [dict(body='沥青炼厂开工率23.4%。沥青产量38.2万吨。第39周沥青库存20万吨。', data_period='第39周')]
        items = web.fundamental_coverage(rows, 'BU2611')
        self.assertEqual(items[0]['detail_status'], 'PERIOD_UNKNOWN')
        self.assertEqual(items[1]['detail_status'], 'VALID')
        foreign = dict(body='9月以来富查伊拉燃料油进口量12.6万桶。', data_period='UNKNOWN')
        self.assertEqual(web.fundamental_coverage([foreign], 'FU2611')[1]['detail_status'], 'SCOPE_MISMATCH')
        domestic = dict(body='9月以来中国燃料油进口量120万吨。', data_period='UNKNOWN')
        self.assertEqual(web.fundamental_coverage([domestic], 'FU2611')[1]['detail_status'], 'VALID')
        domestic['stale'] = True
        self.assertEqual(web.fundamental_coverage([domestic], 'FU2611')[1]['detail_status'], 'STALE')

    def test_registered_fuel_index_can_discover_heavy_distillate_inventory(self):
        page = '<a href="https://finance.sina.com.cn/stock/usstock/c/2026-10-01/doc-test.shtml">富查伊拉成品油库存一周下降20%，重质馏分油回落三成</a>'
        with patch.object(web, 'public_get', return_value=(page, 'https://finance.sina.com.cn/money/future/FU/')):
            self.assertEqual(len(web._search_sina_index('燃料油 库存', 'fundamentals', time.monotonic()+5, 'FU')), 1)

    def test_supply_reports_get_read_slots_before_daily_price_quotes(self):
        rows = [dict(url='https://finance.sina.com.cn/money/future/2026-10-06/doc-price.shtml', title='燃料油基准价格', dimension='fundamentals'),
                dict(url='https://finance.sina.com.cn/money/future/2026-10-01/doc-stock.shtml', title='燃料油进口供应库存', dimension='fundamentals'),
                dict(url='https://finance.sina.com.cn/money/future/2026-09-30/doc-month.shtml', title='9月份国内船燃市场行情大幅上涨', dimension='fundamentals')]
        with patch.object(web, '_search_bing', return_value=[]), patch.object(web, '_search_sina', return_value=rows), patch.object(web, '_search_eastmoney', return_value=[]):
            result = web.search_public_web('燃料油 库存', 'fundamentals', instrument='FU')
        self.assertEqual(result[0]['url'], rows[1]['url'])
        self.assertEqual(result[1]['url'], rows[2]['url'])

    def test_distribution_is_owner_scoped_and_latest_url_deduplicated(self):
        from tests.test_scheduled_research import ScheduledResearchTests, evidence
        fixture = ScheduledResearchTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.save()
        fixture.complete(rows=[evidence()])
        fixture.complete(rows=[evidence(body='燃料油库存100万吨。')])
        stats = cache.get_source_distribution('research-a')
        self.assertEqual(stats['historical_versions']['total'], 2)
        self.assertEqual(stats['latest_stored_documents']['total'], 1)
        self.assertEqual(cache.get_source_distribution('another-owner')['historical_versions']['total'], 0)
        fixture.complete(rows=[evidence(body='燃料油库存100万吨。')])
        stats = cache.get_source_distribution('research-a')
        self.assertIsNone(stats['recent_jobs'][0]['eastmoney_percent'])

    def test_freeze_keeps_verified_inventory_before_newer_price_quotes(self):
        from datetime import datetime
        from tests.test_scheduled_research import ScheduledResearchTests, evidence
        fixture = ScheduledResearchTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.save()
        rows = [evidence(body='截至9月23日当周，燃料油库存1977.8万桶。', published='2026-09-30', url='https://finance.sina.com.cn/report')]
        rows += [evidence(body='燃料油基准价格'+str(4500+i)+'元/吨。', title='燃料油报价'+str(i), published='2026-10-02', url='https://finance.sina.com.cn/price'+str(i)) for i in range(6)]
        fixture.complete(rows=rows)
        result = cache.freeze_materials('research-a', 'FU2611', datetime(2026,10,2,19))
        self.assertEqual(result['fundamental_coverage'][0]['status'], 'AVAILABLE')
        self.assertTrue(any(x['url'].endswith('/report') for x in result['sources']))
        self.assertLessEqual(len([x for x in result['sources'] if x['dimension']=='fundamentals']), 6)


if __name__ == '__main__':
    unittest.main()
