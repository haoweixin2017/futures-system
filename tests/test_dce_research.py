"""DCE API acceptance using the saved official response, without live calls."""
import copy
import asyncio
import ipaddress
import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tests import test_bootstrap
from backend.ai_analysis import fixed_research as fixed, future_trend as trend, web_research as web
from backend.ai_analysis import research_cache as cache


FIXTURE = Path(__file__).parent / 'fixtures/warehouse/dce_20260930.json'


class DCEResearchTests(unittest.TestCase):
    def parse(self, product='JD', payload=None, date='20260930'):
        payload = payload if payload is not None else json.loads(FIXTURE.read_text(encoding='utf-8'))
        source = next(s for s in fixed.FIXED_SOURCES if s['source_id'] == 'DCE_WAREHOUSE_DAILY')
        with patch('backend.ai_analysis.dce_api.warehouse', return_value=(date, payload)), \
             patch.object(fixed, '_stamp', return_value='2026-10-06T12:00:00'):
            return fixed._parse_dce(source, 'product:' + product, product, fixed.Budget(), 'test', {})

    def test_real_jd_lh_subtotals_and_explicit_zero(self):
        for product, change in (('JD', -204), ('LH', -110)):
            with self.subTest(product=product):
                row = self.parse(product)['rows'][0]
                self.assertEqual(row['warehouse_quantity'], 0)
                self.assertEqual(row['warehouse_unit'], '手')
                self.assertEqual(row['warehouse_totals'][0]['change'], change)
                self.assertEqual(row['published_at'], 'UNKNOWN')
                self.assertEqual(row['data_period'], '2026-09-30')
                self.assertEqual(row['expected_update_by'], '2026-10-08T18:00:00')
                self.assertNotIn('token', json.dumps(row))

    def test_lh_group_children_not_counted_twice(self):
        payload = json.loads(FIXTURE.read_text(encoding='utf-8'))
        for row in payload['data']['entityList']:
            if row.get('varietyOrder') == 'lh':
                row['wbillQty'] = row['lastWbillQty']; row['diff'] = 0
        self.assertEqual(self.parse('LH', payload)['rows'][0]['warehouse_quantity'], 110)

    def test_missing_product_subtotal_date_or_inconsistent_sum_rejected(self):
        original = json.loads(FIXTURE.read_text(encoding='utf-8'))
        for kind in ('product', 'subtotal', 'date', 'sum', 'quantity'):
            payload = copy.deepcopy(original)
            rows = payload['data']['entityList']
            if kind == 'product':
                payload['data']['entityList'] = [r for r in rows if r.get('varietyOrder') != 'jd']
            elif kind == 'subtotal':
                payload['data']['entityList'] = [r for r in rows if not (r.get('varietyOrder') == 'jd' and r['whCodeOrder'] is None)]
            else:
                row = next(r for r in rows if r.get('varietyOrder') == 'jd')
                if kind == 'date': row['genDate'] = '20260929'
                if kind == 'sum': row['lastWbillQty'] += 1
                if kind == 'quantity': row['wbillQty'] = None
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.parse(payload=payload)

    def test_registered_http_source_normalizes_and_freezes_with_narrow_scope(self):
        row = self.parse()['rows'][0]
        now = datetime(2026, 10, 6, 13)
        self.assertTrue(trend._verified_data_period(row, now))
        self.assertEqual(len(web.normalize_with_provenance([row], now)), 1)
        for changes in ({'url': row['url'] + '&extra=1'}, {'source_entry': 'OTHER'},
                        {'latest_release_index_url': 'http://www.dce.com.cn/other'},
                        {'latest_verified_at': '2026-10-06T14:00:00'},
                        {'product_code': 'FU'}, {'data_period': '2026-09-29'}):
            with self.subTest(changes=changes):
                self.assertFalse(trend._verified_data_period(dict(row, **changes), now))
        self.assertTrue(trend._is_stale(row, datetime(2026, 10, 7, 13)))
        self.assertEqual(trend.normalize_materials([dict(row, url='http://www.dce.com.cn/other')], now), [])
        with self.assertRaises(ValueError):
            web._public_url(row['url'])

    def test_collection_requires_credentials_and_budget(self):
        with patch('backend.ai_analysis.dce_api.credentials', return_value=('', '')):
            b = fixed.Budget()
            result = fixed.collect_fixed({}, 'product:JD', b)
        self.assertEqual(result['sources'], [])
        self.assertEqual(b.used['index_checks'], 0)
        self.assertTrue(any(s['reason'] == 'DCE_API_CREDENTIALS_MISSING' for s in result['sources_checked']))
        with patch('backend.ai_analysis.dce_api.credentials', return_value=('key', 'secret')), \
             patch('backend.ai_analysis.dce_api.warehouse') as request:
            result = fixed.collect_fixed({}, 'product:JD', fixed.Budget(reads=0))
        request.assert_not_called()

    def test_persisted_canonical_url_keeps_official_period_valid(self):
        row = self.parse()['rows'][0]
        row.update(url=cache.canonical_url(row['url']), version_fetched_at=row['fetched_at'])
        self.assertEqual(cache._invalid_reasons(row, datetime(2026, 10, 6, 13), 60), [])


class DCEClientTests(unittest.TestCase):
    def setUp(self):
        from backend.ai_analysis import dce_api
        self.api = dce_api
        self.api._token_cache = None

    def test_auth_cached_and_latest_date_uses_get(self):
        responses = [dict(success=True, code=200, data=dict(token='private-token', expiresIn=3600)),
                     dict(success=True, code=200, data=dict(tradeDate='20260930')),
                     dict(success=True, code=200, data=dict(entityList=[]))]
        with patch.object(self.api, 'credentials', return_value=('key', 'private-secret')), \
             patch.object(self.api, '_wire', side_effect=responses + responses[1:]) as wire:
            b = fixed.Budget(); b.try_consume('index_checks'); b.try_consume('reads')
            self.api.warehouse('JD', b)
            b2 = fixed.Budget(); b2.try_consume('index_checks'); b2.try_consume('reads')
            self.api.warehouse('LH', b2)
        self.assertEqual(wire.call_count, 5)
        self.assertEqual(wire.call_args_list[1].args[:2], ('GET', '/forward/publicweb/maxTradeDate'))
        self.assertEqual(b.used['index_checks'], 2)
        self.assertEqual(b2.used['index_checks'], 1)

    def test_business_failure_never_becomes_data_or_leaks_message(self):
        for code in (400, 401, 402, 500, 501):
            self.api._token_cache = None
            with self.subTest(code=code), \
                 patch.object(self.api, 'credentials', return_value=('key', 'private-secret')), \
                 patch.object(self.api, '_wire', return_value=dict(success=False, code=code, msg='private-secret')):
                with self.assertRaises(ValueError) as error:
                    self.api.warehouse('JD', fixed.Budget())
                self.assertNotIn('private-secret', str(error.exception))
                self.assertIn(str(code), str(error.exception))

    def test_no_network_when_deadline_or_auth_budget_exhausted(self):
        for b in (fixed.Budget(seconds=0), fixed.Budget(index_checks=0)):
            with patch.object(self.api, 'credentials', return_value=('key', 'secret')), \
                 patch.object(self.api, '_wire') as wire, self.assertRaises((ValueError, TimeoutError)):
                self.api.warehouse('JD', b)
            wire.assert_not_called()

    def wire(self, handler, budget=None):
        import httpx
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with patch.object(self.api.provider, '_resolve_addresses_async',
                          return_value=[ipaddress.ip_address('218.25.154.72')]), \
             patch.object(self.api.httpx, 'AsyncClient',
                          side_effect=lambda **kw: real_client(transport=transport, **kw)):
            return self.api._wire('GET', self.api.LATEST_PATH, {'apikey': 'private-key'}, None,
                                  budget or fixed.Budget())

    def test_redirect_is_rejected_without_following_or_leaking_key(self):
        import httpx
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private-key'})
        with self.assertRaisesRegex(ValueError, 'HTTP_302') as error:
            self.wire(handler)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.host, '218.25.154.72')
        self.assertEqual(seen[0].headers['host'], 'www.dce.com.cn')
        self.assertNotIn('private-key', str(error.exception))

    def test_decoded_response_limit_and_request_deadline(self):
        import gzip
        import httpx
        with self.assertRaisesRegex(ValueError, 'RESPONSE_TOO_LARGE'):
            self.wire(lambda request: httpx.Response(200, headers={'content-encoding': 'gzip'},
                content=gzip.compress(b'x' * (1024 * 1024 + 1))))
        async def stalled(request):
            await asyncio.sleep(3)
            return httpx.Response(200, json={})
        with self.assertRaisesRegex(ValueError, 'TimeoutError'):
            self.wire(stalled, fixed.Budget(seconds=1.1))

    def test_endpoint_and_private_dns_rejected(self):
        with self.assertRaisesRegex(ValueError, 'ENDPOINT_REJECTED'):
            self.api._wire('GET', 'http://127.0.0.1/', {}, None, fixed.Budget())
        with patch.object(self.api.provider, '_resolve_addresses_async',
                          return_value=[ipaddress.ip_address('127.0.0.1')]), \
             patch.object(self.api.httpx, 'AsyncClient') as client, self.assertRaises(ValueError):
            self.api._wire('GET', self.api.LATEST_PATH, {}, None, fixed.Budget())
        client.assert_not_called()
