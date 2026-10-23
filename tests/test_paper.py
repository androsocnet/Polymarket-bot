"""Offline paper execution: fake clocks/HTTP only, real disposable CP5 SQLite."""
import hashlib
import io
import json
from pathlib import Path
import socket
import sqlite3
import unittest
from contextlib import closing, redirect_stdout, redirect_stderr
from decimal import Decimal, localcontext
from unittest.mock import patch

import httpx

from tests.recorder_support import RecorderFixture
from tests.support import make_event


def recorded_fixture(case):
    from polymarket_bot.recorder.models import RunConfig
    from polymarket_bot.recorder.service import Recorder
    f = RecorderFixture()
    case.addCleanup(f.close)
    row = f.market_row()
    f.market_row = lambda: row
    original = f.respond
    def respond(request):
        if request.url.path == '/events/2001':
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(make_event(markets=[row])).encode()))
        return original(request)
    f.client._transport = httpx.MockTransport(respond)
    recorder = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns,
                        wall_us=f.wall_us, sleep=f.sleep)
    result = recorder.run(RunConfig(selected_event_id='2001', selected_market_id='1001'),
                          should_stop=lambda: False, deadline_ns=f.monotonic_ns()+7_000_000_000)
    case.assertEqual(result.exit_code, 0)
    return f


class OfflineTests(unittest.TestCase):
    def setUp(self):
        for target, name in ((socket, 'getaddrinfo'), (socket, 'create_connection'),
                             (socket.socket, 'connect'), (socket.socket, 'connect_ex')):
            guard = patch.object(target, name, side_effect=AssertionError('network forbidden'))
            guard.start(); self.addCleanup(guard.stop)


def synthetic_recording():
    from polymarket_bot.paper import Recording, Snapshot
    from polymarket_bot.market_data.clob import parse_book
    from polymarket_bot.market_data.http import JsonRead
    from tests.support import make_book, TOKEN_UP, TOKEN_DOWN, CONDITION
    snapshots = []
    base_us = 1792507500000000
    for slot in range(1,5):
        start = slot*2_000_000_000
        for outcome, token in [('UP',TOKEN_UP),('DOWN',TOKEN_DOWN)]:
            raw = make_book(asset_id=token, timestamp=str((base_us+start//1000)//1000),
                            bids=[{'price':'0.4','size':'3'},{'price':'0.3','size':'7'}],
                            asks=[{'price':'0.6','size':'2'},{'price':'0.7','size':'3'}])
            book = parse_book(JsonRead(raw,0,0),token_id=token,condition_id=CONDITION)
            snapshots.append(Snapshot(slot,slot-1,outcome,slot,start,start+100_000_000,
                base_us+start//1000+100_000,base_us+start//1000,1_000_000,'a'*64,book,start+100_000_000))
    return Recording('run','1001',CONDITION,{'UP':TOKEN_UP,'DOWN':TOKEN_DOWN},'b'*64,
                     tuple(snapshots),(),8_100_000_000,base_us+8_100_000,
                     tuple(dict(slot_id=s.slot_id,slot_index=s.slot_index,outcome=s.outcome,start_ns=s.start_ns,reason=None) for s in snapshots))


def order(name='buy', side='BUY', qty='1', slot=1, outcome='UP', **overrides):
    from tests.support import TOKEN_UP, TOKEN_DOWN
    return dict(id=name,market_id='1001',token_id=TOKEN_UP if outcome=='UP' else TOKEN_DOWN,
                side=side,quantity=qty,decision_slot_id=slot,**overrides)


class EngineTests(OfflineTests):
    def replay(self, orders, **kwargs):
        from polymarket_bot.paper import simulate
        return simulate(synthetic_recording(),orders,initial_cash='100',fee_bps='100',**kwargs)

    def test_asks_depth_fees_and_next_snapshot(self):
        result = self.replay([order(qty='4')])
        trade = result['ledger'][0]
        self.assertEqual(trade['status'],'FILLED')
        self.assertEqual(trade['snapshot']['slot_id'],2)
        self.assertEqual([(f['price'],f['quantity']) for f in trade['fills']],[('0.6','2'),('0.7','2')])
        self.assertEqual(Decimal(trade['vwap']),Decimal('0.65'))
        self.assertEqual(Decimal(trade['fee']),Decimal('0.026'))
        self.assertEqual(Decimal(trade['depth_impact']),Decimal('0.2'))
        self.assertEqual(Decimal(result['cash']),Decimal('97.374'))
        self.assertEqual(Decimal(result['positions']['UP']['cost_basis']),Decimal('2.626'))
        self.assertEqual(Decimal(result['positions']['UP']['unrealized_pnl']),Decimal('-1.026'))

    def test_sell_bids_average_cost_and_realized_pnl(self):
        result = self.replay([order(qty='4'),order('sell','SELL','3',slot=2)])
        sale = result['ledger'][1]
        self.assertEqual(sale['fills'],[{'price':'0.4','quantity':'3','notional':'1.2'}])
        self.assertEqual(Decimal(result['realized_pnl']),Decimal('-0.7815'))
        self.assertEqual(Decimal(result['positions']['UP']['quantity']),Decimal('1'))
        self.assertEqual(Decimal(result['positions']['UP']['cost_basis']),Decimal('0.6565'))
        self.assertEqual(Decimal(result['cash']),Decimal('98.562'))
        self.assertEqual(Decimal(result['total_pnl']),Decimal('-1.038'))

    def test_partial_exhausted_and_no_reused_depth(self):
        r = self.replay([order('large',qty='10'),order('empty')])
        self.assertEqual([x['status'] for x in r['ledger']],['PARTIAL','REJECTED'])
        self.assertEqual(Decimal(r['ledger'][0]['filled_quantity']),Decimal('5'))
        self.assertEqual(r['ledger'][1]['reason'],'INSUFFICIENT_LIQUIDITY')
        self.assertEqual(Decimal(r['positions']['UP']['quantity']),Decimal('5'))

    def test_tokens_separate_and_cash_position_refusals(self):
        from polymarket_bot.paper import simulate
        r = self.replay([order(),order('down',outcome='DOWN'),order('oversell','SELL','2',slot=2)])
        self.assertEqual(r['ledger'][-1]['reason'],'INSUFFICIENT_POSITION')
        self.assertEqual(Decimal(r['positions']['DOWN']['quantity']),Decimal('1'))
        r = simulate(synthetic_recording(),[order(qty='4')],initial_cash='1',fee_bps='0')
        self.assertEqual(r['ledger'][0]['reason'],'INSUFFICIENT_CASH')
        self.assertEqual(Decimal(r['cash']),Decimal('1'))

    def test_deterministic_and_ambient_decimal_independent(self):
        orders = [order(qty='4'),order('sale','SELL','1',slot=2)]
        first = self.replay(orders)
        with localcontext() as context:
            context.prec=3
            self.assertEqual(first,self.replay(orders))
        self.assertEqual(first,self.replay(orders))

    def test_no_future_snapshot_and_delay_never_go_back(self):
        r = self.replay([order(slot=4)])
        self.assertEqual(r['ledger'][0]['reason'],'NO_FUTURE_SNAPSHOT')
        r = self.replay([order()],delay_us=2_000_000)
        self.assertEqual(r['ledger'][0]['snapshot']['slot_id'],3)
        r = self.replay([order()],max_wait_us=100)
        self.assertEqual(r['ledger'][0]['reason'],'STALE_OR_MISSING_SNAPSHOT')

    def test_stale_missing_or_invalid_book_not_filled(self):
        from dataclasses import replace
        from polymarket_bot.paper import simulate
        recording = synthetic_recording()
        stale = replace(recording,snapshots=tuple(replace(s,source_us=s.source_us-10_000_000) if s.slot_id==2 else s for s in recording.snapshots))
        r = simulate(stale,[order()],initial_cash='100',fee_bps='0')
        self.assertEqual(r['ledger'][0]['reason'],'STALE_OR_MISSING_SNAPSHOT')
        missing = replace(recording,snapshots=tuple(s for s in recording.snapshots if s.slot_id!=2))
        r = simulate(missing,[order()],initial_cash='100',fee_bps='0',max_wait_us=2_000_000)
        self.assertEqual(r['ledger'][0]['reason'],'MISSING_EXECUTION_BOOK')

    def test_wrong_identity_and_bad_instructions_rejected(self):
        instructions = [dict(order(),market_id='other'),dict(order('token'),token_id='999'),
                        order('badqty',qty='NaN'),order('negative',qty='-1'),order('float',qty=1.2),
                        order('badside',side='SHORT'),order('badslot',slot=999),order('boolslot',slot=True),None]
        r = self.replay(instructions)
        self.assertTrue(all(t['status']=='REJECTED' for t in r['ledger']))
        self.assertEqual(Decimal(r['cash']),Decimal('100'))
        self.assertEqual(r['counts']['requested'],len(instructions))

    def test_fee_must_be_explicit_and_config_valid(self):
        from polymarket_bot.paper import simulate
        for fees in (None,'NaN','-1','10001',1.5):
            with self.subTest(fees=fees),self.assertRaises(ValueError):
                simulate(synthetic_recording(),[],initial_cash='1',fee_bps=fees)
        for option in ({'delay_us':-1},{'max_age_us':True},{'max_wait_us':0}):
            with self.subTest(option=option),self.assertRaises(ValueError):
                self.replay([],**option)

    def test_chronological_events_not_input_order_or_future_funding(self):
        orders = [order('laterbuy',slot=3),order('earlysell','SELL',slot=1),order('earlybuy',slot=1)]
        r = self.replay(orders)
        self.assertEqual([t['id'] for t in r['ledger']],['earlysell','earlybuy','laterbuy'])
        self.assertEqual(r['ledger'][0]['reason'],'INSUFFICIENT_POSITION')

    def test_empty_bid_mark_is_unknown_not_zero(self):
        from dataclasses import replace
        from polymarket_bot.paper import simulate
        recording = synthetic_recording()
        snapshots = tuple(replace(s,book=replace(s.book,bids=(),best_bid=None,bid_size=None)) if s.slot_id==4 else s for s in recording.snapshots)
        r = simulate(replace(recording,snapshots=snapshots),[order()],initial_cash='100',fee_bps='0')
        self.assertIsNone(r['positions']['UP']['unrealized_pnl'])
        self.assertIsNone(r['total_pnl'])
        self.assertIsNone(r['settlement'])

    def test_mark_source_age_checked_at_valuation_horizon(self):
        from dataclasses import replace
        from polymarket_bot.paper import simulate
        r = synthetic_recording()
        # Each leg below 5 seconds, total source-to-mark age exceeds 5 seconds.
        r = replace(r,horizon_ns=r.horizon_ns+3_000_000_000,horizon_us=r.horizon_us+3_000_000,
            snapshots=tuple(replace(s,source_us=s.source_us-3_000_000) if s.slot_id==4 else s for s in r.snapshots))
        result = simulate(r,[order()],initial_cash='100',fee_bps='0')
        self.assertIsNone(result['positions']['UP']['unrealized_pnl'])

    def test_decision_book_freshness_and_source_future_bound(self):
        from dataclasses import replace
        from polymarket_bot.paper import simulate
        r = synthetic_recording()
        for delta in (-10_000_000,10_000_000):
            changed = replace(r,snapshots=tuple(replace(s,source_us=s.source_us+delta) if s.slot_id==1 else s for s in r.snapshots))
            with self.subTest(delta=delta):
                result = simulate(changed,[order()],initial_cash='100',fee_bps='0')
                self.assertEqual(result['ledger'][0]['reason'],'STALE_DECISION_BOOK')

    def test_later_source_time_cannot_authorize_earlier_request(self):
        from dataclasses import replace
        from polymarket_bot.paper import simulate
        r = synthetic_recording()
        # Slot 2 request began before decision finished, despite higher source timestamp.
        changed = replace(r,snapshots=tuple(replace(s,start_ns=2_050_000_000) if s.slot_id==2 else s for s in r.snapshots),
                          opportunities=tuple(dict(o,start_ns=2_050_000_000) if o['slot_id']==2 else o for o in r.opportunities))
        result = simulate(changed,[order()],initial_cash='100',fee_bps='0')
        self.assertEqual(result['ledger'][0]['snapshot']['slot_id'],3)

    def test_duplicate_order_id_and_empty_asks_do_not_fill(self):
        from dataclasses import replace
        from polymarket_bot.paper import simulate
        r = self.replay([order(),order()])
        self.assertEqual(r['counts']['filled'],1)
        self.assertEqual(r['counts']['rejected'],1)
        r = synthetic_recording()
        changed = replace(r,snapshots=tuple(replace(s,book=replace(s.book,asks=(),best_ask=None,ask_size=None)) if s.slot_id==2 else s for s in r.snapshots))
        result = simulate(changed,[order()],initial_cash='100',fee_bps='0')
        self.assertEqual(result['ledger'][0]['reason'],'INSUFFICIENT_LIQUIDITY')

    def test_rejected_buy_does_not_consume_liquidity(self):
        from polymarket_bot.paper import simulate
        result = simulate(synthetic_recording(),[order('large',qty='4'),order('small',qty='1')],initial_cash='1',fee_bps='0')
        self.assertEqual([t['status'] for t in result['ledger']],['REJECTED','FILLED'])
        self.assertEqual(Decimal(result['cash']),Decimal('0.4'))

    def test_full_sale_clears_basis_and_new_snapshot_replenishes(self):
        r = self.replay([order(qty='5'),order('sell','SELL','5',slot=2),order('buyagain',slot=3)])
        self.assertEqual([t['status'] for t in r['ledger']],['FILLED']*3)
        self.assertEqual(Decimal(r['positions']['UP']['cost_basis']),Decimal('0.606'))


class TimingRegressionTests(OfflineTests):
    def execution_case(self, sql, *, delay_us=0):
        from polymarket_bot.paper import load_recording, simulate
        f = recorded_fixture(self)
        slots = f.rows("SELECT slot_id FROM sample_slots WHERE status='COMPLETE' ORDER BY start_mono_ns")
        first, following, later = (s['slot_id'] for s in slots[:3])
        with closing(sqlite3.connect(f.db.path)) as conn, conn:
            conn.execute(sql,(following,))
        run = f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        recording = load_recording(f.db.path,run,'1001')
        # Later valid UP book exists; default five-second wait must not skip bad first.
        self.assertTrue(any(s.slot_id==later and s.outcome=='UP' for s in recording.snapshots))
        trade = simulate(recording,[order(slot=first)],initial_cash='100',fee_bps='10',delay_us=delay_us)['ledger'][0]
        return trade, following, later

    def test_invalid_first_opportunity_not_replaced_by_later_valid_book(self):
        trade, following, _ = self.execution_case("UPDATE book_observations SET status='INVALID',best_bid_text=NULL,best_ask_text=NULL,bid_size_text=NULL,ask_size_text=NULL,midpoint_text=NULL,spread_text=NULL WHERE slot_id=? AND outcome='UP'")
        self.assertEqual(trade['status'],'REJECTED')
        self.assertEqual(trade['reason'],'INVALID_EXECUTION_BOOK')
        self.assertEqual(trade['opportunity']['slot_id'],following)
        self.assertEqual(trade['fills'],[])
        self.assertEqual(trade['cash_after'],'100')

    def test_missing_first_token_not_replaced_by_later_valid_book(self):
        trade, following, _ = self.execution_case("DELETE FROM book_observations WHERE slot_id=? AND outcome='UP'")
        self.assertEqual(trade['status'],'REJECTED')
        self.assertEqual(trade['reason'],'MISSING_EXECUTION_BOOK')
        self.assertEqual(trade['opportunity']['slot_id'],following)
        self.assertEqual(trade['fills'],[])

    def test_whole_missing_slot_books_not_skipped(self):
        trade, following, _ = self.execution_case('DELETE FROM book_observations WHERE slot_id=?')
        self.assertEqual(trade['reason'],'MISSING_EXECUTION_BOOK')
        self.assertEqual(trade['opportunity']['slot_id'],following)

    def test_missing_token_uses_batch_request_time_for_delay(self):
        from polymarket_bot.paper import load_recording, simulate
        f = recorded_fixture(self)
        slots = f.rows("SELECT slot_id,end_mono_ns FROM sample_slots WHERE status='COMPLETE' ORDER BY start_mono_ns")
        first, following = slots[:2]
        with closing(sqlite3.connect(f.db.path)) as conn, conn:
            conn.execute('DELETE FROM book_observations WHERE slot_id=? AND outcome=?',(following['slot_id'],'UP'))
            # Capture began before delay elapsed; actual batch began after it.
            conn.execute('UPDATE sample_slots SET start_mono_ns=start_mono_ns-100000000 WHERE slot_id=?',(following['slot_id'],))
        run = f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        r = load_recording(f.db.path,run,'1001')
        trade = simulate(r,[order(slot=first['slot_id'])],initial_cash='100',fee_bps='10',delay_us=1_950_000)['ledger'][0]
        self.assertEqual(trade['reason'],'MISSING_EXECUTION_BOOK')
        self.assertEqual(trade['opportunity']['slot_id'],following['slot_id'])

    def test_missed_slot_without_capture_timestamps_not_skipped(self):
        from polymarket_bot.paper import load_recording, simulate
        f = recorded_fixture(self)
        slots = f.rows("SELECT slot_id FROM sample_slots WHERE status='COMPLETE' ORDER BY start_mono_ns")
        first, following = (s['slot_id'] for s in slots[:2])
        with closing(sqlite3.connect(f.db.path)) as conn, conn:
            conn.execute('DELETE FROM book_observations WHERE slot_id=?',(following,))
            conn.execute('DELETE FROM request_attempts WHERE slot_id=?',(following,))
            conn.execute("UPDATE sample_slots SET status='MISSED',start_mono_ns=NULL,end_mono_ns=NULL,anchor_id=NULL WHERE slot_id=?",(following,))
        run = f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        r = load_recording(f.db.path,run,'1001')
        trade = simulate(r,[order(slot=first)],initial_cash='100',fee_bps='10')['ledger'][0]
        self.assertEqual(trade['reason'],'MISSING_EXECUTION_BOOK')
        self.assertEqual(trade['opportunity']['slot_id'],following)
        self.assertEqual(trade['fills'],[])

    def test_delay_intentionally_moves_past_invalid_opportunity(self):
        trade, _, later = self.execution_case("UPDATE book_observations SET status='INVALID',best_bid_text=NULL,best_ask_text=NULL,bid_size_text=NULL,ask_size_text=NULL,midpoint_text=NULL,spread_text=NULL WHERE slot_id=? AND outcome='UP'",delay_us=2_000_000)
        self.assertEqual(trade['status'],'FILLED')
        self.assertEqual(trade['snapshot']['slot_id'],later)
        self.assertGreater(trade['snapshot']['request_start_mono_ns'],trade['decision']['available_mono_ns'])

    def test_normal_first_eligible_valid_book_fills(self):
        trade, following, _ = self.execution_case('DELETE FROM book_observations WHERE slot_id=? AND 0')
        self.assertEqual(trade['status'],'FILLED')
        self.assertEqual(trade['snapshot']['slot_id'],following)

    def delayed_decision(self, availability_ns, *, source_shift_us=0):
        from dataclasses import replace
        from polymarket_bot.paper import simulate
        r = synthetic_recording()
        r = replace(r,snapshots=tuple(replace(s,available_ns=availability_ns,source_us=s.source_us+source_shift_us)
                                     if s.slot_id==1 else s for s in r.snapshots))
        return simulate(r,[order()],initial_cash='100',fee_bps='10')['ledger'][0]

    def test_recorded_request_end_used_despite_slot_completion_lag(self):
        from polymarket_bot.paper import load_recording, simulate
        f = recorded_fixture(self)
        first = f.rows("SELECT slot_id FROM sample_slots WHERE status='COMPLETE' ORDER BY start_mono_ns")[0]['slot_id']
        receipt_ns = f.rows("SELECT a.end_mono_ns FROM request_attempts a JOIN book_observations b ON a.request_id=b.request_id WHERE b.slot_id=? AND b.outcome='UP'",(first,))[0]['end_mono_ns']
        with closing(sqlite3.connect(f.db.path)) as conn, conn:
            conn.execute('UPDATE sample_slots SET end_mono_ns=? WHERE slot_id=?',(receipt_ns+5_000_001_000,first))
        run = f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        r = load_recording(f.db.path,run,'1001')
        trade = simulate(r,[order(slot=first)],initial_cash='100',fee_bps='10')['ledger'][0]
        self.assertEqual(trade['reason'],'STALE_DECISION_BOOK')
        self.assertEqual(trade['fills'],[])
        decision = next(s for s in r.snapshots if s.slot_id==first and s.outcome=='UP')
        self.assertEqual(decision.receipt_ns,receipt_ns)
        self.assertEqual(decision.available_ns-receipt_ns,5_000_001_000)

    def test_reviewer_5900000us_decision_age_rejected(self):
        trade = self.delayed_decision(7_900_000_000)
        self.assertEqual(trade['reason'],'STALE_DECISION_BOOK')
        self.assertEqual(trade['fills'],[])

    def test_decision_age_boundary_includes_completion_lag(self):
        for available, shift, expected in [
            (6_900_000_000,0,None),          # 4.9s source / 4.8s receipt age.
            (7_000_000_000,0,None),          # Exactly 5s source age.
            (7_000_001_000,0,'STALE_DECISION_BOOK'),
            (7_100_000_000,1_000_000,None),  # Exactly 5s receipt age, younger source.
            (7_100_001_000,1_000_000,'STALE_DECISION_BOOK')]:
            with self.subTest(available=available,shift=shift):
                trade = self.delayed_decision(available,source_shift_us=shift)
                self.assertEqual(trade['reason'],expected)
                if expected is None:
                    self.assertEqual(trade['status'],'FILLED')
                    self.assertEqual(trade['snapshot']['slot_id'],4)
                    self.assertGreater(trade['snapshot']['request_start_mono_ns'],available)

    def test_future_uncertainty_evaluated_at_actual_availability(self):
        # At receipt these sources are too far ahead; at decision exactly allowed / 1us too far.
        for shift, expected in [(1_300_000,None),(1_300_001,'STALE_DECISION_BOOK')]:
            with self.subTest(shift=shift):
                trade = self.delayed_decision(2_300_000_000,source_shift_us=shift)
                self.assertEqual(trade['reason'],expected)
                if expected is None:
                    self.assertEqual(trade['snapshot']['slot_id'],2)


class CausalClockTests(OfflineTests):
    def missed_recording(self):
        from polymarket_bot.paper import load_recording
        f = recorded_fixture(self)
        slots = f.rows("SELECT * FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_index")
        first, missing, later = slots[:3]
        with closing(sqlite3.connect(f.db.path)) as conn, conn:
            conn.execute('DELETE FROM book_observations WHERE slot_id=?',(missing['slot_id'],))
            conn.execute('DELETE FROM request_attempts WHERE slot_id=?',(missing['slot_id'],))
            conn.execute("UPDATE sample_slots SET status='MISSED',start_mono_ns=NULL,end_mono_ns=NULL,anchor_id=NULL WHERE slot_id=?",(missing['slot_id'],))
        run = f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        def load():
            return load_recording(f.db.path,run,'1001')
        return f, first, missing, later, load

    def append_anchor(self, f, *, midpoint_ns, offset_us=0, response_end_ns=None):
        anchor = dict(f.rows('SELECT * FROM clock_anchors ORDER BY request_end_mono_ns LIMIT 1')[0])
        shift_ns = midpoint_ns-anchor['anchor_mono_ns']
        anchor.pop('anchor_id')
        for key in ('request_start_mono_ns','request_end_mono_ns','anchor_mono_ns'):
            anchor[key] += shift_ns
        for key in ('request_start_wall_us','request_end_wall_us'):
            anchor[key] += shift_ns//1000
        anchor['anchor_server_us'] += shift_ns//1000+offset_us
        anchor['server_seconds'] = anchor['anchor_server_us']//1_000_000
        if response_end_ns is not None:
            anchor['request_end_mono_ns'] = response_end_ns
            anchor['request_start_mono_ns'] = 2*midpoint_ns-response_end_ns
        with closing(sqlite3.connect(f.db.path)) as conn, conn:
            return conn.execute('INSERT INTO clock_anchors ('+','.join(anchor)+') VALUES ('+','.join('?' for _ in anchor)+')',tuple(anchor.values())).lastrowid

    def trades(self, recording, first, *, delay_us=0):
        from polymarket_bot.paper import simulate
        return [simulate(recording,[order(outcome=outcome,slot=first['slot_id'])],initial_cash='100',fee_bps='10',delay_us=delay_us)['ledger'][0]
                for outcome in ('UP','DOWN')]

    def test_one_anchor_missing_opportunity_blocks_both_tokens(self):
        _, first, missing, _, load = self.missed_recording()
        for trade in self.trades(load(),first):
            self.assertEqual(trade['reason'],'MISSING_EXECUTION_BOOK')
            self.assertEqual(trade['opportunity']['slot_id'],missing['slot_id'])
            self.assertEqual(trade['fills'],[])

    def test_appending_future_calibration_preserves_prefix_eligibility(self):
        f, first, missing, _, load = self.missed_recording()
        before = load()
        trades = self.trades(before,first)
        for offset in (100_000,3_000_000):
            with self.subTest(offset=offset):
                self.append_anchor(f,midpoint_ns=missing['start_mono_ns']+10_000_000_000+offset*1000,offset_us=offset)
                after = load()
                self.assertEqual(before.snapshots,after.snapshots)
                self.assertEqual(self.trades(after,first),trades)
                self.assertEqual(before.opportunities,after.opportunities)

    def test_latest_causal_anchor_among_multiple_before_and_after(self):
        f, first, missing, _, load = self.missed_recording()
        # Calibration completed between decision and missing target; 100ms correction is causal.
        self.append_anchor(f,midpoint_ns=first['end_mono_ns']+100_000_000,offset_us=100_000)
        before = load()
        expected_start = missing['start_mono_ns']-100_000_000
        for trade in self.trades(before,first):
            self.assertEqual(trade['reason'],'MISSING_EXECUTION_BOOK')
            self.assertEqual(trade['opportunity']['start_ns'],expected_start)
        self.append_anchor(f,midpoint_ns=missing['start_mono_ns']+5_000_000_000,offset_us=3_000_000)
        after = load()
        self.assertEqual(before.snapshots,after.snapshots)
        self.assertEqual(before.opportunities,after.opportunities)
        self.assertEqual(self.trades(before,first),self.trades(after,first))

    def test_calibration_midpoint_before_target_but_response_after_is_future(self):
        f, first, missing, _, load = self.missed_recording()
        before = load()
        target = missing['start_mono_ns']
        self.append_anchor(f,midpoint_ns=target-50_000_000,offset_us=100_000,response_end_ns=target+50_000_000)
        after = load()
        self.assertEqual(before.opportunities,after.opportunities)
        self.assertEqual(self.trades(before,first),self.trades(after,first))

    def test_future_negative_offset_cannot_make_itself_causally_available(self):
        f, first, missing, _, load = self.missed_recording()
        before = load()
        # Its own projection puts target after receipt, but old mapping says target already passed.
        self.append_anchor(f,midpoint_ns=missing['start_mono_ns']+100_000_000,offset_us=-500_000)
        after = load()
        self.assertEqual(before.opportunities,after.opportunities)
        self.assertEqual(self.trades(before,first),self.trades(after,first))

    def test_missing_causal_clock_fails_closed(self):
        f, _, _, _, load = self.missed_recording()
        # Calibration response now ends after missing opportunity; book mappings stay intact.
        with closing(sqlite3.connect(f.db.path)) as conn, conn:
            conn.execute('UPDATE clock_anchors SET request_end_mono_ns=request_end_mono_ns+20000000000')
        for trade in self.trades(load(),f.rows("SELECT * FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_index")[0]):
            self.assertEqual(trade['reason'],'UNSAFE_MISSING_SLOT_TIMING')
            self.assertEqual(trade['fills'],[])

    def test_causal_offset_reordering_missing_slot_fails_closed(self):
        f, first, _, _, load = self.missed_recording()
        self.append_anchor(f,midpoint_ns=first['end_mono_ns']+100_000_000,offset_us=3_000_000)
        for trade in self.trades(load(),first):
            self.assertEqual(trade['reason'],'UNSAFE_MISSING_SLOT_TIMING')
            self.assertEqual(trade['fills'],[])

    def test_conflicting_simultaneous_calibrations_fail_closed(self):
        f, first, _, _, load = self.missed_recording()
        self.append_anchor(f,midpoint_ns=first['end_mono_ns']+100_000_000)
        self.append_anchor(f,midpoint_ns=first['end_mono_ns']+100_000_000,offset_us=100_000)
        for trade in self.trades(load(),first):
            self.assertEqual(trade['reason'],'UNSAFE_MISSING_SLOT_TIMING')
            self.assertEqual(trade['fills'],[])

    def test_ambiguous_later_hole_cannot_override_earlier_valid_execution(self):
        from dataclasses import replace
        from polymarket_bot.paper import simulate
        r = synthetic_recording()
        unsafe = dict(slot_id=4,slot_index=3,outcome='UP',start_ns=None,reason='UNSAFE_MISSING_SLOT_TIMING')
        r = replace(r,opportunities=tuple(o for o in r.opportunities if o['slot_id']!=4 or o['outcome']!='UP')+(unsafe,))
        trade = simulate(r,[order()],initial_cash='100',fee_bps='10')['ledger'][0]
        self.assertEqual(trade['status'],'FILLED')
        self.assertEqual(trade['snapshot']['slot_id'],2)

    def test_intentional_delay_skips_causally_timed_missing_slot(self):
        f, first, missing, later, load = self.missed_recording()
        self.append_anchor(f,midpoint_ns=missing['start_mono_ns']+10_000_000_000,offset_us=3_000_000)
        for trade in self.trades(load(),first,delay_us=2_000_000):
            self.assertEqual(trade['status'],'FILLED')
            self.assertEqual(trade['snapshot']['slot_id'],later['slot_id'])


class CliTests(OfflineTests):
    def invoke(self, f, orders, output, extra=()):
        from polymarket_bot.paper import main
        path = f.db.path.parent/'orders.json'
        path.write_text(json.dumps(orders),encoding='utf8')
        run = f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main(['--db',str(f.db.path),'--run-id',run,'--market-id','1001',
                         '--orders',str(path),'--initial-cash','100','--fee-bps','10',
                         '--output-dir',str(output),*extra])

    def test_offline_cli_output_and_deterministic_bytes_source_unchanged(self):
        f = recorded_fixture(self)
        digest = hashlib.sha256(f.db.path.read_bytes()).hexdigest()
        slot = f.rows("SELECT MIN(slot_id) AS id FROM sample_slots WHERE status='COMPLETE'")[0]['id']
        out = f.db.path.parent/'paper-result'
        self.assertEqual(self.invoke(f,[order(slot=slot)],out),0)
        result = json.loads((out/'report.json').read_text())
        self.assertEqual(result['counts']['filled'],1)
        self.assertEqual(result['assumptions']['fee_status'],'ASSUMED_NOT_VERIFIED')
        self.assertEqual(len((out/'ledger.jsonl').read_text().splitlines()),1)
        out2 = f.db.path.parent/'paper-again'
        self.assertEqual(self.invoke(f,[order(slot=slot)],out2),0)
        for name in ('report.json','ledger.jsonl'):
            self.assertEqual((out/name).read_bytes(),(out2/name).read_bytes())
        self.assertEqual(hashlib.sha256(f.db.path.read_bytes()).hexdigest(),digest)

    def test_cli_existing_output_and_invalid_config_no_overwrite(self):
        f = recorded_fixture(self)
        out = f.db.path.parent/'existing';out.mkdir()
        (out/'sentinel').write_text('untouched')
        self.assertEqual(self.invoke(f,[],out),2)
        self.assertEqual((out/'sentinel').read_text(),'untouched')
        out = f.db.path.parent/'bad'
        self.assertEqual(self.invoke(f,[],out,('--delay-us','-1')),2)
        self.assertFalse(out.exists())

    def test_fee_required_and_malformed_order_file_fail(self):
        from polymarket_bot.paper import main
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as caught:
            main([])
        self.assertEqual(caught.exception.code,2)
        f = recorded_fixture(self)
        out = f.db.path.parent/'bad-shape'
        self.assertEqual(self.invoke(f,{'not':'list'},out),2)
        self.assertFalse(out.exists())


class LoaderTests(OfflineTests):
    def load(self, f):
        from polymarket_bot.paper import load_recording
        run_id = f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        return load_recording(f.db.path, run_id, '1001')

    def test_read_only_depth_and_evidence(self):
        f = recorded_fixture(self)
        before = hashlib.sha256(f.db.path.read_bytes()).hexdigest()
        recording = self.load(f)
        self.assertEqual(recording.db_sha256, before)
        self.assertGreaterEqual(len(recording.snapshots), 4)
        self.assertEqual({s.outcome for s in recording.snapshots}, {'UP','DOWN'})
        first = recording.snapshots[0]
        self.assertTrue(first.book.bids and first.book.asks)
        self.assertGreaterEqual(first.available_ns, first.start_ns)
        self.assertEqual(len(first.raw_sha256), 64)
        self.assertEqual(hashlib.sha256(f.db.path.read_bytes()).hexdigest(), before)

    def test_invalid_missing_and_incomplete_observations_never_loaded(self):
        f = recorded_fixture(self)
        with closing(sqlite3.connect(f.db.path)) as connection, connection:
            connection.execute("UPDATE book_observations SET status='INVALID',best_bid_text=NULL,best_ask_text=NULL,bid_size_text=NULL,ask_size_text=NULL,midpoint_text=NULL,spread_text=NULL WHERE slot_id=(SELECT MIN(slot_id) FROM book_observations) AND outcome='UP'")
        recording = self.load(f)
        self.assertTrue(recording.excluded)
        slot_id = f.rows('SELECT MIN(slot_id) AS id FROM book_observations')[0]['id']
        self.assertFalse(any(s.slot_id == slot_id and s.outcome == 'UP' for s in recording.snapshots))

    def test_raw_token_and_audit_link_conflict_refused(self):
        f = recorded_fixture(self)
        with closing(sqlite3.connect(f.db.path)) as connection, connection:
            connection.execute("UPDATE book_observations SET expected_token_id='999' WHERE outcome='UP'")
        recording = self.load(f)
        self.assertFalse(any(s.outcome == 'UP' for s in recording.snapshots))
        self.assertTrue(recording.excluded)

    def test_corrupt_compressed_depth_excluded_not_crash(self):
        f = recorded_fixture(self)
        slot = f.rows("SELECT MIN(slot_id) AS id FROM book_observations")[0]['id']
        with closing(sqlite3.connect(f.db.path)) as conn,conn:
            conn.execute("UPDATE request_attempts SET response_raw_z=X'00' WHERE slot_id=?",(slot,))
        r = self.load(f)
        self.assertFalse(any(s.slot_id==slot for s in r.snapshots))
        self.assertTrue(any('compressed' in x['reason'] for x in r.excluded))

    def test_conflicting_clob_condition_refused(self):
        import zlib
        f = recorded_fixture(self)
        with closing(sqlite3.connect(f.db.path)) as conn,conn:
            rev = conn.execute('SELECT clob_config_raw_z,raw_sizes_json FROM market_revisions').fetchone()
            config = json.loads(zlib.decompress(rev[0]));config['c']='0x'+'00'*32
            raw=json.dumps(config).encode();sizes=json.loads(rev[1]);sizes['clob']=len(raw)
            conn.execute('UPDATE market_revisions SET clob_config_raw_z=?,raw_sizes_json=?',(zlib.compress(raw),json.dumps(sizes)))
        with self.assertRaises(ValueError):
            self.load(f)

    def test_revision_closed_or_conflicting_clob_cannot_authorize_fills(self):
        f = recorded_fixture(self)
        with closing(sqlite3.connect(f.db.path)) as conn,conn:
            conn.execute('UPDATE market_revisions SET accepting_orders=0')
        with self.assertRaises(ValueError):
            self.load(f)

    def test_raw_corruption_or_wrong_audit_link_excluded(self):
        f = recorded_fixture(self)
        ids = [r['slot_id'] for r in f.rows("SELECT slot_id FROM sample_slots WHERE status='COMPLETE' ORDER BY slot_id")]
        with closing(sqlite3.connect(f.db.path)) as conn, conn:
            conn.execute('UPDATE request_attempts SET raw_complete=0 WHERE slot_id=?',(ids[0],))
            conn.execute('UPDATE book_observations SET request_id=(SELECT request_id FROM book_observations WHERE slot_id=? LIMIT 1) WHERE slot_id=?',(ids[0],ids[1]))
        r = self.load(f)
        self.assertFalse(any(s.slot_id in ids[:2] for s in r.snapshots))
        self.assertGreaterEqual(len(r.excluded),4)

    def test_missing_opposite_book_disclosed(self):
        f = recorded_fixture(self)
        slot = f.rows("SELECT MIN(slot_id) AS id FROM book_observations")[0]['id']
        with closing(sqlite3.connect(f.db.path)) as conn,conn:
            conn.execute("DELETE FROM book_observations WHERE slot_id=? AND outcome='DOWN'",(slot,))
        r = self.load(f)
        self.assertTrue(any(x['slot_id']==slot and x.get('outcome')=='DOWN' for x in r.excluded))

    def test_missing_database_and_wrong_selection_fail_closed(self):
        from polymarket_bot.paper import load_recording
        f = recorded_fixture(self)
        missing = f.db.path.parent/'absent.sqlite'
        with self.assertRaises((ValueError, OSError)):
            load_recording(missing, 'bad', '1001')
        self.assertFalse(missing.exists())
        for run, market in [('bad','1001'),(f.rows('SELECT run_id FROM recording_runs')[0]['run_id'],'999')]:
            with self.subTest(run=run,market=market), self.assertRaises(ValueError):
                load_recording(f.db.path,run,market)
