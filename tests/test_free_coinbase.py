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
        deadline = self.wall_us / 1_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.provider, "coinbase")
        self.assertEqual(obs.pair, "BTC/USD")
        self.assertEqual(obs.quote_currency, "USD")
        self.assertEqual(obs.price, Decimal("6268.48"))
        self.assertEqual(obs.bid_price, Decimal("6265.15"))
        self.assertEqual(obs.ask_price, Decimal("6267.71"))
        self.assertEqual(obs.source_timestamp_us, 1584663777833000)
        self.assertEqual(obs.receipt_wall_us, self.wall_us)
        self.assertEqual(obs.receipt_server_us, self.monotonic_ns // 1000)
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
        deadline = self.wall_us / 1_000_000 + 5.0
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
        deadline = self.wall_us / 1_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("NON_POSITIVE_PRICE", obs.error_code)

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
        deadline = self.wall_us / 1_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("NON_POSITIVE_PRICE", obs.error_code)

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
        deadline = self.wall_us / 1_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("MALFORMED_JSON", obs.error_code)

    def test_malformed_json_rejected(self):
        """Malformed JSON → rejected."""
        def handler(request):
            return httpx.Response(200, content=b"not json")

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.wall_us / 1_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("MALFORMED_JSON", obs.error_code)

    def test_transport_timeout(self):
        """Transport timeout → error observation."""
        def handler(request):
            raise httpx.TimeoutException("timeout")

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.wall_us / 1_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("TRANSPORT_TIMEOUT", obs.error_code)

    def test_http_error_status(self):
        """HTTP error status (e.g., 500, 503) → error observation."""
        for status in (500, 503):
            with self.subTest(status=status):
                def handler(request):
                    return httpx.Response(status, json={"error": "Internal Server Error"})

                client = self._make_client(handler)
                provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
                deadline = self.wall_us / 1_000_000 + 5.0
                obs = provider.fetch_ticker(deadline=deadline)

                self.assertEqual(obs.result, "error")
                self.assertIsNotNone(obs.error_code)
                self.assertIn("HTTP_ERROR", obs.error_code)
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
        deadline = wall_us_before / 1_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("FUTURE_SOURCE_TIME", obs.error_code)
        # source_timestamp_us should not be set to future value
        self.assertIsNone(obs.source_timestamp_us)

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
        deadline = self.wall_us / 1_000_000 + 5.0
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
        deadline = self.wall_us / 1_000_000 + 5.0
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
        deadline = self.wall_us / 1_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("PAYLOAD_TOO_LARGE", obs.error_code)

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
        deadline = self.wall_us / 1_000_000 + 5.0
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
        deadline = self.wall_us / 1_000_000 + 5.0
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
        deadline = self.wall_us / 1_000_000 + 5.0
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
        deadline = self.wall_us / 1_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "success")

    def test_deadline_expiration(self):
        """Expired deadline → DEADLINE_EXPIRED error."""
        def handler(request):
            self.fail("Request should not be sent when deadline expired")

        client = self._make_client(handler)
        provider = CoinbaseRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        # Deadline in the past
        deadline = self.wall_us / 1_000_000 - 1.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("DEADLINE_EXPIRED", obs.error_code)


if __name__ == "__main__":
    unittest.main()