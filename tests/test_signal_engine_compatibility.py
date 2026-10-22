"""Fixed outputs captured before rule extraction; no signal persistence."""
from tests import test_bootstrap  # noqa: F401
import json
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock
from backend.models.market_data import BarData
from backend.signal_engine.signal_engine import SignalEngine


def cases():
    for price in (98, 99, 100, 101, 102):
        for volume in (0, 100, 150, 250):
            for oi in (999, 1000, 1001, 1300):
                yield price, volume, oi


def result_for(price, volume, oi):
    def b(index, close, vol, interest):
        return BarData(instrument='JM2701', trading_day='20261019', period='15m',
                       bar_time=datetime(2026, 10, 19, 9)+timedelta(minutes=index*15),
                       open_price=Decimal('100'), high_price=Decimal(str(max(101, close))),
                       low_price=Decimal(str(min(99, close))), close_price=Decimal(str(close)),
                       volume=vol, turnover=Decimal('0'), open_interest=Decimal(str(interest)))
    bars = [b(i, 100, 100, 1000) for i in range(20)] + [b(20, price, volume, oi)]
    engine = SignalEngine.__new__(SignalEngine)
    engine._save_signal = Mock()
    result = engine.evaluate_15m_signals('JM2701', bars, [])
    return {'result': result.model_dump(mode='json') if result else None,
            'saves': engine._save_signal.call_count}


class SignalCompatibilityTests(unittest.TestCase):
    def test_pre_extraction_golden(self):
        golden = json.loads((Path(__file__).parent/'fixtures/market_simulator/legacy_signals.json').read_text())
        for args, expected in zip(cases(), golden):
            with self.subTest(args=args):
                self.assertEqual(result_for(*args), expected)

