import json
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from tests import test_bootstrap
from backend.ai_analysis import web_research as web


class SinaKeywordSearchTests(unittest.TestCase):
    def test_query_results_are_candidates_and_tracking_is_removed(self):
        payload = {'code': 0, 'data': {'list': [dict(
            title="<font color='red'>燃料油</font>库存", ctime=1790840517,
            intro='搜索摘要(AI生成)', media_show='新浪财经',
            url='https://finance.sina.com.cn/stock/2026-10-01/doc-test.shtml?from=pcsearch'),
            dict(title='内网', url='https://127.0.0.1/a')]}}
        with patch.object(web, 'public_get', return_value=(json.dumps(payload), 'https://search.sina.com.cn/api/news')) as get:
            rows = web._search_sina('燃料油 库存', 'fundamentals', time.monotonic()+5, 'FU')
        params = parse_qs(urlsplit(get.call_args.args[0]).query)
        self.assertEqual(params['q'], ['燃料油 库存'])
        self.assertEqual(params['sort'], ['1'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], '燃料油 库存')
        self.assertEqual(rows[0]['url'], payload['data']['list'][0]['url'].split('?')[0])
        self.assertEqual(rows[0]['content_type'], 'SEARCH_EXCERPT')
        self.assertNotIn('body', rows[0])
        self.assertEqual(rows[0]['published_at'], '2026-10-01T15:41:57+08:00')

    def test_empty_keyword_results_do_not_become_product_index(self):
        with patch.object(web, 'public_get', return_value=('{"code":0,"data":{"list":[]}}', 'https://search.sina.com.cn/api/news')) as get:
            self.assertEqual(web._search_sina('不存在词', 'fundamentals', time.monotonic()+5, 'FU'), [])
        self.assertEqual(get.call_count, 1)

    def test_zero_total_with_null_list_is_an_official_empty_result(self):
        with patch.object(web, 'public_get', return_value=('{"code":0,"data":{"list":null,"total":0}}', 'https://search.sina.com.cn/api/news')):
            self.assertEqual(web._search_sina('不存在词', 'fundamentals', time.monotonic()+5, 'FU'), [])

    def test_errors_and_changed_schema_are_not_empty_success(self):
        for payload in ('[]', '{}', '{"code":429,"data":{"list":[]}}', '{"code":0,"data":{"list":{}}}'):
            with self.subTest(payload=payload), patch.object(web, 'public_get', return_value=(payload, 'https://search.sina.com.cn/api/news')):
                with self.assertRaises(ValueError):
                    web._search_sina('库存', 'fundamentals', time.monotonic()+5, 'FU')

    def test_saved_official_response_parses_offline(self):
        payload = Path(__file__).with_name('fixtures').joinpath('sina_news.json').read_text(encoding='utf-8-sig')
        with patch.object(web, 'public_get', return_value=(payload, 'https://search.sina.com.cn/api/news')):
            rows = web._search_sina('燃料油 库存', 'fundamentals', time.monotonic()+5, 'FU')
        self.assertGreater(len(rows), 0)
        self.assertTrue(all(row['content_type'] == 'SEARCH_EXCERPT' for row in rows))

    def test_search_timestamp_and_summary_do_not_replace_body_metadata(self):
        candidate = dict(url='https://finance.sina.com.cn/2026-10-01/doc-test.shtml',
            dimension='fundamentals', published_at='2026-10-01T15:41:57+08:00', excerpt='AI生成库存100吨')
        with patch.object(web, 'public_get', return_value=('<div id="artibody">燃料油实际正文</div>', candidate['url'])):
            row = web.read_public_page(candidate)
        self.assertEqual(row['published_at'], 'UNKNOWN')
        self.assertEqual(row['body'], '燃料油实际正文')

    def test_keyword_failure_is_visible_when_index_fallback_succeeds(self):
        events = []
        row = dict(url='https://finance.sina.com.cn/money/future/2026-10-01/doc-test.shtml', title='燃料油库存', dimension='fundamentals')
        with patch.object(web, '_search_bing', return_value=[]), patch.object(web, '_search_eastmoney', return_value=[]), patch.object(web, '_search_sina', side_effect=TimeoutError('429')), patch.object(web, '_search_sina_index', return_value=[row]):
            rows = web.search_public_web('燃料油 库存', 'fundamentals', instrument='FU', events=events)
        self.assertEqual(rows[0]['search_provider'], 'Sina futures index')
        self.assertTrue(any(e['status'] == 'FAILED' and e['search_provider'] == 'Sina keyword search' for e in events))
        self.assertTrue(any(e['status'] == 'COMPLETED' and e['search_provider'] == 'Sina futures index' for e in events))

    def test_no_budget_means_no_fallback_request(self):
        with patch.object(web, 'public_get') as get:
            with self.assertRaises(TimeoutError):
                web._search_sina('库存', 'fundamentals', time.monotonic()-1, 'FU')
        get.assert_not_called()
