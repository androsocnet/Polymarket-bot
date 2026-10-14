"""Tests for FREE_REFERENCE approximate observation model and provider protocol."""
import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import runtime_checkable

from polymarket_bot.recorder.free_reference import (
    ApproximateObservation,
    ApproximateReferenceProvider,
    ApproximateReferenceReport,
)


class CapturedSourcePolicyTests(unittest.TestCase):
    def test_missing_context_and_future_receipt_fail_closed(self):
        from polymarket_bot.recorder import free_reference as policy
        from polymarket_bot.recorder.models import ClockAnchor
        from dataclasses import replace
        self.assertTrue(hasattr(policy, 'AcquisitionContext'), 'captured acquisition policy missing')
        anchor = ClockAnchor(1, 1000, 1000, 0, 0, 1000, 1000000, 1000000, 'VALID')
        context = policy.AcquisitionContext(2, 1, anchor, 1, 1000, None)
        obs = ApproximateObservation('coinbase', 'BTC/USD', 'USD', Decimal('1'), None, None,
            1000000, 0, 1000000, '', 0, 'rest', 'coinbase-ticker-v1', 200, 'success', None, 1000)
        self.assertEqual(policy.source_time_status(obs, context), 'SOURCE_TIME_WITHIN_CLOCK_UNCERTAINTY')
        for changed in (replace(obs, receipt_mono_ns=None), replace(obs, receipt_mono_ns=999),
                replace(obs, receipt_mono_ns=120_000_001_001), replace(obs, receipt_server_us=1000001)):
            self.assertEqual(policy.source_time_status(changed, context), 'CLOCK_CONTEXT_UNAVAILABLE')
        with self.assertRaises(FrozenInstanceError):
            setattr(context, 'revision', 1)
        self.assertEqual(policy.source_time_status(obs, None), 'CLOCK_CONTEXT_UNAVAILABLE')


class FreeReferenceModelTests(unittest.TestCase):
    def test_observation_is_frozen_dataclass(self):
        obs = ApproximateObservation(
            provider="coinbase",
            pair="BTC/USD",
            quote_currency="USD",
            price=Decimal("50000.00"),
            bid_price=Decimal("49999.50"),
            ask_price=Decimal("50000.50"),
            source_timestamp_us=1_000_000_000_000,
            receipt_wall_us=1_000_000_000_001,
            receipt_server_us=1_000_000_000_002,
            raw_payload_hash="abc123",
            raw_payload_size=256,
            transport_type="rest",
            parser_revision="coinbase-ticker-v1",
            http_status=200,
            result="success",
            error_code=None,
        )
        with self.assertRaises(FrozenInstanceError):
            setattr(obs, "price", Decimal("1"))

    def test_report_mode_hardcoded_approximate(self):
        report = ApproximateReferenceReport(
            reference_kind="APPROXIMATE_SAMPLE_MEAN_60S",
            window_start_us=1_000_000_000_000,
            window_end_us=1_060_000_000_000,
            observation_count=10,
            source_provider="coinbase",
            was_fallback=False,
            receipt_wall_us=1_060_000_000_000,
            value=Decimal("50000.00"),
        )
        self.assertEqual(report.mode, "APPROXIMATE")
        self.assertEqual(report.verification_status, "UNVERIFIED")

    def test_protocol_is_runtime_checkable(self):
        self.assertTrue(runtime_checkable(ApproximateReferenceProvider))

    def test_observation_fields_present_and_types(self):
        obs = ApproximateObservation(
            provider="coinbase",
            pair="BTC/USD",
            quote_currency="USD",
            price=Decimal("50000.00"),
            bid_price=Decimal("49999.50"),
            ask_price=Decimal("50000.50"),
            source_timestamp_us=1_000_000_000_000,
            receipt_wall_us=1_000_000_000_001,
            receipt_server_us=1_000_000_000_002,
            raw_payload_hash="abc123",
            raw_payload_size=256,
            transport_type="rest",
            parser_revision="coinbase-ticker-v1",
            http_status=200,
            result="success",
            error_code=None,
        )
        self.assertEqual(obs.provider, "coinbase")
        self.assertEqual(obs.pair, "BTC/USD")
        self.assertEqual(obs.quote_currency, "USD")
        self.assertEqual(obs.price, Decimal("50000.00"))
        self.assertEqual(obs.bid_price, Decimal("49999.50"))
        self.assertEqual(obs.ask_price, Decimal("50000.50"))
        self.assertEqual(obs.source_timestamp_us, 1_000_000_000_000)
        self.assertEqual(obs.receipt_wall_us, 1_000_000_000_001)
        self.assertEqual(obs.receipt_server_us, 1_000_000_000_002)
        self.assertEqual(obs.raw_payload_hash, "abc123")
        self.assertEqual(obs.raw_payload_size, 256)
        self.assertEqual(obs.transport_type, "rest")
        self.assertEqual(obs.parser_revision, "coinbase-ticker-v1")
        self.assertEqual(obs.http_status, 200)
        self.assertEqual(obs.result, "success")
        self.assertIsNone(obs.error_code)

    def test_observation_price_can_be_negative_in_dataclass(self):
        """Dataclass doesn't enforce positive; validation at provider/consumer level."""
        obs = ApproximateObservation(
            provider="coinbase",
            pair="BTC/USD",
            quote_currency="USD",
            price=Decimal("-100"),
            bid_price=None,
            ask_price=None,
            source_timestamp_us=None,
            receipt_wall_us=1_000_000_000_000,
            receipt_server_us=None,
            raw_payload_hash="abc123",
            raw_payload_size=256,
            transport_type="rest",
            parser_revision="coinbase-ticker-v1",
            http_status=200,
            result="success",
            error_code=None,
        )
        assert obs.price is not None
        self.assertTrue(obs.price < 0)

    def test_observation_source_timestamp_us_can_be_none(self):
        obs = ApproximateObservation(
            provider="kraken",
            pair="BTC/USD",
            quote_currency="USD",
            price=Decimal("50000.00"),
            bid_price=Decimal("49999.50"),
            ask_price=Decimal("50000.50"),
            source_timestamp_us=None,
            receipt_wall_us=1_000_000_000_000,
            receipt_server_us=None,
            raw_payload_hash="abc123",
            raw_payload_size=256,
            transport_type="rest",
            parser_revision="kraken-ticker-v1",
            http_status=200,
            result="success",
            error_code=None,
        )
        self.assertIsNone(obs.source_timestamp_us)
        self.assertIsNone(obs.receipt_server_us)

    def test_report_all_fields(self):
        report = ApproximateReferenceReport(
            reference_kind="APPROXIMATE_SAMPLE_MEAN_60S",
            window_start_us=1_000_000_000_000,
            window_end_us=1_060_000_000_000,
            observation_count=10,
            source_provider="coinbase",
            was_fallback=False,
            receipt_wall_us=1_060_000_000_000,
            value=Decimal("50000.00"),
            receipt_server_us=1_060_000_000_001,
            diagnostics=(),
        )
        self.assertEqual(report.reference_kind, "APPROXIMATE_SAMPLE_MEAN_60S")
        self.assertEqual(report.window_start_us, 1_000_000_000_000)
        self.assertEqual(report.window_end_us, 1_060_000_000_000)
        self.assertEqual(report.observation_count, 10)
        self.assertEqual(report.source_provider, "coinbase")
        self.assertFalse(report.was_fallback)
        self.assertEqual(report.receipt_wall_us, 1_060_000_000_000)
        self.assertEqual(report.value, Decimal("50000.00"))
        self.assertEqual(report.mode, "APPROXIMATE")
        self.assertEqual(report.verification_status, "UNVERIFIED")
        self.assertEqual(report.receipt_server_us, 1_060_000_000_001)
        self.assertEqual(report.diagnostics, ())

    def test_report_with_failed_computation(self):
        report = ApproximateReferenceReport(
            reference_kind="APPROXIMATE_SAMPLE_MEAN_60S",
            window_start_us=1_000_000_000_000,
            window_end_us=1_060_000_000_000,
            observation_count=0,
            source_provider="coinbase",
            was_fallback=True,
            receipt_wall_us=1_060_000_000_000,
            value=None,
            diagnostics=("EMPTY_WINDOW", "NO_OBSERVATIONS"),
        )
        self.assertIsNone(report.value)
        self.assertTrue(report.was_fallback)
        self.assertEqual(report.diagnostics, ("EMPTY_WINDOW", "NO_OBSERVATIONS"))

    def test_report_defaults(self):
        report = ApproximateReferenceReport(
            reference_kind="APPROXIMATE_SAMPLE_MEAN_60S",
            window_start_us=1_000_000_000_000,
            window_end_us=1_060_000_000_000,
            observation_count=10,
            source_provider="coinbase",
            was_fallback=False,
            receipt_wall_us=1_060_000_000_000,
        )
        self.assertEqual(report.mode, "APPROXIMATE")
        self.assertEqual(report.verification_status, "UNVERIFIED")
        self.assertIsNone(report.value)
        self.assertIsNone(report.receipt_server_us)
        self.assertEqual(report.diagnostics, ())

    def test_observation_bid_ask_can_be_none(self):
        obs = ApproximateObservation(
            provider="coinbase",
            pair="BTC/USD",
            quote_currency="USD",
            price=Decimal("50000.00"),
            bid_price=None,
            ask_price=None,
            source_timestamp_us=1_000_000_000_000,
            receipt_wall_us=1_000_000_000_001,
            receipt_server_us=None,
            raw_payload_hash="abc123",
            raw_payload_size=256,
            transport_type="rest",
            parser_revision="coinbase-ticker-v1",
            http_status=200,
            result="success",
            error_code=None,
        )
        self.assertIsNone(obs.bid_price)
        self.assertIsNone(obs.ask_price)


class FreeReferenceProtocolTests(unittest.TestCase):
    def test_provider_protocol_structure(self):
        """Verify ApproximateReferenceProvider has required properties and method."""
        self.assertTrue(hasattr(ApproximateReferenceProvider, "provider_name"))
        self.assertTrue(hasattr(ApproximateReferenceProvider, "pair"))
        self.assertTrue(hasattr(ApproximateReferenceProvider, "quote_currency"))
        self.assertTrue(hasattr(ApproximateReferenceProvider, "fetch_ticker"))

    def test_can_implement_provider(self):
        """Test that a class can implement the provider protocol."""

        class MockProvider:
            @property
            def provider_name(self) -> str:
                return "mock"

            @property
            def pair(self) -> str:
                return "BTC/USD"

            @property
            def quote_currency(self) -> str:
                return "USD"

            def fetch_ticker(self, *, deadline: float) -> ApproximateObservation:
                return ApproximateObservation(
                    provider="mock",
                    pair="BTC/USD",
                    quote_currency="USD",
                    price=Decimal("50000.00"),
                    bid_price=Decimal("49999.50"),
                    ask_price=Decimal("50000.50"),
                    source_timestamp_us=1_000_000_000_000,
                    receipt_wall_us=1_000_000_000_001,
                    receipt_server_us=None,
                    raw_payload_hash="abc123",
                    raw_payload_size=256,
                    transport_type="rest",
                    parser_revision="mock-v1",
                    http_status=200,
                    result="success",
                    error_code=None,
                )

        provider = MockProvider()
        self.assertIsInstance(provider, ApproximateReferenceProvider)
        obs = provider.fetch_ticker(deadline=10.0)
        self.assertEqual(obs.provider, "mock")


if __name__ == "__main__":
    unittest.main()