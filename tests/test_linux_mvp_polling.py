"""Polling cadence: elapsed-time scheduling, no interval stacking, no negative sleep.

Root cause (paper24-6fe2d3a9): the service slept the full poll interval after each
cycle, so cadence = poll + processing (~2.895s observed against 2s slots); pending
orders skipped their single next-slot window and 6017 BUY attempts expired.
"""
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from polymarket_bot.linux_mvp.config import Settings
from polymarket_bot.linux_mvp.engine import PaperEngine, StepResult
from polymarket_bot.linux_mvp.service import Service
from polymarket_bot.linux_mvp.store import MvpStore
from polymarket_bot.market_data.models import MarketDataError
from tests.linux_mvp_support import MARKET_ID, START, Collector, rising, us

BOT = 'default'

NS = 1_000_000_000


class Clock:
    """Fake monotonic clock in nanoseconds; fake sleep advances it."""

    def __init__(self):
        self.now_ns = 0
        self.sleeps = []

    def monotonic_ns(self):
        return self.now_ns

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now_ns += int(round(seconds * NS))


class ScriptedEngine:
    """Records cycle-start monotonic times, burns processing time, returns script."""

    def __init__(self, results, durations, clock):
        self.results = results
        self.durations = durations
        self.clock = clock
        self.cycles = 0
        self.consecutive_errors = 0
        self.starts = []

    def step(self):
        self.starts.append(self.clock.now_ns)
        self.clock.now_ns += int(round(self.durations[self.cycles] * NS))
        result = self.results[self.cycles]
        self.cycles += 1
        return result


def ok(market_id='1001', slot=0):
    return StepResult('OK', market_id, slot)


def run_scripted(results, durations, *, env=None, clock=None):
    clock = clock or Clock()
    tmp = tempfile.TemporaryDirectory()
    settings = Settings.from_env(env or {}, base=Path(tmp.name))
    store = MvpStore.open(Path(tmp.name) / 'paper.sqlite')
    engine = ScriptedEngine(results, durations, clock)
    service = Service(store, collector=None, settings=settings, run_id='poll',
                      sleep=clock.sleep, now_us=lambda: 0, monotonic_ns=clock.monotonic_ns,
                      engine_factory=lambda *args, **kwargs: engine, log=lambda _: None)
    status = service.run(max_cycles=len(results))
    store.close()
    return clock, engine, status


class CadenceTests(unittest.TestCase):
    def test_processing_time_no_longer_stacks_onto_poll_interval(self):
        durations = [0.4, 0.9, 0.05, 0.4]
        clock, engine, _ = run_scripted([ok()] * 4, durations)
        deltas = [(engine.starts[i + 1] - engine.starts[i]) / NS for i in range(3)]
        for delta in deltas:
            self.assertAlmostEqual(delta, 2.0, places=6)

    def test_cycle_slower_than_interval_never_sleeps_negative(self):
        # Fast cycle then slow cycle: interval covers processing, remainder sleeps.
        clock, engine, _ = run_scripted([ok(), ok()], [0.2, 2.5])
        delta = (engine.starts[1] - engine.starts[0]) / NS
        self.assertAlmostEqual(delta, 2.0, places=6)
        self.assertAlmostEqual(sum(clock.sleeps), 1.8, places=6)
        self.assertTrue(all(delay >= 0 for delay in clock.sleeps))
        # Overrunning cycle starts the next cycle immediately, never with negative sleep.
        clock, engine, _ = run_scripted([ok(), ok()], [2.5, 0.2])
        delta = (engine.starts[1] - engine.starts[0]) / NS
        self.assertAlmostEqual(delta, 2.5, places=6)
        self.assertTrue(all(delay >= 0 for delay in clock.sleeps))

    def test_slot_uncertain_cycles_retry_at_fast_cadence(self):
        results = [StepResult('SLOT_UNCERTAIN', '1001', 1), ok()]
        clock, engine, _ = run_scripted(results, [0.1, 0.1])
        delta = (engine.starts[1] - engine.starts[0]) / NS
        self.assertAlmostEqual(delta, 2.0, places=6)

    def test_unavailable_market_statuses_keep_poll_cadence(self):
        for status in ('WINDOW_EXPIRED', 'OUT_OF_WINDOW', 'WINDOW_ABANDONED'):
            with self.subTest(status=status):
                clock, engine, _ = run_scripted(
                    [StepResult(status, '1001', 1), ok()], [0.1, 0.1])
                delta = (engine.starts[1] - engine.starts[0]) / NS
                self.assertAlmostEqual(delta, 2.1, places=6)

    def test_error_backoff_ignores_elapsed_processing(self):
        results = [StepResult('ERROR', '1001', 1, reason='transport_error'), ok()]
        clock, engine, _ = run_scripted(results, [0.3, 0.3])
        delta = (engine.starts[1] - engine.starts[0]) / NS
        self.assertAlmostEqual(delta, 2.3, places=6)


class ServiceFillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = MvpStore.open(self.base / 'paper.sqlite')
        self.addCleanup(self.store.close)
        self.settings = Settings.from_env({'MVP_POLL_SECONDS': '1'}, base=self.base)
        self.clock = Clock()

    def service(self, samples):
        self.now = us(START)
        collector = Collector(samples)
        engine = PaperEngine(self.store, collector, self.settings, run_id='poll-fill',
                             now_us=lambda: self.now)
        service = Service(self.store, collector, self.settings, run_id='poll-fill',
                          sleep=self.clock.sleep, now_us=lambda: self.now,
                          monotonic_ns=self.clock.monotonic_ns,
                          engine_factory=lambda *a, **k: engine, log=lambda _: None)
        return service

    def advance(self):
        self.now = us(START) + self.clock.now_ns // 1000

    def test_one_second_cadence_reaches_existing_paper_fill_path(self):
        service = self.service([rising(0), rising(1), rising(2), rising(3)])
        status = service.run(max_cycles=4)
        self.advance()
        self.assertEqual(status, 'CYCLE_LIMIT')
        orders = self.store.orders(BOT)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]['status'], 'FILLED')
        self.assertEqual(orders[0]['side'], 'BUY')

    def test_missed_slot_still_expires_instead_of_filling_late(self):
        service = self.service([rising(0), rising(1), rising(2), rising(4)])
        status = service.run(max_cycles=4)
        self.advance()
        self.assertEqual(status, 'CYCLE_LIMIT')
        orders = self.store.orders(BOT)
        expired = [o for o in orders if o['reason'] == 'EXPIRED_EXECUTION_WINDOW']
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]['status'], 'REJECTED')
        self.assertEqual(expired[0]['filled'], Decimal('0'))
        self.assertTrue(all(o['status'] != 'FILLED' for o in orders))

    def test_invalid_and_stale_data_never_create_fills(self):
        from dataclasses import replace
        stale = rising(2)
        old = stale.up.book.source_at - timedelta(seconds=20)
        stale = replace(stale, up=replace(stale.up, book=replace(stale.up.book,
                        source_at=old, source_timestamp_raw=str(us(old) // 1000))))
        service = self.service([rising(0), replace(rising(1), validated_at=None),
                                stale, rising(3)])
        status = service.run(max_cycles=4)
        self.advance()
        self.assertEqual(status, 'CYCLE_LIMIT')
        self.assertEqual(self.store.orders(BOT), [])
        import json
        proofs = [json.loads(e['detail']) for e in self.store.events(BOT)
                  if e['kind'] == 'freshness_evaluated']
        self.assertFalse(proofs[1]['freshness']['valid'])  # invalid timing cycle
        self.assertFalse(proofs[2]['freshness']['outcomes']['UP']['fresh'])  # stale cycle
        reasons = [d['reason'] for d in self.store.decisions_for_market(BOT, MARKET_ID)]
        self.assertEqual(reasons, ['WARMUP', 'STALE_PAIR', 'WARMUP'])
        self.assertTrue(all(d['action'] == 'HOLD' for d in
                            self.store.decisions_for_market(BOT, MARKET_ID)))


if __name__ == '__main__':
    unittest.main()
