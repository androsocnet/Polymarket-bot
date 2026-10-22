"""MVP engine: causal decisions, paper fills, risk gates, stop, rollover, restart."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from polymarket_bot.linux_mvp.config import Settings
from polymarket_bot.linux_mvp.engine import PaperEngine
from polymarket_bot.linux_mvp.store import MvpStore
from polymarket_bot.market_data.models import MarketDataError
from tests.linux_mvp_support import (CONDITION, EVENT_ID, MARKET_ID, START, Collector, descriptor,
                                     rising, snapshot, us)

BOT = 'default'
RUN = 'run-1'
NEXT = START + timedelta(minutes=15)


class EngineTests(unittest.TestCase):
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
        self.collector = Collector(samples)
        engine = PaperEngine(self.store, self.collector, settings or self.settings, run_id=run_id,
                             now_us=lambda: self.now)
        return engine

    def step(self, engine, sample=None):
        self.cycle += 1
        self.now = us(START) + self.cycle * 1_000_000
        return engine.step()

    def decisions(self, market_id=MARKET_ID):
        return self.store.decisions_for_market(BOT, market_id)

    def test_warmup_then_buy_then_next_slot_fill(self):
        engine = self.engine([rising(0), rising(1), rising(2), rising(3)])
        results = [self.step(engine) for _ in range(4)]
        actions = [d['action'] for d in self.decisions()]
        self.assertEqual(actions, ['HOLD', 'HOLD', 'BUY UP', 'HOLD'])
        self.assertEqual([d['reason'] for d in self.decisions()][:2], ['WARMUP', 'WARMUP'])
        self.assertEqual(results[2].action, 'BUY UP')
        orders = self.store.orders(BOT)
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order['status'], 'FILLED')
        self.assertEqual(order['side'], 'BUY')
        self.assertEqual(order['outcome'], 'UP')
        self.assertEqual(order['quantity'], Decimal('9.99'))
        self.assertEqual(order['filled'], Decimal('9.99'))
        self.assertEqual(order['notional'], Decimal('9.99') * Decimal('0.38'))
        self.assertEqual(order['fee'], order['notional'] * Decimal('10') / Decimal('10000'))
        self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP')['quantity'], Decimal('9.99'))
        self.assertEqual(self.store.cash(BOT), Decimal('100') - order['notional'] - order['fee'])
        self.assertEqual(self.store.decision(BOT, MARKET_ID, MARKET_ID + ':2')['signal'], 'UP')
        self.assertEqual(Decimal(self.decisions()[2]['momentum']), Decimal('0.04'))

    def test_duplicate_slot_never_redecides_or_refills(self):
        engine = self.engine([rising(0), rising(1), rising(2), rising(2)])
        for _ in range(3):
            self.step(engine)
        duplicate = self.step(engine)
        self.assertEqual(duplicate.status, 'DUPLICATE_SLOT')
        self.assertEqual(len(self.decisions()), 3)
        self.assertEqual(len(self.store.orders(BOT)), 1)
        self.assertEqual(self.store.order(self.store.orders(BOT)[0]['order_id'])['status'], 'PENDING')

    def test_stale_and_one_sided_books_never_decide_or_order(self):
        stale = self.engine([snapshot(slot=0, lag=10)])
        result = self.step(stale)
        self.assertEqual(result.reason, 'STALE_PAIR')
        self.assertEqual(self.decisions()[0]['action'], 'HOLD')
        one_sided = self.engine([snapshot(slot=1, up_bid=None)])
        result = self.step(one_sided)
        self.assertEqual(result.reason, 'ONE_SIDED_BOOK')
        self.assertEqual(self.store.orders(BOT), [])

    def test_emergency_stop_blocks_execution_and_decisions(self):
        self.store.queue_order('pending-1', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('1'), MARKET_ID + ':0', 'SIGNAL_UP', us(START))
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings.stop_path.write_text('operator stop\n')
        engine = self.engine([rising(1)])
        result = self.step(engine)
        self.assertEqual(result.status, 'STOPPED')
        self.assertEqual(self.collector.calls, 0)
        self.assertEqual(self.store.order('pending-1')['status'], 'PENDING')
        self.assertEqual(self.decisions(), [])
        self.assertIn('emergency_stop', [e['kind'] for e in self.store.events(BOT)])
        again = self.step(engine)
        self.assertEqual(again.status, 'STOPPED')
        self.assertEqual([e['kind'] for e in self.store.events(BOT)].count('emergency_stop'), 1)

    def test_data_errors_back_off_then_stop(self):
        engine = self.engine([MarketDataError('transport_error', 'boom'),
                              MarketDataError('transport_error', 'boom')],
                             settings=replace(self.settings, max_consecutive_errors=2))
        first = self.step(engine)
        self.assertEqual(first.status, 'ERROR')
        second = self.step(engine)
        self.assertEqual(second.status, 'FAILED_STOP')
        third = self.step(engine)
        self.assertEqual(third.status, 'FAILED_STOP')
        self.assertEqual(self.collector.calls, 2)
        self.assertEqual([e['kind'] for e in self.store.events(BOT)].count('data_error'), 2)

    def test_failed_stop_is_retried_by_a_new_process_run(self):
        failing = replace(self.settings, max_consecutive_errors=2)
        engine = self.engine([MarketDataError('transport_error', 'down'),
                              MarketDataError('transport_error', 'down')], settings=failing)
        self.step(engine)
        self.assertEqual(self.step(engine).status, 'FAILED_STOP')
        restarted = self.engine([rising(0)], run_id='run-2')
        self.assertEqual(self.step(restarted).status, 'OK')

    def test_zero_quantity_and_trade_cap_reject_entries(self):
        tiny = replace(self.settings, max_order=Decimal('0.005'), max_exposure=Decimal('10'))
        engine = self.engine([rising(0), rising(1), rising(2)], settings=tiny)
        for _ in range(3):
            self.step(engine)
        self.assertEqual(self.decisions()[-1]['reason'], 'ZERO_QUANTITY')
        self.assertEqual(self.store.orders(BOT), [])

        capped = replace(self.settings, max_trades_per_window=0)
        market_b = descriptor(market_id='1002', start=NEXT, condition='0x' + 'cd' * 32,
                              up='333333', down='444444')
        engine = self.engine([rising(0, start=NEXT, market=market_b),
                              rising(1, start=NEXT, market=market_b),
                              rising(2, start=NEXT, market=market_b)], settings=capped)
        for _ in range(3):
            self.step(engine)
        self.assertEqual(self.decisions('1002')[-1]['reason'], 'WINDOW_TRADE_LIMIT')

    def test_daily_loss_gate_stops_before_collecting(self):
        engine = self.engine([rising(0)])
        with mock.patch.object(self.store, 'realized', return_value=Decimal('-11')):
            result = self.step(engine)
        self.assertEqual(result.status, 'RISK_STOPPED')
        self.assertEqual(result.reason, 'DAILY_LOSS_LIMIT')
        self.assertEqual(self.collector.calls, 0)
        self.assertIn('daily_loss_limit', [e['kind'] for e in self.store.events(BOT)])

    def test_rollover_mark_keeps_old_open_position_explicit(self):
        market_b = descriptor(market_id='1002', start=NEXT, condition='0x' + 'cd' * 32,
                              up='333333', down='444444')
        engine = self.engine([rising(0), rising(1), rising(2), rising(3),
                              rising(0, start=NEXT, market=market_b)])
        for _ in range(5):
            self.step(engine)
        row = self.store.equity_rows(BOT)[-1]
        self.assertGreater(Decimal(row['exposure']), 0)
        self.assertEqual(row['missing_marks'], 1)
        self.assertIsNone(row['unrealized'])

    def test_rollover_never_invents_settlement(self):
        market_b = descriptor(market_id='1002', start=NEXT, condition='0x' + 'cd' * 32,
                              up='333333', down='444444')
        samples = [rising(0), rising(1), rising(0, start=NEXT, market=market_b),
                   rising(1, start=NEXT, market=market_b)]
        engine = self.engine(samples)
        for _ in range(4):
            self.step(engine)
        self.assertEqual(self.store.window(BOT, MARKET_ID)['status'], 'UNKNOWN_SETTLEMENT')
        active = self.store.active_window(BOT)
        self.assertEqual(active['market_id'], '1002')
        self.assertEqual([e['kind'] for e in self.store.events(BOT)].count('rollover'), 1)
        self.assertTrue(all(d['market_id'] == '1002' for d in self.decisions('1002')))

    def test_pending_order_expires_instead_of_filling_stale(self):
        self.store.queue_order('late', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY', Decimal('1'),
                               MARKET_ID + ':0', 'SIGNAL_UP', us(START))
        engine = self.engine([rising(2)])
        result = self.step(engine)
        self.assertEqual(result.status, 'OK')
        order = self.store.order('late')
        self.assertEqual(order['status'], 'REJECTED')
        self.assertEqual(order['reason'], 'EXPIRED_EXECUTION_WINDOW')
        self.assertEqual(order['filled'], Decimal('0'))
        self.assertIn('order_expired', [e['kind'] for e in self.store.events(BOT)])

    def test_restart_mid_window_resumes_without_duplicates(self):
        engine = self.engine([rising(0), rising(1), rising(2)])
        for _ in range(3):
            self.step(engine)
        self.store.close()
        self.store = MvpStore.open(self.base / 'paper.sqlite')
        self.store.start_run('run-2', BOT, initial_cash=Decimal('100'), config={}, started_us=us(START) + 10)
        restarted = self.engine([rising(3)], run_id='run-2')
        self.step(restarted)
        self.assertEqual(len(self.decisions()), 4)
        self.assertEqual(len(self.store.orders(BOT)), 1)
        self.assertEqual(self.store.orders(BOT)[0]['status'], 'FILLED')
        self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP')['quantity'], Decimal('9.99'))

    def test_restart_after_window_end_abandons_window(self):
        engine = self.engine([snapshot(slot=449)])
        self.assertEqual(self.step(engine).status, 'OK')
        market_b = descriptor(market_id='1002', start=NEXT, condition='0x' + 'cd' * 32,
                              up='333333', down='444444')
        later = self.engine([snapshot(slot=450), snapshot(slot=0, start=NEXT, market=market_b)])
        self.assertEqual(self.step(later).status, 'WINDOW_EXPIRED')
        self.assertEqual(self.store.window(BOT, MARKET_ID)['status'], 'ABORTED')
        self.assertEqual(self.step(later).status, 'OK')
        self.assertEqual(self.store.active_window(BOT)['market_id'], '1002')

    def test_equity_flags_missing_marks_instead_of_inventing(self):
        engine = self.engine([rising(0), rising(1), rising(2), rising(3), snapshot(slot=4, up_bid=None)])
        for _ in range(5):
            self.step(engine)
        row = self.store.equity_rows(BOT)[-1]
        self.assertEqual(row['missing_marks'], 1)
        self.assertIsNone(row['unrealized'])
        self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP')['quantity'], Decimal('9.99'))

    def test_holdings_sell_on_holding_period(self):
        falling = [snapshot(slot=n, up_bid=str(Decimal('0.50') - Decimal('0.02') * n),
                            up_ask=str(Decimal('0.52') - Decimal('0.02') * n),
                            down_bid=str(Decimal('0.48') + Decimal('0.02') * n),
                            down_ask=str(Decimal('0.50') + Decimal('0.02') * n)) for n in range(5)]
        engine = self.engine(falling, settings=replace(self.settings, holding_period=1))
        for _ in range(5):
            self.step(engine)
        orders = self.store.orders(BOT)
        self.assertEqual(orders[0]['side'], 'BUY')
        self.assertEqual(orders[0]['outcome'], 'DOWN')
        self.assertEqual(orders[0]['status'], 'FILLED')
        sell_decision = [d for d in self.decisions() if d['action'] == 'SELL']
        self.assertTrue(sell_decision)
        self.assertIn(sell_decision[0]['reason'], ('HOLDING_PERIOD', 'REVERSAL'))


if __name__ == '__main__':
    unittest.main()
