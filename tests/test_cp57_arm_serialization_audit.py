import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import unittest
import zlib
from decimal import Decimal
from typing import Any, cast

import httpx

from polymarket_bot.market_data.numeric import decimal_text, load_numeric_json
from polymarket_bot.recorder.metadata import acquire_market
from polymarket_bot.recorder.models import RunConfig
from polymarket_bot.recorder.service import Recorder
from polymarket_bot.recorder.store import RecorderStoreError, _json
from tests.recorder_support import RecorderFixture

_scripts = str(Path(__file__).resolve().parents[1] / 'scripts')
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)


class ArmSerializationAuditTests(unittest.TestCase):
    def fixture(self):
        fixture = RecorderFixture(offset_us=570_000_000)
        self.addCleanup(fixture.close)
        return fixture

    def test_retained_numeric_event_reaches_actual_arm_before_publication(self):
        root = Path(__file__).resolve().parents[1]
        path = root / 'data/cp5_6c_replacement_20261017_0351adeb-6594-492e-9237-adf7d833dba0.sqlite3'
        expected = 'a488583880d10507a24e82251d0870433c1d39160b77429820a2d339a54ca409'
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
        connection = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
        try:
            rows = connection.execute('SELECT request_id,response_raw_z FROM request_attempts WHERE request_id IN (15,16,17,27,28,29)').fetchall()
            bodies = {number: zlib.decompress(raw) for number, raw in rows}
        finally:
            connection.close()
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
        selected = [event for event in cast(list[dict], load_numeric_json(bodies[27])) if event['id'] == '1197070']
        self.assertEqual(len(selected), 1)
        self.assertIsInstance(selected[0]['liquidity'], Decimal)
        self.assertEqual(bodies[16], bodies[28])
        self.assertEqual(bodies[17], bodies[29])
        fixture = self.fixture()
        fixture.initial_wall_us = 1792193370_000000
        recorder = cast(Any, Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep))
        recorder.run_id = fixture.store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
        recorder.run_deadline_ns = fixture.monotonic_ns()+100_000_000_000
        recorder.config = RunConfig()
        recorder.invalidated = set()
        recorder._calibrate(fixture.monotonic()+10)
        previous = acquire_market(recorder.gamma, recorder.public, clock=recorder.clock,
            deadline=fixture.monotonic()+10, use_fast_path=False)
        recorder._save_revision(previous)
        def respond(request, payload, owner):
            if request.url.path == '/events':
                return httpx.Response(200, stream=httpx.ByteStream(bodies[27]))
            if request.url.path == '/markets/5764590':
                return httpx.Response(200, stream=httpx.ByteStream(bodies[28]))
            if request.url.path.startswith('/clob-markets/'):
                return httpx.Response(200, stream=httpx.ByteStream(bodies[29]))
        cast(Any, fixture).hook = respond
        recorder._prepare_next(recorder.run_deadline_ns)
        fixture.advance((recorder.clock.deadline_ns(1792193388_000000)-fixture.monotonic_ns())/1e9)
        failure = None
        try:
            recorder._prepare_next(recorder.run_deadline_ns)
        except RecorderStoreError as exc:
            failure = (exc.code, str(exc), str(exc.__cause__))
        self.assertFalse(fixture.store._write_failed)
        fixture.store.record_event(recorder.run_id, 'DISCOVERY_GAP', started_us=fixture.wall_us(), detail={'probe': True})
        self.assertIsNone(failure, failure)
        arms = fixture.store._rows("SELECT detail_json FROM recorder_events WHERE detail_json LIKE '%PREPARED_ARM%'")
        self.assertEqual(len(arms), 1)
        self.assertEqual(json.loads(arms[0]['detail_json'])['event']['liquidity'], '4886.8575')
        self.assertEqual(recorder._preparation_phase[1], 'ARMED')

    def test_recorder_json_uses_existing_canonical_decimal_policy(self):
        value = {'nested': (None, True, 2**63-1, Decimal('1E-7'), Decimal('-0.00'))}
        failure = None
        encoded = None
        try:
            encoded = _json(value)
        except RecorderStoreError as exc:
            failure = str(exc)
        self.assertIsNone(failure, failure)
        self.assertEqual(encoded, '{"nested":[null,true,9223372036854775807,"0.0000001","-0.00"]}')
        self.assertEqual(decimal_text(Decimal('1E-7')), '0.0000001')

    def test_recorder_json_rejects_non_string_keys_and_unbounded_integers(self):
        for value in ({1: 'coerced'}, {'n': 2**63}, {'n': 0.1}, {'n': object()}):
            with self.subTest(value=value):
                with self.assertRaises(RecorderStoreError):
                    _json(value)

    def test_decimal_envelope_boundaries_and_fail_closed(self):
        from polymarket_bot.market_data.models import MarketDataError
        fixture = self.fixture()
        run_id = fixture.store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
        for kind in ('PREPARED_ARM', 'PREPARED_ACTIVATION'):
            for number in (Decimal('-123.123456789012345678901234567890'), Decimal('1E-128')):
                detail = dict(type=kind, event={'nested': [dict(number=number)]}, pad='')
                detail['pad'] = 'x' * (1_048_576-len(_json(detail).encode()))
                fixture.store.record_event(run_id, 'DISCOVERY_GAP', started_us=fixture.wall_us(), detail=detail)
                row = fixture.store._rows('SELECT detail_json FROM recorder_events ORDER BY event_id DESC LIMIT 1')[0]
                self.assertEqual(len(row['detail_json'].encode()), 1_048_576)
                self.assertEqual(json.loads(row['detail_json'])['event']['nested'][0]['number'], decimal_text(number))
                detail['pad'] += 'x'
                count = len(fixture.store._rows('SELECT * FROM recorder_events'))
                with self.assertRaises(MarketDataError):
                    fixture.store.record_event(run_id, 'DISCOVERY_GAP', started_us=fixture.wall_us(), detail=detail)
                self.assertEqual(len(fixture.store._rows('SELECT * FROM recorder_events')), count)
            cycle = []
            cycle.append(cycle)
            for invalid in (object(), 0.1, Decimal('NaN'), Decimal('Infinity'), Decimal('1E-1025'),
                            Decimal('1.'+'1'*512), {1: 'bad'}, -(2**63)-1, cycle):
                with self.subTest(kind=kind, invalid=type(invalid)):
                    count = len(fixture.store._rows('SELECT * FROM recorder_events'))
                    with self.assertRaises(RecorderStoreError):
                        fixture.store.record_event(run_id, 'DISCOVERY_GAP', started_us=fixture.wall_us(),
                            detail=dict(type=kind, event={'nested': [invalid]}))
                    self.assertEqual(len(fixture.store._rows('SELECT * FROM recorder_events')), count)
                    self.assertFalse(fixture.store._write_failed)

    def test_decimal_activation_publication_and_transaction_rollback(self):
        from dataclasses import replace
        from datetime import timedelta
        from unittest.mock import patch
        from polymarket_bot.recorder.metadata import _identity
        from polymarket_bot.market_data.models import MarketDataError
        for fault in ('equal', 'oversize', 'insert'):
            with self.subTest(fault=fault):
                fixture = self.fixture()
                recorder = cast(Any, Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
                    wall_us=fixture.wall_us, sleep=fixture.sleep))
                recorder.run_id = fixture.store.start_run(RunConfig(), wall_us=fixture.wall_us(), app_revision='test')
                recorder.run_deadline_ns = fixture.monotonic_ns()+100_000_000_000
                recorder.invalidated = set()
                recorder.config = RunConfig()
                recorder.discovery_gap_id = None
                recorder._calibrate(fixture.monotonic()+10)
                prior = acquire_market(recorder.gamma, recorder.public, clock=recorder.clock,
                    deadline=fixture.monotonic()+10, use_fast_path=False)
                recorder._save_revision(prior)
                boundary = int(prior.market.window_end.timestamp()*1_000_000)
                target = recorder._nominal_target_ns(boundary)
                event = dict(number=Decimal('-0.00'), pad='')
                future = replace(prior, gamma_raw=b'{"id":"1002"}', market=replace(prior.market,
                    market_id='1002', event_id='2002', condition_id='0x'+'cd'*32,
                    window_start=prior.market.window_end, window_end=prior.market.window_end+timedelta(seconds=900),
                    up=replace(prior.market.up, token_id='3'), down=replace(prior.market.down, token_id='4'),
                    metadata=dict(prior.market.metadata, prepared_event=event, prepared_list_evidence=[])))
                prepared = dict(revision=future, owner=(recorder.run_id, prior.market.event_id, _identity(prior)),
                    boundary=boundary, prepared_ns=target-30_000_000_000, validated_ns=target-2_000_000_000,
                    armed_ns=target-2_000_000_000, target_ns=target)
                fixture.advance((target-fixture.monotonic_ns())/1e9)
                detail = dict(type='PREPARED_ACTIVATION', predecessor=prior.market.market_id, boundary_us=boundary,
                    prepared_ns=prepared['prepared_ns'], validated_ns=prepared['validated_ns'],
                    armed_ns=prepared['armed_ns'], target_ns=target, activated_ns=fixture.monotonic_ns(),
                    active_target_ns=recorder.clock.deadline_ns(boundary), event=event, list_evidence=[])
                event['pad'] = 'x'*(1_048_576-len(_json(detail).encode())+(fault == 'oversize'))
                recorder._prepared = prepared
                recorder._armed_opening = None
                before = {table: fixture.rows('SELECT * FROM '+table) for table in
                    ('markets', 'market_revisions', 'reference_associations', 'recorder_events')}
                if fault == 'insert':
                    fixture.store._connection.execute('CREATE TRIGGER reject_activation BEFORE INSERT ON recorder_events BEGIN SELECT RAISE(ABORT, "activation fault"); END')
                with patch.object(fixture.store, 'record_event', wraps=fixture.store.record_event):
                    if fault == 'equal':
                        self.assertTrue(recorder._activate_prepared())
                        row = fixture.rows("SELECT detail_json FROM recorder_events WHERE detail_json LIKE '%PREPARED_ACTIVATION%'")[0]
                        self.assertEqual(len(row['detail_json'].encode()), 1_048_576)
                        self.assertEqual(json.loads(row['detail_json'])['event']['number'], '-0.00')
                        self.assertIs(recorder.revision, future)
                        self.assertEqual(recorder._armed_opening, ('1002', target))
                    else:
                        with self.assertRaises(MarketDataError if fault == 'oversize' else RecorderStoreError):
                            recorder._activate_prepared()
                        self.assertIs(recorder.revision, prior)
                        self.assertIsNone(recorder._armed_opening)
                        for table, rows in before.items():
                            self.assertEqual(fixture.rows('SELECT * FROM '+table), rows)
                self.assertIsNone(recorder._prepared)

    def test_canonical_determinism_context_and_closed_edges(self):
        from decimal import localcontext
        value = dict(z=[Decimal('123.456789'), -(2**63)], a=Decimal('-0.00'))
        expected = _json(value)
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(_json(dict(reversed(list(value.items())))), expected)
            self.assertEqual(_json(value), expected)
        deep = []
        for _ in range(2000):
            deep = [deep]
        for invalid in (float('nan'), float('inf'), float('-inf'), deep):
            with self.subTest(invalid=type(invalid)):
                with self.assertRaises(RecorderStoreError):
                    _json(invalid)

    def test_fatal_lifecycle_matrix(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import patch
        from cp5_run import CP5Recorder
        from polymarket_bot.market_data.models import MarketDataError
        case = self
        for mode in ('healthy', 'flush_fails', 'metadata', 'parser', 'insert_fails'):
            with self.subTest(mode=mode):
                fixture = self.fixture()
                release = threading.Event()
                class FatalRecorder(CP5Recorder):
                    def _record(self, config, *, should_stop):
                        self.run_id = self.store.start_run(config, wall_us=fixture.wall_us(), app_revision='test')
                        self._audit_events = [dict(attempt=1, started_wall_us=fixture.wall_us(), complete=True)]
                        def complete():
                            release.wait(2)
                            self._audit_events.append(dict(attempt=2, started_wall_us=fixture.wall_us(), complete=True))
                        thread = threading.Thread(target=complete)
                        thread.start()
                        def stop():
                            release.set()
                            thread.join(2)
                            case.assertFalse(thread.is_alive())
                        self.reference_worker = SimpleNamespace(stop=stop)
                        if mode == 'metadata':
                            raise MarketDataError('invalid_metadata', 'Offline metadata.')
                        if mode == 'parser':
                            raise MarketDataError('invalid_numeric', 'Offline parser.')
                        if mode == 'insert_fails':
                            self.store._connection.execute('CREATE TRIGGER reject_event BEFORE INSERT ON recorder_events BEGIN SELECT RAISE(ABORT, "offline insert failure"); END')
                            self.store.record_event(self.run_id, 'DISCOVERY_GAP', started_us=fixture.wall_us(), detail={})
                        raise RecorderStoreError('fatal_invariant', 'Healthy preinsert failure.')

                    def _final_persistence(self):
                        if mode == 'flush_fails':
                            raise OSError('Offline audit flush failure.')
                        super()._final_persistence()
                        super()._final_persistence()
                recorder = FatalRecorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
                    wall_us=fixture.wall_us, sleep=fixture.sleep)
                with patch('polymarket_bot.recorder.service.take_checkpoint') as checkpoint:
                    result = recorder.run(RunConfig(), should_stop=lambda: False)
                    checkpoint.assert_not_called()
                self.assertNotEqual(result.exit_code, 0)
                details = [json.loads(row['detail_json']) for row in fixture.rows('SELECT detail_json FROM recorder_events')]
                ids = [row['attempt'] for row in details if row.get('type') == 'ACTUAL_SEND_ATTEMPT']
                run = fixture.rows('SELECT state,exit_reason AS reason FROM recording_runs')[0]
                if mode in ('flush_fails', 'insert_fails'):
                    self.assertEqual(ids, [])
                    self.assertEqual(run['state'], 'RUNNING')
                    self.assertEqual((result.exit_code, result.reason), (1, 'storage_failure'))
                    self.assertIsNone(run['reason'])
                else:
                    self.assertEqual(ids, [1, 2])
                    self.assertEqual(run['state'], 'FAILED')
                    self.assertEqual(run['reason'], result.reason)
                self.assertTrue(release.is_set())
                self.assertTrue(fixture.store._closed)
                from polymarket_bot.recorder.store import RecorderStore
                if mode == 'insert_fails':
                    connection = sqlite3.connect(fixture.db.path)
                    try:
                        connection.execute('DROP TRIGGER reject_event')
                        connection.commit()
                    finally:
                        connection.close()
                reopened = RecorderStore.open(fixture.db.path)
                reopened.close()
                fixture.client.close()
                fixture.client.close()
                self.assertTrue(fixture.client.is_closed)

    def test_fatal_real_worker_timeout_retains_owner_until_join(self):
        import threading
        from unittest.mock import Mock, patch
        from cp5_run import CP5Recorder
        from polymarket_bot.recorder.service import ApproximateReferenceWorker
        from polymarket_bot.recorder.store import RecorderStore
        fixture = self.fixture()
        fixture.store.close()
        fixture.db.path.unlink()
        fixture.store = RecorderStore.open_approximate(fixture.db.path)
        release, entered = threading.Event(), threading.Event()
        class FatalRecorder(CP5Recorder):
            def _record(self, config, *, should_stop):
                self.run_id = self.store.start_run(config, wall_us=fixture.wall_us(), app_revision='test')
                def operation():
                    entered.set()
                    release.wait(10)
                assert self.approximate_worker is not None
                self.approximate_worker.submit(operation)
                if not entered.wait(1):
                    raise AssertionError('Worker never entered.')
                raise RecorderStoreError('fatal_invariant', 'Healthy fatal with active worker.')
            def _final_persistence(self):
                raise AssertionError('Cannot flush before quiescence.')
        session = Mock()
        recorder = cast(Any, FatalRecorder(fixture.store, fixture.public, approximate_reference=session,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us, sleep=fixture.sleep))
        stop = ApproximateReferenceWorker.stop
        try:
            with patch.object(ApproximateReferenceWorker, 'stop', lambda worker: stop(worker, timeout=0.01)):
                result = recorder.run(RunConfig(mode='APPROXIMATE_REFERENCE'), should_stop=lambda: False)
            self.assertEqual((result.exit_code, result.reason), (2, 'approximate_reference_shutdown_failed'))
            self.assertFalse(fixture.store._closed)
            session.stop.assert_not_called()
            run = fixture.rows('SELECT state,exit_reason FROM recording_runs')[0]
            self.assertEqual(run, dict(state='RUNNING', exit_reason=None))
            self.assertEqual(fixture.rows('SELECT * FROM recorder_events'), [])
        finally:
            release.set()
            recorder.approximate_worker.stop()
            recorder.approximate_worker.stop()
            session.stop()
            fixture.store.close()
        reopened = RecorderStore.open_approximate(fixture.db.path)
        reopened.close()

    def test_failed_writer_fatal_exit_does_not_flush(self):
        from cp5_run import CP5Recorder
        fixture = self.fixture()
        class FailedRecorder(CP5Recorder):
            def _record(self, config, *, should_stop):
                self.run_id = self.store.start_run(config, wall_us=fixture.wall_us(), app_revision='test')
                self.finalized = 0
                self.store._write_failed = True
                raise RecorderStoreError('fatal_invariant', 'Failed writer control.')

            def _final_persistence(self):
                raise AssertionError('Failed writer must not receive more writes.')
        recorder = FailedRecorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep)
        result = recorder.run(RunConfig(), should_stop=lambda: False)
        self.assertEqual((result.exit_code, result.reason), (1, 'fatal_invariant'))
        self.assertEqual(fixture.rows('SELECT count(*) AS n FROM recorder_events')[0]['n'], 0)

    def test_healthy_fatal_recovers_pending_ownership(self):
        from cp5_run import CP5Recorder
        from polymarket_bot.recorder.models import SlotTarget
        from tests.recorder_support import seed_writer_market, NOW_US, WINDOW_START_US
        from tests.support import MARKET_ID
        fixture = self.fixture()
        class FatalRecorder(CP5Recorder):
            def _record(self, config, *, should_stop):
                self.run_id = self.store.start_run(config, wall_us=fixture.wall_us(), app_revision='test')
                seed_writer_market(self.store, self.run_id)
                target = SlotTarget(150, WINDOW_START_US+300_000_000)
                self.store.reserve_slot(MARKET_ID, target, run_id=self.run_id, anchor_id=1,
                    metadata_revision_id=1, started_mono_ns=0, started_server_us=NOW_US)
                self.store.record_event(self.run_id, 'DISCOVERY_GAP', started_us=NOW_US,
                    detail=dict(type='APPROXIMATE_OPPORTUNITY', version=2, disposition='submitted',
                        market_id=MARKET_ID, slot_index=150, scheduled_us=target.scheduled_us, target_ns=42))
                self._audit_events = [dict(attempt=1, started_wall_us=NOW_US, complete=True)]
                raise RecorderStoreError('fatal_invariant', 'Pending ownership.')
        recorder = FatalRecorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep)
        result = recorder.run(RunConfig(), should_stop=lambda: False)
        self.assertEqual((result.exit_code, result.reason), (1, 'fatal_invariant'))
        slot = fixture.rows('SELECT status,reason FROM sample_slots')[0]
        self.assertEqual(slot, dict(status='INTERRUPTED', reason='fatal_invariant'))
        events = [json.loads(row['detail_json']) for row in fixture.rows('SELECT detail_json FROM recorder_events')]
        terminal = [row for row in events if row.get('type') == 'APPROXIMATE_OPPORTUNITY_COMPLETION']
        self.assertEqual(len(terminal), 1)
        self.assertEqual((terminal[0]['disposition'], terminal[0]['reason'], terminal[0]['target_ns']),
            ('interrupted', 'fatal_invariant', 42))
        self.assertEqual([row['attempt'] for row in events if row.get('type') == 'ACTUAL_SEND_ATTEMPT'], [1])

    def test_healthy_preinsert_fatal_exit_preserves_completed_audit(self):
        from cp5_run import CP5Recorder
        fixture = self.fixture()
        class FatalRecorder(CP5Recorder):
            def _record(self, config, *, should_stop):
                self.run_id = self.store.start_run(config, wall_us=fixture.wall_us(), app_revision='test')
                self.finalized = 0
                self._audit_events = [dict(attempt=n, started_wall_us=fixture.wall_us(), complete=True) for n in (1, 2, 3)]
                self.store.record_event(self.run_id, 'DISCOVERY_GAP', started_us=fixture.wall_us(), detail={'unknown': object()})
        recorder = FatalRecorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep)
        result = recorder.run(RunConfig(), should_stop=lambda: False)
        self.assertEqual((result.exit_code, result.reason), (1, 'fatal_invariant'))
        self.assertFalse(fixture.store._write_failed)
        events = [json.loads(row['detail_json']) for row in fixture.rows('SELECT detail_json FROM recorder_events')]
        self.assertEqual([event['attempt'] for event in events if event.get('type') == 'ACTUAL_SEND_ATTEMPT'], [1, 2, 3])
