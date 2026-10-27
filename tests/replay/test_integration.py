"""End-to-end integration test for Phase 3B replay pipeline."""
import json
from pathlib import Path
import unittest

from polymarket_bot.replay.adapter import load_replay_recording, ReplayAdapter
from polymarket_bot.replay.cli import main as cli_main
from polymarket_bot.replay.engine import run_replay, ReplayEngine
from polymarket_bot.replay.report import BacktestReport, split_windows
from polymarket_bot.replay.variants import get_variant, evaluate_variants


class ReplayIntegrationTests(unittest.TestCase):
    """End-to-end integration of adapter, engine, report, splits, and variants."""

    def setUp(self):
        from tests.test_paper import recorded_fixture, OfflineTests
        self._offline = OfflineTests()
        self._offline.setUp()
        self.addCleanup(self._offline.doCleanups)
        self.f = recorded_fixture(self._offline)
        self.addCleanup(self.f.close)
        self.run_id = self.f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        self.db_path = self.f.db.path

    def test_full_pipeline_flow(self):
        """Adapter -> Engine -> Report -> CLI full workflow."""
        adapter = ReplayAdapter(self.db_path, self.run_id, '1001')
        adapter.load()
        self.assertGreater(len(adapter.snapshots), 0)

        # Run replay engine
        engine = ReplayEngine(
            initial_cash='100',
            fee_bps='10',
            threshold='0.02',
            lookback=3,
            holding_period=3
        )
        report = engine.run(
            snapshots=adapter.snapshots,
            paper_recording=adapter.paper_recording,
            excluded=adapter.excluded,
            opportunities=adapter.opportunities,
            settlement_evidence=adapter.settlement_evidence
        )

        self.assertIsInstance(report, BacktestReport)
        self.assertEqual(report.schema, 'phase3b-backtest-v1')
        self.assertTrue(len(report.config_hash) == 64)
        self.assertTrue(len(report.source_fingerprint) == 64)
        self.assertGreater(len(report.decisions), 0)

        # Run via CLI and check written files
        out_dir = self.db_path.parent / 'integration-output'
        code = cli_main([
            '--db', str(self.db_path),
            '--run-id', self.run_id,
            '--market-id', '1001',
            '--output-dir', str(out_dir),
            '--initial-cash', '100',
            '--fee-bps', '10'
        ])
        self.assertEqual(code, 0)
        self.assertTrue((out_dir / 'report.json').exists())
        self.assertTrue((out_dir / 'decisions.jsonl').exists())
        self.assertTrue((out_dir / 'ledger.jsonl').exists())

        # Verify CLI determinism: running second time to another dir yields identical bytes
        out_dir2 = self.db_path.parent / 'integration-output-2'
        code2 = cli_main([
            '--db', str(self.db_path),
            '--run-id', self.run_id,
            '--market-id', '1001',
            '--output-dir', str(out_dir2),
            '--initial-cash', '100',
            '--fee-bps', '10'
        ])
        self.assertEqual(code2, 0)
        for name in ('report.json', 'decisions.jsonl', 'ledger.jsonl'):
            self.assertEqual(
                (out_dir / name).read_bytes(),
                (out_dir2 / name).read_bytes()
            )


if __name__ == '__main__':
    unittest.main()