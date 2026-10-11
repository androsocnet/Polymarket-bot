"""Tests for Chainlink Schema v2 report decoding and numeric hardening."""
import json
import struct
import unittest
from dataclasses import replace
from decimal import Decimal, localcontext
from hashlib import sha256

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.recorder import models
from polymarket_bot.recorder.reference import (
    decode_schema_v2_report,
    normalize_reference_fields,
)
from tests.recorder_support import NOW_US


def build_schema_v2_report_payload(
    *,
    feed_id=models.CHAINLINK_FEED_ID,
    valid_from=100,
    observations=160,
    expires=220,
    price_int=6500012345678,
    bid_int=6499000000000,
    ask_int=6501000000000,
):
    signed_report = b"synthetic-signed-report-" + str(price_int).encode()
    report_blob = signed_report.hex()
    return {
        "report": {
            "feedId": feed_id,
            "fullReport": report_blob,
            "validFromTimestamp": valid_from,
            "observationsTimestamp": observations,
            "expiresAt": expires,
            "benchmarkPrice": str(price_int),
            "bid": str(bid_int),
            "ask": str(ask_int),
        },
    }, signed_report


class SchemaV2DecoderTests(unittest.TestCase):
    def test_decode_valid_schema_v2_report(self):
        payload, raw_report = build_schema_v2_report_payload(price_int=6500012345678)
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision="v2-offline",
            receipt_wall_us=NOW_US + 123,
            receipt_server_us=NOW_US,
        )
        self.assertEqual(result.decode_status, "DECODED")
        self.assertEqual(result.decoded_price, Decimal("65000.12345678"))
        self.assertEqual(result.verification_status, "UNVERIFIED")
        self.assertEqual(result.feed_id, models.CHAINLINK_FEED_ID)
        self.assertEqual(result.report_schema_version, 2)
        self.assertEqual(result.valid_from_seconds, 100)
        self.assertEqual(result.observations_seconds, 160)
        self.assertEqual(result.expires_at_seconds, 220)
        self.assertEqual(result.report_hash, sha256(raw_report).hexdigest())
        self.assertEqual(result.raw_signed_report, raw_report)
        self.assertEqual(result.first_received_wall_us, NOW_US + 123)
        self.assertEqual(result.first_received_server_us, NOW_US)
        self.assertEqual(result.diagnostics, ())

    def test_decode_survives_ambient_decimal_context(self):
        payload, raw_report = build_schema_v2_report_payload(price_int=6500012345678)
        with localcontext() as ctx:
            ctx.prec = 3
            result = decode_schema_v2_report(
                payload,
                price_scale=8,
                decoder_revision="v2-offline",
                receipt_wall_us=NOW_US + 123,
                receipt_server_us=NOW_US,
            )
        self.assertEqual(result.decode_status, "DECODED")
        self.assertEqual(result.decoded_price, Decimal("65000.12345678"))

    def test_decode_wrong_feed_id_is_invalid(self):
        payload, _ = build_schema_v2_report_payload(feed_id="0xdeadbeef")
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision="v2-offline",
            receipt_wall_us=NOW_US,
            receipt_server_us=None,
        )
        self.assertEqual(result.decode_status, "INVALID")
        self.assertIsNone(result.decoded_price)
        self.assertTrue(any("identity" in d for d in result.diagnostics))

    def test_decode_invalid_time_interval(self):
        payload, _ = build_schema_v2_report_payload(valid_from=200, observations=160, expires=220)
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision="v2-offline",
            receipt_wall_us=NOW_US,
            receipt_server_us=None,
        )
        self.assertEqual(result.decode_status, "INVALID")
        self.assertIsNone(result.decoded_price)

    def test_decode_negative_price_is_invalid(self):
        payload, _ = build_schema_v2_report_payload(price_int=-1)
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision="v2-offline",
            receipt_wall_us=NOW_US,
            receipt_server_us=None,
        )
        self.assertEqual(result.decode_status, "INVALID")
        self.assertIsNone(result.decoded_price)

    def test_decode_zero_price_is_invalid(self):
        payload, _ = build_schema_v2_report_payload(price_int=0)
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision="v2-offline",
            receipt_wall_us=NOW_US,
            receipt_server_us=None,
        )
        self.assertEqual(result.decode_status, "INVALID")
        self.assertIsNone(result.decoded_price)

    def test_decode_extreme_price_is_contained(self):
        payload, _ = build_schema_v2_report_payload(price_int=10**130)
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision="v2-offline",
            receipt_wall_us=NOW_US,
            receipt_server_us=None,
        )
        self.assertEqual(result.decode_status, "INVALID")
        self.assertIsNone(result.decoded_price)

    def test_decode_without_scale_is_unverified(self):
        payload, _ = build_schema_v2_report_payload()
        result = decode_schema_v2_report(
            payload,
            price_scale=None,
            decoder_revision="v2-offline",
            receipt_wall_us=NOW_US,
            receipt_server_us=None,
        )
        self.assertEqual(result.decode_status, "UNVERIFIED")
        self.assertIsNone(result.decoded_price)

    def test_decode_without_revision_is_unverified(self):
        payload, _ = build_schema_v2_report_payload()
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision=None,
            receipt_wall_us=NOW_US,
            receipt_server_us=None,
        )
        self.assertEqual(result.decode_status, "UNVERIFIED")
        self.assertIsNone(result.decoded_price)

    def test_decode_malformed_payload_structure(self):
        for bad_payload in (
            None,
            "not a dict",
            {},
            {"report": "not-a-dict"},
            {"report": {}},
            {"report": {"feedId": models.CHAINLINK_FEED_ID}},
        ):
            with self.subTest(bad_payload=str(bad_payload)[:50]):
                result = decode_schema_v2_report(
                    bad_payload,
                    price_scale=8,
                    decoder_revision="v2-offline",
                    receipt_wall_us=NOW_US,
                    receipt_server_us=None,
                )
                self.assertEqual(result.decode_status, "INVALID")
                self.assertIsNone(result.decoded_price)

    def test_decode_missing_full_report_bytes(self):
        payload = {
            "report": {
                "feedId": models.CHAINLINK_FEED_ID,
                "validFromTimestamp": 100,
                "observationsTimestamp": 160,
                "expiresAt": 220,
                "benchmarkPrice": "6500012345678",
            },
        }
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision="v2-offline",
            receipt_wall_us=NOW_US,
            receipt_server_us=None,
        )
        self.assertEqual(result.decode_status, "INVALID")

    def test_decode_invalid_hex_full_report(self):
        payload = {
            "report": {
                "feedId": models.CHAINLINK_FEED_ID,
                "fullReport": "not-valid-hex!!",
                "validFromTimestamp": 100,
                "observationsTimestamp": 160,
                "expiresAt": 220,
                "benchmarkPrice": "6500012345678",
            },
        }
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision="v2-offline",
            receipt_wall_us=NOW_US,
            receipt_server_us=None,
        )
        self.assertEqual(result.decode_status, "INVALID")

    def test_decode_oversized_full_report_bytes(self):
        payload = {
            "report": {
                "feedId": models.CHAINLINK_FEED_ID,
                "fullReport": ("aa" * (10_485_761)),
                "validFromTimestamp": 100,
                "observationsTimestamp": 160,
                "expiresAt": 220,
                "benchmarkPrice": "1",
            },
        }
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision="v2-offline",
            receipt_wall_us=NOW_US,
            receipt_server_us=None,
        )
        self.assertEqual(result.decode_status, "INVALID")

    def test_decode_preserves_raw_report_bytes(self):
        payload, raw_report = build_schema_v2_report_payload()
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision="v2-offline",
            receipt_wall_us=NOW_US,
            receipt_server_us=NOW_US,
        )
        self.assertEqual(result.raw_signed_report, raw_report)
        self.assertEqual(result.report_hash, sha256(raw_report).hexdigest())

    def test_decode_extreme_timestamp_is_invalid(self):
        payload, _ = build_schema_v2_report_payload(valid_from=0, observations=2**63, expires=2**63 + 1)
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision="v2-offline",
            receipt_wall_us=NOW_US,
            receipt_server_us=None,
        )
        self.assertEqual(result.decode_status, "INVALID")
        self.assertIsNone(result.decoded_price)

    def test_decode_string_price_with_noninteger_format(self):
        payload, _ = build_schema_v2_report_payload()
        payload["report"]["benchmarkPrice"] = "65000.123"
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision="v2-offline",
            receipt_wall_us=NOW_US,
            receipt_server_us=None,
        )
        self.assertEqual(result.decode_status, "INVALID")
        self.assertIsNone(result.decoded_price)

    def test_decode_flat_report_payload(self):
        signed_report = b"flat-signed-report"
        payload = {
            "feedId": models.CHAINLINK_FEED_ID,
            "fullReport": signed_report.hex(),
            "validFromTimestamp": 100,
            "observationsTimestamp": 160,
            "expiresAt": 220,
            "benchmarkPrice": "5000000000000",
        }
        result = decode_schema_v2_report(
            payload,
            price_scale=8,
            decoder_revision="v2-flat",
            receipt_wall_us=NOW_US,
            receipt_server_us=NOW_US,
        )
        self.assertEqual(result.decode_status, "DECODED")
        self.assertEqual(result.decoded_price, Decimal("50000.00000000"))
        self.assertEqual(result.raw_signed_report, signed_report)
