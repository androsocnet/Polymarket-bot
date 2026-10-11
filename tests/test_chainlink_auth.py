"""Tests for Chainlink HMAC Authentication & Request Signer."""
import hashlib
import hmac
import unittest

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.recorder.chainlink_auth import (
    build_chainlink_auth_headers,
    compute_chainlink_signature,
    verify_chainlink_signature,
    validate_credentials,
)


class ChainlinkAuthTests(unittest.TestCase):
    def test_hmac_signature_vector(self):
        client_id = "test-client-id"
        client_secret = "test-secret-key"
        method = "GET"
        path = "/api/v1/reports/latest"
        timestamp_ms = 1760000000000
        body = None

        expected_body_hash = hashlib.sha256(b"").hexdigest()
        expected_msg = f"{method} {path} {expected_body_hash} {client_id} {timestamp_ms}"
        expected_sig = hmac.new(
            client_secret.encode("utf-8"),
            expected_msg.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        headers = build_chainlink_auth_headers(
            client_id=client_id,
            client_secret=client_secret,
            method=method,
            path=path,
            body=body,
            timestamp_ms=timestamp_ms,
        )

        self.assertEqual(headers["Authorization"], client_id)
        self.assertEqual(headers["X-Authorization-Timestamp"], str(timestamp_ms))
        self.assertEqual(headers["X-Authorization-Signature-SHA256"], expected_sig)
        self.assertNotIn(client_secret, str(headers))

    def test_hmac_signature_with_body(self):
        client_id = "user1"
        client_secret = "secret1"
        body = b'{"feedId":"0x0002"}'
        headers = build_chainlink_auth_headers(
            client_id=client_id,
            client_secret=client_secret,
            method="POST",
            path="/api/v1/reports",
            body=body,
            timestamp_ms=1760000001000,
        )
        sig = headers["X-Authorization-Signature-SHA256"]
        self.assertTrue(
            verify_chainlink_signature(
                sig,
                client_id=client_id,
                client_secret=client_secret,
                method="POST",
                path="/api/v1/reports",
                body=body,
                timestamp_ms=1760000001000,
            )
        )
        # Tampered body should fail verification
        self.assertFalse(
            verify_chainlink_signature(
                sig,
                client_id=client_id,
                client_secret=client_secret,
                method="POST",
                path="/api/v1/reports",
                body=b'{"tampered":true}',
                timestamp_ms=1760000001000,
            )
        )

    def test_validate_credentials_success(self):
        cid, sec = validate_credentials("  my-client-id  ", "  my-secret-key  ")
        self.assertEqual(cid, "my-client-id")
        self.assertEqual(sec, "my-secret-key")

    def test_validate_credentials_failure(self):
        invalid_pairs = [
            (None, "secret"),
            ("", "secret"),
            ("   ", "secret"),
            (12345, "secret"),
            ("client", None),
            ("client", ""),
            ("client", "   "),
            ("client", 99999),
            ("c" * 257, "secret"),
            ("client", "s" * 257),
        ]
        for cid, sec in invalid_pairs:
            with self.subTest(cid=cid, sec=sec):
                with self.assertRaises(MarketDataError) as ctx:
                    validate_credentials(cid, sec)
                self.assertEqual(ctx.exception.code, "invalid_credentials")

    def test_timestamp_validation(self):
        for bad_ts in [-1, 0, "not-a-ts", 10**25]:
            with self.subTest(bad_ts=bad_ts):
                with self.assertRaises(MarketDataError) as ctx:
                    build_chainlink_auth_headers("cid", "sec", "GET", "/api/v1/feeds", None, bad_ts)
                self.assertIn(ctx.exception.code, ("invalid_numeric", "invalid_timestamp"))

    def test_path_and_method_validation(self):
        with self.assertRaises(MarketDataError) as ctx:
            build_chainlink_auth_headers("cid", "sec", "", "/path", None, 1000)
        self.assertEqual(ctx.exception.code, "invalid_method")

        with self.assertRaises(MarketDataError) as ctx:
            build_chainlink_auth_headers("cid", "sec", "GET", "no-slash", None, 1000)
        self.assertEqual(ctx.exception.code, "invalid_path")
