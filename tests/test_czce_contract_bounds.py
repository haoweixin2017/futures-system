import copy
import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

try:
    import test_bootstrap
except ImportError:
    from tests import test_bootstrap

from backend.ai_analysis import future_trend, snapshot
from backend.ai_analysis.research_cache import contract_status


class CzceContractBoundTests(unittest.TestCase):
    targets = [{'horizon_days':3, 'trading_day':'20261012'},
               {'horizon_days':5, 'trading_day':'20261014'}]
    now = datetime(2026, 10, 6, 23)

    def config(self):
        return json.loads((Path(future_trend.__file__).resolve().parents[2] /
            'config/future_contract_rules.json').read_text(encoding='utf-8'))

    def test_official_text_proves_october_targets_without_inventing_expiry(self):
        for symbol in ('SA2701', 'AP2701'):
            with self.subTest(symbol=symbol):
                term = future_trend.contract_lifecycle(symbol, self.targets, self.now)
                self.assertEqual(term['status'], 'RULE_BOUND')
                self.assertEqual(term['last_trading_day_not_before'], '20270101')
                self.assertNotIn('last_trading_day', term)
                self.assertNotIn('last_trading_day_by_rule', term)
                self.assertEqual([future_trend.horizon_term_status(
                    {'contract_lifecycle':term}, t) for t in self.targets], ['WITHIN_RULE_TERM'] * 2)
                for targets in ([dict(self.targets[0], trading_day='20261013')],
                                [self.targets[0], self.targets[0]]):
                    changed = dict(term, targets=targets)
                    self.assertEqual(future_trend.horizon_term_status(
                        {'contract_lifecycle':changed}, self.targets[0]), 'UNKNOWN')

    def test_delivery_month_and_unverified_calendar_targets_remain_unknown(self):
        for symbol in ('SA2701', 'AP2701'):
            term = future_trend.contract_lifecycle(symbol,
                [{'horizon_days':3, 'trading_day':'20270104'}, self.targets[1]], self.now)
            self.assertEqual([t['status'] for t in term['targets']], ['UNKNOWN','WITHIN_RULE_TERM'])
            self.assertEqual(contract_status(symbol, datetime(2027,1,4))['warning'], '期限未核实')
        for symbol in ('AP2702', 'SA2702'):
            self.assertEqual(future_trend.contract_lifecycle(symbol, self.targets, self.now)['status'], 'UNKNOWN')

    def test_frozen_cutoff_before_browser_capture_cannot_use_new_evidence(self):
        for symbol in ('SA2701', 'AP2701'):
            self.assertEqual(future_trend.contract_lifecycle(symbol, self.targets,
                datetime(2026,10,6,14))['status'], 'UNKNOWN')

    def test_bound_evidence_and_applicability_fail_closed(self):
        for changes in ({'sha256':'bad'}, {'evidence_file':'missing.json'},
                        {'verified_at':'2026-10-07T00:00:00'},
                        {'source_url':'https://example.com/rule'},
                        {'source_published_on':'2026-10-07'},
                        {'delivery_months':[2]}, {'effective_from':'2027-02-01'},
                        {'source_title':'错误版本'}, {'applies_from_delivery_month':'2027-02'}):
            edited = copy.deepcopy(self.config())
            rule = next(r for r in edited['rules'] if r['product'] == 'AP')
            rule.update(changes)
            with self.subTest(changes=changes), patch.object(Path, 'read_text', return_value=json.dumps(edited)):
                self.assertEqual(future_trend.contract_lifecycle('AP2701', self.targets,
                    self.now)['status'], 'UNKNOWN')

    def test_special_notices_override_bound_and_conflicts_block(self):
        config = self.config()
        notice = {'instrument':'SA2701', 'source_url':'https://www.czce.com.cn/fixture',
            'source_title':'离线专项公告', 'published_on':'2026-10-06',
            'verified_on':'2026-10-06T22:00:00', 'last_trading_day':'2026-10-12'}
        config['special_notices'] = [notice]
        with patch.object(Path, 'read_text', return_value=json.dumps(config)):
            term = future_trend.contract_lifecycle('SA2701', self.targets, self.now)
        self.assertEqual(term['status'], 'VERIFIED')
        self.assertEqual(term['last_trading_day'], '20261012')
        self.assertEqual([r['status'] for r in term['targets']], ['WITHIN_RULE_TERM','AFTER_RULE_TERM'])
        for notices in ([notice, notice], [dict(notice, source_url='')]):
            config['special_notices'] = notices
            with patch.object(Path, 'read_text', return_value=json.dumps(config)):
                self.assertEqual(future_trend.contract_lifecycle('SA2701', self.targets,
                    self.now)['status'], 'UNKNOWN')

    def test_duplicate_rules_and_capture_tampering_cannot_establish_term(self):
        config = self.config()
        config['rules'].append(next(r for r in config['rules'] if r['product'] == 'AP'))
        with patch.object(Path, 'read_text', return_value=json.dumps(config)):
            self.assertEqual(future_trend.contract_lifecycle('AP2701', self.targets,
                self.now)['status'], 'UNKNOWN')
        with patch.object(Path, 'read_bytes', return_value=b'{}'):
            self.assertEqual(future_trend.contract_lifecycle('AP2701', self.targets,
                self.now)['status'], 'UNKNOWN')

    def test_related_market_cannot_report_available_after_bound(self):
        from decimal import Decimal
        with patch.object(snapshot, '_instrument_meta', return_value=('苹果',10,Decimal('1'))), \
             patch.object(snapshot, '_load_bars', return_value=[]), \
             patch.object(snapshot.RedisClient, 'get_client') as redis:
            redis.return_value.hgetall.return_value = {'timestamp':'2027-01-04T09:59:59',
                'trading_day':'20270104', 'last_price':'100'}
            row = snapshot.build_related_market_snapshots(['AP2701'], '2027-01-04T10:00:00')[0]
        self.assertEqual(row['quality']['availability'], 'TERM_UNKNOWN')
        self.assertIn('CONTRACT_TERM_UNVERIFIED', row['quality']['warnings'])
