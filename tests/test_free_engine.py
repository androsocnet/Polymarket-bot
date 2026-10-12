"""Tests for APPROXIMATE_SAMPLE_MEAN_60S engine and ApproximateReferenceSession."""
import httpx
import unittest
from decimal import Decimal
from datetime import datetime, timezone

from polymarket_bot.recorder.free_reference import (
    ApproximateObservation,
    ApproximateReferenceProvider,
    ApproximateReferenceReport,
    CoinbaseRestProvider,
    KrakenRestProvider,
    compute_approximate_sample_mean_60s,
    ApproximateReferenceSession,
    _WINDOW_US,
)


class SampleMeanEngineTests(unittest.TestCase):
    def setUp(self):
        self.receipt_wall_us = 2_000_000_000_000_000
        self.receipt_server_us = 1_500_000_000_000

    def _make_coinbase_obs(self, price: str, source_ts_us: int | None = None, receipt_us: int | None = None, error_code: str | None = None) -> ApproximateObservation:
        receipt = receipt_us or self.receipt_wall_us
        return ApproximateObservation(
            provider="coinbase",
            pair="BTC/USD",
            quote_currency="USD",
            price=Decimal(price),
            bid_price=Decimal(price),
            ask_price=Decimal(price),
            source_timestamp_us=source_ts_us,
            receipt_wall_us=receipt,
            receipt_server_us=receipt // 1000,
            raw_payload_hash="abc",
            raw_payload_size=100,
            transport_type="rest",
            parser_revision="coinbase-ticker-v1",
            http_status=200,
            result="error" if error_code else "success",
            error_code=error_code,
        )

    def _make_kraken_obs(self, price: str, receipt_us: int | None = None, error_code: str | None = None) -> ApproximateObservation:
        receipt = receipt_us or self.receipt_wall_us
        return ApproximateObservation(
            provider="kraken",
            pair="BTC/USD",
            quote_currency="USD",
            price=Decimal(price),
            bid_price=Decimal(price),
            ask_price=Decimal(price),
            source_timestamp_us=None,
            receipt_wall_us=receipt,
            receipt_server_us=receipt // 1000,
            raw_payload_hash="def",
            raw_payload_size=100,
            transport_type="rest",
            parser_revision="kraken-ticker-v1",
            http_status=200,
            result="error" if error_code else "success",
            error_code=error_code,
        )

    def test_coinbase_selected_when_valid(self):
        """Coinbase observation within window and valid → used as primary."""
        obs = self._make_coinbase_obs("50000.00", source_ts_us=self.receipt_wall_us - 1_000_000)
        report = compute_approximate_sample_mean_60s(
            [obs], receipt_server_us=self.receipt_server_us, receipt_wall_us=self.receipt_wall_us
        )
        self.assertEqual(report.value, Decimal("50000.00"))
        self.assertEqual(report.observation_count, 1)
        self.assertEqual(report.source_provider, "coinbase")
        self.assertFalse(report.was_fallback)
        self.assertEqual(report.reference_kind, "APPROXIMATE_SAMPLE_MEAN_60S")
        self.assertEqual(report.verification_status, "UNVERIFIED")
        self.assertEqual(report.mode, "APPROXIMATE")

    def test_kraken_fallback_when_coinbase_fails(self):
        """Coinbase error observation → Kraken fallback used."""
        coinbase = self._make_coinbase_obs("50000.00", error_code="HTTP_ERROR")
        kraken = self._make_kraken_obs("50100.00")
        report = compute_approximate_sample_mean_60s(
            [coinbase, kraken], receipt_server_us=self.receipt_server_us, receipt_wall_us=self.receipt_wall_us
        )
        self.assertEqual(report.value, Decimal("50100.00"))
        self.assertEqual(report.source_provider, "kraken")
        self.assertIn("EXCLUDED_ERROR_OBSERVATIONS", report.diagnostics)

    def test_both_missing_value_none(self):
        """Both providers missing/error → value=None, diagnostics recorded."""
        coinbase = self._make_coinbase_obs("50000.00", error_code="TRANSPORT_TIMEOUT")
        kraken = self._make_kraken_obs("50100.00", error_code="TRANSPORT_TIMEOUT")
        report = compute_approximate_sample_mean_60s(
            [coinbase, kraken], receipt_server_us=self.receipt_server_us, receipt_wall_us=self.receipt_wall_us
        )
        self.assertIsNone(report.value)
        self.assertEqual(report.observation_count, 0)
        self.assertIn("EXCLUDED_ERROR_OBSERVATIONS", report.diagnostics)

    def test_future_observation_rejected(self):
        """source_timestamp_us > receipt_wall_us → excluded from window."""
        future_ts = self.receipt_wall_us + 5_000_000
        obs = self._make_coinbase_obs("50000.00", source_ts_us=future_ts)
        report = compute_approximate_sample_mean_60s(
            [obs], receipt_server_us=self.receipt_server_us, receipt_wall_us=self.receipt_wall_us
        )
        self.assertIsNone(report.value)
        self.assertEqual(report.observation_count, 0)
        self.assertIn("EXCLUDED_FUTURE_OBSERVATIONS", report.diagnostics)

    def test_stale_observation_excluded(self):
        """Observation older than 60s window → excluded."""
        stale_receipt = self.receipt_wall_us - _WINDOW_US - 10_000_000
        obs = self._make_coinbase_obs("50000.00", source_ts_us=stale_receipt, receipt_us=stale_receipt)
        report = compute_approximate_sample_mean_60s(
            [obs], receipt_server_us=self.receipt_server_us, receipt_wall_us=self.receipt_wall_us
        )
        self.assertIsNone(report.value)
        self.assertEqual(report.observation_count, 0)
        self.assertIn("EXCLUDED_STALE_OBSERVATIONS", report.diagnostics)

    def test_deterministic_same_inputs_same_output(self):
        """Same inputs always produce identical report."""
        obs1 = self._make_coinbase_obs("50000.00", source_ts_us=self.receipt_wall_us - 1_000_000)
        obs2 = self._make_coinbase_obs("50000.00", source_ts_us=self.receipt_wall_us - 1_000_000)
        report1 = compute_approximate_sample_mean_60s(
            [obs1], receipt_server_us=self.receipt_server_us, receipt_wall_us=self.receipt_wall_us
        )
        report2 = compute_approximate_sample_mean_60s(
            [obs2], receipt_server_us=self.receipt_server_us, receipt_wall_us=self.receipt_wall_us
        )
        self.assertEqual(report1.value, report2.value)
        self.assertEqual(report1.window_start_us, report2.window_start_us)
        self.assertEqual(report1.window_end_us, report2.window_end_us)
        self.assertEqual(report1.observation_count, report2.observation_count)
        self.assertEqual(report1.source_provider, report2.source_provider)
        self.assertEqual(report1.was_fallback, report2.was_fallback)
        self.assertEqual(report1.diagnostics, report2.diagnostics)

    def test_arithmetic_mean_correct(self):
        """Arithmetic mean of prices across multiple valid observations."""
        obs1 = self._make_coinbase_obs("50000.00", source_ts_us=self.receipt_wall_us - 1_000_000, receipt_us=self.receipt_wall_us - 10_000_000)
        obs2 = self._make_coinbase_obs("51000.00", source_ts_us=self.receipt_wall_us - 500_000, receipt_us=self.receipt_wall_us - 5_000_000)
        obs3 = self._make_kraken_obs("52000.00", receipt_us=self.receipt_wall_us)
        report = compute_approximate_sample_mean_60s(
            [obs1, obs2, obs3], receipt_server_us=self.receipt_server_us, receipt_wall_us=self.receipt_wall_us
        )
        expected = (Decimal("50000.00") + Decimal("51000.00") + Decimal("52000.00")) / 3
        self.assertEqual(report.value, expected)
        self.assertEqual(report.observation_count, 3)
        self.assertEqual(report.source_provider, "coinbase,kraken")

    def test_kraken_source_timestamp_none_handled(self):
        """Kraken observation with source_timestamp_us=None is handled correctly."""
        obs = self._make_kraken_obs("50000.00")
        report = compute_approximate_sample_mean_60s(
            [obs], receipt_server_us=self.receipt_server_us, receipt_wall_us=self.receipt_wall_us
        )
        self.assertEqual(report.value, Decimal("50000.00"))
        self.assertEqual(report.observation_count, 1)
        self.assertEqual(report.source_provider, "kraken")

    def test_no_lookahead(self):
        """Window end is receipt time, not future; no look-ahead allowed."""
        future_ts = self.receipt_wall_us + 10_000_000
        obs = self._make_coinbase_obs("50000.00", source_ts_us=future_ts, receipt_us=self.receipt_wall_us)
        report = compute_approximate_sample_mean_60s(
            [obs], receipt_server_us=self.receipt_server_us, receipt_wall_us=self.receipt_wall_us
        )
        self.assertIsNone(report.value)
        self.assertEqual(report.observation_count, 0)

    def test_window_bounds_inclusive(self):
        """Observations at exact window boundaries are included."""
        start_bound = self.receipt_wall_us - _WINDOW_US
        end_bound = self.receipt_wall_us
        obs_start = self._make_coinbase_obs("50000.00", source_ts_us=start_bound, receipt_us=start_bound)
        obs_end = self._make_coinbase_obs("51000.00", source_ts_us=end_bound, receipt_us=end_bound)
        report = compute_approximate_sample_mean_60s(
            [obs_start, obs_end], receipt_server_us=self.receipt_server_us, receipt_wall_us=self.receipt_wall_us
        )
        self.assertEqual(report.observation_count, 2)
        expected = (Decimal("50000.00") + Decimal("51000.00")) / 2
        self.assertEqual(report.value, expected)

    def test_observation_at_window_end_with_future_source_rejected(self):
        """Observation at window end but with future source_timestamp is rejected."""
        obs = self._make_coinbase_obs(
            "50000.00",
            source_ts_us=self.receipt_wall_us + 1_000_000,
            receipt_us=self.receipt_wall_us
        )
        report = compute_approximate_sample_mean_60s(
            [obs], receipt_server_us=self.receipt_server_us, receipt_wall_us=self.receipt_wall_us
        )
        self.assertIsNone(report.value)
        self.assertEqual(report.observation_count, 0)
        self.assertIn("EXCLUDED_FUTURE_OBSERVATIONS", report.diagnostics)


class MockProvider(ApproximateReferenceProvider):
    def __init__(self, name: str, response: ApproximateObservation):
        self._name = name
        self._response = response
        self._pair = "BTC/USD"
        self._quote = "USD"

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def pair(self) -> str:
        return self._pair

    @property
    def quote_currency(self) -> str:
        return self._quote

    def fetch_ticker(self, *, deadline: float) -> ApproximateObservation:
        return self._response


class ApproximateReferenceSessionTests(unittest.TestCase):
    def setUp(self):
        self.monotonic_ns = 1_000_000_000_000_000
        self.wall_us = 2_000_000_000_000_000
        self.project = lambda ns: ns // 1000

    def _make_obs(self, provider: str, price: str, error_code: str | None = None, receipt_us: int | None = None) -> ApproximateObservation:
        receipt = receipt_us or self.wall_us
        return ApproximateObservation(
            provider=provider,
            pair="BTC/USD",
            quote_currency="USD",
            price=Decimal(price),
            bid_price=Decimal(price),
            ask_price=Decimal(price),
            source_timestamp_us=receipt - 1_000_000 if provider == "coinbase" else None,
            receipt_wall_us=receipt,
            receipt_server_us=receipt // 1000,
            raw_payload_hash="abc",
            raw_payload_size=100,
            transport_type="rest",
            parser_revision=f"{provider}-ticker-v1",
            http_status=200 if not error_code else 500,
            result="error" if error_code else "success",
            error_code=error_code,
        )

    def test_fetch_returns_report_and_observations(self):
        """Session.fetch returns (report, tuple_of_observations)."""
        coinbase_obs = self._make_obs("coinbase", "50000.00")
        kraken_obs = self._make_obs("kraken", "50100.00")
        providers = {"coinbase": MockProvider("coinbase", coinbase_obs), "kraken": MockProvider("kraken", kraken_obs)}
        session = ApproximateReferenceSession(
            providers=providers,
            monotonic_ns=lambda: self.monotonic_ns,
            wall_us=lambda: self.wall_us,
            sleep=lambda s: None,
            project=self.project,
        )
        session.start()
        report, observations = session.fetch(
            run_id="test-run",
            anchor_id=1,
            project=self.project,
            deadline=self.wall_us / 1_000_000 + 5.0,
        )
        self.assertIsInstance(report, ApproximateReferenceReport)
        self.assertIsInstance(observations, tuple)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].provider, "coinbase")

    def test_coinbase_primary(self):
        """Coinbase is primary when valid."""
        coinbase_obs = self._make_obs("coinbase", "50000.00")
        kraken_obs = self._make_obs("kraken", "50100.00")
        providers = {"coinbase": MockProvider("coinbase", coinbase_obs), "kraken": MockProvider("kraken", kraken_obs)}
        session = ApproximateReferenceSession(
            providers=providers,
            monotonic_ns=lambda: self.monotonic_ns,
            wall_us=lambda: self.wall_us,
            sleep=lambda s: None,
            project=self.project,
        )
        session.start()
        report, observations = session.fetch(
            run_id="test-run",
            anchor_id=1,
            project=self.project,
            deadline=self.wall_us / 1_000_000 + 5.0,
        )
        self.assertEqual(report.source_provider, "coinbase")
        self.assertFalse(report.was_fallback)
        self.assertEqual(report.value, Decimal("50000.00"))
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].provider, "coinbase")

    def test_kraken_fallback_when_coinbase_fails(self):
        """Kraken used when Coinbase fails."""
        coinbase_obs = self._make_obs("coinbase", "50000.00", error_code="HTTP_ERROR")
        kraken_obs = self._make_obs("kraken", "50100.00")
        providers = {"coinbase": MockProvider("coinbase", coinbase_obs), "kraken": MockProvider("kraken", kraken_obs)}
        session = ApproximateReferenceSession(
            providers=providers,
            monotonic_ns=lambda: self.monotonic_ns,
            wall_us=lambda: self.wall_us,
            sleep=lambda s: None,
            project=self.project,
        )
        session.start()
        report, observations = session.fetch(
            run_id="test-run",
            anchor_id=1,
            project=self.project,
            deadline=self.wall_us / 1_000_000 + 5.0,
        )
        self.assertEqual(report.source_provider, "kraken")
        self.assertTrue(report.was_fallback)
        self.assertEqual(report.value, Decimal("50100.00"))
        self.assertIn("FALLBACK_COINBASE_HTTP_ERROR", report.diagnostics)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].provider, "kraken")

    def test_both_fail(self):
        """Both providers fail → value=None, diagnostics recorded."""
        coinbase_obs = self._make_obs("coinbase", "50000.00", error_code="TRANSPORT_TIMEOUT")
        kraken_obs = self._make_obs("kraken", "50100.00", error_code="TRANSPORT_TIMEOUT")
        providers = {"coinbase": MockProvider("coinbase", coinbase_obs), "kraken": MockProvider("kraken", kraken_obs)}
        session = ApproximateReferenceSession(
            providers=providers,
            monotonic_ns=lambda: self.monotonic_ns,
            wall_us=lambda: self.wall_us,
            sleep=lambda s: None,
            project=self.project,
        )
        session.start()
        report, observations = session.fetch(
            run_id="test-run",
            anchor_id=1,
            project=self.project,
            deadline=self.wall_us / 1_000_000 + 5.0,
        )
        self.assertIsNone(report.value)
        self.assertTrue(report.was_fallback)
        self.assertEqual(report.observation_count, 0)
        self.assertIn("FALLBACK_COINBASE_TRANSPORT_TIMEOUT", report.diagnostics)
        self.assertIn("FALLBACK_KRAKEN_TRANSPORT_TIMEOUT", report.diagnostics)
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0].provider, "coinbase")
        self.assertEqual(observations[1].provider, "kraken")

    def test_lifecycle_start_stop(self):
        """start/stop are no-ops for REST providers."""
        coinbase_obs = self._make_obs("coinbase", "50000.00")
        kraken_obs = self._make_obs("kraken", "50100.00")
        providers = {"coinbase": MockProvider("coinbase", coinbase_obs), "kraken": MockProvider("kraken", kraken_obs)}
        session = ApproximateReferenceSession(
            providers=providers,
            monotonic_ns=lambda: self.monotonic_ns,
            wall_us=lambda: self.wall_us,
            sleep=lambda s: None,
            project=self.project,
        )
        session.start()
        session.stop()
        session.start()
        report, _ = session.fetch(
            run_id="test-run",
            anchor_id=1,
            project=self.project,
            deadline=self.wall_us / 1_000_000 + 5.0,
        )
        self.assertIsNotNone(report)

    def test_check_health_noop(self):
        """check_health is a no-op for REST providers."""
        coinbase_obs = self._make_obs("coinbase", "50000.00")
        kraken_obs = self._make_obs("kraken", "50100.00")
        providers = {"coinbase": MockProvider("coinbase", coinbase_obs), "kraken": MockProvider("kraken", kraken_obs)}
        session = ApproximateReferenceSession(
            providers=providers,
            monotonic_ns=lambda: self.monotonic_ns,
            wall_us=lambda: self.wall_us,
            sleep=lambda s: None,
            project=self.project,
        )
        session.start()
        session.check_health()
        report, _ = session.fetch(
            run_id="test-run",
            anchor_id=1,
            project=self.project,
            deadline=self.wall_us / 1_000_000 + 5.0,
        )
        self.assertIsNotNone(report)

    def test_fallback_records_was_fallback_true(self):
        """was_fallback=True when Kraken is used."""
        coinbase_obs = self._make_obs("coinbase", "50000.00", error_code="STALE_OBSERVATION")
        kraken_obs = self._make_obs("kraken", "50100.00")
        providers = {"coinbase": MockProvider("coinbase", coinbase_obs), "kraken": MockProvider("kraken", kraken_obs)}
        session = ApproximateReferenceSession(
            providers=providers,
            monotonic_ns=lambda: self.monotonic_ns,
            wall_us=lambda: self.wall_us,
            sleep=lambda s: None,
            project=self.project,
        )
        session.start()
        report, _ = session.fetch(
            run_id="test-run",
            anchor_id=1,
            project=self.project,
            deadline=self.wall_us / 1_000_000 + 5.0,
        )
        self.assertTrue(report.was_fallback)
        self.assertEqual(report.source_provider, "kraken")

    def test_both_observations_preserved_in_output(self):
        """When fallback occurs and primary was valid but stale, both observations returned."""
        stale_receipt = self.wall_us - _WINDOW_US - 10_000_000
        coinbase_obs = self._make_obs("coinbase", "50000.00", receipt_us=stale_receipt)
        kraken_obs = self._make_obs("kraken", "50100.00")
        providers = {"coinbase": MockProvider("coinbase", coinbase_obs), "kraken": MockProvider("kraken", kraken_obs)}
        session = ApproximateReferenceSession(
            providers=providers,
            monotonic_ns=lambda: self.monotonic_ns,
            wall_us=lambda: self.wall_us,
            sleep=lambda s: None,
            project=self.project,
        )
        session.start()
        report, observations = session.fetch(
            run_id="test-run",
            anchor_id=1,
            project=self.project,
            deadline=self.wall_us / 1_000_000 + 5.0,
        )
        self.assertTrue(report.was_fallback)
        self.assertEqual(report.source_provider, "kraken")
        self.assertIn("FALLBACK_COINBASE_STALE_OBSERVATION", report.diagnostics)

    def test_coinbase_future_source_time_fallback(self):
        """Coinbase with future source_timestamp → fallback to Kraken."""
        future_ts = self.wall_us + 5_000_000
        coinbase_obs = self._make_obs("coinbase", "50000.00")
        coinbase_obs = ApproximateObservation(
            provider=coinbase_obs.provider,
            pair=coinbase_obs.pair,
            quote_currency=coinbase_obs.quote_currency,
            price=coinbase_obs.price,
            bid_price=coinbase_obs.bid_price,
            ask_price=coinbase_obs.ask_price,
            source_timestamp_us=future_ts,
            receipt_wall_us=coinbase_obs.receipt_wall_us,
            receipt_server_us=coinbase_obs.receipt_server_us,
            raw_payload_hash=coinbase_obs.raw_payload_hash,
            raw_payload_size=coinbase_obs.raw_payload_size,
            transport_type=coinbase_obs.transport_type,
            parser_revision=coinbase_obs.parser_revision,
            http_status=200,
            result="error",
            error_code="FUTURE_SOURCE_TIME",
        )
        kraken_obs = self._make_obs("kraken", "50100.00")
        providers = {"coinbase": MockProvider("coinbase", coinbase_obs), "kraken": MockProvider("kraken", kraken_obs)}
        session = ApproximateReferenceSession(
            providers=providers,
            monotonic_ns=lambda: self.monotonic_ns,
            wall_us=lambda: self.wall_us,
            sleep=lambda s: None,
            project=self.project,
        )
        session.start()
        report, observations = session.fetch(
            run_id="test-run",
            anchor_id=1,
            project=self.project,
            deadline=self.wall_us / 1_000_000 + 5.0,
        )
        self.assertTrue(report.was_fallback)
        self.assertEqual(report.source_provider, "kraken")
        self.assertEqual(report.value, Decimal("50100.00"))
        self.assertIn("FALLBACK_COINBASE_FUTURE_SOURCE_TIME", report.diagnostics)

    def test_missing_kraken_provider(self):
        """Missing Kraken provider in dict → handled gracefully."""
        coinbase_obs = self._make_obs("coinbase", "50000.00", error_code="HTTP_ERROR")
        providers = {"coinbase": MockProvider("coinbase", coinbase_obs)}
        session = ApproximateReferenceSession(
            providers=providers,
            monotonic_ns=lambda: self.monotonic_ns,
            wall_us=lambda: self.wall_us,
            sleep=lambda s: None,
            project=self.project,
        )
        session.start()
        report, observations = session.fetch(
            run_id="test-run",
            anchor_id=1,
            project=self.project,
            deadline=self.wall_us / 1_000_000 + 5.0,
        )
        self.assertTrue(report.was_fallback)
        self.assertIsNone(report.value)
        self.assertIn("FALLBACK_COINBASE_HTTP_ERROR", report.diagnostics)
        self.assertIn("FALLBACK_KRAKEN_MISSING_PROVIDER", report.diagnostics)

    def test_missing_coinbase_provider(self):
        """Missing Coinbase provider → fallback to Kraken."""
        kraken_obs = self._make_obs("kraken", "50100.00")
        providers = {"kraken": MockProvider("kraken", kraken_obs)}
        session = ApproximateReferenceSession(
            providers=providers,
            monotonic_ns=lambda: self.monotonic_ns,
            wall_us=lambda: self.wall_us,
            sleep=lambda s: None,
            project=self.project,
        )
        session.start()
        report, observations = session.fetch(
            run_id="test-run",
            anchor_id=1,
            project=self.project,
            deadline=self.wall_us / 1_000_000 + 5.0,
        )
        self.assertTrue(report.was_fallback)
        self.assertEqual(report.source_provider, "kraken")
        self.assertEqual(report.value, Decimal("50100.00"))
        self.assertIn("FALLBACK_COINBASE_MISSING_PROVIDER", report.diagnostics)


if __name__ == "__main__":
    unittest.main()