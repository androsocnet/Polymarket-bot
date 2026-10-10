"""Offline payout classification against the pinned market identity."""
import json
import unittest
from dataclasses import replace
from decimal import Decimal
import httpx

from polymarket_bot.market_data.parsing import parse_market
from polymarket_bot.market_data.http import HttpAttempt, PublicGet
from polymarket_bot.recorder.models import MarketRevision, RunConfig
from polymarket_bot.recorder.resolution import parse_resolution, reconcile_due
from polymarket_bot.recorder.store import RecorderStore, RecorderStoreError, decode_evidence
from tests.recorder_support import DatabaseFixture, NOW_US, WINDOW_START_US, seed_writer_market
from tests.support import CONDITION, NOW, FakeClock, make_event, make_market


class ResolutionParsingTests(unittest.TestCase):
    def setUp(self):
        market = parse_market(make_event(), make_market(), now=NOW)
        self.revision = MarketRevision(market, 1, 0, "rules", "https://example.test/rules",
                                       b"{}", None, 1, {})

    def parse(self, row):
        raw = json.dumps(row).encode()
        return parse_resolution(row, self.revision, raw_response=raw,
                                learned_wall_us=1_000_000, learned_server_us=1_000_001)

    def test_nonunit_payout_maps_saved_indices(self):
        row = {"condition_id": CONDITION, "status": "resolved", "payouts": [1000000, 0],
               "resolved_at": "2026-10-10T01:00:00Z"}
        result = self.parse(row)
        self.assertEqual((result.classification, result.mapped_outcome),
                         ("RESOLVED_DIRECTIONAL", "DOWN"))
        self.assertEqual(result.payouts, (Decimal(1000000), Decimal(0)))
        self.assertEqual(result.reported_us, 1_791_594_000_000_000)

    def test_split_unknown_and_malformed_never_force_winner(self):
        for payouts, classification in [([1, 1], "RESOLVED_OTHER"),
                                         ([0, 0], "RESOLVED_OTHER"),
                                         (["1e99999", 0], "INVALID"),
                                         (["9" * 10000, 0], "INVALID"),
                                         (["1e-99999", 0], "INVALID"),
                                         ([1], "INVALID"), ([1, -1], "INVALID"),
                                         (None, "INVALID")]:
            with self.subTest(payouts=str(payouts)[:40]):
                result = self.parse({"condition_id": CONDITION, "status": "resolved",
                                     "payouts": payouts})
                self.assertEqual(result.classification, classification)
                self.assertIsNone(result.mapped_outcome)
                self.assertTrue(result.provenance.get("diagnostic") if classification == "INVALID" else True)

    def test_reported_time_not_replaced_by_learned_time(self):
        result = self.parse({"condition_id": CONDITION, "status": "resolved",
                             "payouts": [0, 1]})
        self.assertIsNone(result.reported_us)
        self.assertEqual((result.learned_wall_us, result.learned_server_us), (1_000_000, 1_000_001))

    def test_pending_absent_and_identity_mismatch(self):
        self.assertEqual(self.parse(None).classification, "ABSENT")
        self.assertEqual(self.parse({"condition_id": CONDITION, "status": "pending",
                                     "payouts": [1, 0]}).classification, "PENDING")
        self.assertIsNone(self.parse({"condition_id": CONDITION, "status": "pending",
                                      "payouts": [1, 0]}).mapped_outcome)
        bad = self.parse({"condition_id": "0x" + "cd" * 32, "status": "resolved",
                          "payouts": [1, 0]})
        self.assertEqual((bad.classification, bad.mapped_outcome), ("INVALID", None))

    def test_extreme_reported_time_and_status_are_bounded_invalid_evidence(self):
        for value in ("9" * 10000, "1e99999", 10**100, "2026-10-10T01:00:00"):
            with self.subTest(time=str(value)[:20]):
                result = self.parse({"condition_id": CONDITION, "status": "resolved",
                                     "payouts": [1, 0], "resolved_at": value})
                self.assertEqual((result.classification, result.mapped_outcome), ("INVALID", None))
                self.assertLess(len(json.dumps(result.provenance)), 300)
        result = self.parse({"condition_id": CONDITION, "status": "x" * 10000,
                             "payouts": [1, 0]})
        self.assertEqual((result.classification, result.mapped_outcome), ("INVALID", None))
        self.assertLess(len(json.dumps(result.provenance)), 300)

    def test_direct_nonfinite_decimal_payout_is_bounded_invalid_evidence(self):
        for value in (Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"),
                      Decimal("-Infinity")):
            for vector in ([value, Decimal(0)], [Decimal(0), value]):
                with self.subTest(value=str(value), vector=str(vector)):
                    result = parse_resolution({"condition_id": CONDITION, "status": "resolved",
                                               "payouts": vector}, self.revision,
                                              raw_response=b"{}", learned_wall_us=1_000_000,
                                              learned_server_us=None)
                    self.assertEqual((result.classification, result.mapped_outcome), ("INVALID", None))
                    self.assertLess(len(json.dumps(result.provenance)), 300)


class ResolutionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseFixture()
        self.store = RecorderStore.open(self.db.path)
        self.run_id = self.store.start_run(RunConfig(), wall_us=NOW_US, app_revision="test")
        seed_writer_market(self.store, self.run_id)
        self.end_us = WINDOW_START_US + 900_000_000
        self.store.expire_market("1001", observed_us=self.end_us)
        self.clock = FakeClock()
        self.clock.value = 600
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.store.close()
        self.db.close()

    def add_market(self, number):
        condition = "0x" + f"{number:064x}"
        with self.store._connection:
            self.store._connection.execute("""INSERT INTO markets
                (market_id,event_id,condition_id,slug,question,window_start_us,window_end_us,
                 first_seen_us,up_token_id,down_token_id,up_outcome_index,down_outcome_index,
                 lifecycle,resolution_state,resolution_next_due_us,resolution_attempts)
                 SELECT ?,event_id,?,slug,question,window_start_us,window_end_us,
                 first_seen_us,up_token_id,down_token_id,up_outcome_index,down_outcome_index,
                 'EXPIRED','PENDING',?,0 FROM markets WHERE market_id='1001'""",
                (str(number), condition, self.end_us))
            self.store._connection.execute("""INSERT INTO market_revisions
                (market_id,observed_us,run_id,description,resolution_source_url,gamma_raw_z,
                 raw_sizes_json,codec_version,content_hash)
                 SELECT ?,observed_us,run_id,description,resolution_source_url,gamma_raw_z,
                 raw_sizes_json,codec_version,? FROM market_revisions WHERE market_id='1001'""",
                (str(number), f"revision-{number}"))
        return condition

    def public(self, response, *, status=200):
        attempts = []
        requests = []
        def respond(request):
            requests.append(request)
            self.clock.advance(0.01)
            return httpx.Response(status, stream=httpx.ByteStream(json.dumps(response).encode()))
        client = httpx.Client(transport=httpx.MockTransport(respond))
        self.clients.append(client)
        public = PublicGet(client, monotonic=self.clock.monotonic,
                           monotonic_ns=lambda: int(self.clock.value * 1e9),
                           sleep=self.clock.sleep, utcnow=self.clock.utcnow,
                           max_attempts=1, on_attempt=attempts.append)
        return public, attempts, requests

    def save_direct_final(self, payouts, ordinal):
        """Exercise the public parser/writer interface without HTTP JSON token limits."""
        from polymarket_bot.recorder.resolution import _revision_from_row
        revision = _revision_from_row(self.store._one("SELECT * FROM markets WHERE market_id='1001'"))
        row = {"condition_id": CONDITION, "status": "resolved", "payouts": payouts}
        raw = json.dumps({"data": [row]}, separators=(",", ":")).encode()
        learned = self.end_us + ordinal * 1_000_000
        attempt = HttpAttempt("RESOLUTION", "GET", "/v2/resolutions",
            ("condition=" + CONDITION,), 1, ordinal * 1_000_000_000,
            ordinal * 1_000_000_000 + 1_000, learned - 1, learned,
            200, "success", None, raw, True)
        observation = parse_resolution(row, revision, raw_response=raw,
            learned_wall_us=learned, learned_server_us=None)
        self.assertIn(observation.classification, ("RESOLVED_DIRECTIONAL", "RESOLVED_OTHER"))
        self.store.save_resolutions((observation,), requests=(attempt,))
        return raw

    def test_resolution_independent_of_reference(self):
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        with self.store._connection:
            self.store._connection.execute("""INSERT INTO reference_associations
                (market_id,feed_id,role,target_us,status,origin,learned_wall_us,
                 available_live,attempt_count) VALUES (?,?,?,?,?,?,?,?,?)""",
                ("1001", CHAINLINK_FEED_ID, "CLOSING", self.end_us, "DISABLED",
                 "NOT_FETCHED", self.end_us, 0, 0))
        public, attempts, requests = self.public({"data": [{"condition_id": CONDITION,
            "status": "resolved", "payouts": [1000000, 0]}]})
        self.assertEqual(reconcile_due(public, self.store, now_us=self.end_us, deadline=700), 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[0].url.path, "/v2/resolutions")
        row = self.store._one("SELECT resolution_state,resolution_next_due_us FROM markets")
        self.assertEqual((row["resolution_state"], row["resolution_next_due_us"]),
                         ("RESOLVED_DIRECTIONAL", None))
        obs = self.store._one("SELECT * FROM resolution_observations")
        self.assertEqual((obs["mapped_outcome"], obs["payouts_raw_json"]),
                         ("DOWN", "[1000000,0]"))
        self.assertIsNone(obs["reported_resolved_us"])
        self.assertGreater(obs["learned_wall_us"], self.end_us)
        self.assertEqual(self.store._one("SELECT count(*) AS n FROM request_attempts")["n"], 1)
        self.assertEqual(decode_evidence(obs["raw_resolution_z"], obs["raw_size_bytes"], 1),
                         attempts[0].raw_body)
        self.assertEqual(self.store._one("SELECT status FROM reference_associations")["status"], "DISABLED")
        self.assertEqual(reconcile_due(public, self.store, now_us=self.end_us + 10**9, deadline=700), 0)

    def test_transport_failure_and_pending_are_persisted_for_retry(self):
        public, attempts, _ = self.public({"error": "unavailable"}, status=503)
        self.assertEqual(reconcile_due(public, self.store, now_us=self.end_us, deadline=700), 1)
        observation = self.store._one("SELECT classification,provenance_json FROM resolution_observations")
        self.assertEqual(observation["classification"], "INVALID")
        self.assertIn("transport_error", observation["provenance_json"])
        self.assertEqual(self.store._one("SELECT resolution_state FROM markets")["resolution_state"], "RETRY_WAIT")
        self.assertEqual(len(attempts), 1)
        with self.store._connection:
            self.store._connection.execute("UPDATE markets SET resolution_next_due_us=?", (self.end_us,))
        public2, _, _ = self.public({"data": [{"condition_id": CONDITION,
            "status": "pending", "payouts": [1, 0]}]})
        self.assertEqual(reconcile_due(public2, self.store, now_us=self.end_us, deadline=700), 1)
        rows = self.store._rows("SELECT classification,mapped_outcome FROM resolution_observations ORDER BY resolution_id")
        self.assertEqual([(r["classification"], r["mapped_outcome"]) for r in rows],
                         [("INVALID", None), ("PENDING", None)])

    def test_invalid_final_retains_raw_payout_summary_without_winner(self):
        public, _, _ = self.public({"data": [{"condition_id": CONDITION,
            "status": "resolved", "payouts": ["1e99999", 0]}]})
        self.assertEqual(reconcile_due(public, self.store, now_us=self.end_us, deadline=700), 1)
        row = self.store._one("SELECT status_raw,payouts_raw_json,mapped_outcome,classification FROM resolution_observations")
        self.assertEqual((row["status_raw"], row["payouts_raw_json"],
                          row["mapped_outcome"], row["classification"]),
                         ("resolved", '["1e99999",0]', None, "INVALID"))

    def test_unrequested_condition_is_diagnostic_not_a_winner(self):
        public, _, _ = self.public({"data": [{"condition_id": "0x" + "cd" * 32,
            "status": "resolved", "payouts": [1, 0]}]})
        self.assertEqual(reconcile_due(public, self.store, now_us=self.end_us, deadline=700), 1)
        row = self.store._one("SELECT classification,mapped_outcome,provenance_json FROM resolution_observations")
        self.assertEqual((row["classification"], row["mapped_outcome"]), ("INVALID", None))
        self.assertIn("unrequested_condition", row["provenance_json"])

    def test_exhausted_deadline_keeps_due_without_fabricating_request(self):
        public, attempts, requests = self.public({"data": []})
        self.assertEqual(reconcile_due(public, self.store, now_us=self.end_us, deadline=600), 0)
        self.assertEqual((attempts, requests), ([], []))
        row = self.store._one("SELECT resolution_state,resolution_next_due_us,resolution_attempts FROM markets")
        self.assertEqual(tuple(row.values()), ("PENDING", self.end_us, 0))
        self.assertEqual(self.store._one("SELECT count(*) AS n FROM request_attempts")["n"], 0)

    def test_batch_writer_rolls_back_all_observations_on_identity_failure(self):
        bad_condition = "0x" + "cd" * 32
        public, attempts, _ = self.public({"data": []})
        public.get_json("data", "/v2/resolutions",
                        params={"condition": CONDITION + "," + bad_condition}, deadline=700)
        market = self.store.due_resolutions(self.end_us)[0]
        first = parse_resolution(None, market, raw_response=attempts[0].raw_body,
                                 learned_wall_us=attempts[0].ended_wall_us,
                                 learned_server_us=None)
        second = replace(first, market_id="unknown", condition_id=bad_condition)
        with self.assertRaises(RecorderStoreError):
            self.store.save_resolutions((first, second), requests=tuple(attempts))
        self.assertEqual(self.store._one("SELECT count(*) AS n FROM request_attempts")["n"], 0)
        self.assertEqual(self.store._one("SELECT count(*) AS n FROM resolution_observations")["n"], 0)
        self.assertEqual(self.store._one("SELECT resolution_state FROM markets")["resolution_state"], "PENDING")

    def test_missing_reordered_and_duplicated_results_are_explicit(self):
        second = self.add_market(1002)
        third = self.add_market(1003)
        public, _, requests = self.public({"data": [
            {"condition_id": third, "status": "resolved", "payouts": [0, 3]},
            {"condition_id": CONDITION, "status": "resolved", "payouts": [1, 0]},
            {"condition_id": CONDITION, "status": "resolved", "payouts": [0, 1]}]})
        self.assertEqual(reconcile_due(public, self.store, now_us=self.end_us, deadline=700), 3)
        self.assertEqual(len(requests), 1)
        self.assertEqual(set(requests[0].url.params["condition"].split(",")),
                         {CONDITION, second, third})
        rows = self.store._rows("""SELECT m.market_id,m.resolution_state,o.classification,o.provenance_json
            FROM markets m JOIN resolution_observations o USING(market_id) ORDER BY m.market_id""")
        self.assertEqual([(r["market_id"], r["classification"]) for r in rows],
                         [("1001", "INVALID"), ("1002", "ABSENT"),
                          ("1003", "RESOLVED_DIRECTIONAL")])
        self.assertIn("duplicate", rows[0]["provenance_json"])
        self.assertEqual(self.store._one("SELECT count(*) AS n FROM request_attempts")["n"], 1)

    def test_due_limit_and_persisted_exponential_retry(self):
        for number in range(1002, 1024):
            self.add_market(number)
        self.assertEqual(len(self.store.due_resolutions(self.end_us)), 20)
        public, attempts, requests = self.public({"data": []})
        self.assertEqual(reconcile_due(public, self.store, now_us=self.end_us, deadline=700), 20)
        self.assertEqual(len(requests[0].url.params["condition"].split(",")), 20)
        row = self.store._one("SELECT resolution_state,resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")
        self.assertEqual(tuple(row.values()), ("RETRY_WAIT", attempts[0].ended_wall_us + 30_000_000, 1))
        self.assertEqual(len(self.store.due_resolutions(self.end_us + 29_999_999)), 3)
        self.store.close()
        self.store = RecorderStore.open(self.db.path)
        self.assertEqual(len(self.store.due_resolutions(self.end_us)), 3)
        # Isolate the first market from remaining due work to verify 60/120/cap.
        with self.store._connection:
            self.store._connection.execute("UPDATE markets SET resolution_next_due_us=NULL WHERE market_id!='1001'")
        for attempts, delay in [(2, 60), (3, 120), (11, 21600)]:
            if attempts == 11:
                with self.store._connection:
                    self.store._connection.execute("UPDATE markets SET resolution_attempts=10 WHERE market_id='1001'")
            due = self.store._one("SELECT resolution_next_due_us FROM markets WHERE market_id='1001'")["resolution_next_due_us"]
            self.assertEqual(reconcile_due(public, self.store, now_us=due, deadline=700), 1)
            row = self.store._one("SELECT resolution_next_due_us,resolution_attempts FROM markets WHERE market_id='1001'")
            self.assertEqual(tuple(row.values()), (due + delay * 1_000_000, attempts))

    def test_conflicting_final_preserves_both_observations(self):
        public, attempts, _ = self.public({"data": [{"condition_id": CONDITION,
            "status": "resolved", "payouts": [1, 0]}]})
        reconcile_due(public, self.store, now_us=self.end_us, deadline=700)
        original = self.store._one("SELECT selected_resolution_id FROM markets")["selected_resolution_id"]
        attempt = replace(attempts[0], started_wall_us=attempts[0].started_wall_us + 1_000_000,
                          ended_wall_us=attempts[0].ended_wall_us + 1_000_000)
        revision = self.store.due_resolutions(self.end_us + 1)  # Terminal market is absent.
        self.assertEqual(revision, ())
        from polymarket_bot.recorder.resolution import _revision_from_row
        market = _revision_from_row(self.store._one("SELECT * FROM markets"))
        other = parse_resolution({"condition_id": CONDITION, "status": "resolved", "payouts": [0, 1]},
                                 market, raw_response=b'{"second":true}',
                                 learned_wall_us=attempt.ended_wall_us, learned_server_us=None)
        self.store.save_resolutions((other,), requests=(attempt,))
        state = self.store._one("SELECT resolution_state,selected_resolution_id FROM markets")
        self.assertEqual(tuple(state.values()), ("CONFLICT", original))
        self.assertEqual(self.store._one("SELECT count(*) AS n FROM resolution_observations")["n"], 2)

    def test_direct_large_bare_integer_equivalent_final_reloads(self):
        first_raw = self.save_direct_final([10**20, 0], 1)
        original = self.store._one("SELECT selected_resolution_id FROM markets")["selected_resolution_id"]
        second_raw = self.save_direct_final([2 * 10**20, 0], 2)
        market = self.store._one("SELECT resolution_state,selected_resolution_id FROM markets")
        self.assertEqual(tuple(market.values()), ("RESOLVED_DIRECTIONAL", original))
        rows = self.store._rows("SELECT * FROM resolution_observations ORDER BY resolution_id")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["payouts_raw_json"], "[100000000000000000000,0]")
        self.assertEqual([decode_evidence(row["raw_resolution_z"], row["raw_size_bytes"],
            row["codec_version"]) for row in rows], [first_raw, second_raw])

    def test_direct_large_bare_integer_changed_split_conflicts(self):
        first_raw = self.save_direct_final([10**20, 10**20], 1)
        original = self.store._one("SELECT selected_resolution_id FROM markets")["selected_resolution_id"]
        second_raw = self.save_direct_final([10**20, 10**20 + 1], 2)
        market = self.store._one("SELECT resolution_state,selected_resolution_id FROM markets")
        self.assertEqual(tuple(market.values()), ("CONFLICT", original))
        rows = self.store._rows("SELECT * FROM resolution_observations ORDER BY resolution_id")
        self.assertEqual(len(rows), 2)
        self.assertEqual([decode_evidence(row["raw_resolution_z"], row["raw_size_bytes"],
            row["codec_version"]) for row in rows], [first_raw, second_raw])

    def test_corrupt_stored_payout_number_is_structured_and_atomic(self):
        self.save_direct_final([1, 0], 1)
        original = self.store._one("SELECT selected_resolution_id FROM markets")["selected_resolution_id"]
        with self.store._connection:
            self.store._connection.execute("UPDATE resolution_observations SET payouts_raw_json='[1e99999,0]'")
        with self.assertRaises(RecorderStoreError) as caught:
            self.save_direct_final([1, 0], 2)
        self.assertEqual(caught.exception.code, "fatal_invariant")
        self.assertEqual(self.store._one("SELECT count(*) AS n FROM request_attempts")["n"], 1)
        self.assertEqual(self.store._one("SELECT count(*) AS n FROM resolution_observations")["n"], 1)
        market = self.store._one("SELECT resolution_state,selected_resolution_id FROM markets")
        self.assertEqual(tuple(market.values()), ("RESOLVED_DIRECTIONAL", original))

    def append_later_final(self, first_payouts, later_payouts):
        first_public, _, _ = self.public({"data": [{"condition_id": CONDITION,
            "status": "resolved", "payouts": first_payouts}]})
        self.assertEqual(reconcile_due(first_public, self.store, now_us=self.end_us, deadline=700), 1)
        original = self.store._one("SELECT selected_resolution_id FROM markets")["selected_resolution_id"]
        self.clock.advance(1)
        later_public, attempts, _ = self.public({"data": [{"condition_id": CONDITION,
            "status": "resolved", "payouts": later_payouts}]})
        read = later_public.get_json("data", "/v2/resolutions",
            params={"condition": CONDITION}, deadline=700)
        from polymarket_bot.recorder.resolution import _revision_from_row
        market = _revision_from_row(self.store._one("SELECT * FROM markets"))
        observation = parse_resolution(read.payload["data"][0], market,
            raw_response=read.raw_body, learned_wall_us=attempts[0].ended_wall_us,
            learned_server_us=None)
        self.store.save_resolutions((observation,), requests=tuple(attempts))
        row = self.store._one("SELECT resolution_state,selected_resolution_id,resolution_next_due_us FROM markets")
        evidence = self.store._rows("""SELECT payouts_raw_json,raw_resolution_z,raw_size_bytes,codec_version
            FROM resolution_observations ORDER BY resolution_id""")
        self.assertEqual(len(evidence), 2)
        self.assertEqual(json.loads(decode_evidence(evidence[0]["raw_resolution_z"],
            evidence[0]["raw_size_bytes"], evidence[0]["codec_version"]))["data"][0]["payouts"], first_payouts)
        self.assertEqual(json.loads(decode_evidence(evidence[1]["raw_resolution_z"],
            evidence[1]["raw_size_bytes"], evidence[1]["codec_version"]))["data"][0]["payouts"], later_payouts)
        return row, original

    def test_split_then_zero_final_conflicts(self):
        row, original = self.append_later_final([1, 1], [0, 0])
        self.assertEqual((row["resolution_state"], row["selected_resolution_id"],
                          row["resolution_next_due_us"]), ("CONFLICT", original, None))

    def test_changed_split_proportions_conflict(self):
        row, original = self.append_later_final([1, 1], [1, 2])
        self.assertEqual((row["resolution_state"], row["selected_resolution_id"],
                          row["resolution_next_due_us"]), ("CONFLICT", original, None))

    def test_proportional_nondirectional_finals_are_equivalent(self):
        row, original = self.append_later_final([1, 1], [2, 2])
        self.assertEqual((row["resolution_state"], row["selected_resolution_id"],
                          row["resolution_next_due_us"]), ("RESOLVED_OTHER", original, None))

    def test_large_close_split_ratios_remain_distinct(self):
        large = 10**50
        row, original = self.append_later_final([str(large + 1), str(large)],
                                                [str(large), str(large)])
        self.assertEqual((row["resolution_state"], row["selected_resolution_id"],
                          row["resolution_next_due_us"]), ("CONFLICT", original, None))
