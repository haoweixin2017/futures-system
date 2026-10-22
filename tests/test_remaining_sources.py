import unittest
from datetime import datetime
from unittest.mock import patch

from tests import test_bootstrap
from backend.ai_analysis import web_research as web, future_trend as trend, research_cache as cache


class RemainingSourceTests(unittest.TestCase):
    def warehouse(self, **changes):
        return dict(dict(source_kind='OFFICIAL', content_type='BODY', collection_method='FIXED_SOURCE',
            source_entry='SHFE_WAREHOUSE_DAILY', freshness_policy='VERIFIED_DATA_PERIOD',
            product_code='FU', series_id='SHFE:WAREHOUSE_DAILY:FU', latest_release=True,
            url='https://www.shfe.com.cn/data/tradedata/future/stockdata/dailystock_20260930/ZH/shfe/fu.html',
            latest_release_index_url='https://www.shfe.com.cn/data/tradedata/future/dailydata/stock_data_list.dat',
            data_period='2026-09-30', published_at='UNKNOWN', latest_verified_at='2026-10-05T22:00:00',
            expected_update_by='2026-10-08T18:00:00', verification_ttl_hours=24,
            version_fetched_at='2026-10-05T22:00:00', dimension='fundamentals'), **changes)

    def test_verified_official_warehouse_can_freeze_without_publication_time(self):
        row = self.warehouse()
        now = datetime(2026, 10, 5, 23)
        self.assertFalse(trend._is_stale(row, now))
        self.assertEqual(cache._invalid_reasons(row, now, 60), [])
        self.assertEqual(row['published_at'], 'UNKNOWN')

    def test_unknown_time_exception_rejects_unregistered_future_expired_or_wrong_report(self):
        for changes in ({'source_entry': 'OTHER'}, {'source_kind': 'NEWS'},
                        {'latest_verified_at': '2026-10-06T00:00:00'},
                        {'latest_verified_at': '2026-10-04T22:00:00'},
                        {'expected_update_by': '2026-10-05T18:00:00'},
                        {'data_period': '2026-10-06'}, {'url': 'https://finance.sina.com.cn/report'},
                        {'data_period': '2026-09-29'}, {'latest_release': False}):
            with self.subTest(changes=changes):
                row = self.warehouse(**changes)
                self.assertTrue(trend._is_stale(row, datetime(2026, 10, 5, 23)))
                self.assertIn('UNKNOWN_PUBLICATION_TIME', cache._invalid_reasons(row, datetime(2026, 10, 5, 23), 60))

    def test_sina_index_discovers_real_product_links_without_inventing_publication_time(self):
        page = '<a href="https://finance.sina.com.cn/money/future/nyzx/2026-10-05/doc-test.shtml">生意社燃料油10月5日资讯</a>'
        page += '<a href="https://finance.sina.com.cn/money/future/nyzx/2025-10-05/doc-old.shtml">旧燃料油</a>'
        import time
        with patch.object(web, 'public_get', return_value=(page, 'https://finance.sina.com.cn/futuremarket/')):
            rows = web._search_sina_index('燃料油 库存', 'fundamentals', time.monotonic()+5, 'FU2611')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['published_at'], 'UNKNOWN')
        self.assertEqual(web._candidate_time(rows[0]), datetime(2026, 10, 5))
        self.assertFalse(web._recent_candidate(rows[1], datetime(2026, 10, 5)))

    def test_sina_reader_only_uses_artibody(self):
        page = '<meta property="article:published_time" content="2026-10-05T18:18:44+08:00"><body><div class="article-content">广告<div id="artibody">燃料油库存下降，仓单减少。</div>推荐文章</div></body>'
        candidate = dict(url='https://finance.sina.com.cn/money/future/nyzx/2026-10-05/doc-test.shtml', dimension='fundamentals')
        with patch.object(web, 'public_get', return_value=(page, candidate['url'])):
            row = web.read_public_page(candidate)
        self.assertEqual(row['body'], '燃料油库存下降，仓单减少。')
        self.assertNotEqual(row['published_at'], 'UNKNOWN')

    def test_multiple_channels_get_body_read_opportunities_within_existing_cap(self):
        rss = '<rss><channel>' + ''.join('<item><title>燃料油库存</title><link>https://finance.eastmoney.com/a/%s</link></item>' % i for i in range(10)) + '</channel></rss>'
        page = '<a href="https://finance.sina.com.cn/money/future/nyzx/2026-10-05/doc-test.shtml">燃料油供应</a>'
        with patch.object(web, 'public_get', side_effect=[(rss, 'https://www.bing.com'), ('{"code":429}', 'https://search.sina.com.cn'), (page, 'https://finance.sina.com.cn/futuremarket/'), ('{}', 'https://search-api-web.eastmoney.com')]):
            rows = web.search_public_web('燃料油', 'fundamentals')
        self.assertEqual(len(rows), 8)
        self.assertTrue(any(row['search_provider'] == 'Sina futures index' for row in rows[:4]))
