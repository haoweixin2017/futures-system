import json
import unittest
from unittest.mock import patch
from datetime import datetime
from tests import test_bootstrap
from backend.ai_analysis import provider


def call(name, **args):
    return {'id': name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


class WebResearchTests(unittest.TestCase):
    def test_search_channels_merge_order_dedup_and_failure_events(self):
        from backend.ai_analysis import web_research as web
        rss = '<rss><channel><item><title>燃料油库存</title><link>https://finance.eastmoney.com/a/2026100112345.html</link></item></channel></rss>'
        fallback = json.dumps({'result': {'cmsArticleWebOld': [
            {'url': 'https://finance.eastmoney.com/a/2026100112345.html', 'title': '燃料油库存', 'date': '2026-10-01'},
            {'url': 'https://finance.eastmoney.com/a/2', 'title': '燃料油供应', 'date': '2026-10-01'}]}})
        events = []
        with patch.object(web, 'public_get', side_effect=[(rss, 'https://www.bing.com'), ('{"code":0,"data":{"list":[]}}', 'https://search.sina.com.cn/api/news'), (fallback, 'https://search-api-web.eastmoney.com')]):
            rows = web.search_public_web('燃料油', 'fundamentals', events=events)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['search_provider'], 'Bing RSS')
        self.assertEqual(rows[1]['search_provider'], 'Eastmoney public search')
        starts = [e['search_provider'] for e in events if e['status'] in ('RUNNING', 'UNAVAILABLE')]
        self.assertEqual(starts, ['Bing RSS', 'Sina keyword search', 'Eastmoney public search'])
        self.assertTrue(any(e['search_provider'] == 'Sina keyword search' and e['status'] == 'COMPLETED' for e in events))
        events = []
        with patch.object(web, 'public_get', side_effect=[ValueError('bing down'), ('{"code":0,"data":{"list":[]}}', 'https://search.sina.com.cn/api/news'), (fallback, 'https://search-api-web.eastmoney.com')]):
            rows = web.search_public_web('燃料油', 'fundamentals', events=events)
        self.assertEqual(len(rows), 2)
        self.assertIn('bing down', rows[0]['search_warning'])
        self.assertTrue(any(e['status'] == 'FAILED' and 'bing down' in e['message'] for e in events))

    def test_empty_search_still_retains_channel_events(self):
        from backend.ai_analysis import web_research as web
        events = []
        with patch.object(web, 'public_get', side_effect=[('<rss/>', 'https://www.bing.com'), ('{"code":0,"data":{"list":[]}}', 'https://search.sina.com.cn/api/news'), ('{}', 'https://search-api-web.eastmoney.com')]):
            self.assertEqual(web.search_public_web('燃料油', 'events', events=events), [])
        self.assertEqual(sum(e['status'] == 'COMPLETED' for e in events), 3)
        self.assertFalse(any(e['status'] == 'UNAVAILABLE' for e in events))

    def test_later_channel_failure_preserves_bing_and_candidate_cap(self):
        from backend.ai_analysis import web_research as web
        rss = '<rss><channel>' + ''.join(
            '<item><title>燃料油库存</title><link>https://finance.sina.com.cn/a/%s</link></item>' % i
            for i in range(12)) + '</channel></rss>'
        events = []
        with patch.object(web, 'public_get', side_effect=[(rss, 'https://www.bing.com'), ('{"code":0,"data":{"list":[]}}', 'https://search.sina.com.cn/api/news'), ValueError('eastmoney down')]):
            rows = web.search_public_web('燃料油', 'fundamentals', events=events)
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row['search_provider'] == 'Bing RSS' for row in rows))
        self.assertTrue(any(e['status'] == 'FAILED' and 'eastmoney down' in e['message'] for e in events))

    def test_all_channels_fail_preserves_events_and_shared_deadline(self):
        from backend.ai_analysis import web_research as web
        events = []
        with patch.object(web, 'public_get', side_effect=ValueError('offline')) as get:
            with self.assertRaisesRegex(ValueError, 'Bing RSS failed.*Eastmoney public search failed'):
                web.search_public_web('燃料油', 'events', events=events)
        self.assertEqual(get.call_count, 4)
        self.assertEqual(sum(e['status'] == 'FAILED' for e in events), 4)
        events = []
        with patch.object(web, 'public_get') as get:
            with self.assertRaises(ValueError):
                web.search_public_web('燃料油', 'events', timeout=0, events=events)
        get.assert_not_called()

    def test_index_and_commodity_relevance(self):
        from backend.ai_analysis import web_research as web
        for product, title in (('IH', '上证50成交持仓'), ('IF', '沪深300贴水'),
                               ('IC', '中证500两融'), ('IM', '中证1000升水'),
                               ('JD', '鸡蛋供应'), ('LH', '猪价变化'),
                               ('SA', '纯碱开工率'), ('AP', '富士产区')):
            with self.subTest(product=product):
                self.assertTrue(web._relevant({'title': title, 'body': '公开市场报告'}, product + '2610', 'fundamentals'))
        self.assertFalse(web._relevant({'title': '上证50破解版软件下载'}, 'IH2610', 'events'))
        self.assertFalse(web._relevant({'title': '原油炼厂停产'}, 'IH2610', 'events'))

    config = {'id': 'm1', 'display_name': 'Model', 'model_name': 'test', 'provider': 'openai', 'timeout_seconds': 120}
    snapshot = {'market': {'snapshot_time': '2026-10-02T12:00:00', 'instrument': 'FU2611'}}

    def setUp(self):
        # All network discovery is mocked, including the bounded gap supplement.
        self.macro_patch = patch('backend.ai_analysis.web_research.latest_official_macro', return_value=[])
        self.macro_patch.start()
        self.addCleanup(self.macro_patch.stop)

    def test_reader_keeps_fuel_section_beyond_first_4000_characters(self):
        from backend.ai_analysis import web_research as web
        page = '<article>' + ('橡胶库存背景。'*700) + '燃料油高硫库存下降，基差走强。' + '</article>'
        candidate = {'url':'https://finance.eastmoney.com/a/1',
                     'dimension':'fundamentals', '_instrument':'FU2611'}
        with patch.object(web,'public_get',return_value=(page,candidate['url'])):
            row = web.read_public_page(candidate)
        self.assertIn('燃料油高硫库存下降',row['excerpt'])
        self.assertLessEqual(len(row['excerpt']),4000)

    def test_price_only_report_does_not_stop_fundamental_gap_search(self):
        from backend.ai_analysis import web_research as web
        weak = {'url':'https://finance.eastmoney.com/a/1','title':'能化日报',
                'excerpt':'燃料油收涨48元。PVC库存较高，需求回落。','published_at':'2026-09-30'}
        rich = {'url':'https://finance.eastmoney.com/a/2','title':'燃料油库存周报',
                'excerpt':'燃料油高硫库存为100万吨，供应减少，基差走强。','published_at':'2026-10-01'}
        replies = [[call('search_public_web',query='燃料油行情',dimension='fundamentals')], [], []]
        def search(query,dimension,**kw):
            return [weak] if query=='燃料油行情' else [rich] if dimension=='fundamentals' else []
        with patch.object(web,'search_public_web',side_effect=search) as searches, \
             patch.object(web,'read_public_page',side_effect=lambda c,timeout:dict(c)):
            materials = web.run_research(self.snapshot,self.config,
                lambda c,b:provider.ProviderResult(200,'','',None,0,0,1,replies.pop(0)))
        self.assertTrue(any('库存' in c.args[0] for c in searches.call_args_list))
        self.assertIn(rich['url'], [s['url'] for s in materials['sources']])
        self.assertNotIn('fundamentals', materials['coverage_gaps'])
        self.assertLessEqual(materials['research']['search_count'],web.MAX_SEARCHES)
        self.assertLessEqual(materials['research']['pages_read'],web.MAX_READS)

    def test_declared_gbk_html_is_decoded_without_replacement(self):
        from backend.ai_analysis import web_research as web
        import httpx, ipaddress
        client_class = httpx.AsyncClient
        page = '<html><meta charset="gb2312"><title>纽约原油期货行情</title><body>原油价格</body></html>'
        with patch.object(provider, 'resolve_host_addresses', return_value=[ipaddress.ip_address('8.8.8.8')]), patch.object(web.httpx, 'AsyncClient', side_effect=lambda **kw: client_class(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=page.encode('gbk'), headers={'content-type':'text/html'})))):
            body, _ = web.public_get('https://finance.sina.com.cn/a', 2)
        self.assertIn('纽约原油期货行情', body)
        self.assertNotIn('\ufffd', body)

    def test_eastmoney_article_header_time_is_publication_not_render_time(self):
        from backend.ai_analysis import web_research as web
        page = '<!--published at 2026/10/2--><div id="topbox"><div class="infos"><div class="item">2026年09月27日 08:35</div></div></div><div id="ContentBody">燃料油库存下降</div><aside>2026年10月02日</aside>'
        with patch.object(web, 'public_get', return_value=(page, 'https://finance.eastmoney.com/a/1')):
            row = web.read_public_page({'url':'https://finance.eastmoney.com/a/1'}, 2)
        self.assertEqual(row['published_at'], '2026-09-27T08:35:00')

    def test_eastmoney_search_requests_real_time_sort(self):
        from backend.ai_analysis import web_research as web
        from urllib.parse import parse_qs, urlsplit
        fallback = json.dumps({'result':{'cmsArticleWebOld':[{'url':'https://finance.eastmoney.com/a/1','title':'燃料油库存','date':'2026-10-01'}]}})
        with patch.object(web,'public_get',side_effect=[('<rss/>','https://www.bing.com'),('{"code":0,"data":{"list":[]}}','https://search.sina.com.cn/api/news'),(fallback,'https://search-api-web.eastmoney.com')]) as get:
            web.search_public_web('燃料油库存','fundamentals')
        parameter = json.loads(parse_qs(urlsplit(get.call_args.args[0]).query)['param'][0])
        self.assertEqual(parameter['param']['cmsArticleWebOld']['sort'], 'time')

    def test_missing_dimension_gets_bounded_shorter_query_after_model_finishes(self):
        from backend.ai_analysis import web_research as web
        count = 0
        def complete(c, b):
            nonlocal count
            count += 1
            return provider.ProviderResult(200,'','',None,0,0,1,
                [call('search_public_web',query='too many fuel inventory dates',dimension='fundamentals')] if count == 1 else [])
        candidate = {'url':'https://finance.eastmoney.com/a/1','title':'燃料油库存'}
        def search(query, dimension, **kw):
            if query == '燃料油':
                return [candidate]
            return []
        with patch.object(web, 'search_public_web', side_effect=search), patch.object(web,'latest_official_macro',return_value=[]), patch.object(web,'read_public_page',return_value=dict(candidate,excerpt='燃料油供应库存下降',published_at='2026-10-01')):
            materials = web.run_research(self.snapshot, self.config, complete)
        self.assertNotIn('fundamentals', materials['missing_dimensions'])
        self.assertLessEqual(materials['research']['search_count'], web.MAX_SEARCHES)
        self.assertLessEqual(materials['research']['pages_read'], web.MAX_READS)
        self.assertEqual(materials['research']['rounds'], 3)

    def test_old_bing_article_does_not_hide_current_fallback(self):
        from backend.ai_analysis import web_research as web
        rss = '<rss><channel><item><title>燃料油库存</title><link>https://finance.eastmoney.com/a/2026070112345.html</link></item></channel></rss>'
        fallback = json.dumps({'result':{'cmsArticleWebOld':[{'url':'https://finance.eastmoney.com/a/2026100112345.html','title':'燃料油库存','content':'燃料油供应','date':'2026-10-01'}]}})
        with patch.object(web,'public_get',side_effect=[(rss,'https://www.bing.com'),('{"code":0,"data":{"list":[]}}','https://search.sina.com.cn/api/news'),(fallback,'https://search-api-web.eastmoney.com')]):
            rows = web.search_public_web('燃料油库存','fundamentals',instrument='FU2611',cutoff=datetime(2026,10,2,12))
        self.assertEqual(len(rows), 1)
        self.assertIn('/20261001', rows[0]['url'])

    def test_all_dimensions_covered_despite_recovered_search_error(self):
        from backend.ai_analysis import web_research as web
        count = 0
        def complete(c,b):
            nonlocal count
            count += 1
            return provider.ProviderResult(200,'','',None,0,0,1,[call('search_public_web',query='too specific',dimension='fundamentals')] if count == 1 else [])
        def search(query, dimension, **kw):
            if query == 'too specific':
                raise ValueError('No candidates')
            return [{'url':f'https://finance.eastmoney.com/{dimension}','title':'燃料油 原油库存政策 ' + dimension}]
        macro = {'url':'https://www.stats.gov.cn/macro','title':'宏观 PMI','source_kind':'OFFICIAL','latest_release':True,'published_at':'2026-09-30'}
        with patch.object(web,'search_public_web',side_effect=search), patch.object(web,'latest_official_macro',return_value=[macro]), patch.object(web,'read_public_page',side_effect=lambda candidate,timeout:dict(candidate,excerpt='燃料油 原油库存政策 宏观 PMI 正文',published_at='2026-10-01')):
            materials = web.run_research(self.snapshot,self.config,complete)
        self.assertEqual(materials['missing_dimensions'], [])
        self.assertEqual(materials['status'], 'AVAILABLE')
        self.assertTrue(materials['errors'])

    def test_tool_roundtrip_and_supplementary_search(self):
        from backend.ai_analysis import web_research as web
        results = [provider.ProviderResult(200, '', '', None, 1, 1, 1, [call('search_public_web', query='fuel inventory Singapore', dimension='fundamentals')]),
                   provider.ProviderResult(200, '', '', None, 1, 1, 1, [call('read_public_page', candidate_id='C1')]),
                   provider.ProviderResult(200, '', '', None, 1, 1, 1, [call('search_public_web', query='oil sanctions', dimension='events')])]
        bodies = []
        def complete(config, body):
            self.assertLessEqual(config['timeout_seconds'], 30)
            bodies.append(body)
            return results.pop(0)
        with patch.object(web, 'search_public_web', return_value=[{'url': 'https://futures.eastmoney.com/a/1', 'title': '燃料油库存', 'excerpt': '发现'}]), patch.object(web, 'read_public_page', return_value={'url': 'https://futures.eastmoney.com/a/1', 'title': '燃料油库存', 'excerpt': '库存下降正文', 'published_at': '2026-10-01'}):
            materials = web.run_research(self.snapshot, dict(self.config, _research_budget={'searches':2}), complete)
        self.assertEqual(materials['research']['search_count'], 2)
        self.assertEqual(materials['research']['pages_read'], 2)
        self.assertEqual(materials['sources'][0]['content_type'], 'BODY')
        self.assertTrue(any(message['role']=='tool' for message in bodies[1]['messages']))

    def test_model_text_cannot_fabricate_sources(self):
        from backend.ai_analysis import web_research as web
        materials = web.run_research(self.snapshot, self.config, lambda c,b: provider.ProviderResult(200,'','I searched and found sources',None,0,0,1))
        self.assertEqual(materials['sources'], [])
        self.assertEqual(materials['research']['status'], 'UNAVAILABLE')
        self.assertEqual(materials['research']['search_count'], 0)

    def test_private_urls_rejected_before_http(self):
        from backend.ai_analysis import web_research as web
        for url in ['http://example.com', 'https://user:pw@example.com/', 'https://finance.eastmoney.com:444', 'https://127.0.0.1/', 'https://[::1]/']:
            with self.assertRaises((ValueError, provider.ProviderHTTPError)):
                web.public_get(url, 2)

    def test_page_metadata_and_body(self):
        from backend.ai_analysis import web_research as web
        page = '<html><meta property="article:published_time" content="2026-10-01T10:00:00+08:00"><nav>广告</nav><article>库存下降<script>恶意</script><p>正文</p></article></html>'
        with patch.object(web, 'public_get', return_value=(page, 'https://finance.eastmoney.com/a')):
            row = web.read_public_page({'url': 'https://finance.eastmoney.com/a', 'title': '燃料油库存'}, 2)
        self.assertEqual(row['excerpt'], '库存下降 正文')
        self.assertEqual(row['published_at'], '2026-10-01T10:00:00+08:00')
        self.assertEqual(row['data_period'], 'UNKNOWN')

    def test_provider_extracts_tool_only_chat_and_gemini(self):
        import httpx
        cases = [(provider.chat_completion, {'choices': [{'message': {'tool_calls': [call('latest_official_macro')]}}]}),
                 (provider.gemini_generate_content, {'candidates': [{'content': {'parts': [{'functionCall': {'name': 'latest_official_macro', 'args': {}}}]}}]})]
        for method, payload in cases:
            with patch.object(provider, '_post_json', return_value=(httpx.Response(200,json=payload), 1)):
                result = method('https://finance.eastmoney.com','model',None,{},2)
            self.assertEqual(result.content, '')
            self.assertEqual(result.tool_calls[0]['function']['name'], 'latest_official_macro')

    def test_unknown_old_and_future_publications_never_current_sources(self):
        from backend.ai_analysis import web_research as web
        for date, background in [('UNKNOWN', 1), ('2026-01-01', 1), ('2026-10-03', 0)]:
            replies = [[call('search_public_web', query='inventory', dimension='fundamentals')], [call('read_public_page', candidate_id='C1')], []]
            with patch.object(web, 'search_public_web', return_value=[{'url':'https://finance.eastmoney.com/a','title':'Fuel oil inventory'}]), patch.object(web, 'read_public_page', return_value={'url':'https://finance.eastmoney.com/a','title':'Fuel oil inventory','excerpt':'Actual article','published_at':date}):
                materials = web.run_research(self.snapshot,self.config,lambda c,b:provider.ProviderResult(200,'','',None,0,0,1,replies.pop(0)))
            self.assertEqual(materials['sources'], [])
            self.assertEqual(len(materials['background_sources']), background)
            self.assertIn('fundamentals', materials['missing_dimensions'])

    def test_gemini_native_tool_responses_and_model_identity(self):
        from backend.ai_analysis import web_research as web
        bodies = []
        def complete(config, body):
            bodies.append(body)
            tools = [call('latest_official_macro')] if len(bodies)==1 else []
            return provider.ProviderResult(200,'','',None,0,0,1,tools)
        with patch.object(web,'latest_official_macro',return_value=[]), patch.object(web,'search_public_web',return_value=[]):
            materials = web.run_research(self.snapshot,dict(self.config,provider='google_gemini'),complete)
        self.assertIn('functionDeclarations', bodies[0]['tools'][0])
        self.assertEqual(bodies[1]['contents'][-1]['parts'][0]['functionResponse']['name'], 'latest_official_macro')
        self.assertIn('Missing evidence dimensions',bodies[1]['contents'][-1]['parts'][-1]['text'])
        self.assertEqual(materials['research']['model_config_id'], 'm1')

    def test_invalid_and_excess_calls_do_not_execute_http(self):
        from backend.ai_analysis import web_research as web
        replies = [[call('search_public_web',query='oil',dimension='events')]*20]*3
        with patch.object(web, 'search_public_web', return_value=[]) as search:
            materials = web.run_research(self.snapshot,self.config,lambda c,b:provider.ProviderResult(200,'','',None,0,0,1,replies.pop(0)))
        self.assertEqual(search.call_count, 8)
        self.assertEqual(materials['research']['rounds'], 3)
        with patch.object(web, 'read_public_page') as reader:
            materials = web.run_research(self.snapshot,self.config,lambda c,b:provider.ProviderResult(200,'','',None,0,0,1,[call('read_public_page',candidate_id='https://localhost/a')]))
        reader.assert_not_called()
        self.assertEqual(materials['research']['pages_read'], 0)

    def test_partial_evidence_survives_model_failure_and_secrets_redacted(self):
        from backend.ai_analysis import web_research as web
        replies = [[call('search_public_web',query='oil',dimension='events')],[call('read_public_page',candidate_id='C1')]]
        def complete(c,b):
            if not replies:
                raise RuntimeError('api_key=secret secret')
            return provider.ProviderResult(200,'','',None,0,0,1,replies.pop(0))
        with patch.object(web,'search_public_web',return_value=[{'url':'https://finance.eastmoney.com/a','title':'Oil'}]),patch.object(web,'read_public_page',return_value={'url':'https://finance.eastmoney.com/a','title':'Oil','excerpt':'Article','published_at':'2026-10-01'}):
            materials=web.run_research(self.snapshot,dict(self.config,api_key='secret'),complete)
        self.assertEqual(materials['research']['status'],'PARTIAL')
        self.assertEqual(len(materials['sources']),1)
        self.assertNotIn('secret',json.dumps(materials))

    def test_zero_budget_never_calls_model(self):
        from backend.ai_analysis import web_research as web
        from unittest.mock import Mock
        complete = Mock()
        materials = web.run_research(self.snapshot,self.config,complete,time_limit=0)
        complete.assert_not_called()
        self.assertEqual(materials['research']['status'],'TIMEOUT')

    def test_stream_limits_and_private_redirect(self):
        from backend.ai_analysis import web_research as web
        import httpx
        import ipaddress
        client_class = httpx.AsyncClient
        responses = [httpx.Response(302,headers={'location':'https://127.0.0.1/'})]
        with patch.object(provider,'resolve_host_addresses',side_effect=lambda host,port:[ipaddress.ip_address('127.0.0.1' if host=='127.0.0.1' else '8.8.8.8')]),patch.object(web.httpx,'AsyncClient',side_effect=lambda **kw:client_class(transport=httpx.MockTransport(lambda req:responses.pop(0)))):
            with self.assertRaises(provider.ProviderHTTPError):
                web.public_get('https://finance.eastmoney.com/a',2)

    def test_shared_budget_clamps_rounds_and_queries(self):
        from backend.ai_analysis import web_research as web
        config=dict(self.config,_research_budget={'rounds':1,'searches':1,'reads':0})
        with patch.object(web,'search_public_web',return_value=[]) as search:
            materials=web.run_research(self.snapshot,config,lambda c,b:provider.ProviderResult(200,'','',None,0,0,1,[call('search_public_web',query='oil',dimension='events')]*4))
        self.assertEqual(materials['research']['rounds'],1)
        self.assertEqual(search.call_count,1)

    def test_general_search_and_transparent_dynamic_fallback(self):
        from backend.ai_analysis import web_research as web
        documents=[('<rss><channel/></rss>','https://www.bing.com/search'), ('{"code":0,"data":{"list":[]}}','https://search.sina.com.cn/api/news'),
                   ('jQuery('+json.dumps({'result':{'cmsArticleWebOld':[{'url':'https://finance.eastmoney.com/a/1','title':'供需','content':'库存','date':'2026-10-01'}]}})+')','https://search-api-web.eastmoney.com/search/jsonp')]
        with patch.object(web,'public_get',side_effect=documents) as get:
            rows=web.search_public_web('Singapore bunker stocks','fundamentals',10)
        self.assertIn('www.bing.com/search',get.call_args_list[0].args[0])
        from urllib.parse import parse_qs,urlsplit
        parameter=json.loads(parse_qs(urlsplit(get.call_args_list[2].args[0]).query)['param'][0])
        self.assertEqual(parameter['keyword'],'Singapore bunker stocks')
        self.assertIn('Bing',rows[0]['search_warning'])

    def test_recovered_search_is_fallback_not_error(self):
        from backend.ai_analysis import web_research as web
        replies = [[call('search_public_web',query='oil',dimension='events')],
                   [call('read_public_page',candidate_id='C1')], []]
        candidate = {'url':'https://finance.eastmoney.com/a/1','title':'Oil',
                     'search_provider':'Eastmoney public search',
                     'search_warning':'Bing RSS returned no usable candidates'}
        with patch.object(web,'search_public_web',return_value=[candidate]), patch.object(web,'read_public_page',return_value={
                'url':candidate['url'],'title':'Oil','excerpt':'Article','published_at':'2026-10-01'}):
            materials = web.run_research(self.snapshot,self.config,lambda c,b:provider.ProviderResult(200,'','',None,0,0,1,replies.pop(0)))
        self.assertEqual(materials['errors'], [])
        self.assertEqual(materials['research']['status'], 'COMPLETED')
        self.assertTrue(any(event['type']=='FALLBACK' for event in materials['research']['events']))
        self.assertIn('fundamentals',materials['missing_dimensions'])

    def test_early_finish_reads_missing_dimensions_with_retry(self):
        from backend.ai_analysis import web_research as web
        tools = [call('search_public_web',query=dimension,dimension=dimension)
                 for dimension in ('fundamentals','events','related_markets')]
        bodies, reads = [], []
        def complete(c,b):
            bodies.append(b)
            return provider.ProviderResult(200,'','',None,0,0,1,tools if len(bodies)==1 else [])
        def search(query,dimension,timeout,instrument=None,cutoff=None,events=None):
            return [{'url':f'https://finance.eastmoney.com/{dimension}/{i}','title':'燃料油 '+dimension} for i in range(2)]
        def read(candidate,timeout):
            reads.append(candidate['url'])
            if candidate['url'].endswith('/0'):
                raise ValueError('Unreadable page')
            return dict(candidate,excerpt='Verified body',published_at='2026-10-01')
        with patch.object(web,'search_public_web',side_effect=search),patch.object(web,'read_public_page',side_effect=read):
            materials=web.run_research(self.snapshot,dict(self.config,_research_budget={'searches':3}),complete)
        self.assertEqual({row['dimension'] for row in materials['sources']}, {'fundamentals','events','related_markets'})
        self.assertEqual(materials['missing_dimensions'], ['macro'])
        self.assertEqual(materials['research']['pages_read'],6)
        self.assertEqual(len(set(reads)),6)
        self.assertIn('Missing evidence dimensions',bodies[1]['messages'][-1]['content'])

    def test_automatic_reads_share_budget_and_reject_stale_evidence(self):
        from backend.ai_analysis import web_research as web
        count = 0
        def complete(c,b):
            nonlocal count
            count += 1
            return provider.ProviderResult(200,'','',None,0,0,1,
                [call('search_public_web',query='oil',dimension='events')] if count==1 else [])
        candidates=[{'url':f'https://finance.eastmoney.com/{i}','title':'Oil'} for i in range(8)]
        with patch.object(web,'search_public_web',return_value=candidates),patch.object(web,'read_public_page',side_effect=lambda c,timeout:dict(c,excerpt='Old body '+c['url'],published_at='2026-01-01')) as read:
            materials=web.run_research(self.snapshot,dict(self.config,_research_budget={'reads':2}),complete)
        self.assertEqual(read.call_count,2)
        self.assertEqual(materials['research']['pages_read'],2)
        self.assertEqual(materials['sources'],[])
        self.assertIn('events',materials['missing_dimensions'])
        self.assertEqual(len(materials['background_sources']),2)

    def test_early_finish_requests_revised_search_when_no_candidates(self):
        from backend.ai_analysis import web_research as web
        replies = [[call('search_public_web',query='oil',dimension='events')], [],
                   [call('search_public_web',query='oil sanctions latest',dimension='events')]]
        candidates = [{'url':'https://finance.eastmoney.com/news','title':'Oil'}]
        with patch.object(web,'search_public_web',side_effect=[[],candidates]) as search, patch.object(web,'read_public_page',return_value={
                'url':'https://finance.eastmoney.com/news','title':'Oil','excerpt':'Actual body','published_at':'2026-10-01'}):
            materials=web.run_research(self.snapshot,dict(self.config,_research_budget={'searches':2}),lambda c,b:provider.ProviderResult(200,'','',None,0,0,1,replies.pop(0)))
        self.assertEqual(search.call_count,2)
        self.assertEqual(search.call_args.args[0],'oil sanctions latest')
        self.assertEqual(materials['sources'][0]['dimension'],'events')

    def test_automatic_read_timeout_stops_further_requests(self):
        from backend.ai_analysis import web_research as web
        replies = [[call('search_public_web',query='oil',dimension='events')], []]
        with patch.object(web,'search_public_web',return_value=[{'url':f'https://finance.eastmoney.com/{i}','title':'Oil'} for i in range(3)]), patch.object(web,'read_public_page',side_effect=TimeoutError('Research time budget exhausted')) as read:
            materials=web.run_research(self.snapshot,self.config,lambda c,b:provider.ProviderResult(200,'','',None,0,0,1,replies.pop(0)))
        self.assertEqual(read.call_count,1)
        self.assertEqual(materials['research']['status'],'TIMEOUT')
        self.assertEqual(materials['sources'],[])

    def test_unrelated_candidates_are_never_read_or_promoted(self):
        from backend.ai_analysis import web_research as web
        candidates = [
            {'url':'https://www.52pojie.cn/thread-1.html','title':'百度网盘电脑版去广告','excerpt':'燃料油'},
            {'url':'https://zhidao.baidu.com/question/1','title':'原油行情'},
            {'url':'https://finance.eastmoney.com/a/1','title':'纯碱供应恢复','excerpt':'纯碱厂检修'},
            {'url':'https://finance.eastmoney.com/a/2','title':'燃料油库存下降','excerpt':'船用燃料油供需'},
        ]
        count = 0
        def complete(c,b):
            nonlocal count
            count += 1
            return provider.ProviderResult(200,'','',None,0,0,1,
                [call('search_public_web',query='燃料油库存',dimension='fundamentals')] if count==1 else [])
        with patch.object(web,'search_public_web',return_value=candidates), patch.object(web,'read_public_page',return_value={
                'url':candidates[-1]['url'],'title':'燃料油库存下降','excerpt':'船燃库存下降正文','published_at':'2026-10-01'}) as reader:
            materials=web.run_research(self.snapshot,dict(self.config,_research_budget={'searches':1}),complete)
        self.assertEqual(reader.call_count,1)
        self.assertEqual(reader.call_args.args[0]['url'],candidates[-1]['url'])
        self.assertEqual(len(materials['sources']),1)

    def test_bing_forum_results_trigger_professional_fallback(self):
        from backend.ai_analysis import web_research as web
        rss='<rss><channel><item><title>百度网盘</title><link>https://www.52pojie.cn/thread-1.html</link></item></channel></rss>'
        fallback='jQuery('+json.dumps({'result':{'cmsArticleWebOld':[{'url':'https://finance.eastmoney.com/a/1','title':'燃料油库存','content':'船燃供需'}]}})+')'
        with patch.object(web,'public_get',side_effect=[(rss,'https://www.bing.com/search'),('{"code":0,"data":{"list":[]}}','https://search.sina.com.cn/api/news'),(fallback,'https://search-api-web.eastmoney.com/')]) as get:
            rows=web.search_public_web('燃料油库存','fundamentals')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['search_provider'],'Eastmoney public search')
        from urllib.parse import parse_qs,urlsplit
        query=parse_qs(urlsplit(get.call_args_list[0].args[0]).query)['q'][0]
        self.assertIn('site:',query)

    def test_professional_reader_blocks_forum_redirect_before_request(self):
        from backend.ai_analysis import web_research as web
        import httpx,ipaddress
        client_class=httpx.AsyncClient
        requests=[]
        def respond(request):
            requests.append(request)
            return httpx.Response(302,headers={'location':'https://www.52pojie.cn/thread-1.html'})
        with patch.object(provider,'resolve_host_addresses',return_value=[ipaddress.ip_address('8.8.8.8')]), patch.object(web.httpx,'AsyncClient',side_effect=lambda **kw:client_class(transport=httpx.MockTransport(respond))):
            with self.assertRaises(ValueError):
                web.read_public_page({'url':'https://finance.eastmoney.com/a/1','title':'燃料油'},2)
        self.assertEqual(len(requests),1)

    def test_bing_wrong_commodity_also_triggers_fallback(self):
        from backend.ai_analysis import web_research as web
        rss='<rss><channel><item><title>纯碱供应恢复</title><link>https://finance.eastmoney.com/a/1</link></item></channel></rss>'
        fallback=json.dumps({'result':{'cmsArticleWebOld':[{'url':'https://finance.eastmoney.com/a/2','title':'燃料油库存','content':'船燃供需'}]}})
        with patch.object(web,'public_get',side_effect=[(rss,'https://www.bing.com/search'),('{"code":0,"data":{"list":[]}}','https://search.sina.com.cn/api/news'),(fallback,'https://search-api-web.eastmoney.com/')]):
            rows=web.search_public_web('燃料油库存','fundamentals',instrument='FU2611')
        self.assertEqual(rows[0]['url'],'https://finance.eastmoney.com/a/2')

    def test_unrelated_body_cannot_become_evidence(self):
        from backend.ai_analysis import web_research as web
        replies=[[call('search_public_web',query='燃料油库存',dimension='fundamentals')],[],[]]
        with patch.object(web,'search_public_web',return_value=[{'url':'https://finance.eastmoney.com/a/1','title':'燃料油库存'}]), patch.object(web,'read_public_page',return_value={
                'url':'https://finance.eastmoney.com/a/1','title':'纯碱日报','excerpt':'纯碱厂检修结束','published_at':'2026-10-01'}):
            materials=web.run_research(self.snapshot,dict(self.config,_research_budget={'searches':1}),lambda c,b:provider.ProviderResult(200,'','',None,0,0,1,replies.pop(0)))
        self.assertEqual(materials['sources'],[])
        self.assertEqual(materials['background_sources'],[])
        self.assertTrue(any(e['type']=='FILTER' for e in materials['research']['events']))

    def test_professional_host_must_match_domain_boundary(self):
        from backend.ai_analysis import web_research as web
        for url in ('https://finance.eastmoney.com.evil.example/a','https://fake-shfe.com.cn/a'):
            with self.assertRaises(ValueError):
                web._professional_url(url)

    def test_http_403_reports_original_publisher_and_alternative_read_continues(self):
        from backend.ai_analysis import web_research as web
        import httpx,ipaddress
        client_class=httpx.AsyncClient
        with patch.object(provider,'resolve_host_addresses',return_value=[ipaddress.ip_address('172.66.165.81')]), patch.object(web.httpx,'AsyncClient',side_effect=lambda **kw:client_class(transport=httpx.MockTransport(lambda req:httpx.Response(403)))):
            with self.assertRaises(ValueError) as caught:
                web.read_public_page({'url':'https://www.opec.org/news/1','title':'OPEC oil'},2)
        message=str(caught.exception)
        self.assertIn('https://www.opec.org/news/1',message)
        self.assertIn('403',message)
        self.assertNotIn('172.66.165.81',message)
        replies=[[call('search_public_web',query='OPEC oil',dimension='events')],[],[]]
        candidates=[{'url':'https://www.opec.org/news/1','title':'OPEC oil'},
                    {'url':'https://finance.eastmoney.com/a/2','title':'原油 OPEC 产量'}]
        with patch.object(web,'search_public_web',return_value=candidates),patch.object(web,'read_public_page',side_effect=[ValueError(message),{
                'url':candidates[1]['url'],'title':'原油 OPEC 产量','excerpt':'原油产量正文','published_at':'2026-10-01'}]):
            materials=web.run_research(self.snapshot,self.config,lambda c,b:provider.ProviderResult(200,'','',None,0,0,1,replies.pop(0)))
        self.assertEqual(len(materials['sources']),1)
        self.assertEqual(materials['sources'][0]['url'],candidates[1]['url'])
        self.assertIn('https://www.opec.org/news/1',materials['errors'][0]['reason'])

    def test_publisher_http_redirect_retries_https_only(self):
        from backend.ai_analysis import web_research as web
        import httpx
        import ipaddress
        client_class = httpx.AsyncClient
        for target, allowed in [('http://finance.eastmoney.com/a/1', True),
                                ('http://example.com/a/1', False),
                                ('http://user:pw@finance.eastmoney.com/a/1', False),
                                ('http://finance.eastmoney.com:8080/a/1', False)]:
            requests = []
            def respond(request):
                requests.append(request)
                return httpx.Response(302,headers={'location':target}) if len(requests)==1 else httpx.Response(200,text='Article')
            with patch.object(provider,'resolve_host_addresses',return_value=[ipaddress.ip_address('8.8.8.8')]), patch.object(web.httpx,'AsyncClient',side_effect=lambda **kw:client_class(transport=httpx.MockTransport(respond))):
                if allowed:
                    body, url = web.public_get('https://futures.eastmoney.com/a/1',2)
                    self.assertEqual(url,'https://finance.eastmoney.com/a/1')
                    self.assertEqual(body,'Article')
                    self.assertEqual(len(requests),2)
                    self.assertTrue(all(request.url.scheme=='https' for request in requests))
                else:
                    with self.assertRaises(ValueError):
                        web.public_get('https://futures.eastmoney.com/a/1',2)
                    self.assertEqual(len(requests),1)

    def test_public_dns_is_bounded_by_total_deadline(self):
        from backend.ai_analysis import web_research as web
        import time
        def slow_dns(*args):
            time.sleep(0.2)
            return []
        started=time.monotonic()
        with patch.object(provider,'resolve_host_addresses',side_effect=slow_dns):
            with self.assertRaises((TimeoutError,provider.ProviderHTTPError)):
                web.public_get('https://finance.eastmoney.com',0.03)
        self.assertLess(time.monotonic()-started,0.15)

    def test_nonofficial_macro_is_background_even_when_current(self):
        from backend.ai_analysis import web_research as web
        replies=[[call('search_public_web',query='macro outlook',dimension='macro')],[call('read_public_page',candidate_id='C1')],[]]
        with patch.object(web,'search_public_web',return_value=[{'url':'https://finance.eastmoney.com/a','title':'Macro'}]),patch.object(web,'read_public_page',return_value={'url':'https://finance.eastmoney.com/a','title':'Macro','excerpt':'Macro body','published_at':'2026-10-01'}):
            materials=web.run_research(self.snapshot,self.config,lambda c,b:provider.ProviderResult(200,'','',None,0,0,1,replies.pop(0)))
        self.assertEqual(materials['sources'],[])
        self.assertEqual(len(materials['background_sources']),1)
        self.assertIn('macro',materials['missing_dimensions'])

    def test_official_redirect_cannot_keep_official_latest_label(self):
        from backend.ai_analysis import web_research as web
        replies=[[call('latest_official_macro')],[call('read_public_page',candidate_id='C1')],[]]
        candidate={'url':'https://www.stats.gov.cn/a','title':'Macro','source_kind':'OFFICIAL','latest_release':True,'published_at':'2026-10-01'}
        with patch.object(web,'latest_official_macro',return_value=[candidate]),patch.object(web,'search_public_web',return_value=[]),patch.object(web,'read_public_page',return_value={'url':'https://finance.eastmoney.com/a','title':'Macro','excerpt':'Macro body','published_at':'UNKNOWN'}):
            materials=web.run_research(self.snapshot,self.config,lambda c,b:provider.ProviderResult(200,'','',None,0,0,1,replies.pop(0)))
        self.assertEqual(materials['sources'],[])
        self.assertEqual(materials['background_sources'][0]['source_kind'],'NEWS')
        self.assertEqual(materials['background_sources'][0]['published_at'],'UNKNOWN')

    def test_model_tool_request_total_http_deadline(self):
        import asyncio
        import time
        import httpx
        import ipaddress
        client_class=httpx.AsyncClient
        async def slow_response(request):
            await asyncio.sleep(.2)
            return httpx.Response(200,json={})
        started=time.monotonic()
        with patch.object(provider,'resolve_host_addresses',return_value=[ipaddress.ip_address('8.8.8.8')]),patch.object(provider.httpx,'AsyncClient',side_effect=lambda **kw:client_class(transport=httpx.MockTransport(slow_response))):
            with self.assertRaises(provider.ProviderHTTPError):
                provider._post_json('https://finance.eastmoney.com/chat/completions',{}, {'tools':[{}]},.03,False,None)
        self.assertLess(time.monotonic()-started,.15)

    def scoped_research(self, web, completion, **kwargs):
        import inspect
        self.assertIn('topic', inspect.signature(web.run_research).parameters)
        return web.run_research(self.snapshot, self.config, completion, **kwargs)

    def test_macro_topic_rejects_product_tools_and_limits_schema(self):
        from backend.ai_analysis import web_research as web
        bodies = []
        def complete(config, body):
            bodies.append(body)
            calls = [call('search_public_web', query='燃料油', dimension='fundamentals'),
                     call('latest_official_macro')] if len(bodies) == 1 else []
            return provider.ProviderResult(200, '', '', None, 0, 0, 1, calls)
        with patch.object(web, 'search_public_web') as search, patch.object(web, 'latest_official_macro', return_value=[]):
            materials = self.scoped_research(web, complete, topic='macro')
        search.assert_not_called()
        schema = bodies[0]['tools'][0]['function']
        self.assertEqual(schema['parameters']['properties']['dimension']['enum'], ['macro'])
        self.assertEqual(materials['missing_dimensions'], ['macro'])
        self.assertEqual(materials['research']['topic'], 'macro')
        self.assertIsNone(materials['research']['product_code'])
        self.assertTrue(any('scope' in row['reason'].lower() for row in materials['errors']))

    def test_product_topic_excludes_macro_and_uses_topic_commodity(self):
        from backend.ai_analysis import web_research as web
        bodies = []
        def complete(config, body):
            bodies.append(body)
            calls = [call('latest_official_macro'), call('search_public_web', query='PMI', dimension='macro'),
                     call('search_public_web', query='燃料油库存', dimension='fundamentals')] if len(bodies) == 1 else []
            return provider.ProviderResult(200, '', '', None, 0, 0, 1, calls)
        candidate = {'url':'https://finance.eastmoney.com/fu', 'title':'燃料油库存'}
        with patch.object(web, 'search_public_web', return_value=[candidate]) as search, \
             patch.object(web, 'latest_official_macro') as macro, \
             patch.object(web, 'read_public_page', return_value=dict(candidate, excerpt='燃料油供应库存下降', published_at='2026-10-01')):
            self.assertIn('topic', __import__('inspect').signature(web.run_research).parameters)
            materials = web.run_research({'market':dict(self.snapshot['market'], instrument='SC2611')},
                dict(self.config, _research_budget={'searches':1}), complete, topic='product:FU')
        macro.assert_not_called()
        self.assertEqual(search.call_args.kwargs['instrument'], 'FU')
        self.assertNotIn('latest_official_macro', [item['function']['name'] for item in bodies[0]['tools']])
        self.assertNotIn('macro', materials['missing_dimensions'])
        self.assertEqual(materials['sources'][0]['product_code'], 'FU')
        self.assertEqual(materials['sources'][0]['applicable_contracts'], [])

    def test_explicit_dimensions_limit_automatic_supplements(self):
        from backend.ai_analysis import web_research as web
        replies = [[call('search_public_web', query='燃料油政策', dimension='events')], [], []]
        with patch.object(web, 'search_public_web', return_value=[]) as search, patch.object(web, 'latest_official_macro') as macro:
            materials = self.scoped_research(web,
                lambda c,b:provider.ProviderResult(200, '', '', None, 0, 0, 1, replies.pop(0)),
                topic='product:FU', dimensions=['events'])
        macro.assert_not_called()
        self.assertTrue(all(args.args[1] == 'events' for args in search.call_args_list))
        self.assertEqual(materials['missing_dimensions'], ['events'])
        self.assertEqual(materials['research']['dimensions'], ['events'])

    def test_incompatible_dimensions_fail_before_model_call(self):
        from backend.ai_analysis import web_research as web
        from unittest.mock import Mock
        complete = Mock()
        self.assertIn('topic', __import__('inspect').signature(web.run_research).parameters)
        with self.assertRaises(ValueError):
            web.run_research(self.snapshot, self.config, complete, topic='macro', dimensions=['events'])
        complete.assert_not_called()

    def test_full_body_hash_changes_when_same_excerpt_has_different_tail(self):
        from backend.ai_analysis import web_research as web
        import hashlib
        prefix = '燃料油库存下降。' * 600
        candidate = {'url':'https://finance.eastmoney.com/a/1', 'title':'燃料油周报'}
        rows = []
        for tail in ('修订前供应数据', '修订后供应数据'):
            with patch.object(web, 'public_get', return_value=('<article>' + prefix + tail + '</article>', candidate['url'])):
                rows.append(web.read_public_page(candidate))
        self.assertIn('body', rows[0])
        self.assertEqual(rows[0]['body'], prefix + '修订前供应数据')
        self.assertEqual(rows[0]['excerpt'], rows[1]['excerpt'])
        self.assertEqual(rows[0]['content_hash'], hashlib.sha256((prefix + '修订前供应数据').encode('utf-8')).hexdigest())
        self.assertNotEqual(rows[0]['content_hash'], rows[1]['content_hash'])

    def test_contract_scope_requires_code_in_real_page(self):
        from backend.ai_analysis import web_research as web
        for text, scope, contracts in [('燃料油仓单通报', 'product', []),
                                       ('燃料油 FU2611 合约交割通知，SC2611仅作为市场参考', 'contract', ['FU2611'])]:
            candidate = {'url':'https://www.shfe.com.cn/a/1', 'title':'搜索声称 FU2611 专项',
                         'dimension':'events', '_instrument':'FU', 'applicable_contracts':['FU2611']}
            document = '<html><title>燃料油通知</title><article>' + text + '</article></html>'
            with patch.object(web, 'public_get', return_value=(document, candidate['url'])):
                row = web.read_public_page(candidate)
            self.assertEqual(row.get('scope'), scope)
            self.assertEqual(row.get('applicable_contracts'), contracts)
            self.assertEqual(row['product_code'], 'FU')

    def test_reader_extracts_only_explicit_data_period_and_applicable_dates(self):
        from backend.ai_analysis import web_research as web
        candidate = {'url':'https://www.shfe.com.cn/a/1', 'title':'燃料油', 'dimension':'events', '_instrument':'FU'}
        document = '<article>燃料油库存，数据期别：2026年9月；适用期：2026年10月5日至2026年10月9日。</article>'
        with patch.object(web, 'public_get', return_value=(document, candidate['url'])):
            row = web.read_public_page(candidate)
        self.assertEqual(row['data_period'], '2026年9月')
        self.assertEqual(row.get('applicable_from'), '2026-10-05')
        self.assertEqual(row.get('applicable_to'), '2026-10-09')
        with patch.object(web, 'public_get', return_value=('<article>2026年9月发布燃料油库存</article>', candidate['url'])):
            unknown = web.read_public_page(candidate)
        self.assertEqual(unknown['data_period'], 'UNKNOWN')
        self.assertIsNone(unknown.get('applicable_from'))

    def test_official_latest_is_selected_per_series_from_index(self):
        from backend.ai_analysis import web_research as web
        document = ('<ul><li><a href="/cpi-old">居民消费价格 CPI</a>2026-09-09</li>'
                    '<li><a href="/news">统计局新闻活动</a>2026-10-02</li>'
                    '<li><a href="/pmi">采购经理指数 PMI</a>2026-09-30</li>'
                    '<li><a href="/cpi-new">居民消费价格 CPI</a>2026-09-10</li></ul>')
        # setUp mocks discovery; use the original function to exercise index parsing.
        self.macro_patch.stop()
        self.addCleanup(self.macro_patch.start)
        with patch.object(web, 'public_get', return_value=(document, 'https://www.stats.gov.cn/sj/zxfb/')), \
             patch.object(web, '_stamp', return_value='2026-10-02T12:00:00+08:00'):
            rows = web.latest_official_macro()
        self.assertEqual({row['url'] for row in rows}, {'https://www.stats.gov.cn/cpi-new', 'https://www.stats.gov.cn/pmi'})
        self.assertEqual({row['macro_series'] for row in rows}, {'CPI', 'PMI'})
        self.assertTrue(all(row['latest_verified_at'] == '2026-10-02T12:00:00+08:00' for row in rows))
        self.assertTrue(all(row['latest_release_index_url'] == 'https://www.stats.gov.cn/sj/zxfb/' for row in rows))

    def test_normalization_keeps_body_scope_and_latest_verification(self):
        from backend.ai_analysis import web_research as web
        candidate = {'url':'https://www.stats.gov.cn/pmi', 'title':'采购经理指数 PMI',
                     'source_kind':'OFFICIAL', 'latest_release':True, 'macro_series':'PMI',
                     'latest_verified_at':'2026-10-02T12:00:00+08:00',
                     'latest_release_index_url':'https://www.stats.gov.cn/sj/zxfb/', 'published_at':'2026-09-30'}
        replies = [[call('latest_official_macro')], [], []]
        with patch.object(web, 'latest_official_macro', return_value=[candidate]), \
             patch.object(web, 'public_get', return_value=('<html><title>采购经理指数 PMI</title><article>2026年9月制造业采购经理指数 PMI 为50.0。</article></html>', candidate['url'])):
            materials = self.scoped_research(web,
                lambda c,b:provider.ProviderResult(200, '', '', None, 0, 0, 1, replies.pop(0)), topic='macro')
        row = materials['sources'][0]
        self.assertIn('body', row)
        self.assertEqual(row['macro_series'], 'PMI')
        self.assertTrue(row['latest_release'])
        self.assertEqual(row['latest_verified_at'], '2026-10-02T12:00:00+08:00')
        self.assertEqual(row['scope'], 'macro')
        self.assertEqual(row['applicable_contracts'], [])
        self.assertEqual(materials['research']['latest_release_checks'][0]['macro_series'], 'PMI')

    def test_no_tools_is_explicitly_distinct_from_success_without_bodies(self):
        from backend.ai_analysis import web_research as web
        no_tools = self.scoped_research(web,
            lambda c,b:provider.ProviderResult(200, '', 'I checked', None, 0, 0, 1), topic='macro')
        self.assertEqual(no_tools['research']['failure_kind'], 'NO_TOOL_CALLS')
        self.assertEqual(no_tools['research']['tool_support'], 'NOT_INVOKED')
        self.assertTrue(no_tools['research']['pause_required'])
        replies = [[call('latest_official_macro')], [], []]
        empty = self.scoped_research(web,
            lambda c,b:provider.ProviderResult(200, '', '', None, 0, 0, 1, replies.pop(0)), topic='macro')
        self.assertEqual(empty['research']['status'], 'COMPLETED')
        self.assertEqual(empty['research']['failure_kind'], 'NO_NEW_BODY')
        self.assertEqual(empty['research']['tool_support'], 'SUPPORTED')
        self.assertFalse(empty['research']['pause_required'])
        self.assertEqual(empty['research']['search_count'], sum(e['type'] == 'SEARCH' and e['status'] == 'RUNNING' for e in empty['research']['events']))
        searches = [e for e in empty['research']['events'] if e['type'] == 'SEARCH']
        self.assertTrue(all(e.get('tool') == 'latest_official_macro' for e in searches))

    def test_repeated_out_of_scope_tools_pause_without_network_requests(self):
        from backend.ai_analysis import web_research as web
        with patch.object(web, 'latest_official_macro') as macro:
            materials = self.scoped_research(web,
                lambda c,b:provider.ProviderResult(200, '', '', None, 0, 0, 1, [call('latest_official_macro')]), topic='product:FU')
        macro.assert_not_called()
        self.assertEqual(materials['research']['failure_kind'], 'NO_VALID_TOOL_CALLS')
        self.assertTrue(materials['research']['pause_required'])
        self.assertEqual(materials['research']['tool_execution_count'], 0)

    def test_unsupported_tools_and_network_failure_have_different_pause_results(self):
        from backend.ai_analysis import web_research as web
        def unsupported(c,b):
            raise provider.ProviderHTTPError('HTTP 400: tools are not supported by this model')
        result = self.scoped_research(web, unsupported, topic='macro')
        self.assertEqual(result['research']['failure_kind'], 'TOOLS_UNSUPPORTED')
        self.assertTrue(result['research']['pause_required'])
        self.assertEqual(result['research']['rounds'], 1)
        replies = [[call('latest_official_macro')], [], []]
        with patch.object(web, 'latest_official_macro', side_effect=ValueError('HTTP 503: unavailable')):
            failed = self.scoped_research(web,
                lambda c,b:provider.ProviderResult(200, '', '', None, 0, 0, 1, replies.pop(0)), topic='macro')
        self.assertEqual(failed['research']['failure_kind'], 'NETWORK_FAILED')
        self.assertFalse(failed['research']['pause_required'])

    def test_background_body_is_preserved_without_normalizing_it_twice(self):
        from backend.ai_analysis import web_research as web
        import hashlib
        candidate = {'url':'https://finance.eastmoney.com/fu', 'title':'燃料油库存'}
        page = '<html><title>燃料油库存</title><article>燃料油 &amp;lt;供应&amp;gt;库存变化</article></html>'
        replies = [[call('search_public_web', query='燃料油库存', dimension='fundamentals')], [], []]
        with patch.object(web, 'search_public_web', return_value=[candidate]), \
             patch.object(web, 'public_get', return_value=(page, candidate['url'])):
            materials = self.scoped_research(web,
                lambda c,b:provider.ProviderResult(200, '', '', None, 0, 0, 1, replies.pop(0)),
                topic='product:FU', dimensions=['fundamentals'])
        row = materials['background_sources'][0]
        self.assertEqual(row['body'], '燃料油 <供应>库存变化')
        self.assertEqual(row['content_hash'], hashlib.sha256('燃料油 <供应>库存变化'.encode('utf-8')).hexdigest())
        self.assertEqual(row['scope'], 'product')
        self.assertEqual(row['product_code'], 'FU')
        self.assertEqual(row['applicable_contracts'], [])

    def test_arbitrary_official_news_cannot_claim_latest_release(self):
        from backend.ai_analysis import web_research as web
        candidate = {'url':'https://www.stats.gov.cn/news', 'title':'宏观 CPI 新闻', 'source_kind':'OFFICIAL',
                     'latest_release':True, 'macro_series':'CPI', 'latest_verified_at':'2026-10-02T12:00:00+08:00'}
        replies = [[call('search_public_web', query='宏观 CPI', dimension='macro')], [], []]
        with patch.object(web, 'search_public_web', return_value=[candidate]), \
             patch.object(web, 'read_public_page', return_value=dict(candidate, body='CPI 新闻正文', excerpt='CPI 新闻正文', published_at='2026-10-01')):
            materials = self.scoped_research(web,
                lambda c,b:provider.ProviderResult(200, '', '', None, 0, 0, 1, replies.pop(0)), topic='macro')
        self.assertEqual(materials['sources'], [])
        self.assertFalse(materials['background_sources'][0]['latest_release'])
        self.assertIn('MACRO_NOT_VERIFIED_LATEST_OFFICIAL', materials['background_sources'][0]['warnings'])

    def test_only_invalid_tool_calls_report_the_actual_reason(self):
        from backend.ai_analysis import web_research as web
        replies = [[call('latest_official_macro')], []]
        materials = self.scoped_research(web,
            lambda c,b:provider.ProviderResult(200, '', '', None, 0, 0, 1, replies.pop(0)), topic='product:FU')
        self.assertEqual(materials['research']['failure_kind'], 'NO_VALID_TOOL_CALLS')
        self.assertEqual(materials['research']['tool_support'], 'SUPPORTED')
        self.assertEqual(materials['research']['tool_execution_count'], 0)
        self.assertTrue(materials['research']['pause_required'])

    def test_discovery_title_cannot_make_unrelated_page_relevant(self):
        from backend.ai_analysis import web_research as web
        candidate = {'url':'https://finance.eastmoney.com/fu', 'title':'燃料油库存'}
        replies = [[call('search_public_web', query='燃料油库存', dimension='fundamentals')], [], []]
        with patch.object(web, 'search_public_web', return_value=[candidate]), \
             patch.object(web, 'public_get', return_value=('<article>纯碱供应恢复，纯碱厂检修结束。</article>', candidate['url'])):
            materials = self.scoped_research(web,
                lambda c,b:provider.ProviderResult(200, '', '', None, 0, 0, 1, replies.pop(0)),
                topic='product:FU', dimensions=['fundamentals'])
        self.assertEqual(materials['sources'], [])
        self.assertEqual(materials['background_sources'], [])


class FundamentalRequirementsTests(unittest.TestCase):
    def test_ap_jd_registered_metrics_recognize_quantified_sentences(self):
        # 2026-10-07 起注册 AP/JD 基本面指标; 期别用资料期兜底(句内无显式期别)
        from backend.ai_analysis.web_research import fundamental_coverage
        rows = [
            {'body': '山东栖霞冷库苹果库存量约为82万吨，环比减少。', 'url': 'https://a/1',
             'data_period': '2026年9月', 'published_at': '2026-09-28', 'source_kind': 'NEWS'},
            {'body': '产区苹果套袋量同比减少约5%，产量预期下调。', 'url': 'https://a/3',
             'data_period': '2026年6月', 'published_at': '2026-06-30', 'source_kind': 'NEWS'},
            {'body': '9月全国鸡蛋在产蛋鸡存栏量约12.8亿只，环比基本持平。', 'url': 'https://a/2',
             'data_period': '2026年9月', 'published_at': '2026-09-28', 'source_kind': 'NEWS'},
            {'body': '9月鸡蛋销量环比增加3%，需求稳中向好。', 'url': 'https://a/5',
             'data_period': '2026年9月', 'published_at': '2026-09-28', 'source_kind': 'NEWS'},
        ]
        ap = {item['metric']: item['status'] for item in fundamental_coverage(rows, 'AP2701')}
        self.assertEqual(ap.get('冷库库存'), 'AVAILABLE')
        self.assertEqual(ap.get('产量/供应'), 'AVAILABLE')
        jd = {item['metric']: item['status'] for item in fundamental_coverage(rows, 'JD2612')}
        self.assertEqual(jd.get('存栏'), 'AVAILABLE')
        self.assertEqual(jd.get('销量/需求'), 'AVAILABLE')

    def test_price_only_or_other_product_sentences_do_not_qualify(self):
        # 名称门: 其他品种的量化句不算本品种证据 (回归保障)
        from backend.ai_analysis.web_research import fundamental_coverage
        rows = [{'body': '9月全国鸡蛋在产蛋鸡存栏量约12.8亿只。', 'url': 'https://a/2',
                 'data_period': '2026年9月', 'published_at': '2026-09-28', 'source_kind': 'NEWS'}]
        bu = {item['metric']: item['status'] for item in fundamental_coverage(rows, 'BU2611')}
        self.assertEqual(bu.get('炼厂开工/产量'), 'MISSING')
