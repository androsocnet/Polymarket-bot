"""MVP config: PAPER_ONLY is structural, every knob is validated."""
import unittest
from decimal import Decimal
from pathlib import Path

from polymarket_bot.linux_mvp.config import MODE, ConfigError, Settings


class SettingsTests(unittest.TestCase):
    def test_defaults_are_conservative_and_paper_only(self):
        s = Settings.from_env({}, base=Path('/tmp/mvp'))
        self.assertEqual(s.mode, MODE)
        self.assertEqual(s.initial_cash, Decimal('100'))
        self.assertEqual(s.max_order, Decimal('10'))
        self.assertEqual(s.max_exposure, Decimal('10'))
        self.assertEqual(s.fee_bps, Decimal('10'))
        self.assertEqual(s.threshold, Decimal('0.02'))
        self.assertEqual(s.lookback, 3)
        self.assertEqual(s.holding_period, 3)
        self.assertEqual(s.max_trades_per_window, 6)
        self.assertEqual(s.max_daily_loss, Decimal('10'))
        self.assertEqual(s.sample_seconds, 2)
        self.assertEqual(s.poll_seconds, 2)
        self.assertEqual(s.backoff_base_seconds, 2.0)
        self.assertEqual(s.backoff_max_seconds, 30.0)
        self.assertEqual(s.max_source_age_seconds, 5.0)
        self.assertEqual(s.max_consecutive_errors, 5)
        self.assertEqual(s.db_path, Path('/tmp/mvp') / 'paper.sqlite')
        self.assertEqual(s.state_dir, Path('/tmp/mvp') / 'state')
        self.assertEqual(s.bot_id, 'default')

    def test_env_overrides_are_typed(self):
        s = Settings.from_env({
            'MVP_DB_PATH': '/var/lib/mvp/paper.sqlite',
            'MVP_STATE_DIR': '/var/lib/mvp/state',
            'MVP_BOT_ID': 'vps-1',
            'MVP_MAX_ORDER': '4.5',
            'MVP_MAX_EXPOSURE': '20',
            'MVP_MAX_DAILY_LOSS': '3',
            'MVP_MAX_TRADES_PER_WINDOW': '2',
            'MVP_LOOKBACK': '4',
            'MVP_HOLDING_PERIOD': '1',
            'MVP_THRESHOLD': '0.05',
            'MVP_FEE_BPS': '0',
            'MVP_MAX_SOURCE_AGE_SECONDS': '2.5',
            'MVP_MAX_CONSECUTIVE_ERRORS': '3',
            'MVP_POLL_SECONDS': '5',
            'MVP_BACKOFF_BASE_SECONDS': '1.5',
            'MVP_BACKOFF_MAX_SECONDS': '12',
        }, base=Path('/ignored'))
        self.assertEqual(s.db_path, Path('/var/lib/mvp/paper.sqlite'))
        self.assertEqual(s.state_dir, Path('/var/lib/mvp/state'))
        self.assertEqual(s.bot_id, 'vps-1')
        self.assertEqual(s.max_order, Decimal('4.5'))
        self.assertEqual(s.max_exposure, Decimal('20'))
        self.assertEqual(s.max_daily_loss, Decimal('3'))
        self.assertEqual(s.max_trades_per_window, 2)
        self.assertEqual(s.lookback, 4)
        self.assertEqual(s.holding_period, 1)
        self.assertEqual(s.threshold, Decimal('0.05'))
        self.assertEqual(s.fee_bps, Decimal('0'))
        self.assertEqual(s.max_source_age_seconds, 2.5)
        self.assertEqual(s.max_consecutive_errors, 3)
        self.assertEqual(s.poll_seconds, 5)
        self.assertEqual(s.backoff_base_seconds, 1.5)
        self.assertEqual(s.backoff_max_seconds, 12.0)

    def test_live_and_unknown_modes_are_refused(self):
        for value in ('LIVE', 'live', 'PAPER', 'SHADOW', ''):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Settings.from_env({'MODE': value}, base=Path('/tmp/mvp'))

    def test_paper_mode_is_accepted_verbatim(self):
        self.assertEqual(Settings.from_env({'MODE': 'PAPER_ONLY'}, base=Path('/tmp/mvp')).mode, MODE)

    def test_invalid_numbers_and_limits_are_refused(self):
        bad = {
            'MVP_MAX_ORDER': '0',
            'MVP_MAX_EXPOSURE': '-1',
            'MVP_INITIAL_CASH': '0',
            'MVP_FEE_BPS': '10001',
            'MVP_MAX_DAILY_LOSS': '0',
            'MVP_THRESHOLD': '0',
            'MVP_LOOKBACK': '2',
            'MVP_HOLDING_PERIOD': '0',
            'MVP_MAX_TRADES_PER_WINDOW': '451',
            'MVP_SAMPLE_SECONDS': '3',
            'MVP_MAX_SOURCE_AGE_SECONDS': '0',
            'MVP_MAX_CONSECUTIVE_ERRORS': '0',
            'MVP_POLL_SECONDS': '1',
            'MVP_BACKOFF_BASE_SECONDS': '0',
            'MVP_BACKOFF_MAX_SECONDS': '0',
        }
        for key, value in bad.items():
            with self.subTest(key=key), self.assertRaises(ConfigError):
                Settings.from_env({key: value}, base=Path('/tmp/mvp'))

    def test_junk_numeric_text_is_refused(self):
        for value in ('nan', 'inf', ' 1', '1 ', '0x10', '1_0', '', '1,5'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Settings.from_env({'MVP_MAX_ORDER': value}, base=Path('/tmp/mvp'))

    def test_exposure_below_order_is_refused(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({'MVP_MAX_ORDER': '10', 'MVP_MAX_EXPOSURE': '5'}, base=Path('/tmp/mvp'))

    def test_backoff_base_above_max_is_refused(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({'MVP_BACKOFF_BASE_SECONDS': '10', 'MVP_BACKOFF_MAX_SECONDS': '5'},
                              base=Path('/tmp/mvp'))

    def test_poll_interval_accepts_60_and_refuses_61(self):
        self.assertEqual(Settings.from_env({'MVP_POLL_SECONDS': '60'}, base=Path('/tmp/mvp'))
                         .poll_seconds, 60)
        with self.assertRaises(ConfigError):
            Settings.from_env({'MVP_POLL_SECONDS': '61'}, base=Path('/tmp/mvp'))

    def test_paths_and_bot_id_are_required_shapes(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({'MVP_BOT_ID': ' '}, base=Path('/tmp/mvp'))
        with self.assertRaises(ConfigError):
            Settings.from_env({'MVP_DB_PATH': ''}, base=Path('/tmp/mvp'))

    def test_settings_are_frozen(self):
        s = Settings.from_env({}, base=Path('/tmp/mvp'))
        with self.assertRaises(Exception):
            s.max_order = Decimal('99')


if __name__ == '__main__':
    unittest.main()
