"""主备分钟线隔离验证：全部 HTTP 使用固定响应，不触发真实模型或生产写库。"""
import json
import http.client
import socket
import ssl
import unittest
import urllib.error
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from tests import test_bootstrap  # noqa: F401
from backend.storage import eastmoney_history as em, history_backfill as repair
from backend.storage.mysql_client import SessionLocal
from backend.storage.redis_client import RedisClient
from backend.models.database import MarketBarModel


def payload(rows=None,code='fu2701',market=113):
    return json.dumps({'rc':0,'data':{'code':code,'market':market,'klines':rows or [
        '2026-10-13 09:07,4627,4630,4632,4626,1011,46790390,0.13,0.09,4,0,0,183142,57']}})


class SinaOnlyRepairTests(unittest.TestCase):
    def test_success_and_empty_sina_never_call_backup(self):
        for available in (True, False):
            primary = dict(inserted=int(available), missing=int(available),
                           corrected_trading_days=0, corrected_fields=0,
                           auction_fixed=0, available=available)
            with self.subTest(available=available), \
                    patch.object(repair, '_repair_sina_minute_gaps', return_value=primary), \
                    patch.object(repair, '_repair_eastmoney_minutes') as backup:
                result = repair.repair_minute_gaps('LH2701')
                backup.assert_not_called()
                self.assertEqual(result['inserted'], primary['inserted'])
                self.assertEqual(result['fallback_skipped'], 'DISABLED')

    def test_sina_failure_propagates_without_calling_backup(self):
        with patch.object(repair, '_repair_sina_minute_gaps', side_effect=TimeoutError('sina')), \
                patch.object(repair, '_repair_eastmoney_minutes') as backup:
            with self.assertRaisesRegex(TimeoutError, 'sina'):
                repair.repair_minute_gaps('LH2701')
            backup.assert_not_called()


class EastmoneyTransportTests(unittest.TestCase):
    def response(self, raw=None):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = (raw or payload()).encode('utf-8')
        return response

    def test_normal_success_does_not_retry(self):
        with patch.object(em.urllib.request, 'urlopen', return_value=self.response()), patch.object(em.urllib.request, 'build_opener') as build:
            self.assertEqual(len(em.fetch_minute_bars('FU2701')), 1)
        build.assert_not_called()

    def test_disconnected_default_connection_retries_ipv4_and_keeps_provenance(self):
        opener = MagicMock()
        opener.open.return_value = self.response()
        original_resolver = socket.getaddrinfo
        with patch.object(em.urllib.request, 'urlopen', side_effect=http.client.RemoteDisconnected('closed')), patch.object(em.urllib.request, 'build_opener', return_value=opener):
            try:
                rows = em.fetch_minute_bars('FU2701')
            except http.client.RemoteDisconnected:
                self.fail('Default connection closed without trying the IPv4 transport')
        request = opener.open.call_args.args[0]
        self.assertIn('secid=113.fu2701', request.full_url)
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 8)
        provenance = json.loads(rows[0]['provenance_json'])
        self.assertEqual(provenance['request_url'], request.full_url)
        self.assertEqual(provenance['provider'], 'eastmoney')
        self.assertIs(socket.getaddrinfo, original_resolver)

    def test_http_errors_and_invalid_data_are_not_retried(self):
        cases = [urllib.error.HTTPError(em.URL, 429, 'Too many requests', {}, None), None]
        for error in cases:
            with self.subTest(error=error), patch.object(em.urllib.request, 'urlopen', side_effect=error, return_value=self.response(payload(market=114))), patch.object(em.urllib.request, 'build_opener') as build:
                with self.assertRaises((urllib.error.HTTPError, ValueError)):
                    em.fetch_minute_bars('FU2701')
                build.assert_not_called()

    def test_both_transport_failures_propagate_to_existing_sina_fallback(self):
        opener = MagicMock()
        opener.open.side_effect = TimeoutError('IPv4 timeout')
        with patch.object(em.urllib.request, 'urlopen', side_effect=http.client.RemoteDisconnected('closed')), patch.object(em.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(TimeoutError, 'IPv4 timeout'):
                em.fetch_minute_bars('FU2701')
        self.assertEqual(opener.open.call_count, 1)

    def test_ipv4_transport_resolves_per_request_and_retains_tls_hostname_validation(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 443)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.2', 443))]
        connected = object()
        with patch.object(socket, 'getaddrinfo', return_value=addresses) as resolve, patch.object(socket, 'create_connection', side_effect=[OSError('unreachable'), connected]) as connect:
            connection = em._ipv4_https_connection('push2his.eastmoney.com', timeout=8)
            self.assertIs(connection._create_connection((connection.host, 443), 8), connected)
        resolve.assert_called_once_with('push2his.eastmoney.com', 443, socket.AF_INET, socket.SOCK_STREAM)
        self.assertEqual(connect.call_args_list[0].args, (('192.0.2.1', 443), 8, None))
        self.assertEqual(connect.call_args_list[1].args, (('192.0.2.2', 443), 8, None))
        self.assertEqual(connection.host, 'push2his.eastmoney.com')
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)


class EastmoneyTests(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as db:
            db.query(MarketBarModel).delete()
            db.commit()
        RedisClient.get_client().delete('bars:FU2701:1m')

    def bar(self,minute):
        return repair._build_bar('FU2701','1m',datetime(2026,10,13,9,minute),
            Decimal(4600),Decimal(4601),Decimal(4599),Decimal(4600),99,Decimal(180000),Decimal(180000))

    def coverage(self, symbol, start, end, omit=()):
        bars = []
        stamp = start
        while stamp < end:
            if (stamp not in omit and
                    repair.FuturesTradingCalendar.is_trading_time_for(stamp, symbol)):
                bars.append(dict(self.bar(6), instrument_id=symbol, bar_time=stamp,
                                 trading_day=repair.FuturesTradingCalendar.get_trading_day(stamp)))
            stamp += timedelta(minutes=1)
        repair.save_bars_to_mysql(bars)

    def test_complete_database_skips_backup_each_round_despite_sina_zero_counts(self):
        now = datetime(2026,10,13,9,20)
        self.coverage('FU2701', datetime(2026,10,12,21), now - timedelta(minutes=2))
        with patch.object(repair,'fetch_sina_minute_bars',return_value=[self.bar(6)]), patch.object(em,'fetch_minute_bars',side_effect=AssertionError('unnecessary HTTP')) as fetch:
            for _ in range(2):
                report = repair.repair_minute_gaps('FU2701',now)
                self.assertEqual(report['primary_inserted'],0)
                self.assertEqual(report['missing'],0)
                self.assertEqual(report['fallback_skipped'],'DISABLED')
                self.assertNotIn('fallback_error',report)
        fetch.assert_not_called()

    def test_missing_minute_outside_sina_range_stays_missing(self):
        with patch.object(repair,'fetch_sina_minute_bars',return_value=[self.bar(6)]), patch.object(em,'fetch_minute_bars') as fetch:
            report=repair.repair_minute_gaps('FU2701',datetime(2026,10,13,9,20))
        fetch.assert_not_called()
        self.assertEqual(report['fallback_inserted'],0)
        with SessionLocal() as db:self.assertEqual(db.query(MarketBarModel).count(),1)

    def test_coverage_ignores_breaks_weekends_and_recent_minutes(self):
        cases = [('FU2701',datetime(2026,10,12,21),datetime(2026,10,13,13,32)),
                 ('IF2612',datetime(2026,10,12,9,30),datetime(2026,10,13,13,2)),
                 ('T2612',datetime(2026,10,12,9,30),datetime(2026,10,13,15,20)),
                 ('AG2612',datetime(2026,10,9,21),datetime(2026,10,12,9,2))]
        for symbol,start,now in cases:
            with self.subTest(symbol=symbol):
                self.coverage(symbol,start,now-timedelta(minutes=2))
                with patch.object(em,'fetch_minute_bars') as fetch:
                    self.assertEqual(repair._repair_eastmoney_minutes(symbol,now)['skipped'],'NO_MISSING_MINUTES')
                fetch.assert_not_called()

    def test_coverage_opening_guard_expires_for_each_of_first_two_minutes(self):
        start = datetime(2026,10,13,9)
        for minute in (0,1):
            with self.subTest(minute=minute):
                with SessionLocal() as db:
                    db.query(MarketBarModel).delete()
                    db.commit()
                # Prior history keeps the missing opening minute inside coverage.
                self.coverage('FU2701',datetime(2026,10,12,21),start+timedelta(minutes=13),
                              omit=(start+timedelta(minutes=minute),))
                with patch.object(em,'fetch_minute_bars',return_value=[]) as fetch:
                    self.assertIn('skipped',repair._repair_eastmoney_minutes('FU2701',start+timedelta(minutes=14)))
                    fetch.assert_not_called()
                    repair._repair_eastmoney_minutes('FU2701',start+timedelta(minutes=15+minute))
                    fetch.assert_called_once()

    def test_old_history_does_not_expand_scan_beyond_backup_recent_window(self):
        now = datetime(2026,10,15,9,20)
        old = dict(self.bar(6),bar_time=datetime(2020,1,2,9),trading_day='20200102')
        repair.save_bars_to_mysql([old])
        # Fifth trading day is Oct 9; its night session starts Oct 8 at 21:00.
        self.coverage('FU2701',datetime(2026,10,8,21),now-timedelta(minutes=2))
        original = repair.FuturesTradingCalendar.is_trading_time_for
        with patch.object(em,'fetch_minute_bars') as fetch, patch.object(repair.FuturesTradingCalendar,'is_trading_time_for',wraps=original) as check:
            self.assertIn('skipped',repair._repair_eastmoney_minutes('FU2701',now))
        fetch.assert_not_called()
        self.assertTrue(all(call.args[0] >= datetime(2026,10,8,21) for call in check.call_args_list))

    def test_sina_failure_or_empty_never_requests_backup_with_complete_database(self):
        now=datetime(2026,10,13,9,20)
        self.coverage('FU2701',datetime(2026,10,12,21),now-timedelta(minutes=2))
        with patch.object(repair,'fetch_sina_minute_bars',return_value=[]),patch.object(em,'fetch_minute_bars') as fetch:
            self.assertFalse(repair.repair_minute_gaps('FU2701',now)['primary_available'])
            fetch.assert_not_called()
        with patch.object(repair,'fetch_sina_minute_bars',side_effect=TimeoutError('sina')),patch.object(em,'fetch_minute_bars') as fetch:
            with self.assertRaises(TimeoutError):repair.repair_minute_gaps('FU2701',now)
            fetch.assert_not_called()

    def test_real_fixture_normalizes_time_and_preserves_source_values(self):
        row=em.parse_minute_bars('FU2701',payload())[0]
        self.assertEqual(row['bar_time'],datetime(2026,10,13,9,6))
        self.assertEqual(row['volume'],1011)
        self.assertEqual(row['turnover'],Decimal(46790390))
        self.assertEqual(row['open_interest'],Decimal(183142))
        self.assertEqual(row['open_interest_change'],Decimal(57))
        provenance=json.loads(row['provenance_json'])
        self.assertEqual(provenance['provider'],'eastmoney')
        self.assertEqual(provenance['source_role'],'FALLBACK')
        self.assertEqual(provenance['label_semantics'],'START')

    def test_uint32_negative_interest_change_is_decoded(self):
        row=em.parse_minute_bars('FU2701',payload([
            '2026-10-13 09:06,4644,4626,4644,4626,2259,104678000,0,0,0,0,0,183085,4294967252']))[0]
        self.assertEqual(row['open_interest_change'],Decimal(-44))

    def test_contract_market_and_invalid_data_are_rejected(self):
        for raw in (payload(code='bu2701'),payload(market=114),payload(['bad']),
                    payload(['2026-10-13 09:07,NaN,4630,4632,4626,1011,10,0,0,0,0,0,183142,57']),
                    payload(['2026-10-13 09:07,4627,4630,4600,4626,1011,10,0,0,0,0,0,183142,57'])):
            with self.subTest(raw=raw),self.assertRaises(ValueError):
                em.parse_minute_bars('FU2701',raw)
        with self.assertRaises(ValueError):em._secid('FU0')
        self.assertEqual(em._secid('SA2701'),(115,'SA701','CZCE'))
        self.assertEqual(em.parse_minute_bars('SA2701',payload(code='SA701',market=115))[0]['instrument_id'],'SA2701')

    def test_sina_only_repairs_available_minutes(self):
        calls=[]
        primary=[self.bar(5),self.bar(6)]
        fallback=em.parse_minute_bars('FU2701',payload([
            '2026-10-13 09:07,4627,4630,4632,4626,1011,46790390,0,0,0,0,0,183142,57',
            '2026-10-13 09:08,4627,4630,4632,4626,1011,46790390,0,0,0,0,0,183142,57']))
        with patch.object(repair,'fetch_sina_minute_bars',side_effect=lambda *args:calls.append('sina') or primary),patch.object(em,'fetch_minute_bars',side_effect=lambda symbol:calls.append('eastmoney') or fallback):
            first=repair.repair_minute_gaps('FU2701',datetime(2026,10,13,9,20))
            second=repair.repair_minute_gaps('FU2701',datetime(2026,10,13,9,20))
        self.assertEqual(calls,['sina','sina'])
        self.assertEqual(first['primary_source'],'sina')
        self.assertIsNone(first['fallback_source'])
        self.assertEqual(first['primary_inserted'],2)
        self.assertEqual(first['fallback_inserted'],0)
        self.assertTrue(first['primary_available'])
        self.assertEqual(second['inserted'],0)
        self.assertEqual(second['corrected_fields'],0)
        with SessionLocal() as db:
            rows=db.query(MarketBarModel).order_by(MarketBarModel.bar_time).all()
            self.assertEqual([row.bar_time.minute for row in rows],[5,6])
            self.assertEqual(rows[1].volume,99)
            self.assertEqual(rows[1].close_price,Decimal(4600))
            self.assertEqual(json.loads(rows[1].provenance_json)['source_role'],'PRIMARY')
        cached=[json.loads(raw) for raw in RedisClient.get_client().lrange('bars:FU2701:1m',0,-1)]
        self.assertFalse(any(row['bar_time']=='2026-10-13T09:07:00' for row in cached))

    def test_sina_failure_does_not_overwrite_existing_bars(self):
        repair.save_bars_to_mysql([self.bar(6)])
        with patch.object(repair,'fetch_sina_minute_bars',side_effect=TimeoutError('sina')),patch.object(em,'fetch_minute_bars',return_value=em.parse_minute_bars('FU2701',payload())):
            with self.assertRaises(TimeoutError):repair.repair_minute_gaps('FU2701',datetime(2026,10,13,9,20))
        with SessionLocal() as db:
            row=db.query(MarketBarModel).one()
            self.assertEqual(row.close_price,Decimal(4600))
            self.assertEqual(row.volume,99)

    def test_recovered_sina_corrects_backup_prices_and_retains_measured_volume(self):
        repair.save_bars_to_mysql(em.parse_minute_bars('FU2701',payload()))
        # Use a previous trading day too: a later repair must keep the real backup volume.
        for price in (4600,4601):
            source=self.bar(6)
            source.update(open_price=Decimal(price),high_price=Decimal(price+1),low_price=Decimal(price-1),close_price=Decimal(price))
            with patch.object(em,'fetch_minute_bars',return_value=[]),patch.object(repair,'fetch_sina_minute_bars',return_value=[source]):
                report=repair.repair_minute_gaps('FU2701',datetime(2026,10,14,9,20))
            self.assertTrue(report['primary_available'])
            with SessionLocal() as db:
                row=db.query(MarketBarModel).one()
                self.assertEqual(row.close_price,Decimal(price))
                self.assertEqual(row.open_interest,Decimal(180000))
                self.assertEqual(row.volume,1011)
                self.assertEqual(row.turnover,Decimal(46790390))
                provenance=json.loads(row.provenance_json)
                self.assertEqual(provenance['provider'],'sina')
                self.assertEqual(provenance['source_role'],'PRIMARY')
                self.assertEqual(provenance['volume_source'],'eastmoney_history')
                self.assertEqual(provenance['turnover_source'],'eastmoney_history')

    def test_no_primary_data_leaves_gap_without_eastmoney(self):
        with patch.object(repair,'fetch_sina_minute_bars',return_value=[]),patch.object(em,'fetch_minute_bars') as fetch:
            report=repair.repair_minute_gaps('FU2701',datetime(2026,10,13,9,20))
        fetch.assert_not_called()
        self.assertFalse(report['primary_available'])
        self.assertEqual(report['inserted'],0)

    def test_sina_request_failure_preserves_existing_data(self):
        repair.save_bars_to_mysql([self.bar(6)])
        with patch.object(repair,'fetch_sina_minute_bars',side_effect=TimeoutError('sina')),patch.object(em,'fetch_minute_bars') as fetch:
            with self.assertRaises(TimeoutError):repair.repair_minute_gaps('FU2701',datetime(2026,10,13,9,20))
        fetch.assert_not_called()
        with SessionLocal() as db:self.assertEqual(db.query(MarketBarModel).count(),1)

    def test_fallback_failure_does_not_discard_sina_primary(self):
        with patch.object(em,'fetch_minute_bars',side_effect=TimeoutError('eastmoney')),patch.object(repair,'fetch_sina_minute_bars',return_value=[self.bar(6)]):
            report=repair.repair_minute_gaps('FU2701',datetime(2026,10,13,9,20))
        self.assertEqual(report['primary_inserted'],1)
        self.assertTrue(report['primary_available'])
        self.assertEqual(report['fallback_skipped'],'DISABLED')

    def test_both_sources_fail_without_fabricating_bars(self):
        with patch.object(em,'fetch_minute_bars',side_effect=TimeoutError('eastmoney')),patch.object(repair,'fetch_sina_minute_bars',side_effect=TimeoutError('sina')):
            with self.assertRaises(TimeoutError):
                repair.repair_minute_gaps('FU2701',datetime(2026,10,13,9,20))
        with SessionLocal() as db:self.assertEqual(db.query(MarketBarModel).count(),0)

    def test_quality_warns_about_independent_backup_evidence(self):
        from backend.ai_analysis.snapshot import market_quality
        bars=[{'bar_time':'2026-10-13T09:18:00','provenance':{'provider':'eastmoney','label_semantics':'START'}}]
        quality=market_quality('FU2701',{'timestamp':'2026-10-13T09:19:05','last_price':4614,'trading_day':'20261013'},
                               bars,now=datetime(2026,10,13,9,19,5))
        self.assertTrue(quality['can_judge'])
        self.assertIn('FALLBACK_1M_SOURCE_EASTMONEY',quality['warnings'])
        self.assertNotIn('PRIMARY_1M_SOURCE_EASTMONEY',quality['warnings'])

    def test_quality_reports_retained_backup_fields_in_sina_bars(self):
        from backend.ai_analysis.snapshot import market_quality
        quote={'timestamp':'2026-10-13T09:19:05','last_price':4614,'trading_day':'20261013'}
        for backup in (False, True):
            with self.subTest(backup=backup):
                provenance={'provider':'sina','label_semantics':'START'}
                if backup:
                    provenance['volume_source']='eastmoney_history'
                quality=market_quality('FU2701',quote,[{'bar_time':'2026-10-13T09:18:00','provenance':provenance}],now=datetime(2026,10,13,9,19,5))
                self.assertEqual('FALLBACK_1M_SOURCE_EASTMONEY' in quality['warnings'],backup)

    def test_backup_respects_opening_fifteen_minute_guard(self):
        fallback=em.parse_minute_bars('FU2701',payload([
            '2026-10-13 09:01,4627,4630,4632,4626,1011,10,0,0,0,0,0,183142,57']))
        with patch.object(repair,'fetch_sina_minute_bars',return_value=[]),patch.object(em,'fetch_minute_bars',return_value=fallback):
            self.assertEqual(repair._repair_eastmoney_minutes('FU2701',datetime(2026,10,13,9,10),force=True)['inserted'],0)
            self.assertEqual(repair._repair_eastmoney_minutes('FU2701',datetime(2026,10,13,9,20),force=True)['inserted'],1)

    def test_near_two_minutes_remain_realtime(self):
        primary=em.parse_minute_bars('FU2701',payload([
            '2026-10-13 09:19,4627,4630,4632,4626,1011,10,0,0,0,0,0,183142,57']))
        with patch.object(em,'fetch_minute_bars',return_value=primary),patch.object(repair,'fetch_sina_minute_bars',return_value=[]):
            report=repair.repair_minute_gaps('FU2701',datetime(2026,10,13,9,20))
        self.assertEqual(report['inserted'],0)
        with SessionLocal() as db:self.assertEqual(db.query(MarketBarModel).count(),0)

    def test_non_trading_lunch_minute_is_not_created(self):
        rows=em.parse_minute_bars('FU2701',payload([
            '2026-10-13 12:01,4627,4630,4632,4626,1011,10,0,0,0,0,0,183142,57']))
        self.assertEqual(rows,[])


if __name__=='__main__':unittest.main()
