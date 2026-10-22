"""固定来源与规则搜索：以本地保存的真实格式样本覆盖 NBS 目录、央行栏目、EIA 周报。

样本结构与 2026-10-02 对各站点的真实 HTTP 核对记录一致（见交付记录）。
"""
import json
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_bootstrap
from backend.ai_analysis import fixed_research, research_cache, web_research


NOW = datetime(2026, 10, 2, 19)
OWNER = 'fixed-research-a'

NBS_ARTICLE = """<html><head><title>2026年9月中国采购经理指数运行情况</title></head>
<body><div id="ContentBody"><p>9月份，制造业采购经理指数（PMI）为49.8%，比上月上升0.7个百分点。</p>
<p>数据来源：国家统计局。</p></div></body></html>"""

PBC_LIST = """<html><body>
<table><tr>
<td><font class="newslist_style"><a href="/goutongjiaoliu/113456/113469/2026092917474922559/index.html"
 onclick="void(0)" target="_blank" title="中国人民银行调整完善若干货币政策工具" istitle="true">中国人民银行调整完善若干货币政策工具</a></font>
 <span class="hui12">2026-09-29</span></td></tr>
<tr><td><font class="newslist_style"><a href="/goutongjiaoliu/113456/113469/2026091414593871200/index.html"
 title="2026年8月金融统计数据报告" istitle="true">2026年8月金融统计数据报告</a></font>
 <span class="hui12">2026-09-14</span></td></tr>
<tr><td><font class="newslist_style"><a href="/goutongjiaoliu/113456/113469/2026092916445869455/index.html"
 title="中国人民银行行长潘功胜会见欧盟驻华大使范恺珀" istitle="true">会见新闻不构成政策序列</a></font>
 <span class="hui12">2026-09-29</span></td></tr>
</table></body></html>"""

PBC_ARTICLE = """<html><head><title>中国人民银行调整完善若干货币政策工具</title></head>
<body><div class="TRS_Editor"><p>为保持流动性合理充裕，中国人民银行决定调整完善若干货币政策工具，
自2026年10月15日起实施。</p></div></body></html>"""

PBC_STATS_ARTICLE = """<html><head><title>2026年8月金融统计数据报告</title></head>
<body><div class="TRS_Editor"><p>8月末，广义货币(M2)余额同比增长。</p></div></body></html>"""

EIA_SUMMARY = """For the week ending September 25, 2026, U.S. refineries processed 16.3 million
barrels per day (b/d), down 554,000 b/d from the previous week, at 92.5% capacity utilization.

Commercial crude oil inventories (excluding the Strategic Petroleum Reserve) increased 0.9 million
barrels to 427.3 million barrels, 2% above the five-year average. Gasoline inventories decreased
1.7 million barrels, 7% below the five-year average."""

SNAPSHOT = {'owner': OWNER, 'topic': 'product:FU', 'product_code': 'FU',
            'allowed_dimensions': ['fundamentals', 'events', 'related_markets'],
            'market': {'snapshot_time': NOW.isoformat(), 'instrument': 'FU'}}


def budget(**limits):
    return fixed_research.Budget(deadline=time.monotonic() + 90, **limits)


class FixedResearchTests(unittest.TestCase):
    def test_blocked_exchange_sources_are_scoped_and_never_enabled(self):
        for source_id, products in (('CZCE_WAREHOUSE_DAILY', ('SA', 'AP')),):
            with self.subTest(source_id=source_id):
                source = next((s for s in fixed_research.FIXED_SOURCES if s['source_id'] == source_id), None)
                self.assertIsNotNone(source)
                self.assertFalse(source['enabled'])
                self.assertEqual(source['block_verified_on'], '2026-10-05')
                self.assertTrue(source['block_evidence'])
                self.assertEqual(source['products'], products)
                self.assertFalse(fixed_research._source_applicable(source, 'macro'))
                self.assertFalse(fixed_research._source_applicable(source, 'product:IH'))
                for product in products:
                    self.assertTrue(fixed_research._source_applicable(source, 'product:' + product))

    def test_blocked_warehouse_check_emits_evidence_without_data_or_budget_use(self):
        for product, source_id in (('SA', 'CZCE_WAREHOUSE_DAILY'),):
            with self.subTest(product=product):
                result, b = self.collect('product:' + product)
                checks = [s for s in result['sources_checked'] if s['source_id'] == source_id]
                self.assertEqual(len(checks), 1)
                self.assertEqual(checks[0]['status'], 'UNAVAILABLE')
                self.assertIn('SOURCE_BLOCKED', checks[0]['reason'])
                self.assertEqual(result['sources'], [])
                self.assertTrue(any(e.get('source_id') == source_id and e['status'] == 'UNAVAILABLE' for e in result['events']))
                self.assertEqual(b.used['index_checks'], 0)
                self.assertEqual(b.used['reads'], 0)

    def test_index_keywords_and_names(self):
        names = {'IH': '上证50股指', 'IF': '沪深300股指', 'IC': '中证500股指', 'IM': '中证1000股指'}
        for product, name in names.items():
            with self.subTest(product=product):
                words = fixed_research.rule_keywords('product:' + product)
                self.assertIn('股指', words['fundamentals'])
                self.assertNotIn('库存', str(words))
                self.assertNotIn('仓单', str(words))
                self.assertEqual(web_research.future_trend.research_name(product + '2610'), name)

    def test_configured_commodity_keywords(self):
        for product in ('JD', 'LH', 'SA', 'AP'):
            with self.subTest(product=product):
                self.assertIn(product, fixed_research.RULE_KEYWORDS)
                words = fixed_research.rule_keywords('product:' + product)
                self.assertEqual(set(words), {'fundamentals', 'events'})
                self.assertTrue(all(web_research.future_trend.research_name(product) in word for word in words.values()))

    def setUp(self):
        from backend.models.database import AIResearchMaterialModel
        from backend.storage.mysql_client import SessionLocal
        self.Material = AIResearchMaterialModel
        with SessionLocal() as db:
            db.query(self.Material).filter_by(owner_username=OWNER).delete()
            db.commit()
        # 未显式 mock 的来源一律快速失败，绝不访问真实网络
        self.net_guard = patch.object(web_research, 'public_get',
                                      side_effect=ValueError('network disabled in test'))
        self.net_guard.start()
        self.addCleanup(self.net_guard.stop)

    def persist(self, rows):
        from backend.storage.mysql_client import SessionLocal
        with SessionLocal() as db:
            refs, inserted = research_cache.persist_sources(
                db, SimpleNamespace(owner_username=OWNER, job_id='test-job'), rows, NOW)
            db.commit()
        return refs, inserted

    def collect(self, topic='product:FU', b=None, side_effects=None):
        b = b or budget()
        mapping = {'public_get': None, 'latest_official_macro': None, 'read_public_page': None}
        mapping.update(side_effects or {})
        patches = []
        if mapping['public_get'] is not None:
            patches.append(patch.object(web_research, 'public_get', side_effect=mapping['public_get']))
        if mapping['latest_official_macro'] is not None:
            patches.append(patch.object(web_research, 'latest_official_macro',
                                        side_effect=mapping['latest_official_macro']))
        if mapping['read_public_page'] is not None:
            patches.append(patch.object(web_research, 'read_public_page',
                                        side_effect=mapping['read_public_page']))
        for p in patches:
            p.start()
        try:
            return fixed_research.collect_fixed(dict(SNAPSHOT, topic=topic), topic, b), b
        finally:
            for p in patches:
                p.stop()

    def test_nbs_index_discovers_series_and_reads_new_release(self):
        def public_get_side_effect(url, timeout, professional_only=False):
            if 'pbc.gov.cn' in url:
                return '<html><body><table></table></body></html>', url
            raise ValueError('unexpected url ' + url)

        def read_page(candidate, timeout):
            self.assertEqual(candidate['url'], 'https://www.stats.gov.cn/sj/zxfb/202609/t20260930_1965449.html')
            return {'url': candidate['url'], 'title': '2026年9月中国采购经理指数运行情况',
                    'body': '9月份制造业采购经理指数为49.8%', 'excerpt': '9月份制造业采购经理指数为49.8%',
                    'published_at': 'UNKNOWN', 'content_type': 'BODY'}
        result, b = self.collect(topic='macro',
                                 side_effects={'latest_official_macro': lambda timeout: [
                                     {'url': 'https://www.stats.gov.cn/sj/zxfb/202609/t20260930_1965449.html',
                                      'title': '2026年9月中国采购经理指数运行情况', 'published_at': '2026-09-30',
                                      'excerpt': '目录条目', 'source_name': '国家统计局', 'source_kind': 'OFFICIAL',
                                      'dimension': 'macro', 'latest_release': True, 'macro_series': 'PMI',
                                      'latest_verified_at': web_research._stamp()}],
                                     'read_public_page': read_page,
                                     'public_get': public_get_side_effect})
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(len(result['sources']), 1)
        row = result['sources'][0]
        # 日期来自目录：正文缺发布时间时补充目录日期并保留 provenance
        self.assertEqual(row['published_at'], '2026-09-30')
        self.assertEqual(row['macro_series'], 'PMI')
        self.assertEqual(row['series_id'], 'NBS:PMI')
        self.assertEqual(row['collection_method'], 'FIXED_SOURCE')
        check = result['latest_release_checks'][0]
        self.assertEqual((check['macro_series'], check['status']), ('PMI', 'COMPLETED'))
        self.assertEqual(check['index_url'], web_research.MACRO_INDEX)
        self.assertEqual(b.used['index_checks'], 2, 'NBS 与央行两个适用来源各一次列表检查')
        refs, inserted = self.persist(result['sources'])
        self.assertEqual(inserted, 1)
        from backend.storage.mysql_client import SessionLocal
        with SessionLocal() as db:
            material = db.query(self.Material).filter_by(owner_username=OWNER).one()
            self.assertEqual(material.published_at, datetime(2026, 9, 30))
            self.assertIn('"series_id": "NBS:PMI"', material.metadata_json)

    def test_unchanged_body_creates_no_new_version_but_records_check(self):
        from backend.storage.mysql_client import SessionLocal
        body = '9月份制造业采购经理指数为49.8%'
        page = {'url': 'https://www.stats.gov.cn/sj/zxfb/a.html', 'title': 'PMI', 'body': body,
                'excerpt': body, 'published_at': 'UNKNOWN', 'content_type': 'BODY'}
        macro = lambda timeout: [{'url': 'https://www.stats.gov.cn/sj/zxfb/a.html', 'title': 'PMI',
                                  'published_at': '2026-09-30', 'excerpt': '目录条目', 'source_name': '国家统计局',
                                  'source_kind': 'OFFICIAL', 'dimension': 'macro', 'latest_release': True,
                                  'macro_series': 'PMI', 'latest_verified_at': web_research._stamp()}]
        empty_pbc = lambda url, timeout, professional_only=False: (
            '<html><body><table></table></body></html>', url) if 'pbc.gov.cn' in url else (_ for _ in ()).throw(ValueError('unexpected ' + url))
        result, _ = self.collect(topic='macro', side_effects={'latest_official_macro': macro,
                                                              'public_get': empty_pbc,
                                                              'read_public_page': lambda c, timeout: page})
        self.persist(result['sources'])
        with SessionLocal() as db:
            first = db.query(self.Material).filter_by(owner_username=OWNER).one()
            original_fetch = first.version_fetched_at
        result, _ = self.collect(topic='macro', side_effects={'latest_official_macro': macro,
                                                              'public_get': empty_pbc,
                                                              'read_public_page': lambda c, timeout: page})
        self.assertEqual(result['status'], 'COMPLETED')
        with SessionLocal() as db:
            self.assertEqual(db.query(self.Material).filter_by(owner_username=OWNER).count(), 1)
            row = db.query(self.Material).filter_by(owner_username=OWNER).one()
            self.assertEqual(row.version_fetched_at, original_fetch, '未变正文不得刷新获取时间')
        # 检查本身仍被记录（序列核验完成）
        self.assertTrue(any(c['status'] == 'COMPLETED' for c in result['latest_release_checks']))

    def test_revised_body_creates_immutable_new_version(self):
        from backend.storage.mysql_client import SessionLocal
        macro = lambda timeout: [{'url': 'https://www.stats.gov.cn/sj/zxfb/a.html', 'title': 'PMI',
                                  'published_at': '2026-09-30', 'excerpt': '目录条目', 'source_name': '国家统计局',
                                  'source_kind': 'OFFICIAL', 'dimension': 'macro', 'latest_release': True,
                                  'macro_series': 'PMI', 'latest_verified_at': web_research._stamp()}]
        result, _ = self.collect(topic='macro', side_effects={
            'latest_official_macro': macro,
            'read_public_page': lambda c, timeout: {'url': c['url'], 'title': 'PMI', 'body': '初值 49.8%',
                                                    'excerpt': '初值 49.8%', 'published_at': 'UNKNOWN',
                                                    'content_type': 'BODY'}})
        self.persist(result['sources'])
        result, _ = self.collect(topic='macro', side_effects={
            'latest_official_macro': macro,
            'read_public_page': lambda c, timeout: {'url': c['url'], 'title': 'PMI', 'body': '修订值 50.1%',
                                                    'excerpt': '修订值 50.1%', 'published_at': 'UNKNOWN',
                                                    'content_type': 'BODY'}})
        self.persist(result['sources'])
        with SessionLocal() as db:
            versions = db.query(self.Material).filter_by(owner_username=OWNER).order_by(
                self.Material.version_fetched_at).all()
        self.assertEqual(len(versions), 2)
        self.assertNotEqual(versions[0].content_hash, versions[1].content_hash)

    def test_pbc_list_only_keeps_registered_series_and_reads_policy_body(self):
        def public_get_side_effect(url, timeout, professional_only=False):
            if url.endswith('113469/index.html'):
                return PBC_LIST, url
            if '2026092917474922559' in url:
                return PBC_ARTICLE, url
            if '2026091414593871200' in url:
                return PBC_STATS_ARTICLE, url
            raise ValueError('unexpected url ' + url)
        result, _ = self.collect(topic='macro', side_effects={'public_get': public_get_side_effect})
        self.assertEqual(len(result['sources']), 2)
        series = {row['series_id'] for row in result['sources']}
        self.assertEqual(series, {'PBC_NEWS_RELEASE:MONETARY_POLICY', 'PBC_NEWS_RELEASE:FINANCIAL_STATISTICS'})
        policy = next(row for row in result['sources'] if row['series_id'].endswith('MONETARY_POLICY'))
        self.assertEqual(policy['published_at'], '2026-09-29')
        self.assertIn('货币政策工具', policy['body'])
        self.assertTrue(policy['extractable'], '复杂政策正文应进入 AI 提取候选')
        # 会见新闻不属于任何注册序列，被忽略
        self.assertNotIn('范恺珀', json.dumps(result['sources'], ensure_ascii=False))

    def test_eia_summary_is_related_market_reference_not_fundamentals(self):
        def public_get_side_effect(url, timeout, professional_only=False):
            self.assertEqual(url, 'https://ir.eia.gov/wpsr/summary.txt')
            return EIA_SUMMARY, url
        sources = tuple(s for s in fixed_research.FIXED_SOURCES if s['source_id'] != 'SHFE_WAREHOUSE_DAILY')
        with patch.object(fixed_research, 'FIXED_SOURCES', sources):
            result, b = self.collect(topic='product:FU', side_effects={'public_get': public_get_side_effect})
        self.assertEqual(len(result['sources']), 1)
        row = result['sources'][0]
        self.assertEqual(row['dimension'], 'related_markets')
        self.assertEqual(row['product_code'], 'FU')
        self.assertEqual(row['series_id'], 'EIA:WPSR')
        self.assertEqual(row['published_at'], 'UNKNOWN')
        self.assertEqual(row['data_period'], 'Week ending 2026-09-25')
        self.assertEqual(row['source_kind'], 'OFFICIAL')
        self.assertFalse(row['extractable'], '稳定数据表不需要 AI 提取')
        self.assertEqual(b.used['index_checks'], 1)
        self.assertEqual(b.used['reads'], 1)
        # macro 主题下 EIA 不适用
        result, _ = self.collect(topic='macro', side_effects={'public_get': public_get_side_effect})
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['sources_checked'][-1]['status'], 'NOT_APPLICABLE')

    def test_eia_reuses_one_network_read_per_collection_cycle(self):
        source = next(s for s in fixed_research.FIXED_SOURCES if s.get('parser') == 'eia')
        fixed_research._EIA_CYCLE_CACHE.clear()
        self.addCleanup(fixed_research._EIA_CYCLE_CACHE.clear)
        with patch.object(web_research, 'public_get', return_value=(EIA_SUMMARY, source['entry'])) as reader, \
             patch.object(fixed_research, '_stamp', side_effect=['2026-10-02T19:00:00', '2026-10-02T20:00:00']):
            first = fixed_research._parse_eia(source, 'product:FU', 'FU', budget(), OWNER,
                                             {'collection_cycle_id': 'cycle-a'})
            second = fixed_research._parse_eia(source, 'product:BU', 'BU', budget(), OWNER,
                                              {'collection_cycle_id': 'cycle-a'})
            third = fixed_research._parse_eia(source, 'product:FU', 'FU', budget(), OWNER,
                                             {'collection_cycle_id': 'cycle-b'})
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(first['rows'][0]['fetched_at'], second['rows'][0]['fetched_at'])
        self.assertNotEqual(first['rows'][0]['fetched_at'], third['rows'][0]['fetched_at'])

    def test_source_timeout_and_403_are_recorded_not_faked(self):
        def public_get_side_effect(url, timeout, professional_only=False):
            if 'pbc.gov.cn' in url:
                raise ValueError('HTTP 403（网站拒绝自动读取）：' + url)
            raise TimeoutError('Research time budget exhausted')
        result, _ = self.collect(topic='macro', side_effects={'public_get': public_get_side_effect})
        by_source = {item['source_id']: item for item in result['sources_checked']}
        self.assertEqual(by_source['PBC_NEWS_RELEASE']['status'], 'FAILED')
        self.assertIn('403', by_source['PBC_NEWS_RELEASE']['reason'])
        self.assertTrue(result['errors'])
        self.assertEqual(result['sources'], [])
        # NBS 通过 latest_official_macro 抛超时
        result, _ = self.collect(topic='macro', side_effects={
            'latest_official_macro': lambda timeout: (_ for _ in ()).throw(TimeoutError('超时'))})
        by_source = {item['source_id']: item for item in result['sources_checked']}
        self.assertEqual(by_source['NBS_LATEST_RELEASE']['status'], 'FAILED')

    def test_shfe_warehouse_read_failure_is_recorded_not_faked(self):
        result, _ = self.collect(topic='product:FU', side_effects={
            'latest_official_macro': lambda timeout: [], 'read_public_page': lambda c, timeout: {}})
        by_source = {item['source_id']: item for item in result['sources_checked']}
        self.assertEqual(by_source['SHFE_WAREHOUSE_DAILY']['status'], 'FAILED')
        self.assertIn('network disabled', by_source['SHFE_WAREHOUSE_DAILY']['reason'])
        self.assertEqual(by_source['NBS_LATEST_RELEASE']['status'], 'NOT_APPLICABLE')

    def test_budget_truncation_records_unchecked_sources_and_pending_candidates(self):
        b = budget(index_checks=1, reads=1)
        macro = lambda timeout: [
            {'url': f'https://www.stats.gov.cn/sj/zxfb/{i}.html', 'title': f'发布{i}',
             'published_at': f'2026-09-{20+i:02d}', 'excerpt': 'x', 'source_name': '国家统计局',
             'source_kind': 'OFFICIAL', 'dimension': 'macro', 'latest_release': True,
             'macro_series': f'S{i}', 'latest_verified_at': web_research._stamp()}
            for i in range(3)]
        result, _ = self.collect(topic='macro', b=b, side_effects={
            'latest_official_macro': macro,
            'read_public_page': lambda c, timeout: {'url': c['url'], 'title': c['title'],
                                                    'body': '正文' + c['title'], 'excerpt': '正文',
                                                    'published_at': 'UNKNOWN', 'content_type': 'BODY'}})
        self.assertEqual(b.used['reads'], 1, '读取预算应被尊重')
        self.assertEqual(len(result['sources']), 1)
        skipped = [e for e in result['events'] if e['type'] == 'READ' and e['status'] == 'SKIPPED']
        self.assertEqual(len(skipped), 2, '预算截断要记录未读取候选')
        # 列表检查次数用尽：后续来源 SKIPPED_BUDGET
        b2 = budget(index_checks=1)
        result2, _ = self.collect(topic='macro', b=b2, side_effects={'latest_official_macro': lambda timeout: []})
        self.assertEqual(b2.used['index_checks'], 1)
        statuses = {item['source_id']: item['status'] for item in result2['sources_checked']}
        self.assertEqual(statuses['PBC_NEWS_RELEASE'], 'SKIPPED_BUDGET')

    def test_rule_search_uses_fixed_keywords_and_validates_relevance(self):
        seen_queries = []
        def search(query, dimension, **kw):
            seen_queries.append((query, dimension))
            if dimension == 'fundamentals':
                return [{'url': 'https://futures.eastmoney.com/a/fu1.html', 'title': '燃料油库存周报',
                         'excerpt': '燃料油库存下降，供应减少。', 'published_at': '2026-10-01'},
                        {'url': 'https://futures.eastmoney.com/a/other.html', 'title': '螺纹钢价格',
                         'excerpt': '螺纹钢价格上涨。', 'published_at': '2026-10-01'}]
            return [{'url': 'https://futures.eastmoney.com/a/fu2.html', 'title': '燃料油炼厂检修',
                     'excerpt': '炼厂停产影响供应。', 'published_at': '2026-10-01'}]
        def read_page(candidate, timeout):
            return {'url': candidate['url'], 'title': candidate['title'],
                    'body': candidate['excerpt'] + '正文补充', 'excerpt': candidate['excerpt'] + '正文补充',
                    'published_at': candidate.get('published_at') or 'UNKNOWN', 'content_type': 'BODY'}
        b = budget()
        with patch.object(web_research, 'search_public_web', side_effect=search), \
             patch.object(web_research, 'read_public_page', side_effect=read_page):
            result = fixed_research.collect_rule_search(SNAPSHOT, 'product:FU', b)
        self.assertEqual(seen_queries, [('燃料油 库存 仓单 供应 船燃需求', 'fundamentals'),
                                        ('燃料油 炼厂 制裁 航运', 'events')], '固定关键词按计划 7.1 表生成')
        self.assertEqual(result['searches'], 2)
        self.assertEqual(len(result['sources']), 2)
        self.assertTrue(all(row['collection_method'] == 'RULE_SEARCH' for row in result['sources']))
        self.assertTrue(all(row['product_code'] == 'FU' for row in result['sources']))
        # 不相关品种被剔除
        self.assertFalse(any('螺纹钢' in row['title'] for row in result['sources']))
        # 其他品种按名称生成关键词
        keywords = fixed_research.rule_keywords('product:CU')
        self.assertEqual(keywords, {'fundamentals': '铜 库存 仓单 供需', 'events': '铜 政策 制裁 停产'})
        # macro 主题不做规则搜索
        result = fixed_research.collect_rule_search(dict(SNAPSHOT, topic='macro'), 'macro', budget())
        self.assertEqual(result['status'], 'NOT_APPLICABLE')
        self.assertEqual(result['searches'], 0)

    def test_search_budget_shared_and_capped(self):
        b = budget(searches=1)
        with patch.object(web_research, 'search_public_web', return_value=[]) as search:
            result = fixed_research.collect_rule_search(SNAPSHOT, 'product:FU', b)
            self.assertEqual(result['searches'], 1)
            self.assertEqual(search.call_count, 1)
            skipped = [e for e in result['events'] if e['status'] == 'SKIPPED' and e['type'] == 'SEARCH']
            self.assertEqual(len(skipped), 1, '第二个固定检索词因预算被跳过并记录')


if __name__ == '__main__':
    unittest.main()
