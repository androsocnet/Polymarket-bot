"""Tests for Chainlink Public Discovery Client & Dynamic Feed Verification."""
import json
import unittest
from unittest.mock import patch
import httpx

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.recorder.models import CHAINLINK_FEED_ID, CHAINLINK_FEED_NAME
from polymarket_bot.recorder.chainlink_discovery import (
    VerifiedFeedMetadata,
    verify_feed_identity,
    fetch_feed_discovery,
    CHAINLINK_DISCOVERY_URL,
    CHAINLINK_DISCOVERY_HOST,
)


class ChainlinkDiscoveryTests(unittest.TestCase):
    def setUp(self):
        clock = patch('time.monotonic', return_value=0)
        clock.start()
        self.addCleanup(clock.stop)

    def test_discovery_fixed_route_redirect_and_budget(self):
        sent = []
        client = httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r) or
            httpx.Response(302, headers={'Location': 'https://evil.example/secret'})), follow_redirects=True)
        self.addCleanup(client.close)
        for url in ('http://api.dataengine.chain.link/api/v1/feeds',
                    'https://api.dataengine.chain.link/other', CHAINLINK_DISCOVERY_URL+'?secret=x'):
            with self.assertRaises(MarketDataError):
                fetch_feed_discovery(client, url, deadline=1)
        self.assertEqual(sent, [])
        with self.assertRaises(MarketDataError):
            fetch_feed_discovery(client, deadline=1)
        self.assertEqual(len(sent), 1)
        self.assertTrue(all(v <= 1 for v in sent[0].extensions['timeout'].values()))

    def test_discovery_late_stream_and_sanitized_failure(self):
        clock = [0]
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'{}'
                clock[0] = 2
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream())))
        self.addCleanup(client.close)
        with patch('time.monotonic', side_effect=lambda: clock[0]), self.assertRaises(MarketDataError):
            fetch_feed_discovery(client, deadline=1)
        def fail(r):
            raise httpx.ConnectError('secret sentinel')
        other = httpx.Client(transport=httpx.MockTransport(fail))
        self.addCleanup(other.close)
        with self.assertRaises(MarketDataError) as caught:
            fetch_feed_discovery(other, deadline=1)
        self.assertNotIn('sentinel', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_discovery_verification_success(self):
        payload = {
            "feeds": [
                {
                    "feedId": CHAINLINK_FEED_ID,
                    "name": CHAINLINK_FEED_NAME,
                    "status": "active",
                    "schemaVersion": 2,
                    "decimals": 8,
                    "category": "crypto",
                }
            ]
        }
        meta = verify_feed_identity(payload, CHAINLINK_FEED_ID)
        self.assertIsInstance(meta, VerifiedFeedMetadata)
        self.assertEqual(meta.feed_id, CHAINLINK_FEED_ID)
        self.assertEqual(meta.name, CHAINLINK_FEED_NAME)
        self.assertEqual(meta.report_schema_version, 2)
        self.assertEqual(meta.price_scale, 8)
        self.assertEqual(meta.status, "active")
        self.assertEqual(meta.catalog_metadata["category"], "crypto")

    def test_discovery_verification_with_list_payload(self):
        payload = [
            {
                "feed_id": CHAINLINK_FEED_ID.upper(),
                "name": CHAINLINK_FEED_NAME,
                "status": "ACTIVE",
                "schema_version": 2,
                "price_scale": 8,
            }
        ]
        meta = verify_feed_identity(payload, CHAINLINK_FEED_ID)
        self.assertEqual(meta.feed_id, CHAINLINK_FEED_ID)
        self.assertEqual(meta.price_scale, 8)
        self.assertEqual(meta.status, "active")

    def test_discovery_mismatch_feed_not_found(self):
        payload = {"feeds": [{"feedId": "0x1234", "status": "active", "schemaVersion": 2, "decimals": 8}]}
        with self.assertRaises(MarketDataError) as ctx:
            verify_feed_identity(payload, CHAINLINK_FEED_ID)
        self.assertEqual(ctx.exception.code, "feed_not_found")

    def test_discovery_feed_deprecated_or_inactive(self):
        for status, expected_code in [("deprecated", "feed_deprecated"), ("inactive", "feed_inactive"), ("retired", "feed_deprecated")]:
            with self.subTest(status=status):
                payload = {
                    "feeds": [
                        {
                            "feedId": CHAINLINK_FEED_ID,
                            "name": CHAINLINK_FEED_NAME,
                            "status": status,
                            "schemaVersion": 2,
                            "decimals": 8,
                        }
                    ]
                }
                with self.assertRaises(MarketDataError) as ctx:
                    verify_feed_identity(payload, CHAINLINK_FEED_ID)
                self.assertEqual(ctx.exception.code, expected_code)

    def test_discovery_schema_version_mismatch(self):
        payload = {
            "feeds": [
                {
                    "feedId": CHAINLINK_FEED_ID,
                    "name": CHAINLINK_FEED_NAME,
                    "status": "active",
                    "schemaVersion": 3,
                    "decimals": 8,
                }
            ]
        }
        with self.assertRaises(MarketDataError) as ctx:
            verify_feed_identity(payload, CHAINLINK_FEED_ID)
        self.assertEqual(ctx.exception.code, "schema_mismatch")

    def test_discovery_invalid_decimals(self):
        for bad_dec in [-1, 200, "invalid", None]:
            with self.subTest(bad_dec=bad_dec):
                payload = {
                    "feeds": [
                        {
                            "feedId": CHAINLINK_FEED_ID,
                            "name": CHAINLINK_FEED_NAME,
                            "status": "active",
                            "schemaVersion": 2,
                            "decimals": bad_dec,
                        }
                    ]
                }
                with self.assertRaises(MarketDataError) as ctx:
                    verify_feed_identity(payload, CHAINLINK_FEED_ID)
                self.assertEqual(ctx.exception.code, "invalid_decimals")

    def test_discovery_ambiguous_feed_entries(self):
        payload = {
            "feeds": [
                {"feedId": CHAINLINK_FEED_ID, "name": "feed1", "status": "active", "schemaVersion": 2, "decimals": 8},
                {"feedId": CHAINLINK_FEED_ID, "name": "feed2", "status": "active", "schemaVersion": 2, "decimals": 8},
            ]
        }
        with self.assertRaises(MarketDataError) as ctx:
            verify_feed_identity(payload, CHAINLINK_FEED_ID)
        self.assertEqual(ctx.exception.code, "ambiguous_feed")

    def test_fetch_feed_discovery_rejects_disallowed_host(self):
        client = httpx.Client(trust_env=False)
        self.addCleanup(client.close)
        with self.assertRaises(MarketDataError) as ctx:
            fetch_feed_discovery(client, "https://evil.example.com/api/v1/feeds")
        self.assertEqual(ctx.exception.code, "invalid_discovery_host")

    def test_fetch_feed_discovery_successful_read(self):
        sample_payload = {
            "feeds": [
                {
                    "feedId": CHAINLINK_FEED_ID,
                    "name": CHAINLINK_FEED_NAME,
                    "status": "active",
                    "schemaVersion": 2,
                    "decimals": 8,
                }
            ]
        }
        def handler(request: httpx.Request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.host, CHAINLINK_DISCOVERY_HOST)
            self.assertEqual(request.url.path, "/api/v1/feeds")
            self.assertFalse(any("authorization" in h.lower() for h in request.headers))
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(sample_payload).encode()))

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, trust_env=False)
        self.addCleanup(client.close)

        result = fetch_feed_discovery(client, CHAINLINK_DISCOVERY_URL, deadline=10.0)
        self.assertEqual(result, sample_payload)

    def test_fetch_feed_discovery_http_error(self):
        def handler(request: httpx.Request):
            return httpx.Response(500, stream=httpx.ByteStream(b"server error"))

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport, trust_env=False)
        self.addCleanup(client.close)

        with self.assertRaises(MarketDataError) as ctx:
            fetch_feed_discovery(client, CHAINLINK_DISCOVERY_URL, deadline=10.0)
        self.assertEqual(ctx.exception.code, "discovery_http_error")
