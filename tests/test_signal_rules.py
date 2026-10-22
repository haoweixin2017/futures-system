import unittest
from decimal import Decimal
from types import SimpleNamespace
from backend.signal_engine.rules import evaluate_rules


def bar(price=100, volume=100, oi=1000):
    return SimpleNamespace(close_price=Decimal(str(price)), high_price=Decimal('101'),
                           low_price=Decimal('99'), volume=volume,
                           open_interest=None if oi is None else Decimal(str(oi)))


class SignalRulesTests(unittest.TestCase):
    def test_threshold_matrix(self):
        for price in (98, 99, 100, 101, 102):
            for volume in (149, 150, 151):
                for oi in (999, 1000, 1001):
                    with self.subTest(price=price, volume=volume, oi=oi):
                        r = evaluate_rules(bar(price, volume, oi), [bar() for _ in range(20)],
                                           [bar() for _ in range(60)])
                        for side, breakout in [('long', price > 101), ('short', price < 99)]:
                            expected = 35*breakout + 30*(volume >= 150) + 20*(oi > 1000) + 15
                            self.assertEqual(r[side]['score'], expected)
                            self.assertEqual(sum(c['points'] for c in r[side]['conditions']), expected)

    def test_strict_unknown_and_legacy(self):
        for count in (0, 19, 20):
            r = evaluate_rules(bar(102, 150, None), [bar() for _ in range(count)], [])
            self.assertIsNone(r['long']['score'])
        self.assertEqual(evaluate_rules(bar(102, 150, 1001), [bar()]*20, [], 'LEGACY')['long']['score'], 100)

    def test_zero_volume_is_known_but_zero_baseline_unknown(self):
        self.assertEqual(evaluate_rules(bar(volume=0), [bar()]*20, [bar()]*60)['long']['score'], 15)
        self.assertIsNone(evaluate_rules(bar(), [bar(volume=0)]*20, [bar()]*60)['long']['score'])

