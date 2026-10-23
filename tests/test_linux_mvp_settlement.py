"""Synthetic authoritative-resolution evidence; no network or inferred BTC prices."""
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import httpx

from polymarket_bot.linux_mvp.collector import CollectionStopped, LiveCollector
from polymarket_bot.linux_mvp.config import Settings
from polymarket_bot.linux_mvp.engine import PaperEngine
from polymarket_bot.linux_mvp.report import build_report
from polymarket_bot.linux_mvp.store import MvpStore
from tests.linux_mvp_support import (CONDITION, MARKET_ID, START, Collector, descriptor,
                                     rising, snapshot, us)

BOT, RUN = 'default', 'settlement-run'
END = us(START) + 900_000_000
NOW = END + 120_000_000


def evidence(*, winner='UP', reversed_order=False, market_id=MARKET_ID, condition=CONDITION):
    labels = ['Down', 'Up'] if reversed_order else ['Up', 'Down']
    tokens = ['222222', '111111'] if reversed_order else ['111111', '222222']
    payouts = [1000000 if label.upper() == winner else 0 for label in labels]
    gamma = dict(id=market_id, conditionId=condition, outcomes=json.dumps(labels),
                 clobTokenIds=json.dumps(tokens), closed=True, umaResolutionStatus='resolved',
                 slug='btc-updown-15m-' + str(int(START.timestamp())),
                 endDate=(START + timedelta(minutes=15)).isoformat(),
                 outcomePrices=json.dumps(['1' if n else '0' for n in payouts]))
    row = dict(condition_id=condition, status='resolved', payouts=payouts,
               resolved_at=(START + timedelta(minutes=16)).isoformat(),
               transaction_hash='0x' + '12' * 32, resolved_block=123456,
               extended_review=False, was_disputed=False, new_version_q=False)
    return dict(gamma_raw=json.dumps(gamma), resolution_raw=json.dumps({'data': [row]}))


def mutate(bundle, source, **changes):
    result = deepcopy(bundle)
    data = json.loads(result[source + '_raw'])
    (data if source == 'gamma' else data['data'][0]).update(changes)
    result[source + '_raw'] = json.dumps(data)
    return result


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'paper.sqlite'
        self.store = MvpStore.open(self.path)
        self.addCleanup(lambda: self.store.close())
        self.store.start_run(RUN, BOT, initial_cash=Decimal('100'), config={}, started_us=us(START))
        self.settings = Settings.from_env({}, base=Path(self.tmp.name))
        self.store.ensure_window(BOT, RUN, market_id=MARKET_ID, condition_id=CONDITION,
                                 event_id='2001', start_us=us(START), end_us=END, at_us=us(START))
        for outcome, token in [('UP', '111111'), ('DOWN', '222222')]:
            self.store.observe(BOT, RUN, MARKET_ID, 0, outcome, token, Decimal('.4'),
                               Decimal('.5'), Decimal('.45'), us(START), us(START))

    def buy(self, outcome='UP', price='.50', quantity='10'):
        token = '111111' if outcome == 'UP' else '222222'
        self.store.queue_order('buy-' + outcome, BOT, RUN, MARKET_ID, token, outcome, 'BUY',
                               Decimal(quantity), MARKET_ID + ':1', 'TEST', us(START))
        book = snapshot(slot=2, up_ask=price, down_ask=price)
        return self.store.execute_pending(BOT, {outcome: getattr(book, outcome.lower()).book},
                                          Decimal('10'), at_us=us(START) + 4_000_000)

    def close_window(self):
        self.store.close_window(BOT, MARKET_ID, 'UNKNOWN_SETTLEMENT', note='expired', at_us=END)

    def settle(self, bundle=None):
        return self.store.settle_window(BOT, MARKET_ID, evidence=bundle or evidence(), at_us=NOW)

    def test_directional_payouts_and_fees_in_both_outcome_orders(self):
        self.buy('UP')
        self.close_window()
        for bundle in [evidence(), evidence(reversed_order=True)]:
            with self.subTest(reversed_order=bundle):
                result = self.settle(bundle)
                self.assertEqual(result['payout_total'], Decimal('10'))
                self.assertEqual(result['realized_pnl'], Decimal('4.995'))
        self.assertEqual(self.store.cash(BOT), Decimal('104.995'))
        self.assertEqual(self.store.open_positions(BOT), [])

    def test_fill_reconciliation_failure_rolls_back(self):
        with mock.patch.object(self.store, 'reconcile', side_effect=sqlite3.IntegrityError('mismatch')):
            with self.assertRaises(sqlite3.IntegrityError):
                self.buy()
        self.assertEqual(self.store.cash(BOT), Decimal('100'))
        self.assertEqual(self.store.open_positions(BOT), [])
        self.assertEqual(self.store.pending_order(BOT)['status'], 'PENDING')

    def test_stop_arriving_with_final_snapshot_blocks_pending_fill(self):
        self.store.queue_order('pending', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('10'), MARKET_ID + ':1', 'TEST', us(START))
        collector = Collector([])
        def collect():
            self.settings.stop_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings.stop_path.touch()
            return rising(2)
        collector.collect = collect
        engine = PaperEngine(self.store, collector, self.settings, run_id=RUN, now_us=lambda: us(START))
        self.assertEqual(engine.step().status, 'STOPPED')
        self.assertEqual(self.store.pending_order(BOT)['status'], 'PENDING')

    def test_changed_condition_never_fills_pending_order(self):
        self.store.queue_order('pending', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('10'), MARKET_ID + ':1', 'TEST', us(START))
        changed = descriptor(condition='0x' + 'cd' * 32, up='333333', down='444444')
        engine = PaperEngine(self.store, Collector([rising(2, market=changed)]), self.settings,
                             run_id=RUN, now_us=lambda: us(START))
        self.assertEqual(engine.step().status, 'FAILED_STOP')
        self.assertEqual(self.store.open_positions(BOT), [])

    def test_daily_realized_uses_fill_time_not_creation(self):
        self.buy(quantity='30')
        midnight = (END // 86_400_000_000 + 1) * 86_400_000_000
        self.store.queue_order('sell', BOT, RUN, MARKET_ID, '111111', 'UP', 'SELL',
                               Decimal('30'), MARKET_ID + ':2', 'TEST', midnight - 1)
        self.store.execute_pending(BOT, {'UP': snapshot(slot=3, up_bid='.01').up.book},
                                   Decimal('10'), at_us=midnight + 1)
        self.assertLess(self.store.realized(BOT, since_us=midnight), Decimal('-10'))

    def test_sell_loss_stops_before_new_decision_same_cycle(self):
        self.buy(quantity='30')
        self.store.queue_order('sell', BOT, RUN, MARKET_ID, '111111', 'UP', 'SELL',
                               Decimal('30'), MARKET_ID + ':1', 'TEST', us(START))
        engine = PaperEngine(self.store, Collector([snapshot(slot=2, up_bid='.01')]), self.settings,
                             run_id=RUN, now_us=lambda: us(START))
        self.assertEqual(engine.step().status, 'RISK_STOPPED')
        self.assertIsNone(self.store.pending_order(BOT))
        self.assertEqual(self.store.decisions_for_market(BOT, MARKET_ID), [])

    def test_future_mark_is_not_current_equity(self):
        self.buy()
        self.store.equity(BOT, RUN, at_us=NOW, market_id=MARKET_ID,
                          cash=Decimal('94.995'), exposure=Decimal('5.005'), realized=Decimal(0),
                          unrealized=Decimal('1'), missing_marks=0)
        report = build_report(self.path, now_us=NOW - 86_400_000_000)
        self.assertIsNone(report['account']['net_value'])
        self.assertIsNone(report['account']['unrealized'])

    def test_closed_holding_remains_unresolved(self):
        self.buy()
        self.store._conn.execute("UPDATE windows SET status='CLOSED' WHERE bot_id=? AND market_id=?", (BOT, MARKET_ID))
        report = build_report(self.path, now_us=NOW)
        self.assertEqual(report['positions']['open'], 1)
        self.assertEqual(report['positions']['unresolved'], 1)
        self.assertEqual(Decimal(report['account']['exposure']), Decimal('5.005'))

    def test_old_mark_is_not_current_equity(self):
        self.buy()
        self.store.equity(BOT, RUN, at_us=us(START), market_id=MARKET_ID,
                          cash=self.store.cash(BOT), exposure=Decimal('5.005'), realized=Decimal(0),
                          unrealized=Decimal('1'), missing_marks=0)
        report = build_report(self.path, now_us=NOW)
        self.assertIsNone(report['account']['net_value'])
        self.assertFalse(report['performance']['max_drawdown_complete'])
        self.assertEqual(report['equity']['mark_at_us'], us(START))

    def test_long_decimal_cash_does_not_depend_on_ambient_precision(self):
        from decimal import localcontext
        self.buy(price='.333333333333333333333333333333', quantity='3')
        with localcontext() as context:
            context.prec = 512
            expected = Decimal('100') - Decimal('.333333333333333333333333333333') * 3 * Decimal('1.001')
        self.assertEqual(self.store.cash(BOT), expected)
        report = build_report(self.path, now_us=NOW)
        self.assertEqual(Decimal(report['account']['cash']), expected)
        self.assertTrue(report['account']['reconciled'])

    def test_legacy_settlement_report_preserves_payout(self):
        self.buy()
        self.close_window()
        self.settle()
        self.store._conn.execute('ALTER TABLE settlements DROP COLUMN position_count')
        report = build_report(self.path, now_us=NOW)
        self.assertEqual(Decimal(report['account']['cash']), Decimal('104.995'))
        self.assertEqual(Decimal(report['account']['realized']), Decimal('4.995'))
        self.assertIsNone(report['performance']['verified_settled_trades'])

    def test_execution_boundary_rejects_wrong_book_token(self):
        from dataclasses import replace
        self.store.queue_order('pending', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('10'), MARKET_ID + ':1', 'TEST', us(START))
        wrong_book = replace(snapshot(slot=2).up.book, token_id='999999')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.execute_pending(BOT, {'UP': wrong_book}, Decimal('10'), at_us=us(START))
        self.assertEqual(self.store.open_positions(BOT), [])

    def test_resolution_crossing_midnight_records_loss_on_completion_day(self):
        self.buy(quantity='30')
        self.close_window()
        midnight = (NOW // 86_400_000_000 + 1) * 86_400_000_000
        current = [midnight - 1]
        collector = Collector([])
        def resolve(window):
            current[0] = midnight + 1_000_000
            return evidence(winner='DOWN')
        collector.get_resolution = resolve
        engine = PaperEngine(self.store, collector, self.settings, run_id=RUN, now_us=lambda: current[0])
        self.assertEqual(engine.step().status, 'RISK_STOPPED')
        self.assertEqual(self.store.settlements(BOT)[0]['settled_us'], current[0])
        self.assertLess(self.store.realized(BOT, since_us=midnight), Decimal('-10'))
        self.assertEqual(engine.step().status, 'RISK_STOPPED')
        self.assertEqual(collector.calls, 0)

    def test_partial_sale_mark_uses_exact_remaining_basis(self):
        from dataclasses import replace
        from polymarket_bot.market_data.models import BookLevel
        self.store.queue_order('buy', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('3'), MARKET_ID + ':1', 'TEST', us(START))
        book = replace(snapshot(slot=2).up.book, asks=(BookLevel(Decimal('.50'), Decimal('1')),
                                                     BookLevel(Decimal('.60'), Decimal('2'))))
        self.store.execute_pending(BOT, {'UP': book}, Decimal('10'), at_us=us(START))
        self.store.queue_order('sell', BOT, RUN, MARKET_ID, '111111', 'UP', 'SELL',
                               Decimal('2'), MARKET_ID + ':2', 'TEST', us(START))
        book = replace(book, bids=(BookLevel(Decimal('.40'), Decimal('1')),))
        result = self.store.execute_pending(BOT, {'UP': book}, Decimal('10'), at_us=us(START))
        self.assertEqual(result['status'], 'PARTIAL')
        engine = PaperEngine(self.store, Collector([]), self.settings, run_id=RUN, now_us=lambda: us(START))
        engine._mark(MARKET_ID, {'UP': book}, us(START))
        report = build_report(self.path, now_us=us(START))
        self.assertTrue(report['account']['reconciled'])
        self.assertIsNotNone(report['account']['net_value'])
        self.assertEqual(Decimal(report['account']['exposure']), self.store.position(BOT, MARKET_ID, 'UP')['cost_basis'])

    def test_legacy_count_stays_unknown_after_migration(self):
        self.buy()
        self.close_window()
        self.settle()
        self.store._conn.execute('ALTER TABLE settlements DROP COLUMN position_count')
        self.store.close()
        self.store = MvpStore.open(self.path)
        report = build_report(self.path, now_us=NOW)
        self.assertEqual(Decimal(report['account']['cash']), Decimal('104.995'))
        self.assertIsNone(report['performance']['verified_settled_trades'])

    def test_order_terminal_event_records_signal_latency(self):
        self.store.queue_order('pending', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('10'), MARKET_ID + ':1', 'TEST', us(START))
        engine = PaperEngine(self.store, Collector([rising(2)]), self.settings,
                             run_id=RUN, now_us=lambda: us(START) + 4_500_000)
        engine.step()
        row = next(event for event in self.store.events(BOT) if event['kind'] == 'order_executed')
        detail = json.loads(row['detail'])
        self.assertEqual(detail['signal_to_terminal_us'], 4_500_000)
        self.assertEqual(detail['latency_kind'], 'PAPER_DECISION_TO_TERMINAL_WALL_CLOCK')

    def test_reversed_order_pays_up_not_down(self):
        self.buy('UP')
        self.close_window()
        self.settle(evidence(reversed_order=True))
        self.assertEqual(self.store.cash(BOT), Decimal('104.995'))
        self.assertEqual(self.store.settlements(BOT)[0]['winning_outcome'], 'UP')

    def test_retry_backoff_survives_restart(self):
        self.close_window()
        collector = Collector([], resolutions={})
        collector.get_resolution = mock.Mock(return_value={})
        engine = PaperEngine(self.store, collector, self.settings, run_id=RUN, now_us=lambda: NOW)
        engine.step()
        self.store.close()
        self.store = MvpStore.open(self.path)
        restarted = PaperEngine(self.store, collector, self.settings, run_id=RUN, now_us=lambda: NOW + 1)
        restarted.step()
        self.assertEqual(collector.get_resolution.call_count, 1)
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM resolution_attempts').fetchone()[0], 1)
        collector.get_resolution.return_value = evidence()
        restarted.now_us = lambda: NOW + 11_000_000
        restarted.step()
        self.assertEqual(self.store.window(BOT, MARKET_ID)['status'], 'SETTLED')

    def test_report_does_not_reuse_mark_after_unmarked_fill(self):
        self.store.equity(BOT, RUN, at_us=us(START), market_id=MARKET_ID,
                          cash=Decimal('100'), exposure=Decimal(0), realized=Decimal(0),
                          unrealized=Decimal(0), missing_marks=0)
        self.buy()
        report = build_report(self.path, now_us=NOW)
        self.assertEqual(Decimal(report['account']['cash']), Decimal('94.995'))
        self.assertIsNone(report['account']['net_value'])
        self.assertTrue(report['account']['reconciled'])

    def test_maximum_drawdown_survives_recovery(self):
        for index, cash in enumerate(['110', '90', '105']):
            self.store.equity(BOT, RUN, at_us=NOW + index, market_id=MARKET_ID,
                              cash=Decimal(cash), exposure=Decimal(0), realized=Decimal(0),
                              unrealized=Decimal(0), missing_marks=0)
        report = build_report(self.path, now_us=NOW + 3)
        self.assertEqual(Decimal(report['performance']['max_drawdown']), Decimal('20'))

    def test_down_win_and_up_loss(self):
        self.buy('DOWN', '.4')
        self.close_window()
        self.settle(evidence(winner='DOWN', reversed_order=True))
        self.assertEqual(self.store.cash(BOT), Decimal('105.996'))
        self.assertEqual(self.store.realized(BOT), Decimal('5.996'))

    def test_loss_releases_basis_only_with_verified_evidence(self):
        self.buy()
        self.close_window()
        self.settle(evidence(winner='DOWN'))
        self.assertEqual(self.store.cash(BOT), Decimal('94.995'))
        self.assertEqual(self.store.realized(BOT), Decimal('-5.005'))
        self.assertEqual(self.store.open_positions(BOT), [])

    def test_unverified_evidence_never_changes_cash_or_position(self):
        self.buy()
        self.close_window()
        good = evidence()
        invalid = [
            {}, {'gamma_raw': '', 'resolution_raw': ''},
            mutate(good, 'gamma', id='1002'),
            mutate(good, 'gamma', conditionId='0x' + '34' * 32),
            mutate(good, 'gamma', clobTokenIds='["222222", "111111"]'),
            mutate(good, 'gamma', outcomes='["Up", "Up"]'),
            mutate(good, 'gamma', closed=False),
            mutate(good, 'gamma', umaResolutionStatus='proposed'),
            mutate(good, 'gamma', outcomePrices='["0", "1"]'),
            mutate(good, 'gamma', endDate=START.isoformat()),
            mutate(good, 'resolution', condition_id='0x' + '34' * 32),
            mutate(good, 'resolution', status='proposed'),
            mutate(good, 'resolution', payouts=[500000, 500000]),
            mutate(good, 'resolution', payouts=[2, 0]),
            mutate(good, 'resolution', payouts=['NaN', 0]),
            mutate(good, 'resolution', payouts=['Infinity', 0]),
            mutate(good, 'resolution', payouts=[True, 0]),
            mutate(good, 'resolution', extended_review=True),
            mutate(good, 'resolution', new_version_q=True),
            mutate(good, 'resolution', new_version_q=None),
            mutate(good, 'resolution', resolved_block=None),
            mutate(good, 'resolution', transaction_hash='0xshort'),
            mutate(good, 'resolution', resolved_at=START.isoformat()),
            mutate(good, 'resolution', resolved_at='2099-01-01T00:00:00Z'),
            {**good, 'resolution_raw': '{"data":[]}'},
            {**good, 'resolution_raw': json.dumps({'data': json.loads(good['resolution_raw'])['data'] * 2})},
        ]
        for bundle in invalid:
            with self.subTest(bundle=bundle):
                result = self.store.settle_window(BOT, MARKET_ID, evidence=bundle, at_us=NOW)
                self.assertEqual(result['status'], 'UNKNOWN_SETTLEMENT')
                self.assertEqual(self.store.cash(BOT), Decimal('94.995'))
                self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP')['quantity'], Decimal('10'))
        self.assertEqual(self.store.settlements(BOT), [])

    def test_missing_token_identity_cannot_settle(self):
        self.buy()
        self.close_window()
        self.store._conn.execute("DELETE FROM observations WHERE outcome='DOWN'")
        self.assertEqual(self.settle()['status'], 'UNKNOWN_SETTLEMENT')

    def test_active_window_cannot_settle(self):
        self.buy()
        self.assertEqual(self.settle()['status'], 'UNKNOWN_SETTLEMENT')

    def test_repeated_settlement_survives_reopen_without_second_credit(self):
        self.buy()
        self.close_window()
        self.settle()
        self.store.close()
        self.store = MvpStore.open(self.path)
        self.assertTrue(self.settle()['already_settled'])
        self.assertEqual(self.store.cash(BOT), Decimal('104.995'))
        self.assertEqual(len(self.store.settlements(BOT)), 1)

    def test_settlement_interruption_rolls_back_all_mutations(self):
        self.buy()
        self.close_window()
        self.store._conn.execute("CREATE TRIGGER fail_settlement BEFORE INSERT ON settlements "
                                 "BEGIN SELECT RAISE(ABORT, 'simulated crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.settle()
        self.assertEqual(self.store.cash(BOT), Decimal('94.995'))
        self.assertEqual(self.store.window(BOT, MARKET_ID)['status'], 'UNKNOWN_SETTLEMENT')
        self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP')['quantity'], Decimal('10'))
        self.store._conn.execute('DROP TRIGGER fail_settlement')
        self.settle()
        self.assertEqual(self.store.cash(BOT), Decimal('104.995'))

    def test_report_immediately_reflects_settlement_and_keeps_evidence(self):
        self.buy()
        self.close_window()
        self.store.equity(BOT, RUN, at_us=END, market_id=MARKET_ID, cash=Decimal('94.995'),
                           exposure=Decimal('5.005'), realized=Decimal(0), unrealized=None, missing_marks=1)
        self.settle()
        report = build_report(self.path, now_us=NOW)
        self.assertEqual(Decimal(report['account']['cash']), Decimal('104.995'))
        self.assertEqual(Decimal(report['account']['exposure']), Decimal(0))
        self.assertEqual(Decimal(report['account']['net_value']), Decimal('104.995'))
        self.assertEqual(report['performance']['verified_wins'], 1)
        self.assertEqual(report['performance']['verified_losses'], 0)
        saved = self.store.settlements(BOT)[0]
        self.assertIn('gamma_raw', saved['evidence_json'])

    def test_zero_position_resolution_not_counted_as_trade(self):
        self.close_window()
        self.settle()
        report = build_report(self.path, now_us=NOW)
        self.assertEqual(report['performance']['verified_settled_trades'], 0)

    def test_engine_resumes_after_verified_settlement_and_persists_backoff(self):
        self.buy()
        self.close_window()
        market = descriptor(market_id='1002', start=START + timedelta(minutes=15),
                            condition='0x' + 'cd' * 32, up='333333', down='444444')
        collector = Collector([rising(n, start=market.window_start, market=market) for n in range(3)])
        collector.resolutions = {CONDITION: evidence()}
        eng = PaperEngine(self.store, collector, self.settings, run_id=RUN, now_us=lambda: NOW)
        for _ in range(3):
            eng.step()
        self.assertEqual(self.store.window(BOT, MARKET_ID)['status'], 'SETTLED')
        self.assertEqual(self.store.decisions_for_market(BOT, '1002')[-1]['action'], 'BUY UP')

    def test_settlement_loss_triggers_risk_before_collection(self):
        self.buy(quantity='30')
        self.close_window()
        collector = Collector([rising(0)], resolutions={CONDITION: evidence(winner='DOWN')})
        eng = PaperEngine(self.store, collector, self.settings, run_id=RUN, now_us=lambda: NOW)
        result = eng.step()
        self.assertEqual(result.status, 'RISK_STOPPED')
        self.assertEqual(collector.calls, 0)

    def test_stop_propagates_during_resolution(self):
        self.close_window()
        collector = Collector([])
        collector.get_resolution = mock.Mock(side_effect=CollectionStopped('stop'))
        eng = PaperEngine(self.store, collector, self.settings, run_id=RUN, now_us=lambda: NOW)
        with self.assertRaises(CollectionStopped):
            eng.step()

    def test_process_kill_rolls_back_and_releases_lock_with_unknown_position(self):
        import subprocess
        import sys
        import time
        from polymarket_bot.linux_mvp.service import InstanceLockError, single_instance
        self.buy()
        self.close_window()
        ready = Path(self.tmp.name) / 'child-ready'
        code = '''
import sys,time
from pathlib import Path
from polymarket_bot.linux_mvp.store import MvpStore
from polymarket_bot.linux_mvp.service import single_instance
with single_instance(Path(sys.argv[2])):
    store=MvpStore.open(Path(sys.argv[1]))
    with store.transaction() as conn:
        conn.execute("UPDATE positions SET quantity='0',cost_basis='0'")
        Path(sys.argv[3]).touch()
        time.sleep(30)
'''
        child = subprocess.Popen([sys.executable, '-c', code, str(self.path), str(self.settings.state_dir), str(ready)])
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists(), 'child did not reach uncommitted mutation')
            with self.assertRaises(InstanceLockError):
                with single_instance(self.settings.state_dir):
                    pass
            child.kill()
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        # Windows can signal TerminateProcess before kernel file-lock cleanup finishes.
        # Bound reacquisition; persistent lock or orphan still fails recovery check.
        from contextlib import ExitStack
        with ExitStack() as stack:
            deadline = time.monotonic() + 2
            while True:
                try:
                    stack.enter_context(single_instance(self.settings.state_dir))
                    break
                except InstanceLockError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.01)
            self.store.close()
            self.store = MvpStore.open(self.path)
            self.store.reconcile(BOT)
            self.assertEqual(self.store._conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(self.store.window(BOT, MARKET_ID)['status'], 'UNKNOWN_SETTLEMENT')
            self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP')['quantity'], Decimal('10'))
            self.assertEqual(self.store.cash(BOT), Decimal('94.995'))
            self.assertEqual(self.store.settle_window(BOT, MARKET_ID, evidence={}, at_us=NOW)['status'], 'UNKNOWN_SETTLEMENT')
            self.assertEqual(self.store.cash(BOT), Decimal('94.995'))
            self.settle()
            self.assertEqual(self.store.cash(BOT), Decimal('104.995'))
            self.assertTrue(self.settle()['already_settled'])
            self.store.reconcile(BOT)

    def test_old_schema_migration_preserves_window_and_reopens(self):
        path = Path(self.tmp.name) / 'old.sqlite'
        from polymarket_bot.linux_mvp.store import _SCHEMA_SQL
        old = _SCHEMA_SQL.replace(", 'SETTLED'", '').replace(",'SETTLED'", '')
        with closing(sqlite3.connect(path, isolation_level=None)) as conn:
            conn.executescript(old)
            conn.execute("INSERT INTO windows(bot_id,market_id,run_id,condition_id,event_id,start_us,end_us,"
                         "status,note,updated_us) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (BOT, MARKET_ID, RUN, CONDITION, '2001', us(START), END, 'UNKNOWN_SETTLEMENT', '', END))
        migrated = MvpStore.open(path)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.window(BOT, MARKET_ID)['condition_id'], CONDITION)
        self.assertEqual(migrated._conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_live_collector_uses_exact_read_only_market_and_condition_routes(self):
        bundle = evidence()
        requests = []
        def respond(request):
            requests.append((request.method, request.url.path, request.url.query))
            raw = bundle['gamma_raw' if request.url.host.startswith('gamma') else 'resolution_raw']
            return httpx.Response(200, stream=httpx.ByteStream(raw.encode()))
        with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
            with LiveCollector(client) as collector:
                result = collector.get_resolution(self.store.window(BOT, MARKET_ID))
        self.assertEqual(json.loads(result['gamma_raw'])['id'], MARKET_ID)
        self.assertEqual([r[:2] for r in requests], [('GET', '/markets/1001'), ('GET', '/v2/resolutions')])


if __name__ == '__main__':
    unittest.main()
