import threading
import unittest
from decimal import Decimal
from typing import Any
from unittest.mock import Mock

from polymarket_bot.recorder.free_reference import ApproximateObservation, ApproximateReferenceReport
from polymarket_bot.recorder.models import RunConfig, CHAINLINK_FEED_ID, CHAINLINK_FEED_NAME
from polymarket_bot.recorder.reference import OfflineReferenceSession, ActivationEvidence
from polymarket_bot.recorder.service import Recorder
from polymarket_bot.recorder.store import RecorderStore, RecorderStoreError
from tests.recorder_support import RecorderFixture


class FreeModeIsolationTests(unittest.TestCase):
    def fixture(self, approximate=False):
        fixture: Any = RecorderFixture()
        self.addCleanup(fixture.close)
        if approximate:
            fixture.store.close()
            fixture.db.path.unlink()
            fixture.store = RecorderStore.open_approximate(fixture.db.path)
        return fixture

    def recorder(self, fixture, **kwargs) -> Any:
        return Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, **kwargs)

    def test_failed_join_keeps_store_open_and_worker_handle(self):
        from polymarket_bot.market_data.models import MarketDataError
        fixture = self.fixture(True)
        recorder = self.recorder(fixture, approximate_reference=Mock())
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def fetch(**kwargs):
            entered.set()
            release.wait(10)
            return None
        recorder.approximate_reference.fetch.side_effect = fetch
        def hook(request, payload, f):
            if request.url.path == '/books':
                entered.wait(1)
        fixture.hook = hook
        from unittest.mock import patch
        from polymarket_bot.recorder.service import ApproximateReferenceWorker
        original = ApproximateReferenceWorker.stop
        def execute(*args, **kwargs):
            recorder.approximate_worker = ApproximateReferenceWorker()
            recorder.approximate_worker.submit(lambda: fetch())
            self.assertTrue(entered.wait(1))
            raise MarketDataError('original_failure', 'original')
        with patch.object(recorder, '_record', side_effect=execute), patch.object(
                ApproximateReferenceWorker, 'stop', lambda worker: original(worker, timeout=0.01)):
            result = recorder.run(RunConfig(mode='APPROXIMATE_REFERENCE'), should_stop=lambda: False)
            self.assertEqual(result.reason, 'original_failure')
        self.assertFalse(fixture.store._closed)
        self.assertIsNotNone(recorder.approximate_worker._thread)
        release.set()
        recorder.approximate_worker.stop()

    def test_storage_failure_survives_failed_worker_stop(self):
        from unittest.mock import patch
        from polymarket_bot.recorder.service import ApproximateReferenceWorker
        fixture = self.fixture(True)
        recorder = self.recorder(fixture, approximate_reference=Mock())
        release = threading.Event()
        self.addCleanup(release.set)
        def record(*args, **kwargs):
            recorder.approximate_worker.submit(lambda: release.wait(10))
            raise RecorderStoreError('storage_failure', 'original storage error')
        stop = ApproximateReferenceWorker.stop
        with patch.object(recorder, '_record', side_effect=record), patch.object(
                ApproximateReferenceWorker, 'stop', lambda worker: stop(worker, timeout=0.01)):
            result = recorder.run(RunConfig(mode='APPROXIMATE_REFERENCE'), should_stop=lambda: False)
        self.assertEqual(result.reason, 'storage_failure')
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(fixture.store._closed)
        release.set()
        recorder.approximate_worker.stop()

    def test_run_deadline_stops_during_worker_and_joins(self):
        from unittest.mock import patch
        fixture = self.fixture(True)
        session = Mock()
        recorder = self.recorder(fixture, approximate_reference=session)
        entered, release = threading.Event(), threading.Event()
        def fetch(**kwargs):
            entered.set()
            self.assertLessEqual(kwargs['deadline'], recorder.run_deadline_ns / 1000000000)
            release.wait(1)
            return None
        session.fetch.side_effect = fetch
        def record(config, *, should_stop):
            recorder.run_id = fixture.store.start_run(config, wall_us=fixture.wall_us(), app_revision='pilot-test')
            recorder._calibrate(recorder.public.monotonic() + 10)
            recorder._approximate_submit(recorder.run_deadline_ns + 10000000000)
            self.assertTrue(entered.wait(1))
            fixture.sleep(2)
            self.assertTrue(should_stop())
            release.set()
            recorder._shutdown('STOPPED', 'STOP_REQUESTED')
            from polymarket_bot.recorder.models import RunResult
            return RunResult('', 'STOPPED', 0, 0, 'STOP_REQUESTED')
        with patch.object(recorder, '_record', side_effect=record):
            result = recorder.run(RunConfig(mode='APPROXIMATE_REFERENCE'), should_stop=lambda: False,
                                  deadline_ns=fixture.monotonic_ns() + 1000000000)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(recorder.approximate_worker._thread is not None
                         and recorder.approximate_worker._thread.is_alive())
        self.assertTrue(fixture.store._closed)

    def test_stop_and_interrupt_recovery_errors_survive_failed_join(self):
        from unittest.mock import patch
        from polymarket_bot.recorder.service import ApproximateReferenceWorker, _StopRequested
        for signal in (_StopRequested, KeyboardInterrupt):
            fixture = self.fixture(True)
            recorder = self.recorder(fixture, approximate_reference=Mock())
            release = threading.Event()
            self.addCleanup(release.set)
            def record(config, **kwargs):
                recorder.run_id = fixture.store.start_run(config, wall_us=fixture.wall_us(), app_revision='recovery')
                recorder.approximate_worker.submit(lambda: release.wait(10))
                raise signal()
            stop = ApproximateReferenceWorker.stop
            with patch.object(recorder, '_record', side_effect=record), patch.object(
                    fixture.store, 'recover_interrupted', side_effect=RecorderStoreError('recovery_failure', 'original')), patch.object(
                    ApproximateReferenceWorker, 'stop', lambda worker: stop(worker, timeout=0.01)):
                result = recorder.run(RunConfig(mode='APPROXIMATE_REFERENCE'), should_stop=lambda: False)
            self.assertEqual(result.reason, 'recovery_failure')
            self.assertEqual(result.exit_code, 1)
            self.assertFalse(fixture.store._closed)
            release.set()
            recorder.approximate_worker.stop()

    def test_book_only_no_approximate_worker(self):
        for session in (None, Mock()):
            fixture = self.fixture()
            recorder = self.recorder(fixture, approximate_reference=session)
            result = recorder.run(RunConfig(), should_stop=lambda: fixture.book_count >= 1)
            self.assertEqual(result.exit_code, 0)
            self.assertIsNone(recorder.approximate_worker)
            if session is not None:
                session.fetch.assert_not_called()
                session.start.assert_not_called()

    def test_reference_required_no_approximate_worker(self):
        fixture = self.fixture()
        session = OfflineReferenceSession(price_scale=8, decoder_revision='structural',
            discovery_payload={'feedId': CHAINLINK_FEED_ID, 'name': CHAINLINK_FEED_NAME,
                'schemaVersion': 2, 'decimals': 8, 'status': 'active'},
            evidence=ActivationEvidence(*([True] * 6)), latest=lambda deadline: None,
            monotonic_ns=fixture.monotonic_ns, wall_us=fixture.wall_us)
        recorder = self.recorder(fixture, offline_reference=session)
        result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'),
            should_stop=lambda: fixture.book_count >= 1)
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(recorder.approximate_worker)
        self.assertIsNotNone(recorder.reference_worker)

    def test_approximate_mode_starts_worker(self):
        fixture = self.fixture(True)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        owner = threading.get_ident()
        session = Mock()
        def fetch(**kwargs):
            self.assertNotEqual(threading.get_ident(), owner)
            self.assertGreater(kwargs['deadline'], fixture.monotonic_ns() / 1_000_000_000)
            entered.set()
            self.assertTrue(release.wait(2))
            wall = fixture.wall_us()
            observation = ApproximateObservation('coinbase', 'BTC/USD', 'USD', Decimal('50000'),
                None, None, None, wall, kwargs['project'](fixture.monotonic_ns()),
                'abc', 3, 'rest', 'test-v1', 200, 'success', None)
            report = ApproximateReferenceReport('APPROXIMATE_SAMPLE_MEAN_60S', wall-60_000_000,
                wall, 1, 'coinbase', False, wall, Decimal('50000'))
            return report, (observation,)
        session.fetch.side_effect = fetch
        def books(request, payload, f):
            if request.url.path == '/books':
                self.assertTrue(entered.wait(1))
                self.assertFalse(release.is_set())
                release.set()
        fixture.hook = books
        recorder = self.recorder(fixture, approximate_reference=session)
        result = recorder.run(RunConfig(mode='APPROXIMATE_REFERENCE'),
            should_stop=lambda: fixture.book_count >= 1)
        self.assertEqual((result.exit_code, result.slots_finalized), (0, 1))
        self.assertIsNone(recorder.reference_worker)
        self.assertTrue(recorder.approximate_worker._stopped)
        self.assertEqual(len(fixture.rows('SELECT * FROM approximate_observations')), 1)
        self.assertEqual(len(fixture.rows('SELECT * FROM approximate_references')), 1)
        self.assertEqual(fixture.rows('SELECT * FROM reference_fetches'), [])
        self.assertEqual(fixture.rows('SELECT * FROM reference_reports'), [])

    def test_approximate_mode_rejects_if_no_session(self):
        fixture = self.fixture()
        result = self.recorder(fixture).run(RunConfig(mode='APPROXIMATE_REFERENCE'),
            should_stop=lambda: True)
        self.assertEqual(result.reason, 'approximate_session_required')
        self.assertEqual(fixture.requests, [])

    def test_mixed_modes_rejected(self):
        fixture = self.fixture()
        session = Mock()
        result = self.recorder(fixture, approximate_reference=session).run(
            RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: True)
        self.assertEqual(result.reason, 'mixed_modes_rejected')
        session.fetch.assert_not_called()
        self.assertEqual(fixture.requests, [])

    def test_approximate_mode_requires_v2(self):
        fixture = self.fixture()
        fixture.store.close()
        with self.assertRaises(RecorderStoreError) as caught:
            RecorderStore.open_approximate(fixture.db.path)
        self.assertEqual(caught.exception.code, 'schema_version_mismatch')
        fixture.store = RecorderStore.open(fixture.db.path)
        result = self.recorder(fixture, approximate_reference=Mock()).run(
            RunConfig(mode='APPROXIMATE_REFERENCE'), should_stop=lambda: True)
        self.assertEqual(result.reason, 'schema_version_mismatch')
        self.assertEqual(fixture.requests, [])
