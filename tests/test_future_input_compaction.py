import json
import unittest
from tests import test_bootstrap
from backend.ai_analysis import orchestrator


class InputCompactionTests(unittest.TestCase):
    def test_bars_round_trip_preserves_missing_fields_and_evidence(self):
        rows = [{'bar_time':str(i), 'open':'4000', 'close':'4001', 'volume':123,
                 'usage_limits':{'volume_oi':False}} for i in range(100)]
        rows[0]['optional'] = None
        frozen = {'analysis_target':'FUTURE_TREND', 'review_policy_version':'1',
                  'market':{'bars_60m':rows}, 'public_materials':{'sources':[
                      {'source_id':'S1', 'excerpt':'逐字原文', 'dimension':'fundamentals'}]}}
        original = orchestrator._json(frozen)
        compact = orchestrator._model_snapshot(original, compact=True)
        restored = orchestrator.expand_market_bars(compact)
        self.assertEqual(restored, frozen)
        self.assertEqual(json.loads(original), frozen)
        self.assertLess(len(orchestrator._json(compact)), len(original)*0.8)


if __name__ == '__main__':
    unittest.main()
