"""TDD tests for strategy variants grid and parameter evaluation."""
import unittest
from polymarket_bot.replay.variants import (
    Variant, get_variant_grid, get_variant, evaluate_variants
)

class VariantTests(unittest.TestCase):
    """Test variant definitions and evaluation on train-only split."""

    def test_variant_dataclass_exists(self):
        """Variant class should exist."""
        self.assertTrue(callable(Variant))

    def test_default_grid_contains_all_prespecified_variants(self):
        """Grid must contain baseline and all specified hypothesis variants."""
        grid = get_variant_grid()
        self.assertIn('baseline', grid)
        self.assertIn('entry_price_cap', grid)
        self.assertIn('min_remaining', grid)
        self.assertIn('hold_short', grid)
        self.assertIn('hold_long', grid)
        self.assertIn('reversal_confirm', grid)
        self.assertIn('fee_sensitivity', grid)
        self.assertIn('liquidity_sensitivity', grid)

    def test_baseline_matches_settings_defaults(self):
        """Baseline variant has default settings."""
        baseline = get_variant('baseline')
        self.assertEqual(baseline.name, 'baseline')
        self.assertEqual(baseline.parameters.get('holding_period'), 3)
        self.assertEqual(baseline.parameters.get('lookback'), 3)
        self.assertEqual(baseline.parameters.get('fee_bps'), '10')

    def test_entry_price_cap_variants(self):
        """Entry price cap has values in {0.5, 0.6, 0.7, 0.75, 0.8}."""
        caps = get_variant_grid()['entry_price_cap']
        prices = [v.parameters.get('max_entry_price') for v in caps]
        for expected in ('0.5', '0.6', '0.7', '0.75', '0.8'):
            self.assertIn(expected, prices)

    def test_holding_period_variants(self):
        """Holding period variants include short {1, 2} and long {5, 10, 20}."""
        short = [v.parameters.get('holding_period') for v in get_variant_grid()['hold_short']]
        self.assertEqual(short, [1, 2])
        long_holds = [v.parameters.get('holding_period') for v in get_variant_grid()['hold_long']]
        self.assertEqual(long_holds, [5, 10, 20])

    def test_fee_sensitivity_variants(self):
        """Fee sensitivity covers 5, 10, 20, 50 bps."""
        fees = [v.parameters.get('fee_bps') for v in get_variant_grid()['fee_sensitivity']]
        self.assertEqual(fees, ['5', '10', '20', '50'])


class VariantsSplitEnforcementTests(unittest.TestCase):
    """I3 regression: train_only must be enforced against the chronological split.

    Membership is exercised over a real validated recording; the window's
    market_id label is chosen per test because split membership is what's under test.
    """

    def setUp(self):
        from tests.test_paper import recorded_fixture, OfflineTests
        self._offline = OfflineTests()
        self._offline.setUp()
        self.addCleanup(self._offline.doCleanups)
        self.f = recorded_fixture(self._offline)
        self.addCleanup(self.f.close)
        self.run_id = self.f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        from polymarket_bot.replay.adapter import load_replay_recording
        (self._snapshots, self._excluded, self._opportunities, _,
         _, self._paper) = load_replay_recording(self.f.db.path, self.run_id, '1001')
        self._row = self.f.rows(
            "SELECT window_start_us, window_end_us, up_token_id, down_token_id "
            "FROM markets WHERE market_id='1001'")[0]

    def _window(self, market_id):
        from polymarket_bot.replay.adapter import MarketWindow
        return MarketWindow(
            market_id=market_id,
            condition_id=self._paper.condition_id,
            event_id='2001',
            window_start_us=self._row['window_start_us'],
            window_end_us=self._row['window_end_us'],
            up_token_id=self._row['up_token_id'],
            down_token_id=self._row['down_token_id'],
            snapshots=self._snapshots,
            excluded=self._excluded,
            opportunities=self._opportunities,
            settlement_evidence=None,
            settlement_result=None,
            horizon_ns=self._paper.horizon_ns,
            horizon_us=self._paper.horizon_us,
            paper_recording=self._paper)

    def _split(self, *market_ids):
        from polymarket_bot.replay.report import split_windows
        records = [dict(market_id=mid, start_us=100 + i * 200, end_us=200 + i * 200,
                        resolved_us=250 + i * 200) for i, mid in enumerate(market_ids)]
        return split_windows(records, split_us=250)

    def test_train_only_requires_split(self):
        """Calling with train_only=True and no split must fail closed."""
        with self.assertRaises(ValueError):
            evaluate_variants([self._window('1001')],
                              [get_variant('baseline')], train_only=True)

    def test_train_only_rejects_out_of_split_windows(self):
        """Windows outside (or missing from) the train split must be refused."""
        split = self._split('m1')
        with self.assertRaises(ValueError):
            evaluate_variants(
                [self._window('1001')], [get_variant('baseline')],
                train_only=True, split=split)

    def test_train_only_accepts_in_split_windows(self):
        """Train windows within the split evaluate normally over real evidence."""
        split = self._split('1001')
        results = evaluate_variants(
            [self._window('1001')], [get_variant('baseline')],
            train_only=True, split=split)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['train_only'])
        self.assertEqual(results[0]['windows_evaluated'], 1)
        self.assertEqual(len(results[0]['reports']), 1)

    def test_holdout_evaluation_is_explicit(self):
        """Disabling train_only is an explicit escape hatch, still isolated from split."""
        split = self._split('m1')
        results = evaluate_variants(
            [self._window('1001')], [get_variant('baseline')],
            train_only=False, split=split)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['train_only'])
        self.assertEqual(len(results[0]['reports']), 1)


if __name__ == '__main__':
    unittest.main()