"""Read-only report: exact counts, no invented settlement, deterministic rendering."""
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from polymarket_bot.linux_mvp.config import Settings
from polymarket_bot.linux_mvp.engine import PaperEngine
from polymarket_bot.linux_mvp.report import build_report, render
from polymarket_bot.linux_mvp.store import MvpStore
from tests.linux_mvp_support import MARKET_ID, START, Collector, rising, us

BOT = 'default'


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'paper.sqlite'
        self.store = MvpStore.open(self.path)
        self.addCleanup(self.store.close)
        self.store.start_run('run-1', BOT, initial_cash=Decimal('100'), config={}, started_us=us(START))
        self.settings = Settings.from_env({}, base=Path(self.tmp.name))
        self.collector = Collector([rising(0), rising(1), rising(2), rising(3)])
        self.engine = PaperEngine(self.store, self.collector, self.settings, run_id='run-1',
                                  now_us=lambda: us(START))
        for _ in range(4):
            self.engine.step()

    def test_counts_accounting_and_per_window(self):
        report = build_report(self.path, now_us=123456)
        self.assertEqual(report['schema'], 'mvp-report-v1')
        self.assertEqual(report['mode'], 'PAPER_ONLY')
        self.assertEqual(report['execution'], 'HYPOTHETICAL_PAPER_ONLY')
        self.assertEqual(report['settlement'], 'UNKNOWN_NOT_INFERRED')
        self.assertEqual(report['generated_us'], 123456)
        self.assertEqual(report['decisions']['total'], 4)
        self.assertEqual(report['decisions']['by_action'], {'HOLD': 3, 'BUY UP': 1})
        self.assertEqual(report['orders']['total'], 1)
        self.assertEqual(report['orders']['by_status'], {'FILLED': 1})
        self.assertEqual(report['windows']['total'], 1)
        self.assertEqual(report['windows']['by_status'], {'ACTIVE': 1})
        self.assertEqual(report['per_window'][0]['market_id'], MARKET_ID)
        self.assertEqual(report['per_window'][0]['buys'], 1)
        self.assertEqual(report['per_window'][0]['decisions'], 4)
        order = self.store.orders(BOT)[0]
        self.assertEqual(Decimal(report['orders']['fees']), order['fee'])
        latest = self.store.equity_rows(BOT)[-1]
        expected_net = Decimal(latest['cash']) + Decimal(latest['exposure']) + Decimal(latest['unrealized'])
        self.assertEqual(Decimal(report['account']['net_value']), expected_net)
        self.assertEqual(Decimal(report['account']['cash']), self.store.cash(BOT))
        self.assertEqual(report['equity']['missing_marks_last'], 0)
        self.assertEqual(report['rejections'], {})

    def test_rejections_and_missing_marks_are_reported_not_hidden(self):
        self.store.queue_order('rej-1', BOT, 'run-1', MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('1'), MARKET_ID + ':2', 'SIGNAL_UP', us(START))
        self.store.reject_order('rej-1', 'EXPIRED_EXECUTION_WINDOW', at_us=us(START) + 5)
        self.store.equity(BOT, 'run-1', at_us=us(START) + 6, market_id=MARKET_ID,
                          cash=Decimal('96'), exposure=Decimal('3.8'), realized=Decimal('0'),
                          unrealized=None, missing_marks=1)
        report = build_report(self.path, now_us=1)
        self.assertEqual(report['rejections'], {'EXPIRED_EXECUTION_WINDOW': 1})
        self.assertEqual(report['equity']['missing_marks_last'], 1)
        self.assertEqual(report['equity']['unmarked_points'], 1)
        self.assertIsNone(report['account']['net_value'])
        self.assertIsNone(report['equity']['drawdown'])
        text = render(report)
        self.assertIn('missing_marks_last=1', text)
        self.assertNotIn('settled', text.lower())

    def test_render_is_explicit_about_scope(self):
        text = render(build_report(self.path, now_us=1))
        for token in ('PAPER_ONLY', 'HYPOTHETICAL_PAPER_ONLY', 'UNKNOWN_NOT_INFERRED',
                      'assumptions:', 'limitations:', 'no profitability', 'CP5/CP6'):
            self.assertIn(token, text)

    def test_report_is_deterministic_for_fixed_clock(self):
        one = build_report(self.path, now_us=99)
        two = build_report(self.path, now_us=99)
        self.assertEqual(one, two)

    def test_report_includes_positions_and_settlement_metrics(self):
        report = build_report(self.path, now_us=123456)
        self.assertIn('positions', report)
        self.assertEqual(report['positions']['open'], 1)
        self.assertEqual(report['positions']['unresolved'], 1)
        self.assertEqual(report['positions']['unknown_settlement'], 1)
        self.assertEqual(len(report['positions']['items']), 1)
        self.assertEqual(report['positions']['items'][0]['outcome'], 'UP')
        self.assertIn('performance', report)
        self.assertIsNone(report['performance']['win_rate_verified_settled'])
        self.assertEqual(report['performance']['verified_settled_trades'], 0)
        self.assertEqual(report['performance']['realized_pnl'], report['account']['realized'])

    def test_empty_database_is_refused(self):
        empty = Path(self.tmp.name) / 'empty.sqlite'
        MvpStore.open(empty).close()
        with self.assertRaises(ValueError):
            build_report(empty)
        missing = Path(self.tmp.name) / 'missing.sqlite'
        with self.assertRaises(Exception):
            build_report(missing)


if __name__ == '__main__':
    unittest.main()
