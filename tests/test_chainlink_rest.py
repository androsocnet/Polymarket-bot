"""Tests for Chainlink REST Transport Adapter."""
import json
import time
import unittest
from unittest.mock import patch
import httpx

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.recorder.models import CHAINLINK_FEED_ID, CHAINLINK_FEED_NAME
from polymarket_bot.recorder.chainlink_rest import ChainlinkRestClient
from tests.support import FakeClock


def make_valid_report_payload(report_bytes=b"signed-report", valid_from=100, obs=160, exp=220, price=6500012345678):
    signed_hex = report_bytes.hex()
    return {
        "report": {
            "feedId": CHAINLINK_FEED_ID,
            "fullReport": signed_hex,
            "validFromTimestamp": valid_from,
            "observationsTimestamp": obs,
            "expiresAt": exp,
            "benchmarkPrice": str(price),
        },
    }


class ChainlinkRestClientTests(unittest.TestCase):
    def reader(self, handler, *, client_id="test-client", client_secret="test-secret", base_url=None):
        if base_url is None:
            base_url = "https://api.dataengine.chain.link"
        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False, base_url=base_url)
        clock = FakeClock()
        # Use realistic Unix second timestamp (e.g., 2025-05-01 ~ 1746000000)
        base_ts_s = 1746000000
        return client, ChainlinkRestClient(client, client_id=client_id,
                                           client_secret=client_secret,
                                           clock_seconds=lambda: base_ts_s + int(clock.monotonic()),
                                           feed_id=CHAINLINK_FEED_ID)

    def test_get_latest_report_success(self):
        payload = make_valid_report_payload()
        def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.host, "api.dataengine.chain.link")
            self.assertEqual(request.url.path, "/api/v1/reports/latest")
            self.assertEqual(request.url.params["feedId"], CHAINLINK_FEED_ID)
            self.assertIn("Authorization", request.headers)
            self.assertIn("X-Authorization-Timestamp", request.headers)
            self.assertIn("X-Authorization-Signature-SHA256", request.headers)
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload).encode()))

        client, reader = self.reader(handler)
        self.addCleanup(client.close)
        raw_body, meta = reader.get_latest_report(deadline=10.0)
        self.assertEqual(meta["feedId"], CHAINLINK_FEED_ID)
        self.assertEqual(raw_body, bytes.fromhex(payload["report"]["fullReport"]))

    def test_get_report_at_timestamp_success(self):
        payload = make_valid_report_payload()
        def handler(request):
            self.assertEqual(request.url.path, "/api/v1/reports")
            self.assertEqual(request.url.params["feedId"], CHAINLINK_FEED_ID)
            self.assertEqual(request.url.params["timestamp"], "1746000000")
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload).encode()))

        client, reader = self.reader(handler)
        self.addCleanup(client.close)
        raw_body, meta = reader.get_report_at_timestamp(1746000000, deadline=10.0)
        self.assertEqual(raw_body, bytes.fromhex(payload["report"]["fullReport"]))

    def test_get_reports_page_success(self):
        payload = make_valid_report_payload()
        def handler(request):
            self.assertEqual(request.url.path, "/api/v1/reports/page")
            self.assertEqual(request.url.params["feedId"], CHAINLINK_FEED_ID)
            self.assertEqual(request.url.params["limit"], "50")
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps({"reports": [payload]}).encode()))

        client, reader = self.reader(handler)
        self.addCleanup(client.close)
        reports = reader.get_reports_page(start_s=160, end_s=200, limit=50, deadline=10.0)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0][1]["feedId"], CHAINLINK_FEED_ID)

    def test_credentials_never_attached_to_non_chainlink_host(self):
        def handler(request):
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(client.close)
        bad_client = ChainlinkRestClient(client, client_id="cid", client_secret="sec",
                                         clock_seconds=lambda: 123, feed_id=CHAINLINK_FEED_ID)
        for host in ("https://clob.polymarket.com", "https://gamma-api.polymarket.com",
                      "https://evil.example.com"):
            with self.subTest(host=host):
                with self.assertRaises(MarketDataError) as ctx:
                    bad_client._get(host, "/api/v1/reports/latest", deadline=10.0)
                self.assertEqual(ctx.exception.code, "invalid_host")

    def test_http_error_mapped_to_marketdataerror(self):
        def handler(request):
            return httpx.Response(500, stream=httpx.ByteStream(b"server error"))

        client, reader = self.reader(handler)
        self.addCleanup(client.close)
        with self.assertRaises(MarketDataError) as ctx:
            reader.get_latest_report(deadline=10.0)
        self.assertEqual(ctx.exception.code, "chainlink_http_error")

    def test_401_unauthorized_failure(self):
        def handler(request):
            return httpx.Response(401, stream=httpx.ByteStream(b"unauthorized"))

        client, reader = self.reader(handler)
        self.addCleanup(client.close)
        with self.assertRaises(MarketDataError) as ctx:
            reader.get_latest_report(deadline=10.0)
        self.assertEqual(ctx.exception.code, "chainlink_auth_failed")

    def test_malformed_json_response(self):
        def handler(request):
            return httpx.Response(200, stream=httpx.ByteStream(b"not-json"))

        client, reader = self.reader(handler)
        self.addCleanup(client.close)
        with self.assertRaises(MarketDataError) as ctx:
            reader.get_latest_report(deadline=10.0)
        self.assertEqual(ctx.exception.code, "invalid_response")

    def test_timeout_deadline_respected(self):
        def handler(request):
            raise httpx.TimeoutException("simulated timeout")

        client, reader = self.reader(handler)
        self.addCleanup(client.close)
        with self.assertRaises(MarketDataError) as ctx:
            reader.get_latest_report(deadline=0.01)
        self.assertIn(ctx.exception.code, ("chainlink_timeout", "transport_error"))

    def test_response_size_ceiling(self):
        def handler(request):
            return httpx.Response(200, stream=httpx.ByteStream(b"x" * (11 * 1024 * 1024)))

        client, reader = self.reader(handler)
        self.addCleanup(client.close)
        with self.assertRaises(MarketDataError) as ctx:
            reader.get_latest_report(deadline=10.0)
        self.assertEqual(ctx.exception.code, "response_too_large")

    def test_no_credentials_in_error_messages(self):
        def handler(request):
            return httpx.Response(401, stream=httpx.ByteStream(b"unauthorized"))

        client, reader = self.reader(handler, client_secret="secret-key-value-123")
        self.addCleanup(client.close)
        with self.assertRaises(MarketDataError) as ctx:
            reader.get_latest_report(deadline=10.0)
        self.assertNotIn("secret-key-value-123", str(ctx.exception))

    def test_host_is_case_insensitive_match(self):
        def handler(request):
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(make_valid_report_payload()).encode()))

        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(client.close)
        reader = ChainlinkRestClient(client, client_id="cid", client_secret="sec",
                                     clock_seconds=lambda: 1746000000, feed_id=CHAINLINK_FEED_ID)
        meta = reader.get_latest_report(deadline=10.0)
        self.assertIsNotNone(meta)
