"""Phase 1 ingest-only pipeline: storage, rollover, restart, STOP, backoff. No decisions."""
from contextlib import redirect_stdout
from decimal import Decimal
from datetime import timedelta
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from polymarket_bot.linux_mvp import __main__ as cli
from polymarket_bot.linux_mvp.collector import CollectionStopped, LiveCollector
from polymarket_bot.linux_mvp.config import Settings
from polymarket_bot.linux_mvp.ingest import IngestEngine
from polymarket_bot.linux_mvp.service import Service
from polymarket_bot.linux_mvp.store import MvpStore
from polymarket_bot.market_data.models import MarketDataError
from tests.linux_mvp_support import START, Collector, descriptor, snapshot, us

BOT = 'default'
TRADING_TABLES = ('decisions', 'orders', 'positions', 'equity')


def count_rows(store, table):
    return store._conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def market_for(window: int):
    start = START + timedelta(minutes=15 * window)
    prefix = 'ab' if window == 0 else 'cd'
    return descriptor(market_id=str(1001 + window), start=start, condition='0x' + prefix * 32,
                      event=str(2001 + window), up=str(111111 + window), down=str(222222 + window))


def snap(window: int, slot: int):
    market = market_for(window)
    mid = Decimal('0.40') + Decimal('0.02') * ((slot // 5) % 3)
    return snapshot(slot=slot, start=market.window_start, market=market,
                    up_bid=str(mid), up_ask=str(mid + Decimal('0.02')),
                    down_bid=str(Decimal(1) - mid - Decimal('0.02')),
                    down_ask=str(Decimal(1) - mid))


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = MvpStore.open(self.base / 'paper.sqlite')
        self.addCleanup(self.store.close)
        self.settings = Settings.from_env({}, base=self.base)
        self.store.start_run('ingest', BOT, initial_cash=Decimal('100'), config={},
                             started_us=us(START))

    def engine(self, collector, *, run_id='ingest', start=START):
        return IngestEngine(self.store, collector, self.settings, run_id=run_id,
                            now_us=lambda: us(start))

    def test_ingest_stores_books_and_creates_no_trading_rows(self):
        sample = snap(0, 0)
        result = self.engine(Collector([sample])).step()
        self.assertEqual(result.status, 'OK')
        self.assertEqual(result.slot_index, 0)
        rows = self.store.observations_for_market(BOT, '1001')
        self.assertEqual(len(rows), 2)
        by_outcome = {row['outcome']: row for row in rows}
        for outcome, observation in (('UP', sample.up), ('DOWN', sample.down)):
            with self.subTest(outcome=outcome):
                row = by_outcome[outcome]
                self.assertEqual(row['slot_index'], 0)
                self.assertEqual(row['token_id'], observation.outcome.token_id)
                self.assertEqual(row['bid'], observation.book.best_bid)
                self.assertEqual(row['ask'], observation.book.best_ask)
                self.assertEqual(row['midpoint'], observation.book.midpoint)
                self.assertEqual(row['source_at'], us(observation.book.source_at))
                self.assertEqual(row['received_us'], us(observation.request_finished_at))
        self.assertEqual(self.store.active_window(BOT)['market_id'], '1001')
        for table in TRADING_TABLES:
            with self.subTest(table=table):
                self.assertEqual(count_rows(self.store, table), 0)
        heartbeat = self.store.heartbeat_row(BOT)
        self.assertEqual(heartbeat['status'], 'RUNNING')
        self.assertEqual(heartbeat['cycles'], 1)

    def test_duplicate_slot_is_idempotent(self):
        collector = Collector([snap(0, 0), snap(0, 0)])
        engine = self.engine(collector)
        self.assertEqual(engine.step().status, 'OK')
        self.assertEqual(engine.step().status, 'DUPLICATE_SLOT')
        self.assertEqual(len(self.store.observations_for_market(BOT, '1001')), 2)
        self.assertEqual(count_rows(self.store, 'decisions'), 0)

    def test_rollover_closes_old_unknown_and_starts_new_window(self):
        collector = Collector([snap(0, 0), snap(1, 0)])
        engine = self.engine(collector)
        self.assertEqual(engine.step().status, 'OK')
        self.assertEqual(engine.step().status, 'OK')
        old = self.store.window(BOT, '1001')
        self.assertEqual(old['status'], 'UNKNOWN_SETTLEMENT')
        self.assertEqual(old['note'], 'rollover to 1002')
        self.assertEqual(self.store.active_window(BOT)['market_id'], '1002')
        self.assertEqual(len(self.store.observations_for_market(BOT, '1001')), 2)
        self.assertEqual(len(self.store.observations_for_market(BOT, '1002')), 2)
        for table in TRADING_TABLES:
            with self.subTest(table=table):
                self.assertEqual(count_rows(self.store, table), 0)

    def test_transient_rollover_conflict_keeps_active_window_and_recovers(self):
        collector = Collector([snap(0, 0), snap(1, 0), snap(0, 0), snap(1, 1)])
        engine = self.engine(collector)
        self.assertEqual(engine.step().status, 'OK')
        self.assertEqual(engine.step().status, 'OK')
        self.assertEqual(engine.step().status, 'WINDOW_CONFLICT')
        self.assertEqual(self.store.active_window(BOT)['market_id'], '1002')
        self.assertEqual(self.store.window(BOT, '1001')['status'], 'UNKNOWN_SETTLEMENT')
        self.assertEqual(engine.step().status, 'OK')
        self.assertEqual(len(self.store.observations_for_market(BOT, '1002')), 4)
        conflicts = [e for e in self.store.events(BOT) if e['kind'] == 'window_conflict']
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(count_rows(self.store, 'decisions'), 0)

    def test_window_expiry_is_aborted_and_next_market_starts_fresh(self):
        collector = Collector([snap(0, 0), snap(0, 450), snap(1, 0)])
        engine = self.engine(collector)
        self.assertEqual(engine.step().status, 'OK')
        self.assertEqual(engine.step().status, 'WINDOW_EXPIRED')
        self.assertEqual(self.store.window(BOT, '1001')['status'], 'ABORTED')
        self.assertEqual(self.store.active_window(BOT), None)
        self.assertEqual(engine.step().status, 'OK')
        self.assertEqual(self.store.active_window(BOT)['market_id'], '1002')
        self.assertEqual(len(self.store.observations_for_market(BOT, '1002')), 2)

    def test_restart_reuses_active_window_without_duplicate_books(self):
        first = self.engine(Collector([snap(0, 0)]), run_id='run-1')
        self.assertEqual(first.step().status, 'OK')
        window_before = self.store.window(BOT, '1001')
        self.store.close()

        self.store = MvpStore.open(self.base / 'paper.sqlite')
        self.addCleanup(self.store.close)
        self.store.start_run('run-2', BOT, initial_cash=Decimal('100'), config={},
                             started_us=us(START))
        second = self.engine(Collector([snap(0, 0), snap(0, 1)]), run_id='run-2')
        self.assertEqual(second.step().status, 'DUPLICATE_SLOT')
        self.assertEqual(second.step().status, 'OK')
        self.assertEqual(self.store.window(BOT, '1001')['start_us'], window_before['start_us'])
        self.assertEqual(self.store.window(BOT, '1001')['status'], 'ACTIVE')
        self.assertEqual(len(self.store.observations_for_market(BOT, '1001')), 4)
        self.assertEqual(self.store.heartbeat_row(BOT)['run_id'], 'run-2')
        self.assertEqual(count_rows(self.store, 'decisions'), 0)

    def test_stop_halts_before_collection(self):
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings.stop_path.write_text('stop\n', encoding='utf8')
        collector = Collector([snap(0, 0)])
        service = Service(self.store, collector, self.settings, run_id='run-stop',
                          engine_factory=IngestEngine, sleep=lambda seconds: None,
                          now_us=lambda: us(START))
        self.assertEqual(service.run(max_cycles=5), 'STOPPED')
        self.assertEqual(collector.calls, 0)
        self.assertEqual(self.store.heartbeat_row(BOT)['status'], 'STOPPED')
        self.assertEqual(count_rows(self.store, 'windows'), 0)

    def test_stop_during_backoff_is_observed_without_waiting_for_full_delay(self):
        self.settings = Settings.from_env({
            'MVP_MAX_CONSECUTIVE_ERRORS': '3',
            'MVP_BACKOFF_BASE_SECONDS': '30',
            'MVP_BACKOFF_MAX_SECONDS': '30',
        }, base=self.base)
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        delays = []

        def sleep_and_stop(seconds):
            delays.append(seconds)
            self.settings.stop_path.write_text('stop\n', encoding='utf8')

        service = Service(self.store, Collector([MarketDataError('transport_error', 'down')]),
                          self.settings, run_id='run-stop-backoff', engine_factory=IngestEngine,
                          sleep=sleep_and_stop, now_us=lambda: us(START))
        self.assertEqual(service.run(max_cycles=5), 'STOPPED')
        self.assertEqual(sum(delays), 0.25)
        self.assertEqual(self.store.heartbeat_row(BOT)['status'], 'STOPPED')

    def test_live_collector_stop_predicate_aborts_before_attempt(self):
        collector = LiveCollector()
        self.addCleanup(collector.close)
        collector.set_stop_check(lambda: True)
        with self.assertRaises(CollectionStopped):
            collector._before_attempt()

    def test_stop_during_collection_aborts_cleanly(self):
        class InterruptingCollector:
            def __init__(self):
                self.service = None

            def set_stop_check(self, check):
                self.check = check

            def collect(self):
                self.service.request_stop()
                raise CollectionStopped('stop requested')

        collector = InterruptingCollector()
        service = Service(self.store, collector, self.settings, run_id='run-interrupt',
                          engine_factory=IngestEngine, sleep=lambda seconds: None,
                          now_us=lambda: us(START))
        collector.service = service
        self.assertEqual(service.run(max_cycles=1), 'STOPPED_BY_SIGNAL')
        self.assertEqual(self.store.heartbeat_row(BOT)['status'], 'STOPPED_BY_SIGNAL')
        self.assertFalse(any(e['kind'] == 'data_error' for e in self.store.events(BOT)))

    def test_poll_interval_controls_healthy_cycle_delay(self):
        self.settings = Settings.from_env({'MVP_POLL_SECONDS': '5'}, base=self.base)
        collector = Collector([snap(0, 0), snap(0, 1), snap(0, 2)])
        delays = []
        service = Service(self.store, collector, self.settings, run_id='run-poll',
                          engine_factory=IngestEngine, sleep=delays.append,
                          now_us=lambda: us(START))
        self.assertEqual(service.run(max_cycles=3), 'CYCLE_LIMIT')
        self.assertAlmostEqual(sum(delays), 10.0)

    def test_throttle_backoff_is_bounded(self):
        self.settings = Settings.from_env({
            'MVP_MAX_CONSECUTIVE_ERRORS': '3',
            'MVP_BACKOFF_BASE_SECONDS': '2',
            'MVP_BACKOFF_MAX_SECONDS': '3',
            'MVP_POLL_SECONDS': '2',
        }, base=self.base)
        errors = [MarketDataError('rate_limited', 'throttled', http_status=429) for _ in range(3)]
        delays = []
        service = Service(self.store, Collector(errors), self.settings, run_id='run-throttle',
                          engine_factory=IngestEngine, sleep=delays.append,
                          now_us=lambda: us(START))
        self.assertEqual(service.run(max_cycles=10), 'FAILED_STOP')
        self.assertAlmostEqual(sum(delays), 5.0)
        self.assertTrue(all(0 < delay <= 0.25 for delay in delays))
        self.assertEqual(self.store.heartbeat_row(BOT)['status'], 'FAILED_STOP')

    def test_throttle_backoff_resets_after_success(self):
        self.settings = Settings.from_env({
            'MVP_MAX_CONSECUTIVE_ERRORS': '4',
            'MVP_BACKOFF_BASE_SECONDS': '2',
            'MVP_BACKOFF_MAX_SECONDS': '30',
            'MVP_POLL_SECONDS': '2',
        }, base=self.base)
        error = MarketDataError('rate_limited', 'throttled', http_status=429)
        delays = []
        service = Service(self.store, Collector([error, snap(0, 0), error, error]),
                          self.settings, run_id='run-reset', engine_factory=IngestEngine,
                          sleep=delays.append, now_us=lambda: us(START))
        self.assertEqual(service.run(max_cycles=4), 'CYCLE_LIMIT')
        self.assertAlmostEqual(sum(delays), 6.0)

    def test_two_windows_450_slots_ingested_without_decisions(self):
        collector = Collector([snap(window, slot) for window in range(2) for slot in range(450)])
        engine = self.engine(collector)
        statuses = [engine.step().status for _ in range(900)]
        self.assertEqual(set(statuses), {'OK'})
        self.assertEqual(self.store.window(BOT, '1001')['status'], 'UNKNOWN_SETTLEMENT')
        self.assertEqual(self.store.active_window(BOT)['market_id'], '1002')
        self.assertEqual(len(self.store.observations_for_market(BOT, '1001')), 900)
        self.assertEqual(len(self.store.observations_for_market(BOT, '1002')), 900)
        for table in TRADING_TABLES:
            with self.subTest(table=table):
                self.assertEqual(count_rows(self.store, table), 0)
        self.assertEqual(self.store.heartbeat_row(BOT)['cycles'], 900)


class IngestCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = self.base / 'ingest.sqlite'
        self.state = self.base / 'state'

    def test_cli_ingest_wires_only_the_pipeline(self):
        class FakeCollector:
            def __init__(self):
                self.closed = False

            def collect(self):
                return snap(0, 0)

            def close(self):
                self.closed = True

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        out = StringIO()
        with mock.patch.dict(os.environ, {'MODE': 'PAPER_ONLY'}), \
                mock.patch.object(cli, 'LiveCollector', FakeCollector), redirect_stdout(out):
            code = cli.main(['ingest', '--db', str(self.db), '--state-dir', str(self.state),
                             '--cycles', '1'])
        self.assertEqual(code, 0)
        self.assertIn('finished: CYCLE_LIMIT', out.getvalue())
        store = MvpStore.open(self.db)
        self.addCleanup(store.close)
        self.assertEqual(len(store.observations_for_market(BOT, '1001')), 2)
        self.assertEqual(store.heartbeat_row(BOT)['cycles'], 1)
        for table in TRADING_TABLES:
            with self.subTest(table=table):
                self.assertEqual(count_rows(store, table), 0)

    def test_cli_failed_stop_uses_non_restart_exit_code(self):
        class FailingCollector:
            def collect(self):
                raise MarketDataError('transport_error', 'down')

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        out = StringIO()
        with mock.patch.dict(os.environ, {'MODE': 'PAPER_ONLY',
                                          'MVP_MAX_CONSECUTIVE_ERRORS': '1'}), \
                mock.patch.object(cli, 'LiveCollector', FailingCollector), redirect_stdout(out):
            code = cli.main(['ingest', '--db', str(self.db), '--state-dir', str(self.state),
                             '--cycles', '5'])
        self.assertEqual(code, 75)
        self.assertIn('finished: FAILED_STOP', out.getvalue())

    def test_cli_ingest_refuses_live_mode_before_touching_state(self):
        from contextlib import redirect_stderr
        err = StringIO()
        with mock.patch.dict(os.environ, {'MODE': 'LIVE'}), redirect_stderr(err):
            code = cli.main(['ingest', '--db', str(self.db), '--state-dir', str(self.state)])
        self.assertEqual(code, 2)
        self.assertIn('configuration refused', err.getvalue())
        self.assertFalse(self.db.exists())


if __name__ == '__main__':
    unittest.main()
