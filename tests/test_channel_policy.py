"""Channel shutdown must stop automatic requests before any network I/O."""
import unittest
from ipaddress import ip_address
import httpx
from datetime import datetime
from unittest.mock import patch

from tests import test_bootstrap
from backend.ai_analysis import fixed_research as fixed, web_research as web
from backend.ai_analysis import channel_policy


class ChannelShutdownTests(unittest.TestCase):
    def test_blocked_page_reads_do_not_call_http_reader(self):
        for url in ('https://www.czce.com.cn/report', 'https://oil.sci99.com/report',
                    'https://www.cfmmc.com/report'):
            with self.subTest(url=url), patch.object(web, 'public_get') as get:
                with self.assertRaisesRegex(ValueError, 'CHANNEL_DISABLED'):
                    web.read_public_page({'url': url, 'dimension': 'fundamentals'})
                get.assert_not_called()

    def test_direct_http_entry_rejects_blocked_domains_before_dns(self):
        for url in ('https://www.czce.com.cn/report', 'https://oil.sci99.com/report',
                    'https://www.cfmmc.com/report'):
            with self.subTest(url=url), patch.object(web.provider, '_resolve_addresses_async') as dns, \
                    patch.object(web.httpx, 'AsyncClient', side_effect=ValueError('NETWORK_ATTEMPT')) as client:
                with self.assertRaisesRegex(ValueError, 'CHANNEL_DISABLED'):
                    web.public_get(url, timeout=1)
                dns.assert_not_called()
                client.assert_not_called()

    def test_fixed_collector_cannot_reenable_blocked_domain_with_source_flag(self):
        source = {'source_id': 'TEST_SCI', 'entry': 'https://oil.sci99.com/report',
                  'parser': 'nbs', 'topics': (), 'products': ('FU',), 'enabled': True}
        with patch.object(fixed, 'FIXED_SOURCES', (source,)), patch.object(web, 'public_get') as get:
            result = fixed.collect_fixed({'owner': 'test'}, 'product:FU', fixed.Budget())
        self.assertEqual(result['sources_checked'][0]['status'], 'UNAVAILABLE')
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertIn('CHANNEL_DISABLED', result['sources_checked'][0]['reason'])
        self.assertEqual(result['checks_count'], 0)
        self.assertEqual(result['sources'], [])
        get.assert_not_called()

    def test_search_does_not_return_unreadable_blocked_candidates(self):
        rss = '<rss><channel><item><title>燃料油库存</title><link>https://oil.sci99.com/report</link></item></channel></rss>'
        events = []
        with patch.object(web, 'SEARCH_CHANNELS', (('Bing RSS', '_search_bing', None),)), \
                patch.object(web, 'public_get', return_value=(rss, 'https://www.bing.com/search')):
            rows = web.search_public_web('燃料油', 'fundamentals', events=events)
        self.assertEqual(rows, [])
        self.assertTrue(any(event['status'] == 'UNAVAILABLE' and 'CHANNEL_DISABLED' in event.get('message', '')
                            for event in events))

    def test_disabled_search_channel_skips_handler_without_http(self):
        policies = dict(channel_policy.CHANNELS, _search_bing={
            'enabled': False, 'hosts': ['bing.com'], 'reason': 'test disabled'})
        events = []
        with patch.object(channel_policy, 'CHANNELS', policies), \
                patch.object(web, 'SEARCH_CHANNELS', (('Bing RSS', '_search_bing', None),)), \
                patch.object(web, 'public_get') as get:
            self.assertEqual(web.search_public_web('燃料油', 'fundamentals', events=events), [])
        get.assert_not_called()
        self.assertEqual(events[0]['status'], 'UNAVAILABLE')
        self.assertIn('test disabled', events[0]['message'])

    def test_fixed_channel_switch_skips_budget_and_records_reason(self):
        source = next(source for source in fixed.FIXED_SOURCES if source['source_id'] == 'SHFE_WAREHOUSE_DAILY')
        policies = dict(channel_policy.CHANNELS, SHFE_WAREHOUSE_DAILY={
            'enabled': False, 'hosts': ['shfe.com.cn'], 'reason': 'test disabled'})
        budget = fixed.Budget()
        with patch.object(channel_policy, 'CHANNELS', policies), \
                patch.object(fixed, 'FIXED_SOURCES', (source,)), patch.object(web, 'public_get') as get:
            result = fixed.collect_fixed({'owner': 'test'}, 'product:FU', budget)
        get.assert_not_called()
        self.assertEqual(budget.used['reads'], 0)
        self.assertEqual(budget.used['index_checks'], 0)
        self.assertEqual(result['sources_checked'][0]['status'], 'UNAVAILABLE')
        summary = {row['channel_id']: row for row in result['channel_policy']}
        self.assertEqual(summary['SHFE_WAREHOUSE_DAILY']['status'], 'UNAVAILABLE')

    def test_redirect_to_blocked_domain_stops_before_second_http_request(self):
        requests = []

        def respond(request):
            requests.append(request.headers['host'])
            return httpx.Response(302, headers={'location': 'https://oil.sci99.com/report'})

        real_client = httpx.AsyncClient
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(respond), **kwargs)

        with patch.object(web.httpx, 'AsyncClient', side_effect=client), \
                patch.object(web.provider, '_resolve_addresses_async', return_value=[ip_address('8.8.8.8')]) as dns:
            with self.assertRaisesRegex(ValueError, 'CHANNEL_DISABLED'):
                web.public_get('https://finance.sina.com.cn/report', timeout=1, professional_only=True)
        self.assertEqual(requests, ['finance.sina.com.cn'])
        self.assertEqual(dns.call_count, 1)

    def test_enabled_body_reader_retains_real_body_and_unknown_time(self):
        page = '<article>燃料油库存减少。</article>'
        for url in ('https://finance.sina.com.cn/report', 'https://finance.eastmoney.com/report',
                    'https://www.shfe.com.cn/report', 'https://www.dce.com.cn/report'):
            with self.subTest(url=url), patch.object(web, 'public_get', return_value=(page, url)):
                row = web.read_public_page({'url': url, 'dimension': 'fundamentals'})
            self.assertEqual(row['body'], '燃料油库存减少。')
            self.assertEqual(row['published_at'], 'UNKNOWN')

    def test_shutdown_does_not_delete_historical_normalized_rows(self):
        rows = web.normalize_with_provenance([{
            'url': 'https://oil.sci99.com/report', 'body': '燃料油库存减少。',
            'title': '历史燃料油库存', 'excerpt': '燃料油库存减少。',
            'published_at': '2026-10-05T12:00:00', 'data_period': '2026-10-05',
            'dimension': 'fundamentals', 'content_type': 'BODY'}], datetime(2026, 10, 5, 13),
            allowed_hosts=['oil.sci99.com'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['body'], '燃料油库存减少。')


class AbbreviatedReportPeriodTests(unittest.TestCase):
    def test_bu_refinery_utilization_keeps_explicit_same_sentence_week(self):
        body = ('沥青供应方面，据隆众资讯，本周（20260918-0924），中国92家沥青炼厂产能利用率为23.4%，'
                '环比下降1个百分点，其中重交沥青77家企业产能利用率为23.7%，环比下降0.9个百分点。')
        coverage = web.fundamental_coverage([{'body': body, 'published_at': '2026-09-29T00:00:00',
            'url': 'https://finance.eastmoney.com/a/202609293885857551.html'}], 'BU2611')
        self.assertEqual(coverage[0]['status'], 'AVAILABLE')
        self.assertEqual(coverage[0]['evidence'][0]['data_period'], '2026-09-18/2026-09-24')
        self.assertEqual(coverage[0]['evidence'][0]['quote'], body.rstrip('。'))

    def test_abbreviated_period_never_invents_year_or_rolls_invalid_dates(self):
        for text in ('本周（0918-0924）', '本周（20260230-0302）', '本周（20260924-0918）',
                     '本周（20261228-0102）'):
            with self.subTest(text=text):
                self.assertIsNone(web._explicit_sentence_period(text))

    def test_local_period_does_not_leak_to_different_production_sentence(self):
        body = ('本周（20260918-0924）沥青炼厂产能利用率为23.4%。'
                '沥青道路施工需求增加2万吨。')
        coverage = web.fundamental_coverage([{'body': body, 'data_period': '2026-09-18/2026-09-24'}], 'BU2611')
        self.assertEqual(coverage[0]['status'], 'AVAILABLE')
        self.assertEqual(coverage[2]['status'], 'MISSING')
        self.assertEqual(coverage[2]['detail_status'], 'PERIOD_UNKNOWN')

    def test_refinery_body_expires_after_existing_fourteen_day_limit(self):
        row = {'body': '本周（20260918-0924）沥青炼厂产能利用率为23.4%。',
               'published_at': '2026-09-29T08:49:00', 'dimension': 'fundamentals'}
        row['stale'] = web.future_trend._is_stale(row, datetime(2026, 10, 6, 12))
        self.assertEqual(web.fundamental_coverage([row], 'BU2611')[0]['status'], 'AVAILABLE')
        row['stale'] = web.future_trend._is_stale(row, datetime(2026, 10, 14, 12))
        self.assertEqual(web.fundamental_coverage([row], 'BU2611')[0]['detail_status'], 'STALE')

    def test_modified_asphalt_utilization_stays_out_of_refinery_coverage(self):
        coverage = web.fundamental_coverage([{'body': '本周（20260918-0924）沥青改性企业产能利用率为23.4%。',
            'url': 'https://finance.eastmoney.com/report'}], 'BU2611')
        self.assertEqual(coverage[0]['status'], 'MISSING')
