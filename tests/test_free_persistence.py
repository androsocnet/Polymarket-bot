"""Persistence tests for approximate evidence (Task 6)."""
import unittest
import tempfile
import shutil
from typing import Any
from pathlib import Path
from decimal import Decimal

from polymarket_bot.recorder.store import RecorderStore, RecorderStoreError
from polymarket_bot.recorder.free_reference import (
    ApproximateObservation,
    ApproximateReferenceReport,
)
from polymarket_bot.recorder.models import RunConfig
from tests.recorder_support import DatabaseFixture, NOW_US


class FreePersistenceTests(unittest.TestCase):
    def setUp(self):
        # Create temp directory manually for better Windows cleanup control
        self.temp_dir = Path(tempfile.mkdtemp(prefix="free-persistence-test-"))
        self.db_path = self.temp_dir / "recorder.sqlite3"
        self.store: Any = RecorderStore.open_approximate(self.db_path)
        self.addCleanup(self._cleanup)
        self.run_id = self.store.start_run(
            RunConfig(mode="APPROXIMATE_REFERENCE"),
            wall_us=NOW_US,
            app_revision="test",
            offline_reference=None
        )

    def _cleanup(self):
        """Close store and remove temp directory with retry for Windows locking."""
        if hasattr(self, 'store') and self.store:
            self.store.close()
        if hasattr(self, 'temp_dir') and self.temp_dir.exists():
            import time
            for _ in range(5):
                try:
                    shutil.rmtree(self.temp_dir)
                    break
                except PermissionError:
                    time.sleep(0.1)

    def test_save_approximate_observation(self):
        obs = ApproximateObservation(
            provider='coinbase',
            pair='BTC/USD',
            quote_currency='USD',
            price=Decimal('50000'),
            bid_price=Decimal('49999'),
            ask_price=Decimal('50001'),
            source_timestamp_us=123456789,
            receipt_wall_us=999,
            receipt_server_us=999,
            raw_payload_hash='abc',
            raw_payload_size=100,
            transport_type='rest',
            parser_revision='v1',
            http_status=200,
            result='success',
            error_code=None
        )
        obs_id = self.store.save_approximate_observation(self.run_id, obs)
        assert obs_id > 0
        row = self.store._one("SELECT * FROM approximate_observations WHERE observation_id=?", (obs_id,))
        assert row['provider'] == 'coinbase'
        assert row['price_text'] == '50000'
        assert row['bid_price_text'] == '49999'
        assert row['ask_price_text'] == '50001'
        assert row['source_timestamp_us'] == 123456789
        assert row['receipt_wall_us'] == 999
        assert row['receipt_server_us'] == 999
        assert row['raw_payload_hash'] == 'abc'
        assert row['raw_payload_size'] == 100
        assert row['transport_type'] == 'rest'
        assert row['parser_revision'] == 'v1'
        assert row['http_status'] == 200
        assert row['result'] == 'success'
        assert row['error_code'] is None

    def test_save_approximate_observation_with_none_bid_ask(self):
        obs = ApproximateObservation(
            provider='kraken',
            pair='BTC/USD',
            quote_currency='USD',
            price=Decimal('50000'),
            bid_price=None,
            ask_price=None,
            source_timestamp_us=None,
            receipt_wall_us=1000,
            receipt_server_us=1000,
            raw_payload_hash='def',
            raw_payload_size=200,
            transport_type='rest',
            parser_revision='v1',
            http_status=200,
            result='success',
            error_code=None
        )
        obs_id = self.store.save_approximate_observation(self.run_id, obs)
        assert obs_id > 0
        row = self.store._one("SELECT * FROM approximate_observations WHERE observation_id=?", (obs_id,))
        assert row['provider'] == 'kraken'
        assert row['price_text'] == '50000'
        assert row['bid_price_text'] is None
        assert row['ask_price_text'] is None
        assert row['source_timestamp_us'] is None

    def test_save_approximate_reference(self):
        report = ApproximateReferenceReport(
            reference_kind='APPROXIMATE_SAMPLE_MEAN_60S',
            value=Decimal('50000'),
            window_start_us=0,
            window_end_us=60_000_000,
            observation_count=30,
            source_provider='coinbase',
            was_fallback=False,
            receipt_wall_us=60_000_000,
            receipt_server_us=60_000_000
        )
        ref_id = self.store.save_approximate_reference(self.run_id, report)
        assert ref_id > 0
        row = self.store._one("SELECT * FROM approximate_references WHERE reference_id=?", (ref_id,))
        assert row['mode'] == 'APPROXIMATE'
        assert row['verification_status'] == 'UNVERIFIED'
        assert row['reference_kind'] == 'APPROXIMATE_SAMPLE_MEAN_60S'
        assert row['value_text'] == '50000'
        assert row['window_start_us'] == 0
        assert row['window_end_us'] == 60_000_000
        assert row['observation_count'] == 30
        assert row['source_provider'] == 'coinbase'
        assert row['was_fallback'] == 0
        assert row['receipt_wall_us'] == 60_000_000
        assert row['receipt_server_us'] == 60_000_000
        assert row['diagnostics_json'] == '[]'

    def test_save_approximate_reference_with_none_value(self):
        report = ApproximateReferenceReport(
            reference_kind='APPROXIMATE_SAMPLE_MEAN_60S',
            value=None,
            window_start_us=0,
            window_end_us=60_000_000,
            observation_count=0,
            source_provider='',
            was_fallback=True,
            receipt_wall_us=60_000_000,
            receipt_server_us=60_000_000,
            diagnostics=('MISSING_OBSERVATIONS',)
        )
        ref_id = self.store.save_approximate_reference(self.run_id, report)
        assert ref_id > 0
        row = self.store._one("SELECT * FROM approximate_references WHERE reference_id=?", (ref_id,))
        assert row['value_text'] is None
        assert row['observation_count'] == 0
        assert row['was_fallback'] == 1
        assert row['diagnostics_json'] == '["MISSING_OBSERVATIONS"]'

    def test_v1_database_rejects_approximate_save(self):
        # Create v1 database
        import tempfile
        from pathlib import Path
        temp_dir = Path(tempfile.mkdtemp(prefix="recorder-v1-test-"))
        v1_path = temp_dir / "recorder.sqlite3"

        store_v1 = RecorderStore.open(v1_path)
        store_v1.close()

        # Now try to open v1 for approximate - should fail
        with self.assertRaises(RecorderStoreError) as raised:
            RecorderStore.open_approximate(v1_path)
        self.assertEqual(raised.exception.code, "schema_version_mismatch")

        # Cleanup
        import shutil
        for _ in range(5):
            try:
                shutil.rmtree(temp_dir)
                break
            except PermissionError:
                import time
                time.sleep(0.1)

    def test_approximate_mode_start_run(self):
        # Verify APPROXIMATE_REFERENCE mode works with v2 database
        run_id = self.store.start_run(
            RunConfig(mode="APPROXIMATE_REFERENCE"),
            wall_us=NOW_US + 1000,
            app_revision="test2",
            offline_reference=None
        )
        assert run_id is not None
        run = self.store._one("SELECT * FROM recording_runs WHERE run_id=?", (run_id,))
        assert run['mode'] == 'APPROXIMATE_REFERENCE'
        assert run['reference_activation'] == 'DISABLED'
        assert run['state'] == 'RUNNING'

    def test_save_approximate_observation_requires_writer(self):
        # Read-only store should reject writes
        read_only_store = RecorderStore.read_only(self.db_path)
        self.addCleanup(read_only_store.close)

        obs = ApproximateObservation(
            provider='coinbase',
            pair='BTC/USD',
            quote_currency='USD',
            price=Decimal('50000'),
            bid_price=Decimal('49999'),
            ask_price=Decimal('50001'),
            source_timestamp_us=123456789,
            receipt_wall_us=999,
            receipt_server_us=999,
            raw_payload_hash='abc',
            raw_payload_size=100,
            transport_type='rest',
            parser_revision='v1',
            http_status=200,
            result='success',
            error_code=None
        )
        with self.assertRaises(RecorderStoreError) as raised:
            read_only_store.save_approximate_observation(self.run_id, obs)
        self.assertEqual(raised.exception.code, "read_only")

    def test_save_approximate_reference_requires_writer(self):
        read_only_store = RecorderStore.read_only(self.db_path)
        self.addCleanup(read_only_store.close)

        report = ApproximateReferenceReport(
            reference_kind='APPROXIMATE_SAMPLE_MEAN_60S',
            value=Decimal('50000'),
            window_start_us=0,
            window_end_us=60_000_000,
            observation_count=30,
            source_provider='coinbase',
            was_fallback=False,
            receipt_wall_us=60_000_000,
            receipt_server_us=60_000_000
        )
        with self.assertRaises(RecorderStoreError) as raised:
            read_only_store.save_approximate_reference(self.run_id, report)
        self.assertEqual(raised.exception.code, "read_only")

    def test_save_approximate_observation_unique_constraint(self):
        obs = ApproximateObservation(
            provider='coinbase',
            pair='BTC/USD',
            quote_currency='USD',
            price=Decimal('50000'),
            bid_price=Decimal('49999'),
            ask_price=Decimal('50001'),
            source_timestamp_us=123456789,
            receipt_wall_us=999,
            receipt_server_us=999,
            raw_payload_hash='abc',
            raw_payload_size=100,
            transport_type='rest',
            parser_revision='v1',
            http_status=200,
            result='success',
            error_code=None
        )
        # First save succeeds
        obs_id1 = self.store.save_approximate_observation(self.run_id, obs)
        assert obs_id1 > 0

        obs_id2 = self.store.save_approximate_observation(self.run_id, obs)
        self.assertNotEqual(obs_id1, obs_id2)
        self.assertEqual(len(self.store._rows("SELECT * FROM approximate_observations")), 2)


class ResearchPersistenceTests(FreePersistenceTests):
    def test_all_book_attempts_are_durable(self):
        from types import SimpleNamespace
        from polymarket_bot.market_data.http import HttpAttempt
        import json
        self.test_revision_two_recorder_flow()
        attempt = HttpAttempt('BOOK_SINGLE', 'GET', '/book', ('token_id=11',), 1,
            11_000_000_000, 11_100_000_000, NOW_US, NOW_US, 200, 'success', None, b'{}', True)
        self.assertTrue(hasattr(self.recorder, '_persist_book_attempts'), 'book attempts missing')
        self.recorder._persist_book_attempts(SimpleNamespace(attempts=(attempt, attempt)),
            market_id='market', slot_id=1, slot_index=0, target_ns=11_000_000_000,
            up_token='11', down_token='12')
        rows = [json.loads(row['detail_json']) for row in self.store._rows('SELECT detail_json FROM recorder_events')]
        sends = [row for row in rows if row.get('type') == 'BOOK_SEND_ATTEMPT']
        self.assertEqual(len(sends), 2)
        self.assertEqual([row['leg'] for row in sends], ['UP', 'UP'])
        self.assertEqual(sends[0]['started_mono_ns'], 11_000_000_000)

    def test_scheduler_dispositions_and_session_origin_are_durable(self):
        import json
        self.test_revision_two_recorder_flow()
        recorder = self.recorder
        for disposition in ('skipped', 'busy', 'unsubmitted'):
            recorder._research_opportunity(12_000_000_000, disposition, market_id='scheduled', slot_index=6)
        events = [json.loads(row['detail_json']) for row in self.store._rows('SELECT detail_json FROM recorder_events')]
        opportunities = [row for row in events if row.get('type') == 'APPROXIMATE_OPPORTUNITY']
        self.assertEqual([row['disposition'] for row in opportunities[-3:]], ['skipped', 'busy', 'unsubmitted'])
        self.assertTrue(any(row.get('type') == 'APPROXIMATE_SESSION' for row in events), 'session origin absent before fetch')

    def test_v2_membership_rejects_duplicates_and_cross_run_ids(self):
        from polymarket_bot.recorder.service import Recorder
        from dataclasses import replace
        self.test_revision_two_recorder_flow()
        from typing import cast
        from polymarket_bot.recorder.free_reference import ResearchFetchResult
        recorder, result = self.recorder, cast(ResearchFetchResult, self.research_result)
        for evidence in (replace(result.evidence, observation_ids=(1, 1)),
                replace(result.evidence, run_id='foreign'),
                replace(result.evidence, sampling_origin_ns=1),
                replace(result.evidence, contexts=tuple(replace(c, run_id='foreign') for c in result.evidence.contexts)),
                replace(result.evidence, contexts=tuple(replace(c, anchor=replace(c.anchor, uncertainty_us=2_000_000)) for c in result.evidence.contexts))):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                recorder._validate_research_result(replace(result, evidence=evidence))

    def test_partial_fetch_persistence_fails_closed(self):
        self.test_revision_two_recorder_flow()
        import json
        rows = self.store._rows('SELECT detail_json FROM recorder_events')
        fetch = next(json.loads(row['detail_json']) for row in rows if json.loads(row['detail_json']).get('type') == 'APPROXIMATE_FETCH')
        self.assertEqual(fetch.get('persistence_status'), 'COMPLETE', 'missing durable completion barrier')
        from unittest.mock import patch
        recorder = self.recorder
        original_count = len(rows)
        recorder._research_mapping = {}
        with patch.object(self.store, 'save_approximate_reference', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                recorder._persist_research(self.research_result)
        after = self.store._rows('SELECT detail_json FROM recorder_events')
        self.assertEqual(len(after), original_count)
        self.assertEqual(recorder._research_mapping, {})

    def test_multi_fetch_history_is_bounded_and_ids_are_durable(self):
        self.test_revision_two_recorder_flow(cycles=40)
        evidence = self.recorder._research_evidence
        self.assertLessEqual(len(evidence.observation_ids), 64)
        session = self.recorder.approximate_reference
        assert session is not None
        self.assertEqual(len(evidence.observation_ids), len(session._research_observations))
        self.assertEqual(len(self.store._rows('SELECT * FROM approximate_references')), 40)
        self.assertEqual(set(evidence.observation_ids), set(self.recorder._research_mapping))

    def test_delayed_discovery_cannot_attest_scheduler_completeness(self):
        self.test_revision_two_recorder_flow(delay_ns=30_000_000_000)
        result = self.research_result
        assert result is not None
        self.assertIsNone(result.evidence.validated_window_start_ns)
        self.assertIsNone(result.evidence.validated_window_end_ns)
        self.assertIn('INCOMPLETE_PROVENANCE', result.selected_assessment.reasons)

    def test_captured_anchor_and_persisted_origin_are_bound(self):
        from dataclasses import replace
        self.test_revision_two_recorder_flow()
        from typing import cast
        from polymarket_bot.recorder.free_reference import ResearchFetchResult
        result = cast(ResearchFetchResult, self.research_result)
        for field, value in (('server_us', result.context.anchor.server_us + 1),
                ('midpoint_ns', 1), ('request_end_mono_ns', 1), ('status', 'DISCONTINUITY')):
            context = replace(result.context, anchor=replace(result.context.anchor, **{field: value}))
            altered = replace(result, context=context, evidence=replace(result.evidence,
                contexts=tuple(context for _ in result.evidence.contexts)))
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.recorder._validate_research_result(altered)
        context = replace(result.context, sampling_origin_ns=1)
        altered = replace(result, context=context, evidence=replace(result.evidence,
            sampling_origin_ns=1, contexts=tuple(context for _ in result.evidence.contexts)))
        with self.assertRaises(ValueError):
            self.recorder._validate_research_result(altered)

    def test_inflight_discontinuity_survives_receipt_and_drain(self):
        self.test_revision_two_recorder_flow(inflight=True)
        result = self.research_result
        assert result is not None
        self.assertIn(10_100_000_000, result.evidence.discontinuity_ends_ns)
        self.assertIn('CLOCK_DISCONTINUITY', result.selected_assessment.reasons)
        self.assertIn(10_100_000_000, self.recorder._research_evidence.discontinuity_ends_ns)
        self.assertIn(result.report.receipt_mono_ns + 1, self.recorder._research_evidence.discontinuity_ends_ns)

    def test_late_discontinuity_warmup_fallback_replays_exactly(self):
        self.check_late_discontinuity_selection(cycles=1, provider='kraken', warming='WARMING_UP')

    def test_late_discontinuity_disqualifies_postwarmup_fallback(self):
        self.check_late_discontinuity_selection(cycles=40, provider='coinbase', warming='FULL')

    def check_late_discontinuity_selection(self, *, cycles, provider, warming):
        import json
        from dataclasses import replace
        from unittest.mock import patch
        from tests.test_cp5_safety import CP5SafetyTests
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.free_reference import CoinbaseRestProvider
        original_fetch = CoinbaseRestProvider.fetch_ticker
        original_persist = Recorder._persist_research
        seen = []
        def fetch(instance, **kwargs):
            observation = original_fetch(instance, **kwargs)
            return (replace(observation, price=None, bid_price=None,
                ask_price=None, result='error', error_code='HTTP_ERROR', source_timestamp_us=None)
                if instance.provider_name == 'coinbase' else observation)
        def persist(recorder, result):
            seen.append(result)
            if len(seen) == cycles:
                self.assertEqual(result.selected_assessment.selected_provider, 'kraken')
                end = result.report.receipt_mono_ns - 1
                recorder._research_evidence = replace(recorder._research_evidence,
                    discontinuity_ends_ns=(end,))
                recorder.store.record_event(recorder.run_id, 'CLOCK_PAUSE',
                    started_us=result.report.receipt_wall_us, detail=dict(
                        type='APPROXIMATE_DISCONTINUITY', version=2, run_id=recorder.run_id,
                        anchor_id=recorder.anchor_id, calibration_end_ns=end))
            return original_persist(recorder, result)
        with patch.object(CoinbaseRestProvider, 'fetch_ticker', fetch), patch.object(Recorder, '_persist_research', persist):
            self.test_revision_two_recorder_flow(cycles=cycles)
        rows = tuple([dict(row) for row in self.store._rows('SELECT * FROM ' + table)]
            for table in ('approximate_observations', 'approximate_references', 'recorder_events', 'clock_anchors'))
        replay = CP5SafetyTests.analyzer().replay_rows(*rows)
        self.assertTrue(replay['exact'])
        final = replay['reports'][-1]
        self.assertEqual(final['warmup_status'], warming)
        self.assertEqual(final['selected_assessment']['selected_provider'], provider)
        self.assertIn('CLOCK_DISCONTINUITY', final['selected_assessment']['reasons'])
        detail = next(json.loads(e['detail_json']) for e in reversed(rows[2])
            if json.loads(e['detail_json']).get('type') == 'APPROXIMATE_FETCH')
        self.assertEqual(detail['receipt_mono_ns'], seen[-1].report.receipt_mono_ns)
        self.assertGreaterEqual(detail['completion_ns'], seen[-1].completion_ns)
        self.assertEqual(rows[1][-1]['source_provider'], provider)
        if provider == 'kraken':
            self.assertIn('PRIMARY_INSUFFICIENT', final['selected_assessment']['reasons'])
        else:
            self.assertEqual(final['selected_assessment']['coverage_quality'], 'INSUFFICIENT')

    def test_revision_two_recorder_flow(self, cycles=1, delay_ns=0, inflight=False):
        import json
        import httpx
        from polymarket_bot.market_data.http import PublicGet
        from polymarket_bot.recorder.service import Recorder, ApproximateReferenceWorker
        from polymarket_bot.recorder.free_reference import ApproximateReferenceSession, CoinbaseRestProvider, KrakenRestProvider
        from polymarket_bot.recorder.models import ClockAnchor
        mono = [10_000_000_000]
        calls = {}
        wall = lambda: NOW_US + mono[0] // 1000
        def respond(request):
            mono[0] += 100_000_000
            if inflight and mono[0] == 10_100_000_000:
                from dataclasses import replace
                recorder._research_evidence = replace(recorder._research_evidence, discontinuity_ends_ns=(mono[0],))
            calls[request.url.host] = calls.get(request.url.host, 0) + 1
            if calls[request.url.host] == 1:
                return httpx.Response(429, json={})
            if request.url.host == 'api.kraken.com':
                return httpx.Response(200, json={'error': [], 'result': {'BTC/USD': {'c': ['100'], 'b': ['99'], 'a': ['101']}}})
            return httpx.Response(200, json={'price': '100', 'bid': '99', 'ask': '101', 'time': '2026-01-01T00:00:00Z'})
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            providers = {name: cls(client, monotonic_ns=lambda: mono[0], wall_us=wall, sleep=lambda _: None)
                for name, cls in (('coinbase', CoinbaseRestProvider), ('kraken', KrakenRestProvider))}
            session = ApproximateReferenceSession(providers=providers, monotonic_ns=lambda: mono[0], wall_us=wall, sleep=lambda _: None)
            session.start()
            recorder = Recorder(self.store, PublicGet(client, monotonic=lambda: mono[0] / 1e9, sleep=lambda _: None, utcnow=lambda: __import__('datetime').datetime.now(__import__('datetime').timezone.utc)), monotonic_ns=lambda: mono[0], wall_us=wall, sleep=lambda _: None, approximate_reference=session)
            recorder.run_id = self.run_id
            recorder.run_deadline_ns = 100_000_000_000
            recorder.approximate_worker = ApproximateReferenceWorker()
            recorder.clock.anchor = ClockAnchor(NOW_US // 1_000_000, 0, 0, NOW_US, NOW_US, 0, NOW_US, 1_000_000, 'VALID')
            recorder.anchor_id = self.store.save_anchor(self.run_id, recorder.clock.anchor)
            mono[0] += delay_ns
            self.assertTrue(recorder._approximate_submit(mono[0] + 2_000_000_000))
            recorder.approximate_worker.stop()
            self.recorder = recorder
            self.research_result = recorder.approximate_worker.take()
            if inflight:
                from dataclasses import replace
                assert self.research_result is not None
                recorder._research_evidence = replace(recorder._research_evidence,
                    discontinuity_ends_ns=recorder._research_evidence.discontinuity_ends_ns + (self.research_result.report.receipt_mono_ns + 1,))
            mono[0] += 1_000_000_000
            recorder._approximate_drain(result=self.research_result)
            for index in range(1, cycles):
                from polymarket_bot.recorder.service import ApproximateReferenceWorker
                mono[0] = 10_000_000_000 + index * 2_000_000_000
                recorder.approximate_worker = ApproximateReferenceWorker()
                self.assertTrue(recorder._approximate_submit(mono[0] + 2_000_000_000))
                recorder.approximate_worker.stop()
                recorder._approximate_drain()
        events = [json.loads(row['detail_json']) for row in self.store._rows('SELECT * FROM recorder_events')]
        fetch = next(row for row in events if row.get('type') == 'APPROXIMATE_FETCH')
        self.assertEqual(fetch['version'], 2)
        self.assertEqual(fetch['run_id'], self.run_id)
        self.assertEqual(fetch['sampling_origin_ns'], 10_000_000_000)
        self.assertLessEqual(fetch['completion_ns'], mono[0])
        self.assertGreaterEqual(fetch['completion_ns'], fetch['receipt_mono_ns'])
        self.assertEqual(len(fetch['observation_ids']), 2)
        self.assertEqual(len(fetch['request_attempts']), 4)
        self.assertEqual([row['attempt'] for row in fetch['request_attempts']], [1, 2, 1, 2])
        self.assertTrue(all(row['started_mono_ns'] <= row['ended_mono_ns'] for row in fetch['request_attempts']))
        self.assertEqual(fetch['selected_assessment']['coverage_quality'], 'INSUFFICIENT')
        self.assertTrue(any(row.get('type') == 'APPROXIMATE_OPPORTUNITY' for row in events))


if __name__ == "__main__":
    unittest.main()