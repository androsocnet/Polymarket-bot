"""Service: single instance, bounded backoff, STOP semantics, CLI wiring and refusals."""
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from polymarket_bot.linux_mvp import __main__ as cli
from polymarket_bot.linux_mvp.config import Settings
from polymarket_bot.linux_mvp.service import InstanceLockError, Service, single_instance
from polymarket_bot.linux_mvp.store import MvpStore
from polymarket_bot.market_data.models import MarketDataError
from tests.linux_mvp_support import START, Collector, rising, us

BOT = 'default'


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'

    def test_second_instance_is_refused_and_released(self):
        with single_instance(self.state):
            with self.assertRaises(InstanceLockError):
                with single_instance(self.state):
                    pass
        with single_instance(self.state):
            pass


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = MvpStore.open(self.base / 'paper.sqlite')
        self.addCleanup(self.store.close)
        self.settings = Settings.from_env({}, base=self.base)

    def test_bounded_run_completes_and_persists_heartbeat(self):
        collector = Collector([rising(0), rising(1), rising(2)])
        service = Service(self.store, collector, self.settings, run_id='run-svc',
                          sleep=lambda seconds: None, now_us=lambda: us(START))
        status = service.run(max_cycles=3)
        self.assertEqual(status, 'CYCLE_LIMIT')
        self.assertEqual(collector.calls, 3)
        heartbeat = self.store.heartbeat_row(BOT)
        self.assertEqual(heartbeat['status'], 'RUNNING')
        self.assertEqual(heartbeat['cycles'], 3)
        self.assertEqual(len(self.store.decisions_for_market(BOT, '1001')), 3)

    def test_stop_file_halts_without_collecting(self):
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings.stop_path.write_text('stop\n')
        collector = Collector([rising(0)])
        service = Service(self.store, collector, self.settings, run_id='run-stop',
                          sleep=lambda seconds: None, now_us=lambda: us(START))
        self.assertEqual(service.run(max_cycles=5), 'STOPPED')
        self.assertEqual(collector.calls, 0)
        self.assertEqual(self.store.heartbeat_row(BOT)['status'], 'STOPPED')

    def test_signal_stop_between_cycles(self):
        collector = Collector([rising(0), rising(1), rising(2)])
        service = Service(self.store, collector, self.settings, run_id='run-signal',
                          sleep=lambda seconds: service.request_stop(), now_us=lambda: us(START))
        self.assertEqual(service.run(), 'STOPPED_BY_SIGNAL')
        self.assertEqual(collector.calls, 1)
        self.assertEqual(self.store.heartbeat_row(BOT)['status'], 'STOPPED_BY_SIGNAL')

    def test_error_backoff_is_bounded_and_fails_closed(self):
        self.settings = Settings.from_env({'MVP_MAX_CONSECUTIVE_ERRORS': '2'}, base=self.base)
        collector = Collector([MarketDataError('transport_error', 'down'),
                               MarketDataError('transport_error', 'down')])
        delays = []
        service = Service(self.store, collector, self.settings, run_id='run-err',
                          sleep=delays.append, now_us=lambda: us(START))
        self.assertEqual(service.run(max_cycles=10), 'FAILED_STOP')
        self.assertAlmostEqual(sum(delays), 2.0)
        self.assertTrue(all(0 < delay <= 0.25 for delay in delays))
        self.assertEqual(collector.calls, 2)
        self.assertEqual(self.store.heartbeat_row(BOT)['status'], 'FAILED_STOP')


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = self.base / 'cli.sqlite'
        self.state = self.base / 'state'

    def test_live_environment_is_refused_before_touching_state(self):
        err = StringIO()
        with mock.patch.dict(os.environ, {'MODE': 'LIVE'}), redirect_stderr(err):
            code = cli.main(['run', '--db', str(self.db), '--state-dir', str(self.state)])
        self.assertEqual(code, 2)
        self.assertIn('configuration refused', err.getvalue())
        self.assertFalse(self.db.exists())

    def test_run_wires_collector_and_lock_without_network(self):
        class FakeCollector:
            def __init__(self):
                self.closed = False

            def collect(self):
                return rising(0)

            def close(self):
                self.closed = True

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        out = StringIO()
        with mock.patch.dict(os.environ, {'MODE': 'PAPER_ONLY'}), \
                mock.patch.object(cli, 'LiveCollector', FakeCollector), redirect_stdout(out):
            code = cli.main(['run', '--db', str(self.db), '--state-dir', str(self.state),
                             '--cycles', '1'])
        self.assertEqual(code, 0)
        self.assertIn('finished: CYCLE_LIMIT', out.getvalue())
        store = MvpStore.open(self.db)
        self.addCleanup(store.close)
        self.assertEqual(len(store.decisions_for_market(BOT, '1001')), 1)
        self.assertEqual(store.heartbeat_row(BOT)['cycles'], 1)

    def test_report_cli_json_and_text(self):
        store = MvpStore.open(self.db)
        store.start_run('run-cli', BOT, initial_cash=Decimal('100'), config={}, started_us=us(START))
        store.ensure_window(BOT, 'run-cli', market_id='1001', condition_id='0x' + 'ab' * 32,
                            event_id='2001', start_us=us(START), end_us=us(START) + 900_000_000,
                            at_us=us(START))
        store.close()
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(['report', '--db', str(self.db), '--json']), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report['mode'], 'PAPER_ONLY')
        self.assertEqual(report['windows']['total'], 1)
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(['report', '--db', str(self.db)]), 0)
        self.assertIn('PAPER_ONLY MVP report', out.getvalue())
        self.assertIn('UNKNOWN_NOT_INFERRED', out.getvalue())

    def test_report_cli_refuses_missing_database(self):
        err = StringIO()
        with redirect_stderr(err):
            self.assertEqual(cli.main(['report', '--db', str(self.base / 'nope.sqlite')]), 2)
        self.assertIn('report refused', err.getvalue())


if __name__ == '__main__':
    unittest.main()
