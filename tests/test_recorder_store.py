"""Atomic recorder writes and recovery against the store's real owned SQLite."""
from dataclasses import replace
from decimal import Decimal
import json
import unittest
from uuid import UUID

from polymarket_bot.recorder import models
from polymarket_bot.recorder.store import RecorderStore, RecorderStoreError, decode_evidence
from tests.recorder_support import (BookFixture, DatabaseFixture, NOW_US, WINDOW_START_US,
                                    down_book, paired_bundle, seed_writer_market)
from tests.support import CONDITION, MARKET_ID, TOKEN_DOWN, TOKEN_UP, make_book


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(models, "RunConfig"), "Typed RunConfig and slot writer are missing")
        self.fixture = DatabaseFixture()
        self.addCleanup(self.fixture.close)
        self.store = RecorderStore.open(self.fixture.path)
        self.addCleanup(self.store.close)
        self.run = self.store.start_run(models.RunConfig(), wall_us=NOW_US, app_revision="task5-test")
        seed_writer_market(self.store, self.run)

    def test_prepared_complete_event_json_boundary(self):
        from typing import Any, cast
        from polymarket_bot.market_data.models import MarketDataError
        run_id = cast(Any, self).run
        for event_type in ('PREPARED_ARM', 'PREPARED_ACTIVATION'):
            for delta in (-1, 0, 1):
                with self.subTest(event_type=event_type, delta=delta):
                    detail: dict[str, Any] = dict(type=event_type, identity=['2002', '1002'], boundary_us=900000000,
                        event=dict(description=''), list_evidence=[dict(sha256='a'*64, bytes=1689540, complete=True)])
                    size = len(json.dumps(detail, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())
                    detail['event']['description'] = 'x'*(1_048_576-size+delta)
                    count = len(self.rows('recorder_events'))
                    if delta > 0:
                        with self.assertRaises(MarketDataError) as caught:
                            self.store.record_event(run_id, 'DISCOVERY_GAP', started_us=NOW_US, detail=detail)
                        self.assertEqual(caught.exception.reason, 'EVIDENCE_BOUND_EXCEEDED')
                        self.assertEqual(len(self.rows('recorder_events')), count)
                        self.assertFalse(self.store._write_failed)
                    else:
                        event_id = self.store.record_event(run_id, 'DISCOVERY_GAP', started_us=NOW_US, detail=detail)
                        saved = self.store._one('SELECT length(CAST(detail_json AS BLOB)) AS bytes FROM recorder_events WHERE event_id=?', (event_id,))
                        assert saved is not None
                        self.assertEqual(saved['bytes'], 1_048_576+delta)

    def rows(self, table, where="", params=()):
        cursor = self.store._connection.execute(f"SELECT * FROM {table} {where}", params)
        keys = [column[0] for column in cursor.description]
        return [dict(zip(keys, row)) for row in cursor]

    def target(self, index=150):
        return models.SlotTarget(index, WINDOW_START_US + index * 2_000_000)

    def reserve(self, target=None):
        return self.store.reserve_slot(MARKET_ID, target or self.target(), run_id=self.run,
                                      anchor_id=1, metadata_revision_id=1,
                                      started_mono_ns=0, started_server_us=NOW_US)

    def commit(self, bundle=None, target=None, **changes):
        target = target or self.target()
        reference = models.ReferenceAssociationDraft(
            MARKET_ID, "SLOT_CURRENT", target.scheduled_us, "DISABLED", "NOT_FETCHED",
            False, NOW_US + 100_000, NOW_US + 100_000)
        value = models.SlotCommit(MARKET_ID, target, self.run, 1, 1,
                                  bundle or paired_bundle(), reference, 0, 100_000_000)
        return replace(value, **changes)

    def finalize(self, slot_id, commit):
        self.store.finalize_slot(slot_id, commit, committed_wall_us=NOW_US + 200_000)

    def test_run_policy_and_finish_are_durable(self):
        run = self.rows("recording_runs")[0]
        self.assertEqual(str(UUID(self.run)), self.run)
        self.assertEqual((run["mode"], run["reference_activation"], run["state"]),
                         ("BOOK_ONLY", "DISABLED", "RUNNING"))
        config = json.loads(run["config_json"])
        self.assertEqual((config["cadence_us"], config["slot_count"], config["max_attempts"],
                          config["max_response_bytes"], config["decimal_input_characters"],
                          config["decimal_coefficient_digits"], config["decimal_exponent_limit"],
                          config["arithmetic_precision"]),
                         (2_000_000, 450, 2, 10_485_760, 256, 128, 128, 512))
        self.assertIsNone(run["activation_evidence_json"])
        self.store.finish_run(self.run, state="STOPPED", wall_us=NOW_US + 1_000_000, reason="requested")
        finished = self.rows("recording_runs")[0]
        self.assertEqual((finished["mode"], finished["state"], finished["ended_wall_us"], finished["exit_reason"]),
                         ("BOOK_ONLY", "STOPPED", NOW_US + 1_000_000, "requested"))

    def test_pair_and_evidence_one_transaction(self):
        slot = self.reserve()
        self.assertFalse(self.store._connection.in_transaction)
        self.assertEqual(self.rows("book_observations"), [])
        self.assertEqual(self.rows("request_attempts"), [])
        self.assertEqual(self.rows("reference_associations"), [])
        self.assertEqual(self.rows("sample_slots")[0]["status"], "IN_PROGRESS")
        commit = self.commit()
        self.finalize(slot, commit)
        self.assertFalse(self.store._connection.in_transaction)
        saved = self.rows("sample_slots")[0]
        self.assertEqual((saved["status"], saved["reference_status"], saved["delay_us"],
                          saved["finished_server_us"]),
                         ("COMPLETE", "REFERENCE_INCOMPLETE", 0, NOW_US + 100_000))
        outcomes = {row["outcome"]: row for row in self.rows("book_observations")}
        self.assertEqual(set(outcomes), {"UP", "DOWN"})
        self.assertEqual((outcomes["UP"]["expected_token_id"], outcomes["DOWN"]["expected_token_id"]),
                         (TOKEN_UP, TOKEN_DOWN))
        self.assertEqual((outcomes["UP"]["source_us"], outcomes["DOWN"]["source_us"]),
                         (NOW_US, NOW_US + 100_000))
        self.assertEqual((outcomes["UP"]["best_bid_text"], outcomes["UP"]["bid_size_text"],
                          outcomes["UP"]["midpoint_text"], outcomes["UP"]["spread_text"]),
                         ("0.60", "2.000000000000000000000000000000001", "0.605", "0.01"))
        self.assertEqual((outcomes["UP"]["tick_size_text"], outcomes["UP"]["min_order_size_text"],
                          outcomes["UP"]["bid_level_count"], outcomes["UP"]["ask_level_count"]),
                         ("0.01", "1.000", 257, 1))
        self.assertIsNone(outcomes["DOWN"]["best_ask_text"])
        self.assertIsNone(outcomes["DOWN"]["midpoint_text"])
        self.assertEqual(outcomes["DOWN"]["ask_level_count"], 0)
        attempts = self.rows("request_attempts")
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertEqual(outcomes["UP"]["request_id"], attempt["request_id"])
        self.assertEqual(outcomes["DOWN"]["request_id"], attempt["request_id"])
        self.assertEqual(decode_evidence(attempt["response_raw_z"], attempt["raw_size_bytes"],
                                        attempt["codec_version"]), commit.books.attempts[0].raw_body)
        self.assertEqual((attempt["raw_complete"], attempt["started_server_us"], attempt["ended_server_us"]),
                         (1, NOW_US, NOW_US + 100_000))
        association = self.rows("reference_associations")[0]
        self.assertEqual((association["slot_id"], association["status"], association["origin"],
                          association["available_live"], association["learned_wall_us"]),
                         (slot, "DISABLED", "NOT_FETCHED", 0, NOW_US + 100_000))
        self.assertEqual(association["feed_id"], "0x0002ee6757e8822c00d273bc340fc24c9cafe123a4ff2ea1dbdb31944bc7d95f")
        self.assertEqual(self.rows("recording_runs")[0]["last_committed_wall_us"], NOW_US + 200_000)

    def test_partial_and_failed_preserve_explicit_results(self):
        for index, kind, status, valid_count in ((150, "partial", "PARTIAL", 1),
                                                 (151, "failed", "FAILED", 0)):
            target = self.target(index)
            slot = self.reserve(target)
            self.finalize(slot, self.commit(paired_bundle(kind), target))
            rows = self.rows("book_observations", "WHERE slot_id=?", (slot,))
            self.assertEqual(len(rows), 2)
            self.assertEqual(sum(row["status"] == "VALID" for row in rows), valid_count)
            self.assertEqual(self.rows("sample_slots", "WHERE slot_id=?", (slot,))[0]["status"], status)
            for row in rows:
                if row["status"] != "VALID":
                    self.assertIsNone(row["best_bid_text"])
                    self.assertIsNone(row["midpoint_text"])
                    self.assertIsNotNone(row["error_code"])

    def test_missed_slot_has_no_observations_or_attempts(self):
        self.store.mark_missed(MARKET_ID, self.target(0), run_id=None, reason="BEFORE_START", wall_us=NOW_US)
        missed = self.rows("sample_slots")[0]
        self.assertEqual((missed["status"], missed["reason"], missed["metadata_revision_id"], missed["run_id"]),
                         ("MISSED", "BEFORE_START", 1, None))
        self.assertIsNone(missed["start_mono_ns"])
        self.assertEqual(self.rows("book_observations"), [])
        self.assertEqual(self.rows("request_attempts"), [])

    def assert_invariant(self, action):
        with self.assertRaises(RecorderStoreError) as raised:
            action()
        self.assertEqual(raised.exception.code, "fatal_invariant")
        self.assertFalse(self.store._connection.in_transaction)

    def test_wrong_slot_formula_is_rejected_before_reservation_or_missed_write(self):
        for target in (models.SlotTarget(150, NOW_US + 1), models.SlotTarget(-1, WINDOW_START_US - 2_000_000),
                       models.SlotTarget(450, WINDOW_START_US + 900_000_000), models.SlotTarget(True, WINDOW_START_US + 2_000_000)):
            with self.subTest(target=target):
                self.assert_invariant(lambda: self.reserve(target))
                self.assert_invariant(lambda: self.store.mark_missed(MARKET_ID, target, run_id=self.run,
                                      reason="DOWNTIME", wall_us=NOW_US))
        self.assertEqual(self.rows("sample_slots"), [])

    def test_reservation_requires_same_market_revision_and_run_anchor(self):
        other_run = self.store.start_run(models.RunConfig(), wall_us=NOW_US, app_revision="another")
        with self.store._connection:
            self.store._connection.execute("""INSERT INTO markets SELECT
                'other','other-event','other-condition','other-slug',question,window_start_us,window_end_us,
                first_seen_us,'33','44',0,1,lifecycle,resolution_state,resolution_next_due_us,
                resolution_attempts,selected_resolution_id FROM markets WHERE market_id=?""", (MARKET_ID,))
            self.store._connection.execute("""INSERT INTO market_revisions
                SELECT 2,'other',observed_us,run_id,description,resolution_source_url,active,closed,
                accepting_orders,enable_order_book,fee_config_json,tick_size_text,min_order_size_text,
                delay_config_json,gamma_raw_z,clob_config_raw_z,raw_sizes_json,codec_version,content_hash
                FROM market_revisions WHERE revision_id=1""")
        for market, run, anchor, revision in ((MARKET_ID, self.run, 1, 2),
                                             (MARKET_ID, other_run, 1, 1),
                                             ("unknown", self.run, 1, 1),
                                             (MARKET_ID, self.run, 99, 1),
                                             (MARKET_ID, self.run, 1, 99)):
            with self.subTest(market=market, run=run, anchor=anchor, revision=revision):
                self.assert_invariant(lambda: self.store.reserve_slot(market, self.target(), run_id=run,
                                      anchor_id=anchor, metadata_revision_id=revision,
                                      started_mono_ns=0, started_server_us=NOW_US))
        self.assertEqual(self.rows("sample_slots"), [])

    def test_reservation_is_idempotent_only_for_equal_in_progress_identity(self):
        slot = self.reserve()
        self.assertEqual(self.reserve(), slot)
        self.assert_invariant(lambda: self.store.reserve_slot(MARKET_ID, self.target(), run_id=self.run,
                              anchor_id=1, metadata_revision_id=1,
                              started_mono_ns=1, started_server_us=NOW_US))
        self.assertEqual(len(self.rows("sample_slots")), 1)

    def test_finalize_rejects_changed_reservation_identity_or_times(self):
        slot = self.reserve()
        commit = self.commit()
        for changes in (dict(market_id="unknown"), dict(run_id="unknown"), dict(anchor_id=99),
                        dict(metadata_revision_id=99), dict(target=self.target(151)),
                        dict(started_mono_ns=1), dict(ended_mono_ns=-1)):
            with self.subTest(changes=changes):
                self.assert_invariant(lambda: self.finalize(slot, replace(commit, **changes)))
        self.assertEqual(self.rows("request_attempts"), [])
        self.assertEqual(self.rows("book_observations"), [])

    def test_book_mapping_and_evidence_links_are_independently_validated(self):
        slot = self.reserve()
        commit = self.commit()
        up = commit.books.up
        variants = (replace(up, token_id=TOKEN_DOWN), replace(up, condition_id="wrong"),
                    replace(up, outcome="DOWN"), replace(up, book=replace(up.book, token_id=TOKEN_DOWN)),
                    replace(up, book=replace(up.book, condition_id="wrong")),
                    replace(up, attempt_index=-1), replace(up, attempt_index=99),
                    replace(up, attempt_index=None), replace(up, book=None),
                    replace(up, book=replace(up.book, best_bid=Decimal("NaN"))),
                    replace(up, book=replace(up.book, best_bid=0.6)))
        for result in variants:
            with self.subTest(result=result):
                changed = replace(commit, books=replace(commit.books, up=result))
                self.assert_invariant(lambda: self.finalize(slot, changed))
        attempt = commit.books.attempts[0]
        for changes in (dict(selectors=(TOKEN_UP,)), dict(method="POST", path="/order"),
                        dict(raw_complete=False), dict(ended_mono_ns=200_000_000)):
            with self.subTest(attempt_changes=changes):
                changed = replace(commit, books=replace(commit.books, attempts=(replace(attempt, **changes),)))
                self.assert_invariant(lambda: self.finalize(slot, changed))
        self.assertEqual(self.rows("request_attempts"), [])
        self.assertEqual(self.rows("book_observations"), [])

    def test_original_reference_requires_exact_slot_and_disabled_provenance(self):
        slot = self.reserve()
        commit = self.commit()
        for changes in (dict(market_id="wrong"), dict(role="OPENING"), dict(target_us=NOW_US + 1),
                        dict(slot_id=slot + 1), dict(status="AVAILABLE"), dict(origin="LIVE_LATEST"),
                        dict(available_live=True), dict(fetch_id=99), dict(report_id=99), dict(attempt_count=1)):
            with self.subTest(changes=changes):
                changed = replace(commit, reference=replace(commit.reference, **changes))
                self.assert_invariant(lambda: self.finalize(slot, changed))
        self.assertEqual(self.rows("reference_associations"), [])
        self.assertEqual(self.rows("book_observations"), [])

    def test_conflicting_duplicate_is_fatal_invariant(self):
        slot = self.reserve()
        commit = self.commit()
        self.finalize(slot, commit)
        original = {table: self.rows(table) for table in ("sample_slots", "book_observations",
                                                         "request_attempts", "reference_associations")}
        self.finalize(slot, commit)
        self.assertEqual(original, {table: self.rows(table) for table in original})
        attempt = commit.books.attempts[0]
        variants = (replace(commit, ended_mono_ns=100_000_001),
                    replace(commit, books=replace(commit.books, up=replace(commit.books.up, quality={"changed": True}))),
                    replace(commit, books=replace(commit.books, attempts=(replace(attempt, raw_body=attempt.raw_body + b" "),))),
                    replace(commit, reference=replace(commit.reference, learned_wall_us=NOW_US + 100_001)))
        for changed in variants:
            self.assert_invariant(lambda: self.finalize(slot, changed))
        self.assert_invariant(lambda: self.reserve())
        self.assertEqual(original, {table: self.rows(table) for table in original})

    def test_missed_is_idempotent_and_cannot_be_replaced_by_capture(self):
        target = self.target()
        self.store.mark_missed(MARKET_ID, target, run_id=self.run, reason="DOWNTIME", wall_us=NOW_US)
        original = self.rows("sample_slots")
        self.store.mark_missed(MARKET_ID, target, run_id=self.run, reason="DOWNTIME", wall_us=NOW_US)
        self.assert_invariant(lambda: self.store.mark_missed(MARKET_ID, target, run_id=self.run,
                              reason="BEFORE_START", wall_us=NOW_US))
        self.assert_invariant(lambda: self.reserve())
        self.assert_invariant(lambda: self.finalize(original[0]["slot_id"], self.commit()))
        self.assertEqual(self.rows("sample_slots"), original)
        self.assertEqual(self.rows("book_observations"), [])

    def test_mid_transaction_failure_rolls_back(self):
        slot = self.reserve()
        self.store._connection.execute("""CREATE TEMP TRIGGER injected_write_failure
            BEFORE INSERT ON book_observations WHEN NEW.outcome='DOWN'
            BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END""")
        with self.assertRaises(RecorderStoreError) as raised:
            self.finalize(slot, self.commit())
        self.assertEqual(raised.exception.code, "storage_failure")
        self.assertEqual(self.rows("sample_slots")[0]["status"], "IN_PROGRESS")
        self.assertEqual(self.rows("book_observations"), [])
        self.assertEqual(self.rows("request_attempts"), [])
        self.assertEqual(self.rows("reference_associations"), [])
        self.assertIsNone(self.rows("recording_runs")[0]["last_committed_wall_us"])
        self.store._connection.execute("DROP TRIGGER injected_write_failure")
        with self.assertRaises(RecorderStoreError) as stopped:
            self.store.start_run(models.RunConfig(), wall_us=NOW_US, app_revision="must-not-write")
        self.assertEqual(stopped.exception.code, "storage_failure")
        self.assertEqual(len(self.rows("recording_runs")), 1)

    def test_disabled_run_and_terminal_run_state_are_enforced(self):
        self.assert_invariant(lambda: self.store.start_run(models.RunConfig(mode="REFERENCE_REQUIRED"),
                              wall_us=NOW_US, app_revision="missing-gates"))
        self.assert_invariant(lambda: self.store.finish_run(self.run, state="RUNNING", wall_us=NOW_US, reason="bad"))
        self.assert_invariant(lambda: self.store.finish_run("unknown", state="STOPPED", wall_us=NOW_US, reason="bad"))
        self.store.finish_run(self.run, state="STOPPED", wall_us=NOW_US + 1, reason="stop")
        self.store.finish_run(self.run, state="STOPPED", wall_us=NOW_US + 1, reason="stop")
        self.assert_invariant(lambda: self.store.finish_run(self.run, state="FAILED", wall_us=NOW_US + 2, reason="replace"))
        self.assert_invariant(lambda: self.reserve())

    def test_recovery_never_reacquires_reserved_slot(self):
        self.assertTrue(callable(getattr(self.store, "recover_interrupted", None)),
                        "Writer-owned interrupted reservation recovery is missing")
        completed = self.reserve()
        self.finalize(completed, self.commit())
        target = self.target(151)
        interrupted = self.store.reserve_slot(MARKET_ID, target, run_id=self.run, anchor_id=1,
            metadata_revision_id=1, started_mono_ns=2_100_000_000, started_server_us=NOW_US + 2_100_000)
        original_slot = self.rows("sample_slots", "WHERE slot_id=?", (interrupted,))[0]
        original_complete = self.rows("sample_slots", "WHERE slot_id=?", (completed,))[0]
        original_books = self.rows("book_observations", "WHERE slot_id=? ORDER BY outcome", (completed,))
        original_attempts = self.rows("request_attempts")
        self.store.close()
        self.store = RecorderStore.open(self.fixture.path)
        self.addCleanup(self.store.close)
        self.store.recover_interrupted(wall_us=NOW_US + 10_000_000)
        self.assertFalse(self.store._connection.in_transaction)
        recovered = self.rows("sample_slots", "WHERE slot_id=?", (interrupted,))[0]
        self.assertEqual((recovered["status"], recovered["reason"], recovered["reference_status"]),
                         ("INTERRUPTED", "RESTART_RECOVERY", "REFERENCE_INCOMPLETE"))
        for key in ("market_id", "slot_index", "scheduled_us", "run_id", "anchor_id", "metadata_revision_id",
                    "started_server_us", "start_mono_ns", "delay_us"):
            self.assertEqual(recovered[key], original_slot[key], key)
        self.assertIsNone(recovered["end_mono_ns"])
        self.assertIsNone(recovered["finished_server_us"])
        self.assertEqual(recovered["committed_wall_us"], NOW_US + 10_000_000)
        results = self.rows("book_observations", "WHERE slot_id=? ORDER BY outcome", (interrupted,))
        self.assertEqual([(row["outcome"], row["expected_token_id"], row["expected_condition_id"], row["status"])
                          for row in results],
                         [("DOWN", TOKEN_DOWN, CONDITION, "INTERRUPTED"),
                          ("UP", TOKEN_UP, CONDITION, "INTERRUPTED")])
        for row in results:
            for key in ("request_id", "source_us", "best_bid_text", "best_ask_text", "midpoint_text", "bid_level_count"):
                self.assertIsNone(row[key])
        run = self.rows("recording_runs")[0]
        self.assertEqual((run["state"], run["mode"], run["started_wall_us"], run["ended_wall_us"]),
                         ("INTERRUPTED", "BOOK_ONLY", NOW_US, NOW_US + 10_000_000))
        self.assertEqual(self.rows("sample_slots", "WHERE slot_id=?", (completed,))[0], original_complete)
        self.assertEqual(self.rows("book_observations", "WHERE slot_id=? ORDER BY outcome", (completed,)), original_books)
        self.assertEqual(self.rows("request_attempts"), original_attempts)
        association = self.rows("reference_associations", "WHERE slot_id=?", (interrupted,))[0]
        self.assertEqual((association["status"], association["origin"], association["learned_wall_us"],
                          association["learned_server_us"]), ("DISABLED", "NOT_FETCHED", NOW_US + 10_000_000, None))
        snapshot = {table: self.rows(table) for table in ("sample_slots", "book_observations", "recording_runs", "reference_associations")}
        self.store.recover_interrupted(wall_us=NOW_US + 20_000_000)
        self.assertEqual(snapshot, {table: self.rows(table) for table in snapshot})
        self.assert_invariant(lambda: self.reserve(target))
        self.assert_invariant(lambda: self.finalize(interrupted, self.commit(target=target, started_mono_ns=2_100_000_000,
                                                                           ended_mono_ns=2_200_000_000)))
        new_run = self.store.start_run(models.RunConfig(), wall_us=NOW_US + 20_000_000, app_revision="restart")
        self.assertNotEqual(new_run, self.run)
        self.assertEqual(len(self.rows("sample_slots")), 2)

    def test_recovery_is_one_transaction_and_keeps_finished_runs(self):
        self.assertTrue(callable(getattr(self.store, "recover_interrupted", None)),
                        "Writer-owned interrupted reservation recovery is missing")
        self.reserve()
        second = self.reserve(self.target(151))
        finished = self.store.start_run(models.RunConfig(), wall_us=NOW_US, app_revision="finished")
        self.store.finish_run(finished, state="STOPPED", wall_us=NOW_US + 1, reason="clean")
        finished_row = self.rows("recording_runs", "WHERE run_id=?", (finished,))[0]
        self.store._connection.execute(f"""CREATE TEMP TRIGGER recovery_failure
            BEFORE INSERT ON book_observations WHEN NEW.slot_id={second}
            BEGIN SELECT RAISE(ABORT, 'injected recovery failure'); END""")
        with self.assertRaises(RecorderStoreError) as raised:
            self.store.recover_interrupted(wall_us=NOW_US + 10_000_000)
        self.assertEqual(raised.exception.code, "storage_failure")
        self.assertEqual([row["status"] for row in self.rows("sample_slots")], ["IN_PROGRESS", "IN_PROGRESS"])
        self.assertEqual(self.rows("book_observations"), [])
        self.assertEqual(self.rows("reference_associations"), [])
        self.assertEqual(self.rows("recording_runs", "WHERE run_id=?", (self.run,))[0]["state"], "RUNNING")
        self.store.close()
        self.store = RecorderStore.open(self.fixture.path)
        self.addCleanup(self.store.close)
        self.store.recover_interrupted(wall_us=NOW_US + 20_000_000)
        self.assertEqual([row["status"] for row in self.rows("sample_slots")], ["INTERRUPTED", "INTERRUPTED"])
        self.assertEqual(len(self.rows("book_observations")), 4)
        self.assertEqual(self.rows("recording_runs", "WHERE run_id=?", (finished,))[0], finished_row)

    def test_valid_quote_cannot_cross_slot_deadline_but_late_evidence_can(self):
        slot = self.reserve()
        commit = self.commit(ended_mono_ns=2_000_000_000)
        attempt = replace(commit.books.attempts[0], ended_mono_ns=2_000_000_000)
        commit = replace(commit, books=replace(commit.books, attempts=(attempt,)))
        self.assert_invariant(lambda: self.finalize(slot, commit))
        late = replace(commit, books=replace(commit.books,
            up=replace(commit.books.up, status="LATE", error_code="late_response"),
            down=replace(commit.books.down, status="LATE", error_code="late_response")))
        self.finalize(slot, late)
        self.assertEqual(self.rows("sample_slots")[0]["status"], "FAILED")
        for row in self.rows("book_observations"):
            self.assertEqual(row["status"], "LATE")
            self.assertIsNotNone(row["source_us"])
            self.assertIsNone(row["best_bid_text"])
            self.assertIsNone(row["midpoint_text"])

    def test_numeric_and_source_summary_contracts_reject_contradictions(self):
        slot = self.reserve()
        commit = self.commit()
        up = commit.books.up
        variants = (replace(up, book=replace(up.book, best_bid=Decimal("-0.1"))),
                    replace(up, book=replace(up.book, bid_size=Decimal("-1"))),
                    replace(up, book=replace(up.book, midpoint=Decimal("0.8"))),
                    replace(up, book=replace(up.book, source_timestamp_raw="0")),
                    replace(up, book=replace(up.book, source_at=commit.books.down.book.source_at)),
                    replace(up, book_metadata={"tick_size": Decimal("-1")}),
                    replace(up, book_metadata={"min_order_size": Decimal("NaN")}))
        for result in variants:
            with self.subTest(result=result):
                self.assert_invariant(lambda: self.finalize(slot, replace(commit, books=replace(commit.books, up=result))))
        self.assertEqual(self.rows("book_observations"), [])

    def test_fallback_attempts_keep_distinct_original_bodies_and_links(self):
        fixture = BookFixture(batch=[], status=404,
                              singles={TOKEN_UP: (200, make_book()), TOKEN_DOWN: (200, down_book())})
        self.addCleanup(fixture.close)
        books = fixture.collect()
        slot = self.reserve()
        self.finalize(slot, self.commit(books, ended_mono_ns=300_000_000))
        attempts = self.rows("request_attempts", "ORDER BY request_id")
        self.assertEqual([(row["operation"], row["http_status"]) for row in attempts],
                         [("BOOK_BATCH", 404), ("BOOK_SINGLE", 200), ("BOOK_SINGLE", 200)])
        results = {row["outcome"]: row for row in self.rows("book_observations")}
        self.assertEqual(results["UP"]["request_id"], attempts[1]["request_id"])
        self.assertEqual(results["DOWN"]["request_id"], attempts[2]["request_id"])
        for saved, original in zip(attempts, books.attempts):
            self.assertEqual(decode_evidence(saved["response_raw_z"], saved["raw_size_bytes"],
                                            saved["codec_version"]), original.raw_body)
        self.assertEqual((attempts[1]["ended_server_us"], attempts[2]["ended_server_us"]),
                         (NOW_US + 200_000, NOW_US + 300_000))

    def test_fallback_expiry_before_down_attempt_persists_valid_partial(self):
        fixture = BookFixture(batch=[], status=404,
                              singles={TOKEN_UP: (200, make_book()), TOKEN_DOWN: (200, down_book())})
        self.addCleanup(fixture.close)
        actual_get = fixture.public.get_json

        def expire_before_down_transport(source, path, *, params, deadline):
            # DOWN passed the collector's can_start; the real transport now refuses to start.
            if params["token_id"] == TOKEN_DOWN:
                fixture.clock.advance(deadline - fixture.clock.monotonic())
            return actual_get(source, path, params=params, deadline=deadline)

        fixture.public.get_json = expire_before_down_transport
        books = fixture.collect()
        self.assertEqual(fixture.requests, [("POST", "/books"), ("GET", "/book")])
        self.assertEqual([(attempt.operation, attempt.selectors) for attempt in books.attempts],
                         [("BOOK_BATCH", (TOKEN_UP, TOKEN_DOWN)), ("BOOK_SINGLE", ("token_id=" + TOKEN_UP,))])
        slot = self.reserve()
        commit = self.commit(books, ended_mono_ns=2_000_000_000)
        commit = replace(commit, reference=replace(commit.reference,
                         learned_wall_us=NOW_US + 2_000_000, learned_server_us=NOW_US + 2_000_000))
        try:
            self.finalize(slot, commit)
        except RecorderStoreError as exc:
            self.fail(f"No-attempt fallback expiry must durably preserve PARTIAL: {exc.code}: {exc}")
        self.assertEqual(self.rows("sample_slots")[0]["status"], "PARTIAL")
        results = {row["outcome"]: row for row in self.rows("book_observations")}
        self.assertEqual(set(results), {"UP", "DOWN"})
        attempts = self.rows("request_attempts", "ORDER BY request_id")
        self.assertEqual(len(attempts), 2)
        self.assertEqual((results["UP"]["status"], results["UP"]["request_id"], results["UP"]["best_bid_text"]),
                         ("VALID", attempts[1]["request_id"], "0.60"))
        self.assertEqual((results["DOWN"]["status"], results["DOWN"]["request_id"], results["DOWN"]["best_bid_text"]),
                         ("LATE", None, None))
        self.assertIsNone(books.down.attempt_index)
        for saved, original in zip(attempts, books.attempts):
            self.assertEqual(decode_evidence(saved["response_raw_z"], saved["raw_size_bytes"],
                                            saved["codec_version"]), original.raw_body)

    def test_started_failed_down_fallback_keeps_its_own_attempt(self):
        fixture = BookFixture(batch=[], status=404,
                              singles={TOKEN_UP: (200, make_book()), TOKEN_DOWN: (404, b'{"error":"missing"}')})
        self.addCleanup(fixture.close)
        books = fixture.collect()
        slot = self.reserve()
        self.finalize(slot, self.commit(books, ended_mono_ns=300_000_000))
        attempts = self.rows("request_attempts", "ORDER BY request_id")
        results = {row["outcome"]: row for row in self.rows("book_observations")}
        self.assertEqual(self.rows("sample_slots")[0]["status"], "PARTIAL")
        self.assertEqual(len(attempts), 3)
        self.assertEqual((results["UP"]["status"], results["UP"]["request_id"]),
                         ("VALID", attempts[1]["request_id"]))
        self.assertEqual((results["DOWN"]["status"], results["DOWN"]["request_id"]),
                         ("UNAVAILABLE", attempts[2]["request_id"]))
        self.assertEqual(decode_evidence(attempts[2]["response_raw_z"], attempts[2]["raw_size_bytes"],
                                        attempts[2]["codec_version"]), b'{"error":"missing"}')

    def test_no_attempt_failure_remains_missing_and_truncated_bytes_stay_diagnostic(self):
        slot = self.reserve()
        commit = self.commit()
        failed = replace(commit, books=replace(commit.books,
            up=replace(commit.books.up, status="UNAVAILABLE", book=None, attempt_index=None, error_code="not_started"),
            down=replace(commit.books.down, status="UNAVAILABLE", book=None, attempt_index=None, error_code="not_started"),
            attempts=()))
        self.finalize(slot, failed)
        self.assertEqual(self.rows("sample_slots")[0]["status"], "FAILED")
        self.assertEqual(self.rows("request_attempts"), [])
        self.assertEqual([row["request_id"] for row in self.rows("book_observations")], [None, None])
        target = self.target(151)
        slot2 = self.reserve(target)
        attempt = replace(commit.books.attempts[0], raw_body=b'{"partial":', raw_complete=False,
                          result="failure", error_code="response_too_large")
        diagnostic = replace(failed, target=target, reference=replace(failed.reference, target_us=target.scheduled_us),
                             books=replace(failed.books, attempts=(attempt,),
                                 up=replace(failed.books.up, attempt_index=0), down=replace(failed.books.down, attempt_index=0)))
        self.finalize(slot2, diagnostic)
        saved = self.rows("request_attempts")[0]
        self.assertEqual(saved["raw_complete"], 0)
        self.assertEqual(decode_evidence(saved["response_raw_z"], saved["raw_size_bytes"], saved["codec_version"]),
                         b'{"partial":')


if __name__ == "__main__":
    unittest.main()
