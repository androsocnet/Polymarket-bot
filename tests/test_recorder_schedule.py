"""Window-aligned scheduling with deterministic injected clocks."""
import importlib
import unittest
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

from polymarket_bot.market_data.http import JsonRead
from polymarket_bot.market_data.models import MarketDataError

from polymarket_bot.market_data.parsing import parse_market
from polymarket_bot.recorder.models import RunConfig, SlotTarget
from polymarket_bot.recorder.store import RecorderStore, RecorderStoreError
from tests.recorder_support import DatabaseFixture, NOW_US, WINDOW_START_US, seed_writer_market
from tests.support import MARKET_ID, NOW, make_event, make_market


class SlotGridTests(unittest.TestCase):
    def test_grid_is_exactly_450_window_aligned_slots(self):
        # Wrong cadence, count or index origin loses/crosses window slots.
        try:
            schedule = importlib.import_module("polymarket_bot.recorder.schedule")
        except ModuleNotFoundError:
            self.fail("Recorder slot scheduling is not implemented")
        grid = schedule.slot_grid(1_000_000_000)
        self.assertEqual(len(grid), 450)
        self.assertEqual([s.index for s in grid], list(range(450)))
        self.assertEqual(grid[0].scheduled_us, 1_000_000_000)
        self.assertEqual(grid[1].scheduled_us, 1_002_000_000)
        self.assertEqual(grid[-1].scheduled_us, 1_898_000_000)
        self.assertTrue(all(b.scheduled_us - a.scheduled_us == 2_000_000
                            for a, b in zip(grid, grid[1:])))

    def test_grid_rejects_noninteger_and_overflowing_persisted_targets(self):
        from polymarket_bot.recorder.schedule import slot_grid
        for value in (True, 1.5, 2**63 - 1, -2**63 - 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                slot_grid(value)


class SlotSelectionTests(unittest.TestCase):
    def setUp(self):
        self.schedule = importlib.import_module("polymarket_bot.recorder.schedule")
        self.market = parse_market(make_event(), make_market(), now=NOW)

    def select(self, offset, terminal=()):
        self.assertTrue(hasattr(self.schedule, "select_future_slot"),
                        "Future-only slot selection is not implemented")
        return self.schedule.select_future_slot(
            self.market, now_us=WINDOW_START_US + offset,
            terminal_indices=set(terminal))

    def test_start_midwindow_selects_future_slot(self):
        selection = self.select(3_000_000)
        self.assertEqual(selection.missed, (0, 1))
        self.assertEqual(selection.target, SlotTarget(2, WINDOW_START_US + 4_000_000))

    def test_armed_late_wake_versus_passed_unarmed_slot(self):
        self.assertTrue(hasattr(self.schedule, "classify_wake"),
                        "Armed wake classification is not implemented")
        target = SlotTarget(1, WINDOW_START_US + 2_000_000)
        for delay, expected in ((0, "CAPTURE"), (1_900_000, "CAPTURE"),
                                (2_000_000, "MISSED"), (9_000_000, "MISSED")):
            with self.subTest(delay=delay):
                self.assertEqual(self.schedule.classify_wake(
                    target, now_us=target.scheduled_us + delay,
                    window_end_us=WINDOW_START_US + 900_000_000), expected)
        # An unarmed slot has no allowance for even one microsecond of lateness.
        selection = self.select(2_000_001, (0,))
        self.assertEqual(selection.missed, (1,))
        self.assertEqual(selection.target.index, 2)

    def test_processing_overrun_skips_all_passed_slots_without_burst(self):
        selection = self.select(9_900_000, (0, 1))
        self.assertEqual(selection.missed, (2, 3, 4))
        self.assertEqual(selection.target, SlotTarget(5, WINDOW_START_US + 10_000_000))

    def test_exact_unarmed_target_is_not_future(self):
        selection = self.select(4_000_000, (0, 1))
        self.assertEqual(selection.missed, (2,))
        self.assertEqual(selection.target.index, 3)

    def test_terminal_slots_are_never_reselected_or_remissed(self):
        selection = self.select(3_000_000, (0, 2, 3))
        self.assertEqual(selection.missed, (1,))
        self.assertEqual(selection.target.index, 4)
        self.assertIsNone(self.select(-1, range(450)).target)

    def test_market_expiry_and_last_armed_slot_have_no_next_window_capture(self):
        selection = self.select(900_000_000, range(449))
        self.assertEqual(selection.missed, (449,))
        self.assertIsNone(selection.target)
        self.assertTrue(hasattr(self.schedule, "classify_wake"))
        target = SlotTarget(449, WINDOW_START_US + 898_000_000)
        self.assertEqual(self.schedule.classify_wake(
            target, now_us=WINDOW_START_US + 900_000_000,
            window_end_us=WINDOW_START_US + 900_000_000), "MISSED")


    def test_armed_wake_before_target_is_missed_not_an_early_request(self):
        self.assertTrue(hasattr(self.schedule, "classify_wake"))
        self.assertEqual(self.schedule.classify_wake(
            SlotTarget(1, WINDOW_START_US + 2_000_000),
            now_us=WINDOW_START_US + 1_999_999,
            window_end_us=WINDOW_START_US + 900_000_000), "MISSED")

    def test_selection_and_wake_reject_noninteger_timing_and_bad_indices(self):
        with self.assertRaises(ValueError):
            self.select(0.5)
        for terminal in ({True}, {-1}, {450}):
            with self.subTest(terminal=terminal), self.assertRaises(ValueError):
                self.select(0, terminal)
        with self.assertRaises(ValueError):
            self.schedule.classify_wake(SlotTarget(0, WINDOW_START_US),
                now_us=WINDOW_START_US + 0.5, window_end_us=WINDOW_START_US + 900_000_000)


class InjectedClock:
    def __init__(self, mono_ns=12_000_000_000, wall_us=123_456_789):
        self.mono_ns = mono_ns
        self.wall_us = wall_us

    def read(self, seconds=1_791_593_400, *, start=10_000_000_000,
             end=12_000_000_000, start_wall=111_111_111, end_wall=222_222_222):
        self.mono_ns = end
        return JsonRead(seconds, start / 1_000_000_000, end / 1_000_000_000,
                        str(seconds).encode(), start, end, start_wall, end_wall)


class RecorderClockTests(unittest.TestCase):
    def setUp(self):
        try:
            clock_module = importlib.import_module("polymarket_bot.recorder.clock")
        except ModuleNotFoundError:
            self.fail("Calibrated recorder clock is not implemented")
        self.injected = InjectedClock()
        self.clock = clock_module.RecorderClock(
            monotonic_ns=lambda: self.injected.mono_ns,
            wall_us=lambda: self.injected.wall_us)

    def test_midpoint_uncertainty_and_observed_wall_times_are_integer_evidence(self):
        anchor = self.clock.calibrate(self.injected.read())
        self.assertEqual((anchor.server_seconds, anchor.midpoint_ns, anchor.server_us,
                          anchor.uncertainty_us, anchor.status),
                         (1_791_593_400, 11_000_000_000, 1_791_593_400_000_000,
                          2_000_000, "VALID"))
        self.assertEqual((anchor.request_start_mono_ns, anchor.request_end_mono_ns,
                          anchor.request_start_wall_us, anchor.request_end_wall_us),
                         (10_000_000_000, 12_000_000_000, 111_111_111, 222_222_222))
        self.assertEqual(self.clock.server_us_at(11_234_567_890), 1_791_593_400_234_567)
        self.assertEqual(self.clock.deadline_ns(1_791_593_402_000_001), 13_000_001_000)
        self.assertEqual(self.clock.now(), datetime(2026, 10, 10, 0, 50, 1, tzinfo=timezone.utc))
        self.injected.wall_us = -1_000_000_000
        self.assertEqual(self.clock.now(), datetime(2026, 10, 10, 0, 50, 1, tzinfo=timezone.utc))

    def test_fractional_microsecond_rtt_rounds_uncertainty_up(self):
        anchor = self.clock.calibrate(self.injected.read(start=10, end=13))
        self.assertEqual(anchor.midpoint_ns, 11)
        self.assertEqual(anchor.uncertainty_us, 1_000_001)

    def test_anchor_bounds_and_epoch(self):
        self.assertFalse(self.clock.usable())
        self.assertTrue(self.clock.refresh_due())
        with self.assertRaises(MarketDataError):
            self.clock.now()
        self.clock.calibrate(self.injected.read())
        for moment, refresh, usable in ((70_999_999_999, False, True),
                                         (71_000_000_000, True, True),
                                         (131_000_000_000, True, True),
                                         (131_000_000_001, True, False),
                                         (132_000_000_000, True, False)):
            self.injected.mono_ns = moment
            self.assertEqual(self.clock.refresh_due(), refresh)
            self.assertEqual(self.clock.usable(), usable)
        with self.assertRaises(MarketDataError):
            self.clock.deadline_ns(1_791_593_600_000_000)
        new_clock = type(self.clock)(monotonic_ns=lambda: 100, wall_us=lambda: 0)
        self.assertFalse(new_clock.usable())
        with self.assertRaises(MarketDataError):
            new_clock.server_us_at(100)
        self.injected.mono_ns = 1
        self.assertFalse(self.clock.usable())

    def test_discontinuity_exceeds_combined_uncertainty_plus_one_second(self):
        self.clock.calibrate(self.injected.read(seconds=1000, start=0, end=2_000_000_000))
        # Old/new uncertainties both 2 seconds; +5 seconds exactly is allowed.
        boundary = self.clock.calibrate(self.injected.read(
            seconds=1065, start=60_000_000_000, end=62_000_000_000))
        self.assertEqual(boundary.status, "VALID")
        shifted = self.clock.calibrate(self.injected.read(
            seconds=1131, start=120_000_000_000, end=122_000_000_000))
        self.assertEqual(shifted.status, "DISCONTINUITY")
        self.assertEqual(shifted.diagnostic["shift_us"], 6_000_000)
        self.assertFalse(self.clock.usable())
        with self.assertRaises(MarketDataError):
            self.clock.now()
        fresh = self.clock.calibrate(self.injected.read(
            seconds=1133, start=122_000_000_000, end=124_000_000_000))
        self.assertEqual(fresh.status, "VALID")
        self.assertTrue(self.clock.usable())

    def test_negative_discontinuity_also_pauses(self):
        self.clock.calibrate(self.injected.read(seconds=1000, start=0, end=0))
        shifted = self.clock.calibrate(self.injected.read(seconds=997, start=1_000_000_000,
                                                        end=1_000_000_000))
        self.assertEqual(shifted.status, "DISCONTINUITY")
        self.assertEqual(shifted.diagnostic["shift_us"], -4_000_000)
        self.assertFalse(self.clock.usable())

    def test_legacy_float_timing_is_explicit_and_wall_start_is_estimated(self):
        self.injected.mono_ns = 12_000_000_000
        anchor = self.clock.calibrate(JsonRead(1000, 10.0, 12.0))
        self.assertEqual(anchor.midpoint_ns, 11_000_000_000)
        self.assertEqual(anchor.diagnostic["timing_source"], "LEGACY_FLOAT_DERIVED")
        self.assertEqual(anchor.request_end_wall_us, 123_456_789)
        self.assertEqual(anchor.request_start_wall_us, 121_456_789)

    def test_invalid_samples_do_not_replace_a_valid_anchor(self):
        anchor = self.clock.calibrate(self.injected.read())
        valid = self.injected.read()
        cases = [replace(valid, payload=bad) for bad in (True, "1000", -1, 0, 2**10000,
                                                        253_402_300_800)]
        cases += [replace(valid, started_mono_ns=13_000_000_000),
                  replace(valid, finished_mono_ns=13_000_000_000),
                  replace(valid, started_wall_us=None),
                  replace(valid, started_mono_ns=-1),
                  JsonRead(1000, float("nan"), 12.0)]
        for read in cases:
            with self.subTest(payload_type=type(read.payload).__name__), self.assertRaises(MarketDataError):
                self.clock.calibrate(read)
            self.assertIs(self.clock.anchor, anchor)
        self.assertTrue(self.clock.usable())

    def test_mapping_rejects_noninteger_or_unpersistable_targets(self):
        self.clock.calibrate(self.injected.read())
        for target in (True, 1.5, 2**10000, -2**10000):
            with self.subTest(target_type=type(target).__name__), self.assertRaises(MarketDataError):
                self.clock.deadline_ns(target)
        with self.assertRaises(MarketDataError):
            self.clock.deadline_ns(2**63 - 1)

    def test_legacy_oversized_integer_timing_becomes_structured_failure(self):
        with self.assertRaises(MarketDataError):
            self.clock.calibrate(JsonRead(1000, 2**10000, 2**10000))


class AnchorPersistenceTests(unittest.TestCase):
    def setUp(self):
        from polymarket_bot.recorder.clock import RecorderClock
        self.fixture = DatabaseFixture()
        self.addCleanup(self.fixture.close)
        self.store = RecorderStore.open(self.fixture.path)
        self.addCleanup(self.store.close)
        self.run_id = self.store.start_run(RunConfig(), wall_us=123, app_revision="test")
        self.injected = InjectedClock()
        self.clock = RecorderClock(monotonic_ns=lambda: self.injected.mono_ns,
                                   wall_us=lambda: self.injected.wall_us)
        self.anchor = self.clock.calibrate(self.injected.read())

    def save(self, anchor=None, run_id=None):
        self.assertTrue(hasattr(self.store, "save_anchor"), "Anchor persistence is not implemented")
        return self.store.save_anchor(run_id or self.run_id, anchor or self.anchor)

    def rows(self):
        connection = sqlite3.connect(self.fixture.path)
        try:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM clock_anchors ORDER BY anchor_id")]
        finally:
            connection.close()

    def test_anchor_insert_is_append_only_with_exact_observations(self):
        first = self.save()
        second = self.save()
        self.assertNotEqual(first, second)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], dict(anchor_id=first, run_id=self.run_id,
            server_seconds=1_791_593_400, request_start_mono_ns=10_000_000_000,
            request_end_mono_ns=12_000_000_000, request_start_wall_us=111_111_111,
            request_end_wall_us=222_222_222, anchor_mono_ns=11_000_000_000,
            anchor_server_us=1_791_593_400_000_000, uncertainty_us=2_000_000,
            status="VALID", diagnostic_json=None))

    def test_discontinuity_is_retained_as_invalid_anchor_evidence(self):
        self.save()
        shifted = self.clock.calibrate(self.injected.read(seconds=1_791_593_466,
                                                        start=70_000_000_000,
                                                        end=72_000_000_000))
        self.save(shifted)
        rows = self.rows()
        self.assertEqual(rows[0]["status"], "VALID")
        self.assertEqual(rows[1]["status"], "DISCONTINUITY")
        self.assertEqual(json.loads(rows[1]["diagnostic_json"]),
                         {"shift_us": 6_000_000, "threshold_us": 5_000_000})

    def test_restart_saves_distinct_run_epochs_without_reusing_clock(self):
        self.save()
        self.store.close()
        restarted_store = RecorderStore.open(self.fixture.path)
        self.addCleanup(restarted_store.close)
        new_run = restarted_store.start_run(RunConfig(), wall_us=456, app_revision="test")
        new_injected = InjectedClock()
        new_clock = type(self.clock)(monotonic_ns=lambda: new_injected.mono_ns,
                                     wall_us=lambda: new_injected.wall_us)
        self.assertFalse(new_clock.usable())
        new_anchor = new_clock.calibrate(new_injected.read(seconds=1_791_593_500, start=100, end=200))
        restarted_store.save_anchor(new_run, new_anchor)
        rows = self.rows()
        self.assertEqual([row["run_id"] for row in rows], [self.run_id, new_run])
        self.assertEqual([row["anchor_mono_ns"] for row in rows], [11_000_000_000, 150])
        self.assertEqual(rows[0]["anchor_server_us"], 1_791_593_400_000_000)
        self.assertEqual(new_clock.server_us_at(1150), 1_791_593_500_000_001)

    def test_anchor_writer_rejects_bad_run_and_inconsistent_integer_evidence(self):
        self.assertTrue(hasattr(self.store, "save_anchor"), "Anchor writer validation is missing")
        with self.assertRaises(RecorderStoreError):
            self.store.save_anchor("unknown", self.anchor)
        changes = ({"server_seconds": True}, {"server_us": 2**63},
                   {"midpoint_ns": 0}, {"server_us": 1},
                   {"uncertainty_us": 0}, {"request_end_mono_ns": 9_000_000_000},
                   {"status": "UNAVAILABLE"}, {"request_start_wall_us": 1.5})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(RecorderStoreError):
                self.store.save_anchor(self.run_id, replace(self.anchor, **change))
        self.assertEqual(self.rows(), [])
        self.save()
        self.assertEqual(len(self.rows()), 1)

    def test_selected_misses_keep_start_and_downtime_reasons_without_book_requests(self):
        from polymarket_bot.recorder.schedule import select_future_slot, slot_grid
        seed_writer_market(self.store, self.run_id)
        market = parse_market(make_event(), make_market(), now=NOW)
        grid = slot_grid(WINDOW_START_US)
        initial = select_future_slot(market, now_us=WINDOW_START_US + 3_000_000,
                                     terminal_indices=set())
        for index in initial.missed:
            self.store.mark_missed(MARKET_ID, grid[index], run_id=self.run_id,
                                   reason="BEFORE_START", wall_us=123)
        resumed = select_future_slot(market, now_us=WINDOW_START_US + 5_000_000,
                                     terminal_indices={0, 1})
        for index in resumed.missed:
            self.store.mark_missed(MARKET_ID, grid[index], run_id=self.run_id,
                                   reason="DOWNTIME", wall_us=456)
        rows = self.store._rows("SELECT slot_index,status,reason FROM sample_slots ORDER BY slot_index")
        self.assertEqual(rows, [dict(slot_index=0, status="MISSED", reason="BEFORE_START"),
                                dict(slot_index=1, status="MISSED", reason="BEFORE_START"),
                                dict(slot_index=2, status="MISSED", reason="DOWNTIME")])
        self.assertEqual(resumed.target.index, 3)
        self.assertEqual(self.store._rows("SELECT * FROM request_attempts"), [])
        self.assertEqual(self.store._rows("SELECT * FROM book_observations"), [])

    def test_armed_lateness_is_preserved_when_reserving_selected_target(self):
        from polymarket_bot.recorder.schedule import classify_wake, select_future_slot
        seed_writer_market(self.store, self.run_id)
        market = parse_market(make_event(), make_market(), now=NOW)
        target = select_future_slot(market, now_us=NOW_US + 1_000_000,
                                    terminal_indices=set(range(151))).target
        self.assertEqual(target, SlotTarget(151, NOW_US + 2_000_000))
        wake_us = NOW_US + 3_900_000
        self.assertEqual(classify_wake(target, now_us=wake_us,
                                      window_end_us=WINDOW_START_US + 900_000_000), "CAPTURE")
        self.store.reserve_slot(MARKET_ID, target, run_id=self.run_id, anchor_id=1,
                                metadata_revision_id=1, started_mono_ns=3_900_000_000,
                                started_server_us=wake_us)
        row = self.store._rows("SELECT slot_index,status,delay_us FROM sample_slots")[0]
        self.assertEqual(row, dict(slot_index=151, status="IN_PROGRESS", delay_us=1_900_000))
