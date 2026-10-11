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
    word = lambda value: (value % (1 << 256)).to_bytes(32, 'big')
    feed = bytes.fromhex(feed_id[2:]) if len(feed_id) == 66 else bytes(32)
    blob = feed + b''.join(word(value) for value in
        (valid_from, observations, 0, 0, expires, price_int))
    signed_report = (bytes(96) + word(224) + word(480) + word(544) + bytes(32)
        + word(224) + blob + word(1) + word(1) + word(1) + word(2))
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


def managed_authorization(revision):
    from polymarket_bot.recorder.reference import ManagedAuthorization
    return ManagedAuthorization('offline-test-authority', 'injected-test-permission',
        models.CHAINLINK_FEED_ID, revision, 'canonical-ABI-regression-suite')


class SchemaV2DecoderTests(unittest.TestCase):
    def test_managed_production_rejects_boolean_only_authority(self):
        from polymarket_bot.recorder.reference import ManagedReferenceSession, ActivationEvidence
        session = ManagedReferenceSession(evidence=ActivationEvidence(*([True]*6)),
            discovery=lambda d: self.fail('Discovery without authority'),
            credentials=lambda: self.fail('Credentials without authority'),
            monotonic_ns=lambda: 0, wall_us=lambda: NOW_US, decoder_revision='abi')
        with self.assertRaises(MarketDataError) as caught:
            session.start('REFERENCE_REQUIRED', 2)
        self.assertEqual(caught.exception.code, 'activation_required')

    def test_managed_lifecycle_gates_cleanup_and_idempotence(self):
        from polymarket_bot.recorder.reference import ManagedReferenceSession, ActivationEvidence
        calls = []
        class Rest:
            def close(self): calls.append('rest-close')
        class Live:
            def start(self): calls.append('live-start')
            def stop(self): calls.append('live-stop')
            def get_freshest_report(self, *args): return None
            def get_connection_gaps(self): return ()
        discovery = {'feedId': models.CHAINLINK_FEED_ID,
            'name': models.CHAINLINK_FEED_NAME, 'status': 'active', 'schemaVersion': 2, 'decimals': 8}
        session = ManagedReferenceSession(evidence=ActivationEvidence(*([True]*6)),
            discovery=lambda deadline: discovery, credentials=lambda: calls.append('credentials') or ('id', 'secret'),
            rest_factory=lambda *args: Rest(), live_factory=lambda *args: Live(),
            monotonic_ns=lambda: 0, wall_us=lambda: NOW_US, decoder_revision='abi-v2', authorization=managed_authorization('abi-v2'))
        session.start('BOOK_ONLY', 2)
        self.assertEqual(calls, [])
        session.start('REFERENCE_REQUIRED', 2)
        self.assertEqual(calls, ['credentials', 'live-start'])
        session.stop(); session.stop()
        self.assertEqual(calls, ['credentials', 'live-start', 'live-stop', 'rest-close'])
        denied = ManagedReferenceSession(evidence=ActivationEvidence(*([False]*6)),
            discovery=lambda d: self.fail('Discovery before permission'),
            credentials=lambda: self.fail('Credentials before permission'),
            monotonic_ns=lambda: 0, wall_us=lambda: NOW_US, decoder_revision='abi-v2', authorization=managed_authorization('abi-v2'))
        with self.assertRaises(MarketDataError): denied.start('REFERENCE_REQUIRED', 2)

    def test_decoder_rejects_noncanonical_hex_before_allocation(self):
        payload, raw = build_schema_v2_report_payload()
        payload['report']['fullReport'] = raw.hex()[:64] + ' ' + raw.hex()[64:]
        result = decode_schema_v2_report(payload, price_scale=8, decoder_revision='abi',
            receipt_wall_us=NOW_US, receipt_server_us=NOW_US)
        self.assertEqual(result.decode_status, 'INVALID')

    def test_decoder_rejects_noncanonical_offsets_lengths_and_words(self):
        payload, raw = build_schema_v2_report_payload()
        for offset, value in ((96, 225), (128, 224), (160, 2**255), (224, 223),
                              (480, 33), (544, 0), (288, 2**32), (352, 2**192),
                              (448, 2**191)):
            with self.subTest(offset=offset):
                changed = raw[:offset] + value.to_bytes(32, 'big') + raw[offset+32:]
                payload['report']['fullReport'] = changed.hex()
                result = decode_schema_v2_report(payload, price_scale=8, decoder_revision='abi',
                    receipt_wall_us=NOW_US, receipt_server_us=NOW_US)
                self.assertEqual(result.decode_status, 'INVALID')
                self.assertEqual(result.report_hash, sha256(changed).hexdigest())

    def test_signed_only_transport_has_canonical_timestamp_fields(self):
        from polymarket_bot.recorder.chainlink_rest import parse_report
        payload, raw = build_schema_v2_report_payload()
        signed, fields, draft = parse_report({'fullReport': raw.hex()})
        self.assertEqual(fields['observationsTimestamp'], 160)
        self.assertEqual(fields['validFromTimestamp'], 100)
        self.assertEqual(fields['benchmarkPrice'], '6500012345678')

    def test_managed_pin_conflict_precedes_credentials(self):
        from polymarket_bot.recorder.reference import ManagedReferenceSession, ActivationEvidence
        session = ManagedReferenceSession(evidence=ActivationEvidence(*([True]*6)),
            discovery=lambda d: {'feedId': models.CHAINLINK_FEED_ID, 'name': models.CHAINLINK_FEED_NAME,
                'status': 'active', 'schemaVersion': 2, 'decimals': 8},
            credentials=lambda: self.fail('Credentials loaded despite persisted pin conflict'),
            monotonic_ns=lambda: 0, wall_us=lambda: NOW_US, decoder_revision='abi', authorization=managed_authorization('abi'))
        with self.assertRaises(MarketDataError) as caught:
            session.start('REFERENCE_REQUIRED', 2, persisted_pin={'price_scale': 9, 'decoder_revision': 'abi'})
        self.assertEqual(caught.exception.code, 'identity_conflict')

    def test_managed_start_failure_closes_rest_and_consumer(self):
        from polymarket_bot.recorder.reference import ManagedReferenceSession, ActivationEvidence
        calls = []
        class Rest:
            def close(self): calls.append('rest-close')
        class Live:
            def start(self): raise MarketDataError('websocket_failure', 'Synthetic failure')
            def stop(self): calls.append('live-stop')
        session = ManagedReferenceSession(evidence=ActivationEvidence(*([True]*6)),
            discovery=lambda d: {'feedId': models.CHAINLINK_FEED_ID, 'name': models.CHAINLINK_FEED_NAME,
                'status': 'active', 'schemaVersion': 2, 'decimals': 8},
            credentials=lambda: ('id', 'secret'), rest_factory=lambda *a: Rest(),
            live_factory=lambda *a: Live(), monotonic_ns=lambda: 0, wall_us=lambda: NOW_US,
            decoder_revision='abi', authorization=managed_authorization('abi'))
        with self.assertRaises(MarketDataError): session.start('REFERENCE_REQUIRED', 2)
        session.stop()
        self.assertEqual(calls, ['live-stop', 'rest-close'])
        self.assertIsNone(session.rest)
        self.assertIsNone(session.consumer)

    def test_inclusive_boundary_selection_and_overlap(self):
        from polymarket_bot.recorder.reference import select_boundary_report
        payload, raw = build_schema_v2_report_payload()
        report = decode_schema_v2_report(payload, price_scale=8, decoder_revision='abi',
            receipt_wall_us=NOW_US, receipt_server_us=NOW_US)
        for target in (100, 160, 120):
            self.assertEqual(select_boundary_report((report,), target*1_000_000), report)
        for target in (99, 161):
            self.assertIsNone(select_boundary_report((report,), target*1_000_000))
        self.assertIsNone(select_boundary_report((report,), 100_000_001))
        with self.assertRaises(MarketDataError) as caught:
            select_boundary_report((report, replace(report, report_hash='other')), 120_000_000)
        self.assertEqual(caught.exception.code, 'BOUNDARY_AMBIGUOUS')

    def test_signed_content_without_envelope_fields(self):
        payload, raw = build_schema_v2_report_payload()
        result = decode_schema_v2_report({'fullReport': raw.hex()}, price_scale=8,
            decoder_revision='abi-v2', receipt_wall_us=NOW_US, receipt_server_us=NOW_US)
        self.assertEqual(result.decode_status, 'DECODED')
        self.assertEqual(result.observations_seconds, 160)
        self.assertEqual(result.decoded_price, Decimal('65000.12345678'))

    def test_truncated_signed_content_rejected(self):
        payload, raw = build_schema_v2_report_payload()
        for size in (0, 31, 223, 255, 479, 575, len(raw)-1):
            with self.subTest(size=size):
                payload['report']['fullReport'] = raw[:size].hex()
                result = decode_schema_v2_report(payload, price_scale=8,
                    decoder_revision='abi-v2', receipt_wall_us=NOW_US, receipt_server_us=NOW_US)
                self.assertEqual(result.decode_status, 'INVALID')

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
        nested, signed_report = build_schema_v2_report_payload(price_int=5000000000000)
        payload = nested['report']
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
