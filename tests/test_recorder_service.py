"""Offline coordinator behavior through actual adapters and the owned database."""
import json
import sqlite3
import unittest
from unittest.mock import patch

import httpx

from polymarket_bot.recorder.models import RunConfig
from tests.recorder_support import RecorderFixture, NOW_US
from polymarket_bot.recorder.store import decode_evidence
from polymarket_bot.recorder.store import RecorderStore, RecorderStoreError


class RecorderServiceTests(unittest.TestCase):
    def test_checkpoint_baseline_precedes_capture_and_shutdown_precedes_close(self):
        fixture = self.fixture()
        def before_capture(request,payload,f):
            if request.url.path=="/books":
                self.assertEqual(f.store._connection.execute("SELECT COUNT(*) FROM storage_checkpoints").fetchone()[0],1)
        fixture.hook = before_capture
        result = self.run_recorder(fixture,pairs=1)
        self.assertEqual(result.exit_code,0)
        checkpoints = fixture.rows("SELECT * FROM storage_checkpoints ORDER BY checkpoint_id")
        self.assertEqual(len(checkpoints),2)
        self.assertEqual([row["committed_slot_count"] for row in checkpoints],[0,152])
        self.assertEqual(checkpoints[-1]["db_file_bytes"],fixture.db.path.stat().st_size)

    def test_hourly_checkpoint_overhead_cannot_burst_or_change_cadence(self):
        fixture = self.fixture()
        def long_gap(request,payload,f):
            if request.url.path=="/books" and f.book_count==1:
                f.advance(3601)
        fixture.hook = long_gap
        from polymarket_bot.recorder import service
        self.assertTrue(hasattr(service,"take_checkpoint"),"Coordinator checkpoint integration is missing")
        real_checkpoint = service.take_checkpoint
        observed = []
        def slow_checkpoint(store,*,run_id,wall_us):
            self.assertFalse(store._connection.in_transaction)
            real_checkpoint(store,run_id=run_id,wall_us=wall_us)
            observed.append(fixture.monotonic_ns())
            if len(observed)==2:
                fixture.advance(3)
        with patch.object(service,"take_checkpoint",side_effect=slow_checkpoint):
            result = self.run_recorder(fixture,pairs=3)
        self.assertEqual(result.exit_code,0)
        checkpoints = fixture.rows("SELECT * FROM storage_checkpoints ORDER BY checkpoint_id")
        self.assertEqual(len(checkpoints),3)
        self.assertGreaterEqual(checkpoints[1]["observed_wall_us"]-checkpoints[0]["observed_wall_us"],3_600_000_000)
        books = [row[2] for row in fixture.requests if row[1]=="/books"]
        self.assertTrue(all(b-a>=2_000_000_000 for a,b in zip(books,books[1:])))
        config = json.loads(fixture.rows("SELECT config_json FROM recording_runs")[0]["config_json"])
        self.assertEqual(config["cadence_us"],2_000_000)

    def test_checkpoint_deferred_when_armed_target_has_no_budget(self):
        fixture = self.fixture()
        from polymarket_bot.recorder.service import Recorder
        recorder = Recorder(fixture.store,fixture.public,monotonic_ns=fixture.monotonic_ns,
                            wall_us=fixture.wall_us,sleep=fixture.sleep)
        self.assertTrue(hasattr(recorder,"_checkpoint_due"),"Budget-aware checkpoint scheduling is missing")
        recorder.run_id = fixture.store.start_run(RunConfig(),wall_us=fixture.wall_us(),app_revision="fixture")
        recorder.next_checkpoint_ns = fixture.monotonic_ns()
        recorder._checkpoint_due(fixture.monotonic_ns()+500_000)
        self.assertEqual(len(fixture.rows("SELECT * FROM storage_checkpoints")),0)
        recorder._checkpoint_due(fixture.monotonic_ns()+2_000_000_000)
        self.assertEqual(len(fixture.rows("SELECT * FROM storage_checkpoints")),1)

    def test_restart_keeps_each_run_startup_baseline(self):
        fixture = self.fixture()
        self.run_recorder(fixture,pairs=1)
        fixture.store = RecorderStore.open(fixture.db.path)
        fixture.advance(4)
        result = self.run_recorder(fixture,pairs=2)
        self.assertEqual(result.exit_code,0)
        rows = fixture.rows("SELECT run_id,COUNT(*) n,MIN(observed_wall_us) first FROM storage_checkpoints GROUP BY run_id")
        self.assertEqual(len(rows),2)
        self.assertTrue(all(row["n"]==2 for row in rows))

    def fixture(self, **kwargs):
        fixture = RecorderFixture(**kwargs)
        self.addCleanup(fixture.close)
        return fixture

    def run_recorder(self, fixture, *, pairs=2, should_stop=None, config=None):
        from polymarket_bot.recorder.service import Recorder
        return Recorder(fixture.store, fixture.public,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us,
            sleep=fixture.sleep).run(config or RunConfig(),
                should_stop=should_stop or (lambda: fixture.book_count >= pairs))

    def test_reserve_batch_atomic_finalize_and_future_grid(self):
        fixture = self.fixture()
        def inspect(request, payload, f):
            if request.url.path == "/books":
                row = f.store._connection.execute(
                    "SELECT status FROM sample_slots ORDER BY slot_index DESC LIMIT 1").fetchone()
                self.assertEqual(row[0], "IN_PROGRESS")
        fixture.hook = inspect
        result = self.run_recorder(fixture)
        self.assertEqual((result.state, result.exit_code, result.slots_finalized), ("STOPPED", 0, 2))
        slots = fixture.rows("SELECT * FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_index")
        self.assertEqual([row["slot_index"] for row in slots], [151, 152])
        self.assertEqual([row["scheduled_us"] for row in slots], [NOW_US + 2_000_000, NOW_US + 4_000_000])
        self.assertEqual(len(fixture.rows("SELECT * FROM book_observations")), 4)
        attempts = fixture.rows("SELECT * FROM request_attempts WHERE operation='BOOK_BATCH'")
        self.assertEqual(len(attempts), 2)
        self.assertEqual({row["method"] for row in attempts}, {"POST"})
        self.assertEqual({row["path"] for row in attempts}, {"/books"})
        self.assertEqual([row["start_mono_ns"] for row in attempts],
                         [fixture.epoch_ns + 2_005_000_000, fixture.epoch_ns + 4_005_000_000])
        anchor = fixture.rows("SELECT * FROM clock_anchors")[0]
        self.assertEqual(anchor["request_start_mono_ns"], fixture.epoch_ns)
        self.assertEqual(anchor["anchor_mono_ns"], fixture.epoch_ns + 5_000_000)
        self.assertEqual(anchor["diagnostic_json"], None)
        self.assertTrue(all(row["status"] == "DISABLED" for row in fixture.rows("SELECT * FROM reference_associations")))

    def test_old_and_new_market_never_share_a_pair(self):
        fixture = self.fixture(offset_us=597_000_000)
        result = self.run_recorder(fixture)
        self.assertEqual(result.slots_finalized, 2)
        slots = fixture.rows("SELECT market_id,slot_index FROM sample_slots WHERE status='COMPLETE' ORDER BY scheduled_us")
        self.assertEqual([(row["market_id"], row["slot_index"]) for row in slots], [("1001", 449), ("1002", 1)])
        books = fixture.rows("""SELECT m.condition_id,b.expected_condition_id FROM book_observations b
            JOIN sample_slots s USING(slot_id) JOIN markets m USING(market_id)""")
        self.assertTrue(all(row["condition_id"] == row["expected_condition_id"] for row in books))
        old = fixture.rows("SELECT * FROM markets WHERE market_id='1001'")[0]
        self.assertEqual(old["lifecycle"], "EXPIRED")
        for method, path, ns, timeout, body in fixture.requests:
            if path == "/books" and "9000000000000000000001" in body.decode():
                self.assertLess(fixture.initial_wall_us + (ns-fixture.epoch_ns)//1000, NOW_US+600_000_000)

    def test_rollover_gap_includes_successful_acquisition_and_retry_wait(self):
        from polymarket_bot.recorder.metrics import measure
        for fail_first in (False, True):
            with self.subTest(fail_first=fail_first):
                fixture = self.fixture(offset_us=597_000_000)
                failures = []
                def outage(request, payload, f):
                    if (fail_first and f.book_count == 1 and request.url.path.startswith("/events/slug/")
                            and not failures):
                        failures.append(True)
                        f.advance(10.2)
                        return httpx.Response(503, stream=httpx.ByteStream(b"outage"))
                fixture.hook = outage
                result = self.run_recorder(fixture, pairs=2)
                self.assertEqual(result.exit_code, 0)
                old, new = fixture.rows("SELECT * FROM markets ORDER BY window_start_us")
                expected_us = new["first_seen_us"]-old["window_end_us"]
                self.assertEqual(expected_us, 12_230_000 if fail_first else 20_000)
                reader = RecorderStore.read_only(fixture.db.path)
                try:
                    report = measure(reader, start_us=fixture.initial_wall_us,
                                     end_us=fixture.wall_us()+1_000_000)
                finally:
                    reader.close()
                self.assertIsNone(report.diagnostics["quality_time_unavailable_reason"])
                self.assertEqual(report.rollover_gaps["count"], 1)
                self.assertAlmostEqual(report.rollover_gaps["duration_seconds"], expected_us/1_000_000)
                actual = [row for row in fixture.rows("SELECT * FROM recorder_events WHERE kind='DISCOVERY_GAP'")
                          if not json.loads(row["detail_json"]).get("diagnostic_only")]
                self.assertEqual(len(actual), 1)
                self.assertEqual((actual[0]["started_us"], actual[0]["ended_us"]),
                                 (old["window_end_us"], new["first_seen_us"]))

    def test_bad_numeric_payload_does_not_end_run(self):
        fixture = self.fixture()
        def malformed(request, payload, f):
            if request.url.path == "/books" and f.book_count == 1:
                for row in payload:
                    row["bids"][0]["size"] = "1e99999999999999999999999"
        fixture.hook = malformed
        result = self.run_recorder(fixture)
        self.assertEqual((result.exit_code, result.slots_finalized), (0, 2))
        slots = fixture.rows("SELECT slot_index,status FROM sample_slots WHERE status!='MISSED' ORDER BY slot_index")
        self.assertEqual([(row["slot_index"], row["status"]) for row in slots], [(151, "FAILED"), (152, "COMPLETE")])
        self.assertEqual([row["status"] for row in fixture.rows("SELECT status FROM book_observations WHERE slot_id=(SELECT slot_id FROM sample_slots WHERE slot_index=151)")], ["INVALID", "INVALID"])

    def test_extreme_aware_gamma_timestamps_are_discovery_failures(self):
        for timestamp in ("0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"):
            with self.subTest(timestamp=timestamp):
                fixture = self.fixture()
                def invalid(request, payload, f):
                    if request.url.path.startswith("/events/slug/"):
                        payload["markets"][0]["eventStartTime"] = timestamp
                    elif request.url.path == "/events":
                        payload[0]["markets"][0]["eventStartTime"] = timestamp
                fixture.hook = invalid
                result = self.run_recorder(fixture, should_stop=lambda: fixture.elapsed_ns > 1_000_000_000)
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(fixture.book_count, 0)
                events = fixture.rows("SELECT detail_json FROM recorder_events WHERE kind='DISCOVERY_GAP'")
                self.assertTrue(events)
                self.assertIn("invalid_metadata", events[0]["detail_json"])

    def test_fractional_fallback_window_keeps_exact_slot_identity(self):
        fixture = self.fixture()
        from datetime import datetime, timedelta
        def fractional(request, payload, f):
            if request.url.path == "/events":
                payload[0]["slug"] = "fractional-event"
                rows = payload[0]["markets"]
            elif request.url.path.startswith("/markets/"):
                rows = [payload]
            else:
                return
            for row in rows:
                row["slug"] = "fractional-market"
                for field in ("eventStartTime", "endDate"):
                    row[field] = (datetime.fromisoformat(row[field]) + timedelta(microseconds=500_000)).isoformat()
        fixture.hook = fractional
        result = self.run_recorder(fixture, pairs=2, config=RunConfig(use_fast_path=False))
        self.assertEqual((result.exit_code, result.slots_finalized), (0, 2))
        slots = fixture.rows("SELECT scheduled_us FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_index")
        self.assertEqual([row["scheduled_us"] for row in slots], [NOW_US+500_000, NOW_US+2_500_000])
        self.assertEqual(fixture.rows("SELECT window_start_us FROM markets")[0]["window_start_us"], NOW_US-299_500_000)

    def test_partial_outcome_survives_independently(self):
        fixture = self.fixture()
        def partial(request, payload, f):
            if request.url.path == "/books":
                payload[0]["bids"][0]["price"] = "NaN"
        fixture.hook = partial
        self.run_recorder(fixture, pairs=1)
        self.assertEqual(fixture.rows("SELECT status FROM sample_slots WHERE slot_index=151")[0]["status"], "PARTIAL")
        books = fixture.rows("SELECT status,best_bid_text FROM book_observations ORDER BY outcome")
        self.assertEqual([(row["status"], row["best_bid_text"]) for row in books], [("INVALID", None), ("VALID", "0.60")])

    def test_late_original_bytes_survive_expiry_and_new_condition(self):
        fixture = self.fixture(offset_us=597_000_000)
        raw = b"old condition response across expiry"
        class LateBody(httpx.SyncByteStream):
            def __iter__(self):
                yield raw
                fixture.advance(2.5)
        def late(request, payload, f):
            if request.url.path == "/books" and f.book_count == 1:
                return httpx.Response(200, stream=LateBody())
        fixture.hook = late
        self.run_recorder(fixture)
        old = fixture.rows("SELECT status FROM sample_slots WHERE market_id='1001' AND slot_index=449")[0]
        self.assertEqual(old["status"], "FAILED")
        self.assertEqual({row["status"] for row in fixture.rows("SELECT status FROM book_observations WHERE expected_condition_id=?", ("0x"+"ab"*32,))}, {"LATE"})
        attempt = fixture.rows("SELECT * FROM request_attempts WHERE operation='BOOK_BATCH' ORDER BY request_id")[0]
        self.assertEqual(decode_evidence(attempt["response_raw_z"], attempt["raw_size_bytes"], attempt["codec_version"]), raw)
        self.assertEqual(fixture.rows("SELECT status FROM sample_slots WHERE market_id='1002' AND slot_index=1")[0]["status"], "COMPLETE")

    def test_discovery_gap_is_durable_and_retry_pauses_two_seconds(self):
        fixture = self.fixture()
        failures = []
        def gap(request, payload, f):
            if request.url.path.startswith("/events/slug/") and not failures:
                failures.append(f.monotonic_ns())
                f.advance(10.2)
                return httpx.Response(503, stream=httpx.ByteStream(b"outage"))
        fixture.hook = gap
        self.run_recorder(fixture, pairs=1)
        events = fixture.rows("SELECT * FROM recorder_events WHERE kind='DISCOVERY_GAP'")
        self.assertEqual(len(events), 1)
        self.assertIn("transport_error", events[0]["detail_json"])
        discovery = [row for row in fixture.requests if row[1].startswith("/events/slug/")]
        self.assertGreaterEqual((discovery[1][2]-discovery[0][2])/1e9, 12.2)
        self.assertTrue(all(value <= 10 for value in discovery[0][3].values()))
        self.assertEqual(fixture.rows("SELECT slot_index FROM sample_slots WHERE status='COMPLETE'")[0]["slot_index"], 157)

    def test_ambiguous_discovery_never_starts_books(self):
        fixture = self.fixture()
        def ambiguity(request, payload, f):
            if request.url.path.startswith("/events/slug/"):
                payload["markets"].append(dict(payload["markets"][0], id="1999"))
            elif request.url.path == "/events":
                payload[0]["markets"].append(dict(payload[0]["markets"][0], id="1999"))
        fixture.hook = ambiguity
        result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+5_000_000)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(fixture.book_count, 0)
        self.assertTrue(all("ambiguous_market" in row["detail_json"] for row in fixture.rows("SELECT detail_json FROM recorder_events WHERE kind='DISCOVERY_GAP'")))

    def test_clock_refresh_and_metadata_revision_used_by_later_slot(self):
        fixture = self.fixture()
        def changed(request, payload, f):
            if request.url.path == "/markets/1001":
                payload["description"] = "rules observed after startup"
        fixture.hook = changed
        self.run_recorder(fixture, pairs=34)
        anchors = fixture.rows("SELECT * FROM clock_anchors")
        self.assertEqual(len(anchors), 2)
        self.assertGreaterEqual(anchors[1]["request_start_mono_ns"]-anchors[0]["anchor_mono_ns"], 60_000_000_000)
        revisions = fixture.rows("SELECT * FROM market_revisions ORDER BY revision_id")
        self.assertEqual(len(revisions), 2)
        last = fixture.rows("SELECT metadata_revision_id FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_index DESC LIMIT 1")[0]
        self.assertEqual(last["metadata_revision_id"], revisions[-1]["revision_id"])

    def test_failed_metadata_refresh_throttles_and_retains_age(self):
        fixture = self.fixture()
        def failed(request, payload, f):
            if request.url.path == "/markets/1001":
                return httpx.Response(503, stream=httpx.ByteStream(b"metadata unavailable"))
        fixture.hook = failed
        self.run_recorder(fixture, pairs=36)
        refresh = [row for row in fixture.requests if row[1] == "/markets/1001"]
        self.assertEqual(len(refresh), 2)  # One logical refresh, one bounded retry.
        self.assertEqual(len(fixture.rows("SELECT * FROM market_revisions")), 1)
        events = fixture.rows("SELECT detail_json FROM recorder_events WHERE kind='DISCOVERY_GAP'")
        self.assertTrue(any("metadata_refresh_failed" in row["detail_json"] and "age_us" in row["detail_json"] for row in events))

    def test_not_ready_metadata_pauses_capture(self):
        fixture = self.fixture()
        def not_ready(request, payload, f):
            if request.url.path == "/markets/1001":
                payload["enableOrderBook"] = False
        fixture.hook = not_ready
        self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+70_000_000)
        self.assertLess(fixture.book_count, 34)
        slots = fixture.rows("SELECT reason FROM sample_slots WHERE scheduled_us>=?", (NOW_US+64_000_000,))
        self.assertTrue(slots)
        self.assertTrue(all(row["reason"] == "METADATA_NOT_READY" for row in slots))

    def test_expired_anchor_stops_books_until_calibration_succeeds(self):
        fixture = self.fixture()
        def failed_clock(request, payload, f):
            if request.url.path == "/time" and f.elapsed_ns > 0:
                return httpx.Response(503, stream=httpx.ByteStream(b"time unavailable"))
        fixture.hook = failed_clock
        self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+130_000_000)
        books = [row for row in fixture.requests if row[1] == "/books"]
        self.assertTrue(books)
        self.assertTrue(all(row[2]-fixture.epoch_ns <= 120_005_000_000 for row in books))
        self.assertTrue(fixture.rows("SELECT * FROM recorder_events WHERE kind='CLOCK_PAUSE'"))

    def test_discontinuity_requires_pause_fresh_clock_and_market_validation(self):
        fixture = self.fixture()
        calls = []
        def shift(request, payload, f):
            if request.url.path == "/time":
                calls.append(f.monotonic_ns())
                if len(calls) >= 2:
                    return httpx.Response(200, stream=httpx.ByteStream(str(payload+20).encode()))
        fixture.hook = shift
        self.run_recorder(fixture, pairs=34)
        self.assertEqual([row["status"] for row in fixture.rows("SELECT status FROM clock_anchors")], ["VALID", "DISCONTINUITY", "VALID"])
        self.assertTrue(fixture.rows("SELECT * FROM recorder_events WHERE kind='CLOCK_PAUSE'"))
        self.assertEqual(len([row for row in fixture.requests if row[1].startswith("/events/slug/")]), 2)
        complete = fixture.rows("SELECT slot_index FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_index")
        self.assertEqual(len({row["slot_index"] for row in complete}), 34)
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE reason='CLOCK_PAUSE'"))

    def reopen(self, fixture, *, advance_us, epoch_ns=1007):
        fixture.store.close()
        fixture.initial_wall_us = fixture.wall_us() + advance_us
        fixture.elapsed_ns = 0
        fixture.epoch_ns = epoch_ns
        fixture.store = RecorderStore.open(fixture.db.path)
        fixture.book_count = 0
        fixture.requests.clear()
        fixture.hook = None
        fixture.sleep_hook = None

    def test_restart_preserves_original_quality(self):
        fixture = self.fixture()
        self.run_recorder(fixture)
        slots = fixture.rows("SELECT * FROM sample_slots WHERE status='COMPLETE'")
        books = fixture.rows("SELECT * FROM book_observations ORDER BY slot_id,outcome")
        references = fixture.rows("SELECT * FROM reference_associations")
        old_run = fixture.rows("SELECT * FROM recording_runs")[0]
        self.reopen(fixture, advance_us=20_000_000)
        result = self.run_recorder(fixture, pairs=1)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(fixture.rows("SELECT * FROM sample_slots WHERE run_id=? AND status='COMPLETE'", (old_run["run_id"],)), slots)
        self.assertEqual(fixture.rows("SELECT * FROM book_observations WHERE slot_id IN (?,?) ORDER BY slot_id,outcome", tuple(row["slot_id"] for row in slots)), books)
        self.assertEqual(fixture.rows("SELECT * FROM reference_associations WHERE slot_id IN (?,?)", tuple(row["slot_id"] for row in slots)), references)
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE reason='DOWNTIME'"))
        new_anchor = fixture.rows("SELECT * FROM clock_anchors WHERE run_id=?", (result.run_id,))[0]
        self.assertEqual(new_anchor["request_start_mono_ns"], 1007)
        self.assertTrue(fixture.rows("SELECT * FROM recorder_events WHERE kind='RESTART'"))

    def test_ctrl_c_after_reservation_persists_interrupted_and_closes(self):
        fixture = self.fixture()
        reserve = fixture.store.reserve_slot
        def interrupted(*args, **kwargs):
            reserve(*args, **kwargs)
            raise KeyboardInterrupt
        fixture.store.reserve_slot = interrupted
        try:
            result = self.run_recorder(fixture, pairs=1)
        except KeyboardInterrupt:
            self.fail("Coordinator leaked Ctrl+C instead of persisting interruption and closing.")
        self.assertEqual((result.exit_code, result.state), (130, "INTERRUPTED"))
        self.assertEqual(fixture.book_count, 0)
        slots = fixture.rows("SELECT * FROM sample_slots WHERE status='INTERRUPTED'")
        self.assertEqual(len(slots), 1)
        self.assertEqual({row["status"] for row in fixture.rows("SELECT * FROM book_observations")}, {"INTERRUPTED"})
        self.assertEqual(fixture.store._lock_file, None)

    def test_write_failure_stops_capture(self):
        fixture = self.fixture()
        fixture.store._connection.execute("""CREATE TRIGGER reject_book BEFORE INSERT ON book_observations
            BEGIN SELECT RAISE(FAIL, 'forced write failure'); END""")
        result = self.run_recorder(fixture, pairs=2)
        self.assertEqual((result.state, result.exit_code, result.slots_finalized), ("FAILED", 1, 0))
        self.assertEqual(result.reason, "storage_failure")
        self.assertEqual(fixture.book_count, 1)
        self.assertEqual(fixture.rows("SELECT * FROM book_observations"), [])
        self.assertEqual(fixture.rows("SELECT * FROM request_attempts WHERE operation='BOOK_BATCH'"), [])
        self.assertEqual(fixture.rows("SELECT status FROM sample_slots WHERE slot_index=151")[0]["status"], "IN_PROGRESS")
        self.assertEqual(fixture.store._lock_file, None)

    def test_restart_recovers_reserved_slot_before_new_run(self):
        fixture = self.fixture()
        reserve = fixture.store.reserve_slot
        def abrupt(*args, **kwargs):
            reserve(*args, **kwargs)
            raise RuntimeError("abrupt simulated process loss")
        fixture.store.reserve_slot = abrupt
        with self.assertRaisesRegex(RuntimeError, "abrupt simulated process loss"):
            self.run_recorder(fixture)
        self.assertEqual(fixture.public.max_attempts, 3)
        self.assertIsNone(fixture.public.on_attempt)
        self.assertIsNone(fixture.public.before_attempt)
        self.assertEqual(fixture.store._lock_file, None)
        self.reopen(fixture, advance_us=5_000_000)
        def inspect(request, payload, f):
            if request.url.path == "/time":
                self.assertEqual(f.store._connection.execute("SELECT status FROM sample_slots WHERE slot_index=151").fetchone()[0], "INTERRUPTED")
        fixture.hook = inspect
        result = self.run_recorder(fixture, pairs=1)
        self.assertEqual(result.exit_code, 0)
        slot = fixture.rows("SELECT * FROM sample_slots WHERE slot_index=151")[0]
        self.assertIsNone(slot["finished_server_us"])
        self.assertIsNone(slot["end_mono_ns"])
        old_run = fixture.rows("SELECT * FROM recording_runs WHERE run_id=?", (slot["run_id"],))[0]
        self.assertEqual(old_run["state"], "INTERRUPTED")

    def test_known_expired_downtime_and_unknown_windows_are_disclosed(self):
        fixture = self.fixture()
        self.run_recorder(fixture, pairs=1)
        self.reopen(fixture, advance_us=1_800_000_000)
        self.run_recorder(fixture, pairs=1)
        known = fixture.rows("SELECT * FROM sample_slots WHERE market_id='1001'")
        self.assertEqual(len(known), 450)
        self.assertEqual(known[-1]["reason"], "DOWNTIME")
        self.assertEqual(fixture.rows("SELECT lifecycle FROM markets WHERE market_id='1001'")[0]["lifecycle"], "EXPIRED")
        self.assertEqual({row["market_id"] for row in fixture.rows("SELECT market_id FROM markets")}, {"1001", "1003"})
        gaps = fixture.rows("SELECT detail_json FROM recorder_events WHERE kind='DISCOVERY_GAP'")
        self.assertTrue(any(json.loads(row["detail_json"]).get("unknown_windows") for row in gaps))

    def test_exhausted_housekeeping_allowance_preserves_due_queue_without_http(self):
        from polymarket_bot.recorder.service import Recorder
        fixture = self.fixture(offset_us=597_000_000)
        self.run_recorder(fixture)
        original_due = fixture.rows("SELECT resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")
        self.reopen(fixture, advance_us=31_000_000)
        original = Recorder._housekeeping
        stopped = []
        snapshots = []
        def spent(recorder, target_ns, **kwargs):
            fixture.advance(3)  # The armed future target is less than two seconds away.
            before_requests = len(fixture.requests)
            before = fixture.rows("SELECT resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")
            original(recorder, target_ns, **kwargs)
            after = fixture.rows("SELECT resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")
            snapshots.append((before_requests, len(fixture.requests), before, after))
            stopped.append(True)
        with patch.object(Recorder, "_housekeeping", spent):
            result = self.run_recorder(fixture, should_stop=lambda: bool(stopped))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0][0], snapshots[0][1])
        self.assertEqual(snapshots[0][2], snapshots[0][3])
        self.assertEqual(snapshots[0][3], original_due)
        self.assertEqual(fixture.book_count, 0)

    def test_fatal_invariant_stops_before_further_http(self):
        fixture = self.fixture()
        def invalid(*args, **kwargs):
            raise RecorderStoreError("fatal_invariant", "injected writer invariant")
        fixture.store.finalize_slot = invalid
        result = self.run_recorder(fixture)
        self.assertEqual((result.exit_code, result.reason), (1, "fatal_invariant"))
        self.assertEqual(fixture.book_count, 1)

    def test_reference_worker_never_blocks_book_owner_or_writes_sqlite(self):
        import threading
        from polymarket_bot.recorder import service
        self.assertTrue(hasattr(service, 'ReferenceWorker'))
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(2)
            return 'evidence'
        worker = service.ReferenceWorker()
        self.addCleanup(release.set)
        self.assertTrue(worker.submit(slow))
        self.assertTrue(entered.wait(1))
        self.assertIsNone(worker.take())
        self.assertFalse(worker.submit(slow))
        release.set()
        worker.stop(timeout=2)
        self.assertEqual(worker.take(), 'evidence')
        self.assertFalse(worker.submit(slow))

    def test_offline_reference_capture_keeps_books_and_unverified_evidence(self):
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder import reference
        fixture = self.fixture()
        self.assertTrue(hasattr(reference, 'OfflineReferenceSession'))
        session = reference.OfflineReferenceSession(price_scale=8,
            decoder_revision='structural-v2', discovery_payload={
                'feedId': reference.CHAINLINK_FEED_ID, 'name': 'BTC/USD-Streams-TWAP-60s-mainnet-production',
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=reference.ActivationEvidence(True, True, True, True, True, True),
            latest=lambda deadline: ({'feedId': reference.CHAINLINK_FEED_ID,
                'fullReport': str(fixture.wall_us()//1_000_000).encode().hex(), 'validFromTimestamp': fixture.wall_us()//1_000_000-60,
                'observationsTimestamp': fixture.wall_us()//1_000_000,
                'expiresAt': fixture.wall_us()//1_000_000+60, 'benchmarkPrice': '6000000000000'}),
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us)
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=2)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.slots_finalized, 2)
        self.assertEqual(fixture.rows("SELECT DISTINCT verification_status FROM reference_reports"),
                         [{'verification_status': 'UNVERIFIED'}])
        rows = fixture.rows("SELECT status,available_live FROM reference_associations WHERE role='SLOT_CURRENT'")
        self.assertTrue(rows)
        self.assertTrue(all(r['status']=='UNAVAILABLE' and r['available_live']==0 for r in rows))
        self.assertEqual(fixture.rows('PRAGMA user_version')[0]['user_version'], 1)
        feed = fixture.rows('SELECT price_scale,decoder_revision,verification_evidence_json FROM reference_feeds')[0]
        self.assertEqual(feed['price_scale'], 8)
        self.assertEqual(feed['decoder_revision'], 'structural-v2')
        self.assertIn('SYNTHETIC_OFFLINE_ONLY', feed['verification_evidence_json'])

    def test_slow_offline_fallback_does_not_drop_valid_book_capture(self):
        import threading
        from polymarket_bot.recorder.reference import OfflineReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        from polymarket_bot.recorder.service import Recorder
        fixture = self.fixture()
        entered, release = threading.Event(), threading.Event()
        def latest(deadline):
            entered.set()
            release.wait(2)
            return None
        session = OfflineReferenceSession(price_scale=8, decoder_revision='structural',
            discovery_payload={'feedId': CHAINLINK_FEED_ID, 'name': 'BTC/USD-Streams-TWAP-60s-mainnet-production',
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=ActivationEvidence(True, True, True, True, True, True), latest=latest,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us)
        def books(request, payload, f):
            if request.url.path == '/books':
                self.assertTrue(entered.wait(1))
                self.assertFalse(release.is_set())
                release.set()
        fixture.hook = books
        self.addCleanup(release.set)
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=1)
        self.assertEqual(result.slots_finalized, 1)
        self.assertEqual(fixture.rows("SELECT status FROM sample_slots WHERE slot_index=151")[0]['status'], 'COMPLETE')

    def test_offline_worker_joined_when_book_writer_fails(self):
        import threading
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.reference import OfflineReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        fixture = self.fixture()
        session = OfflineReferenceSession(price_scale=8, decoder_revision='structural',
            discovery_payload={'feedId': CHAINLINK_FEED_ID, 'name': 'BTC/USD-Streams-TWAP-60s-mainnet-production',
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=ActivationEvidence(True, True, True, True, True, True), latest=lambda deadline: None,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us)
        fixture.store.finalize_slot = lambda *a, **k: (_ for _ in ()).throw(RecorderStoreError('storage_failure', 'injected'))
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=1)
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(recorder.reference_worker._stopped)
        self.assertFalse(recorder.reference_worker._thread and recorder.reference_worker._thread.is_alive())

    def test_malformed_offline_reference_and_missing_policy_preserve_books(self):
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.reference import OfflineReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        fixture = self.fixture()
        session = OfflineReferenceSession(price_scale=8, decoder_revision='structural',
            discovery_payload={'feedId': CHAINLINK_FEED_ID, 'name': 'BTC/USD-Streams-TWAP-60s-mainnet-production',
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=ActivationEvidence(True, True, True, True, True, True),
            latest=lambda deadline: {'feedId': CHAINLINK_FEED_ID, 'fullReport': '616263',
                'validFromTimestamp': 1, 'observationsTimestamp': True, 'expiresAt': 3,
                'benchmarkPrice': 'NaN'}, boundary=lambda target, deadline: None,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us)
        with patch('os.getenv', side_effect=AssertionError('credential lookup')), \
             patch('hmac.new', side_effect=AssertionError('signing')), \
             patch('threading.Thread', wraps=__import__('threading').Thread):
            recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
                wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
            result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=2)
        self.assertEqual(result.slots_finalized, 2)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(fixture.rows('SELECT DISTINCT decode_status FROM reference_reports'), [{'decode_status': 'INVALID'}])
        self.assertTrue(fixture.rows("SELECT * FROM reference_associations WHERE status='AMBIGUOUS'"))
        self.assertFalse(fixture.rows('SELECT * FROM reference_associations WHERE available_live=1'))

    def test_book_only_ignores_supplied_reference_session_without_threads(self):
        fixture = self.fixture()
        from polymarket_bot.recorder.service import Recorder
        with patch('threading.Thread', side_effect=AssertionError('reference thread')), \
             patch('os.getenv', side_effect=AssertionError('credential lookup')):
            recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
                wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=object())
            result = recorder.run(RunConfig(), should_stop=lambda: fixture.book_count>=1)
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(recorder.reference_worker)

    def test_offline_session_rechecks_mutated_gate_before_startup(self):
        from polymarket_bot.recorder.reference import OfflineReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        from polymarket_bot.recorder.service import Recorder
        from dataclasses import replace
        fixture = self.fixture()
        session = OfflineReferenceSession(price_scale=8, decoder_revision='structural',
            discovery_payload={'feedId': CHAINLINK_FEED_ID, 'name': 'BTC/USD-Streams-TWAP-60s-mainnet-production',
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=ActivationEvidence(True, True, True, True, True, True), latest=lambda deadline: None,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us)
        session.evidence = replace(session.evidence, boundary_verified=False)
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=1)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(fixture.requests, [])
        self.assertEqual(fixture.rows('SELECT * FROM recording_runs'), [])

    def test_offline_gap_provider_persists_once_without_schema_change(self):
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.reference import OfflineReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        fixture = self.fixture()
        session = OfflineReferenceSession(price_scale=8, decoder_revision='structural',
            discovery_payload={'feedId': CHAINLINK_FEED_ID, 'name': 'BTC/USD-Streams-TWAP-60s-mainnet-production',
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=ActivationEvidence(True, True, True, True, True, True), latest=lambda deadline: None,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us)
        session.gaps = lambda: [(fixture.epoch_ns/1e9, fixture.epoch_ns/1e9+0.1)]
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: fixture.book_count>=3)
        gaps = [r for r in fixture.rows('SELECT detail_json FROM recorder_events') if 'REFERENCE_CONNECTION_GAP' in r['detail_json']]
        self.assertEqual(len(gaps), 1)

    def test_reference_required_refused_before_startup(self):
        fixture = self.fixture()
        result = self.run_recorder(fixture, config=RunConfig(mode="REFERENCE_REQUIRED"))
        self.assertEqual((result.exit_code, result.reason), (2, "reference_runtime_unavailable"))
        self.assertEqual(fixture.requests, [])
        self.assertEqual(fixture.rows("SELECT * FROM recording_runs"), [])

    def test_due_resolution_runs_in_same_writer_before_future_capture(self):
        fixture = self.fixture(offset_us=597_000_000)
        result = self.run_recorder(fixture)
        self.assertEqual(result.exit_code, 0)
        attempts = fixture.rows("SELECT * FROM request_attempts WHERE operation='RESOLUTION'")
        self.assertEqual(len(attempts), 1)
        self.assertEqual((attempts[0]["method"], attempts[0]["path"]), ("GET", "/v2/resolutions"))
        observation = fixture.rows("SELECT * FROM resolution_observations")[0]
        self.assertEqual(observation["classification"], "ABSENT")
        old = fixture.rows("SELECT * FROM markets WHERE market_id='1001'")[0]
        self.assertEqual((old["resolution_state"], old["resolution_attempts"]), ("RETRY_WAIT", 1))
        self.assertIsNotNone(observation["learned_server_us"])
        self.assertEqual(observation["learned_wall_us"], attempts[0]["ended_wall_us"])
        anchor = fixture.rows("SELECT * FROM clock_anchors ORDER BY anchor_id DESC")[0]
        self.assertEqual(observation["learned_server_us"], anchor["anchor_server_us"]+
            (attempts[0]["end_mono_ns"]-anchor["anchor_mono_ns"])//1000)
        self.assertEqual(old["resolution_next_due_us"], observation["learned_server_us"]+30_000_000)
        self.assertEqual(len(fixture.rows("SELECT * FROM recording_runs")), 1)

    def test_resolution_overrun_skips_target_and_does_not_redispatch_due_job(self):
        fixture = self.fixture(offset_us=597_000_000)
        def slow(request, payload, f):
            if request.url.path == "/v2/resolutions":
                f.advance(3.2)
                return httpx.Response(503, stream=httpx.ByteStream(b"late resolution"))
        fixture.hook = slow
        self.run_recorder(fixture, pairs=3)
        calls = [row for row in fixture.requests if row[1] == "/v2/resolutions"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(0 < value < 2 for value in calls[0][3].values()))
        self.assertEqual(fixture.rows("SELECT reason FROM sample_slots WHERE market_id='1002' AND slot_index=1")[0]["reason"], "PROCESSING_OVERRUN")
        self.assertEqual([row["slot_index"] for row in fixture.rows("SELECT slot_index FROM sample_slots WHERE market_id='1002' AND status='COMPLETE' ORDER BY slot_index")], [2, 3])
        self.assertEqual(fixture.rows("SELECT resolution_attempts FROM markets WHERE market_id='1001'")[0]["resolution_attempts"], 1)

    def test_resolution_backward_wall_step_does_not_stop_book_capture(self):
        fixture = self.fixture(offset_us=597_000_000)
        def wall_step(request, payload, f):
            if request.url.path == "/v2/resolutions":
                f.initial_wall_us -= 1_000_000
        fixture.hook = wall_step
        result = self.run_recorder(fixture, pairs=3)
        self.assertEqual((result.exit_code,result.slots_finalized),(0,3))
        request = fixture.rows("SELECT * FROM request_attempts WHERE operation='RESOLUTION'")[0]
        self.assertLess(request["ended_wall_us"],request["started_wall_us"])
        self.assertGreater(request["latency_ns"],0)
        evidence = fixture.rows("SELECT provenance_json FROM resolution_observations")[0]
        self.assertTrue(json.loads(evidence["provenance_json"])["wall_clock_movement"]["request_backward"])
        self.assertEqual(fixture.rows("SELECT COUNT(*) n FROM sample_slots WHERE status='COMPLETE'")[0]["n"],3)

    def test_invalid_numeric_metadata_pauses_until_valid_revision(self):
        fixture = self.fixture()
        calls = []
        def invalid_first(request, payload, f):
            if request.url.path.startswith("/clob-markets/"):
                calls.append(True)
                payload["mts"] = "NaN" if len(calls)==1 else "1e-2"
        fixture.hook = invalid_first
        result = self.run_recorder(fixture,pairs=1)
        self.assertEqual(result.exit_code,0)
        first_book = next(row for row in fixture.requests if row[1]=="/books")
        self.assertGreaterEqual(first_book[2]-fixture.epoch_ns,60_000_000_000)
        revisions = fixture.rows("SELECT tick_size_text FROM market_revisions ORDER BY revision_id")
        self.assertEqual([row["tick_size_text"] for row in revisions],[None,"0.01"])
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE reason='METADATA_NOT_READY'"))

    def test_metadata_overrun_skips_passed_slots_and_clamps_http(self):
        fixture = self.fixture()
        def slow(request, payload, f):
            if request.url.path == "/markets/1001":
                f.advance(5)
        fixture.hook = slow
        self.run_recorder(fixture, pairs=34)
        calls = [row for row in fixture.requests if row[1] == "/markets/1001"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(0 < value < 2 for value in calls[0][3].values()))
        skipped = fixture.rows("SELECT slot_index,reason FROM sample_slots WHERE slot_index IN (182,183)")
        self.assertEqual([(row["slot_index"], row["reason"]) for row in skipped], [(182, "PROCESSING_OVERRUN"), (183, "PROCESSING_OVERRUN")])

    def test_identity_conflict_pauses_and_retains_expiry_resolution_queue(self):
        fixture = self.fixture()
        def conflict(request, payload, f):
            if request.url.path == "/markets/1001":
                payload["conditionId"] = "0x"+"ff"*32
        fixture.hook = conflict
        result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+605_000_000)
        self.assertEqual(result.exit_code, 0)
        old = fixture.rows("SELECT * FROM markets WHERE market_id='1001'")[0]
        self.assertEqual(old["lifecycle"], "INVALIDATED")
        self.assertEqual(old["resolution_attempts"], 1)
        conflicts = fixture.rows("SELECT * FROM recorder_events WHERE kind='IDENTITY_CONFLICT'")
        self.assertTrue(conflicts)
        self.assertTrue(all(row["started_us"] >= NOW_US+60_000_000 for row in conflicts))
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE market_id='1002' AND status='COMPLETE'"))
        old_books = fixture.rows("SELECT s.scheduled_us FROM sample_slots s JOIN book_observations b USING(slot_id) WHERE s.market_id='1001'")
        self.assertTrue(all(row["scheduled_us"] < NOW_US+64_000_000 for row in old_books))

    def test_book_only_never_looks_up_credentials_or_uses_external_attempt_callback(self):
        fixture = self.fixture()
        def forbidden(attempt):
            self.fail("Caller callback must not run inside coordinator HTTP operations.")
        fixture.public.on_attempt = forbidden
        with patch("os.getenv", side_effect=AssertionError("credential environment access")), \
             patch("pathlib.Path.read_text", side_effect=AssertionError("credential file access")):
            result = self.run_recorder(fixture, pairs=1)
        self.assertEqual(result.exit_code, 0)
        self.assertIs(fixture.public.on_attempt, forbidden)
        self.assertEqual(fixture.public.max_attempts, 3)
        self.assertTrue(all(row[1] in ("/books", "/time") or row[1].startswith(("/events/slug/", "/clob-markets/")) for row in fixture.requests))

    def test_stop_after_completed_http_starts_no_fallback_or_retry(self):
        fixture = self.fixture()
        stopping = []
        def stopped(request, payload, f):
            if request.url.path == "/books":
                stopping.append(True)
                return httpx.Response(404, stream=httpx.ByteStream(b"route missing"))
        fixture.hook = stopped
        self.run_recorder(fixture, should_stop=lambda: bool(stopping))
        self.assertEqual(len([row for row in fixture.requests if row[1] in ("/books", "/book")]), 1)
        self.assertEqual(fixture.rows("SELECT status FROM sample_slots WHERE slot_index=151")[0]["status"], "INTERRUPTED")

    def test_ctrl_c_during_http_stops_and_restores_transport_state(self):
        fixture = self.fixture()
        def interrupted(request, payload, f):
            if request.url.path == "/books":
                raise KeyboardInterrupt
        fixture.hook = interrupted
        result = self.run_recorder(fixture)
        self.assertEqual((result.exit_code, result.slots_finalized), (130, 0))
        self.assertEqual(fixture.book_count, 1)
        self.assertEqual(fixture.public.max_attempts, 3)
        self.assertIsNone(fixture.public.on_attempt)
        self.assertEqual(fixture.store._lock_file, None)

    def test_stop_after_transient_failure_starts_no_retry(self):
        fixture = self.fixture()
        stopping = []
        def stopped(request, payload, f):
            if request.url.path == "/books":
                stopping.append(True)
                return httpx.Response(503, stream=httpx.ByteStream(b"busy"))
        fixture.hook = stopped
        result = self.run_recorder(fixture, should_stop=lambda: bool(stopping))
        self.assertEqual((result.exit_code, result.state), (0, "STOPPED"))
        self.assertEqual(fixture.book_count, 1)
        self.assertEqual(fixture.rows("SELECT status FROM sample_slots WHERE slot_index=151")[0]["status"], "INTERRUPTED")

    def test_anchor_expiring_during_armed_wait_pauses_without_request(self):
        fixture = self.fixture(offset_us=1_000_000)
        def failed_clock(request, payload, f):
            if request.url.path == "/time" and f.elapsed_ns:
                return httpx.Response(503, stream=httpx.ByteStream(b"time unavailable"))
        fixture.hook = failed_clock
        result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+130_000_000)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(fixture.rows("SELECT * FROM recorder_events WHERE kind='CLOCK_PAUSE'"))
        self.assertTrue(all(row[2]-fixture.epoch_ns <= 120_005_000_000 for row in fixture.requests if row[1] == "/books"))

    def test_discontinuity_across_expiry_materializes_old_coverage_and_queue(self):
        fixture = self.fixture(offset_us=536_000_000)
        calls = []
        def shifted(request, payload, f):
            if request.url.path == "/time":
                calls.append(True)
                if len(calls) == 2:
                    f.server_shift_us = 20_000_000
                    return httpx.Response(200, stream=httpx.ByteStream(str(payload+20).encode()))
        fixture.hook = shifted
        self.run_recorder(fixture, pairs=34)
        old = fixture.rows("SELECT * FROM markets WHERE market_id='1001'")[0]
        self.assertEqual(old["lifecycle"], "EXPIRED")
        self.assertEqual(len(fixture.rows("SELECT * FROM sample_slots WHERE market_id='1001'")), 450)
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE market_id='1001' AND reason='CLOCK_PAUSE'"))
        self.assertIsNotNone(old["resolution_next_due_us"])

    def test_restart_of_invalidated_current_market_keeps_pause_until_rollover(self):
        fixture = self.fixture()
        def conflict(request, payload, f):
            if request.url.path == "/markets/1001":
                payload["conditionId"] = "0x"+"ff"*32
        fixture.hook = conflict
        self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+65_000_000)
        self.reopen(fixture, advance_us=530_000_000)
        result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+605_000_000)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE market_id='1002' AND status='COMPLETE'"))
        self.assertEqual(fixture.rows("SELECT lifecycle FROM markets WHERE market_id='1001'")[0]["lifecycle"], "INVALIDATED")
        self.assertTrue(all(b"9000000000000000000001" not in row[4] for row in fixture.requests if row[1] == "/books"))

    def test_before_rollover_clock_calibration_uses_remaining_allowance(self):
        fixture = self.fixture(offset_us=597_000_000)
        self.run_recorder(fixture)
        anchors = fixture.rows("SELECT * FROM clock_anchors ORDER BY anchor_id")
        self.assertEqual(len(anchors), 2)
        time_calls = [row for row in fixture.requests if row[1] == "/time"]
        self.assertTrue(all(0 < value < 2 for value in time_calls[1][3].values()))

    def test_close_failure_returns_nonzero_after_capture_stops(self):
        fixture = self.fixture()
        close = fixture.store.close
        calls = []
        def failure():
            close()
            if not calls:
                calls.append(True)
                raise sqlite3.OperationalError("close failed")
        fixture.store.close = failure
        result = self.run_recorder(fixture, pairs=1)
        self.assertEqual((result.state, result.exit_code, result.reason), ("FAILED", 1, "storage_failure"))
        self.assertEqual(fixture.book_count, 1)

    def test_armed_wake_lateness_and_expired_intervals_do_not_burst(self):
        fixture = self.fixture()
        wakes = []
        def delayed(f):
            if not wakes:
                f.advance(0.4)
            elif len(wakes) == 1:
                f.advance(4.1)
            wakes.append(True)
        fixture.sleep_hook = delayed
        self.run_recorder(fixture)
        captured = fixture.rows("SELECT slot_index,delay_us FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_index")
        self.assertEqual([(row["slot_index"], row["delay_us"]) for row in captured], [(151, 400_000), (155, 0)])
        missed = fixture.rows("SELECT status FROM sample_slots WHERE slot_index IN (152,153,154)")
        self.assertEqual([row["status"] for row in missed], ["MISSED"]*3)

    def test_restart_without_known_identity_discloses_unknown_downtime(self):
        fixture = self.fixture()
        def unavailable(request, payload, f):
            if request.url.path.startswith("/events/slug/"):
                return httpx.Response(503, stream=httpx.ByteStream(b"unknown market"))
        fixture.hook = unavailable
        self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+3_000_000)
        self.assertEqual(fixture.rows("SELECT * FROM markets"), [])
        self.reopen(fixture, advance_us=1_800_000_000)
        self.run_recorder(fixture, pairs=1)
        gaps = fixture.rows("SELECT * FROM recorder_events WHERE kind='DISCOVERY_GAP'")
        self.assertTrue(any(json.loads(row["detail_json"]).get("unknown_windows") for row in gaps))

    def test_discovery_gap_after_forward_clock_shift_uses_server_domain(self):
        fixture = self.fixture()
        calls = []
        def shifted_outage(request, payload, f):
            if request.url.path == "/time":
                calls.append(True)
                if len(calls) == 2:
                    f.server_shift_us = 20_000_000
                    return httpx.Response(200, stream=httpx.ByteStream(str(payload+20).encode()))
            elif request.url.path.startswith("/events/slug/") and len(calls) >= 2:
                return httpx.Response(503, stream=httpx.ByteStream(b"discovery outage after shift"))
        fixture.hook = shifted_outage
        result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= NOW_US+70_000_000)
        self.assertEqual(result.exit_code, 0)
        self.assertGreaterEqual(len(calls), 3, "Discontinuity requires prompt fresh calibration.")
        gaps = fixture.rows("SELECT * FROM recorder_events WHERE kind='DISCOVERY_GAP'")
        self.assertTrue(gaps)
        self.assertTrue(any(row["ended_us"] is None for row in gaps),
                        "Without validated reacquisition the durable acquisition gap remains open.")
        self.assertTrue(all(row["ended_us"] is None or row["ended_us"] >= row["started_us"] for row in gaps))
        self.assertTrue(all(row["ended_us"] is None or row["ended_us"] >= NOW_US+80_000_000 for row in gaps))

    def test_resolution_server_due_and_receipt_backoff_survive_offset_restart(self):
        fixture = self.fixture(offset_us=597_000_000)
        self.run_recorder(fixture, pairs=1)
        self.reopen(fixture, advance_us=0)
        fixture.server_shift_us = 300_000_000
        result = self.run_recorder(fixture, pairs=1)
        self.assertEqual(result.exit_code, 0)
        original = fixture.rows("SELECT * FROM resolution_observations")
        self.assertEqual(len(original), 1, "Expired server-domain work is already due despite local wall lag.")
        observation = original[0]
        attempt = fixture.rows("SELECT * FROM request_attempts WHERE operation='RESOLUTION'")[0]
        self.assertEqual(observation["learned_wall_us"], attempt["ended_wall_us"])
        self.assertGreater(observation["learned_server_us"]-observation["learned_wall_us"], 299_000_000)
        queue = fixture.rows("SELECT resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")[0]
        self.assertEqual(queue, {"resolution_next_due_us": observation["learned_server_us"]+30_000_000,
                                 "resolution_attempts": 1})
        self.reopen(fixture, advance_us=0)
        self.run_recorder(fixture, pairs=1)
        self.assertEqual(fixture.rows("SELECT resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")[0], queue)
        self.assertFalse([row for row in fixture.requests if row[1] == "/v2/resolutions"])
        self.reopen(fixture, advance_us=31_000_000)
        def failed_resolution(request, payload, f):
            if request.url.path == "/v2/resolutions":
                return httpx.Response(503, stream=httpx.ByteStream(b"resolution unavailable"))
        fixture.hook = failed_resolution
        self.assertEqual(self.run_recorder(fixture, pairs=1).exit_code, 0)
        observations = fixture.rows("SELECT * FROM resolution_observations ORDER BY resolution_id")
        self.assertEqual(observations[:1], original)
        self.assertEqual(observations[1]["classification"], "INVALID")
        self.assertGreater(observations[1]["learned_server_us"], queue["resolution_next_due_us"])
        retried = fixture.rows("SELECT resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")[0]
        self.assertEqual(retried, {"resolution_next_due_us": observations[1]["learned_server_us"]+60_000_000,
                                   "resolution_attempts": 2})

    def test_expired_resolution_progresses_during_outage_and_ambiguity(self):
        for mode in ("outage", "ambiguity"):
            with self.subTest(mode=mode):
                fixture = self.fixture(offset_us=597_000_000)
                self.run_recorder(fixture, pairs=1)
                self.reopen(fixture, advance_us=10_000_000)
                started_wall = fixture.wall_us()
                def gap(request, payload, f):
                    if request.url.path.startswith("/events"):
                        if mode == "outage":
                            return httpx.Response(503, stream=httpx.ByteStream(b"Gamma unavailable"))
                        events = payload if type(payload) is list else [payload]
                        for event in events:
                            event["markets"].append(dict(event["markets"][0], id="1999"))
                fixture.hook = gap
                result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= started_wall+5_000_000)
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(fixture.book_count, 0)
                self.assertEqual(fixture.rows("SELECT * FROM market_revisions WHERE run_id=?", (result.run_id,)), [])
                observations = fixture.rows("SELECT classification FROM resolution_observations")
                self.assertEqual(observations, [{"classification": "ABSENT"}])
                requests = [row for row in fixture.requests if row[1] == "/v2/resolutions"]
                self.assertEqual(len(requests), 1)
                self.assertTrue(all(0 < value <= 2 for value in requests[0][3].values()))
                discovery = [row for row in fixture.requests if row[1].startswith("/events/slug/")]
                self.assertGreaterEqual((discovery[-1][2]-discovery[0][2])/1e9, 2)

    def test_first_identity_conflict_on_restart_preserves_pin_and_rolls_over(self):
        fixture = self.fixture()
        self.run_recorder(fixture, pairs=1)
        original = fixture.rows("SELECT up_token_id,down_token_id,condition_id FROM markets WHERE market_id='1001'")[0]
        self.reopen(fixture, advance_us=590_000_000)
        def conflict(request, payload, f):
            if f.market_row()["id"] != "1001":
                return
            if request.url.path.startswith("/events/slug/"):
                row = payload["markets"][0]
                row["clobTokenIds"] = json.dumps([str(int(token)+100) for token in json.loads(row["clobTokenIds"])])
            elif request.url.path.startswith("/clob-markets/"):
                for row in payload["t"]:
                    row["t"] = str(int(row["t"])+100)
        fixture.hook = conflict
        result = self.run_recorder(fixture, pairs=1)
        self.assertEqual((result.exit_code, result.state), (0, "STOPPED"))
        self.assertEqual(fixture.rows("SELECT up_token_id,down_token_id,condition_id FROM markets WHERE market_id='1001'")[0], original)
        self.assertEqual(fixture.rows("SELECT lifecycle FROM markets WHERE market_id='1001'")[0]["lifecycle"], "INVALIDATED")
        self.assertTrue(fixture.rows("SELECT * FROM recorder_events WHERE kind='IDENTITY_CONFLICT' AND run_id=?", (result.run_id,)))
        revision = fixture.rows("SELECT * FROM market_revisions WHERE market_id='1001' ORDER BY revision_id DESC")[0]
        raw = decode_evidence(revision["gamma_raw_z"], json.loads(revision["raw_sizes_json"])["gamma"], revision["codec_version"])
        self.assertIn(str(int(original["up_token_id"])+100).encode(), raw)
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE market_id='1002' AND status='COMPLETE'"))
        self.assertFalse([row for row in fixture.requests if row[1] == "/books" and original["up_token_id"].encode() in row[4]])
        self.assertTrue(fixture.rows("SELECT * FROM resolution_observations WHERE market_id='1001'"))

    def test_revalidation_gap_does_not_starve_unrelated_expired_queue(self):
        fixture = self.fixture(offset_us=597_000_000)
        self.run_recorder(fixture)
        self.reopen(fixture, advance_us=60_000_000)
        started_wall = fixture.wall_us()
        conflict_seen = []
        def gap_after_conflict(request, payload, f):
            if request.url.path == "/markets/1002":
                payload["conditionId"] = "0x"+"ff"*32
                conflict_seen.append(f.monotonic_ns())
            elif request.url.path.startswith("/events") and conflict_seen:
                return httpx.Response(503, stream=httpx.ByteStream(b"revalidation unavailable"))
        fixture.hook = gap_after_conflict
        result = self.run_recorder(fixture, should_stop=lambda: fixture.wall_us() >= started_wall+70_000_000)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(conflict_seen)
        self.assertTrue(fixture.rows("SELECT * FROM recorder_events WHERE kind='DISCOVERY_GAP' AND run_id=?", (result.run_id,)))
        old = fixture.rows("SELECT * FROM markets WHERE market_id='1001'")[0]
        self.assertEqual(old["resolution_attempts"], 3,
                         "Revalidation must spend remaining allowance on independent old due work.")
        self.assertFalse([row for row in fixture.requests if row[1] == "/books" and row[2] >= conflict_seen[0]])

    def test_metadata_overrun_past_anchor_expiry_pauses_and_recalibrates(self):
        fixture = self.fixture()
        def blocking_metadata(request, payload, f):
            if request.url.path == "/time" and 0 < f.elapsed_ns < 126_000_000_000:
                return httpx.Response(503, stream=httpx.ByteStream(b"clock unavailable"))
            if request.url.path == "/markets/1001" and f.elapsed_ns < 126_000_000_000:
                f.advance(65)
        fixture.hook = blocking_metadata
        result = self.run_recorder(fixture, should_stop=lambda: fixture.elapsed_ns >= 132_000_000_000)
        self.assertEqual((result.exit_code, result.state), (0, "STOPPED"))
        self.assertEqual(fixture.rows("SELECT state FROM recording_runs WHERE run_id=?", (result.run_id,))[0]["state"], "STOPPED")
        pauses = fixture.rows("SELECT * FROM recorder_events WHERE kind='CLOCK_PAUSE'")
        self.assertTrue(pauses)
        self.assertTrue(any(json.loads(row["detail_json"]).get("time_domain") == "LOCAL_WALL" for row in pauses))
        anchors = fixture.rows("SELECT * FROM clock_anchors ORDER BY anchor_id")
        self.assertGreaterEqual(len(anchors), 2)
        fresh_ns = anchors[-1]["request_end_mono_ns"]
        books = [row for row in fixture.requests if row[1] == "/books"]
        self.assertTrue(any(row[2] >= fresh_ns for row in books))
        self.assertFalse([row for row in books if 120_005_000_000 < row[2]-fixture.epoch_ns < fresh_ns-fixture.epoch_ns])
        self.assertTrue(fixture.rows("SELECT * FROM sample_slots WHERE reason='CLOCK_PAUSE'"))


if __name__ == "__main__":
    unittest.main()
