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
            with self.assertRaises(ValueError):
                recorder._persist_research(self.research_result)
        after = self.store._rows('SELECT detail_json FROM recorder_events')
        self.assertEqual(len(after), original_count)
        self.assertEqual(recorder._research_mapping, {})

    def test_nested_transaction_failures_never_commit_caught_inner_writes(self):
        import sqlite3
        for failure in (sqlite3.IntegrityError, RuntimeError, KeyboardInterrupt):
            for caught in (False, True):
                with self.subTest(failure=failure, caught=caught):
                    before = self.store._rows('SELECT * FROM recorder_events')
                    def operation():
                        with self.store._transaction():
                            try:
                                with self.store._transaction():
                                    self.store.record_event(self.run_id, 'DISCOVERY_GAP', started_us=NOW_US)
                                    raise failure('nested failure')
                            except failure:
                                if not caught:
                                    raise
                    expected = RecorderStoreError if failure is sqlite3.IntegrityError else failure
                    with self.assertRaises(expected):
                        operation()
                    if failure is sqlite3.IntegrityError:
                        self.assertTrue(self.store._write_failed)
                        self.store._write_failed = False
                    self.assertEqual(self.store._rows('SELECT * FROM recorder_events'), before)

    def test_nested_publication_rejected_before_rows_or_memory_change(self):
        from unittest.mock import patch
        from polymarket_bot.recorder.service import Recorder
        original = Recorder._persist_research
        def persist(recorder, result):
            before = recorder.store._rows('SELECT * FROM approximate_observations')
            evidence, mapping = recorder._research_evidence, dict(recorder._research_mapping)
            with recorder.store._transaction():
                with self.assertRaises(ValueError):
                    original(recorder, result)
            self.assertEqual(recorder.store._rows('SELECT * FROM approximate_observations'), before)
            self.assertEqual(recorder._research_mapping, mapping)
            self.assertEqual(recorder._research_evidence, evidence)
            return original(recorder, result)
        with patch.object(Recorder, '_persist_research', persist):
            self.test_revision_two_recorder_flow()

    def test_current_acquisition_owner_and_original_types_are_bound(self):
        import json
        from unittest.mock import patch
        from polymarket_bot.recorder.service import Recorder
        original = Recorder._publish_research
        def reject(recorder, result):
            with self.assertRaises(RuntimeError):
                with recorder.store._transaction():
                    with self.assertRaises(ValueError):
                        original(recorder, result)
                    raise RuntimeError('rollback mutation')
        def persist(recorder, result):
            opportunity, identity, attempts = recorder._research_pending
            row = recorder.store._one('SELECT detail_json FROM recorder_events WHERE event_id=?', (opportunity,))
            authority = json.loads(row['detail_json'])
            tables = ('approximate_observations', 'approximate_references', 'recorder_events')
            for field, value in (('version', 1), ('run_id', 'foreign'), ('slot_id', True),
                    ('slot_index', True), ('target_ns', identity['target_ns'] + 1), ('scheduled_us', 1)):
                changed = dict(authority, **{field: value})
                with recorder.store._transaction():
                    recorder.store._connection.execute('UPDATE recorder_events SET detail_json=? WHERE event_id=?',
                        (json.dumps(changed), opportunity))
                before = [recorder.store._rows('SELECT * FROM ' + table) for table in tables]
                with self.subTest(field=field):
                    reject(recorder, result)
                self.assertEqual([recorder.store._rows('SELECT * FROM ' + table) for table in tables], before)
                recorder._research_mapping = {}
                with recorder.store._transaction():
                    recorder.store._connection.execute('UPDATE recorder_events SET detail_json=? WHERE event_id=?',
                        (json.dumps(authority), opportunity))
            replacement = recorder.store.record_event(recorder.run_id, 'DISCOVERY_GAP', started_us=NOW_US,
                detail=authority)
            from dataclasses import replace
            result = replace(result, evidence=replace(result.evidence,
                scheduled_opportunities=result.evidence.scheduled_opportunities +
                ((replacement, identity['target_ns'], 'submitted'),)))
            recorder._research_pending = replacement, identity, attempts
            before = [recorder.store._rows('SELECT * FROM ' + table) for table in tables]
            reject(recorder, result)
            self.assertEqual([recorder.store._rows('SELECT * FROM ' + table) for table in tables], before)
            recorder._research_mapping = {}
            recorder._research_pending = opportunity, identity, attempts
            for field, value in (('opportunity_id', replacement), ('run_id', 'foreign'), ('target_ns', 1),
                    ('slot_id', 2), ('slot_index', 2), ('scheduled_us', 2)):
                mutated = [dict(attempts[0], **{field: value}), *attempts[1:]]
                recorder._research_pending = opportunity, identity, mutated
                with self.subTest(attempt=field):
                    reject(recorder, result)
                recorder._research_mapping = {}
            recorder._research_pending = opportunity, identity, attempts
            raise OSError('checked identity mutations')
        with patch.object(Recorder, '_persist_research', persist):
            with self.assertRaises(OSError):
                self.test_revision_two_recorder_flow()

    def test_stop_and_interrupt_drain_live_and_undrained_acquisitions(self):
        import json
        import sqlite3
        from threading import Event, Timer
        from unittest.mock import patch
        from polymarket_bot.recorder.service import Recorder
        for interrupt in (False, True):
            for completed in (False, True):
                with self.subTest(interrupt=interrupt, completed=completed):
                    self.run_id = self.store.start_run(RunConfig(mode='APPROXIMATE_REFERENCE'),
                        wall_us=NOW_US, app_revision='stop')
                    def before_drain(recorder, result):
                        entered, release = Event(), Event()
                        recorder.finalized = 0
                        def record(owner, config, **kwargs):
                            def work():
                                entered.set()
                                if not release.wait(2):
                                    raise RuntimeError('barrier timed out')
                                return result
                            self.assertTrue(owner.approximate_worker.submit(work))
                            self.assertTrue(entered.wait(2))
                            if completed:
                                release.set()
                                owner.approximate_worker.stop()
                            else:
                                timer = Timer(0.02, release.set)
                                timer.start()
                                self.addCleanup(timer.join)
                            if interrupt:
                                raise KeyboardInterrupt
                            owner.public.before_attempt()
                            raise AssertionError('stop guard did not stop')
                        try:
                            with patch.object(Recorder, '_record', record):
                                try:
                                    outcome = recorder._execute_started_run(RunConfig(mode='APPROXIMATE_REFERENCE'),
                                        should_stop=lambda: True)
                                except ValueError as exc:
                                    self.fail(str(exc))
                        finally:
                            if recorder.store._closed:
                                self.store = RecorderStore.open_approximate(self.db_path)
                        self.assertEqual(outcome.exit_code, 130 if interrupt else 0)
                        self.assertTrue(recorder.store._closed)
                        self.assertIsNone(recorder.approximate_worker._thread)
                        with sqlite3.connect(self.db_path) as connection:
                            rows = [json.loads(row[0]) for row in connection.execute(
                                'SELECT detail_json FROM recorder_events WHERE run_id=?', (self.run_id,))]
                            terminal = [row for row in rows if row.get('type') == 'APPROXIMATE_OPPORTUNITY_COMPLETION']
                            self.assertEqual(len(terminal), 1)
                            self.assertEqual(terminal[0]['disposition'], 'completed')
                            self.assertTrue(any(row.get('reason') == ('KEYBOARD_INTERRUPT' if interrupt else 'STOP_REQUESTED') for row in rows))
                        raise OSError('stop fixture after lifecycle check')
                    with self.assertRaises(OSError):
                        self.test_revision_two_recorder_flow(before_drain=before_drain)

    def test_authority_queries_do_not_decode_pilot_history(self):
        import json
        from unittest.mock import patch
        from polymarket_bot.recorder.service import Recorder
        original = Recorder._persist_research
        def persist(recorder, result):
            for _ in range(600):
                recorder.store.record_event(recorder.run_id, 'DISCOVERY_GAP', started_us=NOW_US,
                    detail=dict(type='ACTUAL_SEND_ATTEMPT', payload='x' * 4096))
            decoded = []
            loads = json.loads
            def decode(value, *args, **kwargs):
                decoded.append(len(value))
                return loads(value, *args, **kwargs)
            steps = [0]
            def progress():
                steps[0] += 1
                return 0
            recorder.store._connection.set_progress_handler(progress, 1)
            try:
                with patch.object(json, 'loads', side_effect=decode):
                    original(recorder, result)
            finally:
                recorder.store._connection.set_progress_handler(None, 0)
            self.assertLess(len(decoded), 40)
            self.assertLess(steps[0], 20000)
        with patch.object(Recorder, '_persist_research', persist):
            self.test_revision_two_recorder_flow()

    def test_old_history_does_not_increase_authority_vm_work(self):
        from unittest.mock import patch
        from polymarket_bot.recorder.service import Recorder
        original = Recorder._persist_research
        def persist(recorder, result):
            recorder._research_event_floor = recorder._research_pending[0]
            steps = [0]
            def progress():
                steps[0] += 1
                return 0
            recorder.store._connection.set_progress_handler(progress, 1)
            try:
                original(recorder, result)
            finally:
                recorder.store._connection.set_progress_handler(None, 0)
            self.assertLess(steps[0], 2500)
        for _ in range(600):
            self.store.record_event(self.run_id, 'DISCOVERY_GAP', started_us=NOW_US,
                detail=dict(type='ACTUAL_SEND_ATTEMPT', payload='x' * 4096))
        with patch.object(Recorder, '_persist_research', persist):
            self.test_revision_two_recorder_flow()

    def test_authority_range_is_bounded_and_old_duplicates_fail_closed(self):
        from unittest.mock import patch
        from polymarket_bot.recorder.service import Recorder
        original = Recorder._persist_research
        def persist(recorder, result):
            for _ in range(4100):
                recorder.store.record_event(recorder.run_id, 'DISCOVERY_GAP', started_us=NOW_US,
                    detail=dict(type='ACTUAL_SEND_ATTEMPT'))
            before = recorder.store._rows('SELECT * FROM approximate_observations')
            steps = [0]
            def progress():
                steps[0] += 1
                return 0
            recorder.store._connection.set_progress_handler(progress, 1)
            try:
                with self.assertRaisesRegex(ValueError, 'bound exceeded'):
                    original(recorder, result)
            finally:
                recorder.store._connection.set_progress_handler(None, 0)
            self.assertLess(steps[0], 30000)
            self.assertEqual(recorder.store._rows('SELECT * FROM approximate_observations'), before)
            self.assertEqual(recorder._research_mapping, {})
            raise OSError('bounded overflow checked')
        with patch.object(Recorder, '_persist_research', persist), self.assertRaises(OSError):
            self.test_revision_two_recorder_flow()

    def test_after_insert_faults_reopen_without_orphans(self):
        import sqlite3
        import json
        from unittest.mock import patch
        from polymarket_bot.recorder.service import Recorder
        original = Recorder._persist_research
        for position in range(1, 6):
            for error in (OSError, sqlite3.IntegrityError):
                with self.subTest(position=position, error=error):
                    self.run_id = self.store.start_run(RunConfig(mode='APPROXIMATE_REFERENCE'),
                        wall_us=NOW_US, app_revision='crash')
                    def persist(recorder, result):
                        before = {table: recorder.store._rows('SELECT * FROM ' + table) for table in
                            ('approximate_observations', 'approximate_references', 'recorder_events')}
                        insert = recorder.store._insert
                        count = [0]
                        def fail(table, values):
                            value = insert(table, values)
                            count[0] += 1
                            if count[0] == position:
                                raise error('after successful insert')
                            return value
                        with patch.object(recorder.store, '_insert', side_effect=fail):
                            with self.assertRaises(RecorderStoreError if error is sqlite3.IntegrityError else OSError):
                                original(recorder, result)
                        self.assertEqual(recorder._research_mapping, {})
                        recorder.store.close()
                        self.store = RecorderStore.open_approximate(self.db_path)
                        for table, rows in before.items():
                            self.assertEqual(self.store._rows('SELECT * FROM ' + table), rows)
                        for _ in range(2):
                            self.store.recover_interrupted(wall_us=NOW_US + 30_000_000,
                                run_id=self.run_id, finish_runs=False)
                        terminal = [json.loads(row['detail_json']) for row in self.store._rows(
                            'SELECT detail_json FROM recorder_events WHERE run_id=?', (self.run_id,))
                            if json.loads(row['detail_json']).get('type') == 'APPROXIMATE_OPPORTUNITY_COMPLETION']
                        self.assertEqual(len(terminal), 1)
                        self.assertEqual(terminal[0]['disposition'], 'interrupted')
                        self.assertNotIn('reference_id', terminal[0])
                        self.assertNotIn('completion_ns', terminal[0])
                        raise OSError('checked crash boundary')
                    with patch.object(Recorder, '_persist_research', persist), self.assertRaises(OSError):
                        self.test_revision_two_recorder_flow()

    def test_stopped_submit_never_leaves_unexplained_pending_owner(self):
        from polymarket_bot.recorder.service import ApproximateReferenceWorker
        self.test_revision_two_recorder_flow()
        self.recorder.approximate_worker = ApproximateReferenceWorker()
        self.recorder.approximate_worker.stop()
        self.assertFalse(self.recorder._approximate_submit(20_000_000_000, target_ns=14_000_000_000))
        import json
        rows = [json.loads(row['detail_json']) for row in self.store._rows('SELECT detail_json FROM recorder_events')]
        last = [row for row in rows if row.get('type') == 'APPROXIMATE_OPPORTUNITY'][-1]
        self.assertEqual(last['disposition'], 'unsubmitted')

    def test_commit_failure_rolls_back_rows_and_keeps_memory(self):
        import sqlite3
        from unittest.mock import patch
        from polymarket_bot.recorder.service import Recorder
        original = Recorder._persist_research
        def persist(recorder, result):
            connection = recorder.store._connection
            before = [recorder.store._rows('SELECT * FROM ' + table) for table in
                ('approximate_observations', 'approximate_references', 'recorder_events')]
            class Connection:
                def __getattr__(self, name):
                    return getattr(connection, name)
                def commit(self):
                    raise sqlite3.OperationalError('commit interrupted')
            recorder.store._connection = Connection()
            try:
                with self.assertRaises(RecorderStoreError):
                    original(recorder, result)
            finally:
                recorder.store._connection = connection
            self.assertTrue(recorder.store._write_failed)
            self.assertEqual(recorder._research_mapping, {})
            recorder.store.close()
            self.store = RecorderStore.open_approximate(self.db_path)
            self.assertEqual([self.store._rows('SELECT * FROM ' + table) for table in
                ('approximate_observations', 'approximate_references', 'recorder_events')], before)
            raise OSError('checked commit failure')
        with patch.object(Recorder, '_persist_research', persist), self.assertRaises(OSError):
            self.test_revision_two_recorder_flow()

    def test_live_worker_timeout_retains_store_and_thread_ownership(self):
        from threading import Event
        from polymarket_bot.recorder.service import ApproximateReferenceWorker
        from polymarket_bot.market_data.models import MarketDataError
        self.test_revision_two_recorder_flow()
        release = Event()
        worker = self.recorder.approximate_worker = ApproximateReferenceWorker()
        try:
            self.assertTrue(worker.submit(lambda: release.wait(2)))
            with self.assertRaises(MarketDataError):
                worker.stop(timeout=0)
            assert worker._thread is not None
            self.assertTrue(worker._thread.is_alive())
            self.assertFalse(self.store._closed)
            self.assertFalse(worker.submit(lambda: None))
        finally:
            release.set()
            worker.stop()
            worker.take()

    def test_deadline_clock_and_overflow_returns_are_explicit(self):
        from dataclasses import replace
        from unittest.mock import patch
        self.test_revision_two_recorder_flow()
        recorder = self.recorder
        assert recorder.clock.anchor is not None
        for anchor in (None, replace(recorder.clock.anchor, status='DISCONTINUITY')):
            with patch.object(recorder.clock, 'anchor', anchor):
                self.assertFalse(recorder._approximate_submit(20_000_000_000, target_ns=14_000_000_000))
                self.assertEqual(recorder._research_evidence.scheduled_opportunities[-1][2], 'unsubmitted')
        with patch.object(recorder, 'monotonic_ns', return_value=20_000_000_000):
            self.assertFalse(recorder._approximate_submit(20_000_000_000, target_ns=16_000_000_000))
        for index in range(124):
            recorder._research_opportunity(18_000_000_000 + index * 2_000_000_000, 'skipped')
        with self.assertRaises(ValueError):
            recorder._research_opportunity(300_000_000_000, 'busy')

    def test_pending_rollover_preserves_original_owner_and_fresh_run_grid(self):
        from threading import Event
        from unittest.mock import patch
        from polymarket_bot.recorder.service import ApproximateReferenceWorker
        import json
        def before_drain(recorder, result):
            pending = recorder._research_pending
            origin = recorder.approximate_reference.sampling_origin_ns
            release, entered = Event(), Event()
            worker = recorder.approximate_worker = ApproximateReferenceWorker()
            def work():
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('rollover barrier timed out')
                return result
            try:
                self.assertTrue(worker.submit(work))
                self.assertTrue(entered.wait(2))
                recorder._research_opportunity(12_000_000_000, 'busy', market_id='next-market', slot_index=0)
                self.assertEqual(recorder._research_pending, pending)
                self.assertEqual(recorder.approximate_reference.sampling_origin_ns, origin)
                release.set()
                worker.stop()
                recorder._approximate_drain()
                fetch = [json.loads(row['detail_json']) for row in self.store._rows('SELECT detail_json FROM recorder_events')
                    if json.loads(row['detail_json']).get('type') == 'APPROXIMATE_FETCH'][-1]
                self.assertEqual(fetch['opportunity_id'], pending[0])
                self.assertEqual(fetch['target_ns'], pending[1]['target_ns'])
                recorder.config = RunConfig(mode='APPROXIMATE_REFERENCE')
                with patch.object(recorder.clock, 'deadline_ns', return_value=20_000_000_000):
                    self.assertEqual(recorder._nominal_target_ns(900_000_000), 20_000_000_000)
                    self.assertEqual(recorder._nominal_target_ns(902_000_000), 22_000_000_000)
            finally:
                release.set()
                worker.stop()
            raise OSError('checked pending rollover')
        with self.assertRaises(OSError):
            self.test_revision_two_recorder_flow(before_drain=before_drain)

    def test_completed_undrained_rollover_binds_changed_market(self):
        from dataclasses import replace
        from unittest.mock import patch
        from polymarket_bot.recorder.service import ApproximateReferenceWorker
        import json
        def before_drain(recorder, result):
            pending = recorder._research_pending
            origin = recorder.approximate_reference.sampling_origin_ns
            worker = recorder.approximate_worker = ApproximateReferenceWorker()
            self.assertTrue(worker.submit(lambda: result))
            assert worker._thread is not None
            worker._thread.join(2)
            self.assertFalse(worker._thread.is_alive())
            recorder.revision = None
            with patch.object(recorder, 'monotonic_ns', return_value=12_000_000_000):
                self.assertFalse(recorder._approximate_submit(20_000_000_000, target_ns=12_000_000_000,
                    market_id='changed-market', slot_index=0, scheduled_us=NOW_US + 12_000_000))
            self.assertEqual(recorder._research_evidence.scheduled_opportunities[-1][2], 'busy')
            self.assertEqual(recorder._research_pending, pending)
            recorder._approximate_drain()
            fetch = [json.loads(row['detail_json']) for row in self.store._rows('SELECT detail_json FROM recorder_events')
                if json.loads(row['detail_json']).get('type') == 'APPROXIMATE_FETCH'][-1]
            for key in ('market_id', 'slot_id', 'slot_index', 'target_ns', 'scheduled_us'):
                self.assertEqual(fetch.get(key), pending[1].get(key))
            self.assertEqual(fetch['opportunity_id'], pending[0])
            self.assertEqual(recorder.approximate_reference.sampling_origin_ns, origin)
            recorder.config = RunConfig(mode='APPROXIMATE_REFERENCE')
            with patch.object(recorder.clock, 'deadline_ns', return_value=20_000_000_000):
                self.assertEqual(recorder._nominal_target_ns(900_000_000), 20_000_000_000)
                self.assertEqual(recorder._nominal_target_ns(902_000_000), 22_000_000_000)
            from tests.test_cp5_safety import CP5SafetyTests
            rows = tuple(self.store._rows('SELECT * FROM ' + table) for table in
                ('approximate_observations', 'approximate_references', 'recorder_events', 'clock_anchors'))
            self.assertTrue(CP5SafetyTests.analyzer().replay_rows(*rows)['exact'])
            raise OSError('completed rollover checked')
        with self.assertRaises(OSError):
            self.test_revision_two_recorder_flow(before_drain=before_drain,
                submit_identity=dict(market_id='original-market', slot_index=299, scheduled_us=NOW_US + 10_000_000))

    def test_padded_dispositions_remain_aligned_through_delayed_drain(self):
        from dataclasses import replace
        from unittest.mock import patch
        self.test_revision_two_recorder_flow(cycles=40)
        recorder = self.recorder
        for disposition, target in (('skipped', 28_000_000_001), ('busy', 30_000_000_000), ('unsubmitted', 32_000_000_000)):
            opportunity = recorder._research_opportunity(target, disposition)
            with patch.object(recorder, 'monotonic_ns', return_value=1_000_000_000_000):
                recorder._research_opportunity(90_000_000_000, 'busy')
            self.assertIn(opportunity, {row[0] for row in recorder._research_evidence.scheduled_opportunities})
        evidence = recorder._research_evidence
        self.assertEqual(len(evidence.observation_ids), len(evidence.contexts))
        self.assertEqual(set(evidence.observation_ids), set(recorder._research_mapping))
        self.assertEqual({(op, name) for op, name, _, _, _ in evidence.request_outcomes}, set(evidence.expected_requests))

    def test_submission_requires_nominal_and_active_utc_opening_and_run_budget(self):
        from unittest.mock import patch
        from polymarket_bot.recorder.service import ApproximateReferenceWorker
        self.test_revision_two_recorder_flow()
        recorder = self.recorder
        for now, target, scheduled, run_end in ((13_000_000_000, 14_000_000_000, NOW_US + 14_000_000, 30_000_000_000),
                (14_000_000_000, 14_000_000_000, NOW_US + 15_000_000, 30_000_000_000),
                (16_000_000_000, 14_000_000_000, NOW_US + 14_000_000, 30_000_000_000),
                (14_000_000_000, 14_000_000_000, NOW_US + 14_000_000, 14_000_000_000)):
            with self.subTest(now=now, target=target, scheduled=scheduled, run_end=run_end):
                recorder.approximate_worker = ApproximateReferenceWorker()
                recorder.run_deadline_ns = run_end
                with patch.object(recorder, 'monotonic_ns', return_value=now):
                    accepted = recorder._approximate_submit(30_000_000_000, target_ns=target,
                        scheduled_us=scheduled, market_id='guard', slot_index=1)
                recorder.approximate_worker.stop()
                self.assertFalse(accepted)
                self.assertIsNone(recorder.approximate_worker._thread)
                self.assertEqual(recorder._research_evidence.scheduled_opportunities[-1][2], 'unsubmitted')

    def test_fresh_recorder_projection_and_expired_drain_keep_captured_owner(self):
        from unittest.mock import patch
        from polymarket_bot.recorder.service import Recorder
        def before_drain(recorder, result):
            recorder.config = RunConfig(mode='APPROXIMATE_REFERENCE')
            with patch.object(recorder.clock, 'deadline_ns', return_value=20_000_000_000):
                self.assertEqual(recorder._nominal_target_ns(1_000_000), 20_000_000_000)
            fresh = Recorder(self.store, recorder.public, monotonic_ns=recorder.monotonic_ns,
                wall_us=recorder.wall_us, sleep=lambda _: None)
            fresh.config = RunConfig(mode='APPROXIMATE_REFERENCE')
            with patch.object(fresh.clock, 'deadline_ns', return_value=99_000_000_000):
                self.assertEqual(fresh._nominal_target_ns(1_000_000), 99_000_000_000)
            self.assertFalse(hasattr(fresh, '_research_pending'))
            with patch.object(recorder.clock, 'usable', return_value=False):
                recorder._approximate_drain(result=result)
            self.assertEqual(len(self.store._rows('SELECT * FROM approximate_references')), 1)
            raise OSError('captured result survived current clock expiry')
        with self.assertRaises(OSError):
            self.test_revision_two_recorder_flow(before_drain=before_drain)

    def test_opening_equality_accepts_but_end_and_run_equality_never_send(self):
        from unittest.mock import patch
        from polymarket_bot.recorder.service import ApproximateReferenceWorker
        def before_drain(recorder, result):
            recorder._approximate_drain(result=result)
            recorder.approximate_worker = ApproximateReferenceWorker()
            now = recorder.monotonic_ns()
            self.assertTrue(recorder._approximate_submit(now + 1_000_000_000, target_ns=now,
                scheduled_us=NOW_US + now // 1000))
            recorder.approximate_worker.stop()
            recorder._approximate_drain()
            original = recorder._research_pending
            for limit in (now, now - 1):
                recorder.approximate_worker = ApproximateReferenceWorker()
                recorder.run_deadline_ns = limit
                self.assertFalse(recorder._approximate_submit(now + 1_000_000_000, target_ns=now))
                self.assertIsNone(recorder.approximate_worker._thread)
                self.assertEqual(recorder._research_pending, original)
            raise OSError('opening and deadline boundaries checked')
        with self.assertRaises(OSError):
            self.test_revision_two_recorder_flow(before_drain=before_drain)

    def test_duplicate_actual_drain_preserves_every_row_and_memory(self):
        import json
        self.test_revision_two_recorder_flow(cycles=2)
        recorder = self.recorder
        before = {table: self.store._rows('SELECT * FROM ' + table) for table in
            ('approximate_observations', 'approximate_references', 'recorder_events')}
        evidence, mapping = recorder._research_evidence, dict(recorder._research_mapping)
        recorder._approximate_drain()
        self.assertEqual({table: self.store._rows('SELECT * FROM ' + table) for table in before}, before)
        self.assertEqual(recorder._research_evidence, evidence)
        self.assertEqual(recorder._research_mapping, mapping)
        from tests.test_cp5_safety import CP5SafetyTests
        rows = tuple(self.store._rows('SELECT * FROM ' + table) for table in
            ('approximate_observations', 'approximate_references', 'recorder_events', 'clock_anchors'))
        self.assertTrue(CP5SafetyTests.analyzer().replay_rows(*rows)['exact'])

    def test_rejected_worker_submission_has_explicit_terminal(self):
        import json
        from unittest.mock import patch
        from polymarket_bot.recorder.service import ApproximateReferenceWorker
        self.test_revision_two_recorder_flow()
        recorder = self.recorder
        recorder.approximate_worker = ApproximateReferenceWorker()
        with patch.object(recorder.approximate_worker, 'submit', return_value=False):
            self.assertFalse(recorder._approximate_submit(20_000_000_000, target_ns=10_000_000_000))
        opportunity = recorder._research_evidence.scheduled_opportunities[-1][0]
        terminals = [json.loads(row['detail_json']) for row in self.store._rows('SELECT detail_json FROM recorder_events')
            if json.loads(row['detail_json']).get('opportunity_id') == opportunity
            and json.loads(row['detail_json']).get('type') == 'APPROXIMATE_OPPORTUNITY_COMPLETION']
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]['disposition'], 'interrupted')
        self.assertNotIn('reference_id', terminals[0])

    def test_padded_skipped_authority_survives_owner_delay(self):
        from unittest.mock import patch
        self.test_revision_two_recorder_flow()
        recorder = self.recorder
        skipped = recorder._research_opportunity(12_000_000_000, 'skipped')
        with patch.object(recorder, 'monotonic_ns', return_value=73_000_000_000):
            recorder._research_opportunity(74_000_000_000, 'busy')
        self.assertIn(skipped, {row[0] for row in recorder._research_evidence.scheduled_opportunities})
        with patch.object(recorder, 'monotonic_ns', return_value=200_000_000_000):
            recorder._research_opportunity(76_000_000_000, 'unsubmitted')
        self.assertIn(skipped, {row[0] for row in recorder._research_evidence.scheduled_opportunities})

    def test_producer_rejects_omitted_and_duplicate_authority(self):
        from dataclasses import replace
        self.test_revision_two_recorder_flow()
        result = self.research_result
        assert result is not None
        opportunity = result.evidence.scheduled_opportunities[0]
        for rows in ((), (opportunity, opportunity)):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.recorder._validate_research_result(replace(result,
                    evidence=replace(result.evidence, scheduled_opportunities=rows)))

    def test_publication_rolls_back_each_write_failure(self):
        from unittest.mock import patch
        from polymarket_bot.recorder.service import Recorder
        original = Recorder._persist_research
        tables = ('approximate_observations', 'approximate_references', 'recorder_events')
        for method in ('save_approximate_observation', 'save_approximate_reference', 'fetch', 'completion'):
            with self.subTest(method=method):
                self.run_id = self.store.start_run(RunConfig(mode='APPROXIMATE_REFERENCE'),
                    wall_us=NOW_US, app_revision='fault')
                def persist(recorder, result):
                    before = [recorder.store._rows('SELECT * FROM ' + table) for table in tables]
                    write = recorder.store.record_event
                    def event(*args, **kwargs):
                        kind = kwargs.get('detail', {}).get('type')
                        if kind == ('APPROXIMATE_FETCH' if method == 'fetch' else 'APPROXIMATE_OPPORTUNITY_COMPLETION'):
                            raise OSError('publication interrupted')
                        return write(*args, **kwargs)
                    name = method if method.startswith('save_') else 'record_event'
                    failure = OSError('publication interrupted') if method.startswith('save_') else event
                    with patch.object(recorder.store, name, side_effect=failure):
                        with self.assertRaises(OSError):
                            original(recorder, result)
                    after = [recorder.store._rows('SELECT * FROM ' + table) for table in tables]
                    self.assertEqual(after, before)
                    self.assertEqual(recorder._research_mapping, {})
                    raise OSError('stop fixture after checked failure')
                with patch.object(Recorder, '_persist_research', persist):
                    with self.assertRaises(OSError):
                        self.test_revision_two_recorder_flow()

    def test_duplicate_drain_and_changed_submission_fail_closed(self):
        from unittest.mock import patch
        from polymarket_bot.recorder.service import Recorder
        original = Recorder._persist_research
        def persist(recorder, result):
            opportunity, identity, attempts = recorder._research_pending
            for changed in (dict(identity, target_ns=identity['target_ns'] + 1),
                    dict(identity, market_id='foreign'), dict(identity, slot_id=True)):
                recorder._research_pending = opportunity, changed, attempts
                recorder._research_mapping = {}
                with self.subTest(identity=changed):
                    with self.assertRaises(RuntimeError):
                        with recorder.store._transaction():
                            with self.assertRaises(ValueError):
                                original(recorder, result)
                            raise RuntimeError('rollback mutation fixture')
            recorder._research_pending = opportunity, identity, attempts
            original(recorder, result)
            before = recorder.store._rows('SELECT * FROM approximate_references')
            recorder._research_mapping = {}
            with self.assertRaises(ValueError):
                original(recorder, result)
            self.assertEqual(recorder.store._rows('SELECT * FROM approximate_references'), before)
        with patch.object(Recorder, '_persist_research', persist):
            self.test_revision_two_recorder_flow()

    def test_pending_opportunity_recovery_is_explicit_and_idempotent(self):
        import json
        self.test_revision_two_recorder_flow()
        pending = self.recorder._research_opportunity(14_000_000_000, 'submitted',
            market_id='pending', slot_index=7)
        for _ in range(2):
            self.store.recover_interrupted(wall_us=NOW_US + 20_000_000, run_id=self.run_id, finish_runs=False)
        rows = [json.loads(row['detail_json']) for row in self.store._rows('SELECT detail_json FROM recorder_events')]
        terminal = [row for row in rows if row.get('type') == 'APPROXIMATE_OPPORTUNITY_COMPLETION'
            and row.get('opportunity_id') == pending]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]['disposition'], 'interrupted')
        self.assertNotIn('reference_id', terminal[0])

    def test_opportunity_failure_prevents_worker_and_completed_worker_stays_busy(self):
        from unittest.mock import patch
        self.test_revision_two_recorder_flow()
        recorder = self.recorder
        from polymarket_bot.recorder.service import ApproximateReferenceWorker
        recorder.approximate_worker = ApproximateReferenceWorker()
        with patch.object(self.store, 'record_event', side_effect=OSError('opportunity interrupted')):
            with self.assertRaises(OSError):
                recorder._approximate_submit(20_000_000_000, target_ns=14_000_000_000)
        self.assertIsNone(recorder.approximate_worker._thread)
        self.assertTrue(recorder.approximate_worker.submit(lambda: None))
        recorder.approximate_worker.stop()
        self.assertFalse(recorder._approximate_submit(20_000_000_000, target_ns=10_000_000_000))
        self.assertEqual(recorder._research_evidence.scheduled_opportunities[-1][2], 'busy')
        recorder._approximate_drain(stopping=True)

    def test_shutdown_drains_pending_research_atomically(self):
        from unittest.mock import patch
        from polymarket_bot.recorder.service import Recorder
        original = Recorder._persist_research
        def persist(recorder, result):
            self.assertIsNone(recorder.approximate_worker._thread)
            return original(recorder, result)
        with patch.object(Recorder, '_persist_research', persist):
            self.test_revision_two_recorder_flow()
        self.recorder._shutdown('STOPPED', 'fixture')
        import json
        completions = [json.loads(row['detail_json']) for row in self.store._rows('SELECT detail_json FROM recorder_events')
            if json.loads(row['detail_json']).get('type') == 'APPROXIMATE_OPPORTUNITY_COMPLETION']
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0]['disposition'], 'completed')

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

    def test_provider_retry_deadline_equality_never_sends(self):
        import httpx
        from polymarket_bot.recorder.free_reference import CoinbaseRestProvider, KrakenRestProvider
        for provider_class in (CoinbaseRestProvider, KrakenRestProvider):
            for phase in ('initial', 'response', 'backoff', 'retry_send', 'retry_receipt'):
                for failure in ('429', 'timeout', 'transport'):
                    with self.subTest(provider=provider_class.provider_name, phase=phase, failure=failure):
                        now, sends = [0], []
                        end = 1_000_000_000
                        def respond(request):
                            sends.append(now[0])
                            if len(sends) == 1:
                                if phase == 'response':
                                    now[0] = end
                                elif phase == 'backoff':
                                    now[0] = end - 100_000_000
                                if failure == 'timeout':
                                    raise httpx.ReadTimeout('offline', request=request)
                                if failure == 'transport':
                                    raise httpx.ConnectError('offline', request=request)
                                return httpx.Response(429)
                            now[0] = end
                            return httpx.Response(200, json={})
                        def sleep(seconds):
                            now[0] = end if phase == 'retry_send' else end - 1
                        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                            provider = provider_class(client, monotonic_ns=lambda: now[0], wall_us=lambda: NOW_US, sleep=sleep)
                            if phase == 'initial':
                                now[0] = end
                            observation = provider.fetch_ticker(deadline=1.0)
                        self.assertEqual(len(sends), 0 if phase == 'initial' else 2 if phase == 'retry_receipt' else 1)
                        self.assertTrue(all(send < end for send in sends))
                        self.assertEqual(observation.result, 'error')
                        self.assertIsNone(observation.price)
                        if phase in ('initial', 'retry_send', 'retry_receipt') or (phase == 'response' and failure == '429'):
                            self.assertEqual(observation.error_code, 'DEADLINE_EXPIRED')

    def test_delayed_window_dispositions_replay_exactly(self):
        import json
        from tests.test_cp5_safety import CP5SafetyTests
        dispositions = {9: 'skipped', 10: 'busy', 11: 'unsubmitted'}
        self.test_revision_two_recorder_flow(cycles=40, dispositions=dispositions, drain_delay_ns=200_000_000_000)
        rows = tuple(self.store._rows('SELECT * FROM ' + table) for table in
            ('approximate_observations', 'approximate_references', 'recorder_events', 'clock_anchors'))
        replay = CP5SafetyTests.analyzer().replay_rows(*rows)
        self.assertTrue(replay['exact'], replay)
        fetch = [json.loads(row['detail_json']) for row in rows[2]
            if json.loads(row['detail_json']).get('type') == 'APPROXIMATE_FETCH'][-1]
        members = fetch['evidence']['scheduled_opportunities']
        self.assertTrue(set(dispositions.values()).issubset({row[2] for row in members}))
        evidence = self.recorder._research_evidence
        self.assertEqual(len(evidence.observation_ids), len(evidence.contexts))
        self.assertEqual(set(evidence.observation_ids), set(self.recorder._research_mapping))
        self.assertEqual({(op, name) for op, name, _, _, _ in evidence.request_outcomes}, set(evidence.expected_requests))
        self.assertTrue(set(dispositions.values()).issubset({row[2] for row in evidence.scheduled_opportunities}))

    def test_revision_two_recorder_flow(self, cycles=1, delay_ns=0, inflight=False, before_drain=None, dispositions=None, drain_delay_ns=0, submit_identity=None):
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
            self.assertTrue(recorder._approximate_submit(mono[0] + 2_000_000_000, **(submit_identity or {})))
            recorder.approximate_worker.stop()
            self.recorder = recorder
            self.research_result = recorder.approximate_worker.take()
            if inflight:
                from dataclasses import replace
                assert self.research_result is not None
                recorder._research_evidence = replace(recorder._research_evidence,
                    discontinuity_ends_ns=recorder._research_evidence.discontinuity_ends_ns + (self.research_result.report.receipt_mono_ns + 1,))
            mono[0] += 1_000_000_000
            if before_drain is not None:
                before_drain(recorder, self.research_result)
            recorder._approximate_drain(result=self.research_result)
            for index in range(1, cycles):
                from polymarket_bot.recorder.service import ApproximateReferenceWorker
                mono[0] = 10_000_000_000 + index * 2_000_000_000
                if dispositions and index in dispositions:
                    recorder._research_opportunity(mono[0], dispositions[index])
                    continue
                recorder.approximate_worker = ApproximateReferenceWorker()
                self.assertTrue(recorder._approximate_submit(mono[0] + 2_000_000_000, **(submit_identity or {})))
                recorder.approximate_worker.stop()
                mono[0] += drain_delay_ns
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