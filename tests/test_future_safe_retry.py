import unittest
import httpx
from unittest.mock import AsyncMock
from unittest.mock import patch
from tests import test_bootstrap
from backend.ai_analysis import orchestrator, provider


class SafeRetryTests(unittest.TestCase):
    def test_network_disconnect_is_not_replayed_across_addresses(self):
        original_client = httpx.AsyncClient
        for failure, kind in ((httpx.ConnectError('offline'), 'CONNECT'),
                              (httpx.ReadTimeout('reading'), 'RESPONSE_UNKNOWN'),
                              (httpx.RemoteProtocolError('disconnected'), 'RESPONSE_UNKNOWN')):
            count = []
            def handler(request):
                count.append(request)
                raise failure
            transport = httpx.MockTransport(handler)
            def client(**kwargs):
                return original_client(transport=transport, **kwargs)
            import ipaddress
            with self.subTest(kind=kind), patch.object(provider,'_resolve_addresses_async',
                     AsyncMock(return_value=[ipaddress.ip_address('8.8.8.8'),ipaddress.ip_address('8.8.4.4')])), \
                 patch.object(provider.httpx,'AsyncClient',side_effect=client):
                with self.assertRaises(provider.ProviderHTTPError) as caught:
                    provider.chat_completion('https://example.com/v1','model',None,{},1,safe_mode=True)
                self.assertEqual(caught.exception.error_kind,kind)
                self.assertEqual(len(count),1)

    def config(self):
        return {'_safe_network':True, '_deadline':100.0, 'timeout_seconds':30}

    def test_connect_and_429_retry_once_with_remaining_deadline(self):
        for error in (provider.ProviderHTTPError('断网', error_kind='CONNECT'),
                      provider.ProviderHTTPError('429', status_code=429, retry_after=2)):
            with self.subTest(error=error), patch.object(orchestrator.time, 'perf_counter', side_effect=[10,10,12]), \
                 patch.object(orchestrator.time, 'sleep') as sleep, \
                 patch.object(orchestrator, '_next_call_sequence', side_effect=[1,2]), \
                 patch.object(orchestrator, '_invoke_once', side_effect=[error,'ok']) as call:
                self.assertEqual(orchestrator._invoke('run',1,'SINGLE',self.config(),[]),'ok')
                self.assertEqual(call.call_count,2)
                self.assertEqual(call.call_args.args[3]['timeout_seconds'],30)
                sleep.assert_called_once()

    def test_unknown_response_timeout_auth_and_parse_do_not_retry(self):
        for error in (provider.ProviderHTTPError('读取超时', error_kind='RESPONSE_UNKNOWN'),
                      provider.ProviderHTTPError('认证',status_code=401),
                      provider.ProviderHTTPError('解析',status_code=200)):
            with self.subTest(error=error), patch.object(orchestrator.time,'perf_counter',return_value=10), \
                 patch.object(orchestrator,'_next_call_sequence',return_value=1), \
                 patch.object(orchestrator,'_invoke_once',side_effect=error) as call:
                with self.assertRaises(provider.ProviderHTTPError):
                    orchestrator._invoke('run',1,'SINGLE',self.config(),[])
                self.assertEqual(call.call_count,1)

    def test_retry_after_exceeding_total_deadline_is_not_retried(self):
        with patch.object(orchestrator.time,'perf_counter',return_value=99), \
             patch.object(orchestrator,'_next_call_sequence',return_value=1), \
             patch.object(orchestrator,'_invoke_once',side_effect=provider.ProviderHTTPError('429',status_code=429,retry_after=5)) as call:
            with self.assertRaises(provider.ProviderHTTPError):
                orchestrator._invoke('run',1,'SINGLE',self.config(),[])
            self.assertEqual(call.call_count,1)
            self.assertEqual(call.call_args.args[3]['timeout_seconds'],1)

    def test_future_trend_budget_covers_main_correction_and_review(self):
        # 最坏合法路径: 主调用+引用/身份纠正+复核, 每次 _invoke 还可重试一次 → 预算 (4*votes+2)×timeout
        self.assertEqual(orchestrator._future_trend_budget_seconds(30, 1), 180)
        self.assertEqual(orchestrator._future_trend_budget_seconds(30, 3), 420)
        config = {'_safe_network':True, 'timeout_seconds':30,
                  '_deadline':orchestrator._future_trend_budget_seconds(30, 1)}
        with patch.object(orchestrator.time, 'perf_counter', side_effect=[0, 50, 100]), \
             patch.object(orchestrator, '_next_call_sequence', side_effect=[1, 2, 3]), \
             patch.object(orchestrator, '_invoke_once', side_effect=['main', 'corrected', 'reviewed']) as call:
            self.assertEqual(orchestrator._invoke('run',1,'SINGLE',config,[]), 'main')
            self.assertEqual(orchestrator._invoke('run',2,'CORRECTION',config,[]), 'corrected')
            self.assertEqual(orchestrator._invoke('run',3,'REVIEW',config,[]), 'reviewed')
            self.assertEqual(call.call_count,3)
