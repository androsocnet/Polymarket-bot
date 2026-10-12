import threading
import unittest
from decimal import Decimal
from unittest.mock import Mock

from polymarket_bot.recorder.free_reference import ApproximateObservation, ApproximateReferenceReport
from polymarket_bot.recorder.models import RunConfig, CHAINLINK_FEED_ID, CHAINLINK_FEED_NAME
from polymarket_bot.recorder.reference import OfflineReferenceSession, ActivationEvidence
from polymarket_bot.recorder.service import Recorder
from polymarket_bot.recorder.store import RecorderStore, RecorderStoreError
from tests.recorder_support import RecorderFixture


class FreeModeIsolationTests(unittest.TestCase):
    def fixture(self, approximate=False):
        fixture = RecorderFixture()
        self.addCleanup(fixture.close)
        if approximate:
            fixture.store.close()
            fixture.db.path.unlink()
            fixture.store = RecorderStore.open_approximate(fixture.db.path)
        return fixture

    def recorder(self, fixture, **kwargs):
        return Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, **kwargs)

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
            self.assertGreater(kwargs['deadline'], fixture.wall_us() / 1_000_000)
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
