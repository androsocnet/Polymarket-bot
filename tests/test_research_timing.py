"""Prospective receipt-time research gates; never execution approval."""
import unittest

from polymarket_bot.recorder.research_timing import assess_receipt_timing


class ResearchTimingTests(unittest.TestCase):
    def assess(self, **changes):
        values = dict(run_id='run', receipt_run_id='run', source_us=100_000_000,
            request_started_mono_ns=9_900_000_000, receipt_mono_ns=10_000_000_000,
            cutoff_mono_ns=11_000_000_000, receipt_bounds_us=(99_000_000, 101_000_000),
            cutoff_bounds_us=(100_000_000, 102_000_000), window_start_us=90_000_000,
            window_end_us=900_000_000, raw_present=True, raw_complete=True,
            clock_valid=True, anchor_age_ns=20_000_000_000)
        values.update(changes)
        return assess_receipt_timing(**values)

    def test_overlap_proves_availability_not_source_order_or_execution(self):
        result = self.assess()
        self.assertTrue(result['eligible'])
        self.assertEqual(result['source_order'], 'SOURCE_ORDER_UNCERTAIN')
        self.assertEqual(result['source_age_bounds_us'], (0, 2_000_000))
        self.assertFalse(result['execution_certified'])
        self.assertEqual(self.assess(source_us=99_000_000)['source_order'], 'SOURCE_NOT_AFTER_RECEIPT')

    def test_future_information_different_run_and_invalid_integers(self):
        for changes, reason in [({'receipt_mono_ns': 12_000_000_000}, 'RECEIPT_AFTER_CUTOFF'),
                ({'source_us': 101_000_001}, 'DEFINITE_FUTURE_SOURCE'),
                ({'receipt_run_id': 'other'}, 'RUN_MISMATCH'),
                ({'source_us': None}, 'TIMING_INVALID'),
                ({'receipt_mono_ns': True}, 'TIMING_INVALID'),
                ({'cutoff_mono_ns': 2**63}, 'TIMING_INVALID'),
                ({'request_started_mono_ns': 10_000_000_001}, 'TIMING_INVALID')]:
            with self.subTest(changes=changes):
                result = self.assess(**changes)
                self.assertFalse(result['eligible'])
                self.assertIn(reason, result['reasons'])

    def test_stale_missing_and_cross_clock_evidence(self):
        for changes, reason in [({'cutoff_mono_ns': 15_000_000_001}, 'STALE_RECEIPT'),
                ({'request_started_mono_ns': 4_999_999_999}, 'STALE_RESPONSE'),
                ({'source_us': 96_999_999}, 'STALE_SOURCE'),
                ({'raw_present': False}, 'RAW_EVIDENCE_UNAVAILABLE'),
                ({'raw_complete': False}, 'RAW_EVIDENCE_UNAVAILABLE'),
                ({'raw_complete': 1}, 'RAW_EVIDENCE_UNAVAILABLE'),
                ({'clock_valid': False}, 'CLOCK_UNAVAILABLE'),
                ({'anchor_age_ns': 120_000_000_001}, 'CLOCK_UNAVAILABLE'),
                ({'cutoff_bounds_us': (110_000_000, 112_000_000)}, 'CLOCK_RELATIONSHIP_INVALID'),
                ({'receipt_bounds_us': (101_000_000, 99_000_000)}, 'TIMING_INVALID'),
                ({'receipt_bounds_us': (97_000_000, 102_000_001)}, 'CLOCK_UNCERTAINTY_EXCESSIVE')]:
            with self.subTest(changes=changes):
                result = self.assess(**changes)
                self.assertFalse(result['eligible'])
                self.assertIn(reason, result['reasons'])

    def test_market_boundaries_source_and_entire_brackets(self):
        for changes in ({'window_start_us': 99_000_001}, {'window_end_us': 102_000_000},
                        {'source_us': 89_999_999}, {'window_end_us': 100_000_000}):
            with self.subTest(changes=changes):
                self.assertIn('MARKET_BOUNDARY', self.assess(**changes)['reasons'])
        self.assertTrue(self.assess(window_start_us=99_000_000, window_end_us=102_000_001)['eligible'])

    def test_exact_five_second_age_limits(self):
        self.assertTrue(self.assess(request_started_mono_ns=5_000_000_000,
            cutoff_mono_ns=15_000_000_000, cutoff_bounds_us=(104_000_000, 106_000_000),
            source_us=101_000_000)['eligible'])


if __name__ == '__main__':
    unittest.main()
