import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from polymarket_bot.linux_mvp.config import Settings
from polymarket_bot.linux_mvp.engine import PaperEngine
from polymarket_bot.linux_mvp.store import MvpStore
from polymarket_bot.market_data.models import BookLevel
from tests.linux_mvp_support import (CONDITION, EVENT_ID, MARKET_ID, START, UP_TOKEN,
                                     Collector, book, rising, snapshot, us)


class ExtraExecutionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'paper.sqlite'
        self.store = MvpStore.open(self.path)
        self.addCleanup(lambda: self.store.close())
        self.store.start_run('r', 'default', initial_cash=Decimal('100'), config={}, started_us=us(START))
        self.settings = Settings.from_env({}, base=Path(self.tmp.name))

    def evidence(self, order_id):
        row = self.store._conn.execute(
            'SELECT evidence_json FROM execution_evidence WHERE order_id=?', (order_id,)).fetchone()
        return None if row is None else json.loads(row[0])

    def engine(self, samples, settings=None, **kwargs):
        return PaperEngine(self.store, Collector(samples), settings or self.settings, run_id='r',
                           now_us=lambda: us(START), **kwargs)

    def assert_linked(self, order, sample, slot):
        evidence = self.evidence(order['order_id'])
        self.assertEqual(evidence['verification'], 'CAPTURED')
        self.assertEqual(evidence['side'], order['side'])
        self.assertEqual(evidence['slot_index'], slot)
        levels = sample.up.book.asks if order['side'] == 'BUY' else sample.up.book.bids
        self.assertEqual(evidence['levels'], [{'price': str(x.price), 'size': str(x.size)} for x in levels])
        self.assertEqual(Decimal(evidence['filled']), order['filled'])
        self.assertEqual(Decimal(evidence['notional']), order['notional'])
        self.assertEqual(Decimal(evidence['fee']), order['fee'])
        self.assertEqual(sum(Decimal(x['quantity']) for x in evidence['consumed']), order['filled'])
        event = next(e for e in self.store.events('default') if e['id'] == evidence['freshness_event_id'])
        self.assertEqual(event['kind'], 'freshness_evaluated')
        self.assertTrue(json.loads(event['detail'])['freshness']['outcomes']['UP']['fresh'])
        return evidence

    def test_engine_full_buy_and_sell_record_opposite_sides(self):
        samples = [rising(n) for n in range(6)]
        engine = self.engine(samples, replace(self.settings, holding_period=1))
        for _ in samples:
            engine.step()
        orders = self.store.orders('default')
        self.assertGreaterEqual(len(orders), 2)
        buy, sell = orders[:2]
        self.assertEqual((buy['side'], buy['status']), ('BUY', 'FILLED'))
        self.assertEqual((sell['side'], sell['status']), ('SELL', 'FILLED'))
        buy_evidence = self.assert_linked(buy, samples[3], 3)
        sell_evidence = self.assert_linked(sell, samples[5], 5)
        self.assertEqual(Decimal(buy_evidence['position_after']), buy['filled'])
        self.assertEqual(Decimal(sell_evidence['position_after']), Decimal('0'))
        self.assertEqual(Decimal(sell_evidence['cash_after']), self.store.cash('default'))

    def test_duplicate_slot_uses_changed_execution_book_and_freshness(self):
        stale = rising(3)
        old = stale.up.book.source_at - timedelta(seconds=10)
        stale = replace(stale, up=replace(stale.up, book=replace(
            stale.up.book, source_at=old, source_timestamp_raw=str(us(old) // 1000))))
        changed = snapshot(slot=3, up_bid='0.40', up_ask='0.42')
        samples = [rising(0), rising(1), rising(2), stale, changed]
        engine = self.engine(samples)
        for _ in range(3):
            engine.step()
        order = self.store.orders('default')[0]
        self.assertEqual(engine.step().reason, 'STALE_PAIR')
        self.assertIsNone(self.evidence(order['order_id']))
        self.assertEqual(engine.step().status, 'DUPLICATE_SLOT')
        order = self.store.order(order['order_id'])
        evidence = self.assert_linked(order, changed, 3)
        self.assertEqual(evidence['levels'][0]['price'], '0.42')
        events = [e for e in self.store.events('default') if e['kind'] == 'freshness_evaluated']
        self.assertEqual(evidence['freshness_event_id'], events[-1]['id'])
        self.assertNotEqual(events[-2]['id'], events[-1]['id'])

    def queue(self, order_id='o'):
        self.store.ensure_window('default', 'r', market_id=MARKET_ID, condition_id=CONDITION,
                                 event_id=EVENT_ID, start_us=us(START),
                                 end_us=us(START) + 900_000_000, at_us=us(START))
        self.store.queue_order(order_id, 'default', 'r', MARKET_ID, UP_TOKEN, 'UP', 'BUY',
                               Decimal('1'), MARKET_ID + ':0', 'SIGNAL', us(START))

    def test_unsorted_duplicate_asks_capture_input_and_sorted_consumption(self):
        self.queue()
        asks = tuple(BookLevel(Decimal(price), Decimal(size)) for price, size in
                     [('0.40', '3'), ('0.30', '0.25'), ('0.35', '2'), ('0.30', '0.50')])
        supplied = replace(book(UP_TOKEN), asks=asks)
        result = self.store.execute_pending('default', {'UP': supplied}, Decimal('0'),
                                            at_us=us(START), depth_captured=True)
        self.assertEqual((result['status'], result['filled'], result['notional']),
                         ('FILLED', Decimal('1'), Decimal('0.3125')))
        evidence = self.evidence('o')
        self.assertEqual(evidence['verification'], 'CAPTURED')
        self.assertEqual(evidence['levels'], [
            {'price': '0.40', 'size': '3'}, {'price': '0.30', 'size': '0.25'},
            {'price': '0.35', 'size': '2'}, {'price': '0.30', 'size': '0.50'}])
        self.assertEqual(evidence['consumed'], [
            {'price': '0.30', 'quantity': '0.25', 'notional': '0.0750', 'fee': None},
            {'price': '0.30', 'quantity': '0.50', 'notional': '0.1500', 'fee': None},
            {'price': '0.35', 'quantity': '0.25', 'notional': '0.0875', 'fee': None}])
        self.assertEqual(Decimal(evidence['filled']), Decimal('1'))
        self.assertEqual(Decimal(evidence['notional']), Decimal('0.3125'))

    def test_distinct_orders_reuse_same_book_without_depletion(self):
        self.queue('first')
        supplied = book(UP_TOKEN, size='1')
        books = {'UP': supplied}
        first = self.store.execute_pending('default', books, Decimal('0'), at_us=us(START),
                                           depth_captured=True)
        self.assertTrue(self.store.queue_order(
            'second', 'default', 'r', MARKET_ID, UP_TOKEN, 'UP', 'BUY', Decimal('1'),
            MARKET_ID + ':1', 'SIGNAL', us(START)))
        second = self.store.execute_pending('default', books, Decimal('0'), at_us=us(START),
                                            depth_captured=True)
        for order_id, result in [('first', first), ('second', second)]:
            self.assertEqual((result['order_id'], result['status'], result['filled'], result['notional']),
                             (order_id, 'FILLED', Decimal('1'), Decimal('0.32')))
            evidence = self.evidence(order_id)
            self.assertEqual(evidence['verification'], 'CAPTURED')
            self.assertEqual(evidence['levels'], [{'price': '0.32', 'size': '1'}])
            self.assertEqual(evidence['consumed'], [
                {'price': '0.32', 'quantity': '1', 'notional': '0.32', 'fee': None}])
            self.assertEqual((Decimal(evidence['filled']), Decimal(evidence['notional'])),
                             (Decimal('1'), Decimal('0.32')))
        self.assertEqual(supplied.asks[0].size, Decimal('1'))
        self.assertEqual(self.store.cash('default'), Decimal('99.36'))

    def test_fee_rejection_records_book_without_fill(self):
        self.queue()
        result = self.store.execute_pending('default', {'UP': book(UP_TOKEN)}, {}, at_us=us(START),
                                            freshness_event_id=7, slot_index=1)
        evidence = self.evidence('o')
        self.assertEqual(result['reason'], 'FEE_UNVERIFIED_OR_CHANGED')
        self.assertEqual(evidence['status'], 'REJECTED')
        self.assertEqual(evidence['levels'], [{'price': '0.32', 'size': '100'}])
        self.assertEqual(evidence['consumed'], [])
        self.assertEqual(Decimal(evidence['filled']), 0)
        self.assertEqual(evidence['cash_before'], evidence['cash_after'])
        self.assertEqual(self.store.cash('default'), Decimal('100'))

    def test_stale_and_late_never_fill(self):
        self.queue()
        stale = rising(1)
        old = stale.up.book.source_at - timedelta(seconds=10)
        stale = replace(stale, up=replace(stale.up, book=replace(
            stale.up.book, source_at=old, source_timestamp_raw=str(us(old) // 1000))))
        engine = self.engine([stale, rising(2)])
        self.assertEqual(engine.step().reason, 'STALE_PAIR')
        self.assertEqual(self.store.order('o')['status'], 'PENDING')
        self.assertIsNone(self.evidence('o'))
        engine.step()
        order = self.store.order('o')
        self.assertEqual(order['reason'], 'EXPIRED_EXECUTION_WINDOW')
        self.assertEqual(order['filled'], Decimal('0'))
        evidence = self.evidence('o')
        self.assertEqual(evidence['verification'], 'UNVERIFIED')
        self.assertEqual(evidence['consumed'], [])
        self.assertEqual(self.store.cash('default'), Decimal('100'))

    def test_reopen_old_database_creates_evidence_table_without_losing_orders(self):
        self.queue()
        self.store._conn.execute('DROP TABLE execution_evidence')
        self.store.close()
        self.store = MvpStore.open(self.path)
        self.assertEqual(self.store.order('o')['status'], 'PENDING')
        self.store.reject_order('o', 'EXPIRED', at_us=us(START))
        self.assertEqual(self.evidence('o')['verification'], 'UNVERIFIED')
