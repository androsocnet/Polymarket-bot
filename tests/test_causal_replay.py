"""Synthetic causal replay checks. No network or production database."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
import json
import tempfile
import unittest

from tests.linux_mvp_support import START, descriptor, snapshot, us
from polymarket_bot.causal_replay import replay, SCENARIOS


def cycle(i, *, bid=None, size='100', market=None, stale=False, metadata=True):
    market = market or descriptor()
    bid = D('.30') + D('.02') * i if bid is None else D(bid)
    s = snapshot(slot=i, market=market, start=market.window_start,
                 up_bid=str(bid), up_ask=str(bid + D('.02')), size=size)
    # Fixed monotonic domain rather than process runtime.
    now = 10_000_000_000 + (us(s.validated_at) - us(START)) * 1000
    shift = now - s.validated_mono_ns
    def read(r, book=False):
        if r is None:
            return None
        raw = dict(tick_size='.01', min_order_size='1') if book and metadata else r.payload
        return replace(r, payload=raw,
                       started_mono_ns=r.started_mono_ns + shift,
                       finished_mono_ns=r.finished_mono_ns + shift,
                       started_mono=(r.started_mono_ns + shift) / 1e9,
                       finished_mono=(r.finished_mono_ns + shift) / 1e9)
    up = replace(s.up, timing=read(s.up.timing, True))
    down = replace(s.down, timing=read(s.down.timing, True))
    if stale:
        old = up.book.source_at - timedelta(seconds=30)
        up = replace(up, book=replace(up.book, source_at=old,
                                     source_timestamp_raw=str(us(old) // 1000)))
    s = replace(s, up=up, down=down, initial_time=read(s.initial_time),
                final_time=read(s.final_time), validated_mono_ns=now)
    return dict(kind='cycle', cycle=i, decision_mono_ns=now,
                decision_wall_us=us(s.validated_at), snapshot=s, error=None)


def records(cycles, **env):
    return [dict(kind='start', session='synthetic', settings=env), *cycles]


class CausalReplayTests(unittest.TestCase):
    def run_records(self, rows, scenario='OPTIMISTIC'):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = replay(rows, scenario=SCENARIOS[scenario] if isinstance(scenario, str) else scenario,
                        db_dir=Path(tmp.name))
        return result.report

    def test_buy_sell_feedback_and_reconcile(self):
        r = self.run_records(records([cycle(i) for i in range(6)],
                                     MVP_HOLDING_PERIOD='1', MVP_MAX_TRADES_PER_WINDOW='1'))
        self.assertEqual((r['trade_counts']['buys'], r['trade_counts']['sells']), (1, 1))
        self.assertEqual(r['account']['performance'], 'DETERMINISTIC')
        self.assertEqual(D(r['account']['cash']), D('100') + D(r['account']['realized']))
        self.assertTrue(r['round_trips'])
        self.assertFalse(r['certified'])
        self.assertEqual(r['strict_certified_results']['executions'], [])

    def test_partial_and_no_fill_use_first_receipt(self):
        rows = [cycle(i, size='2' if i == 3 else '100') for i in range(4)]
        r = self.run_records(records(rows))
        self.assertEqual(r['estimated_executions'][0]['status'], 'PARTIAL')
        self.assertEqual(D(r['estimated_executions'][0]['filled']), D('2'))
        rows[3] = cycle(3, size='0')
        r = self.run_records(records(rows, MVP_MAX_TRADES_PER_WINDOW='1'))
        self.assertEqual(r['estimated_executions'][0]['status'], 'NO_FILL')

    def test_end_pending_unknown(self):
        r = self.run_records(records([cycle(i) for i in range(3)]))
        self.assertEqual(r['account']['performance'], 'UNKNOWN')
        self.assertEqual(r['trade_counts']['unknown'], 1)
        self.assertIsNone(r['account']['net_pnl'])

    def test_receipt_missing_metadata_halts_not_skips(self):
        r = self.run_records(records([cycle(i, metadata=(i != 3)) for i in range(7)]))
        self.assertEqual(r['account']['performance'], 'UNKNOWN')
        self.assertEqual(r['trade_counts']['buys'], 0)
        self.assertLess(len(r['strategy_decisions']), 7)

    def test_fresh_strategy_not_weakened(self):
        r = self.run_records(records([cycle(i, stale=True) for i in range(5)]))
        self.assertEqual(r['trade_counts']['buys'], 0)
        self.assertTrue(all(d['reason'] == 'STALE_PAIR' for d in r['strategy_decisions']))

    def test_extra_cost_reduces_cash_basis_realized(self):
        r = self.run_records(records([cycle(i) for i in range(6)],
                                     MVP_HOLDING_PERIOD='1', MVP_MAX_TRADES_PER_WINDOW='1'),
                             'PESSIMISTIC')
        self.assertGreater(D(r['fees']['extra_cost']), 0)
        self.assertEqual(D(r['account']['cash']), D('100') + D(r['account']['realized']))
        self.assertEqual(D(r['account']['gross_realized']) - D(r['fees']['total']),
                         D(r['account']['realized']))

    def test_prefix_no_lookahead_and_json_determinism(self):
        rows = records([cycle(i) for i in range(8)], MVP_MAX_TRADES_PER_WINDOW='1')
        full = self.run_records(rows)
        again = self.run_records(rows)
        self.assertEqual(json.dumps(full, sort_keys=True), json.dumps(again, sort_keys=True))
        prefix = self.run_records(rows[:5])
        self.assertEqual(prefix['strategy_decisions'], full['strategy_decisions'][:4])
        self.assertEqual(prefix['estimated_executions'], full['estimated_executions'][:1])

    def test_lifetime_depth_not_replenished_by_new_receipt(self):
        from polymarket_bot.causal_replay import DepthLedger
        from tests.test_estimated import book
        ledger = DepthLedger()
        ledger.charge('t', ((D('.50'), D('2')),))
        adjusted = ledger.adapt(book(900), D('.5'))
        # Full depth 3 at .50, consumed 2, fraction .5 -> (3-2)*.5 = 0.5
        self.assertEqual(adjusted.asks[0].size, D('0.5'))
        self.assertEqual(adjusted.asks[1].size, D('2'))

    def test_risk_stop_latched_after_realized_loss(self):
        rows = [cycle(i, bid='.10' if i >= 4 else None) for i in range(8)]
        r = self.run_records(records(rows, MVP_HOLDING_PERIOD='1', MVP_MAX_DAILY_LOSS='0.01'))
        self.assertEqual(r['terminal_status'], 'RISK_STOPPED')
        self.assertEqual(r['trade_counts']['buys'], 1)
        self.assertEqual(r['trade_counts']['sells'], 1)

    def test_fee_schedule_change_unknown_before_account_mutation(self):
        rows = [cycle(i) for i in range(6)]
        s = rows[3]['snapshot']
        metadata = dict(s.market.metadata, feeSchedule=dict(rate='.08', exponent=1, takerOnly=True))
        rows[3]['snapshot'] = replace(s, market=replace(s.market, metadata=metadata))
        r = self.run_records(records(rows))
        self.assertEqual(r['halted']['reason'], 'FEE_SCHEDULE_CHANGED_OR_UNVERIFIED')
        self.assertEqual(D(r['account']['cash']), D('100'))

    def test_rollover_keeps_unknown_position_and_blocks_other_market(self):
        next_market = descriptor(market_id='1002', start=START + timedelta(seconds=900),
                                 up='333333', down='444444', condition='0x' + 'cd' * 32)
        rows = [cycle(i) for i in range(4)] + [cycle(i, market=next_market) for i in range(4)]
        r = self.run_records(records(rows, MVP_HOLDING_PERIOD='449'))
        self.assertEqual(r['trade_counts']['buys'], 1)
        self.assertTrue(any(d['reason'] == 'POSITION_LIMIT' for d in r['strategy_decisions']))
        self.assertEqual(r['unresolved'][0]['market_id'], '1001')

    def test_settlement_only_at_recorded_receipt(self):
        from tests.test_linux_mvp_settlement import evidence
        rows = [cycle(i) for i in range(4)]
        next_row = cycle(481, bid='.40')
        resolution = dict(kind='resolution', window=dict(market_id='1001'),
                          evidence=evidence(), received_mono_ns=next_row['decision_mono_ns'] - 500_000_000,
                          received_wall_us=next_row['decision_wall_us'] - 500_000)
        before = self.run_records(records(rows, MVP_HOLDING_PERIOD='449'))
        after = self.run_records(records([*rows, resolution, next_row], MVP_HOLDING_PERIOD='449'))
        self.assertFalse(before['settlements'])
        self.assertEqual(after['settlements'][0]['status'], 'RESOLVED')
        self.assertEqual(after['settlements'][0]['settled_us'], resolution['received_wall_us'])

    def test_cancel_before_ready_known_but_late_cancel_gap_unknown(self):
        from polymarket_bot.estimated import Scenario
        from tests.linux_mvp_support import CONDITION
        market = replace(descriptor(), window_end=START + timedelta(seconds=6))
        successor = descriptor(market_id='1002', start=START + timedelta(seconds=6),
                               condition='0x' + 'cd' * 32, up='333333', down='444444')
        rows = [cycle(i, market=market) for i in range(3)] + [cycle(0, market=successor)]
        r = self.run_records(records(rows), Scenario(local_delay_ns=3_000_000_000))
        self.assertEqual(r['trade_counts']['cancels'], 1)
        self.assertIsNone(r['halted'])
        r = self.run_records(records(rows))
        self.assertEqual(r['trade_counts']['unknown'], 1)
        self.assertEqual(r['halted']['reason'], 'CANCELLED_WITH_EVIDENCE_GAP')

    def test_gross_realized_excludes_open_position_fee_allocation(self):
        r = self.run_records(records([cycle(i) for i in range(4)], MVP_HOLDING_PERIOD='449'))
        self.assertEqual(D(r['account']['gross_realized']), 0)
        self.assertGreater(D(r['fees']['market_fees']), 0)

    def test_daily_loss_from_settlement_latches_before_new_receipt(self):
        from tests.test_linux_mvp_settlement import evidence
        rows = [cycle(i) for i in range(4)]
        next_row = cycle(481, bid='.40')
        receipt = dict(kind='resolution', window=dict(market_id='1001'),
                       evidence=evidence(winner='DOWN'),
                       received_mono_ns=next_row['decision_mono_ns'] - 500_000_000,
                       received_wall_us=next_row['decision_wall_us'] - 500_000)
        r = self.run_records(records([*rows, receipt, next_row],
                                     MVP_HOLDING_PERIOD='449', MVP_MAX_DAILY_LOSS='0.01'))
        self.assertEqual(r['terminal_status'], 'RISK_STOPPED')
        self.assertEqual(r['trade_counts']['buys'], 1)

    def test_identity_conflict_in_execution_book_unknown(self):
        rows = [cycle(i) for i in range(5)]
        s = rows[3]['snapshot']
        rows[3]['snapshot'] = replace(s, up=replace(s.up, book=replace(s.up.book, condition_id='bad')))
        r = self.run_records(records(rows))
        self.assertEqual(r['trade_counts']['buys'], 0)
        self.assertEqual(r['account']['performance'], 'UNKNOWN')

    def test_hard_risk_cash_and_exposure_unchanged(self):
        r = self.run_records(records([cycle(i) for i in range(5)], MVP_INITIAL_CASH='1'))
        self.assertEqual(r['trade_counts']['buys'], 0)
        self.assertTrue(any(d['reason'] == 'INSUFFICIENT_CASH_RESERVE' for d in r['strategy_decisions']))

    def test_capture_codec_integration_and_attempt_records_ignored(self):
        from polymarket_bot.depth_capture import encode, decode
        rows = records([cycle(i) for i in range(4)], MVP_MAX_TRADES_PER_WINDOW='1')
        rows.insert(2, dict(kind='attempt', cycle=0, actual_send=True))
        rows.append(dict(kind='summary', session='synthetic'))
        r = self.run_records([decode(encode(row)) for row in rows])
        self.assertEqual(r['trade_counts']['buys'], 1)

    def test_nonmonotonic_capture_refused(self):
        with self.assertRaises(ValueError):
            self.run_records(records([cycle(1), cycle(0)]))


if __name__ == '__main__':
    unittest.main()
