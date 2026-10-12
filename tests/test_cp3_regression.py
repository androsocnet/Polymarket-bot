"""Offline CP3 durability regressions with timely, calibrated evidence."""
import json
import unittest

from tests.recorder_support import NOW_US
from tests.support import MARKET_ID
from tests import test_recorder_metrics as metrics_tests


class CP3RegressionTests(unittest.TestCase):
    fixture = metrics_tests.RecorderMetricsTests.fixture
    slot = metrics_tests.RecorderMetricsTests.slot
    reference = metrics_tests.RecorderMetricsTests.reference
    request = metrics_tests.RecorderMetricsTests.request
    metrics = metrics_tests.RecorderMetricsTests.metrics

    def managed(self, store, run_id):
        with store._connection:
            store._connection.execute("""UPDATE recording_runs SET mode='REFERENCE_REQUIRED',
                reference_activation='VERIFIED',activation_evidence_json=? WHERE run_id=?""",
                (json.dumps({'scope': 'MANAGED_PRODUCTION'}), run_id))

    def evidence(self, store, run_id, *, role='SLOT_CURRENT', policy='managed-signed-inclusive-seconds-v1'):
        slot = self.slot(store, run_id, 150, 'COMPLETE') if role == 'SLOT_CURRENT' else None
        fetch, _ = self.reference(store, run_id, slot, role=role,
            learned=NOW_US+600_000_000 if role == 'CLOSING' else NOW_US,
            origin='LIVE_LATEST' if slot is not None else 'HISTORICAL_LOOKUP', verified='UNVERIFIED')
        with store._connection:
            store._connection.execute('UPDATE reference_associations SET selection_policy_revision=? WHERE fetch_id=?',
                (policy, fetch))
        row = store._one('SELECT a.*,r.report_hash FROM reference_associations a JOIN reference_reports r USING(report_id) WHERE a.fetch_id=?', (fetch,))
        assert row is not None
        identity = {name: row[name] for name in ('market_id', 'role', 'slot_id', 'target_us',
            'learned_wall_us', 'report_hash')}
        row['commit_event_id'] = store.record_event(run_id, 'DISCOVERY_GAP', started_us=row['learned_wall_us'],
            detail=dict(identity, reason='REFERENCE_COMMIT', available_live=bool(row['available_live'])))
        return row

    def proof(self, row) -> dict:
        return dict(reason='REFERENCE_DURABLE', durability_basis='POST_COMMIT_BARRIER_V1',
            commit_event_id=row['commit_event_id'], market_id=row['market_id'], role=row['role'],
            slot_id=row['slot_id'], target_us=row['target_us'], learned_wall_us=row['learned_wall_us'],
            report_hash=row['report_hash'], available_live=bool(row['available_live']),
            durable_server_us=row['learned_server_us']+1_000_000)

    def count(self, store, role='SLOT_CURRENT', *, end=NOW_US+4_000_000):
        coverage = self.metrics().measure(store, start_us=NOW_US, end_us=end).reference_coverage
        return coverage[{'SLOT_CURRENT': 'live_slots', 'OPENING': 'opening_count', 'CLOSING': 'closing_count'}[role]]

    def test_managed_boundaries_require_post_commit_proof(self):
        for role in ('OPENING', 'CLOSING'):
            with self.subTest(role=role):
                store, run_id = self.fixture()
                self.managed(store, run_id)
                row = self.evidence(store, run_id, role=role)
                end = row['learned_wall_us']+4_000_000
                self.assertEqual(self.count(store, role, end=end), 0)
                store.record_event(run_id, 'DISCOVERY_GAP', started_us=row['learned_wall_us']+1_000_000, detail=self.proof(row))
                self.assertEqual(self.count(store, role, end=end), 1)

    def test_slot_proof_requires_exact_identity_and_timely_learning(self):
        changes = dict(market_id='another-market', role='OPENING', slot_id=999,
            target_us=NOW_US+2_000_000, learned_wall_us=NOW_US+1,
            report_hash='different-signed-report', available_live=False,
            durable_server_us=NOW_US-1)
        for field, wrong in changes.items():
            with self.subTest(field=field):
                store, run_id = self.fixture()
                self.managed(store, run_id)
                row = self.evidence(store, run_id)
                proof = self.proof(row)
                proof[field] = wrong
                store.record_event(run_id, 'DISCOVERY_GAP', started_us=NOW_US+1_000_000, detail=proof)
                self.assertEqual(self.count(store), 0)

    def test_exact_timely_slot_proof_counts(self):
        store, run_id = self.fixture()
        self.managed(store, run_id)
        row = self.evidence(store, run_id)
        store.record_event(run_id, 'DISCOVERY_GAP', started_us=NOW_US+1_000_000, detail=self.proof(row))
        self.assertEqual(self.count(store), 1)

    def test_untagged_legacy_association_not_new_managed_evidence(self):
        store, run_id = self.fixture()
        self.managed(store, run_id)
        row = self.evidence(store, run_id, policy='signed-inclusive-seconds-v1')
        store.record_event(run_id, 'DISCOVERY_GAP', started_us=NOW_US+1_000_000, detail=self.proof(row))
        self.assertEqual(self.count(store), 0)

    def test_proof_from_other_run_cannot_validate_slot(self):
        from polymarket_bot.recorder.models import RunConfig
        store, run_id = self.fixture()
        self.managed(store, run_id)
        row = self.evidence(store, run_id)
        other = store.start_run(RunConfig(), wall_us=NOW_US, app_revision='unrelated')
        with store._connection:
            store._connection.execute("""INSERT INTO clock_anchors
                (run_id,server_seconds,request_start_mono_ns,request_end_mono_ns,
                 request_start_wall_us,request_end_wall_us,anchor_mono_ns,anchor_server_us,
                 uncertainty_us,status) VALUES (?,?,0,0,?,?,0,?,1000000,'VALID')""",
                (other, NOW_US//1_000_000, NOW_US, NOW_US, NOW_US))
        store.record_event(other, 'DISCOVERY_GAP', started_us=NOW_US+1_000_000, detail=self.proof(row))
        self.assertEqual(self.count(store), 0)

    def test_public_discovery_never_runs_supplied_client_auth(self):
        import httpx
        from polymarket_bot.recorder.chainlink_discovery import fetch_feed_discovery
        sent = []
        def respond(request):
            sent.append(request)
            return httpx.Response(200, json={'feeds': []}, headers={'Set-Cookie': 'response_secret=value'})
        with httpx.Client(auth=('synthetic-id', 'synthetic-secret'),
                headers={'Authorization': 'default-secret', 'X-API-Key': 'default-key'},
                cookies={'session': 'synthetic-cookie'}, transport=httpx.MockTransport(respond),
                trust_env=False) as client:
            self.assertEqual(fetch_feed_discovery(client), {'feeds': []})
            self.assertEqual(len(sent), 1)
            for header in ('authorization', 'x-api-key', 'cookie'):
                self.assertNotIn(header, sent[0].headers)
            self.assertEqual(len(client.cookies), 0)

    def test_same_stream_arrival_can_back_two_slot_associations(self):
        from dataclasses import replace
        from typing import cast
        from polymarket_bot.recorder.models import ReferenceReportDraft
        from polymarket_bot.recorder.chainlink_ws import ChainlinkLiveConsumer
        from polymarket_bot.recorder.reference import ManagedReferenceSession, ActivationEvidence
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID, ReferenceAssociationDraft
        from tests.test_schema_v2_decoder import build_schema_v2_report_payload
        store, run_id = self.fixture()
        now_ns = [0]
        payload, _ = build_schema_v2_report_payload(valid_from=NOW_US//1_000_000-60,
            observations=NOW_US//1_000_000, expires=NOW_US//1_000_000+60)
        frame = json.dumps(payload).encode()
        consumer = ChainlinkLiveConsumer('wss://ws.dataengine.chain.link', CHAINLINK_FEED_ID,
            'synthetic-id', 'synthetic-secret', clock_seconds=lambda: NOW_US/1_000_000,
            monotonic=lambda: now_ns[0]/1_000_000_000)
        consumer._handle_websocket_message(frame)
        session = ManagedReferenceSession(evidence=ActivationEvidence(*([True]*6)),
            discovery=lambda deadline: None, credentials=lambda: self.fail('No credentials needed'),
            monotonic_ns=lambda: now_ns[0], wall_us=lambda: NOW_US+now_ns[0]//1000,
            decoder_revision='abi')
        session.consumer, session.price_scale = consumer, 8
        with store._connection:
            store._connection.execute("""INSERT INTO clock_anchors
                (anchor_id,run_id,server_seconds,request_start_mono_ns,request_end_mono_ns,
                 request_start_wall_us,request_end_wall_us,anchor_mono_ns,anchor_server_us,
                 uncertainty_us,status) VALUES (2,?,?,4000000000,4000000000,?,?,4000000000,?,1000000,'VALID')""",
                (run_id, NOW_US//1_000_000+4, NOW_US+4_000_000, NOW_US+4_000_000, NOW_US+4_100_000))
        for index in (150, 151, 152):
            now_ns[0] = (index-150)*2_000_000_000
            slot = self.slot(store, run_id, index, 'COMPLETE', committed=NOW_US+now_ns[0]//1000)
            row = ReferenceAssociationDraft(MARKET_ID, 'SLOT_CURRENT', NOW_US+now_ns[0]//1000,
                'PENDING', 'NOT_FETCHED', False, NOW_US+now_ns[0]//1000,
                NOW_US+now_ns[0]//1000, slot_id=slot)
            _, fetch, reports, _ = session.fetch(row, 10, run_id=run_id, anchor_id=2 if index == 152 else 1,
                project=lambda ns: NOW_US+ns//1000+(100_000 if index == 152 else 0), buffer_only=True)
            reports = cast(tuple[ReferenceReportDraft, ...], reports)
            self.assertEqual(len(reports), 1)
            association = replace(row, status='AVAILABLE', origin='LIVE_LATEST', available_live=True,
                report_hash=reports[0].report_hash, selection_policy_revision='managed-signed-inclusive-seconds-v1')
            store.save_reference_evidence(fetch, reports, (association,))
        saved_reports = store._rows('SELECT * FROM reference_reports')
        self.assertEqual(len(saved_reports), 1)
        self.assertEqual(len(store._rows('SELECT * FROM reference_associations')), 3)
        self.assertEqual(len(store._rows('SELECT * FROM reference_fetches')), 1)
        self.assertEqual(store._rows('SELECT anchor_id,ended_server_us FROM request_attempts')[0],
            {'anchor_id': 1, 'ended_server_us': NOW_US})
        self.assertEqual(saved_reports[0]['first_received_server_us'], NOW_US)
        # Deduplication cannot hide an authoritative decoded-price conflict.
        from polymarket_bot.recorder.store import RecorderStoreError
        assert reports[0].decoded_price is not None
        with self.assertRaises(RecorderStoreError) as caught:
            store.save_reference_evidence(fetch, (replace(reports[0], decoded_price=reports[0].decoded_price+1),), ())
        self.assertEqual(caught.exception.code, 'fatal_invariant')

    def test_dormant_reconciler_refuses_managed_runs_before_fetch(self):
        from polymarket_bot.recorder import reference
        from polymarket_bot.market_data.models import MarketDataError
        from tests.recorder_support import WINDOW_START_US
        store, run_id = self.fixture()
        self.managed(store, run_id)
        store.save_reference_plan(reference.boundary_reference(MARKET_ID, 'OPENING',
            window_start_us=WINDOW_START_US, window_end_us=WINDOW_START_US+900_000_000,
            learned_wall_us=NOW_US, learned_server_us=NOW_US))
        fail = self.fail
        class Reader:
            def fetch_boundary(self, row, deadline):
                fail('Dormant offline reconciler fetched managed evidence')
        with self.assertRaises(MarketDataError) as caught:
            reference.reconcile_due_reference_boundaries(store, Reader(), NOW_US, 10, monotonic=lambda: 0)
        self.assertEqual(caught.exception.code, 'activation_required')
        self.assertEqual(store._rows('SELECT * FROM reference_fetches'), [])

    def test_dormant_reconciler_output_never_becomes_point_in_time_input(self):
        from polymarket_bot.recorder import reference
        from dataclasses import replace
        from tests.recorder_support import WINDOW_START_US
        from polymarket_bot.recorder.models import HttpAttempt, ReferenceFetchDraft
        store, run_id = self.fixture()
        plan = reference.boundary_reference(MARKET_ID, 'OPENING', window_start_us=WINDOW_START_US,
            window_end_us=WINDOW_START_US+900_000_000, learned_wall_us=NOW_US, learned_server_us=NOW_US)
        store.save_reference_plan(plan)
        report = reference.normalize_reference_fields(b'offline-signed', {'feed_id': reference.CHAINLINK_FEED_ID,
            'report_schema_version': 2, 'valid_from_seconds': WINDOW_START_US//1_000_000,
            'observations_seconds': NOW_US//1_000_000, 'expires_at_seconds': NOW_US//1_000_000+60,
            'price_integer': '100'}, price_scale=8, decoder_revision='offline-v2',
            received_wall_us=NOW_US+123, received_server_us=NOW_US)
        attempt = HttpAttempt('REFERENCE_LOOKUP', 'GET', '/api/v1/reports',
            ('feed_id='+reference.CHAINLINK_FEED_ID,), 1, 0, 100_000_000,
            NOW_US-100_000, NOW_US+123, 200, 'success', None, b'offline-envelope', True)
        fetch = ReferenceFetchDraft(attempt, run_id, reference.CHAINLINK_FEED_ID, 'OPENING',
            'HISTORICAL_LOOKUP', WINDOW_START_US, 'AVAILABLE', None, b'offline-envelope', NOW_US)
        class Reader:
            def fetch_boundary(self, row, deadline):
                return fetch, (report,)
        reference.reconcile_due_reference_boundaries(store, Reader(), NOW_US, 10, monotonic=lambda: 0)
        saved = store._one('SELECT * FROM reference_associations ORDER BY association_id DESC LIMIT 1')
        assert saved is not None
        association = replace(plan, status=saved['status'], origin=saved['origin'],
            learned_wall_us=saved['learned_wall_us'], learned_server_us=saved['learned_server_us'],
            report_hash=report.report_hash, selection_policy_revision=saved['selection_policy_revision'])
        self.assertFalse(reference.eligible_as_of(association, report, decision_us=NOW_US, role='OPENING'))
        self.assertEqual(store._rows("SELECT * FROM recorder_events WHERE detail_json LIKE '%REFERENCE_DURABLE%'"), [])


if __name__ == '__main__':
    unittest.main()
