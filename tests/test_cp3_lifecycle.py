"""Offline CP3 lifecycle regressions; credentials and resources are synthetic."""
import unittest

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.recorder.models import CHAINLINK_FEED_ID, CHAINLINK_FEED_NAME, RunConfig
from polymarket_bot.recorder.service import Recorder
from tests.recorder_support import NOW_US, RecorderFixture
from tests.test_schema_v2_decoder import managed_authorization


class Rest:
    def close(self):
        pass


class Live:
    def __init__(self):
        self.cleanup_failed = False
        self.terminal = False

    def start(self):
        pass

    def stop(self):
        if self.cleanup_failed:
            raise OSError('synthetic cleanup failure')

    def check_health(self):
        if self.terminal:
            raise MarketDataError('websocket_failed', 'Synthetic terminal worker failure.')

    def get_connection_gaps(self):
        return ()

    def get_freshest_report(self, *args):
        return None


class CP3LifecycleTests(unittest.TestCase):
    def session(self, *, live=None, credentials=None, fixture=None):
        from polymarket_bot.recorder.reference import ActivationEvidence, ManagedReferenceSession
        return ManagedReferenceSession(evidence=ActivationEvidence(*([True]*6)),
            discovery=lambda deadline: {'feedId': CHAINLINK_FEED_ID, 'name': CHAINLINK_FEED_NAME,
                'status': 'active', 'schemaVersion': 2, 'decimals': 8},
            credentials=credentials or (lambda: ('synthetic-id', 'synthetic-secret')),
            rest_factory=lambda *args: Rest(), live_factory=lambda *args: live or Live(),
            monotonic_ns=fixture.monotonic_ns if fixture else lambda: 0,
            wall_us=fixture.wall_us if fixture else lambda: NOW_US,
            decoder_revision='abi', authorization=managed_authorization('abi'))

    def test_failed_cleanup_prevents_restart_overwriting_retained_handle(self):
        live = Live()
        session = self.session(live=live)
        self.addCleanup(session.stop)
        session.start('REFERENCE_REQUIRED', 10)
        live.cleanup_failed = True
        with self.assertRaises(MarketDataError):
            session.stop()
        self.assertIs(session.consumer, live)
        try:
            with self.assertRaises(MarketDataError) as caught:
                session.start('REFERENCE_REQUIRED', 10)
            self.assertEqual(caught.exception.code, 'reference_shutdown_failed')
            self.assertIs(session.consumer, live)
        finally:
            live.cleanup_failed = False
        session.stop()
        self.assertIsNone(session.consumer)
        session.start('REFERENCE_REQUIRED', 10)
        self.assertTrue(session.started)

    def test_failed_rest_cleanup_retains_handle_until_recovered(self):
        session = self.session()
        self.addCleanup(session.stop)
        session.start('REFERENCE_REQUIRED', 10)
        rest = session.rest
        assert isinstance(rest, Rest)
        def failed_close():
            raise OSError('synthetic REST cleanup failure')
        rest.close = failed_close
        with self.assertRaises(MarketDataError):
            session.stop()
        self.assertIs(session.rest, rest)
        with self.assertRaises(MarketDataError) as caught:
            session.start('REFERENCE_REQUIRED', 10)
        self.assertEqual(caught.exception.code, 'reference_shutdown_failed')
        self.assertIs(session.rest, rest)
        rest.close = lambda: None
        session.stop()
        self.assertIsNone(session.rest)
        session.start('REFERENCE_REQUIRED', 10)
        self.assertTrue(session.started)

    def test_partial_persisted_pin_rejected_before_credentials(self):
        for pin in ({'price_scale': None, 'decoder_revision': 'abi'},
                    {'price_scale': 8, 'decoder_revision': None},
                    {'price_scale': 9, 'decoder_revision': 'abi'}):
            with self.subTest(pin=pin):
                calls = []
                session = self.session(credentials=lambda: calls.append('credentials') or ('synthetic-id', 'synthetic-secret'))
                self.addCleanup(session.stop)
                with self.assertRaises(MarketDataError) as caught:
                    session.start('REFERENCE_REQUIRED', 10, persisted_pin=pin)
                self.assertEqual(caught.exception.code, 'identity_conflict')
                self.assertEqual(calls, [])

    def test_uninitialized_persisted_pin_still_activates(self):
        session = self.session()
        self.addCleanup(session.stop)
        session.start('REFERENCE_REQUIRED', 10, persisted_pin={'price_scale': None, 'decoder_revision': None})
        self.assertTrue(session.started)

    def test_terminal_stream_failure_detected_before_clock_or_market_sampling(self):
        fixture = RecorderFixture()
        self.addCleanup(fixture.close)
        live = Live()
        live.terminal = True
        session = self.session(live=live, fixture=fixture)
        recorder = Recorder(fixture.store, fixture.public, monotonic_ns=fixture.monotonic_ns,
            wall_us=fixture.wall_us, sleep=fixture.sleep, offline_reference=session)
        # No calibrated clock and no acquired market. Bound the broken path so
        # failure detection cannot pass because active slot sampling noticed it.
        result = recorder.run(RunConfig(mode='REFERENCE_REQUIRED'), should_stop=lambda: bool(fixture.requests))
        self.assertEqual(result.reason, 'websocket_failed')
        self.assertEqual(result.state, 'FAILED')
        self.assertEqual(fixture.requests, [])


if __name__ == '__main__':
    unittest.main()
