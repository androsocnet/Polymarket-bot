"""Offline reproductions of independent CP3 review findings."""
import json
import unittest

from tests import test_cp3_regression as regression
from tests.recorder_support import NOW_US, RecorderFixture
from tests.support import MARKET_ID


class CP3ReviewTests(unittest.TestCase):
    fixture = regression.CP3RegressionTests.fixture
    slot = regression.CP3RegressionTests.slot
    reference = regression.CP3RegressionTests.reference
    request = regression.CP3RegressionTests.request
    metrics = regression.CP3RegressionTests.metrics
    managed = regression.CP3RegressionTests.managed
    evidence = regression.CP3RegressionTests.evidence
    proof = regression.CP3RegressionTests.proof
    count = regression.CP3RegressionTests.count

    def drain_fixture(self):
        from polymarket_bot.recorder.models import HttpAttempt, ReferenceAssociationDraft, ReferenceFetchDraft
        from polymarket_bot.recorder.reference import ActivationEvidence, ManagedReferenceSession, normalize_reference_fields
        from polymarket_bot.recorder.service import Recorder, ReferenceWorker
        store, run_id = self.fixture()
        self.managed(store, run_id)
        public_fixture = RecorderFixture()
        self.addCleanup(public_fixture.close)
        elapsed = [0]
        wall = [NOW_US]
        session = ManagedReferenceSession(evidence=ActivationEvidence(*([True]*6)),
            discovery=lambda deadline: None, credentials=lambda: self.fail('No credentials needed'),
            monotonic_ns=lambda: elapsed[0]*1000, wall_us=lambda: wall[0], decoder_revision='offline-v2')
        recorder = Recorder(store, public_fixture.public, monotonic_ns=lambda: elapsed[0]*1000,
            wall_us=lambda: wall[0], sleep=lambda seconds: None, offline_reference=session)
        recorder.run_id = run_id
        recorder.reference_worker = ReferenceWorker()
        recorder.clock.usable = lambda: True
        recorder._now = lambda: NOW_US+elapsed[0]
        slot = self.slot(store, run_id, 150, 'COMPLETE')
        row = ReferenceAssociationDraft(MARKET_ID, 'SLOT_CURRENT', NOW_US,
            'PENDING', 'NOT_FETCHED', False, NOW_US, NOW_US, slot_id=slot)
        report = normalize_reference_fields(b'synthetic-review-report', {
            'feed_id': row.feed_id, 'report_schema_version': 2,
            'valid_from_seconds': NOW_US//1_000_000-60,
            'observations_seconds': NOW_US//1_000_000,
            'expires_at_seconds': NOW_US//1_000_000+60, 'price_integer': '100'},
            price_scale=8, decoder_revision='offline-v2', received_wall_us=NOW_US,
            received_server_us=NOW_US)
        attempt = HttpAttempt('REFERENCE_LATEST', 'GET', '/ws/v1',
            ('feed_id='+row.feed_id,), 1, 0, 0, NOW_US, NOW_US,
            200, 'success', None, b'synthetic-review-envelope', True)
        fetch = ReferenceFetchDraft(attempt, run_id, row.feed_id, 'SLOT_CURRENT',
            'LIVE_LATEST', None, 'AVAILABLE', None, b'synthetic-review-envelope', NOW_US,
            anchor_id=1)
        return recorder, (row, fetch, (report,), 2), elapsed, wall

    def test_proof_timestamp_cannot_precede_proof_commit(self):
        recorder, result, elapsed, _ = self.drain_fixture()
        original = recorder.store.record_event
        committed = []
        def delayed_event(*args, **kwargs):
            elapsed[0] += 3_000_000
            event_id = original(*args, **kwargs)
            committed.append(NOW_US+elapsed[0])
            return event_id
        recorder.store.record_event = delayed_event
        recorder._reference_drain(result=result)
        proof = json.loads(recorder.store._rows(
            "SELECT detail_json FROM recorder_events WHERE detail_json LIKE '%REFERENCE_DURABLE%'")[0]['detail_json'])
        self.assertGreaterEqual(proof['durable_server_us'], committed[0])
        barrier = recorder.store._one('SELECT * FROM recorder_events WHERE event_id=?',
            (proof['commit_event_id'],))
        self.assertIsNotNone(barrier)
        assert barrier is not None
        self.assertEqual(json.loads(barrier['detail_json'])['reason'], 'REFERENCE_COMMIT')
        self.assertEqual(self.count(recorder.store, end=NOW_US+7_000_000), 0)
        self.assertEqual(proof['durability_basis'], 'POST_COMMIT_BARRIER_V1')

    def test_emitted_proof_wall_time_cannot_precede_association(self):
        recorder, result, _, wall = self.drain_fixture()
        original = recorder.store.save_reference_evidence
        def backward_wall(fetch, reports, associations):
            saved = original(fetch, reports, associations)
            if any(row.available_live for row in associations):
                wall[0] = NOW_US-1_000_000
            return saved
        recorder.store.save_reference_evidence = backward_wall
        recorder._reference_drain(result=result)
        events = recorder.store._rows('SELECT started_us,detail_json FROM recorder_events')
        proofs = [event for event in events if json.loads(event['detail_json'])['reason'] == 'REFERENCE_DURABLE']
        for event in proofs:
            proof = json.loads(event['detail_json'])
            self.assertGreaterEqual(event['started_us'], proof['learned_wall_us'])
        rejected = [event for event in events if json.loads(event['detail_json'])['reason'] == 'REFERENCE_CLOCK_ORDER_INVALID']
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]['started_us'], NOW_US-1_000_000)
        self.assertEqual(proofs, [])
        self.assertEqual(self.count(recorder.store), 0)

    def test_live_coverage_requires_freshness_at_durability(self):
        store, run_id = self.fixture()
        self.managed(store, run_id)
        row = self.evidence(store, run_id)
        with store._connection:
            store._connection.execute('UPDATE reference_reports SET observations_seconds=?',
                ((NOW_US-4_000_000)//1_000_000,))
            store._connection.execute('UPDATE reference_associations SET learned_server_us=?, learned_wall_us=?',
                (NOW_US+900_000, NOW_US+900_000))
        row = store._one('SELECT a.*,r.report_hash FROM reference_associations a JOIN reference_reports r USING(report_id)')
        assert row is not None
        barrier = store._one("SELECT event_id FROM recorder_events WHERE detail_json LIKE '%REFERENCE_COMMIT%'")
        assert barrier is not None
        row['commit_event_id'] = barrier['event_id']
        proof = self.proof(row)
        proof['durable_server_us'] = NOW_US+1_200_000
        with store._connection:
            store._connection.execute('UPDATE recorder_events SET started_us=?, detail_json=? WHERE event_id=?',
                (row['learned_wall_us'], json.dumps(dict(proof, reason='REFERENCE_COMMIT')), barrier['event_id']))
        store.record_event(run_id, 'DISCOVERY_GAP', started_us=NOW_US+1_200_000, detail=proof)
        self.assertEqual(self.count(store), 0)

    def test_raw_backward_learning_wall_rejected_before_association(self):
        recorder, result, _, wall = self.drain_fixture()
        wall[0] = NOW_US-1_000_000
        recorder._reference_drain(result=result)
        self.assertEqual(recorder.store._rows('SELECT * FROM reference_associations'), [])
        events = recorder.store._rows('SELECT started_us,detail_json FROM recorder_events')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['started_us'], NOW_US-1_000_000)
        self.assertEqual(json.loads(events[0]['detail_json'])['reason'], 'REFERENCE_CLOCK_ORDER_INVALID')

    def test_equal_learning_wall_resamples_real_clock(self):
        recorder, result, elapsed, wall = self.drain_fixture()
        recorder.store.save_reference_plan(result[0])
        def advance(seconds):
            elapsed[0] += round(seconds*1_000_000)
            wall[0] += round(seconds*1_000_000)
        recorder.sleep = advance
        recorder._reference_drain(result=result)
        rows = recorder.store._rows('SELECT * FROM reference_associations ORDER BY association_id')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['learned_wall_us'], wall[0])
        self.assertGreater(wall[0], NOW_US)
        self.assertEqual(self.count(recorder.store), 1)

    def test_equal_learning_wall_without_progress_fails_closed(self):
        recorder, result, _, _ = self.drain_fixture()
        recorder.store.save_reference_plan(result[0])
        recorder._reference_drain(result=result)
        self.assertEqual(len(recorder.store._rows('SELECT * FROM reference_associations')), 1)
        self.assertEqual(self.count(recorder.store), 0)
        self.assertEqual(json.loads(recorder.store._rows('SELECT detail_json FROM recorder_events')[0]
            ['detail_json'])['reason'], 'REFERENCE_LEARNING_TIME_COLLISION')

    def test_pre_barrier_managed_proof_rejected(self):
        from polymarket_bot.recorder.models import ReferenceAssociationDraft
        from polymarket_bot.recorder.reference import managed_durable_evidence
        store, run_id = self.fixture()
        self.managed(store, run_id)
        row = self.evidence(store, run_id)
        association = ReferenceAssociationDraft(**{name: row[name] for name in
            ReferenceAssociationDraft.__dataclass_fields__ if name != 'available_live'}, available_live=True)
        proof = self.proof(row)
        proof.pop('durability_basis')
        self.assertFalse(managed_durable_evidence(association, row['report_hash'], proof,
            decision_us=NOW_US+4_000_000))

    def test_barrier_linkage_required_for_coverage(self):
        for damage in ('missing_id', 'deleted', 'foreign_run', 'identity', 'wrong_reason', 'late'):
            with self.subTest(damage=damage):
                store, run_id = self.fixture()
                self.managed(store, run_id)
                row = self.evidence(store, run_id)
                proof = self.proof(row)
                barrier_id = row['commit_event_id']
                with store._connection:
                    if damage == 'missing_id':
                        proof['commit_event_id'] = 999999
                    elif damage == 'deleted':
                        store._connection.execute('DELETE FROM recorder_events WHERE event_id=?', (barrier_id,))
                    elif damage == 'foreign_run':
                        from polymarket_bot.recorder.models import RunConfig
                        other = store.start_run(RunConfig(), wall_us=NOW_US, app_revision='foreign')
                        self.managed(store, other)
                        store._connection.execute("""INSERT INTO clock_anchors
                            (run_id,server_seconds,request_start_mono_ns,request_end_mono_ns,
                             request_start_wall_us,request_end_wall_us,anchor_mono_ns,anchor_server_us,
                             uncertainty_us,status) VALUES (?,?,0,0,?,?,0,?,1000000,'VALID')""",
                            (other, NOW_US//1_000_000, NOW_US, NOW_US, NOW_US))
                        store._connection.execute('UPDATE recorder_events SET run_id=? WHERE event_id=?', (other, barrier_id))
                    elif damage == 'late':
                        store._connection.execute('UPDATE recorder_events SET started_us=? WHERE event_id=?', (NOW_US+2_000_000, barrier_id))
                    else:
                        event = store._one('SELECT detail_json FROM recorder_events WHERE event_id=?', (barrier_id,))
                        assert event is not None
                        barrier = json.loads(event['detail_json'])
                        barrier['market_id' if damage == 'identity' else 'reason'] = 'foreign'
                        store._connection.execute('UPDATE recorder_events SET detail_json=? WHERE event_id=?', (json.dumps(barrier), barrier_id))
                store.record_event(run_id, 'DISCOVERY_GAP', started_us=NOW_US+1_000_000, detail=proof)
                self.assertEqual(self.count(store), 0)

    def test_failed_socket_close_retains_underlying_handle(self):
        from polymarket_bot.market_data.models import MarketDataError
        from polymarket_bot.recorder.chainlink_ws import ChainlinkLiveConsumer
        from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
        class Socket:
            def close(self) -> None:
                raise OSError('synthetic close failure')
        for path in ('stop', 'worker'):
            with self.subTest(path=path):
                consumer = ChainlinkLiveConsumer('wss://ws.dataengine.chain.link', CHAINLINK_FEED_ID,
                    'synthetic-id', 'synthetic-secret')
                socket = Socket()
                consumer._ws = socket
                if path == 'stop':
                    with self.assertRaises(MarketDataError):
                        consumer.stop()
                else:
                    consumer._connect = lambda: None
                    consumer._message_loop = lambda: None
                    with self.assertRaises(OSError):
                        consumer._run()
                self.assertIs(consumer._ws, socket)
                with self.assertRaises(MarketDataError):
                    consumer.start()
                socket.close = lambda: None
                consumer.stop()
                self.assertIsNone(consumer._ws)


if __name__ == '__main__':
    unittest.main()
