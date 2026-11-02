"""Persistent safety admission: real temporary SQLite, no public requests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from io import StringIO
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from polymarket_bot.linux_mvp import __main__ as cli
from polymarket_bot.linux_mvp.config import Settings
from polymarket_bot.linux_mvp.engine import PaperEngine
from polymarket_bot.linux_mvp.service import Service
from polymarket_bot.linux_mvp.store import MvpStore
from tests.linux_mvp_support import Collector, START, rising, us


class TerminalStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = self.base / 'paper.sqlite'
        self.store = MvpStore.open(self.db)
        self.addCleanup(lambda: self.store.close())
        self.settings = Settings.from_env({}, base=self.base)
        self.start('old')

    def start(self, run):
        self.store.start_run(run, 'default', initial_cash=Decimal('100'), config={}, started_us=us(START))

    def latch(self, status):
        self.store.heartbeat('default', 'old', at_us=us(START), market_id=None,
                             status=status, cycles=2, last_error='test terminal')

    def counts(self):
        return [self.store._conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                for table in ('runs', 'decisions', 'orders', 'observations', 'events')]

    def test_new_process_and_later_utc_day_cannot_rearm_either_latch(self):
        for status in ('FAILED_STOP', 'RISK_STOPPED'):
            with self.subTest(status=status):
                self.store._conn.execute('DELETE FROM heartbeat')
                self.latch(status)
                before = self.counts()
                heartbeat = self.store.heartbeat_row('default')
                self.settings.state_dir.mkdir(exist_ok=True)
                self.settings.stop_path.touch()  # Keep baseline reproduction offline even without guard.
                result = subprocess.run([sys.executable, '-B', '-m', 'polymarket_bot.linux_mvp',
                                         'run', '--db', str(self.db), '--state-dir', str(self.base/'state'),
                                         '--cycles', '1'], capture_output=True, text=True, timeout=10,
                                        env={**os.environ, 'MODE':'PAPER_ONLY', 'MVP_BOT_ID':'default',
                                             'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')})
                self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
                self.assertEqual(self.counts(), before)
                self.assertEqual(self.store.heartbeat_row('default'), heartbeat)
                self.store.close()
                self.store = MvpStore.open(self.db)
                collector = Collector([rising(0)])
                engine = PaperEngine(self.store, collector, self.settings, run_id='reboot',
                                     now_us=lambda: us(START)+3*86_400_000_000)
                self.assertEqual(engine.step().status, status)
                self.assertEqual(collector.calls, 0)
                self.assertEqual(self.counts(), before)

    def test_service_refuses_terminal_latch_before_engine_construction(self):
        self.latch('FAILED_STOP')
        before = self.counts()
        collector = Collector([rising(0)])
        def forbidden_engine(*args, **kwargs):
            self.fail('engine constructed after terminal stop')
        service = Service(self.store, collector, self.settings, run_id='new',
                          engine_factory=forbidden_engine, log=lambda _: None)
        with self.assertRaises(sqlite3.Error):
            service.run(max_cycles=1)
        self.assertEqual(self.counts(), before)
        self.assertEqual(collector.calls, 0)

    def test_risk_limit_stop_survives_next_day_with_recovered_daily_pnl(self):
        collector = Collector([rising(0)])
        engine = PaperEngine(self.store, collector, self.settings, run_id='old', now_us=lambda: us(START))
        with mock.patch.object(self.store, 'realized', return_value=Decimal('-11')):
            self.assertEqual(engine.step().status, 'RISK_STOPPED')
        self.store.close()
        self.store = MvpStore.open(self.db)
        before = self.counts()
        later = PaperEngine(self.store, collector, self.settings, run_id='next-day',
                            now_us=lambda: us(START)+86_400_000_000)
        self.assertEqual(later.step().status, 'RISK_STOPPED')
        self.assertEqual(collector.calls, 0)
        self.assertEqual(self.counts(), before)

    def test_terminal_latch_cannot_be_overwritten_by_stop_signal_or_running(self):
        self.latch('RISK_STOPPED')
        before = self.store.heartbeat_row('default')
        for status in ('RUNNING', 'STOPPED', 'STOPPED_BY_SIGNAL', 'FAILED_STOP'):
            with self.subTest(status=status), self.assertRaises(sqlite3.Error):
                self.store.heartbeat('default', 'old', at_us=us(START)+10, market_id=None,
                                     status=status, cycles=0, last_error=None)
            self.assertEqual(self.store.heartbeat_row('default'), before)

    def test_safe_restart_and_emergency_stop_remain_available(self):
        for previous in ('RUNNING', 'ERROR', 'STOPPED_BY_SIGNAL', 'STOPPED'):
            with self.subTest(previous=previous):
                self.store.heartbeat('default', 'old', at_us=us(START), market_id=None,
                                     status=previous, cycles=0, last_error=None)
                collector = Collector([rising(0)])
                service = Service(self.store, collector, self.settings, run_id='safe-'+previous,
                                  now_us=lambda: us(START), sleep=lambda _: None, log=lambda _: None)
                self.assertEqual(service.run(max_cycles=1), 'CYCLE_LIMIT')
                self.assertEqual(collector.calls, 1)
        self.settings.state_dir.mkdir(exist_ok=True)
        self.settings.stop_path.touch()
        collector = Collector([rising(1)])
        service = Service(self.store, collector, self.settings, run_id='stop', log=lambda _: None)
        self.assertEqual(service.run(max_cycles=1), 'STOPPED')
        self.assertEqual(collector.calls, 0)

    def test_inconsistent_or_unreadable_safety_state_refuses_without_new_run(self):
        for mutation in ("DELETE FROM heartbeat", "UPDATE heartbeat SET status='UNKNOWN'",
                         "UPDATE heartbeat SET run_id='missing'", "UPDATE heartbeat SET cycles=-1"):
            with self.subTest(mutation=mutation):
                self.store._conn.execute('DELETE FROM heartbeat')
                self.store.heartbeat('default', 'old', at_us=us(START), market_id=None,
                                     status='RUNNING', cycles=0, last_error=None)
                self.store._conn.execute(mutation)
                before = self.counts()
                self.settings.state_dir.mkdir(exist_ok=True)
                self.settings.stop_path.touch()
                with mock.patch.dict(os.environ, {'MODE':'PAPER_ONLY', 'MVP_BOT_ID':'default'}), \
                        redirect_stderr(StringIO()), redirect_stdout(StringIO()):
                    self.assertEqual(cli.main(['run','--db',str(self.db),'--state-dir',str(self.base/'state'),'--cycles','1']),75)
                self.assertEqual(self.counts(), before)
        bad = self.base/'bad.sqlite'
        bad.write_bytes(b'not sqlite')
        with redirect_stderr(StringIO()):
            self.assertEqual(cli.main(['run','--db',str(bad),'--state-dir',str(self.base/'bad-state')]),75)

    def test_terminal_latch_during_collection_blocks_fill_and_decision(self):
        engine = PaperEngine(self.store, Collector([rising(0), rising(1), rising(2)]),
                             self.settings, run_id='old', now_us=lambda: us(START))
        for _ in range(3):
            engine.step()
        before = self.counts()
        pending = self.store.pending_order('default')
        self.assertIsNotNone(pending)
        def collect():
            other = MvpStore.open(self.db)
            try:
                other.heartbeat('default','old',at_us=us(START),market_id='1001',
                                status='RISK_STOPPED',cycles=3,last_error='external stop')
            finally:
                other.close()
            return rising(3)
        engine.collector.collect = collect
        with self.assertRaises(sqlite3.Error):
            engine.step()
        self.assertEqual(self.store.pending_order('default'),pending)
        self.assertEqual(self.counts()[:3],before[:3])
        self.assertEqual(self.store.heartbeat_row('default')['status'],'RISK_STOPPED')

    def test_latch_after_execution_before_decision_is_checked_under_writer_lock(self):
        engine = PaperEngine(self.store, Collector([rising(0)]), self.settings,
                             run_id='old', now_us=lambda: us(START))
        original = engine._execute_pending
        def latch_after_execution(*args):
            original(*args)
            self.latch('FAILED_STOP')
        with mock.patch.object(engine, '_execute_pending', side_effect=latch_after_execution):
            with self.assertRaises(sqlite3.Error):
                engine.step()
        self.assertEqual(self.store.decisions_for_market('default','1001'),[])
        self.assertEqual(self.store.orders('default'),[])

    def test_orphan_ledger_cannot_be_admitted_as_fresh(self):
        engine = PaperEngine(self.store, Collector([rising(0), rising(1), rising(2)]),
                             self.settings, run_id='old', now_us=lambda: us(START))
        for _ in range(3):
            engine.step()
        for table in ('heartbeat','runs','accounts'):
            self.store._conn.execute(f'DELETE FROM {table}')
        before = self.counts()
        with self.assertRaises(sqlite3.Error):
            self.start('new')
        self.assertEqual(self.counts(),before)

    def test_unreadable_database_closes_connection(self):
        bad = self.base/'unreadable.sqlite'
        bad.write_bytes(b'not sqlite')
        connection = sqlite3.connect(bad)
        with mock.patch('polymarket_bot.linux_mvp.store.sqlite3.connect', return_value=connection):
            with self.assertRaises(sqlite3.DatabaseError):
                MvpStore.open(bad)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')

    def test_run_and_initial_heartbeat_commit_or_rollback_together(self):
        self.assertEqual(self.store.heartbeat_row('default')['run_id'], 'old')
        self.store._conn.execute("CREATE TRIGGER refuse_beat BEFORE UPDATE ON heartbeat BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.Error):
            self.start('rollback')
        self.assertEqual(self.store._conn.execute("SELECT count(*) FROM runs WHERE run_id='rollback'").fetchone()[0],0)
        self.assertEqual(self.store.heartbeat_row('default')['run_id'],'old')

    def test_concurrent_start_waits_for_terminal_commit_and_refuses(self):
        other = MvpStore.open(self.db)
        self.addCleanup(other.close)
        ready = threading.Event()
        entered = threading.Event()
        go = threading.Event()
        def admit():
            store = MvpStore.open(self.db)
            try:
                ready.set()
                go.wait(10)
                store._conn.set_trace_callback(lambda sql: entered.set() if sql == 'BEGIN IMMEDIATE' else None)
                try:
                    store.start_run('racer','default',initial_cash=Decimal('100'),config={},started_us=us(START))
                except sqlite3.Error:
                    return 'refused'
                return 'admitted'
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(admit)
            self.assertTrue(ready.wait(10))
            other._conn.execute('BEGIN IMMEDIATE')
            other._conn.execute("UPDATE heartbeat SET status='FAILED_STOP' WHERE bot_id='default'")
            go.set()
            self.assertTrue(entered.wait(10))
            other._conn.execute('COMMIT')
            self.assertEqual(future.result(timeout=15),'refused')
        self.assertEqual(self.store.heartbeat_row('default')['status'],'FAILED_STOP')
        self.assertEqual(self.store._conn.execute('SELECT count(*) FROM runs').fetchone()[0],1)
