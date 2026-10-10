"""Recorder metadata uses strict discovery and preserves source evidence."""
import json
import unittest
from datetime import timedelta

import httpx

from polymarket_bot.market_data.gamma import GammaClient
from polymarket_bot.market_data.http import JsonRead, PublicGet
from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.recorder.clock import RecorderClock
from polymarket_bot.recorder.metadata import acquire_market, refresh_revision
from polymarket_bot.recorder.models import RunConfig
from polymarket_bot.recorder.store import RecorderStore, RecorderStoreError, decode_evidence
from tests.recorder_support import DatabaseFixture, NOW_US
from tests.support import CONDITION, NOW, TOKEN_DOWN, TOKEN_UP, FakeClock, make_event, make_market


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.time = FakeClock()
        self.clock = RecorderClock(monotonic_ns=lambda: int(self.time.value * 1e9),
                                   wall_us=lambda: int(NOW.timestamp() * 1e6))
        self.clock.calibrate(JsonRead(int(NOW.timestamp()), 0, 0,
                                      started_mono_ns=0, finished_mono_ns=0,
                                      started_wall_us=int(NOW.timestamp() * 1e6),
                                      finished_wall_us=int(NOW.timestamp() * 1e6)))
        self.clients = []
        self.databases = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        for store, fixture in self.databases:
            store.close()
            fixture.close()

    def database(self):
        fixture = DatabaseFixture()
        store = RecorderStore.open(fixture.path)
        self.databases.append((store, fixture))
        return store, store.start_run(RunConfig(), wall_us=NOW_US, app_revision="test")

    def transport(self, *, market=None, event=None, config=None, fast_status=200):
        market = market or make_market()
        event = event or make_event(markets=[market])
        config = config or {"t": [{"t": TOKEN_UP, "o": "Up"},
                                  {"t": TOKEN_DOWN, "o": "Down"}],
                            "mts": 0.01, "mos": 5, "mbf": 0, "fd": {"r": 0.02}}
        raw_event = json.dumps(event, separators=(",", ": ")).encode()
        raw_market = json.dumps(market, separators=(",", ": ")).encode()
        raw_config = json.dumps(config, separators=(",", ": ")).encode()
        initial_market_id = market["id"]
        paths = []

        def respond(request):
            paths.append(request.url.path)
            self.time.advance(0.01)
            if request.url.path.startswith("/events/slug/"):
                return httpx.Response(fast_status, stream=httpx.ByteStream(raw_event if fast_status == 200 else b"{}"))
            if request.url.path == "/events":
                return httpx.Response(200, stream=httpx.ByteStream(b"[" + raw_event + b"]"))
            if request.url.path == "/markets/" + initial_market_id:
                current_raw = json.dumps(market, separators=(",", ": ")).encode()
                return httpx.Response(200, stream=httpx.ByteStream(current_raw))
            if request.url.path == "/clob-markets/" + CONDITION:
                current_raw = json.dumps(config, separators=(",", ": ")).encode()
                return httpx.Response(200, stream=httpx.ByteStream(current_raw))
            raise AssertionError(request.url.path)

        client = httpx.Client(transport=httpx.MockTransport(respond))
        self.clients.append(client)
        attempts = []
        public = PublicGet(client, monotonic=self.time.monotonic,
                           monotonic_ns=lambda: int(self.time.value * 1e9),
                           sleep=self.time.sleep, utcnow=self.time.utcnow,
                           max_attempts=1, on_attempt=attempts.append)
        return GammaClient(public), public, paths, attempts, raw_event, raw_market, raw_config

    def test_forced_fallback_independent_of_slug(self):
        gamma, public, paths, attempts, _, raw_market, _ = self.transport()
        revision = acquire_market(gamma, public, clock=self.clock, deadline=120,
                                  use_fast_path=False)
        self.assertFalse(any(path.startswith("/events/slug/") for path in paths))
        self.assertEqual(revision.market.market_id, "1001")
        self.assertEqual(revision.gamma_raw, raw_market)
        self.assertEqual(revision.discovery_source, "filtered_list")
        self.assertTrue(any(attempt.path == "/markets/1001" for attempt in attempts))

    def test_original_outcome_indices_and_rules(self):
        market = make_market(outcomes='["Down", "Up"]',
                             clobTokenIds=json.dumps([TOKEN_DOWN, TOKEN_UP]),
                             outcomePrices='["0.9", "0.1"]',
                             positionIds='["wrong-down", "wrong-up"]',
                             description="First rule.\nSecond rule.",
                             resolutionSource="https://example.test/official-rules")
        gamma, public, _, _, raw_event, _, raw_config = self.transport(market=market)
        revision = acquire_market(gamma, public, clock=self.clock, deadline=120,
                                  use_fast_path=True)
        self.assertEqual((revision.up_index, revision.down_index), (1, 0))
        self.assertEqual((revision.market.up.token_id, revision.market.down.token_id),
                         (TOKEN_UP, TOKEN_DOWN))
        self.assertEqual((revision.description, revision.resolution_source_url),
                         ("First rule.\nSecond rule.", "https://example.test/official-rules"))
        self.assertEqual((revision.gamma_raw, revision.clob_config_raw),
                         (raw_event, raw_config))
        self.assertEqual(str(revision.market.up.metadata_price), "0.1")
        self.assertNotEqual(revision.market.up.token_id, "wrong-up")

    def test_raw_revision_is_saved_with_original_indices(self):
        market = make_market(outcomes='["Down", "Up"]',
                             clobTokenIds=json.dumps([TOKEN_DOWN, TOKEN_UP]),
                             description="Exact rule text", resolutionSource="https://example.test/source")
        gamma, public, _, _, raw_event, _, raw_config = self.transport(market=market)
        revision = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        store, run_id = self.database()
        revision_id = store.save_market_revision(run_id, revision)
        row = store._one("SELECT * FROM markets WHERE market_id=?", (market["id"],))
        saved = store._one("SELECT * FROM market_revisions WHERE revision_id=?", (revision_id,))
        self.assertEqual((row["up_outcome_index"], row["down_outcome_index"]), (1, 0))
        self.assertEqual((saved["description"], saved["resolution_source_url"]),
                         ("Exact rule text", "https://example.test/source"))
        self.assertEqual(decode_evidence(saved["gamma_raw_z"],
                                         json.loads(saved["raw_sizes_json"])["gamma"], 1), raw_event)
        self.assertEqual(decode_evidence(saved["clob_config_raw_z"],
                                         json.loads(saved["raw_sizes_json"])["clob"], 1), raw_config)

    def test_unchanged_refresh_preserves_first_observed_revision(self):
        gamma, public, _, _, _, _, _ = self.transport()
        prior = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        store, run_id = self.database()
        first_id = store.save_market_revision(run_id, prior)
        self.time.advance(61)
        refreshed = refresh_revision(prior, gamma, public, clock=self.clock, deadline=120)
        self.assertGreater(refreshed.observed_us, prior.observed_us)
        self.assertEqual(store.save_market_revision(run_id, refreshed), first_id)
        self.assertEqual(store._one("SELECT count(*) AS n FROM market_revisions")["n"], 1)
        self.assertEqual(store._one("SELECT observed_us FROM market_revisions")["observed_us"], prior.observed_us)

    def test_refresh_conflict_does_not_rewrite_identity(self):
        market = make_market()
        gamma, public, _, _, _, _, _ = self.transport(market=market)
        prior = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        store, run_id = self.database()
        store.save_market_revision(run_id, prior)
        self.time.advance(61)
        market["conditionId"] = "0x" + "cd" * 32
        result = refresh_revision(prior, gamma, public, clock=self.clock, deadline=120)
        self.assertFalse(result.capture_ready)
        self.assertIn("identity_conflict", " ".join(result.warnings))
        with self.assertRaises(RecorderStoreError):
            store.save_market_revision(run_id, result)
        saved = store._one("SELECT * FROM markets WHERE market_id='1001'")
        self.assertEqual(saved["condition_id"], CONDITION)
        self.assertEqual(saved["lifecycle"], "INVALIDATED")
        rows = store._rows("SELECT gamma_raw_z,raw_sizes_json FROM market_revisions ORDER BY revision_id")
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(decode_evidence(rows[1]["gamma_raw_z"],
                                                    json.loads(rows[1]["raw_sizes_json"])["gamma"], 1))["conditionId"],
                         "0x" + "cd" * 32)
        with self.assertRaises(RecorderStoreError):
            store.save_market_revision(run_id, prior)
        end_us = int(prior.market.window_end.timestamp() * 1e6)
        store.expire_market(prior.market.market_id, observed_us=end_us)
        queued = store._one("SELECT lifecycle,resolution_next_due_us FROM markets")
        self.assertEqual((queued["lifecycle"], queued["resolution_next_due_us"]),
                         ("INVALIDATED", end_us))

    def test_expired_open_flags_do_not_block_rollover(self):
        gamma, public, _, _, _, _, _ = self.transport()
        revision = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        store, run_id = self.database()
        store.save_market_revision(run_id, revision)
        self.assertTrue(revision.market.accepting_orders)
        end_us = int(revision.market.window_end.timestamp() * 1e6)
        store.expire_market(revision.market.market_id, observed_us=end_us)
        row = store._one("SELECT lifecycle,resolution_state,resolution_next_due_us FROM markets WHERE market_id='1001'")
        self.assertEqual(tuple(row.values()), ("EXPIRED", "PENDING", end_us))

    def test_changed_slug_is_revision_without_rewriting_pinned_identity(self):
        market = make_market()
        gamma, public, _, _, _, _, _ = self.transport(market=market)
        prior = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        store, run_id = self.database()
        first_id = store.save_market_revision(run_id, prior)
        self.time.advance(61)
        market["slug"] = "renamed-again"
        refreshed = refresh_revision(prior, gamma, public, clock=self.clock, deadline=120)
        self.assertTrue(refreshed.capture_ready)
        self.assertNotEqual(store.save_market_revision(run_id, refreshed), first_id)
        self.assertEqual(store._one("SELECT slug,lifecycle FROM markets")["slug"],
                         "renamed-test-market")
        self.assertEqual(store._one("SELECT lifecycle FROM markets")["lifecycle"], "CURRENT")

    def test_config_failure_keeps_prior_observation_and_warns_of_age(self):
        config = {"t": [{"t": TOKEN_UP, "o": "Up"}, {"t": TOKEN_DOWN, "o": "Down"}]}
        gamma, public, _, _, _, _, _ = self.transport(config=config)
        prior = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        self.time.advance(61)
        config["t"] = [{"t": TOKEN_UP, "o": "Up"}]
        refreshed = refresh_revision(prior, gamma, public, clock=self.clock, deadline=120)
        self.assertEqual(refreshed.observed_us, prior.observed_us)
        self.assertIn("age_us=", " ".join(refreshed.warnings))
        self.assertEqual(refreshed.gamma_raw, prior.gamma_raw)

    def test_failed_refresh_waits_for_next_metadata_interval(self):
        config = {"t": [{"t": TOKEN_UP, "o": "Up"}, {"t": TOKEN_DOWN, "o": "Down"}]}
        gamma, public, paths, _, _, _, _ = self.transport(config=config)
        prior = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        self.time.advance(61)
        config["t"] = []
        failed = refresh_revision(prior, gamma, public, clock=self.clock, deadline=120)
        count = len(paths)
        again = refresh_revision(failed, gamma, public, clock=self.clock, deadline=120)
        self.assertEqual(len(paths), count)
        self.assertEqual(again.observed_us, prior.observed_us)

    def test_closed_market_pauses_capture_with_observed_revision(self):
        market = make_market()
        gamma, public, _, _, _, _, _ = self.transport(market=market)
        prior = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        self.time.advance(61)
        market["closed"] = True
        refreshed = refresh_revision(prior, gamma, public, clock=self.clock, deadline=120)
        self.assertFalse(refreshed.capture_ready)
        self.assertTrue(refreshed.market.closed)
        self.assertGreater(refreshed.observed_us, prior.observed_us)

    def test_not_ready_market_pauses_capture_and_saves_operational_flag(self):
        market = make_market()
        gamma, public, _, _, _, _, _ = self.transport(market=market)
        prior = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        store, run_id = self.database()
        store.save_market_revision(run_id, prior)
        self.time.advance(61)
        market["enableOrderBook"] = False
        refreshed = refresh_revision(prior, gamma, public, clock=self.clock, deadline=120)
        self.assertFalse(refreshed.capture_ready)
        revision_id = store.save_market_revision(run_id, refreshed)
        self.assertEqual(store._one("SELECT enable_order_book FROM market_revisions WHERE revision_id=?",
                                    (revision_id,))["enable_order_book"], 0)

    def test_archived_market_pauses_capture(self):
        market = make_market()
        gamma, public, _, _, _, _, _ = self.transport(market=market)
        prior = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        self.time.advance(61)
        market["archived"] = True
        refreshed = refresh_revision(prior, gamma, public, clock=self.clock, deadline=120)
        self.assertFalse(refreshed.capture_ready)

    def test_refresh_window_conflict_invalidates_without_rewriting_start_end(self):
        market = make_market()
        gamma, public, _, _, _, _, _ = self.transport(market=market)
        prior = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        store, run_id = self.database()
        store.save_market_revision(run_id, prior)
        self.time.advance(61)
        market["eventStartTime"] = (NOW - timedelta(minutes=4)).isoformat()
        market["endDate"] = (NOW + timedelta(minutes=11)).isoformat()
        conflict = refresh_revision(prior, gamma, public, clock=self.clock, deadline=120)
        self.assertFalse(conflict.capture_ready)
        with self.assertRaises(RecorderStoreError):
            store.save_market_revision(run_id, conflict)
        saved = store._one("SELECT window_start_us,window_end_us,lifecycle FROM markets")
        self.assertEqual((saved["window_start_us"], saved["window_end_us"]),
                         (int(prior.market.window_start.timestamp() * 1e6),
                          int(prior.market.window_end.timestamp() * 1e6)))
        self.assertEqual(saved["lifecycle"], "INVALIDATED")

    def test_refresh_changed_market_id_pauses_capture(self):
        market = make_market()
        gamma, public, _, _, _, _, _ = self.transport(market=market)
        prior = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        store, run_id = self.database()
        store.save_market_revision(run_id, prior)
        self.time.advance(61)
        market["id"] = "1002"
        conflict = refresh_revision(prior, gamma, public, clock=self.clock, deadline=120)
        self.assertFalse(conflict.capture_ready)
        self.assertIn("identity_conflict", conflict.warnings)
        with self.assertRaises(RecorderStoreError):
            store.save_market_revision(run_id, conflict)
        self.assertEqual(store._one("SELECT market_id,lifecycle FROM markets")["lifecycle"],
                         "INVALIDATED")
        self.assertEqual(store._one("SELECT count(*) AS n FROM market_revisions")["n"], 2)

    def test_ambiguous_fallback_rejected(self):
        event = make_event(markets=[make_market(), make_market(id="1002")])
        gamma, public, _, _, _, _, _ = self.transport(event=event)
        with self.assertRaises(MarketDataError) as caught:
            acquire_market(gamma, public, clock=self.clock, deadline=120,
                           use_fast_path=False)
        self.assertEqual(caught.exception.code, "ambiguous_market")

    def test_malformed_clob_token_type_is_invalid_metadata(self):
        config = {"t": [{"t": TOKEN_UP, "o": "Up"}, {"t": None, "o": "Down"}]}
        gamma, public, _, _, _, _, _ = self.transport(config=config)
        with self.assertRaises(MarketDataError) as caught:
            acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        self.assertEqual(caught.exception.code, "invalid_metadata")

    def test_clob_token_mismatch_rejects_acquisition(self):
        config = {"t": [{"t": TOKEN_UP, "o": "Up"}, {"t": "9999", "o": "Down"}]}
        gamma, public, _, _, _, _, _ = self.transport(config=config)
        with self.assertRaises(MarketDataError) as caught:
            acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        self.assertEqual(caught.exception.code, "identity_conflict")

    def test_record_event_persists_scoped_diagnostic(self):
        gamma, public, _, _, _, _, _ = self.transport()
        revision = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        store, run_id = self.database()
        store.save_market_revision(run_id, revision)
        event_id = store.record_event(run_id, market_id=revision.market.market_id,
                                      kind="IDENTITY_CONFLICT", started_us=NOW_US,
                                      ended_us=NOW_US + 12, detail={"code": "identity_conflict"})
        row = store._one("SELECT * FROM recorder_events WHERE event_id=?", (event_id,))
        self.assertEqual((row["run_id"], row["market_id"], row["kind"]),
                         (run_id, "1001", "IDENTITY_CONFLICT"))
        self.assertEqual(json.loads(row["detail_json"]), {"code": "identity_conflict"})
