"""SYNTHETIC financial fixtures; never historical performance evidence."""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from polymarket_bot import analytics
from polymarket_bot.linux_mvp.store import MvpStore
from tests.linux_mvp_support import book

D = Decimal
DAY = 86_400_000_000


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-analytics-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'synthetic.sqlite'
        self.store = MvpStore.open(self.path)
        self.addCleanup(self.store.close)
        self.store.start_run('r', 'b', initial_cash=D('100'), config={'fee_bps': '100'}, started_us=0)
        self.window('m')

    def window(self, market, start=0, end=900_000_000):
        self.store.ensure_window('b', 'r', market_id=market, condition_id='c', event_id='e',
                                 start_us=start, end_us=end, at_us=start)

    def fill(self, name, side, qty, price, at, *, market='m', outcome='UP', reason='SIGNAL_UP', size='100'):
        token = 'u' if outcome == 'UP' else 'd'
        slot = str(at // 1_000_000)
        decision_at = at - 1_000_000
        self.store.decide('b', 'r', market, int(slot), slot, side, reason, outcome,
                          D('.02'), D('.5'), D('100'), D('0'), {}, decision_at)
        self.store.observe('b', 'r', market, int(slot), outcome, token, D(price) - D('.01'),
                           D(price), D(price) - D('.005'), decision_at, decision_at)
        self.store.queue_order(name, 'b', 'r', market, token, outcome, side, D(qty), slot, reason, decision_at)
        self.store.execute_pending('b', {outcome: book(token, bid=price, ask=price, condition='c', size=size)},
                                   D('100'), at_us=at)

    def report(self):
        return analytics.analyze(self.path, source_label='SYNTHETIC')

    def settle(self, market, winner, at, payout, pnl):
        # Fixture directly encodes an already-authoritative settlement row, not evidence verification.
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO settlements(bot_id,market_id,condition_id,status,winning_outcome,"
                         "payout_total,realized_pnl,settled_us,position_count) VALUES(?,?,?,'RESOLVED',?,?,?,?,1)",
                         ('b', market, 'c', winner, payout, pnl, at))
            conn.execute("UPDATE positions SET quantity='0',cost_basis='0' WHERE market_id=?", (market,))
            conn.execute("UPDATE windows SET status='SETTLED' WHERE market_id=?", (market,))

    def test_fees_once_closed_trade_not_sell_fill(self):
        self.fill('buy', 'BUY', '10', '.4', 2_000_000)
        self.fill('partial', 'SELL', '4', '.5', 4_000_000, reason='HOLDING_PERIOD')
        self.fill('close', 'SELL', '6', '.3', 6_000_000, reason='REVERSAL')
        r = self.report()
        self.assertEqual(r['fills']['count'], 3)
        self.assertEqual(r['closed']['count'], 1)
        self.assertEqual(D(r['realized_net_pnl']), D('-.278'))
        self.assertEqual(D(r['fees']), D('.078'))
        self.assertEqual(D(r['closed']['expectancy']), D('-.278'))
        self.assertEqual(r['closed']['wins'], 0)
        self.assertEqual(r['closed']['losses'], 1)
        self.assertEqual(D(r['closed']['profit_factor']), D('0'))
        self.assertTrue(r['reconciliation']['ok'])
        self.assertEqual(D(r['trades'][0]['gross_pnl']), D('-.2'))
        self.assertEqual(D(r['trades'][0]['holding_seconds']), D('4'))
        self.assertEqual(r['groups']['outcome']['UP']['count'], 1)
        self.assertEqual(D(r['realized_by_exit_reason']['HOLDING_PERIOD']), D('.364'))

    def test_partial_buy_and_sell_remain_open_unknown(self):
        self.fill('buy', 'BUY', '10', '.4', 2_000_000, size='5')
        self.fill('sell', 'SELL', '5', '.5', 4_000_000, size='2', reason='HOLDING_PERIOD')
        self.store.close_window('b', 'm', 'UNKNOWN_SETTLEMENT', at_us=900_000_000, note='synthetic')
        r = self.report()
        self.assertEqual(r['closed']['count'], 0)
        self.assertEqual(r['fills']['partial'], 2)
        self.assertEqual(r['open_trades'][0]['status'], 'UNKNOWN_SETTLEMENT')
        self.assertEqual(D(r['open_trades'][0]['quantity']), D('3'))
        self.assertEqual(D(r['realized_net_pnl']), D('.182'))
        self.assertEqual(D(r['open_trades'][0]['remaining_basis']), D('1.212'))
        self.assertEqual(r['open_trades'][0]['exit_reason'], 'HOLDING_PERIOD')
        self.assertTrue(r['reconciliation']['ok'])

    def test_weighted_basis_multiple_entries_and_outcomes(self):
        self.fill('a', 'BUY', '2', '.2', 2_000_000)
        self.fill('b', 'BUY', '2', '.6', 4_000_000)
        self.fill('c', 'SELL', '4', '.5', 6_000_000)
        self.fill('d', 'BUY', '2', '.5', 8_000_000, outcome='DOWN')
        self.fill('e', 'SELL', '2', '.4', 10_000_000, outcome='DOWN')
        r = self.report()
        self.assertEqual(r['closed']['count'], 2)
        self.assertEqual(r['closed']['wins'], 1)
        self.assertEqual(r['closed']['losses'], 1)
        self.assertEqual(D(r['closed']['net_pnl']), D('.146'))
        self.assertEqual(D(r['groups']['outcome']['UP']['net_pnl']), D('.364'))
        self.assertEqual(D(r['groups']['outcome']['DOWN']['net_pnl']), D('-.218'))
        self.assertEqual(D(r['trades'][0]['entry_price']), D('.4'))

    def test_settlement_closes_residual_not_prior_completed_trade(self):
        self.fill('a', 'BUY', '10', '.4', 2_000_000)
        self.fill('b', 'SELL', '4', '.5', 4_000_000)
        self.settle('m', 'UP', DAY, '6', '3.576')
        r = self.report()
        self.assertEqual(r['closed']['count'], 1)
        self.assertEqual(D(r['realized_net_pnl']), D('3.94'))
        self.assertEqual(D(r['closed']['net_pnl']), D('3.94'))
        self.assertEqual(D(r['realized_by_utc_day']['1970-01-01']), D('.364'))
        self.assertEqual(D(r['realized_by_utc_day']['1970-01-02']), D('3.576'))
        self.assertTrue(r['reconciliation']['ok'])
        self.assertIsNone(r['closed']['profit_factor'])

    def test_utc_boundary_uses_execution_not_decision_time(self):
        self.window('night', DAY - 900_000_000, DAY + 900_000_000)
        self.fill('a', 'BUY', '1', '.4', DAY - 1, market='night')
        self.fill('b', 'SELL', '1', '.5', DAY, market='night')
        self.assertEqual(set(self.report()['realized_by_utc_day']), {'1970-01-02'})

    def test_reconciliation_detects_tampered_realized_and_positions(self):
        self.fill('a', 'BUY', '1', '.4', 2_000_000)
        self.fill('b', 'SELL', '1', '.5', 4_000_000)
        self.store._conn.execute("UPDATE orders SET realized='9' WHERE order_id='b'")
        r = self.report()
        self.assertFalse(r['reconciliation']['ok'])
        self.assertTrue(r['reconciliation']['errors'])

    def test_equity_maximum_drawdown_not_current_drawdown(self):
        for at, value in [(1, '110'), (2, '80'), (3, '108')]:
            self.store.equity('b', 'r', at_us=at, market_id='m', cash=D(value), exposure=D('0'),
                              realized=D('0'), unrealized=D('0'), missing_marks=0)
        r = self.report()
        self.assertEqual(D(r['equity']['sampled_max_drawdown']), D('30'))
        self.assertEqual(r['equity']['coverage'], 'SAMPLED_ONLY')
        self.store.equity('b', 'r', at_us=4, market_id='m', cash=D('1'), exposure=D('1'),
                          realized=D('0'), unrealized=None, missing_marks=1)
        self.assertIsNone(self.report()['equity']['sampled_max_drawdown'])

    def test_missing_optional_data_explicit_and_no_inferred_settlement(self):
        self.fill('a', 'BUY', '1', '.4', 2_000_000)
        self.store._conn.execute('DROP TABLE observations')
        self.store._conn.execute('DROP TABLE settlements')
        r = self.report()
        self.assertIn('observations', r['missing_tables'])
        self.assertIn('settlements', r['missing_tables'])
        self.assertEqual(r['closed']['count'], 0)
        self.assertIsNone(r['open_trades'][0]['decision_ask'])
        self.assertIsNone(r['equity']['sampled_max_drawdown'])

    def test_rejects_nonfinite_and_missing_financial_fields(self):
        self.fill('a', 'BUY', '1', '.4', 2_000_000)
        for value in ('NaN', '-1', 'Infinity', ''):
            self.store._conn.execute('UPDATE orders SET fee=?', (value,))
            with self.assertRaises(ValueError):
                self.report()

    def test_read_only_deterministic_cli_and_missing_path(self):
        self.fill('a', 'BUY', '1', '.4', 2_000_000)
        self.store.close()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        r = self.report()
        self.assertEqual(r, self.report())
        result = subprocess.run([sys.executable, '-m', 'polymarket_bot.analytics', str(self.path),
                                 '--source-label', 'SYNTHETIC'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), r)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        missing = self.path.parent / 'missing.sqlite'
        with self.assertRaises((ValueError, sqlite3.Error)):
            analytics.analyze(missing)
        self.assertFalse(missing.exists())

    def test_evaluation_chronological_purge_and_preserved_costs(self):
        self.fill('a', 'BUY', '1', '.4', 2_000_000)
        self.fill('b', 'SELL', '1', '.5', 4_000_000)
        self.window('overlap', 800_000_000, 1_700_000_000)
        self.fill('c', 'BUY', '1', '.4', 802_000_000, market='overlap')
        self.fill('d', 'SELL', '1', '.5', 804_000_000, market='overlap')
        self.window('test', 1_000_000_000, 1_900_000_000)
        self.fill('e', 'BUY', '1', '.8', 1_002_000_000, market='test')
        self.fill('f', 'SELL', '1', '.7', 1_004_000_000, market='test')
        report = self.report()
        result = analytics.evaluate(report, split_us=1_000_000_000, max_entry_price=D('.6'))
        self.assertEqual(result['train']['baseline']['count'], 1)
        self.assertEqual(D(result['train']['candidate']['net_pnl']), D('.091'))
        self.assertEqual(result['test']['baseline']['count'], 1)
        self.assertEqual(result['test']['candidate']['count'], 0)
        self.assertEqual(result['excluded']['boundary_or_missing_time'], 1)
        identity = analytics.evaluate(report, split_us=1_000_000_000)
        self.assertEqual(identity['test']['baseline'], identity['test']['candidate'])
        self.assertEqual(result, analytics.evaluate(report, split_us=1_000_000_000, max_entry_price=D('.6')))
        self.assertEqual(report, self.report())
        self.store._conn.execute("UPDATE orders SET fee='.001' WHERE order_id='f'")
        with self.assertRaisesRegex(ValueError, 'reconcil'):
            analytics.evaluate(self.report(), split_us=1_000_000_000)

    def test_evaluation_never_uses_future_quote_or_fill_price_for_selection(self):
        self.fill('a', 'BUY', '1', '.8', 2_000_000)
        self.fill('b', 'SELL', '1', '.9', 4_000_000)
        self.store._conn.execute("UPDATE observations SET ask='.4' WHERE slot_index=2")
        result = analytics.evaluate(self.report(), split_us=1_000_000_000, max_entry_price=D('.6'))
        self.assertEqual(result['train']['candidate']['count'], 1)
        self.store._conn.execute('UPDATE observations SET received_us=9000000 WHERE slot_index=2')
        result = analytics.evaluate(self.report(), split_us=1_000_000_000, max_entry_price=D('.6'))
        self.assertEqual(result['train']['candidate']['count'], 0)
        self.assertEqual(result['train']['missing_entry_inputs'], 1)

    def test_evaluation_late_entry_and_settlement_crossing_split(self):
        self.fill('a', 'BUY', '1', '.4', 899_000_000)
        self.settle('m', 'UP', 1_100_000_000, '1', '.596')
        result = analytics.evaluate(self.report(), split_us=1_000_000_000)
        self.assertEqual(result['train']['baseline']['count'], 0)
        self.assertEqual(result['excluded']['boundary_or_missing_time'], 1)
        result = analytics.evaluate(self.report(), split_us=1_200_000_000, min_remaining_seconds=60)
        self.assertEqual(result['train']['baseline']['count'], 1)
        self.assertEqual(result['train']['candidate']['count'], 0)

    def test_evaluation_training_unchanged_by_future_outcomes(self):
        self.fill('a', 'BUY', '1', '.4', 2_000_000)
        self.fill('b', 'SELL', '1', '.5', 4_000_000)
        before = analytics.evaluate(self.report(), split_us=1_000_000_000)['train']
        self.window('future', 1_000_000_000, 1_900_000_000)
        self.fill('c', 'BUY', '1', '.8', 1_002_000_000, market='future')
        self.fill('d', 'SELL', '1', '.1', 1_004_000_000, market='future')
        after = analytics.evaluate(self.report(), split_us=1_000_000_000)['train']
        self.assertEqual(before, after)

    def test_evaluation_cli_exposes_fixed_parameters(self):
        self.fill('a', 'BUY', '1', '.4', 2_000_000)
        self.fill('b', 'SELL', '1', '.5', 4_000_000)
        result = subprocess.run([sys.executable, '-m', 'polymarket_bot.analytics', str(self.path),
                                 '--split-us', '1000000000', '--max-entry-price', '.6',
                                 '--min-remaining-seconds', '60'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['evaluation']['train']['candidate']['count'], 1)

    def test_evaluation_rejects_invalid_parameters(self):
        r = self.report()
        for kwargs in ({'max_entry_price': D('NaN')}, {'max_entry_price': D('-1')},
                       {'min_remaining_seconds': -1}, {'split_us': True}, {'split_us': -1}):
            with self.assertRaises(ValueError):
                analytics.evaluate(r, **({'split_us': 1_000_000_000} | kwargs))

    def test_multiple_accounts_require_selection(self):
        self.store.start_run('r2', 'other', initial_cash=D('100'), config={}, started_us=1)
        with self.assertRaisesRegex(ValueError, 'bot'):
            self.report()
        self.assertEqual(analytics.analyze(self.path, bot_id='b')['bot_id'], 'b')


if __name__ == '__main__':
    unittest.main()
