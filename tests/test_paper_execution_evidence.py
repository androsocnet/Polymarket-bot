import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from dataclasses import replace
from pathlib import Path

from polymarket_bot.linux_mvp.store import MvpStore
from polymarket_bot.linux_mvp.engine import PaperEngine
from polymarket_bot.linux_mvp.config import Settings
from tests.linux_mvp_support import CONDITION, EVENT_ID, MARKET_ID, START, UP_TOKEN, book, us


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MvpStore.open(Path(self.tmp.name) / 'db.sqlite')
        self.addCleanup(self.store.close)
        self.store.start_run('r', 'b', initial_cash=Decimal('100'), config={}, started_us=us(START))
        self.store.ensure_window('b', 'r', market_id=MARKET_ID, condition_id=CONDITION,
                                 event_id=EVENT_ID, start_us=us(START),
                                 end_us=us(START) + 900000000, at_us=us(START))
        self.store.queue_order(order_id='o', bot_id='b', run_id='r', market_id=MARKET_ID,
                               token_id=UP_TOKEN, outcome='UP', side='BUY', quantity=Decimal('5'),
                               decision_slot_id=MARKET_ID + ':1', reason='SIGNAL', at_us=us(START))

    def evidence(self):
        return json.loads(self.store._conn.execute(
            'SELECT evidence_json FROM execution_evidence WHERE order_id=?', ('o',)).fetchone()[0])

    def test_partial_records_exact_book_and_accounting(self):
        result = self.store.execute_pending('b', {'UP': book(UP_TOKEN, ask='0.32', size='2')},
                                            Decimal('10'), at_us=us(START),
                                            freshness_event_id=7, slot_index=2)
        evidence = self.evidence()
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(evidence['levels'], [{'price': '0.32', 'size': '2'}])
        self.assertEqual(evidence['consumed'][0]['quantity'], '2')
        self.assertEqual(evidence['freshness_event_id'], 7)
        self.assertEqual(evidence['slot_index'], 2)
        self.assertEqual(evidence['verification'], 'UNVERIFIED')
        self.assertIsNone(evidence['consumed'][0]['fee'])
        self.assertEqual(Decimal(evidence['cash_after']), self.store.cash('b'))
        self.assertEqual(Decimal(evidence['fee']), result['fee'])

    def test_absent_book_and_direct_rejection_are_unverified(self):
        self.store.reject_order('o', 'EXPIRED', at_us=us(START))
        self.assertEqual(self.evidence()['verification'], 'UNVERIFIED')
        self.assertIsNone(self.evidence()['levels'])

    def test_engine_depth_capture_does_not_claim_liquidity_verification(self):
        engine = PaperEngine(self.store, None, replace(Settings.from_env({}, base=Path(self.tmp.name)), bot_id='b'), run_id='r')
        engine.freshness_event_id = 123
        engine._execute_pending(MARKET_ID, 2, {'UP': book(UP_TOKEN)}, us(START), Decimal('0'))
        self.assertEqual(self.evidence()['verification'], 'CAPTURED')

    def test_rejection_does_not_link_other_market_freshness(self):
        engine = PaperEngine(self.store, None, replace(Settings.from_env({}, base=Path(self.tmp.name)), bot_id='b'), run_id='r')
        engine.freshness_event_id = 123
        engine._execute_pending('another-market', 2, {}, us(START), {})
        self.assertIsNone(self.evidence()['freshness_event_id'])
        self.assertIsNone(self.evidence()['slot_index'])

    def test_expired_order_links_current_slot(self):
        engine = PaperEngine(self.store, None, replace(Settings.from_env({}, base=Path(self.tmp.name)), bot_id='b'), run_id='r')
        engine.freshness_event_id = 123
        engine._execute_pending(MARKET_ID, 3, {}, us(START), {})
        self.assertEqual(self.evidence()['slot_index'], 3)
        self.assertEqual(self.evidence()['freshness_event_id'], 123)

    def test_step_clears_prior_freshness_before_early_stop(self):
        engine = PaperEngine(self.store, None, replace(Settings.from_env({}, base=Path(self.tmp.name)), bot_id='b'), run_id='r')
        engine.freshness_event_id = 123
        engine.settings.stop_path.parent.mkdir(parents=True, exist_ok=True)
        engine.settings.stop_path.touch()
        engine.step()
        self.assertIsNone(engine.freshness_event_id)

    def test_nested_reject_failure_rolls_back_even_when_caught(self):
        self.store._conn.execute("CREATE TRIGGER fail_evidence BEFORE INSERT ON execution_evidence BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with self.store.transaction():
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.reject_order('o', 'EXPIRED', at_us=us(START))
        self.assertEqual(self.store.order('o')['status'], 'PENDING')

    def test_flat_fee_evidence_keeps_supplied_precision(self):
        self.store.execute_pending('b', {'UP': book(UP_TOKEN)}, Decimal('10.00'), at_us=us(START))
        self.assertEqual(self.evidence()['fee_bps'], '10.00')

    def test_rejected_market_fee_evidence_keeps_supplied_dict(self):
        supplied = {'status': 'UNVERIFIED', 'nested': {'rate': Decimal('0.0700')}}
        result = self.store.execute_pending('b', {'UP': book(UP_TOKEN)}, supplied, at_us=us(START))
        self.assertEqual(result['reason'], 'FEE_UNVERIFIED_OR_CHANGED')
        self.assertEqual(self.evidence()['fee_bps'], {'status': 'UNVERIFIED', 'nested': {'rate': '0.0700'}})

    def test_fee_evidence_update_failure_rolls_back_rejection(self):
        self.store._conn.execute('INSERT INTO order_fees(order_id, evidence_json) VALUES(?,?)', ('o', '{}'))
        self.store._conn.execute("CREATE TRIGGER fail_fee_update BEFORE UPDATE ON order_fees BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.execute_pending('b', {'UP': book(UP_TOKEN)}, {'status': 'UNVERIFIED'}, at_us=us(START))
        self.assertEqual(self.store.order('o')['status'], 'PENDING')
        self.assertEqual(self.store.cash('b'), Decimal('100'))
        self.assertIsNone(self.store._conn.execute('SELECT 1 FROM execution_evidence WHERE order_id=?', ('o',)).fetchone())
        self.assertIsNone(self.store._conn.execute('SELECT execution_evidence_json FROM order_fees WHERE order_id=?', ('o',)).fetchone()[0])

    def test_evidence_insert_failure_rolls_back_fill(self):
        self.store._conn.execute("CREATE TRIGGER fail_evidence BEFORE INSERT ON execution_evidence BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.execute_pending('b', {'UP': book(UP_TOKEN)}, Decimal('0'), at_us=us(START))
        self.assertEqual(self.store.order('o')['status'], 'PENDING')
        self.assertEqual(self.store.cash('b'), Decimal('100'))
