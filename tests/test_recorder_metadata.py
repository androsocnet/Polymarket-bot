"""Recorder metadata uses strict discovery and preserves source evidence."""
import json
import unittest
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast

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

    def test_successor_failure_reasons(self):
        from polymarket_bot.recorder.metadata import acquire_next_market
        gamma, public, *_ = self.transport()
        previous = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=False)
        start = previous.market.window_end
        future = make_market(id='1002', conditionId='0x'+'cd'*32, clobTokenIds='["3","4"]',
            eventStartTime=start.isoformat(), endDate=(start+timedelta(seconds=900)).isoformat())
        ambiguous = [make_event(id='2002', markets=[future]), make_event(id='2003', markets=[dict(
            future, id='1003', conditionId='0x'+'ef'*32, clobTokenIds='["5","6"]')])]
        cases = [(json.dumps(ambiguous).encode(), 'AMBIGUOUS_SUCCESSOR'), (b'[{', 'JSON_DECODE_ERROR'), (b'[{"x":1e999999999999999999}]', 'NUMERIC_PARSE_ERROR'),
            (b'{}', 'EVENT_SCHEMA_INVALID'), (b'[]', 'NO_VALID_SUCCESSOR'),
            (b' '* (10*1024*1024+1), 'RESPONSE_TOO_LARGE')]
        for body, reason in cases:
            with self.subTest(reason=reason):
                attempts = []
                with httpx.Client(transport=httpx.MockTransport(lambda request:
                        httpx.Response(200, stream=httpx.ByteStream(body))), trust_env=False) as client:
                    public = PublicGet(client, monotonic=self.time.monotonic, sleep=self.time.sleep,
                        utcnow=self.time.utcnow, max_attempts=1, on_attempt=attempts.append)
                    with self.assertRaises(MarketDataError) as caught:
                        acquire_next_market(previous, GammaClient(public), public, clock=self.clock, deadline=120)
                    self.assertEqual(getattr(caught.exception, 'reason', None), reason)
                    self.assertEqual(len(attempts), 1)
                    self.assertEqual(public.on_attempt, attempts.append)

    def test_large_successor_list_keeps_bounded_provenance(self):
        import hashlib
        from polymarket_bot.recorder.metadata import acquire_next_market
        gamma, public, *_ = self.transport()
        previous = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=False)
        previous = replace(previous, market=replace(previous.market, condition_id='0x'+'ef'*32))
        start = previous.market.window_end
        future = make_market(id='1002', clobTokenIds='["3","4"]', eventStartTime=start.isoformat(),
            endDate=(start+timedelta(seconds=900)).isoformat())
        event = make_event(id='2002', markets=[future])
        compact = json.dumps([event]).encode()
        body = compact+b' '*(1_689_540-len(compact))
        attempts = []
        def respond(request):
            raw = body if request.url.path == '/events' else json.dumps(future).encode() if request.url.path == '/markets/1002' else b'{"t":[{"t":"3"},{"t":"4"}]}'
            return httpx.Response(200, stream=httpx.ByteStream(raw))
        with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
            public = PublicGet(client, monotonic=self.time.monotonic, sleep=self.time.sleep,
                utcnow=self.time.utcnow, max_attempts=1, on_attempt=attempts.append)
            revision = acquire_next_market(previous, GammaClient(public), public, clock=self.clock, deadline=120)
        self.assertTrue(revision.capture_ready)
        self.assertEqual(cast(Any, revision.market.metadata['prepared_list_evidence'])[0]['sha256'], hashlib.sha256(body).hexdigest())
        self.assertEqual(cast(Any, revision.market.metadata['prepared_list_evidence'])[0]['bytes'], len(body))
        self.assertEqual(attempts[0].raw_body, body)
        self.assertLess(len(json.dumps(revision.market.metadata, default=str)), 1_048_576)

    def test_successor_evidence_exact_boundary(self):
        from polymarket_bot.recorder.metadata import acquire_next_market
        gamma, public, *_ = self.transport()
        previous = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=False)
        previous = replace(previous, market=replace(previous.market, condition_id='0x'+'ef'*32))
        start = previous.market.window_end
        future = make_market(id='1002', clobTokenIds='["3","4"]', eventStartTime=start.isoformat(),
            endDate=(start+timedelta(seconds=900)).isoformat())
        event = make_event(id='2002', markets=[future], evidence='')
        gamma, public, *_ = self.transport(market=future, event=event, config={'t':[{'t':'3'},{'t':'4'}]})
        revision = acquire_next_market(previous, gamma, public, clock=self.clock, deadline=120)
        size = len(json.dumps(dict(event=event, list_evidence=revision.market.metadata['prepared_list_evidence']), default=str).encode())
        padding = 1_048_576-size-4
        for delta in (-1, 0, 1):
            with self.subTest(delta=delta):
                event['evidence'] = 'x'*(padding+delta)
                gamma, public, *_ = self.transport(market=future, event=event, config={'t':[{'t':'3'},{'t':'4'}]})
                if delta <= 0:
                    candidate = acquire_next_market(previous, gamma, public, clock=self.clock, deadline=120)
                    actual = len(json.dumps(dict(event=event, list_evidence=candidate.market.metadata['prepared_list_evidence']), default=str).encode())
                    self.assertEqual(actual, 1_048_576+delta)
                else:
                    with self.assertRaises(MarketDataError) as caught:
                        acquire_next_market(previous, gamma, public, clock=self.clock, deadline=120)
                    self.assertEqual(caught.exception.reason, 'EVIDENCE_BOUND_EXCEEDED')

    def test_prepared_event_evidence_bounded_before_arming(self):
        from polymarket_bot.recorder.metadata import acquire_next_market
        gamma, public, _, _, _, _, _ = self.transport()
        previous = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=False)
        previous = replace(previous, market=replace(previous.market, condition_id='0x'+'ef'*32))
        start = previous.market.window_end
        future = make_market(id='1002', clobTokenIds='["3","4"]', eventStartTime=start.isoformat(),
            endDate=(start+timedelta(seconds=900)).isoformat())
        gamma, public, _, _, _, _, _ = self.transport(market=future,
            event=make_event(id='2002', markets=[future], evidence='x'*1_048_576),
            config={'t':[{'t':'3'},{'t':'4'}]})
        with self.assertRaises(MarketDataError) as caught:
            acquire_next_market(previous, gamma, public, clock=self.clock, deadline=120)
        self.assertEqual(caught.exception.reason, 'EVIDENCE_BOUND_EXCEEDED')

    def test_prepared_identity_change_blocks_activation(self):
        from polymarket_bot.recorder import metadata
        self.assertTrue(callable(getattr(metadata, 'acquire_next_market', None)))
        gamma, public, _, _, _, _, _ = self.transport()
        previous = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=False)
        original = previous
        start = previous.market.window_end
        market = make_market(id='1002', clobTokenIds='["3","4"]', outcomes='["Down","Up"]',
            eventStartTime=start.isoformat(), endDate=(start+timedelta(seconds=900)).isoformat())
        previous = replace(previous, market=replace(previous.market, condition_id='0x'+'ef'*32))
        config = {'t':[{'t':'3','o':'Down'},{'t':'4','o':'Up'}]}
        gamma, public, paths, _, raw_event, raw_market, raw_config = self.transport(
            market=market, event=make_event(id='2002', markets=[market]), config=config)
        prepared = metadata.acquire_next_market(previous, gamma, public, clock=self.clock, deadline=120)
        self.assertEqual((prepared.up_index, prepared.down_index), (1, 0))
        self.assertEqual((prepared.gamma_raw, prepared.clob_config_raw), (raw_market, raw_config))
        import hashlib
        self.assertEqual(cast(Any, prepared.market.metadata['prepared_list_evidence'])[0]['sha256'],
                         hashlib.sha256(('['+raw_event.decode()+']').encode()).hexdigest())
        self.assertEqual(cast(Any, prepared.market.metadata['prepared_event'])['id'], '2002')
        self.assertFalse(any('/slug/' in path for path in paths))
        refreshed = metadata.revalidate_prepared(previous, prepared, gamma, public, clock=self.clock, deadline=120)
        self.assertGreater(refreshed.observed_us, prepared.observed_us)
        market['clobTokenIds'] = '["5","6"]'
        with self.assertRaises(MarketDataError):
            metadata.revalidate_prepared(previous, prepared, gamma, public, clock=self.clock, deadline=120)
        self.assertEqual(original.market.market_id, '1001')
        market['clobTokenIds'] = '["3","4"]'
        for bad in ({'mts':'0.01'}, {'t':None}, {'t':[{'t':'3','o':'Up'},{'t':'4','o':'Down'}]}):
            config.clear()
            cast(Any, config).update(bad)
            with self.assertRaises(MarketDataError):
                metadata.acquire_next_market(previous, gamma, public, clock=self.clock, deadline=120)

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

    def test_invalid_known_clob_numbers_pause_and_preserve_raw_diagnostics(self):
        config = {"mts": "NaN", "mos": {"bad": "type"}, "mbf": "1e999999999999999",
                  "tbf": True, "fd": {"r": "Infinity", "vendor": "opaque"}}
        gamma, public, _, _, _, _, raw_config = self.transport(config=config)
        revision = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        self.assertFalse(revision.capture_ready)
        self.assertTrue(all(any("invalid_clob_numeric:"+field in w for w in revision.warnings)
                            for field in ("mts", "mos", "mbf", "tbf", "fd.r")))
        store, run_id = self.database()
        store.save_market_revision(run_id, revision)
        saved = store._one("SELECT * FROM market_revisions")
        self.assertIsNone(saved["tick_size_text"])
        self.assertIsNone(saved["min_order_size_text"])
        self.assertEqual(json.loads(saved["fee_config_json"]), {"fd": {"vendor": "opaque"}})
        self.assertEqual(decode_evidence(saved["clob_config_raw_z"],
            json.loads(saved["raw_sizes_json"])["clob"], 1), raw_config)
        self.assertEqual(store._one("SELECT lifecycle FROM markets")["lifecycle"], "CURRENT")
        diagnostic = store._one("SELECT detail_json FROM recorder_events WHERE kind='DISCOVERY_GAP'")
        self.assertIn("invalid_clob_numeric:mos", diagnostic["detail_json"])

    def test_known_clob_numbers_store_exact_canonical_text(self):
        config = {"mts": "1e-2", "mos": "1.000000000000000000000000000000001",
                  "mbf": "0.000", "tbf": "2e1", "fd": {"r": "2e-2", "e": 3}}
        gamma, public, _, _, _, _, _ = self.transport(config=config)
        revision = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        self.assertTrue(revision.capture_ready)
        store, run_id = self.database()
        store.save_market_revision(run_id, revision)
        saved = store._one("SELECT * FROM market_revisions")
        self.assertEqual((saved["tick_size_text"], saved["min_order_size_text"]),
                         ("0.01", "1.000000000000000000000000000000001"))
        self.assertEqual(json.loads(saved["fee_config_json"]),
                         {"mbf": "0.000", "tbf": "20", "fd": {"r": "0.02", "e": 3}})

    def test_bounded_zero_sizes_and_signed_fees_do_not_invent_new_domain_rules(self):
        config = {"mts": "0.000", "mos": 0, "mbf": "-1e-2", "tbf": 0, "fd": {"r": "-2e-2"}}
        gamma, public, _, _, _, _, _ = self.transport(config=config)
        revision = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        self.assertTrue(revision.capture_ready)
        store, run_id = self.database()
        store.save_market_revision(run_id, revision)
        saved = store._one("SELECT * FROM market_revisions")
        self.assertEqual((saved["tick_size_text"],saved["min_order_size_text"]),("0.000","0"))
        self.assertEqual(json.loads(saved["fee_config_json"]),{"mbf":"-0.01","tbf":"0","fd":{"r":"-0.02"}})

    def test_discovery_gap_closure_preserves_start_and_rejects_contradictions(self):
        store, run_id = self.database()
        gap = store.record_event(run_id,"DISCOVERY_GAP",started_us=NOW_US,
            detail={"acquisition_gap":True,"time_domain":"SERVER"})
        for event_id, owner, end in ((gap+1,run_id,NOW_US+1),(gap,"unknown",NOW_US+1),(gap,run_id,NOW_US-1)):
            with self.assertRaises(RecorderStoreError):
                store.finish_discovery_gap(event_id,run_id=owner,ended_us=end)
        self.assertIsNone(store._one("SELECT ended_us FROM recorder_events WHERE event_id=?",(gap,))["ended_us"])
        store.finish_discovery_gap(gap,run_id=run_id,ended_us=NOW_US+20_000)
        with self.assertRaises(RecorderStoreError):
            store.finish_discovery_gap(gap,run_id=run_id,ended_us=NOW_US+30_000)
        saved = store._one("SELECT started_us,ended_us FROM recorder_events WHERE event_id=?",(gap,))
        self.assertEqual((saved["started_us"],saved["ended_us"]),(NOW_US,NOW_US+20_000))

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

    def test_changed_content_returning_to_prior_value_appends_revision(self):
        first_market = make_market(description="A")
        gamma_a, public_a, _, _, _, _, _ = self.transport(market=first_market)
        first = acquire_market(gamma_a, public_a, clock=self.clock, deadline=120, use_fast_path=True)
        second_market = make_market(description="B")
        gamma_b, public_b, _, _, _, _, _ = self.transport(market=second_market)
        second = acquire_market(gamma_b, public_b, clock=self.clock, deadline=120, use_fast_path=True)
        store, run_id = self.database()
        first_id = store.save_market_revision(run_id, first)
        second_id = store.save_market_revision(run_id, second)
        returned = replace(first, observed_us=second.observed_us + 1)
        third_id = store.save_market_revision(run_id, returned)
        self.assertEqual(len({first_id, second_id, third_id}), 3)
        rows = store._rows("SELECT revision_id,observed_us,description,content_hash FROM market_revisions ORDER BY revision_id")
        self.assertEqual([row["description"] for row in rows], ["A", "B", "A"])
        self.assertEqual(rows[2]["observed_us"], returned.observed_us)
        self.assertEqual(rows[2]["content_hash"], rows[0]["content_hash"])

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

    def test_clob_identity_conflict_saves_new_response_as_diagnostic(self):
        config = {"t": [{"t": TOKEN_UP, "o": "Up"}, {"t": TOKEN_DOWN, "o": "Down"}]}
        gamma, public, _, _, _, _, _ = self.transport(config=config)
        prior = acquire_market(gamma, public, clock=self.clock, deadline=120, use_fast_path=True)
        store, run_id = self.database()
        store.save_market_revision(run_id, prior)
        self.time.advance(61)
        config["t"][1]["t"] = "9999"
        conflicting_raw = json.dumps(config, separators=(",", ": ")).encode()
        conflict = refresh_revision(prior, gamma, public, clock=self.clock, deadline=120)
        self.assertFalse(conflict.capture_ready)
        self.assertIn("identity_conflict", conflict.warnings)
        self.assertEqual(conflict.clob_config_raw, conflicting_raw)
        with self.assertRaises(RecorderStoreError):
            store.save_market_revision(run_id, conflict)
        row = store._one("SELECT clob_config_raw_z,raw_sizes_json FROM market_revisions ORDER BY revision_id DESC LIMIT 1")
        self.assertEqual(decode_evidence(row["clob_config_raw_z"],
                                         json.loads(row["raw_sizes_json"])["clob"], 1), conflicting_raw)
        self.assertEqual(store._one("SELECT condition_id,lifecycle FROM markets")["condition_id"], CONDITION)
        self.assertEqual(store._one("SELECT lifecycle FROM markets")["lifecycle"], "INVALIDATED")

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
