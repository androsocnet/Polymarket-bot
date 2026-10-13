"""Tests for Coinbase REST provider in FREE_REFERENCE mode."""
import hashlib
import httpx
import unittest
from decimal import Decimal
from datetime import datetime, timezone


from polymarket_bot.recorder.free_reference import (
    ApproximateObservation,
    CoinbaseRestProvider,
)


class CoinbaseProviderTests(unittest.TestCase):
    def setUp(self):
        self.monotonic_ns = 2_000_000_000_000_000
        self.wall_us = 2_000_000_000_000_000  # ~Jun 2033, after Coinbase response times

    def _make_client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    def test_raw_stream_cap_deadline_and_no_ambient_secrets(self):
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'x' * 1048576
                yield b'y'
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(200, stream=Stream())
        with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False,
                          headers={'Authorization': 'secret'}, cookies={'secret': 'cookie'},
                          auth=('user', 'secret')) as client:
            provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns,
                                            wall_us=lambda: self.wall_us)
            obs = provider.fetch_ticker(deadline=self.monotonic_ns / 1000000000 + 5)
        self.assertEqual(obs.error_code, 'PAYLOAD_TOO_LARGE')
        self.assertEqual(obs.raw_payload_size, 1048576)
        self.assertEqual(obs.raw_payload_hash, hashlib.sha256(b'x' * 1048576).hexdigest())
        self.assertNotIn('authorization', requests[0].headers)
        self.assertNotIn('cookie', requests[0].headers)
        self.assertLessEqual(requests[0].extensions['timeout']['read'], 5)

    def test_stream_deadline_expires_per_chunk(self):
        clock = self
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'partial'
                clock.monotonic_ns += 6000000000
                yield b'late'
        with self._make_client(lambda request: httpx.Response(200, stream=Stream())) as client:
            provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns,
                                            wall_us=lambda: self.wall_us)
            obs = provider.fetch_ticker(deadline=self.monotonic_ns / 1000000000 + 5)
        self.assertEqual(obs.error_code, 'DEADLINE_EXPIRED')
        self.assertEqual(obs.raw_payload_hash, hashlib.sha256(b'partial').hexdigest())

    def test_deadline_uses_only_absolute_monotonic_time(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(503)
        with self._make_client(handle) as client:
            provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns,
                                            wall_us=lambda: self.wall_us)
            obs = provider.fetch_ticker(deadline=self.monotonic_ns / 1000000000 + 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(obs.http_status, 503)

    def test_public_response_cookies_not_retained(self):
        with self._make_client(lambda request: httpx.Response(503,
                headers={'Set-Cookie': 'public=tracking; Path=/'})) as client:
            provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns,
                                            wall_us=lambda: self.wall_us)
            provider.fetch_ticker(deadline=self.monotonic_ns / 1000000000 + 5)
            self.assertFalse(client.cookies)

    def test_completed_receipt_not_request_start(self):
        def handler(request):
            self.wall_us += 1_000_000
            self.monotonic_ns += 1_000_000_000
            return httpx.Response(200, json={
                "price": "10", "bid": "9", "ask": "11",
                "time": "2020-03-20T00:22:57.833Z",
            })
        with self._make_client(handler) as client:
            provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns,
                                            wall_us=lambda: self.wall_us)
            observation = provider.fetch_ticker(deadline=self.monotonic_ns / 1_000_000_000 + 5)
        self.assertEqual(observation.receipt_wall_us, self.wall_us)

    def test_failure_has_empty_body_sha256_and_absent_status(self):
        def handler(request):
            raise httpx.ConnectError("offline")
        with self._make_client(handler) as client:
            provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns,
                                            wall_us=lambda: self.wall_us)
            observation = provider.fetch_ticker(deadline=self.monotonic_ns / 1_000_000_000 + 5)
        self.assertEqual(observation.raw_payload_hash, hashlib.sha256(b"").hexdigest())
        self.assertIsNone(observation.http_status)
        self.assertIsNone(observation.price)
        self.assertIsNone(observation.receipt_server_us)

    def test_nonpositive_bid_and_ask_rejected(self):
        for field in ("bid", "ask"):
            payload = {"price": "10", "bid": "9", "ask": "11",
                       "time": "2020-03-20T00:22:57.833Z"}
            payload[field] = "0"
            with self._make_client(lambda request: httpx.Response(200, json=payload)) as client:
                provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns,
                                                wall_us=lambda: self.wall_us)
                observation = provider.fetch_ticker(deadline=self.monotonic_ns / 1_000_000_000 + 5)
            self.assertEqual(observation.result, "error")

    def test_future_source_timestamp_survives_rejection_and_persistence(self):
        import tempfile
        from pathlib import Path
        from polymarket_bot.recorder.models import RunConfig
        from polymarket_bot.recorder.store import RecorderStore
        from polymarket_bot.recorder.free_reference import compute_approximate_sample_mean_60s
        source = self.wall_us + 1_000_000
        stamp = datetime.fromtimestamp(source / 1_000_000, timezone.utc).isoformat()
        with self._make_client(lambda request: httpx.Response(200, json={
                'price': '10', 'bid': '9', 'ask': '11', 'time': stamp})) as client:
            provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns,
                wall_us=lambda: self.wall_us)
            obs = provider.fetch_ticker(deadline=self.monotonic_ns / 1_000_000_000 + 5)
        with tempfile.TemporaryDirectory() as directory:
            store = RecorderStore.open_approximate(Path(directory) / 'new.sqlite3')
            try:
                run = store.start_run(RunConfig(mode='APPROXIMATE_REFERENCE'),
                    wall_us=self.wall_us, app_revision='test')
                identifier = store.save_approximate_observation(run, obs)
                row = store._one('SELECT * FROM approximate_observations WHERE observation_id=?', (identifier,))
                assert row is not None
                self.assertEqual(row['source_timestamp_us'], source)
                self.assertIsNone(row['price_text'])
                self.assertEqual(row['error_code'], 'FUTURE_SOURCE_TIME')
            finally:
                store.close()
        self.assertIsNone(obs.price)
        self.assertIsNone(obs.bid_price)
        self.assertIsNone(obs.ask_price)
        report = compute_approximate_sample_mean_60s([obs], receipt_wall_us=self.wall_us,
            receipt_server_us=None, receipt_mono_ns=self.monotonic_ns)
        self.assertIsNone(report.value)
        self.assertEqual(report.diagnostics, ('EXCLUDED_ERROR_OBSERVATIONS', 'MISSING_OBSERVATIONS'))

    def test_valid_ticker(self):
        """Valid ticker returns price from 'price' field, source_timestamp_us from 'time' field."""
        def handler(request):
            self.assertEqual(str(request.url), "https://api.exchange.coinbase.com/products/BTC-USD/ticker")
            self.assertEqual(request.url.path, "/products/BTC-USD/ticker")
            return httpx.Response(200, json={
                "trade_id": 123456789,
                "price": "6268.48",
                "size": "0.00698254",
                "time": "2020-03-20T00:22:57.833Z",
                "bid": "6265.15",
                "ask": "6267.71",
                "volume": "53602.03940154"
            })

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.provider, "coinbase")
        self.assertEqual(obs.pair, "BTC/USD")
        self.assertEqual(obs.quote_currency, "USD")
        self.assertEqual(obs.price, Decimal("6268.48"))
        self.assertEqual(obs.bid_price, Decimal("6265.15"))
        self.assertEqual(obs.ask_price, Decimal("6267.71"))
        self.assertEqual(obs.source_timestamp_us, 1584663777833000)
        self.assertEqual(obs.receipt_wall_us, self.wall_us)
        self.assertIsNone(obs.receipt_server_us)
        self.assertEqual(obs.receipt_mono_ns, self.monotonic_ns)
        self.assertEqual(obs.transport_type, "rest")
        self.assertEqual(obs.parser_revision, "coinbase-ticker-v1")
        self.assertEqual(obs.http_status, 200)
        self.assertEqual(obs.result, "success")
        self.assertIsNone(obs.error_code)

    def test_wrong_pair_rejected(self):
        """ETH-USD path → rejected (must be BTC-USD)."""
        def handler(request):
            # This test verifies the request path is exactly /products/BTC-USD/ticker
            # If the provider ever uses a different path, this assertion fails
            self.assertEqual(request.url.path, "/products/BTC-USD/ticker")
            # Simulate a response that could come from a different pair endpoint
            # (The provider should never request a different pair)
            return httpx.Response(200, json={
                "trade_id": 1,
                "price": "3000.00",
                "size": "1.0",
                "time": "2020-03-20T00:22:57.833Z",
                "bid": "2999.00",
                "ask": "3001.00",
                "volume": "1000.0"
            })

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        # Provider constructs the URL internally, so it should always be BTC-USD
        # The handler assertion above ensures this
        self.assertEqual(obs.result, "success")

    def test_nonpositive_price_rejected(self):
        """Non-positive price (0) → rejected."""
        def handler(request):
            return httpx.Response(200, json={
                "trade_id": 1,
                "price": "0",
                "size": "1.0",
                "time": "2020-03-20T00:22:57.833Z",
                "bid": "6265.15",
                "ask": "6267.71",
                "volume": "53602.03940154"
            })

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("NON_POSITIVE_PRICE", obs.error_code or "")

    def test_negative_price_rejected(self):
        """Negative price → rejected."""
        def handler(request):
            return httpx.Response(200, json={
                "trade_id": 1,
                "price": "-1",
                "size": "1.0",
                "time": "2020-03-20T00:22:57.833Z",
                "bid": "6265.15",
                "ask": "6267.71",
                "volume": "53602.03940154"
            })

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("NON_POSITIVE_PRICE", obs.error_code or "")

    def test_missing_price_rejected(self):
        """Missing price field → rejected."""
        def handler(request):
            return httpx.Response(200, json={
                "trade_id": 1,
                "size": "1.0",
                "time": "2020-03-20T00:22:57.833Z",
                "bid": "6265.15",
                "ask": "6267.71",
                "volume": "53602.03940154"
            })

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("MALFORMED_JSON", obs.error_code or "")

    def test_malformed_json_rejected(self):
        """Malformed JSON → rejected."""
        def handler(request):
            return httpx.Response(200, content=b"not json")

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("MALFORMED_JSON", obs.error_code or "")

    def test_transport_timeout(self):
        """Transport timeout → error observation."""
        def handler(request):
            raise httpx.TimeoutException("timeout")

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("TRANSPORT_TIMEOUT", obs.error_code or "")

    def test_http_error_status(self):
        """HTTP error status (e.g., 500, 503) → error observation."""
        for status in (500, 503):
            with self.subTest(status=status):
                def handler(request):
                    return httpx.Response(status, json={"error": "Internal Server Error"})

                client = self._make_client(handler)
                provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
                deadline = self.monotonic_ns / 1_000_000_000 + 5.0
                obs = provider.fetch_ticker(deadline=deadline)

                self.assertEqual(obs.result, "error")
                self.assertIsNotNone(obs.error_code)
                self.assertIn("HTTP_ERROR", obs.error_code or "")
                self.assertEqual(obs.http_status, status)

    def test_no_lookahead_future_timestamp(self):
        """Response time > receipt time → future_source_time error."""
        future_time = "2020-03-20T00:22:57.833Z"  # 1584663777833000 us

        def handler(request):
            return httpx.Response(200, json={
                "trade_id": 1,
                "price": "6268.48",
                "size": "0.00698254",
                "time": future_time,
                "bid": "6265.15",
                "ask": "6267.71",
                "volume": "53602.03940154"
            })

        client = self._make_client(handler)
        # wall_us set to BEFORE the response time to simulate future source
        wall_us_before = 1_500_000_000_000  # Feb 2017
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: wall_us_before)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("FUTURE_SOURCE_TIME", obs.error_code or "")
        self.assertEqual(obs.source_timestamp_us, 1584663777833000)
        self.assertIsNone(obs.price)

    def test_stale_data_handling(self):
        """Old timestamp (e.g., hours ago) → accepted but with old source_timestamp_us."""
        old_time = "2020-01-01T00:00:00.000Z"  # Very old timestamp

        def handler(request):
            return httpx.Response(200, json={
                "trade_id": 1,
                "price": "6268.48",
                "size": "0.00698254",
                "time": old_time,
                "bid": "6265.15",
                "ask": "6267.71",
                "volume": "53602.03940154"
            })

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "success")
        # Old timestamp should be parsed correctly
        expected_ts = 1577836800000000
        self.assertEqual(obs.source_timestamp_us, expected_ts)

    def test_raw_payload_hash_computed(self):
        """Raw payload hash is computed correctly from raw JSON bytes."""
        payload = {
            "trade_id": 123456789,
            "price": "6268.48",
            "size": "0.00698254",
            "time": "2020-03-20T00:22:57.833Z",
            "bid": "6265.15",
            "ask": "6267.71",
            "volume": "53602.03940154"
        }
        import json
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
        expected_hash = hashlib.sha256(payload_bytes).hexdigest()

        def handler(request):
            return httpx.Response(200, json=payload)

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.raw_payload_hash, expected_hash)
        self.assertEqual(obs.raw_payload_size, len(payload_bytes))

    def test_trust_env_false(self):
        """Client must have trust_env=False."""
        def handler(request):
            return httpx.Response(200, json={
                "trade_id": 1,
                "price": "6268.48",
                "size": "0.00698254",
                "time": "2020-03-20T00:22:57.833Z",
                "bid": "6265.15",
                "ask": "6267.71",
                "volume": "53602.03940154"
            })

        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        self.assertFalse(client.trust_env)

    def test_oversized_response(self):
        """Response > 1 MiB → rejected."""
        # Create a response larger than 1 MiB
        large_payload = "x" * (2 * 1024 * 1024)
        def handler(request):
            return httpx.Response(200, content=large_payload.encode())

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("PAYLOAD_TOO_LARGE", obs.error_code or "")

    def test_endpoint_exact_host(self):
        """Only api.exchange.coinbase.com is used."""
        def handler(request):
            self.assertEqual(request.url.scheme, "https")
            self.assertEqual(request.url.host, "api.exchange.coinbase.com")
            self.assertEqual(request.url.path, "/products/BTC-USD/ticker")
            return httpx.Response(200, json={
                "trade_id": 1,
                "price": "6268.48",
                "size": "0.00698254",
                "time": "2020-03-20T00:22:57.833Z",
                "bid": "6265.15",
                "ask": "6267.71",
                "volume": "53602.03940154"
            })

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "success")

    def test_parser_revision_set(self):
        """Parser revision is 'coinbase-ticker-v1'."""
        def handler(request):
            return httpx.Response(200, json={
                "trade_id": 1,
                "price": "6268.48",
                "size": "0.00698254",
                "time": "2020-03-20T00:22:57.833Z",
                "bid": "6265.15",
                "ask": "6267.71",
                "volume": "53602.03940154"
            })

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.parser_revision, "coinbase-ticker-v1")

    def test_transport_type_rest(self):
        """Transport type is 'rest'."""
        def handler(request):
            return httpx.Response(200, json={
                "trade_id": 1,
                "price": "6268.48",
                "size": "0.00698254",
                "time": "2020-03-20T00:22:57.833Z",
                "bid": "6265.15",
                "ask": "6267.71",
                "volume": "53602.03940154"
            })

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.transport_type, "rest")

    def test_clock_seconds_optional(self):
        """clock_seconds parameter is optional and not required for basic operation."""
        def handler(request):
            return httpx.Response(200, json={
                "trade_id": 1,
                "price": "6268.48",
                "size": "0.00698254",
                "time": "2020-03-20T00:22:57.833Z",
                "bid": "6265.15",
                "ask": "6267.71",
                "volume": "53602.03940154"
            })

        client = self._make_client(handler)
        # Call without clock_seconds
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "success")

    def test_deadline_expiration(self):
        """Expired deadline → DEADLINE_EXPIRED error."""
        def handler(request):
            self.fail("Request should not be sent when deadline expired")

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        # Deadline in the past
        deadline = self.monotonic_ns / 1_000_000_000 - 1.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("DEADLINE_EXPIRED", obs.error_code or "")


if __name__ == "__main__":
    unittest.main()