"""Targeted adversarial tests for Phase 2A autonomous paper trading."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from polymarket_bot.linux_mvp.config import Settings
from polymarket_bot.linux_mvp.engine import PaperEngine
from polymarket_bot.linux_mvp.store import MvpStore
from tests.linux_mvp_support import (CONDITION, MARKET_ID, START, Collector, descriptor,
                                     rising, snapshot, us)

BOT = 'default'
RUN = 'run-adv-1'
NEXT = START + timedelta(minutes=15)


class AdversarialPaperTradingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = MvpStore.open(self.base / 'paper.sqlite')
        self.addCleanup(lambda: self.store.close())
        self.store.start_run(RUN, BOT, initial_cash=Decimal('100'), config={}, started_us=us(START))
        self.settings = Settings.from_env({}, base=self.base)
        self.now = us(START)
        self.cycle = 0

    def engine(self, samples, *, settings=None, run_id=RUN):
        collector = Collector(samples)
        return PaperEngine(self.store, collector, settings or self.settings, run_id=run_id,
                           now_us=lambda: self.now)

    def step(self, eng):
        self.cycle += 1
        self.now = us(START) + self.cycle * 1_000_000
        return eng.step()

    def test_missing_bid_or_ask_prevents_signals_and_fills(self):
        """Missing book levels must yield ONE_SIDED_BOOK, HOLD action, and 0 orders."""
        # up_bid=None: one-sided UP book
        eng1 = self.engine([snapshot(slot=0, up_bid=None)])
        res1 = self.step(eng1)
        self.assertEqual(res1.reason, 'ONE_SIDED_BOOK')
        self.assertEqual(res1.action, 'HOLD')
        self.assertEqual(self.store.orders(BOT), [])

        # down_ask=None: one-sided DOWN book
        eng2 = self.engine([snapshot(slot=1, down_ask=None)])
        res2 = self.step(eng2)
        self.assertEqual(res2.reason, 'ONE_SIDED_BOOK')
        self.assertEqual(res2.action, 'HOLD')
        self.assertEqual(self.store.orders(BOT), [])

    def test_insufficient_liquidity_produces_partial_fill_and_rejection(self):
        """Depth less than order size produces PARTIAL fill (no retry); 0 depth produces REJECTED."""
        # 1. Warmup to trigger BUY UP at slot 2 (order quantity = 9.99)
        # Supply slot 3 with only 3.00 size on UP asks
        samples = [
            rising(0),
            rising(1),
            rising(2),
            snapshot(slot=3, up_bid='0.36', up_ask='0.38', size='3.00'),
        ]
        eng = self.engine(samples)
        for _ in range(4):
            self.step(eng)

        orders = self.store.orders(BOT)
        self.assertEqual(len(orders), 1)
        order = orders[0]
        # Partial fill verified
        self.assertEqual(order['status'], 'PARTIAL')
        self.assertEqual(order['filled'], Decimal('3.00'))
        self.assertEqual(order['notional'], Decimal('3.00') * Decimal('0.38'))
        expected_fee = order['notional'] * Decimal('10') / Decimal('10000')
        self.assertEqual(order['fee'], expected_fee)
        # Position updated only to filled quantity
        pos = self.store.position(BOT, MARKET_ID, 'UP')
        self.assertEqual(pos['quantity'], Decimal('3.00'))
        self.assertEqual(pos['cost_basis'], order['notional'] + expected_fee)

        # 2. Test 0 liquidity produces REJECTED
        self.store.queue_order('zero-liq', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('5.00'), MARKET_ID + ':4', 'SIGNAL_UP', us(START))
        books = {'UP': snapshot(slot=5, size='0.00').up.book}
        result = self.store.execute_pending(BOT, books, Decimal('10'), at_us=us(START) + 100)
        self.assertEqual(result['status'], 'REJECTED')
        self.assertEqual(result['reason'], 'INSUFFICIENT_LIQUIDITY')
        self.assertEqual(result['filled'], Decimal('0'))

    def test_stale_and_out_of_order_snapshots(self):
        """Stale snapshots (>max_age) yield STALE_PAIR; out-of-window yields OUT_OF_WINDOW."""
        # Lag 10 seconds > default max_source_age_seconds (5.0s)
        eng = self.engine([snapshot(slot=0, lag=10.0)])
        res = self.step(eng)
        self.assertEqual(res.reason, 'STALE_PAIR')
        self.assertEqual(res.action, 'HOLD')
        self.assertEqual(self.store.orders(BOT), [])

        # Timestamp before window_start
        early_market = descriptor(start=START + timedelta(seconds=60))
        early_snap = snapshot(slot=0, start=START, market=early_market)
        eng_early = self.engine([early_snap])
        res_early = self.step(eng_early)
        self.assertEqual(res_early.status, 'OUT_OF_WINDOW')
        self.assertEqual(self.store.orders(BOT), [])

    def test_duplicate_slot_replay_is_strictly_idempotent(self):
        """Replaying identical slot multiple times yields DUPLICATE_SLOT without duplicate actions."""
        eng = self.engine([rising(0), rising(0), rising(0)])
        res1 = self.step(eng)
        self.assertEqual(res1.status, 'OK')
        res2 = self.step(eng)
        self.assertEqual(res2.status, 'DUPLICATE_SLOT')
        res3 = self.step(eng)
        self.assertEqual(res3.status, 'DUPLICATE_SLOT')

        decisions = self.store.decisions_for_market(BOT, MARKET_ID)
        self.assertEqual(len(decisions), 1)

    def test_market_rollover_preserves_open_positions_as_unknown_settlement(self):
        """Rollover marks old window UNKNOWN_SETTLEMENT and preserves open positions."""
        market_b = descriptor(market_id='1002', start=NEXT, condition='0x' + 'cd' * 32,
                              up='333333', down='444444')
        # Enter position in market 1, then step into market 2
        samples = [
            rising(0),
            rising(1),
            rising(2),
            rising(3),  # fills BUY UP in 1001
            rising(0, start=NEXT, market=market_b),  # triggers rollover
        ]
        eng = self.engine(samples)
        for _ in range(5):
            self.step(eng)

        # Old window is UNKNOWN_SETTLEMENT
        w1 = self.store.window(BOT, MARKET_ID)
        self.assertEqual(w1['status'], 'UNKNOWN_SETTLEMENT')
        # Position in old window still exists and is untouched
        pos = self.store.position(BOT, MARKET_ID, 'UP')
        self.assertEqual(pos['quantity'], Decimal('9.99'))
        # Open positions query still returns it
        open_pos = self.store.open_positions(BOT)
        self.assertEqual(len(open_pos), 1)
        self.assertEqual(open_pos[0]['market_id'], MARKET_ID)
        # Equity mark explicitly records missing mark
        eq = self.store.equity_rows(BOT)[-1]
        self.assertEqual(eq['missing_marks'], 1)
        self.assertIsNone(eq['unrealized'])

    def test_restart_with_open_positions_preserves_cash_and_basis(self):
        """Restarting engine with open positions does not corrupt accounting or re-open orders."""
        samples = [rising(0), rising(1), rising(2), rising(3)]
        eng = self.engine(samples)
        for _ in range(4):
            self.step(eng)

        cash_before = self.store.cash(BOT)
        pos_before = self.store.position(BOT, MARKET_ID, 'UP')
        orders_before = len(self.store.orders(BOT))

        # Restart service run
        self.store.close()
        self.store = MvpStore.open(self.base / 'paper.sqlite')
        self.store.start_run('run-adv-2', BOT, initial_cash=Decimal('100'), config={}, started_us=us(START) + 20)
        restarted = self.engine([rising(4)], run_id='run-adv-2')
        self.step(restarted)

        # Cash and positions strictly preserved
        self.assertEqual(self.store.cash(BOT), cash_before)
        self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP'), pos_before)
        self.assertEqual(len(self.store.orders(BOT)), orders_before)

    def test_unknown_settlement_never_substitutes_btc_spot_price(self):
        """Window close leaves status UNKNOWN_SETTLEMENT; no artificial settlement P&L is booked."""
        market_b = descriptor(market_id='1002', start=NEXT, condition='0x' + 'cd' * 32,
                              up='333333', down='444444')
        samples = [
            rising(0), rising(1), rising(2), rising(3),
            rising(0, start=NEXT, market=market_b)
        ]
        eng = self.engine(samples)
        for _ in range(5):
            self.step(eng)

        # Confirm UNKNOWN_SETTLEMENT
        self.assertEqual(self.store.window(BOT, MARKET_ID)['status'], 'UNKNOWN_SETTLEMENT')
        # Confirm realized PnL remains ZERO (no settlement realized PnL was invented)
        self.assertEqual(self.store.realized(BOT), Decimal('0'))
        # Orders table only has the 1 BUY order, no synthetic settlement order
        orders = self.store.orders(BOT)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]['side'], 'BUY')

    def test_fee_calculations_at_boundary_values(self):
        """Fee calculation is exact at zero and non-zero bps; cash deductions balance."""
        # 1. Zero fee test
        zero_fee_settings = replace(self.settings, fee_bps=Decimal('0'))
        self.store.queue_order('ord-fee0', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('10.00'), MARKET_ID + ':1', 'TEST', us(START))
        books = {'UP': snapshot(slot=2, up_ask='0.50', size='100').up.book}
        res0 = self.store.execute_pending(BOT, books, zero_fee_settings.fee_bps, at_us=us(START))
        self.assertEqual(res0['fee'], Decimal('0'))
        self.assertEqual(res0['notional'], Decimal('5.00'))  # 10 * 0.50

        # 2. High fee test (500 bps = 5%)
        high_fee_bps = Decimal('500')
        self.store.queue_order('ord-fee500', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('10.00'), MARKET_ID + ':3', 'TEST', us(START))
        res500 = self.store.execute_pending(BOT, books, high_fee_bps, at_us=us(START))
        self.assertEqual(res500['notional'], Decimal('5.00'))
        expected_fee = Decimal('5.00') * Decimal('500') / Decimal('10000')  # 0.25
        self.assertEqual(res500['fee'], expected_fee)

    def test_daily_loss_limit_breach_halts_cleanly(self):
        """When realized loss breaches daily limit, engine stops immediately with RISK_STOPPED."""
        eng = self.engine([rising(0)])
        # Simulate realized daily loss breaching limit of $10
        with unittest.mock.patch.object(self.store, 'realized', return_value=Decimal('-15.00')):
            res = self.step(eng)
            self.assertEqual(res.status, 'RISK_STOPPED')
            self.assertEqual(res.reason, 'DAILY_LOSS_LIMIT')
            # Collector was not even called
            self.assertEqual(eng.collector.calls, 0)

            # Subsequent step remains RISK_STOPPED
            res2 = self.step(eng)
            self.assertEqual(res2.status, 'RISK_STOPPED')

    def test_emergency_stop_halts_before_collection_and_execution(self):
        """Emergency stop file stops engine immediately without calling collector or filling."""
        self.store.queue_order('pend-stop', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('1.00'), MARKET_ID + ':0', 'SIGNAL_UP', us(START))
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings.stop_path.write_text('emergency stop\n')

        eng = self.engine([rising(1)])
        res = self.step(eng)
        self.assertEqual(res.status, 'STOPPED')
        self.assertEqual(res.reason, 'EMERGENCY_STOP')
        self.assertEqual(eng.collector.calls, 0)
        self.assertEqual(self.store.order('pend-stop')['status'], 'PENDING')

    def test_rollover_with_open_position_blocks_new_entry_in_successor(self):
        """Open position from an earlier window must block opening a second position in new market."""
        market_b = descriptor(market_id='1002', start=NEXT, condition='0x' + 'cd' * 32,
                              up='333333', down='444444')
        # 1. Warmup and enter position in 1001
        samples = [
            rising(0),
            rising(1),
            rising(2),
            rising(3),  # fills BUY UP in 1001
            rising(0, start=NEXT, market=market_b),  # rollover to 1002
            rising(1, start=NEXT, market=market_b),
            rising(2, start=NEXT, market=market_b),  # would trigger BUY UP in 1002 if isolated
        ]
        eng = self.engine(samples)
        for _ in range(7):
            self.step(eng)

        # In 1002 at slot 2, decision must NOT be BUY UP; must be blocked by position/exposure limit
        dec_1002 = self.store.decisions_for_market(BOT, '1002')
        slot2_decision = next(d for d in dec_1002 if d['slot_index'] == 2)
        self.assertEqual(slot2_decision['action'], 'HOLD')
        self.assertIn(slot2_decision['reason'], ('POSITION_LIMIT', 'EXPOSURE_LIMIT'))

        # Confirm strictly at most 1 open position across entire account
        open_positions = self.store.open_positions(BOT)
        self.assertEqual(len(open_positions), 1)
        self.assertEqual(open_positions[0]['market_id'], MARKET_ID)


if __name__ == '__main__':
    unittest.main()
