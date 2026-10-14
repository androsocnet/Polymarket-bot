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


class ResearchAssessmentTests(unittest.TestCase):
    def setUp(self):
        from polymarket_bot.recorder import free_reference as policy
        from polymarket_bot.recorder.models import ClockAnchor
        self.policy = policy
        self.assertTrue(hasattr(policy, 'assess_provider'), 'pure research assessment missing')
        self.origin = 10_000_000_000
        self.end = 70_000_000_000
        self.wall = 1_070_000_000
        anchor = ClockAnchor(1, self.origin, self.origin, 0, 0, self.origin,
            1_010_000_000, 1_000_000, 'VALID')
        self.context = policy.AcquisitionContext(2, '22f60ec8-3c93-4d67-9a5f-0cb2b0841625', anchor, 1, self.origin, None)

    def observation(self, ns, provider='coinbase', **changes):
        from dataclasses import replace
        wall = 1_000_000_000 + ns // 1000
        obs = ApproximateObservation(provider, 'BTC/USD', 'USD', Decimal('10'),
            Decimal('9'), Decimal('11'), wall if provider == 'coinbase' else None,
            wall, wall, 'abc', 100, 'rest', provider + '-ticker-v1', 200,
            'success', None, ns)
        return replace(obs, **changes)

    def assess(self, observations, provider='coinbase', end=None, evidence_changes=None, **kwargs):
        from dataclasses import replace
        end = self.end if end is None else end
        ids = tuple(range(1, len(observations) + 1))
        opportunities = []
        requests = []
        assigned = {}
        for observation_id, obs in zip(ids, observations):
            ns = obs.receipt_mono_ns
            opportunity_id = assigned.get(ns)
            if opportunity_id is None or any(row[0] == opportunity_id and row[1] == obs.provider for row in requests):
                opportunity_id = len(opportunities) + 1
                opportunities.append((opportunity_id, ns, 'completed'))
                assigned[ns] = opportunity_id
            requests.append((opportunity_id, obs.provider, observation_id, ns,
                'success' if obs.result == 'success' and obs.error_code is None else 'error'))
        evidence = self.policy.ResearchWindowEvidence(2, self.context.run_id, self.origin, ids,
            tuple(self.context for _ in ids), tuple(opportunities), tuple(requests), (),
            max(end - 60_000_000_000, self.origin), end,
            tuple((row[0], row[1]) for row in requests))
        if evidence_changes:
            evidence = replace(evidence, **evidence_changes)
        return self.policy.assess_provider(observations, provider=provider,
            report_wall_us=1_000_000_000 + end // 1000, report_ns=end,
            evidence=evidence, **kwargs)

    def full(self, provider='coinbase'):
        return [self.observation(self.origin + i * 2_000_000_000, provider) for i in range(30)]

    def test_one_sample_numeric_valid_not_sufficient(self):
        result = self.assess([self.observation(self.end)])
        self.assertEqual((result.value, result.value_status, result.coverage_quality),
            (Decimal('10'), 'VALID', 'INSUFFICIENT'))
        self.assertTrue(result.strict_gap)
        self.assertEqual(result.contributing_ids, (1,))
        self.assertEqual(result.warmup_status, 'FULL')

    def test_receipt_cells_and_pairs(self):
        for indices, quality, count in ((range(30), 'COMPLETE', 30),
                (range(0, 30, 2), 'DEGRADED', 15), (range(2, 30, 2), 'INSUFFICIENT', 14)):
            with self.subTest(quality=quality):
                obs = [self.observation(self.origin + i * 2_000_000_000) for i in indices]
                result = self.assess(obs)
                self.assertEqual(result.coverage_quality, quality)
                self.assertEqual(len(result.occupied_cells), count)
        burst = self.assess([self.observation(self.end)] * 40)
        self.assertEqual(burst.occupied_cells, (29,))
        self.assertEqual(burst.coverage_quality, 'INSUFFICIENT')
        for i in range(1, 30):
            for jitter, cell in ((-1, i - 1), (0, i), (1, i)):
                with self.subTest(boundary=i, jitter=jitter):
                    result = self.assess([self.observation(self.origin + i * 2_000_000_000 + jitter)])
                    self.assertEqual(result.occupied_cells, (cell,))
        for ns, cells in ((self.origin, (0,)), (self.end, (29,)),
                (self.origin - 1, ()), (self.end + 1, ())):
            with self.subTest(endpoint=ns):
                self.assertEqual(self.assess([self.observation(ns)]).occupied_cells, cells)

    def test_warmup_clips_only_oldest_partial_pair(self):
        for age, status, partial, full in ((59_999_999_999, 'WARMING_UP', 0, 14),
                (60_000_000_000, 'FULL', None, 15), (3_999_999_999, 'WARMING_UP', 14, 0),
                (4_000_000_000, 'WARMING_UP', None, 1)):
            with self.subTest(age=age):
                end = self.origin + age
                result = self.assess([self.observation(end)], end=end)
                self.assertEqual(result.warmup_status, status)
                self.assertEqual(result.partial_pair, partial)
                self.assertEqual(len(result.exposed_pairs), full)
                self.assertEqual(result.failed_requests, 0)
                self.assertEqual(result.provisional, status == 'WARMING_UP')
        self.assertEqual(self.assess([self.observation(self.origin + 1)],
            end=self.origin + 1).coverage_quality, 'INSUFFICIENT')

    def test_invalid_identity_and_numeric_inputs(self):
        for changes in ({'pair': 'ETH/USD'}, {'quote_currency': 'EUR'},
                {'parser_revision': 'unknown'}, {'transport_type': 'websocket'},
                {'price': Decimal('NaN')}, {'price': Decimal('0')}, {'price': None},
                {'result': 'error', 'error_code': 'HTTP_ERROR'}, {'http_status': 500}):
            with self.subTest(changes=changes):
                from dataclasses import replace
                result = self.assess([replace(self.observation(self.end), **changes)])
                self.assertEqual(result.value_status, 'INVALID')
                self.assertEqual(result.coverage_quality, 'INSUFFICIENT')
        self.assertEqual(self.assess([]).value_status, 'INVALID')
        mixed = self.full() + [self.observation(self.end, 'kraken', price=Decimal('999'))]
        self.assertEqual(self.assess(mixed).value, Decimal('10'))
        self.assertEqual(self.assess(mixed).coverage_quality, 'COMPLETE')

    def test_provider_scoping_fallback_and_request_diagnostics(self):
        error = self.observation(self.end, 'kraken', result='error', error_code='HTTP_ERROR', price=None)
        self.assertEqual(self.assess(self.full() + [error]).coverage_quality, 'COMPLETE')
        primary_error = self.observation(self.end, result='error', error_code='HTTP_ERROR', price=None)
        result = self.assess(self.full('kraken') + [primary_error], provider='kraken', was_fallback=True)
        self.assertEqual(result.coverage_quality, 'DEGRADED')
        result = self.assess(self.full() + [primary_error])
        self.assertEqual((result.coverage_quality, result.failed_requests), ('DEGRADED', 1))
        opportunities = tuple((i + 1, self.origin + i * 2_000_000_000, 'completed') for i in range(30))
        result = self.assess(self.full(), evidence_changes={
            'scheduled_opportunities': opportunities + ((31, self.end, 'busy'),)})
        self.assertEqual((result.coverage_quality, result.missing_requests), ('DEGRADED', 1))

    def test_provenance_and_membership_fail_closed(self):
        from dataclasses import replace
        for changes in ({'scheduled_opportunities': ()}, {'observation_ids': ()}, {'contexts': ()},
                {'observation_ids': (1,) * 30}, {'run_id': 'foreign-run'},
                {'run_id': ''}, {'run_id': ' '}, {'run_id': 2},
                {'contexts': (replace(self.context, anchor_id=0),) * 30},
                {'contexts': (replace(self.context, sampling_origin_ns=0),) * 30}):
            with self.subTest(changes=changes):
                self.assertEqual(self.assess(self.full(), evidence_changes=changes).coverage_quality,
                    'INSUFFICIENT')
        obs = self.full() + [self.observation(self.origin - 1, receipt_wall_us=self.wall)]
        self.assertIn('WINDOW_MEMBERSHIP_DISAGREEMENT', self.assess(obs).reasons)
        obs[-1] = replace(obs[-1], result='error', error_code='HTTP_ERROR', price=None)
        self.assertNotIn('WINDOW_MEMBERSHIP_DISAGREEMENT', self.assess(obs).reasons)
        for age, quality in ((59_999_999_999, 'INSUFFICIENT'), (60_000_000_000, 'COMPLETE')):
            self.assertEqual(self.assess(self.full(), evidence_changes={
                'discontinuity_ends_ns': (self.end - age,)}).coverage_quality, quality)

    def test_request_membership_rejects_incomplete_or_orphan_evidence(self):
        for changes in ({'scheduled_opportunities': ((1, self.end, 'completed'),)},
                {'request_outcomes': ((999, 'coinbase', 'error'),)},
                {'request_outcomes': ((1, 'coinbase', 'error'), (1, 'coinbase', 'success'))},
                {'request_outcomes': ((1, 'coinbase', 1, self.origin, 'success'),) * 2},
                {'request_outcomes': ((1, 'coinbase', 1, self.origin, 'error'),)},
                {'request_outcomes': ()}, {'expected_requests': ((999, 'coinbase'),)},
                {'validated_window_start_ns': None},
                {'validated_window_end_ns': self.end - 1},
                {'scheduled_opportunities': ((1, self.origin, 'busy'),)}):
            with self.subTest(changes=changes):
                self.assertIn('INCOMPLETE_PROVENANCE', self.assess(self.full(),
                    evidence_changes=changes).reasons)

    def test_final_request_counts_once_and_uses_actual_receipt_window(self):
        error = self.observation(self.end, result='error', error_code='HTTP_ERROR', price=None)
        result = self.assess(self.full() + [error])
        self.assertEqual(result.failed_requests, 1)
        for ns, count in ((self.origin - 1, 0), (self.origin, 1),
                (self.end, 1), (self.end + 1, 0)):
            with self.subTest(ns=ns):
                error = self.observation(ns, result='error', error_code='HTTP_ERROR', price=None)
                result = self.assess(self.full() + [error])
                self.assertEqual(result.failed_requests, count)
        result = self.assess(self.full() + [self.observation(self.end,
            result='error', error_code='HTTP_ERROR', price=None)] * 2)
        self.assertEqual(result.failed_requests, 2)
        self.assertEqual(self.assess(self.full()).failed_requests, 0)

    def test_same_fetch_disagreement_is_explicit_not_consensus(self):
        self.assertTrue(hasattr(self.policy.ResearchAssessment, '__dataclass_fields__'))
        self.assertIn('same_fetch_disagreements', self.policy.ResearchAssessment.__dataclass_fields__)
        observations = self.full() + [self.observation(self.origin, 'kraken', price=Decimal('1000'))]
        result = self.assess(observations)
        pair = result.same_fetch_disagreements[0]
        self.assertEqual(pair, (1, 1, 31, Decimal('10'), Decimal('1000'), Decimal('990'), 0))
        self.assertEqual(result.coverage_quality, 'COMPLETE')
        self.assertEqual(result.value, Decimal('10'))
        self.assertEqual(result.provider_availability, (('coinbase', 30), ('kraken', 1)))
        self.assertEqual(result.scheduled_receipt_jitter_ns[0], (1, 0))
        self.assertEqual(result.cell_phase_offsets_ns[0], 0)
        interleaved = [self.observation(self.origin), self.observation(self.origin + 2_000_000_000),
            self.observation(self.origin + 2_000_000_000, 'kraken', price=Decimal('20')),
            self.observation(self.origin, 'kraken', price=Decimal('30'))]
        pairs = self.assess(interleaved).same_fetch_disagreements
        self.assertEqual([(row[0], row[1], row[2], row[5]) for row in pairs],
            [(1, 1, 4, Decimal('20')), (2, 2, 3, Decimal('10'))])
        error = self.observation(self.origin, 'kraken', result='error', error_code='HTTP_ERROR', price=None)
        self.assertEqual(self.assess(self.full() + [error]).same_fetch_disagreements[0][5], None)
        for ns in (self.origin - 1, self.end + 1):
            result = self.assess(self.full() + [self.observation(ns, 'kraken')])
            self.assertFalse(any(row[5] is not None for row in result.same_fetch_disagreements))
            self.assertEqual(dict(result.provider_availability)['kraken'], 0)

    def test_uncertainty_policy_and_decimal_precision_preserved(self):
        from decimal import localcontext, Inexact
        obs = self.observation(self.end, source_timestamp_us=self.wall + 1_000_000)
        self.assertEqual(self.assess([obs]).value_status, 'VALID')
        obs = self.observation(self.end, source_timestamp_us=self.wall + 1_000_001)
        self.assertEqual(self.assess([obs]).value_status, 'INVALID')
        observations = [self.observation(self.end, price=Decimal(x)) for x in ('1', '1', '2')]
        with localcontext() as context:
            context.prec = 3
            context.traps[Inexact] = True
            result = self.assess(observations)
        assert result.value is not None
        self.assertEqual(len(result.value.as_tuple().digits), 512)


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
            receipt_server_us=None,
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
            receipt_server_us=None,
            raw_payload_hash="def",
            raw_payload_size=100,
            transport_type="rest",
            parser_revision="kraken-ticker-v1",
            http_status=200,
            result="error" if error_code else "success",
            error_code=error_code,
        )

    def test_mean_independent_of_ambient_decimal_context(self):
        from decimal import localcontext, Inexact, ROUND_DOWN, ROUND_UP
        observations = [self._make_coinbase_obs(value) for value in ('1', '1', '2')]
        results = []
        for precision, rounding in ((3, ROUND_DOWN), (19, ROUND_UP)):
            with localcontext() as context:
                context.prec = precision
                context.rounding = rounding
                context.traps[Inexact] = True
                results.append(compute_approximate_sample_mean_60s(observations,
                    receipt_server_us=None, receipt_wall_us=self.receipt_wall_us).value)
        self.assertEqual(results[0], results[1])
        assert results[0] is not None
        self.assertEqual(len(results[0].as_tuple().digits), 512)

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
            receipt_server_us=None,
            raw_payload_hash="abc",
            raw_payload_size=100,
            transport_type="rest",
            parser_revision=f"{provider}-ticker-v1",
            http_status=200 if not error_code else 500,
            result="error" if error_code else "success",
            error_code=error_code,
        )

    def test_rolling_history_mean_and_bounded_eviction(self):
        providers = {name: MockProvider(name, self._make_obs(name, '10'))
                     for name in ('coinbase', 'kraken')}
        session = ApproximateReferenceSession(providers=providers,
            monotonic_ns=lambda: self.monotonic_ns, wall_us=lambda: self.wall_us,
            sleep=lambda seconds: None)
        session.fetch(run_id='run', anchor_id=None, project=None,
                      deadline=self.wall_us / 1000000 + 5)
        self.wall_us += 2000000
        self.monotonic_ns += 2000000000
        providers['coinbase'] = MockProvider('coinbase', self._make_obs('coinbase', '20'))
        session._providers = providers
        report, observations = session.fetch(run_id='run', anchor_id=None, project=None,
                                             deadline=self.wall_us / 1000000 + 5)
        self.assertEqual(report.value, Decimal('15'))
        self.assertEqual(report.observation_count, 2)
        self.assertIn('OBSERVATION_GAP', report.diagnostics)
        self.assertEqual(len(observations), 2)
        self.wall_us += 61000000
        self.monotonic_ns += 61000000000
        providers['coinbase'] = MockProvider('coinbase', self._make_obs('coinbase', '30'))
        report, _ = session.fetch(run_id='run', anchor_id=None, project=None,
                                  deadline=self.wall_us / 1000000 + 5)
        self.assertEqual(report.value, Decimal('30'))
        self.assertIn('EXCLUDED_STALE_OBSERVATIONS', report.diagnostics)
        self.assertLessEqual(len(session._history['coinbase']), 64)

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
        self.assertEqual(tuple(obs.provider for obs in observations), ("coinbase", "kraken"))
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
        self.assertEqual(tuple(obs.provider for obs in observations), ("coinbase", "kraken"))
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
        self.assertEqual(tuple(obs.provider for obs in observations), ("coinbase", "kraken"))
        self.assertEqual(observations[1].provider, "kraken")
        self.assertEqual(observations[0].error_code, "HTTP_ERROR")

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