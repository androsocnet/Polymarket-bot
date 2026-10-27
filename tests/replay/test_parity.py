"""I6 regression: replay must reproduce the live strategy baseline on identical inputs.

The replay decision loop is a parameterized copy of strategy._run. This parity
test keeps the copy honest: on the same validated recording with default
configuration, replay decisions, orders, and the execution ledger must match
strategy.run_strategy exactly (on the shared keys), byte for byte.
"""
import json
import unittest

from polymarket_bot.replay.adapter import load_replay_recording
from polymarket_bot.replay.engine import run_replay
from polymarket_bot.strategy import load_strategy_recording, run_strategy

from tests.replay.fixtures import traded_fixture


class ParityTests(unittest.TestCase):
    """Replay vs live-baseline parity on a fixture that actually trades."""

    def setUp(self):
        self.f = traded_fixture(self)
        self.run_id = self.f.rows('SELECT run_id FROM recording_runs')[0]['run_id']

    def test_replay_matches_run_strategy_baseline(self):
        live = run_strategy(load_strategy_recording(self.f.db.path, self.run_id, '1001'))
        snapshots, _, _, _, _, paper_recording = load_replay_recording(
            self.f.db.path, self.run_id, '1001')
        report = run_replay(snapshots=snapshots, paper_recording=paper_recording)

        live_slots = {d['slot_id']: d for d in live['decisions']}
        self.assertEqual({d['slot_id'] for d in report.decisions}, set(live_slots))
        for d in report.decisions:
            ref = live_slots[d['slot_id']]
            for key in ('action', 'reason', 'signal', 'momentum', 'cash',
                        'exposure', 'positions', 'instruction', 'midpoints'):
                self.assertEqual(
                    d.get(key), ref.get(key),
                    f"slot {d['slot_id']}: replay '{key}' diverges from run_strategy")

        replay_ledger = [t for t in report.ledger]
        live_ledger = live['execution']['ledger']
        self.assertEqual(len(replay_ledger), len(live_ledger))
        for replay_trade, live_trade in zip(replay_ledger, live_ledger):
            self.assertEqual(
                json.dumps(replay_trade, sort_keys=True, default=str),
                json.dumps(live_trade, sort_keys=True, default=str))

        execution = live['execution']
        self.assertEqual(execution['counts'], report.counts)
        self.assertEqual(execution['cash'], report.cash)
        self.assertEqual(execution['fees_paid'], report.fees_paid)


if __name__ == '__main__':
    unittest.main()
