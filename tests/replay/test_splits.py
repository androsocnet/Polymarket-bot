"""TDD tests for chronological splits with purging and settlement leakage prevention."""
import unittest
from decimal import Decimal
from polymarket_bot.replay.report import split_windows, ChronologicalSplit


class SplitTests(unittest.TestCase):
    """Test train/validation/test chronological splitting, purging, and leakage prevention."""

    def test_split_dataclass_exists(self):
        """ChronologicalSplit class should exist."""
        self.assertTrue(callable(ChronologicalSplit))

    def test_chronological_splits_partition_properly(self):
        """Windows strictly before boundary go to train, strictly after to test."""
        windows = [
            {'market_id': 'm1', 'start_us': 100, 'end_us': 200, 'resolved_us': 250},
            {'market_id': 'm2', 'start_us': 300, 'end_us': 400, 'resolved_us': 450},
            {'market_id': 'm3', 'start_us': 500, 'end_us': 600, 'resolved_us': 650},
            {'market_id': 'm4', 'start_us': 700, 'end_us': 800, 'resolved_us': 850},
        ]
        # Split at 450 (between m2 and m3)
        split = split_windows(windows, split_us=450)
        self.assertEqual([w['market_id'] for w in split.train], ['m1', 'm2'])
        self.assertEqual([w['market_id'] for w in split.test], ['m3', 'm4'])
        self.assertEqual(len(split.purged), 0)

    def test_overlapping_windows_purged(self):
        """Windows spanning the split boundary are purged from both splits."""
        windows = [
            {'market_id': 'm1', 'start_us': 100, 'end_us': 200, 'resolved_us': 250},
            {'market_id': 'm_overlap', 'start_us': 300, 'end_us': 500, 'resolved_us': 550},  # spans 400
            {'market_id': 'm3', 'start_us': 600, 'end_us': 700, 'resolved_us': 750},
        ]
        split = split_windows(windows, split_us=400)
        self.assertEqual([w['market_id'] for w in split.train], ['m1'])
        self.assertEqual([w['market_id'] for w in split.test], ['m3'])
        self.assertEqual([w['market_id'] for w in split.purged], ['m_overlap'])

    def test_settlement_delay_leakage_prevented(self):
        """Market ending before boundary but resolving AFTER boundary is purged from train."""
        windows = [
            # Ends at 350, but resolves at 450 (after boundary 400) -> leakage risk -> purge
            {'market_id': 'm_late_settle', 'start_us': 200, 'end_us': 350, 'resolved_us': 450},
            {'market_id': 'm_clean', 'start_us': 500, 'end_us': 600, 'resolved_us': 650},
        ]
        split = split_windows(windows, split_us=400)
        self.assertEqual(len(split.train), 0)
        self.assertEqual([w['market_id'] for w in split.purged], ['m_late_settle'])
        self.assertEqual([w['market_id'] for w in split.test], ['m_clean'])

    def test_three_way_split_train_val_test(self):
        """Train/val/test splits supported with purging at both boundaries."""
        windows = [
            {'market_id': 'm1', 'start_us': 100, 'end_us': 200, 'resolved_us': 250},
            {'market_id': 'm2', 'start_us': 300, 'end_us': 400, 'resolved_us': 450},
            {'market_id': 'm3', 'start_us': 500, 'end_us': 600, 'resolved_us': 650},
            {'market_id': 'm4', 'start_us': 700, 'end_us': 800, 'resolved_us': 850},
        ]
        split = split_windows(windows, train_end_us=250, val_end_us=550)
        self.assertEqual([w['market_id'] for w in split.train], ['m1'])
        self.assertEqual([w['market_id'] for w in split.val], ['m2'])
        self.assertEqual([w['market_id'] for w in split.test], ['m4'])


if __name__ == '__main__':
    unittest.main()