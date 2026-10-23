"""Offline, long-only paper IOC replay. No network client or real order path."""
import argparse
from contextlib import closing
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys

from .market_data.clob import parse_book
from .market_data.http import JsonRead
from .market_data.models import MarketDataError, OutcomeBook
from .market_data.numeric import load_numeric_json, parse_decimal
from .recorder.store import EvidenceError, decode_evidence


@dataclass(frozen=True)
class Snapshot:
    slot_id: int
    slot_index: int
    outcome: str
    request_id: int
    start_ns: int
    available_ns: int
    receipt_us: int
    source_us: int
    uncertainty_us: int
    raw_sha256: str
    book: OutcomeBook
    receipt_ns: int


@dataclass(frozen=True)
class Recording:
    run_id: str
    market_id: str
    condition_id: str
    tokens: dict
    db_sha256: str
    snapshots: tuple[Snapshot, ...]
    excluded: tuple[dict, ...]
    horizon_ns: int
    horizon_us: int
    opportunities: tuple[dict, ...]


def _require(ok, reason):
    if not ok:
        raise ValueError(reason)


def _missing_slot_start(slot, anchors, slots):
    """Replay calibrations causally; a response cannot revise an already due target."""
    start, previous = None, None
    for a in sorted(anchors.values(),key=lambda a:(a['request_end_mono_ns'],a['anchor_id'])):
        end = a['request_end_mono_ns']
        if start is not None and end > start:
            break
        projected = a['anchor_mono_ns']+(slot['scheduled_us']-a['anchor_server_us'])*1000
        if (a['status']!='VALID' or
                not a['request_start_mono_ns'] <= a['anchor_mono_ns'] <= end or
                (previous is not None and a['request_start_mono_ns'] < previous)):
            return None
        if projected < end:
            return None
        start, previous = projected, end
    if start is None:
        return None
    # Clock corrections must not move a grid hole across recorded neighboring captures.
    if any((s['slot_index'] < slot['slot_index'] and s['end_mono_ns'] is not None and s['end_mono_ns'] >= start) or
           (s['slot_index'] > slot['slot_index'] and s['start_mono_ns'] is not None and s['start_mono_ns'] <= start)
           for s in slots):
        return None
    return start


def load_recording(db: Path, run_id: str, market_id: str) -> Recording:
    """Read terminal CP5 recording, reparse full evidence; never open writer."""
    db = Path(db).resolve(strict=True)
    _require(db.is_file(), 'database must be file')
    wal = Path(str(db)+'-wal')
    _require(not wal.exists() or wal.stat().st_size == 0, 'active WAL not supported; use closed recording')
    digest = sha256(db.read_bytes()).hexdigest()
    snapshots, excluded, opportunities = [], [], []
    with closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        _require(conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'database integrity failed')
        _require(not conn.execute('PRAGMA foreign_key_check').fetchall(), 'foreign keys failed')
        run = conn.execute('SELECT * FROM recording_runs WHERE run_id=?', (run_id,)).fetchone()
        market = conn.execute('SELECT * FROM markets WHERE market_id=?', (market_id,)).fetchone()
        _require(run is not None and market is not None, 'unknown run/market')
        _require(run['state'] in ('STOPPED','FAILED','INTERRUPTED') and run['ended_wall_us'] is not None,
                 'recording must be terminal')
        config = json.loads(run['config_json'])
        _require(config.get('selected_market_id') == market_id and
                 config.get('selected_event_id') == market['event_id'], 'selected run identity mismatch')
        tokens = {'UP':market['up_token_id'], 'DOWN':market['down_token_id']}
        condition = market['condition_id']
        _require(tokens['UP'] != tokens['DOWN'], 'token collision')
        slots = conn.execute('SELECT * FROM sample_slots WHERE run_id=? AND market_id=? ORDER BY start_mono_ns,slot_id',
                             (run_id,market_id)).fetchall()
        anchors = {r['anchor_id']: r for r in conn.execute('SELECT * FROM clock_anchors WHERE run_id=?', (run_id,))}
        revisions = {}
        horizon_ns = max((s['end_mono_ns'] or 0 for s in slots), default=0)
        _require(horizon_ns > 0 and anchors, 'no timed recording')
        anchor = max((a for a in anchors.values() if a['status']=='VALID'), key=lambda a:a['anchor_mono_ns'], default=None)
        _require(anchor is not None, 'no valid anchor')
        horizon_us = anchor['anchor_server_us']+(horizon_ns-anchor['anchor_mono_ns'])//1000
        for slot in slots:
            # Preserve failed/missing slots; scheduled grid supplies time if capture never began.
            slot_start = slot['start_mono_ns']
            if slot_start is None:
                slot_start = _missing_slot_start(slot,anchors,slots)
            slot_opportunities = {outcome:dict(slot_id=slot['slot_id'],slot_index=slot['slot_index'],outcome=outcome,start_ns=slot_start,
                                               reason='MISSING_EXECUTION_BOOK' if slot_start is not None else 'UNSAFE_MISSING_SLOT_TIMING') for outcome in tokens}
            opportunities.extend(slot_opportunities.values())
            observations = conn.execute('SELECT * FROM book_observations WHERE slot_id=? ORDER BY outcome', (slot['slot_id'],)).fetchall()
            for outcome in tokens.keys()-{o['outcome'] for o in observations}:
                attempts = conn.execute("SELECT start_mono_ns,selector_json FROM request_attempts WHERE run_id=? AND market_id=? AND slot_id=? AND operation IN ('BOOK_BATCH','BOOK_SINGLE') ORDER BY start_mono_ns",
                    (run_id,market_id,slot['slot_id'])).fetchall()
                for attempt in attempts:
                    try:
                        selectors = json.loads(attempt['selector_json'])
                    except (ValueError,TypeError):
                        continue
                    if isinstance(selectors,list) and tokens[outcome] in selectors:
                        slot_opportunities[outcome]['start_ns'] = attempt['start_mono_ns']
                        break
            if not observations:
                excluded.append({'slot_id':slot['slot_id'], 'reason':'NO_OBSERVATIONS'})
            elif slot['status']=='COMPLETE':
                for outcome in sorted(tokens.keys()-{o['outcome'] for o in observations}):
                    excluded.append({'slot_id':slot['slot_id'], 'outcome':outcome, 'reason':'MISSING_OBSERVATION'})
            for obs in observations:
                opportunity = slot_opportunities[obs['outcome']]
                attempt_start = conn.execute('SELECT start_mono_ns FROM request_attempts WHERE request_id=? AND run_id=? AND market_id=? AND slot_id=?',
                    (obs['request_id'],run_id,market_id,slot['slot_id'])).fetchone()
                if attempt_start is not None:
                    opportunity['start_ns'] = attempt_start[0]
                opportunity['reason'] = 'INVALID_EXECUTION_BOOK'
                try:
                    _require(slot['status']=='COMPLETE' and obs['status']=='VALID', 'slot/book not COMPLETE/VALID')
                    token = tokens[obs['outcome']]
                    _require(obs['expected_token_id']==token and obs['expected_condition_id']==condition, 'observation identity mismatch')
                    a = anchors[slot['anchor_id']]
                    _require(a['status']=='VALID', 'invalid clock anchor')
                    revision_id = slot['metadata_revision_id']
                    if revision_id not in revisions:
                        rev = conn.execute('SELECT * FROM market_revisions WHERE revision_id=?', (revision_id,)).fetchone()
                        _require(rev is not None and rev['market_id']==market_id and rev['run_id']==run_id, 'revision identity mismatch')
                        _require(rev['active']==1 and rev['closed']==0 and rev['accepting_orders']==1 and
                                 rev['enable_order_book']==1, 'revision not open for books')
                        sizes = json.loads(rev['raw_sizes_json'])
                        gamma = load_numeric_json(decode_evidence(rev['gamma_raw_z'],sizes['gamma'],rev['codec_version']))
                        _require(gamma['id']==market_id and gamma['conditionId']==condition, 'Gamma identity mismatch')
                        outcomes = json.loads(gamma['outcomes']) if isinstance(gamma['outcomes'],str) else gamma['outcomes']
                        ids = json.loads(gamma['clobTokenIds']) if isinstance(gamma['clobTokenIds'],str) else gamma['clobTokenIds']
                        _require(dict(zip((o.upper() for o in outcomes),ids))==tokens and len(ids)==len(outcomes)==2, 'Gamma token mismatch')
                        clob = load_numeric_json(decode_evidence(rev['clob_config_raw_z'],sizes['clob'],rev['codec_version']))
                        _require(isinstance(clob,dict) and any(k in clob for k in ('condition_id','conditionId','c')), 'missing CLOB condition')
                        _require(all(clob[k]==condition for k in ('condition_id','conditionId','c') if k in clob), 'CLOB condition mismatch')
                        _require(isinstance(clob.get('t'),list) and len(clob['t'])==2 and
                                 sorted(t['t'] for t in clob['t'])==sorted(tokens.values()), 'CLOB token mismatch')
                        for row in clob['t']:
                            if row.get('o') in ('Up','Down','UP','DOWN'):
                                _require(tokens[row['o'].upper()]==row['t'], 'CLOB outcome mismatch')
                        revisions[revision_id] = True
                    attempt = conn.execute('SELECT * FROM request_attempts WHERE request_id=?', (obs['request_id'],)).fetchone()
                    _require(attempt is not None and attempt['run_id']==run_id and attempt['market_id']==market_id and
                             attempt['slot_id']==slot['slot_id'] and attempt['anchor_id']==slot['anchor_id'], 'audit linkage mismatch')
                    _require(attempt['http_status']==200 and attempt['result']=='success' and attempt['raw_complete']==1, 'incomplete audit')
                    _require((attempt['method'],attempt['operation'],attempt['path']) in
                             (('POST','BOOK_BATCH','/books'),('GET','BOOK_SINGLE','/book')), 'not book request')
                    _require(slot['start_mono_ns'] <= attempt['start_mono_ns'] <= attempt['end_mono_ns'] <= slot['end_mono_ns'], 'bad availability ordering')
                    raw = decode_evidence(attempt['response_raw_z'],attempt['raw_size_bytes'],attempt['codec_version'])
                    _require(len(raw)==attempt['response_bytes'], 'raw length mismatch')
                    payload = load_numeric_json(raw)
                    if attempt['method']=='POST':
                        _require(isinstance(payload,list), 'batch not list')
                        matches = [b for b in payload if isinstance(b,dict) and b.get('asset_id')==token]
                        _require(len(matches)==1, 'missing/duplicate token')
                        payload = matches[0]
                    book = parse_book(JsonRead(payload,0,0), token_id=token, condition_id=condition)
                    source_us = int(book.source_timestamp_raw)*1000
                    _require(source_us==obs['source_us'] and book.source_timestamp_raw==obs['source_timestamp_raw'], 'source mismatch')
                    for name in ('best_bid','best_ask','bid_size','ask_size'):
                        text = obs[name+'_text']
                        _require((None if text is None else parse_decimal(text,field=name))==getattr(book,name), 'summary mismatch')
                    receipt_us = a['anchor_server_us']+(attempt['end_mono_ns']-a['anchor_mono_ns'])//1000
                    _require(attempt['ended_server_us']==receipt_us, 'receipt clock mismatch')
                    _require(market['window_start_us'] <= receipt_us < market['window_end_us'], 'outside market window')
                    snapshots.append(Snapshot(slot['slot_id'],slot['slot_index'],obs['outcome'],obs['request_id'],
                        attempt['start_mono_ns'],slot['end_mono_ns'],receipt_us,source_us,a['uncertainty_us'],sha256(raw).hexdigest(),book,
                        attempt['end_mono_ns']))
                    opportunity['reason'] = None
                except (ValueError, TypeError, KeyError, MarketDataError, EvidenceError) as exc:
                    excluded.append({'slot_id':slot['slot_id'], 'outcome':obs['outcome'], 'reason':str(exc)})
    _require(sha256(db.read_bytes()).hexdigest()==digest, 'source database changed during read')
    _require(bool(snapshots), 'no validated snapshots')
    return Recording(run_id,market_id,condition,tokens,digest,tuple(sorted(snapshots,key=lambda s:(s.start_ns,s.request_id,s.outcome))),
                     tuple(excluded),horizon_ns,horizon_us,
                     tuple(sorted(opportunities,key=lambda o:(-1 if o['start_ns'] is None else o['start_ns'],o['slot_id'],o['outcome']))))


ZERO = Decimal(0)
ARITHMETIC = Context(prec=512, rounding=ROUND_HALF_EVEN)


def _number(value, name, *, positive=False):
    try:
        number = parse_decimal(value, field=name)
    except MarketDataError as exc:
        raise ValueError('invalid '+name) from exc
    _require(number >= 0 and (not positive or number > 0), 'invalid '+name)
    return number


def _text(value):
    if value is None:
        return None
    if value == 0:
        return '0'
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _proof(snapshot):
    return dict(slot_id=snapshot.slot_id,slot_index=snapshot.slot_index,request_id=snapshot.request_id,
                outcome=snapshot.outcome,token_id=snapshot.book.token_id,condition_id=snapshot.book.condition_id,
                request_start_mono_ns=snapshot.start_ns,available_mono_ns=snapshot.available_ns,
                receipt_server_us=snapshot.receipt_us,source_us=snapshot.source_us,
                source_timestamp_raw=snapshot.book.source_timestamp_raw,raw_sha256=snapshot.raw_sha256)


def _fresh(snapshot, *, now_us, max_age_us):
    return (-snapshot.uncertainty_us <= now_us-snapshot.source_us <= max_age_us and
            0 <= now_us-snapshot.receipt_us <= max_age_us)


def simulate(recording: Recording, orders, *, initial_cash, fee_bps,
             delay_us=0, max_wait_us=5_000_000, max_age_us=5_000_000):
    """Manual snapshot decisions; next-request market IOC, no settlement model."""
    cash = _number(initial_cash,'initial_cash')
    fees = _number(fee_bps,'fee_bps')
    _require(fees <= 10000, 'fee_bps exceeds 10000')
    for name,value in (('delay_us',delay_us),('max_wait_us',max_wait_us),('max_age_us',max_age_us)):
        _require(type(value) is int and value >= (0 if name=='delay_us' else 1), 'invalid '+name)
    _require(isinstance(orders,list) and len(orders)<=10000, 'orders must be list, maximum 10000')
    with localcontext(ARITHMETIC):
        return _simulate(recording,orders,cash,fees,delay_us,max_wait_us,max_age_us)


def _simulate(recording, orders, initial_cash, fees, delay_us, max_wait_us, max_age_us):
    cash, realized, total_fees = initial_cash, ZERO, ZERO
    positions = {o:{'quantity':ZERO,'cost_basis':ZERO} for o in recording.tokens}
    events, ledger, consumed, seen = [], [], {}, set()
    for sequence,instruction in enumerate(orders):
        entry = dict(sequence=sequence,id=None,status='REJECTED',reason=None,filled_quantity='0',
                     fee='0',fills=[],snapshot=None)
        event_time, decision_time, future, decision = -1, -1, None, None
        try:
            _require(isinstance(instruction,dict), 'INVALID_INSTRUCTION')
            entry['requested'] = instruction.copy()
            required = {'id','market_id','token_id','side','quantity','decision_slot_id'}
            _require(set(instruction)==required, 'INVALID_INSTRUCTION_FIELDS')
            name = instruction['id']
            _require(isinstance(name,str) and 0<len(name)<=128 and name not in seen, 'INVALID_OR_DUPLICATE_ID')
            seen.add(name); entry['id'] = name
            _require(instruction['market_id']==recording.market_id, 'WRONG_MARKET')
            outcome = next((o for o,t in recording.tokens.items() if t==instruction['token_id']),None)
            _require(outcome is not None, 'WRONG_TOKEN')
            _require(instruction['side'] in ('BUY','SELL'), 'INVALID_SIDE')
            quantity = _number(instruction['quantity'],'quantity',positive=True)
            _require(type(instruction['decision_slot_id']) is int, 'INVALID_DECISION_SLOT')
            same_slot = [s for s in recording.snapshots if s.slot_id==instruction['decision_slot_id']]
            decision = next((s for s in same_slot if s.outcome==outcome),None)
            _require(decision is not None, 'MISSING_DECISION_BOOK')
            decision_time = max(s.available_ns for s in same_slot)
            event_time = decision_time
            entry.update(outcome=outcome,quantity=_text(quantity),side=instruction['side'],decision=_proof(decision))
            decision_us = decision.receipt_us+(decision_time-decision.receipt_ns)//1000
            _require(_fresh(decision,now_us=decision_us,max_age_us=max_age_us), 'STALE_DECISION_BOOK')
            ready = decision_time+delay_us*1000
            opportunity = next((o for o in recording.opportunities if o['outcome']==outcome and
                                o['start_ns'] is not None and o['start_ns']>decision_time and o['start_ns']>=ready),None)
            unsafe = next((o for o in recording.opportunities if o['outcome']==outcome and
                           o['start_ns'] is None and decision.slot_index < o['slot_index'] and
                           (opportunity is None or o['slot_index'] <= opportunity['slot_index'])),None)
            if unsafe is not None:
                entry['opportunity'] = unsafe.copy()
                raise ValueError('UNSAFE_MISSING_SLOT_TIMING')
            _require(opportunity is not None,'NO_FUTURE_SNAPSHOT')
            event_time = opportunity['start_ns']
            if opportunity['reason'] is not None:
                entry['opportunity'] = opportunity.copy()
                raise ValueError(opportunity['reason'])
            future = next((s for s in recording.snapshots if s.slot_id==opportunity['slot_id'] and s.outcome==outcome),None)
            _require(future is not None,'MISSING_EXECUTION_BOOK')
            event_time = future.start_ns
            entry['snapshot'] = _proof(future)
            _require(future.start_ns-ready<=max_wait_us*1000 and
                     _fresh(future,now_us=future.receipt_us,max_age_us=max_age_us), 'STALE_OR_MISSING_SNAPSHOT')
        except (ValueError, TypeError) as exc:
            entry['reason'] = str(exc)
        events.append((event_time,decision_time,sequence,entry,future,decision))
    for _,decision_time,sequence,entry,snapshot,decision in sorted(events,key=lambda e:e[:3]):
        if entry['reason'] is None:
            side, outcome = entry['side'], entry['outcome']
            requested = Decimal(entry['quantity'])
            position = positions[outcome]
            key = (snapshot.request_id,snapshot.book.token_id,side)
            if key not in consumed:
                depth = snapshot.book.asks if side=='BUY' else snapshot.book.bids
                levels = {}
                for level in depth:
                    levels[level.price] = levels.get(level.price,ZERO)+level.size
                consumed[key] = [[p,levels[p]] for p in sorted(levels,reverse=side=='SELL') if levels[p]>0]
            depth = consumed[key]
            remaining, fills, notional = requested, [], ZERO
            for price,available in depth:
                take = min(remaining,available)
                if take>0:
                    fills.append({'price':_text(price),'quantity':_text(take),'notional':_text(price*take)})
                    remaining -= take; notional += price*take
                if remaining==0:
                    break
            filled = requested-remaining
            fee = notional*fees/Decimal(10000)
            reason = None
            if side=='SELL' and requested>position['quantity']:
                reason = 'INSUFFICIENT_POSITION'
            elif filled==0:
                reason = 'INSUFFICIENT_LIQUIDITY'
            elif side=='BUY' and notional+fee>cash:
                reason = 'INSUFFICIENT_CASH'
            if reason:
                entry['reason'] = reason
            else:
                # Consume only committed fills; rejected instructions leave depth/cash unchanged.
                by_price = {Decimal(f['price']):Decimal(f['quantity']) for f in fills}
                for level in depth:
                    level[1] -= by_price.get(level[0],ZERO)
                if side=='BUY':
                    cash -= notional+fee
                    position['quantity'] += filled
                    position['cost_basis'] += notional+fee
                    trade_realized = ZERO
                else:
                    allocated = position['cost_basis'] if filled==position['quantity'] else position['cost_basis']*filled/position['quantity']
                    trade_realized = notional-fee-allocated
                    realized += trade_realized
                    cash += notional-fee
                    position['quantity'] -= filled
                    position['cost_basis'] -= allocated
                total_fees += fee
                observed = snapshot.book.best_ask if side=='BUY' else snapshot.book.best_bid
                decision_price = decision.book.best_ask if side=='BUY' else decision.book.best_bid
                sign = Decimal(1) if side=='BUY' else Decimal(-1)
                entry.update(status='FILLED' if remaining==0 else 'PARTIAL',reason=None if remaining==0 else 'INSUFFICIENT_LIQUIDITY',
                    filled_quantity=_text(filled),unfilled_quantity=_text(remaining),fills=fills,notional=_text(notional),fee=_text(fee),
                    vwap=_text(notional/filled),observed_best_price=_text(observed),decision_best_price=_text(decision_price),
                    depth_impact=_text(sign*(notional-observed*filled)),
                    decision_slippage=None if decision_price is None else _text(sign*(notional-decision_price*filled)),
                    realized_pnl=_text(trade_realized))
        entry.update(cash_after=_text(cash),positions_after={o:{k:_text(v) for k,v in p.items()} for o,p in positions.items()})
        ledger.append(entry)
    valued, unrealized, marks_missing = {}, ZERO, []
    for outcome,position in positions.items():
        candidates = [s for s in recording.snapshots if s.outcome==outcome and s.available_ns<=recording.horizon_ns]
        last = max(candidates,key=lambda s:s.available_ns,default=None)
        valid = last is not None and _fresh(last,now_us=recording.horizon_us,max_age_us=max_age_us) and last.book.best_bid is not None
        mark = last.book.best_bid if valid else None
        pnl = (mark*position['quantity']-position['cost_basis']) if mark is not None else (ZERO if position['quantity']==0 else None)
        if pnl is None:
            marks_missing.append(outcome)
        else:
            unrealized += pnl
        valued[outcome] = dict(token_id=recording.tokens[outcome],quantity=_text(position['quantity']),cost_basis=_text(position['cost_basis']),
            average_cost=_text(position['cost_basis']/position['quantity']) if position['quantity'] else None,
            indicative_bid_mark=_text(mark),mark_evidence=_proof(last) if valid else None,unrealized_pnl=_text(pnl))
    return dict(schema='cp6-paper-v1',mode='OFFLINE_PAPER_ONLY',source=dict(run_id=recording.run_id,market_id=recording.market_id,
        condition_id=recording.condition_id,db_sha256=recording.db_sha256,validated_snapshots=len(recording.snapshots),
        excluded=list(recording.excluded)),assumptions=dict(fee_bps=_text(fees),fee_status='ASSUMED_NOT_VERIFIED',
        fee_model='notional_bps_both_sides; buy fees capitalized',execution='market IOC, strictly later request; no shorting',
        delay_us=delay_us,max_wait_us=max_wait_us,max_source_age_us=max_age_us,
        liquidity='shared within request/token/side; new snapshots replenish exogenously; no persistent market impact',
        slippage='observed depth VWAP; decision quote movement separate; no extra slippage added',
        precision='512 significant Decimal digits, ROUND_HALF_EVEN; average cost/divisions may round',
        valuation='latest fresh best bid at replay end, indicative only; ignores exit fees and liquidation depth',
        batch_atomic=False),counts=dict(requested=len(orders),filled=sum(t['status']=='FILLED' for t in ledger),
        partial=sum(t['status']=='PARTIAL' for t in ledger),rejected=sum(t['status']=='REJECTED' for t in ledger)),
        initial_cash=_text(initial_cash),cash=_text(cash),fees_paid=_text(total_fees),positions=valued,realized_pnl=_text(realized),
        unrealized_pnl=None if marks_missing else _text(unrealized),total_pnl=None if marks_missing else _text(realized+unrealized),
        missing_valuation=marks_missing,settlement=None,limitations=['No settlement or verified reference price',
        'Short recording demonstrates mechanics, not strategy profitability','No queue position, hidden liquidity or future fill guarantee'],ledger=ledger)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--market-id',required=True)
    parser.add_argument('--orders',type=Path,required=True,help='manual JSON decisions; no real orders')
    parser.add_argument('--initial-cash',required=True)
    parser.add_argument('--fee-bps',required=True,help='assumed notional fee; real fees remain unverified')
    parser.add_argument('--delay-us',type=int,default=0)
    parser.add_argument('--max-wait-us',type=int,default=5_000_000)
    parser.add_argument('--max-age-us',type=int,default=5_000_000)
    parser.add_argument('--output-dir',type=Path,required=True,help='new directory; never overwrite')
    args = parser.parse_args(argv)
    created = False
    try:
        _require(not args.output_dir.exists(), 'output directory already exists')
        _require(args.orders.stat().st_size<=10*1024*1024, 'orders file too large')
        orders = json.loads(args.orders.read_text(encoding='utf8'))
        recording = load_recording(args.db,args.run_id,args.market_id)
        report = simulate(recording,orders,initial_cash=args.initial_cash,fee_bps=args.fee_bps,
                          delay_us=args.delay_us,max_wait_us=args.max_wait_us,max_age_us=args.max_age_us)
        args.output_dir.mkdir()
        created = True
        (args.output_dir/'report.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n',encoding='utf8')
        (args.output_dir/'ledger.jsonl').write_text(''.join(json.dumps(t,sort_keys=True)+'\n' for t in report['ledger']),encoding='utf8')
        print('OFFLINE_PAPER_ONLY '+json.dumps(report['counts'],sort_keys=True))
        return 0
    except (ValueError,OSError,sqlite3.Error,MarketDataError) as exc:
        if created:
            # Only remove our own newly created outputs, never a preexisting directory.
            for name in ('report.json','ledger.jsonl'):
                (args.output_dir/name).unlink(missing_ok=True)
            args.output_dir.rmdir()
        print('paper: '+str(exc),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
