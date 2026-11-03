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

    def test_missing_clock_evidence_fails_closed_before_window_creation(self):
        engine = self.engine([replace(rising(0), initial_time=None)])
        result = self.step(engine)
        self.assertEqual(result.reason, 'TIMING_INVALID')
        self.assertEqual(self.decisions(), [])
        self.assertIsNone(self.store.active_window(BOT))

    def test_malformed_validation_time_fails_closed(self):
        engine = self.engine([replace(rising(0), validated_at=None)])
        self.assertEqual(self.step(engine).reason, 'TIMING_INVALID')
        self.assertEqual(self.decisions(), [])

    def test_freshness_evidence_persisted_with_real_wall_receipts(self):
        import json
        engine = self.engine([rising(0)])
        self.step(engine)
        evidence = json.loads(self.decisions()[0]['inputs_json'])['freshness']
        self.assertTrue(evidence['outcomes']['UP']['fresh'])
        row = self.store.observations_for_market(BOT, MARKET_ID)[0]
        self.assertEqual(row['received_us'], evidence['outcomes'][row['outcome']]['receipt_wall_us'])

    def test_integer_clock_negative_legacy_lag_does_not_reject_valid_pair(self):
        sample = rising(0)
        sample = replace(sample, up=replace(sample.up,
                         request_finished_at=sample.validated_at + timedelta(seconds=.7)))
        engine = self.engine([sample])
        result = self.step(engine)
        self.assertEqual(result.reason, 'WARMUP')

    def test_fresh_receipt_with_old_source_never_orders(self):
        sample = rising(0)
        old = sample.up.book.source_at - timedelta(seconds=20)
        sample = replace(sample, up=replace(sample.up, book=replace(sample.up.book,
                         source_at=old, source_timestamp_raw=str(us(old) // 1000))))
        engine = self.engine([sample])
        self.assertEqual(self.step(engine).reason, 'STALE_PAIR')
        self.assertEqual(self.store.orders(BOT), [])

    def test_missing_other_receipt_blocks_pending_fill(self):
        sample = rising(1)
        sample = replace(sample, down=replace(sample.down, timing=None))
        self.store.queue_order('pending-invalid', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('1'), MARKET_ID + ':0', 'SIGNAL_UP', us(START))
        engine = self.engine([sample])
        self.assertEqual(self.step(engine).reason, 'STALE_PAIR')
        self.assertEqual(self.store.order('pending-invalid')['status'], 'PENDING')

    def test_delayed_decision_never_executes_pending(self):
        sample = rising(1)
        self.store.queue_order('pending-delay', BOT, RUN, MARKET_ID, '111111', 'UP', 'BUY',
                               Decimal('1'), MARKET_ID + ':0', 'SIGNAL_UP', us(START))
        collector = mock.Mock()
        collector.collect.return_value = sample
        engine = PaperEngine(self.store, collector, self.settings, run_id=RUN,
                             now_us=lambda: self.now,
                             monotonic_ns=lambda: sample.validated_mono_ns + 6_000_000_000)
        self.assertEqual(self.step(engine).reason, 'SLOT_UNCERTAIN')
        self.assertEqual(self.store.order('pending-delay')['status'], 'PENDING')
        self.assertEqual(self.decisions(), [])

    def test_rejected_cycles_keep_proof_after_good_heartbeat(self):
        import json
        good = rising(0)
        receipt = good.down.timing.finished_mono_ns
        wide_first = replace(good.initial_time, payload=good.final_time.payload - 1,
                             started_mono=(receipt - 400_000_000) / 1e9,
                             started_mono_ns=receipt - 400_000_000,
                             started_wall_us=good.down.timing.finished_wall_us - 400_000)
        wide_final = replace(good.final_time, payload=good.final_time.payload - 1,
                             started_mono=receipt / 1e9, started_mono_ns=receipt,
                             started_wall_us=good.down.timing.finished_wall_us)
        wide_first = replace(wide_first, started_mono=(receipt - 1_010_000_000) / 1e9,
                             started_mono_ns=receipt - 1_010_000_000,
                             started_wall_us=good.down.timing.finished_wall_us - 1_010_000)
        wide_final = replace(wide_final, payload=good.final_time.payload,
                             started_mono=(receipt + 1_000_000) / 1e9,
                             started_mono_ns=receipt + 1_000_000,
                             started_wall_us=good.down.timing.finished_wall_us + 1_000,
                             finished_mono=(receipt + 800_000_000) / 1e9,
                             finished_mono_ns=receipt + 800_000_000,
                             finished_wall_us=good.down.timing.finished_wall_us + 800_000)
        wide = replace(good, initial_time=wide_first, final_time=wide_final,
                       validated_mono_ns=receipt + 800_000_000,
                       validated_at=good.down.request_finished_at + timedelta(seconds=.8))
        invalid = replace(good, initial_time=None)
        outside = replace(good, market=replace(good.market,
                          window_start=good.validated_at, window_end=good.market.window_end))
        engine = self.engine([invalid, wide, outside, good])
        for reason in ('TIMING_INVALID', 'SLOT_UNCERTAIN', 'OUT_OF_WINDOW'):
            self.assertEqual(self.step(engine).reason, reason)
            self.assertIsNone(self.store.active_window(BOT))
            self.assertEqual(self.decisions(), [])
            self.assertEqual(self.store.equity_rows(BOT), [])
            self.assertEqual(self.store.orders(BOT), [])
        self.assertEqual(self.step(engine).reason, 'WARMUP')
        events = [e for e in self.store.events(BOT) if e['kind'] == 'freshness_evaluated']
        self.assertEqual(len(events), 4)
        proofs = [json.loads(e['detail']) for e in events]
        self.assertFalse(proofs[0]['freshness']['valid'])
        self.assertTrue(proofs[0]['freshness']['error'])
        self.assertIsNone(proofs[0]['freshness']['captured']['initial_time'])
        proof = proofs[1]['freshness']
        self.assertGreater(proof['server_upper_us'] - proof['server_lower_us'], 2_000_000)
        self.assertFalse(proof['slot_valid'])
        self.assertEqual([p['cycle'] for p in proofs], [1, 2, 3, 4])

    def test_duplicate_fill_and_mark_link_actual_second_cycle_proof(self):
        import json
        stale, fresh = rising(3), rising(3)
        def old(observation):
            source = observation.book.source_at - timedelta(seconds=10)
            return replace(observation, book=replace(observation.book, source_at=source,
                           source_timestamp_raw=str(us(source) // 1000)))
        stale = replace(stale, up=old(stale.up), down=old(stale.down))
        engine = self.engine([stale, fresh])
        for slot in range(3):
            self.step(self.engine([rising(slot)]))
        engine.now_us = lambda: self.now
        self.assertEqual(self.store.orders(BOT)[0]['status'], 'PENDING')
        self.assertEqual(engine.step().reason, 'STALE_PAIR')
        self.assertEqual(engine.step().status, 'DUPLICATE_SLOT')
        self.assertEqual(self.store.orders(BOT)[0]['status'], 'FILLED')
        self.assertEqual(self.decisions()[-1]['reason'], 'STALE_PAIR')
        events = self.store.events(BOT)
        proofs = [e for e in events if e['kind'] == 'freshness_evaluated'][-2:]
        self.assertEqual(len(proofs), 2)
        self.assertEqual(proofs[0]['at_us'], proofs[1]['at_us'])
        first, second = [json.loads(e['detail']) for e in proofs]
        self.assertFalse(first['freshness']['outcomes']['UP']['fresh'])
        self.assertTrue(second['freshness']['outcomes']['UP']['fresh'])
        observation = next(row for row in self.store.observations_for_market(BOT, MARKET_ID)
                           if row['slot_index'] == 3 and row['outcome'] == 'UP')
        self.assertEqual(observation['source_at'], first['freshness']['outcomes']['UP']['source_us'])
        self.assertNotEqual(observation['source_at'], second['freshness']['outcomes']['UP']['source_us'])
        execution = json.loads(next(e['detail'] for e in events if e['kind'] == 'order_executed'))
        self.assertEqual(execution['freshness_event_id'], proofs[1]['id'])
        mark = json.loads([e for e in events if e['kind'] == 'equity_marked'][-1]['detail'])
        self.assertEqual(mark['freshness_event_id'], proofs[1]['id'])
        self.assertEqual(mark['equity_id'], self.store.equity_rows(BOT)[-1]['id'])

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
        self.assertEqual(order['quantity'], Decimal('9.82'))
        self.assertEqual(order['filled'], Decimal('9.82'))
        self.assertEqual(order['notional'], Decimal('9.82') * Decimal('0.38'))
        self.assertEqual(order['fee'], Decimal('0.16195'))
        self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP')['quantity'], Decimal('9.82'))
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

    def test_failed_stop_remains_terminal_for_a_new_process_run(self):
        failing = replace(self.settings, max_consecutive_errors=2)
        engine = self.engine([MarketDataError('transport_error', 'down'),
                              MarketDataError('transport_error', 'down')], settings=failing)
        self.step(engine)
        self.assertEqual(self.step(engine).status, 'FAILED_STOP')
        restarted = self.engine([rising(0)], run_id='run-2')
        self.assertEqual(self.step(restarted).status, 'FAILED_STOP')
        self.assertEqual(self.collector.calls, 0)

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
        self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP')['quantity'], Decimal('9.82'))

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
        self.assertEqual(self.store.position(BOT, MARKET_ID, 'UP')['quantity'], Decimal('9.82'))

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
