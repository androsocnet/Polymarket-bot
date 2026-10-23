"""Deterministic OFFLINE momentum baseline; hypothetical orders only."""
import argparse
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from decimal import Decimal, ROUND_FLOOR, localcontext

from .paper import ARITHMETIC, ZERO, _fresh, _number, _proof, _require, _text, load_recording, simulate
from .market_data.models import MarketDataError


def load_strategy_recording(db, run_id, market_id):
    """Add rejection availability from authoritative slot ends; CP6 loader unchanged."""
    recording=load_recording(db,run_id,market_id)
    with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        ends=dict(conn.execute('SELECT slot_id,end_mono_ns FROM sample_slots WHERE run_id=? AND market_id=?',(run_id,market_id)))
    _require(sha256(db.read_bytes()).hexdigest()==recording.db_sha256,'source database changed during read')
    return replace(recording,opportunities=tuple(dict(o,available_ns=ends[o['slot_id']]) for o in recording.opportunities))


def run_strategy(recording, *, initial_cash='100', max_order='10', max_exposure='10',
                 max_positions=1, fee_bps='10', threshold='0.02', lookback=3,
                 holding_period=3, delay_us=0, max_age_us=5_000_000):
    """Only revealed paired observations and settled fills reach decision logic."""
    settings = dict(initial_cash=initial_cash,max_order=max_order,max_exposure=max_exposure,
                    fee_bps=fee_bps,threshold=threshold)
    for name,value in settings.items():
        settings[name] = _number(value,name,positive=name!='fee_bps')
    _require(settings['fee_bps']<=10000,'invalid fee_bps')
    for name,value,minimum in [('lookback',lookback,3),('holding_period',holding_period,1),
                               ('delay_us',delay_us,0),('max_age_us',max_age_us,1)]:
        _require(type(value) is int and value>=minimum,'invalid '+name)
    _require(type(max_positions) is int and max_positions in (0,1),'max_positions must be 0 or 1')
    with localcontext(ARITHMETIC):
        return _run(recording,settings,max_positions,lookback,holding_period,delay_us,max_age_us)


def _run(recording, settings, max_positions, lookback, holding_period, delay_us, max_age_us):
    config = {k:_text(v) for k,v in settings.items()}
    config.update(max_positions=max_positions,lookback=lookback,holding_period=holding_period,
                  delay_us=delay_us,max_age_us=max_age_us,max_wait_us=5_000_000,quantity_step='0.01')
    orders, decisions, history = [], [], []
    pending, held_age, held_since, last_index = None, 0, None, -1
    options = dict(initial_cash=config['initial_cash'],fee_bps=config['fee_bps'],delay_us=delay_us,max_age_us=max_age_us)
    groups = {}
    for s in recording.snapshots:
        groups.setdefault(s.slot_id,{})[s.outcome] = s
    availability={(s.slot_id,s.outcome):s.available_ns for s in recording.snapshots}
    pairs = {slot:pair for slot,pair in groups.items() if set(pair)=={'UP','DOWN'}}
    pair_times = {slot:max(s.available_ns for s in pair.values()) for slot,pair in pairs.items()}
    times = sorted({s.available_ns for s in recording.snapshots} | {o['available_ns'] for o in recording.opportunities if o.get('available_ns') is not None} | set(pair_times.values()) |
                   {o['start_ns'] for o in recording.opportunities if o['start_ns'] is not None and o['reason'] is not None})
    empty = replace(recording,snapshots=(),opportunities=(),excluded=())
    state = simulate(empty,[],**options)
    for now in times:
        revealed = tuple(s for s in recording.snapshots if s.available_ns<=now)
        index = max((s.slot_index for s in revealed),default=-1)
        # An unavailable VALID book remains a pending execution, never later liquidity.
        visible = {(s.slot_id,s.outcome) for s in revealed}
        opportunities = tuple(dict(o,reason='AWAITING_EXECUTION_AVAILABILITY')
            if (o['reason'] is None and (o['slot_id'],o['outcome']) not in visible) or
               (o.get('available_ns',availability.get((o['slot_id'],o['outcome']))) is not None and
                o.get('available_ns',availability.get((o['slot_id'],o['outcome'])))>now) else o
            for o in recording.opportunities if
            (o['start_ns'] is not None and o['start_ns']<=now) or
            (o['start_ns'] is None and o['slot_index']<=index))
        prefix = replace(recording,snapshots=revealed,opportunities=opportunities,excluded=(),horizon_ns=now)
        if pending is not None:
            # ponytail: bounded offline replay; use accepted incremental engine if recordings grow.
            candidate = simulate(prefix,orders,**options)
            trade = next(t for t in candidate['ledger'] if t['id']==pending)
            if trade['reason'] not in ('NO_FUTURE_SNAPSHOT','AWAITING_EXECUTION_AVAILABILITY'):
                state, pending = candidate, None
                if trade.get('side')=='BUY' and Decimal(trade['filled_quantity'])>0:
                    held_age, held_since = 0, now
        for slot in sorted((slot for slot,t in pair_times.items() if t==now),key=lambda slot:pairs[slot]['UP'].slot_index):
            pair = pairs[slot]
            positions = {o:{k:p[k] for k in ('quantity','cost_basis')} for o,p in state['positions'].items()}
            exposure = sum((Decimal(p['cost_basis']) for p in positions.values()),ZERO)
            d = dict(slot_id=slot,available_mono_ns=now,action='HOLD',reason=None,signal='HOLD',
                     inputs=[_proof(pair[o]) for o in ('UP','DOWN')],midpoints={},momentum=None,
                     cash=state['cash'],exposure=_text(exposure),positions=positions,instruction=None)
            fresh = all(_fresh(s,now_us=s.receipt_us+(now-s.receipt_ns)//1000,max_age_us=max_age_us) and
                        s.book.best_bid is not None and s.book.best_ask is not None for s in pair.values())
            if pair['UP'].slot_index<=last_index:
                d['reason']='OUT_OF_ORDER_PAIR'
            elif not fresh:
                history=[]; d['reason']='STALE_PAIR'
            else:
                last_index=pair['UP'].slot_index
                if held_since is not None and now>held_since and any(Decimal(p['quantity'])>0 for p in positions.values()):
                    held_age += 1
                d['midpoints']={o:_text((s.book.best_bid+s.book.best_ask)/2) for o,s in pair.items()}
                history.append(dict(slot_id=slot,midpoint=d['midpoints']['UP'],inputs=d['inputs']))
                history=history[-lookback:]
                d['lookback_inputs']=list(history)
                if len(history)<lookback:
                    d['reason']='WARMUP'
                else:
                    momentum=Decimal(history[-1]['midpoint'])-Decimal(history[0]['midpoint'])
                    d['momentum']=_text(momentum)
                    d['signal']='UP' if momentum>=settings['threshold'] else 'DOWN' if momentum<=-settings['threshold'] else 'HOLD'
                owned=next((o for o,p in positions.items() if Decimal(p['quantity'])>0),None)
                if pending is not None:
                    d['reason']='PENDING_EXECUTION'
                elif owned:
                    if held_age>=holding_period or d['signal'] not in ('HOLD',owned):
                        d['action']='SELL'; d['reason']='HOLDING_PERIOD' if held_age>=holding_period else 'REVERSAL'
                        outcome,side,quantity=owned,'SELL',positions[owned]['quantity']
                elif d['signal']!='HOLD':
                    quantity=(settings['max_order']/(1+settings['fee_bps']/10000)/Decimal('.01')).to_integral_value(rounding=ROUND_FLOOR)*Decimal('.01')
                    reserve=quantity*(1+settings['fee_bps']/10000)
                    reason=('POSITION_LIMIT' if max_positions==0 else 'ZERO_QUANTITY' if quantity<=0 else
                            'INSUFFICIENT_CASH_RESERVE' if reserve>Decimal(state['cash']) else
                            'EXPOSURE_LIMIT' if exposure+reserve>settings['max_exposure'] else None)
                    d['reason']=reason
                    if reason is None:
                        outcome,side,quantity=d['signal'],'BUY',_text(quantity)
                        d['action']='BUY '+outcome
                if d['action']!='HOLD':
                    instruction=dict(id='strategy-'+str(len(orders)+1),market_id=recording.market_id,
                        token_id=recording.tokens[outcome],side=side,quantity=quantity,decision_slot_id=slot)
                    orders.append(instruction); d['instruction']=instruction.copy(); pending=instruction['id']
            decisions.append(d)
    # Finalization does not feed new decisions; unresolved instructions reject, holdings stay open.
    execution=simulate(recording,orders,**options)
    return dict(schema='cp6-strategy-v1',mode='OFFLINE_PAPER_ONLY',configuration=config,decisions=decisions,
                orders=orders,execution=execution,assumptions=dict(strategy='UP midpoint momentum; paired observations only; no tuning',
                budget='BUY quantity uses $1 ceiling plus assumed fees; reserve checked before submission; no resizing',
                exposure='sum of remaining fee-inclusive cost basis',quantity_step='SIMULATION_ASSUMPTION_NOT_VERIFIED',
                fee='ASSUMED_NOT_VERIFIED',settlement='unknown; never inferred',
                limitations='26-second real capture cannot establish profitability, win rate or 15-minute performance'))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--market-id',required=True)
    parser.add_argument('--output-dir',type=Path,required=True,help='new output directory; never overwrite')
    for name,default in [('initial-cash','100'),('max-order','10'),('max-exposure','10'),('fee-bps','10'),('threshold','0.02')]:
        parser.add_argument('--'+name,default=default)
    for name,default in [('max-positions',1),('lookback',3),('holding-period',3),('delay-us',0),('max-age-us',5_000_000)]:
        parser.add_argument('--'+name,type=int,default=default)
    args=vars(parser.parse_args(argv))
    db,run,market,out=(args.pop(k) for k in ('db','run_id','market_id','output_dir'))
    created=False
    names=('report.json','decisions.jsonl','ledger.jsonl')
    try:
        _require(not out.exists(),'output directory already exists')
        report=run_strategy(load_strategy_recording(db,run,market),**args)
        out.mkdir(); created=True
        for name,data in zip(names,(report,report['decisions'],report['execution']['ledger'])):
            text=json.dumps(data,indent=2,sort_keys=True)+'\n' if name=='report.json' else ''.join(json.dumps(row,sort_keys=True)+'\n' for row in data)
            (out/name).write_text(text,encoding='utf8')
        print('OFFLINE_STRATEGY_ONLY '+json.dumps(report['execution']['counts'],sort_keys=True))
        return 0
    except (ValueError,OSError,sqlite3.Error,MarketDataError) as exc:
        if created:
            for name in names:(out/name).unlink(missing_ok=True)
            out.rmdir()
        print('strategy: '+str(exc),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
