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


if __name__ == '__main__':
    unittest.main()