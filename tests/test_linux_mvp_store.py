"""MVP store: schema guardrails, idempotency, and exactly-once fill transactions."""
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from polymarket_bot.linux_mvp.store import SCHEMA, MvpStore
from tests.linux_mvp_support import (CONDITION, DOWN_TOKEN, EVENT_ID, MARKET_ID, START,
                                     UP_TOKEN, book, us)

BOT = 'bot-test'
RUN = 'run-test'
MARKET = dict(market_id=MARKET_ID, condition_id=CONDITION, event_id=EVENT_ID,
              start_us=us(START), end_us=us(START) + 900_000_000)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MvpStore.open(Path(self.tmp.name) / 'paper.sqlite')
        self.addCleanup(self.store.close)
        self.store.start_run(RUN, BOT, initial_cash=Decimal('100'), config={'mode': 'PAPER_ONLY'},
                             started_us=us(START))

    def test_pragmas_and_schema_version(self):
        self.assertEqual(self.store.schema_version, SCHEMA)
        self.assertEqual(self.store.journal_mode, 'wal')
        self.assertEqual(self.store.synchronous, 2)
        tables = set(self.store.table_names())
        for name in ('runs', 'windows', 'observations', 'decisions', 'orders', 'positions',
                     'equity', 'events', 'heartbeat'):
            self.assertIn(name, tables)

    def test_run_and_window_state(self):
        self.assertTrue(self.store.ensure_window(BOT, RUN, **MARKET, at_us=us(START)))
        self.assertFalse(self.store.ensure_window(BOT, RUN, **MARKET, at_us=us(START)))
        active = self.store.active_window(BOT)
        self.assertEqual(active['market_id'], MARKET_ID)
        self.assertEqual(active['status'], 'ACTIVE')
        self.store.close_window(BOT, MARKET_ID, 'UNKNOWN_SETTLEMENT', at_us=us(START) + 900_000_000,
                                note='no settlement market')
        self.assertIsNone(self.store.active_window(BOT))
        self.assertEqual(self.store.window(BOT, MARKET_ID)['status'], 'UNKNOWN_SETTLEMENT')

    def test_observations_and_decisions_are_first_write_wins(self):
        args = dict(bot_id=BOT, run_id=RUN, market_id=MARKET_ID, slot_index=3, outcome='UP',
                    token_id=UP_TOKEN, bid=Decimal('0.30'), ask=Decimal('0.32'),
                    midpoint=Decimal('0.31'), source_at=us(START), received_us=us(START))
        self.assertTrue(self.store.observe(**args))
        args['bid'] = Decimal('0.99')
        self.assertFalse(self.store.observe(**args))
        rows = self.store.observations_for_market(BOT, MARKET_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['bid'], Decimal('0.30'))

        decision = dict(bot_id=BOT, run_id=RUN, market_id=MARKET_ID, slot_index=3,
                        slot_id=MARKET_ID + ':3', action='HOLD', reason='NO_SIGNAL', signal='HOLD',
                        momentum=Decimal('0'), mid_up=Decimal('0.31'), cash=Decimal('100'),
                        exposure=Decimal('0'), inputs={}, at_us=us(START))
        self.assertTrue(self.store.decide(**decision))
        decision['action'] = 'BUY'
        self.assertFalse(self.store.decide(**decision))
        self.assertEqual(self.store.decision(BOT, MARKET_ID, MARKET_ID + ':3')['action'], 'HOLD')
        self.assertTrue(self.store.decide(**{**decision, 'slot_id': MARKET_ID + ':4',
                                             'slot_index': 4}))

    def test_order_ids_dedupe_and_execute_once(self):
        order = dict(order_id='o-1', bot_id=BOT, run_id=RUN, market_id=MARKET_ID,
                     token_id=UP_TOKEN, outcome='UP', side='BUY', quantity=Decimal('9.99'),
                     decision_slot_id=MARKET_ID + ':3', reason='SIGNAL_BUY',
                     at_us=us(START))
        self.assertTrue(self.store.queue_order(**order))
        self.assertFalse(self.store.queue_order(**order))
        self.assertEqual(self.store.order('o-1')['status'], 'PENDING')
        self.assertEqual(self.store.pending_order(BOT)['order_id'], 'o-1')
        result = self.store.execute_pending(BOT, {'UP': book(UP_TOKEN, ask='0.32', size='100')},
                                            fee_bps=Decimal('10'), at_us=us(START))
        self.assertEqual(result['status'], 'FILLED')
        self.assertEqual(self.store.order('o-1')['filled'], Decimal('9.99'))
        self.assertIsNone(self.store.pending_order(BOT))
        again = self.store.execute_pending(BOT, {'UP': book(UP_TOKEN, ask='0.32')},
                                           fee_bps=Decimal('10'), at_us=us(START) + 1)
        self.assertIsNone(again)
        position = self.store.position(BOT, MARKET_ID, 'UP')
        self.assertEqual(position['quantity'], Decimal('9.99'))
        expected_notional = Decimal('9.99') * Decimal('0.32')
        expected_fee = expected_notional * Decimal('10') / Decimal('10000')
        self.assertEqual(result['notional'], expected_notional)
        self.assertEqual(result['fee'], expected_fee)
        self.assertEqual(self.store.cash(BOT), Decimal('100') - expected_notional - expected_fee)
        self.assertEqual(self.store.trade_count(BOT, MARKET_ID), 1)

    def test_partial_fill_is_terminal_and_retry_is_noop(self):
        self.store.queue_order(order_id='o-2', bot_id=BOT, run_id=RUN, market_id=MARKET_ID,
                               token_id=UP_TOKEN, outcome='UP', side='BUY',
                               quantity=Decimal('5'), decision_slot_id='d', reason='SIGNAL_BUY',
                               at_us=us(START))
        thin = book(UP_TOKEN, ask='0.32', size='2')
        result = self.store.execute_pending(BOT, {'UP': thin}, fee_bps=Decimal('0'),
                                            at_us=us(START))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(self.store.order('o-2')['filled'], Decimal('2'))
        self.assertIsNone(self.store.execute_pending(BOT, {'UP': thin}, fee_bps=Decimal('0'),
                                                     at_us=us(START) + 1))

    def test_no_liquidity_rejects_without_position(self):
        self.store.queue_order(order_id='o-3', bot_id=BOT, run_id=RUN, market_id=MARKET_ID,
                               token_id=UP_TOKEN, outcome='UP', side='BUY',
                               quantity=Decimal('1'), decision_slot_id='d', reason='SIGNAL_BUY',
                               at_us=us(START))
        empty = book(UP_TOKEN, bid=None, ask=None)
        result = self.store.execute_pending(BOT, {'UP': empty}, fee_bps=Decimal('10'),
                                            at_us=us(START))
        self.assertEqual(result['status'], 'REJECTED')
        self.assertEqual(result['reason'], 'INSUFFICIENT_LIQUIDITY')
        self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP')['quantity'], Decimal('0'))
        self.assertEqual(self.store.cash(BOT), Decimal('100'))

    def test_sell_realizes_pnl_and_reduces_basis(self):
        for order_id, side, quantity in (('b', 'BUY', '10'), ('s', 'SELL', '10')):
            self.store.queue_order(order_id=order_id, bot_id=BOT, run_id=RUN, market_id=MARKET_ID,
                                   token_id=UP_TOKEN, outcome='UP', side=side,
                                   quantity=Decimal(quantity), decision_slot_id='d',
                                   reason='SIGNAL', at_us=us(START))
        self.store.execute_pending(BOT, {'UP': book(UP_TOKEN, ask='0.40')}, fee_bps=Decimal('0'),
                                   at_us=us(START))
        sold = self.store.execute_pending(BOT, {'UP': book(UP_TOKEN, bid='0.55')},
                                          fee_bps=Decimal('0'), at_us=us(START) + 1)
        self.assertEqual(sold['status'], 'FILLED')
        self.assertEqual(sold['realized'], Decimal('1.50'))
        self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP')['quantity'], Decimal('0'))
        self.assertEqual(self.store.realized(BOT), Decimal('1.50'))
        self.assertEqual(self.store.cash(BOT), Decimal('101.50'))

    def test_equity_heartbeat_and_events_are_append_only(self):
        self.store.equity(BOT, RUN, at_us=us(START), market_id=MARKET_ID, cash=Decimal('98'),
                          exposure=Decimal('2'), realized=Decimal('0'),
                          unrealized=Decimal('-0.1'), missing_marks=1)
        self.store.heartbeat(BOT, RUN, at_us=us(START), market_id=MARKET_ID, status='RUNNING',
                             cycles=7, last_error=None)
        self.store.event(BOT, RUN, at_us=us(START), kind='rollover', detail='A->B')
        self.assertEqual(self.store.heartbeat_row(BOT)['cycles'], 7)
        self.assertEqual(self.store.equity_rows(BOT)[0]['missing_marks'], 1)
        self.assertEqual([e['kind'] for e in self.store.events(BOT)], ['rollover'])

    def test_schema_refuses_non_paper_runs_and_bad_status(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.start_run('r-bad', BOT, initial_cash=Decimal('100'),
                                 config={}, started_us=us(START), mode='LIVE')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.close_window(BOT, MARKET_ID, 'SETTLED', at_us=us(START), note='')

    def test_reopen_keeps_state_and_lock_is_optional(self):
        self.store.ensure_window(BOT, RUN, **MARKET, at_us=us(START))
        path = self.store.path
        self.store.close()
        again = MvpStore.open(path)
        self.addCleanup(again.close)
        self.assertEqual(again.active_window(BOT)['market_id'], MARKET_ID)


if __name__ == '__main__':
    unittest.main()
