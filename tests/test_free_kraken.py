"""Tests for Kraken REST provider in FREE_REFERENCE mode."""
import hashlib
import httpx
import unittest
from decimal import Decimal
from unittest.mock import patch

from polymarket_bot.recorder.free_reference import (
    ApproximateObservation,
    KrakenRestProvider,
)


class KrakenProviderTests(unittest.TestCase):
    def setUp(self):
        self.monotonic_ns = 1_000_000_000_000
        self.wall_us = 1_000_000_000_000

    def _make_client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    def test_valid_ticker(self):
        """Valid ticker returns price from 'c' field, source_timestamp_us=None."""
        def handler(request):
            self.assertIn("assetVersion", request.url.params)
            self.assertEqual(request.url.params["pair"], "XBTUSD")
            self.assertEqual(request.url.params["assetVersion"], "1")
            return httpx.Response(200, json={
                "error": [],
                "result": {
                    "BTC/USD": {
                        "a": ["30300.10000", "1", "1.000"],
                        "b": ["30300.00000", "1", "1.000"],
                        "c": ["30303.20000", "0.00067643"],
                        "v": ["4083.67001100", "4412.73601799"],
                        "p": ["30706.77771", "30689.13205"],
                        "t": [34619, 38907],
                        "l": ["29868.30000", "31631.00000"],
                        "h": ["31631.00000", "31631.00000"],
                        "o": "30502.80000"
                    }
                }
            })

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.provider, "kraken")
        self.assertEqual(obs.pair, "BTC/USD")
        self.assertEqual(obs.quote_currency, "USD")
        self.assertEqual(obs.price, Decimal("30303.20000"))
        self.assertEqual(obs.bid_price, Decimal("30300.00000"))
        self.assertEqual(obs.ask_price, Decimal("30300.10000"))
        self.assertIsNone(obs.source_timestamp_us)
        self.assertEqual(obs.receipt_wall_us, self.wall_us)
        self.assertIsNone(obs.receipt_server_us)
        self.assertEqual(obs.receipt_mono_ns, self.monotonic_ns)
        self.assertEqual(obs.transport_type, "rest")
        self.assertEqual(obs.parser_revision, "kraken-ticker-v1")
        self.assertEqual(obs.http_status, 200)
        self.assertEqual(obs.result, "success")
        self.assertIsNone(obs.error_code)

    def test_source_timestamp_always_none(self):
        """source_timestamp_us MUST be None for Kraken (no trustworthy exchange timestamp)."""
        def handler(request):
            return httpx.Response(200, json={
                "error": [],
                "result": {"BTC/USD": {"c": ["50000.00", "1"], "b": ["49999.00", "1"], "a": ["50001.00", "1"]}}
            })

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertIsNone(obs.source_timestamp_us, "Kraken Ticker has NO trustworthy exchange timestamp")

    def test_default_assetversion_rejected(self):
        """XXBTZUSD keys when no assetVersion=1 → must reject."""
        def handler(request):
            self.assertEqual(request.url.params["pair"], "XBTUSD")
            return httpx.Response(200, json={
                "error": [],
                "result": {"XXBTZUSD": {"c": ["50000.00", "1"], "b": ["49999.00", "1"], "a": ["50001.00", "1"]}}
            })

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("WRONG_PAIR_KEY", obs.error_code or "")

    def test_wrong_pair_rejected(self):
        """XETHZUSD → rejected."""
        def handler(request):
            self.assertEqual(request.url.params["pair"], "XBTUSD")
            return httpx.Response(200, json={
                "error": [],
                "result": {"XETHZUSD": {"c": ["3000.00", "1"], "b": ["2999.00", "1"], "a": ["3001.00", "1"]}}
            })

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("WRONG_PAIR_KEY", obs.error_code or "")

    def test_nonpositive_price_rejected(self):
        """Non-positive price → rejected."""
        def handler(request):
            return httpx.Response(200, json={
                "error": [],
                "result": {"BTC/USD": {"c": ["0.00", "1"], "b": ["49999.00", "1"], "a": ["50001.00", "1"]}}
            })

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("NON_POSITIVE_PRICE", obs.error_code or "")

    def test_negative_price_rejected(self):
        """Negative price → rejected."""
        def handler(request):
            return httpx.Response(200, json={
                "error": [],
                "result": {"BTC/USD": {"c": ["-100.00", "1"], "b": ["49999.00", "1"], "a": ["50001.00", "1"]}}
            })

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("NON_POSITIVE_PRICE", obs.error_code or "")

    def test_kraken_error_array_rejected(self):
        """Error array non-empty → rejected."""
        def handler(request):
            return httpx.Response(200, json={
                "error": ["EGeneral:Invalid arguments"],
                "result": {}
            })

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("KRAKEN_API_ERROR", obs.error_code or "")

    def test_malformed_json_rejected(self):
        """Malformed JSON → rejected."""
        def handler(request):
            return httpx.Response(200, content=b"not json")

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
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
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("TRANSPORT_TIMEOUT", obs.error_code or "")

    def test_http_error_status(self):
        """HTTP error status (e.g., 500) → error observation."""
        def handler(request):
            return httpx.Response(500, json={"error": ["Internal Server Error"]})

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "error")
        self.assertIsNotNone(obs.error_code)
        self.assertIn("HTTP_ERROR", obs.error_code or "")

    def test_raw_payload_hash_computed(self):
        """Raw payload hash is computed correctly from raw JSON bytes."""
        payload = {
            "error": [],
            "result": {"BTC/USD": {"c": ["50000.00", "1"], "b": ["49999.00", "1"], "a": ["50001.00", "1"]}}
        }
        import json
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
        expected_hash = hashlib.sha256(payload_bytes).hexdigest()

        def handler(request):
            return httpx.Response(200, json=payload)

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.raw_payload_hash, expected_hash)

    def test_trust_env_false(self):
        """Client must have trust_env=False."""
        def handler(request):
            return httpx.Response(200, json={
                "error": [],
                "result": {"BTC/USD": {"c": ["50000.00", "1"], "b": ["49999.00", "1"], "a": ["50001.00", "1"]}}
            })

        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        self.assertFalse(client.trust_env)

    def test_endpoint_exact_host(self):
        """Only api.kraken.com is used."""
        def handler(request):
            self.assertEqual(str(request.url).startswith("https://api.kraken.com/"), True)
            self.assertEqual(request.url.path, "/0/public/Ticker")
            return httpx.Response(200, json={
                "error": [],
                "result": {"BTC/USD": {"c": ["50000.00", "1"], "b": ["49999.00", "1"], "a": ["50001.00", "1"]}}
            })

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "success")

    def test_parser_revision_set(self):
        """Parser revision is 'kraken-ticker-v1'."""
        def handler(request):
            return httpx.Response(200, json={
                "error": [],
                "result": {"BTC/USD": {"c": ["50000.00", "1"], "b": ["49999.00", "1"], "a": ["50001.00", "1"]}}
            })

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.parser_revision, "kraken-ticker-v1")

    def test_bid_ask_parsed(self):
        """Bid and ask are parsed from 'b' and 'a' fields."""
        def handler(request):
            return httpx.Response(200, json={
                "error": [],
                "result": {
                    "BTC/USD": {
                        "a": ["50001.50", "1", "1.000"],
                        "b": ["49999.50", "1", "1.000"],
                        "c": ["50000.00", "1"]
                    }
                }
            })

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.bid_price, Decimal("49999.50"))
        self.assertEqual(obs.ask_price, Decimal("50001.50"))

    def test_result_key_must_be_btc_usd(self):
        """Result must have 'BTC/USD' key (from assetVersion=1)."""
        def handler(request):
            return httpx.Response(200, json={
                "error": [],
                "result": {"BTC/USD": {"c": ["50000.00", "1"], "b": ["49999.00", "1"], "a": ["50001.00", "1"]}}
            })

        client = self._make_client(handler)
        provider = KrakenRestProvider(client, monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us)
        deadline = self.monotonic_ns / 1_000_000_000 + 5.0
        obs = provider.fetch_ticker(deadline=deadline)

        self.assertEqual(obs.result, "success")
        self.assertEqual(obs.pair, "BTC/USD")


if __name__ == "__main__":
    unittest.main()