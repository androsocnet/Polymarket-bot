"""Offline reference evidence: no credentials, signer or live report decoder."""
from dataclasses import replace
from decimal import Decimal, localcontext
from hashlib import sha256
import importlib
import unittest
from unittest.mock import patch

from polymarket_bot.recorder import models
from polymarket_bot.market_data.http import HttpAttempt
from polymarket_bot.recorder.store import RecorderStore, decode_evidence
from tests.recorder_support import (DatabaseFixture, NOW_US, WINDOW_START_US,
                                    seed_writer_market, paired_bundle)
from tests.support import MARKET_ID


class ReferenceTests(unittest.TestCase):
    def reference(self):
        try:
            return importlib.import_module("polymarket_bot.recorder.reference")
        except ModuleNotFoundError:
            self.fail("Credential-free reference gate implementation is missing")

    def test_book_only_never_reads_or_constructs_credentials(self):
        fixture = DatabaseFixture()
        self.addCleanup(fixture.close)
        store = RecorderStore.open(fixture.path)
        self.addCleanup(store.close)
        with patch("os.getenv") as getter, patch("hmac.new") as signer, \
                patch("httpx.Client") as factory, patch("httpx.request") as request, \
                patch("pathlib.Path.read_text") as secret_file:
            reference = self.reference()
            importlib.reload(reference)
            reference.require_reference_activation("BOOK_ONLY", None)
            value = reference.disabled_reference("market", NOW_US,
                learned_wall_us=NOW_US + 10, learned_server_us=None)
            store.start_run(models.RunConfig(), wall_us=NOW_US, app_revision="offline")
        self.assertEqual((value.status, value.origin, value.available_live, value.report_id),
                         ("DISABLED", "NOT_FETCHED", False, None))
        self.assertEqual([spy.call_count for spy in (getter, signer, factory, request, secret_file)],
                         [0, 0, 0, 0, 0])

    def test_each_missing_activation_gate_refuses(self):
        reference = self.reference()
        evidence = reference.ActivationEvidence(True, True, True, True, True, True)
        with patch("os.getenv") as getter, patch("hmac.new") as signer, \
                patch("httpx.Client") as factory, patch("httpx.request") as request:
            for name in evidence.__dataclass_fields__:
                with self.subTest(gate=name), self.assertRaises(Exception) as caught:
                    reference.require_reference_activation("REFERENCE_REQUIRED", replace(evidence, **{name: False}))
                self.assertEqual(caught.exception.code, "activation_required")
            with self.assertRaises(Exception) as caught:
                reference.require_reference_activation("REFERENCE_REQUIRED", None)
            self.assertEqual(caught.exception.code, "activation_required")
            # These six true booleans are synthetic nonsecret test evidence only.
            reference.require_reference_activation("REFERENCE_REQUIRED", evidence)
        self.assertEqual([spy.call_count for spy in (getter, signer, factory, request)], [0, 0, 0, 0])

    def normalize(self, *, raw=b"signed-report", fields=None, scale=8, revision="offline-v2", receipt=NOW_US):
        reference = self.reference()
        self.assertTrue(hasattr(reference, "normalize_reference_fields"), "Offline normalization is missing")
        return reference.normalize_reference_fields(raw, fields if fields is not None else {
            "feed_id": models.CHAINLINK_FEED_ID, "report_schema_version": 2,
            "valid_from_seconds": NOW_US // 1_000_000 - 60,
            "observations_seconds": NOW_US // 1_000_000,
            "expires_at_seconds": NOW_US // 1_000_000 + 60,
            "price_integer": "6000012345678"}, price_scale=scale, decoder_revision=revision,
            received_wall_us=receipt + 123, received_server_us=receipt)

    def test_boundary_reconciler_retains_unverified_and_retries_exact_target(self):
        reference = self.reference()
        self.assertTrue(hasattr(reference, "reconcile_due_reference_boundaries"))
        store, run = self.database()
        plan = reference.boundary_reference(MARKET_ID, "OPENING",
            window_start_us=WINDOW_START_US, window_end_us=WINDOW_START_US + 900_000_000,
            learned_wall_us=NOW_US, learned_server_us=NOW_US)
        store.save_reference_plan(plan)
        fetch = self.fetch(run)
        report = self.normalize()
        class Reader:
            def fetch_boundary(self, row, deadline):
                self.target = row.target_us
                return fetch, (report,)
        reader = Reader()
        count = reference.reconcile_due_reference_boundaries(store, reader, NOW_US, 10,
            monotonic=lambda: 0, selection_policy=lambda reports, target: reports[0],
            policy_revision="offline-policy")
        self.assertEqual(count, 1)
        self.assertEqual(reader.target, WINDOW_START_US)
        row = store._one("SELECT * FROM reference_associations ORDER BY association_id DESC LIMIT 1")
        self.assertEqual((row["status"], row["origin"], row["available_live"], row["reason"]),
                         ("UNAVAILABLE", "HISTORICAL_LOOKUP", 0, "DON_UNVERIFIED"))
        self.assertEqual(row["next_due_us"], NOW_US + 60_000_000)
        self.assertEqual(store._one("SELECT verification_status FROM reference_reports")["verification_status"], "UNVERIFIED")
        self.assertEqual(reference.reconcile_due_reference_boundaries(store, reader, NOW_US, 0,
            monotonic=lambda: 0), 0)

    def test_scale_and_source_times_are_independent_of_actual_receipt(self):
        with localcontext() as context:
            context.prec = 3
            value = self.normalize(receipt=NOW_US + 10_000_000)
        self.assertEqual(value.decoded_price, Decimal("60000.12345678"))
        self.assertEqual((value.valid_from_seconds, value.observations_seconds,
                          value.expires_at_seconds),
                         (NOW_US // 1_000_000 - 60, NOW_US // 1_000_000, NOW_US // 1_000_000 + 60))
        self.assertEqual((value.first_received_wall_us, value.first_received_server_us),
                         (NOW_US + 10_000_123, NOW_US + 10_000_000))
        self.assertEqual((value.decode_status, value.verification_status), ("DECODED", "UNVERIFIED"))
        self.assertEqual(value.report_hash, sha256(b"signed-report").hexdigest())
        self.assertEqual(value.raw_signed_report, b"signed-report")
        for scale, revision in ((None, "offline-v2"), (8, None)):
            with self.subTest(scale=scale, revision=revision):
                unknown = self.normalize(scale=scale, revision=revision)
                self.assertEqual((unknown.decode_status, unknown.decoded_price), ("UNVERIFIED", None))

    def test_reference_numeric_failure_is_evidence(self):
        good = {"feed_id": models.CHAINLINK_FEED_ID, "report_schema_version": 2,
                "valid_from_seconds": 1, "observations_seconds": 60, "expires_at_seconds": 120,
                "price_integer": "1"}
        bad = (("price_integer", "1e999999999"), ("price_integer", "9" * 129),
               ("price_integer", True), ("price_integer", "1.1"),
               ("price_integer", 10**5000), ("observations_seconds", 2**63),
               ("valid_from_seconds", "9" * 5000), ("expires_at_seconds", -1))
        for field, value in bad:
            with self.subTest(field=field):
                report = self.normalize(fields=good | {field: value}, raw=b"invalid-signed-bytes")
                self.assertEqual((report.decode_status, report.decoded_price), ("INVALID", None))
                self.assertLessEqual(len(str(report.diagnostics)), 256)
                self.assertEqual(report.raw_signed_report, b"invalid-signed-bytes")
        for scale in (-1, 129, True, "8"):
            with self.subTest(scale=scale):
                report = self.normalize(scale=scale)
                self.assertEqual((report.decode_status, report.decoded_price), ("INVALID", None))

    def test_only_exact_feed_schema_and_valid_interval_can_decode(self):
        fields = {"feed_id": models.CHAINLINK_FEED_ID, "report_schema_version": 2,
                  "valid_from_seconds": 10, "observations_seconds": 60,
                  "expires_at_seconds": 120, "price_integer": "1"}
        for changes in ({"feed_id": "spot-feed"}, {"report_schema_version": 3},
                        {"valid_from_seconds": 61}, {"expires_at_seconds": 59},
                        {"price_integer": "-1"}, {"observations_seconds": None}):
            with self.subTest(changes=changes):
                report = self.normalize(fields=fields | changes)
                self.assertEqual((report.decode_status, report.decoded_price), ("INVALID", None))
                self.assertEqual(report.feed_id, models.CHAINLINK_FEED_ID)

    def database(self):
        fixture = DatabaseFixture()
        self.addCleanup(fixture.close)
        store = RecorderStore.open(fixture.path)
        self.addCleanup(store.close)
        run = store.start_run(models.RunConfig(), wall_us=NOW_US, app_revision="offline-reference")
        seed_writer_market(store, run)
        self.assertTrue(hasattr(store, "save_reference_evidence"), "Reference evidence writer is missing")
        return store, run

    def fetch(self, run, *, receipt=NOW_US, origin="HISTORICAL_LOOKUP", purpose="OPENING"):
        self.assertTrue(hasattr(models, "ReferenceFetchDraft"), "Reference fetch record is missing")
        attempt = HttpAttempt("REFERENCE_LOOKUP", "GET", "/api/v1/reports",
            ("feed_id=" + models.CHAINLINK_FEED_ID,), 1, 0, 100_000_000,
            receipt - 100_000, receipt + 123, 200, "success", None, b"offline-envelope", True)
        return models.ReferenceFetchDraft(attempt=attempt, run_id=run,
            feed_id=models.CHAINLINK_FEED_ID, purpose=purpose, origin=origin,
            target_us=WINDOW_START_US, status="AVAILABLE", error_code=None,
            raw_response=b"offline-envelope", received_server_us=receipt)

    def test_four_tables_preserve_raw_and_first_receipt_fetch(self):
        store, run = self.database()
        report = self.normalize()
        association = models.ReferenceAssociationDraft(MARKET_ID, "OPENING", WINDOW_START_US,
            "AVAILABLE", "HISTORICAL_LOOKUP", False, NOW_US + 123, NOW_US,
            report_hash=report.report_hash, selection_policy_revision="offline-boundary-v1")
        store.save_reference_evidence(self.fetch(run), (report,), (association,))
        later = self.normalize(receipt=NOW_US + 10_000_000)
        store.save_reference_evidence(self.fetch(run, receipt=NOW_US + 10_000_000), (later,),
            (replace(association, learned_wall_us=NOW_US + 10_000_123,
                     learned_server_us=NOW_US + 10_000_000),))
        connection = store._connection
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM reference_feeds").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM reference_fetches").fetchone()[0], 2)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM reference_reports").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM reference_associations").fetchone()[0], 2)
        saved = dict(zip([c[0] for c in connection.execute("SELECT * FROM reference_reports").description],
                         connection.execute("SELECT * FROM reference_reports").fetchone()))
        self.assertEqual((saved["first_fetch_id"], saved["first_received_wall_us"],
                          saved["first_received_server_us"], saved["decoded_price_text"]),
                         (1, NOW_US + 123, NOW_US, "60000.12345678"))
        self.assertEqual(decode_evidence(saved["raw_report_z"], saved["raw_size_bytes"], saved["codec_version"]),
                         b"signed-report")
        fetch_raw = connection.execute("SELECT response_raw_z,raw_size_bytes,codec_version FROM reference_fetches").fetchone()
        self.assertEqual(decode_evidence(*fetch_raw), b"offline-envelope")
        feed = connection.execute("SELECT lookback_seconds,report_schema_version,price_scale,verified_us FROM reference_feeds").fetchone()
        self.assertEqual(feed, (60, 2, None, None))
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unassociated_invalid_reports_are_retained_and_bad_link_rolls_back(self):
        store, run = self.database()
        report = self.normalize(fields={}, raw=b"invalid-original")
        store.save_reference_evidence(self.fetch(run), (report,), ())
        self.assertEqual(store._connection.execute("SELECT decode_status,decoded_price_text FROM reference_reports").fetchone(),
                         ("INVALID", None))
        wrong = models.ReferenceAssociationDraft(MARKET_ID, "CLOSING", WINDOW_START_US,
            "AVAILABLE", "HISTORICAL_LOOKUP", False, NOW_US + 10_000_123, NOW_US + 10_000_000,
            report_hash=report.report_hash)
        with self.assertRaises(Exception):
            store.save_reference_evidence(self.fetch(run, receipt=NOW_US + 10_000_000),
                (self.normalize(fields={}, raw=b"invalid-original", receipt=NOW_US + 10_000_000),), (wrong,))
        self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM reference_fetches").fetchone()[0], 1)

    def test_multi_report_associations_link_hash_and_reject_unknown_or_disagreeing_id(self):
        store, run = self.database()
        first, second = self.normalize(raw=b"first"), self.normalize(raw=b"second")
        association = models.ReferenceAssociationDraft(MARKET_ID, "OPENING", WINDOW_START_US,
            "AVAILABLE", "HISTORICAL_LOOKUP", False, NOW_US + 123, NOW_US,
            report_hash=second.report_hash)
        store.save_reference_evidence(self.fetch(run), (first, second), (association,))
        linked = store._connection.execute("SELECT r.report_hash FROM reference_associations a "
            "JOIN reference_reports r USING(report_id)").fetchone()[0]
        self.assertEqual(linked, sha256(b"second").hexdigest())
        for changes in ({"report_hash": "unknown"}, {"report_id": 1}, {"report_hash": None}):
            with self.subTest(changes=changes), self.assertRaises(Exception):
                store.save_reference_evidence(self.fetch(run, receipt=NOW_US + 10_000_000),
                    (self.normalize(raw=b"first", receipt=NOW_US + 10_000_000),
                     self.normalize(raw=b"second", receipt=NOW_US + 10_000_000)),
                    (replace(association, learned_wall_us=NOW_US + 10_000_123,
                             learned_server_us=NOW_US + 10_000_000, **changes),))
        self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM reference_fetches").fetchone()[0], 1)

    def test_writer_refuses_invented_receipt_hash_or_foreign_feed(self):
        store, run = self.database()
        report = self.normalize()
        for changes in ({"first_received_wall_us": NOW_US},
                        {"first_received_server_us": NOW_US - 60_000_000},
                        {"report_hash": "not-the-raw-hash"}, {"feed_id": "spot"}):
            with self.subTest(changes=changes), self.assertRaises(Exception):
                store.save_reference_evidence(self.fetch(run), (replace(report, **changes),), ())
        self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM reference_fetches").fetchone()[0], 0)

    def eligible(self, association, report, decision, role):
        reference = self.reference()
        self.assertTrue(hasattr(reference, "eligible_as_of"), "Reference as-of eligibility is missing")
        return reference.eligible_as_of(association, report, decision_us=decision, role=role)

    def test_live_current_requires_actual_receipt_verification_and_five_second_age(self):
        report = replace(self.normalize(receipt=NOW_US + 1_000_000), verification_status="VERIFIED")
        association = models.ReferenceAssociationDraft(MARKET_ID, "SLOT_CURRENT", NOW_US,
            "AVAILABLE", "LIVE_LATEST", True, NOW_US + 1_000_123, NOW_US + 1_000_000,
            slot_id=1, fetch_id=1, report_id=1, report_hash=report.report_hash)
        for decision, expected in ((NOW_US, False), (NOW_US + 999_999, False),
                                   (NOW_US + 1_000_000, True), (NOW_US + 5_000_000, True),
                                   (NOW_US + 5_000_001, False)):
            with self.subTest(decision=decision):
                self.assertEqual(self.eligible(association, report, decision, "SLOT_CURRENT"), expected)
        for changes in ({"verification_status": "UNVERIFIED"}, {"decode_status": "INVALID"},
                        {"decoded_price": None}, {"feed_id": "spot"}, {"report_schema_version": 3},
                        {"observations_seconds": NOW_US // 1_000_000 + 3},
                        {"expires_at_seconds": NOW_US // 1_000_000},
                        {"first_received_server_us": None}):
            with self.subTest(changes=changes):
                self.assertFalse(self.eligible(association, replace(report, **changes),
                                              NOW_US + 1_000_000, "SLOT_CURRENT"))
        for changes in ({"available_live": False}, {"origin": "HISTORICAL_LOOKUP"},
                        {"learned_server_us": None}, {"status": "UNAVAILABLE"},
                        {"report_hash": "wrong"}):
            with self.subTest(changes=changes):
                self.assertFalse(self.eligible(replace(association, **changes), report,
                                              NOW_US + 1_000_000, "SLOT_CURRENT"))

    def test_repaired_opening_is_prospective_only(self):
        report = replace(self.normalize(receipt=NOW_US + 10_000_000), verification_status="VERIFIED")
        association = models.ReferenceAssociationDraft(MARKET_ID, "OPENING", WINDOW_START_US,
            "AVAILABLE", "HISTORICAL_LOOKUP", False, NOW_US + 10_000_123, NOW_US + 10_000_000,
            fetch_id=1, report_id=1, report_hash=report.report_hash,
            selection_policy_revision="synthetic-verified-boundary")
        self.assertFalse(self.eligible(association, report, NOW_US + 9_000_000, "OPENING"))
        self.assertTrue(self.eligible(association, report, NOW_US + 10_000_000, "OPENING"))
        self.assertTrue(self.eligible(association, report, NOW_US + 600_000_000, "OPENING"))
        self.assertEqual((association.origin, association.available_live), ("HISTORICAL_LOOKUP", False))
        self.assertFalse(self.eligible(replace(association, selection_policy_revision=None), report,
                                      NOW_US + 10_000_000, "OPENING"))
        self.assertFalse(self.eligible(association, report, NOW_US + 10_000_000, "SLOT_CURRENT"))
        self.assertFalse(self.eligible(replace(association, role="CLOSING"), report,
                                      NOW_US + 900_000_000, "CLOSING"))

    def test_historical_current_never_becomes_live(self):
        store, run = self.database()
        target = models.SlotTarget(150, NOW_US)
        slot = store.reserve_slot(MARKET_ID, target, run_id=run, anchor_id=1,
            metadata_revision_id=1, started_mono_ns=0, started_server_us=NOW_US)
        disabled = self.reference().disabled_reference(MARKET_ID, NOW_US,
            learned_wall_us=NOW_US + 100_000, learned_server_us=NOW_US + 100_000)
        store.finalize_slot(slot, models.SlotCommit(MARKET_ID, target, run, 1, 1,
            paired_bundle(), disabled, 0, 100_000_000), committed_wall_us=NOW_US + 200_000)
        report = replace(self.normalize(receipt=NOW_US + 1_000_000), verification_status="VERIFIED")
        association = models.ReferenceAssociationDraft(MARKET_ID, "SLOT_CURRENT", NOW_US,
            "AVAILABLE", "HISTORICAL_LOOKUP", False, NOW_US + 1_000_123, NOW_US + 1_000_000,
            slot_id=slot, report_hash=report.report_hash)
        fetch = replace(self.fetch(run, receipt=NOW_US + 1_000_000, purpose="REPAIR"), target_us=NOW_US)
        store.save_reference_evidence(fetch, (report,), (association,))
        self.assertFalse(self.eligible(association, report, NOW_US + 2_000_000, "SLOT_CURRENT"))
        self.assertEqual(store._connection.execute("SELECT reference_status FROM sample_slots").fetchone()[0],
                         "REFERENCE_INCOMPLETE")
        self.assertEqual(store._connection.execute("SELECT status,available_live,report_id FROM reference_associations "
            "ORDER BY association_id LIMIT 1").fetchone(), ("DISABLED", 0, None))

    def test_projection_and_due_work_use_latest_evidence_without_mutation(self):
        reference = self.reference()
        self.assertTrue(hasattr(reference, "reference_status_projection"), "Reference status projection is missing")
        self.assertTrue(hasattr(reference, "reference_due_work"), "Reference due work is missing")
        opening = models.ReferenceAssociationDraft(MARKET_ID, "OPENING", WINDOW_START_US,
            "PENDING", "NOT_FETCHED", False, NOW_US, NOW_US, next_due_us=NOW_US)
        later = replace(opening, status="AVAILABLE", learned_wall_us=NOW_US + 1, next_due_us=None)
        closing = replace(opening, role="CLOSING", target_us=WINDOW_START_US + 900_000_000,
                          next_due_us=NOW_US + 10)
        conflict = replace(opening, market_id="other", status="CONFLICT", next_due_us=NOW_US - 10)
        rows = (later, closing, opening, conflict)
        projection = reference.reference_status_projection(rows)
        self.assertIn(later, projection)
        self.assertNotIn(opening, projection)
        self.assertEqual(reference.reference_due_work(rows, now_us=NOW_US, limit=1), (conflict,))
        self.assertEqual(reference.reference_due_work(rows, now_us=NOW_US + 10, limit=2), (conflict, closing))
        self.assertEqual(reference.reference_due_work(rows, now_us=NOW_US, limit=0), ())
        self.assertEqual(opening.status, "PENDING")
        with self.assertRaises(ValueError):
            reference.reference_due_work(rows, now_us=NOW_US, limit=-1)

    def test_boundary_plans_persist_exact_targets_without_requests(self):
        reference = self.reference()
        self.assertTrue(hasattr(reference, "boundary_reference"), "Dormant boundary planning is missing")
        store, run = self.database()
        self.assertTrue(hasattr(store, "save_reference_plan"), "Boundary due-plan persistence is missing")
        for role, target in (("OPENING", WINDOW_START_US), ("CLOSING", WINDOW_START_US + 900_000_000)):
            plan = reference.boundary_reference(MARKET_ID, role,
                window_start_us=WINDOW_START_US, window_end_us=WINDOW_START_US + 900_000_000,
                learned_wall_us=NOW_US, learned_server_us=NOW_US)
            self.assertEqual((plan.target_us, plan.next_due_us, plan.status, plan.origin),
                             (target, target, "PENDING", "NOT_FETCHED"))
            store.save_reference_plan(plan)
            store.save_reference_plan(plan)
        self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM reference_associations").fetchone()[0], 2)
        self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM request_attempts").fetchone()[0], 0)
        self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM reference_fetches").fetchone()[0], 0)
        with self.assertRaises(Exception):
            store.save_reference_plan(replace(plan, target_us=plan.target_us - 1))
        with self.assertRaises(Exception):
            store.save_reference_plan(replace(plan, origin="HISTORICAL_LOOKUP"))

    def test_boundary_retries_skip_passed_targets_and_cap_backoff_at_six_hours(self):
        reference = self.reference()
        self.assertTrue(hasattr(reference, "boundary_retry_due"), "Dormant boundary retry schedule is missing")
        target = WINDOW_START_US
        for attempt, now_offset, due_offset in ((1, 0, 2), (2, 2, 5), (3, 5, 15), (4, 15, 30),
                                               (5, 30, 90), (6, 90, 210), (1, 6, 15),
                                               (1, 100, 160), (1000, 100, 21700)):
            with self.subTest(attempt=attempt, now=now_offset):
                self.assertEqual(reference.boundary_retry_due(target,
                    attempt_count=attempt, now_us=target + now_offset * 1_000_000),
                    target + due_offset * 1_000_000)
        with self.assertRaises(ValueError):
            reference.boundary_retry_due(target, attempt_count=0, now_us=target)

    def test_page_plans_enforce_bounded_short_gap_work_without_retrieval(self):
        reference = self.reference()
        self.assertTrue(hasattr(reference, "reference_page_plan"), "Dormant page bounds are missing")
        with patch("httpx.request") as request:
            self.assertEqual(reference.reference_page_plan(reports_per_call=100, calls=2), (100, 100))
            self.assertEqual(reference.reference_page_plan(reports_per_call=7, calls=1), (7,))
            for kwargs in ({"reports_per_call": 101, "calls": 1}, {"reports_per_call": 100, "calls": 3},
                           {"reports_per_call": 0, "calls": 1}, {"reports_per_call": True, "calls": 1}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    reference.reference_page_plan(**kwargs)
        self.assertEqual(request.call_count, 0)

    def test_missing_ambiguous_and_conflicting_fetches_remain_evidence_without_prices(self):
        store, run = self.database()
        for index, status in enumerate(("UNAVAILABLE", "AMBIGUOUS", "CONFLICT")):
            receipt = NOW_US + index * 10_000_000
            fetch = self.fetch(run, receipt=receipt)
            fetch = replace(fetch, attempt=replace(fetch.attempt, http_status=404, result="http_error", error_code="missing"),
                            status=status, error_code="missing", raw_response=None)
            association = models.ReferenceAssociationDraft(MARKET_ID, "OPENING", WINDOW_START_US,
                status, "HISTORICAL_LOOKUP", False, receipt + 123, receipt,
                reason="No verified boundary report", next_due_us=receipt + 60_000_000, attempt_count=index + 1)
            store.save_reference_evidence(fetch, (), (association,))
        self.assertEqual(store._connection.execute("SELECT status,report_id,reason FROM reference_associations "
            "ORDER BY association_id").fetchall(),
            [(status, None, "No verified boundary report") for status in ("UNAVAILABLE", "AMBIGUOUS", "CONFLICT")])
        self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM reference_reports").fetchone()[0], 0)

    def test_invalid_or_unknown_scale_cannot_claim_available_boundary(self):
        for index, report in enumerate((self.normalize(fields={}, raw=b"invalid"),
                                        self.normalize(scale=None, raw=b"unknown"))):
            store, run = self.database()
            association = models.ReferenceAssociationDraft(MARKET_ID, "OPENING", WINDOW_START_US,
                "AVAILABLE", "HISTORICAL_LOOKUP", False, NOW_US + 123, NOW_US,
                report_hash=report.report_hash, selection_policy_revision="offline")
            with self.subTest(index=index), self.assertRaises(Exception):
                store.save_reference_evidence(self.fetch(run), (report,), (association,))
            with self.subTest(index=index):
                self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM reference_fetches").fetchone()[0], 0)

    def test_live_latest_cannot_claim_an_earlier_slots_original_coverage(self):
        for offset, allowed in ((-1, False), (1_000_000, True), (2_000_000, False)):
            store, run = self.database()
            target = models.SlotTarget(150, NOW_US)
            slot = store.reserve_slot(MARKET_ID, target, run_id=run, anchor_id=1,
                metadata_revision_id=1, started_mono_ns=0, started_server_us=NOW_US)
            receipt = NOW_US + offset
            report = replace(self.normalize(receipt=receipt), verification_status="VERIFIED")
            fetch = self.fetch(run, receipt=receipt, origin="LIVE_LATEST", purpose="SLOT_CURRENT")
            fetch = replace(fetch, attempt=replace(fetch.attempt, operation="REFERENCE_LATEST",
                            path="/api/v1/reports/latest"), target_us=NOW_US)
            association = models.ReferenceAssociationDraft(MARKET_ID, "SLOT_CURRENT", NOW_US,
                "AVAILABLE", "LIVE_LATEST", True, receipt + 123, receipt, slot_id=slot,
                report_hash=report.report_hash)
            with self.subTest(offset=offset):
                if allowed:
                    store.save_reference_evidence(fetch, (report,), (association,))
                    self.assertEqual(store._connection.execute("SELECT available_live FROM reference_associations").fetchone()[0], 1)
                else:
                    with self.assertRaises(Exception):
                        store.save_reference_evidence(fetch, (report,), (association,))
                    self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM reference_associations").fetchone()[0], 0)
