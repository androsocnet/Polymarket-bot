import json
import threading
import unittest
from unittest.mock import patch

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.recorder.chainlink_ws import ChainlinkLiveConsumer
from polymarket_bot.recorder.models import CHAINLINK_FEED_ID
from tests.test_chainlink_rest import make_valid_report_payload


class ChainlinkLiveConsumerTests(unittest.TestCase):
    def consumer(self, **kwargs):
        return ChainlinkLiveConsumer('wss://ws.dataengine.chain.link', CHAINLINK_FEED_ID,
                                    'cid', 'sec', clock_seconds=lambda: 160, **kwargs)

    def test_exact_wss_host(self):
        for url in ('wss://ws.dataengine.chain.link.evil', 'wss://evil/dataengine.chain.link',
                    'wss://user@ws.dataengine.chain.link', 'wss://ws.dataengine.chain.link:443',
                    'wss://ws.dataengine.chain.link/other', 'wss://ws.dataengine.chain.link?x=1'):
            with self.subTest(url=url), self.assertRaises(MarketDataError):
                ChainlinkLiveConsumer(ws_url=url, feed_id=CHAINLINK_FEED_ID, client_id='cid',
                                     client_secret='sec', clock_seconds=lambda: 160)

    def test_real_adapter_signs_feed_query_without_network(self):
        from polymarket_bot.recorder.chainlink_auth import build_chainlink_auth_headers
        consumer = self.consumer()
        with patch('polymarket_bot.recorder.chainlink_ws.connect') as connect:
            consumer._connect()
        args, kw = connect.call_args
        target = '/ws/v1?feedIDs=' + CHAINLINK_FEED_ID
        self.assertEqual(args, ('wss://ws.dataengine.chain.link' + target,))
        self.assertEqual(kw['additional_headers'], build_chainlink_auth_headers('cid', 'sec', 'GET', target, None, 160000))
        self.assertIsNone(kw['proxy'])
        self.assertEqual(kw['max_size'], 10 * 1024 * 1024)

    def test_dependency_redirect_loop_never_opens_second_origin(self):
        from websockets.sync.client import reconnect
        from websockets.exceptions import InvalidStatus
        from websockets.http11 import Response
        from websockets.datastructures import Headers
        for status in (300, 301, 302, 303, 307, 308):
            for location in ('wss://evil.example/ws', '/other'):
                calls = []
                def opening(instance, deadline):
                    calls.append(instance.uri)
                    if len(calls) > 1:
                        self.fail('redirect followed with signed headers')
                    raise InvalidStatus(Response(status, 'Redirect', Headers({'Location': location}), b''))
                consumer = self.consumer()
                with self.subTest(status=status, location=location), patch.object(reconnect, 'open_connection', opening):
                    with self.assertRaises(InvalidStatus):
                        consumer._connect()
                self.assertEqual(len(calls), 1)

    def test_transient_handshake_statuses_retry_auth_terminal(self):
        from websockets.exceptions import InvalidStatus
        from websockets.http11 import Response
        from websockets.datastructures import Headers
        for status in (429, 500, 502, 503, 504, 401, 403):
            calls, waits = [], []
            def connector(*args, **kwargs):
                calls.append(status)
                raise InvalidStatus(Response(status, 'Rejected', Headers(), b''))
            consumer = self.consumer(connector=connector)
            def wait(delay):
                waits.append(delay)
                if len(waits) == 3:
                    consumer._stop_event.set()
            with patch.object(consumer._stop_event, 'wait', side_effect=wait):
                consumer._run()
            if status in (401, 403):
                self.assertEqual(calls, [status])
                self.assertEqual(waits, [])
                self.assertIsNotNone(consumer._failure)
            else:
                self.assertEqual(waits, [1, 2, 4])
                self.assertEqual(len(calls), 3)
                self.assertIsNone(consumer._failure)

    def test_digest_conflicts_rejected_before_timestamp_order(self):
        consumer = self.consumer()
        original = make_valid_report_payload()
        consumer._handle_websocket_message(json.dumps(original).encode())
        first = consumer.get_freshest_report(5, 160)
        for field, value in (('observationsTimestamp', 161), ('benchmarkPrice', '7')):
            altered = make_valid_report_payload()
            altered['report'][field] = value
            with self.subTest(field=field), self.assertRaises(MarketDataError):
                consumer._handle_websocket_message(json.dumps(altered).encode())
            self.assertEqual(consumer.get_freshest_report(5, 160), first)
        consumer._handle_websocket_message(json.dumps(original, indent=2).encode())
        self.assertEqual(consumer.get_freshest_report(5, 160), first)

    def test_numeric_validation_fail_closed(self):
        consumer = self.consumer()
        for field, value in (('observationsTimestamp', True), ('observationsTimestamp', 160.5),
                             ('observationsTimestamp', '9' * 257), ('benchmarkPrice', '1e129'),
                             ('benchmarkPrice', 'NaN'), ('benchmarkPrice', '-1'), ('expiresAt', 150)):
            frame = make_valid_report_payload()
            frame['report'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(MarketDataError):
                consumer._handle_websocket_message(json.dumps(frame).encode())
        with self.assertRaises(MarketDataError):
            consumer._handle_websocket_message(b'{"x":NaN}')
        self.assertIsNone(consumer.get_freshest_report(5, 160))

    def test_latest_bounded_deterministic_and_receipt_evidence(self):
        consumer = self.consumer()
        for obs, raw in ((160, b'z'), (158, b'a'), (160, b'b'), (160, b'z')):
            consumer._handle_websocket_message(json.dumps(make_valid_report_payload(raw, obs=obs)).encode())
        result = consumer.get_freshest_report(5, 162)
        self.assertEqual(result[1]['observationsTimestamp'], 160)
        self.assertIn('receipt_wall_us', result[1])
        self.assertIn('receipt_monotonic', result[1])
        self.assertIn('raw_message', result[1])
        self.assertLessEqual(consumer.report_count(), 1)
        result[1]['observationsTimestamp'] = 1
        self.assertEqual(consumer.get_freshest_report(5, 162)[1]['observationsTimestamp'], 160)
        self.assertIsNone(consumer.get_freshest_report(5, 159))
        self.assertIsNone(consumer.get_freshest_report(5, 166))
        for age, now in ((6, 160), (True, 160), (5, float('nan')), (5, float('inf'))):
            with self.assertRaises(MarketDataError):
                consumer.get_freshest_report(age, now)

    def test_expiry_and_valid_from(self):
        consumer = self.consumer()
        consumer._handle_websocket_message(json.dumps(make_valid_report_payload(exp=161)).encode())
        self.assertIsNone(consumer.get_freshest_report(5, 162))

    def test_backoff_one_two_four_capped_even_clean_disconnect(self):
        consumer = self.consumer(connector=lambda *a, **kw: None)
        waits = []
        def wait(delay):
            waits.append(delay)
            if len(waits) == 7:
                consumer._stop_event.set()
            return consumer._stop_event.is_set()
        with patch.object(consumer._stop_event, 'wait', side_effect=wait):
            consumer._run()
        self.assertEqual(waits, [1, 2, 4, 8, 16, 30, 30])

    def test_shutdown_closes_before_join_and_no_orphan(self):
        entered, closed = threading.Event(), threading.Event()
        class Connection:
            def recv(self, timeout):
                entered.set()
                closed.wait(2)
                raise ConnectionError()
            def close(self):
                closed.set()
        consumer = self.consumer(connector=lambda *a, **kw: Connection())
        consumer.start()
        self.assertTrue(entered.wait(2))
        thread = consumer._thread
        consumer.start()
        self.assertIs(thread, consumer._thread)
        consumer.stop()
        self.assertTrue(closed.is_set())
        self.assertFalse(thread.is_alive())
        self.assertFalse(consumer.is_connected())
        consumer.stop()

    def test_malformed_message_stops_and_clears_eligibility(self):
        class Connection:
            def recv(self, timeout):
                return b'{"x":NaN}'
            def close(self):
                pass
        consumer = self.consumer(connector=lambda *a, **kw: Connection())
        consumer._handle_websocket_message(json.dumps(make_valid_report_payload()).encode())
        consumer._run()
        self.assertFalse(consumer.is_connected())
        self.assertIsNone(consumer.get_freshest_report(5, 160))
        with self.assertRaises(MarketDataError):
            consumer.start()

    def test_programming_errors_not_hidden(self):
        consumer = self.consumer(connector=lambda *a, **kw: (_ for _ in ()).throw(TypeError('bug')))
        with self.assertRaises(TypeError):
            consumer._run()
        self.assertIsNotNone(consumer._failure)
        self.assertTrue(consumer._stop_event.is_set())
        with self.assertRaises(MarketDataError):
            consumer.start()

    def test_shutdown_joins_even_when_close_raises(self):
        consumer = self.consumer()
        class Connection:
            def close(self):
                raise OSError('credential sentinel')
        from unittest.mock import Mock
        thread = Mock()
        thread.is_alive.return_value = False
        consumer._ws = Connection()
        consumer._thread = thread
        with self.assertRaises(MarketDataError) as caught:
            consumer.stop()
        thread.join.assert_called_once_with(timeout=5)
        self.assertNotIn('sentinel', str(caught.exception))
        self.assertIsNone(consumer._thread)
        self.assertFalse(consumer.is_connected())
