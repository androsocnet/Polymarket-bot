"""TDD tests for replay CLI."""
import io
import json
from pathlib import Path
import unittest
from contextlib import redirect_stdout, redirect_stderr

from polymarket_bot.replay.cli import main


class ReplayCliTests(unittest.TestCase):
    """Test replay CLI arguments, outputs, error handling, and determinism."""

    def setUp(self):
        from tests.test_paper import recorded_fixture, OfflineTests
        self._offline = OfflineTests()
        self._offline.setUp()
        self.addCleanup(self._offline.doCleanups)
        self.f = recorded_fixture(self._offline)
        self.addCleanup(self.f.close)
        self.run_id = self.f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        self.db_path = self.f.db.path

    def test_cli_requires_db_and_run_id(self):
        """CLI requires --db and --run-id."""
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main([])
        self.assertEqual(caught.exception.code, 2)

    def test_cli_runs_and_writes_outputs(self):
        """CLI runs replay and creates report.json, decisions.jsonl, ledger.jsonl."""
        out_dir = self.db_path.parent / 'replay-result'
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main([
                '--db', str(self.db_path),
                '--run-id', self.run_id,
                '--market-id', '1001',
                '--output-dir', str(out_dir),
            ])
        self.assertEqual(code, 0)
        self.assertTrue((out_dir / 'report.json').exists())
        self.assertTrue((out_dir / 'decisions.jsonl').exists())
        self.assertTrue((out_dir / 'ledger.jsonl').exists())

        # Validate report JSON content
        report_data = json.loads((out_dir / 'report.json').read_text(encoding='utf8'))
        self.assertEqual(report_data['schema'], 'phase3b-backtest-v1')
        self.assertIn('config_hash', report_data)
        self.assertIn('source_fingerprint', report_data)

    def test_cli_never_overwrites_existing_directory(self):
        """CLI exits with error if output directory already exists."""
        out_dir = self.db_path.parent / 'existing-dir'
        out_dir.mkdir()
        (out_dir / 'sentinel').write_text('untouched')

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main([
                '--db', str(self.db_path),
                '--run-id', self.run_id,
                '--market-id', '1001',
                '--output-dir', str(out_dir),
            ])
        self.assertEqual(code, 2)
        self.assertEqual((out_dir / 'sentinel').read_text(), 'untouched')


if __name__ == '__main__':
    unittest.main()