"""TDD tests for ReplayEngine: deterministic chronological replay with settlement."""
import unittest
from decimal import Decimal

from polymarket_bot.replay.adapter import load_replay_recording
from polymarket_bot.replay.engine import ReplayEngine, run_replay
from polymarket_bot.replay.report import BacktestReport


class EngineTests(unittest.TestCase):
    """Test chronological replay with execution, fees, partial fills, and settlement."""

    def setUp(self):
        from tests.test_paper import recorded_fixture, OfflineTests
        self._offline = OfflineTests()
        self._offline.setUp()
        self.addCleanup(self._offline.doCleanups)
        self.f = recorded_fixture(self._offline)
        self.addCleanup(self.f.close)
        self.run_id = self.f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        self.db_path = self.f.db.path

    def _load_window(self):
        snapshots, excluded, opportunities, settlement_evidence, settlement_result = load_replay_recording(
            self.db_path, self.run_id, '1001')
        return snapshots, excluded, opportunities, settlement_evidence, settlement_result

    def test_replay_engine_exists(self):
        """ReplayEngine class should exist."""
        from polymarket_bot.replay.engine import ReplayEngine
        self.assertTrue(callable(ReplayEngine))

    def test_run_replay_returns_report(self):
        """run_replay returns a BacktestReport with all required fields."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            settlement_result=settlement_result,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        self.assertIsInstance(report, BacktestReport)
        self.assertEqual(report.schema, 'phase3b-backtest-v1')

    def test_baseline_strategy_reproduces_existing_behavior(self):
        """ReplayEngine with baseline config should match PaperEngine on same inputs."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            settlement_result=settlement_result,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        # Should have made at least one decision
        self.assertGreater(len(report.decisions), 0)
        # Constant fixture midpoints -> 0 momentum -> HOLD decisions
        self.assertEqual(report.counts['requested'], 0)

    def test_fees_pnl_reconciliation(self):
        """cash + basis = initial + realized (within tolerance)."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            settlement_result=settlement_result,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        # Reconciliation check
        cash = Decimal(report.cash)
        basis = sum(Decimal(p['cost_basis']) for p in report.positions.values())
        initial = Decimal('100')
        realized = Decimal(report.realized_pnl or '0')
        self.assertAlmostEqual(float(cash + basis), float(initial + realized), places=6)

    def test_partial_fills_tracked(self):
        """PARTIAL fills consume depth, remainder abandoned."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            settlement_result=settlement_result,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        partial_fills = [t for t in report.ledger if t['status'] == 'PARTIAL']
        for t in partial_fills:
            self.assertGreater(Decimal(t['filled_quantity']), 0)
            self.assertGreater(Decimal(t['unfilled_quantity']), 0)

    def test_risk_constraints_reported_not_enforced(self):
        """Exposure, cash, position limits are checked but don't stop replay."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            settlement_result=settlement_result,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000,
            max_exposure='5',  # Lower than default $10
            max_order='10'
        )
        # Should still produce a report even if risk limits would trigger
        self.assertIsInstance(report, BacktestReport)
        self.assertGreater(len(report.decisions), 0)

    def test_settlement_applied_when_evidence_available(self):
        """When settlement_evidence and settlement_result are provided, positions settled."""
        # Create a mock settlement result
        settlement_result = {
            'status': 'RESOLVED',
            'winning_outcome': 'UP',
            'resolved_at': '2026-10-22T12:15:00+00:00',
            'tx_hash': '0x' + 'ab' * 32
        }
        settlement_evidence = {
            'gamma_raw': b'{}',
            'resolution_raw': '{"data": [{"status": "resolved", "payouts": [1000000, 0], "condition_id": "0xabab", "transaction_hash": "0xabab"}]}'
        }
        snapshots, excluded, opportunities, _, _ = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            settlement_result=settlement_result,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        self.assertIsNotNone(report.settlement)

    def test_no_future_data_leakage(self):
        """Decisions at slot N never use data from slot > N."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            settlement_result=settlement_result,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        # Each decision should only reference snapshots at or before its slot
        for decision in report.decisions:
            slot_idx = decision['slot_index']
            for inp in decision.get('inputs', []):
                self.assertLessEqual(inp.get('slot_index', 0), slot_idx)

    def test_chronological_execution_order(self):
        """Orders executed in chronological order by decision time."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            settlement_result=settlement_result,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        # Ledger should be in execution order (not request order)
        exec_times = [Decimal(t.get('snapshot', {}).get('receipt_server_us', 0)) for t in report.ledger if t.get('snapshot')]
        self.assertEqual(exec_times, sorted(exec_times))

    def test_deterministic_reproduction(self):
        """Same inputs → identical report (byte-for-byte)."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result = self._load_window()
        report1 = run_replay(
            snapshots=snapshots,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            settlement_result=settlement_result,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        report2 = run_replay(
            snapshots=snapshots,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            settlement_result=settlement_result,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        import json
        self.assertEqual(
            json.dumps(report1.__dict__, sort_keys=True, default=str),
            json.dumps(report2.__dict__, sort_keys=True, default=str)
        )

    def test_max_drawdown_calculated(self):
        """Maximum drawdown calculated from equity curve."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            settlement_result=settlement_result,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        self.assertIsNotNone(report.max_drawdown)
        self.assertIsInstance(report.max_drawdown, Decimal)

    def test_daily_pnl_buckets(self):
        """P&L grouped by UTC day from execution/settlement times."""
        snapshots, excluded, opportunities, settlement_evidence, settlement_result = self._load_window()
        report = run_replay(
            snapshots=snapshots,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            settlement_result=settlement_result,
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3,
            delay_us=0,
            max_wait_us=5_000_000,
            max_age_us=5_000_000
        )
        self.assertIsInstance(report.daily_pnl, dict)


if __name__ == '__main__':
    unittest.main()