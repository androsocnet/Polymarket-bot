"""Unit and adversarial tests for Phase 2B authoritative settlement and accounting."""
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from polymarket_bot.linux_mvp.config import Settings
from polymarket_bot.linux_mvp.engine import PaperEngine
from polymarket_bot.linux_mvp.report import build_report
from polymarket_bot.linux_mvp.settlement import classify_resolution
from polymarket_bot.linux_mvp.store import MvpStore
from tests.linux_mvp_support import CONDITION, MARKET_ID, START, Collector, descriptor, rising, snapshot, us

BOT = 'default'
RUN = 'run-settle-1'


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = MvpStore.open(self.base / 'paper.sqlite')
        self.addCleanup(self.store.close)
        self.store.start_run(RUN, BOT, initial_cash=Decimal('100'), config={}, started_us=us(START))
        self.settings = Settings.from_env({}, base=self.base)

    def test_classify_resolution_handles_up_down_and_anomalies(self):
        """classify_resolution strictly validates condition_id, status, and payout vector."""
        # Valid UP win (index 0 > 0)
        up_payload = {
            'condition_id': CONDITION,
            'status': 'resolved',
            'payouts': [1000000, 0],
            'resolved_at': '2026-10-23T01:00:00Z',
            'transaction_hash': '0x1111'
        }
        res_up = classify_resolution(up_payload, condition_id=CONDITION, up_index=0)
        self.assertEqual(res_up['status'], 'RESOLVED')
        self.assertEqual(res_up['winning_outcome'], 'UP')
        self.assertEqual(res_up['tx_hash'], '0x1111')

        # Valid DOWN win (index 1 > 0)
        down_payload = {
            'condition_id': CONDITION,
            'status': 'resolved',
            'payouts': [0, 1000000],
            'resolved_at': '2026-10-23T01:00:00Z',
            'transaction_hash': '0x2222'
        }
        res_down = classify_resolution(down_payload, condition_id=CONDITION, up_index=0)
        self.assertEqual(res_down['status'], 'RESOLVED')
        self.assertEqual(res_down['winning_outcome'], 'DOWN')

        # Pending status
        pending_payload = {'condition_id': CONDITION, 'status': 'open', 'payouts': None}
        res_pending = classify_resolution(pending_payload, condition_id=CONDITION)
        self.assertEqual(res_pending['status'], 'PENDING')
        self.assertIsNone(res_pending['winning_outcome'])

        # Condition mismatch -> INVALID
        wrong_cond = classify_resolution(up_payload, condition_id='0x' + '99' * 32)
        self.assertEqual(wrong_cond['status'], 'INVALID')
        self.assertIsNone(wrong_cond['winning_outcome'])

        # Contradictory / split payouts [500000, 500000] -> INVALID / unverified
        split_payload = {'condition_id': CONDITION, 'status': 'resolved', 'payouts': [500000, 500000]}
        res_split = classify_resolution(split_payload, condition_id=CONDITION)
        self.assertEqual(res_split['status'], 'INVALID')
        self.assertIsNone(res_split['winning_outcome'])

        # Malformed payouts vector
        bad_vector = {'condition_id': CONDITION, 'status': 'resolved', 'payouts': [1000000]}
        res_bad = classify_resolution(bad_vector, condition_id=CONDITION)
        self.assertEqual(res_bad['status'], 'INVALID')

    def test_store_settle_window_is_idempotent_and_balances_accounting(self):
        """Settling a winning position credits $1.00 payout per share and releases exposure."""
        self.store.ensure_window(BOT, RUN, market_id=MARKET_ID, condition_id=CONDITION,
                                 event_id='2001', start_us=us(START), end_us=us(START) + 900_000_000,
                                 at_us=us(START))
        self.store.close_window(BOT, MARKET_ID, 'UNKNOWN_SETTLEMENT', note='window_ended',
                                at_us=us(START) + 900_000_000)

        # Simulate filled BUY UP order of 10 shares at $0.50 + 10 bps fee ($0.005)
        # Cost basis = $5.005, Cash remaining = $94.995
        self.store.queue_order('ord-1', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('10.00'), MARKET_ID + ':1', 'SIGNAL_UP', us(START))
        books = {'UP': snapshot(slot=2, up_ask='0.50', size='100').up.book}
        fill = self.store.execute_pending(BOT, books, Decimal('10'), at_us=us(START) + 2_000_000)
        self.assertEqual(fill['status'], 'FILLED')

        cash_before = self.store.cash(BOT)
        self.assertEqual(cash_before, Decimal('94.995'))
        pos_before = self.store.position(BOT, MARKET_ID, 'UP')
        self.assertEqual(pos_before['quantity'], Decimal('10.00'))
        self.assertEqual(pos_before['cost_basis'], Decimal('5.005'))

        # Settle UP win
        res = self.store.settle_window(BOT, MARKET_ID, 'UP', resolved_at='2026-10-23T01:00:00Z',
                                       tx_hash='0xabc', at_us=us(START) + 1_000_000_000)
        self.assertEqual(res['status'], 'RESOLVED')
        self.assertEqual(res['winning_outcome'], 'UP')
        self.assertEqual(res['payout_total'], Decimal('10.00'))
        self.assertEqual(res['realized_pnl'], Decimal('4.995'))  # $10.00 - $5.005

        # Cash balances: 94.995 + 10.00 = 104.995
        self.assertEqual(self.store.cash(BOT), Decimal('104.995'))
        self.assertEqual(self.store.realized(BOT), Decimal('4.995'))

        # Position is cleared and exposure released
        pos_after = self.store.position(BOT, MARKET_ID, 'UP')
        self.assertEqual(pos_after['quantity'], Decimal('0'))
        self.assertEqual(pos_after['cost_basis'], Decimal('0'))
        self.assertEqual(len(self.store.open_positions(BOT)), 0)

        # Window status is SETTLED
        win = self.store.window(BOT, MARKET_ID)
        self.assertEqual(win['status'], 'SETTLED')

        # Idempotency check: repeat settlement has zero effect
        res_repeat = self.store.settle_window(BOT, MARKET_ID, 'UP', resolved_at='2026-10-23T01:00:00Z',
                                              tx_hash='0xabc', at_us=us(START) + 1_000_000_000)
        self.assertEqual(res_repeat['status'], 'RESOLVED')
        self.assertEqual(self.store.cash(BOT), Decimal('104.995'))
        self.assertEqual(self.store.realized(BOT), Decimal('4.995'))

    def test_store_settle_window_losing_position(self):
        """Settling a losing position produces $0.00 payout, loss equal to cost basis, and 0 exposure."""
        self.store.ensure_window(BOT, RUN, market_id='1002', condition_id=CONDITION,
                                 event_id='2001', start_us=us(START), end_us=us(START) + 900_000_000,
                                 at_us=us(START))
        self.store.close_window(BOT, '1002', 'UNKNOWN_SETTLEMENT', note='window_ended',
                                at_us=us(START) + 900_000_000)

        # Buy 10 shares of DOWN at $0.40 + fee $0.004 -> cost basis $4.004
        self.store.queue_order('ord-2', BOT, RUN, '1002', '222222', 'DOWN', 'BUY',
                               Decimal('10.00'), '1002:1', 'SIGNAL_DOWN', us(START))
        books = {'DOWN': snapshot(slot=2, down_ask='0.40', size='100').down.book}
        self.store.execute_pending(BOT, books, Decimal('10'), at_us=us(START) + 2_000_000)

        # Settle UP win (DOWN loses)
        res = self.store.settle_window(BOT, '1002', 'UP', resolved_at='2026-10-23T01:00:00Z',
                                       tx_hash='0xdef', at_us=us(START) + 1_000_000_000)
        self.assertEqual(res['status'], 'RESOLVED')
        self.assertEqual(res['payout_total'], Decimal('0'))
        self.assertEqual(res['realized_pnl'], Decimal('-4.004'))
        self.assertEqual(self.store.position(BOT, '1002', 'DOWN')['quantity'], Decimal('0'))

    def test_engine_reconciles_settlement_and_unlocks_trading(self):
        """Engine automatically polls resolution, settles winning position, and unlocks trading in new window."""
        market_a = descriptor(market_id='1001', start=START, condition=CONDITION)
        market_b = descriptor(market_id='1002', start=START + timedelta(minutes=15),
                              condition='0x' + 'cd' * 32, up='333333', down='444444')

        # Steps:
        # 1-4: warmup and fill BUY UP in market 1001
        # 5: rollover to 1002 (1001 becomes UNKNOWN_SETTLEMENT, holding 9.99 UP shares)
        # 6-7: 1002 warmup slots (new trades blocked by 1001 position)
        samples = [
            rising(0, market=market_a),
            rising(1, market=market_a),
            rising(2, market=market_a),
            rising(3, market=market_a),  # fills BUY UP in 1001
            rising(0, start=START + timedelta(minutes=15), market=market_b),  # rollover
            rising(1, start=START + timedelta(minutes=15), market=market_b),
            rising(2, start=START + timedelta(minutes=15), market=market_b),  # slot 2 in 1002
        ]
        collector = Collector(samples)
        # Provide resolution for condition 1001: UP won
        collector.resolutions = {
            CONDITION: {
                'condition_id': CONDITION,
                'status': 'resolved',
                'payouts': [1000000, 0],
                'resolved_at': '2026-10-23T01:00:00Z',
                'transaction_hash': '0x9999'
            }
        }
        eng = PaperEngine(self.store, collector, self.settings, run_id=RUN,
                          now_us=lambda: us(START) + 901_000_000)

        for _ in range(7):
            eng.step()

        # Window 1001 should now be SETTLED
        win1 = self.store.window(BOT, '1001')
        self.assertEqual(win1['status'], 'SETTLED')

        # Position in 1001 cleared to 0
        pos1 = self.store.position(BOT, '1001', 'UP')
        self.assertEqual(pos1['quantity'], Decimal('0'))

        # Because position in 1001 settled, 1002 slot 2 was NOT blocked by POSITION_LIMIT!
        dec_1002 = self.store.decisions_for_market(BOT, '1002')
        slot2_dec = next(d for d in dec_1002 if d['slot_index'] == 2)
        self.assertEqual(slot2_dec['action'], 'BUY UP')
        self.assertEqual(slot2_dec['reason'], 'HOLD')  # action is BUY UP, reason HOLD/SIGNAL_UP

    def test_report_includes_settled_performance_and_win_rate(self):
        """Report accounts for settled wins and calculates verified win rate."""
        self.store.ensure_window(BOT, RUN, market_id='1001', condition_id=CONDITION,
                                 event_id='2001', start_us=us(START), end_us=us(START) + 900_000_000,
                                 at_us=us(START))
        self.store.close_window(BOT, '1001', 'UNKNOWN_SETTLEMENT', note='ended',
                                at_us=us(START) + 900_000_000)
        self.store.queue_order('ord-w', BOT, RUN, '1001', '111111', 'UP', 'BUY',
                               Decimal('10.00'), '1001:1', 'SIGNAL_UP', us(START))
        books = {'UP': snapshot(slot=2, up_ask='0.50', size='100').up.book}
        self.store.execute_pending(BOT, books, Decimal('10'), at_us=us(START) + 2_000_000)

        # Settle UP win
        self.store.settle_window(BOT, '1001', 'UP', resolved_at='2026-10-23T01:00:00Z',
                                 tx_hash='0x123', at_us=us(START) + 1_000_000_000)

        report = build_report(self.base / 'paper.sqlite')
        self.assertEqual(report['performance']['verified_settled_trades'], 1)
        self.assertEqual(report['performance']['verified_wins'], 1)
        self.assertEqual(report['performance']['verified_losses'], 0)
        self.assertEqual(report['performance']['win_rate_verified_settled'], '1.0000')
        self.assertEqual(report['positions']['open'], 0)
        self.assertEqual(report['positions']['unresolved'], 0)

    def test_missing_and_delayed_resolution_preserves_unknown_settlement(self):
        """Missing or delayed public API response leaves window UNKNOWN_SETTLEMENT without error."""
        market_a = descriptor(market_id='1001', start=START, condition=CONDITION)
        market_b = descriptor(market_id='1002', start=START + timedelta(minutes=15),
                              condition='0x' + 'cd' * 32, up='333333', down='444444')
        samples = [
            rising(0, market=market_a),
            rising(0, start=START + timedelta(minutes=15), market=market_b),
        ]
        collector = Collector(samples)
        # Empty resolutions dict -> public API returned None / 404
        collector.resolutions = {}
        eng = PaperEngine(self.store, collector, self.settings, run_id=RUN,
                          now_us=lambda: us(START) + 901_000_000)
        for _ in range(2):
            eng.step()

        win = self.store.window(BOT, '1001')
        self.assertEqual(win['status'], 'UNKNOWN_SETTLEMENT')
        self.assertEqual(len(self.store.settlements(BOT)), 0)

    def test_engine_restart_with_unsettled_windows_recovers_and_settles(self):
        """Service restart preserves UNKNOWN_SETTLEMENT and successfully resolves in next run."""
        self.store.ensure_window(BOT, 'run-old', market_id='1001', condition_id=CONDITION,
                                 event_id='2001', start_us=us(START), end_us=us(START) + 900_000_000,
                                 at_us=us(START))
        self.store.close_window(BOT, '1001', 'UNKNOWN_SETTLEMENT', note='rollover',
                                at_us=us(START) + 900_000_000)

        # Restart service run
        self.store.start_run('run-new', BOT, initial_cash=Decimal('100'), config={},
                             started_us=us(START) + 901_000_000)
        market_b = descriptor(market_id='1002', start=START + timedelta(minutes=15),
                              condition='0x' + 'cd' * 32)
        collector = Collector([rising(0, start=START + timedelta(minutes=15), market=market_b)])
        collector.resolutions = {
            CONDITION: {
                'condition_id': CONDITION,
                'status': 'resolved',
                'payouts': [0, 1000000],  # DOWN won
                'resolved_at': '2026-10-23T01:00:00Z',
                'transaction_hash': '0xdown'
            }
        }
        restarted_eng = PaperEngine(self.store, collector, self.settings, run_id='run-new',
                                    now_us=lambda: us(START) + 902_000_000)
        restarted_eng.step()

        # Window 1001 resolved in new run
        win = self.store.window(BOT, '1001')
        self.assertEqual(win['status'], 'SETTLED')
        self.assertEqual(win['note'], 'SETTLED_DOWN')
        settlement = self.store.settlements(BOT)[0]
        self.assertEqual(settlement['winning_outcome'], 'DOWN')


if __name__ == '__main__':
    unittest.main()
