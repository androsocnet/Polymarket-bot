"""Offline strategy tests: real CP6 matching, controlled parsed paired books."""
from dataclasses import replace
from decimal import Decimal, localcontext
import unittest
from tests.test_paper import OfflineTests, synthetic_recording


def recording(mids, *, ask_size='100', bid_size='100'):
    from polymarket_bot.market_data.clob import parse_book
    from polymarket_bot.market_data.http import JsonRead
    from tests.support import make_book
    base = synthetic_recording()
    snapshots = []
    for i, mid in enumerate(mids,1):
        for template in base.snapshots[:2]:
            start = i*2_000_000_000
            m = Decimal(str(mid)) if template.outcome=='UP' else 1-Decimal(str(mid))
            book = parse_book(JsonRead(make_book(asset_id=template.book.token_id,
                timestamp=str((1792507500000000+start//1000)//1000),
                bids=[{'price':str(m-Decimal('.01')),'size':bid_size}],
                asks=[{'price':str(m+Decimal('.01')),'size':ask_size}]),0,0),
                token_id=template.book.token_id,condition_id=base.condition_id)
            snapshots.append(replace(template,slot_id=i,slot_index=i-1,request_id=i,start_ns=start,
                available_ns=start+100_000_000,receipt_ns=start+100_000_000,
                receipt_us=1792507500000000+start//1000+100000,source_us=1792507500000000+start//1000,book=book))
    return replace(base,snapshots=tuple(snapshots),horizon_ns=len(mids)*2_000_000_000+100000000,
        horizon_us=1792507500000000+len(mids)*2000000+100000,
        opportunities=tuple(dict(slot_id=s.slot_id,slot_index=s.slot_index,outcome=s.outcome,start_ns=s.start_ns,available_ns=s.available_ns,reason=None) for s in snapshots))


class StrategyTests(OfflineTests):
    def run_strategy(self, r=None, **kw):
        from polymarket_bot.strategy import run_strategy
        return run_strategy(r or recording(['.4','.42','.45','.46','.47','.48','.49','.5']),**kw)

    def test_strategy_api_exists(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('polymarket_bot.strategy'),'separate offline strategy missing')

    def test_up_entry_holding_exit_and_fee_accounting(self):
        r = self.run_strategy()
        self.assertEqual([d['action'] for d in r['decisions'][:6]],['HOLD','HOLD','BUY UP','HOLD','HOLD','HOLD'])
        self.assertEqual(r['decisions'][6]['action'],'SELL')
        trades = r['execution']['ledger']
        self.assertEqual([t['side'] for t in trades[:2]],['BUY','SELL'])
        self.assertEqual(trades[0]['requested']['quantity'],'9.99')
        self.assertEqual(Decimal(trades[0]['notional'])+Decimal(trades[0]['fee']),Decimal('4.6999953'))
        self.assertEqual(Decimal(trades[1]['realized_pnl']),Decimal('0.1902096'))
        self.assertEqual(r['execution']['settlement'],None)

    def test_down_and_hold(self):
        r = self.run_strategy(recording(['.6','.58','.55','.54']))
        self.assertEqual(r['decisions'][2]['action'],'BUY DOWN')
        r = self.run_strategy(recording(['.4']*5))
        self.assertTrue(all(d['action']=='HOLD' for d in r['decisions']))
        self.assertEqual(r['execution']['counts']['requested'],0)

    def test_reversal_closes_before_opposite_entry(self):
        r = self.run_strategy(recording(['.4','.42','.45','.44','.39','.38','.37']),holding_period=100)
        self.assertEqual(r['decisions'][4]['action'],'SELL')
        self.assertEqual(r['decisions'][5]['action'],'BUY DOWN')
        self.assertEqual(r['execution']['ledger'][1]['snapshot']['slot_id'],6)
        self.assertTrue(all(sum(Decimal(p['quantity'])>0 for p in d['positions'].values())<=1 for d in r['decisions']))

    def test_partial_entry_and_exit_remain_owned(self):
        r = self.run_strategy(recording(['.4','.42','.45','.44','.39','.38','.37'],ask_size='2',bid_size='1'),holding_period=1)
        ts = r['execution']['ledger']
        self.assertEqual(ts[0]['status'],'PARTIAL')
        self.assertEqual(ts[0]['filled_quantity'],'2')
        self.assertEqual(ts[1]['status'],'PARTIAL')
        self.assertEqual(ts[1]['filled_quantity'],'1')
        self.assertEqual(ts[2]['side'],'SELL')
        self.assertFalse(any(t.get('outcome')=='DOWN' and t.get('side')=='BUY' for t in ts[:3]))

    def test_risk_rejects_without_resizing(self):
        for kw, reason in [({'initial_cash':'9.99'},'INSUFFICIENT_CASH_RESERVE'),
                           ({'max_exposure':'9.99'},'EXPOSURE_LIMIT'),({'max_positions':0},'POSITION_LIMIT')]:
            with self.subTest(kw=kw):
                r = self.run_strategy(**kw)
                self.assertEqual(r['decisions'][2]['reason'],reason)
                self.assertEqual(r['execution']['counts']['requested'],0)

    def test_budget_price_rises_falls_exact_boundary_and_fees(self):
        for price in ('.01','.5','.99'):
            r = self.run_strategy(recording(['.4','.42','.45',price]),holding_period=100)
            trade = r['execution']['ledger'][0]
            self.assertEqual(trade['requested']['quantity'],'9.99')
            self.assertLessEqual(Decimal(trade['notional'])+Decimal(trade['fee']),Decimal('10'))
        r = self.run_strategy(recording(['.4','.42','.45','.99']),fee_bps='0',initial_cash='10')
        self.assertEqual(r['execution']['ledger'][0]['notional'],'10')
        self.assertEqual(r['execution']['cash'],'0')

    def test_open_at_end_not_closed_with_future_book(self):
        r = self.run_strategy(recording(['.4','.42','.45','.46']))
        self.assertEqual(r['execution']['positions']['UP']['quantity'],'9.99')
        self.assertIsNotNone(r['execution']['positions']['UP']['indicative_bid_mark'])
        self.assertEqual(len(r['execution']['ledger']),1)
        r = self.run_strategy(recording(['.4','.42','.45']))
        self.assertEqual(r['execution']['ledger'][0]['reason'],'NO_FUTURE_SNAPSHOT')
        self.assertEqual(r['execution']['positions']['UP']['quantity'],'0')

    def test_missing_barrier_stale_and_delay(self):
        base = recording(['.4','.42','.45','.46','.47','.48'])
        missing = replace(base,snapshots=tuple(s for s in base.snapshots if s.slot_id!=4),opportunities=tuple(
            dict(o,reason='MISSING_EXECUTION_BOOK') if o['slot_id']==4 else o for o in base.opportunities))
        self.assertEqual(self.run_strategy(missing)['execution']['ledger'][0]['reason'],'MISSING_EXECUTION_BOOK')
        delayed = self.run_strategy(base,delay_us=2_000_000)
        self.assertEqual(delayed['decisions'][3]['reason'],'PENDING_EXECUTION')
        self.assertEqual(delayed['execution']['ledger'][0]['snapshot']['slot_id'],5)
        stale = replace(base,snapshots=tuple(replace(s,source_us=s.source_us-10000000) if s.slot_id==3 else s for s in base.snapshots))
        self.assertEqual(self.run_strategy(stale)['decisions'][2]['reason'],'STALE_PAIR')

    def test_no_early_fill_with_late_response(self):
        base = recording(['.4','.42','.45','.46','.47','.48'])
        late = replace(base,snapshots=tuple(replace(s,available_ns=11_000_000_000) if s.slot_id==4 else s for s in base.snapshots))
        r = self.run_strategy(late)
        d = next(d for d in r['decisions'] if d['slot_id']==5)
        self.assertEqual(d['reason'],'PENDING_EXECUTION')
        self.assertEqual(d['positions']['UP']['quantity'],'0')

    def test_causal_prefix_invariance_independent_future_prices(self):
        short = recording(['.4','.42','.45','.46','.47'])
        a = self.run_strategy(short)
        for suffix in (['.01','.99','.1'],['.9','.8','.7']):
            b = self.run_strategy(recording(['.4','.42','.45','.46','.47']+suffix))
            self.assertEqual(a['decisions'],b['decisions'][:5])
            self.assertEqual(a['orders'],b['orders'][:len(a['orders'])])

    def test_stale_pairs_do_not_advance_holding_period(self):
        b=recording(['.4','.42','.45','.46','.47','.48','.49','.50','.51'])
        b=replace(b,snapshots=tuple(replace(s,source_us=s.source_us-10000000) if s.slot_id==5 else s for s in b.snapshots))
        r=self.run_strategy(b,holding_period=4)
        self.assertEqual(next(d for d in r['decisions'] if d['slot_id']==8)['action'],'HOLD')
        self.assertEqual(next(d for d in r['decisions'] if d['slot_id']==9)['action'],'SELL')

    def test_unavailable_invalid_response_does_not_change_earlier_decisions(self):
        b=recording(['.4','.42','.45','.46','.47','.48'])
        late=replace(b,snapshots=tuple(replace(s,available_ns=11000000000) if s.slot_id==4 else s for s in b.snapshots))
        invalid=replace(late,snapshots=tuple(s for s in late.snapshots if s.slot_id!=4),opportunities=tuple(
            dict(o,reason='INVALID_EXECUTION_BOOK',available_ns=11000000000) if o['slot_id']==4 else o for o in late.opportunities))
        a=next(d for d in self.run_strategy(late)['decisions'] if d['slot_id']==5)
        z=next(d for d in self.run_strategy(invalid)['decisions'] if d['slot_id']==5)
        self.assertEqual(a,z)

    def test_fee_inclusive_reserve_exact_boundary(self):
        b=recording(['.4','.42','.45','.99'])
        r=self.run_strategy(b,initial_cash='9.99999',max_exposure='9.99999')
        self.assertEqual(r['execution']['ledger'][0]['status'],'FILLED')
        self.assertEqual(r['execution']['cash'],'0')
        r=self.run_strategy(b,initial_cash='9.999989')
        self.assertEqual(r['decisions'][2]['reason'],'INSUFFICIENT_CASH_RESERVE')
        self.assertEqual(r['orders'],[])

    def test_invalid_and_stale_execution_not_chased(self):
        b=recording(['.4','.42','.45','.46','.47'])
        invalid=replace(b,snapshots=tuple(s for s in b.snapshots if s.slot_id!=4),opportunities=tuple(
            dict(o,reason='INVALID_EXECUTION_BOOK') if o['slot_id']==4 else o for o in b.opportunities))
        self.assertEqual(self.run_strategy(invalid)['execution']['ledger'][0]['reason'],'INVALID_EXECUTION_BOOK')
        stale=replace(b,snapshots=tuple(replace(s,source_us=s.source_us-10000000) if s.slot_id==4 else s for s in b.snapshots))
        self.assertEqual(self.run_strategy(stale)['execution']['ledger'][0]['reason'],'STALE_OR_MISSING_SNAPSHOT')

    def test_unpaired_entry_fill_counts_next_pair_as_subsequent(self):
        b=recording(['.4','.42','.45','.46','.47','.48'])
        b=replace(b,snapshots=tuple(s for s in b.snapshots if s.slot_id!=4 or s.outcome=='UP'))
        r=self.run_strategy(b,holding_period=1)
        self.assertEqual(next(d for d in r['decisions'] if d['slot_id']==5)['action'],'SELL')

    def test_stale_fill_slot_pair_does_not_delay_holding_exit(self):
        b=recording(['.4','.42','.45','.46','.47','.48','.49','.50','.51'])
        b=replace(b,snapshots=tuple(replace(s,source_us=s.source_us-10000000)
            if s.slot_id==4 and s.outcome=='DOWN' else s for s in b.snapshots))
        for period,expected in ((1,5),(3,7)):
            with self.subTest(holding_period=period):
                r=self.run_strategy(b,holding_period=period)
                self.assertEqual(r['execution']['ledger'][0]['snapshot']['slot_id'],4)
                self.assertEqual(next(d['slot_id'] for d in r['decisions'] if d['action']=='SELL'),expected)

    def test_holding_exit_does_not_wait_for_signal_rewarmup(self):
        b=recording(['.4','.42','.45','.46','.47','.48','.49'])
        b=replace(b,snapshots=tuple(replace(s,source_us=s.source_us-10000000) if s.slot_id==5 else s for s in b.snapshots))
        r=self.run_strategy(b,holding_period=1)
        self.assertEqual(next(d for d in r['decisions'] if d['slot_id']==6)['action'],'SELL')

    def test_unsafe_gap_rejects_pending_entry(self):
        b=recording(['.4','.42','.45','.46','.47'])
        b=replace(b,snapshots=tuple(s for s in b.snapshots if s.slot_id!=4),opportunities=tuple(
            dict(o,start_ns=None,reason='UNSAFE_MISSING_SLOT_TIMING') if o['slot_id']==4 else o for o in b.opportunities))
        r=self.run_strategy(b)
        self.assertEqual(r['execution']['ledger'][0]['reason'],'UNSAFE_MISSING_SLOT_TIMING')
        self.assertEqual(r['decisions'][-1]['positions']['UP']['quantity'],'0')

    def test_deterministic_ambient_decimal_and_bad_config(self):
        a = self.run_strategy()
        with localcontext() as ctx:
            ctx.prec=6
            self.assertEqual(a,self.run_strategy())
        for kw in ({'fee_bps':'NaN'},{'max_order':'0'},{'lookback':2},{'max_positions':2},{'delay_us':-1},{'holding_period':0},{'threshold':'0'}):
            with self.subTest(kw=kw),self.assertRaises(ValueError):self.run_strategy(**kw)


class CliTests(OfflineTests):
    def test_cli_writes_deterministic_reports_without_overwrite(self):
        from polymarket_bot import strategy
        from tests.test_paper import recorded_fixture
        from pathlib import Path
        import tempfile,json
        from contextlib import redirect_stdout,redirect_stderr
        import io
        self.assertTrue(callable(getattr(strategy,'main',None)),'offline CLI missing')
        f=recorded_fixture(self)
        run=f.rows('SELECT run_id FROM recording_runs')[0]['run_id']
        with tempfile.TemporaryDirectory() as temp:
            outputs=[]
            for name in ('one','two'):
                out=Path(temp)/name
                args=['--db',str(f.db.path),'--run-id',run,'--market-id','1001','--output-dir',str(out)]
                with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                    self.assertEqual(strategy.main(args),0)
                    self.assertEqual(strategy.main(args),2)
                outputs.append({p.name:p.read_bytes() for p in out.iterdir()})
            self.assertEqual(outputs[0],outputs[1])
            report=json.loads(outputs[0]['report.json'])
            self.assertEqual(report['mode'],'OFFLINE_PAPER_ONLY')
            self.assertEqual(report['configuration']['max_order'],'10')
            bad=Path(temp)/'bad'
            with redirect_stderr(io.StringIO()):
                self.assertEqual(strategy.main(args[:-1]+[str(bad),'--fee-bps','NaN']),2)
            self.assertFalse(bad.exists())


if __name__=='__main__':unittest.main()
