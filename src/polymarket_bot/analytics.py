"""Offline PAPER_ONLY ledger analytics. No engine, network or writable store imports."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3

ZERO = Decimal(0)
ARITHMETIC = Context(prec=512)
TOLERANCE = Decimal('1e-480')  # Only accommodates the store's 512-digit proportional basis rounding.
TABLES = ('runs', 'accounts', 'windows', 'orders', 'positions', 'settlements',
          'decisions', 'observations', 'equity', 'events')
ASSUMPTIONS = [
    'PAPER_ONLY recorded executions, not exchange-confirmed fills or fees.',
    'One fill means one terminal FILLED/PARTIAL order aggregate; depth-level fills are not persisted.',
    'Trade means one market/outcome position episode from flat to flat; weighted-average basis matches store.',
    'BUY fees enter basis; SELL realized and settlement realized are already net. Never subtract fees twice.',
    'UNKNOWN_SETTLEMENT stays open; no inferred winner, payout or loss.',
    'Groups describe CLOSED trades; realized_by_exit_reason includes partial exits from open trades.',
    'Spread/slippage costs cannot be isolated reliably from persisted top-of-book summaries.',
    'Equity is sampled bid-mark equity, excludes hypothetical liquidation fees; gaps suppress maximum drawdown.',
]


def _d(value, *, nonnegative=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f'missing/invalid financial value: {value!r}') from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise ValueError(f'invalid financial value: {value!r}')
    return result


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    return value


def _stats(trades):
    values = [t['net_pnl'] for t in trades]
    profit = sum((v for v in values if v > 0), ZERO)
    loss = -sum((v for v in values if v < 0), ZERO)
    count = len(values)
    return dict(count=count, wins=sum(v > 0 for v in values), losses=sum(v < 0 for v in values),
                breakeven=sum(v == 0 for v in values), net_pnl=sum(values, ZERO),
                win_rate=Decimal(sum(v > 0 for v in values)) / count if count else None,
                profit_factor=profit / loss if loss else None,
                profit_factor_note=None if loss else 'UNDEFINED_NO_LOSSES',
                expectancy=sum(values, ZERO) / count if count else None)


def _band(value, cuts):
    if value is None:
        return 'MISSING'
    for label, upper in cuts:
        if value < upper:
            return label
    return 'GE_' + str(cuts[-1][1])


def analyze(path: Path, *, bot_id: str | None = None, source_label: str = 'UNVERIFIED_SOURCE') -> dict:
    """Read one consistent account snapshot; never migrate or open a writer connection."""
    with localcontext(ARITHMETIC), closing(sqlite3.connect(
            Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(set(TABLES) - names)
        if any(t not in names for t in ('runs', 'accounts', 'orders', 'positions')):
            raise ValueError('unsupported/incomplete MVP ledger: runs/accounts/orders/positions required')
        accounts = [dict(r) for r in conn.execute('SELECT * FROM accounts ORDER BY bot_id')]
        if bot_id is None:
            if len(accounts) != 1:
                raise ValueError('select --bot-id explicitly for zero/multiple bot accounts')
            bot_id = accounts[0]['bot_id']
        account = next((r for r in accounts if r['bot_id'] == bot_id), None)
        if account is None:
            raise ValueError('bot account not found')
        data = {}
        for table in TABLES:
            data[table] = ([dict(r) for r in conn.execute(
                f'SELECT * FROM "{table}" WHERE bot_id=?', (bot_id,))] if table in names else [])
        if not data['runs'] or any(r['mode'] != 'PAPER_ONLY' or r['schema'] != 'mvp-v1' for r in data['runs']):
            raise ValueError('only mvp-v1 PAPER_ONLY ledgers supported')
        canonical = {k: sorted(v, key=lambda r: json.dumps(r, sort_keys=True)) for k, v in data.items()}
        fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        result = _analyze(data, account)
        result.update(schema='strategy-analytics-v1', bot_id=bot_id, source_label=source_label,
                      scope='bot_account_across_runs', snapshot_sha256=fingerprint,
                      missing_tables=missing, assumptions=ASSUMPTIONS,
                      missing_data=['Visible depth/queue position, exchange fees, measured order-path latency, '
                                    'and reliable decision-time edge estimates are unavailable.',
                                    'Equity missing or incomplete.' if not data['equity'] else
                                    'Equity only proves sampled drawdown, not intracycle extrema.'])
        return _jsonable(result)


def _analyze(data, account):
    errors = []

    def check(left, right, label):
        if abs(left - right) > TOLERANCE:
            errors.append(f'{label}: reconstructed={left} recorded={right}')

    windows = {r['market_id']: r for r in data['windows']}
    decisions = {(r['market_id'], r['slot_id']): r for r in data['decisions']}
    observations = {(r['market_id'], r['slot_index'], r['outcome']): r for r in data['observations']}
    active, closed, realized_events = {}, [], []
    initial = _d(account['initial_cash'], nonnegative=True)
    cash, fees, ledger_realized, notional = initial, ZERO, ZERO, ZERO
    fills = [r for r in data['orders'] if r['status'] in ('FILLED', 'PARTIAL')]
    timeline = [(r['updated_us'], 0, r['created_us'], r['order_id'], r) for r in fills]
    timeline += [(r['settled_us'], 1, 0, r['market_id'], r) for r in data['settlements'] if r['status'] == 'RESOLVED']
    for at, kind, _, _, row in sorted(timeline):
        market = row['market_id']
        if kind == 1:
            winner = row['winning_outcome']
            if winner not in ('UP', 'DOWN'):
                raise ValueError('RESOLVED settlement missing winning outcome')
            payout, pnl = ZERO, ZERO
            count = 0
            for key, trade in list(active.items()):
                if key[0] != market:
                    continue
                paid = trade['quantity'] if trade['outcome'] == winner else ZERO
                realized = paid - trade['remaining_basis']
                payout += paid
                pnl += realized
                trade['net_pnl'] += realized
                trade['proceeds'] += paid
                trade.update(quantity=ZERO, remaining_basis=ZERO, exit_reason='SETTLEMENT', closed_us=at)
                closed.append(trade)
                del active[key]
                count += 1
            check(payout, _d(row['payout_total'], nonnegative=True), f'{market} settlement payout')
            check(pnl, _d(row['realized_pnl']), f'{market} settlement realized')
            if row.get('position_count') is not None and count != row['position_count']:
                errors.append(f'{market} settlement position_count mismatch')
            cash += _d(row['payout_total'], nonnegative=True)
            ledger_realized += _d(row['realized_pnl'])
            realized_events.append((at, 'SETTLEMENT', _d(row['realized_pnl'])))
            continue
        side, outcome = row['side'], row['outcome']
        if side not in ('BUY', 'SELL') or outcome not in ('UP', 'DOWN'):
            raise ValueError('invalid order side/outcome')
        qty = _d(row['filled'], nonnegative=True)
        requested = _d(row['quantity'], nonnegative=True)
        amount, fee = _d(row['notional'], nonnegative=True), _d(row['fee'], nonnegative=True)
        if qty <= 0 or qty > requested or amount > qty or at < row['created_us']:
            raise ValueError('invalid filled order quantity/notional/timing')
        fees += fee
        notional += amount
        key = market, outcome
        decision = decisions.get((market, row['decision_slot_id']))
        reason = decision['reason'] if decision and side == 'SELL' else row['reason']
        if side == 'BUY':
            cash -= amount + fee
            if key not in active:
                window = windows.get(market, {})
                obs = observations.get((market, decision['slot_index'], outcome)) if decision else None
                decision_ask = None
                if (obs and obs['received_us'] <= decision['at_us'] and obs['source_at'] <= decision['at_us']
                        and decision['at_us'] <= row['created_us'] and obs.get('ask') is not None):
                    ask = _d(obs['ask'], nonnegative=True)
                    if 0 < ask <= 1:
                        decision_ask = ask
                active[key] = dict(market_id=market, outcome=outcome, opened_us=at, closed_us=None,
                                   decision_us=decision['at_us'] if decision else None,
                                   decision_ask=decision_ask, market_start_us=window.get('start_us'),
                                   market_end_us=window.get('end_us'), quantity=ZERO, remaining_basis=ZERO,
                                   entry_quantity=ZERO, entry_notional=ZERO, fees=ZERO, proceeds=ZERO,
                                   net_pnl=ZERO, exit_reason=None, entry_count=0)
            trade = active[key]
            trade['entry_count'] += 1
            trade['quantity'] += qty
            trade['entry_quantity'] += qty
            trade['remaining_basis'] += amount + fee
            trade['entry_notional'] += amount
            trade['fees'] += fee
            check(ZERO, _d(row['realized']), f"{row['order_id']} BUY realized")
        else:
            cash += amount - fee
            trade = active.get(key)
            if trade is None or qty > trade['quantity']:
                raise ValueError('SELL without sufficient reconstructed position; full history required')
            allocated = trade['remaining_basis'] * qty / trade['quantity']
            realized = amount - fee - allocated
            check(realized, _d(row['realized']), f"{row['order_id']} SELL realized")
            trade['remaining_basis'] -= allocated
            trade['quantity'] -= qty
            trade['net_pnl'] += realized
            trade['proceeds'] += amount
            trade['fees'] += fee
            trade['exit_reason'] = reason
            ledger_realized += _d(row['realized'])
            realized_events.append((at, reason, _d(row['realized'])))
            if trade['quantity'] == 0:
                trade['closed_us'] = at
                closed.append(trade)
                del active[key]
    recorded_positions = {(r['market_id'], r['outcome']): r for r in data['positions']}
    for key in set(recorded_positions) | set(active):
        row = recorded_positions.get(key, {})
        trade = active.get(key, {})
        for field, column in [('quantity', 'quantity'), ('remaining_basis', 'cost_basis')]:
            check(trade.get(field, ZERO), _d(row.get(column, '0'), nonnegative=True), f'{key} {column}')
    basis = sum((_d(r['cost_basis'], nonnegative=True) for r in data['positions']), ZERO)
    check(cash + basis, initial + ledger_realized, 'account cash + basis = initial + realized')
    check(sum((t['net_pnl'] for t in closed + list(active.values())), ZERO), ledger_realized,
          'episode realized total')
    for trade in closed + list(active.values()):
        trade['entry_price'] = trade['entry_notional'] / trade['entry_quantity']
        trade['gross_pnl'] = trade['proceeds'] - trade['entry_notional'] if trade['closed_us'] is not None else None
        trade['holding_seconds'] = ((Decimal(trade['closed_us'] - trade['opened_us']) / 1_000_000)
                                    if trade['closed_us'] is not None else None)
        end = trade['market_end_us']
        trade['remaining_seconds_at_entry'] = Decimal(end - trade['opened_us']) / 1_000_000 if end is not None else None
        trade['decision_remaining_seconds'] = (Decimal(end - trade['decision_us']) / 1_000_000
                                               if end is not None and trade['decision_us'] is not None else None)
        trade['status'] = 'CLOSED' if trade['closed_us'] is not None else windows.get(trade['market_id'], {}).get('status', 'MISSING_WINDOW')
    groups = {}
    dimensions = {
        'outcome': lambda t: t['outcome'],
        'entry_price': lambda t: _band(t['entry_price'], [('LT_0.25', Decimal('.25')), ('0.25_TO_0.5', Decimal('.5')), ('0.5_TO_0.75', Decimal('.75'))]),
        'exit_reason': lambda t: t['exit_reason'],
        'holding_seconds': lambda t: _band(t['holding_seconds'], [('LT_10', 10), ('10_TO_30', 30), ('30_TO_60', 60)]),
        'remaining_seconds': lambda t: _band(t['remaining_seconds_at_entry'], [('LT_60', 60), ('60_TO_300', 300), ('300_TO_600', 600)]),
    }
    for dimension, label in dimensions.items():
        buckets = defaultdict(list)
        for trade in closed:
            buckets[label(trade)].append(trade)
        groups[dimension] = {key: _stats(value) for key, value in sorted(buckets.items())}
    daily, reasons = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    for at, reason, pnl in realized_events:
        day = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=at)).date().isoformat()
        daily[day] += pnl
        reasons[reason] += pnl
    peak, drawdown, valid = initial, ZERO, 0
    equity = sorted(data['equity'], key=lambda r: (r['at_us'], r['id']))
    for row in equity:
        if row['missing_marks'] != 0 or row['unrealized'] is None:
            continue
        value = _d(row['cash']) + _d(row['exposure']) + _d(row['unrealized'])
        peak = max(peak, value)
        drawdown = max(drawdown, peak - value)
        valid += 1
    diagnostics = dict(order_statuses=dict(Counter(r['status'] for r in data['orders'])),
                       decision_reasons=dict(Counter(r['reason'] for r in data['decisions'])),
                       event_kinds=dict(Counter(r['kind'] for r in data['events'])),
                       window_statuses=dict(Counter(r['status'] for r in data['windows'])),
                       observations=len(data['observations']),
                       execution_delay_us=[r['updated_us'] - r['created_us'] for r in fills],
                       missing_decision_quotes=sum(t['decision_ask'] is None for t in closed + list(active.values())))
    return dict(realized_net_pnl=ledger_realized, fees=fees, filled_notional=notional,
                closed=_stats(closed), fills=dict(count=len(fills), partial=sum(r['status'] == 'PARTIAL' for r in fills),
                buys=sum(r['side'] == 'BUY' for r in fills), sells=sum(r['side'] == 'SELL' for r in fills)),
                account=dict(initial_cash=initial, cash=cash, remaining_basis=basis),
                reconciliation=dict(ok=not errors, errors=errors, absolute_tolerance=TOLERANCE),
                trades=closed, open_trades=list(active.values()), groups=groups,
                biggest_losses=sorted(closed, key=lambda t: (t['net_pnl'], t['opened_us'], t['market_id']))[:10],
                realized_by_utc_day=dict(sorted(daily.items())), realized_by_exit_reason=dict(sorted(reasons.items())),
                equity=dict(points=len(equity), reliable_points=valid, missing_points=len(equity) - valid,
                            sampled_max_drawdown=drawdown if equity and valid == len(equity) else None,
                            coverage='SAMPLED_ONLY' if equity and valid == len(equity) else 'MISSING_OR_GAPPED'),
                diagnostics=diagnostics)


def evaluate(report: dict, *, split_us: int, max_entry_price: Decimal = Decimal('1'),
             min_remaining_seconds: int = 0) -> dict:
    """Fixed, decision-time entry-filter cohorts, NOT counterfactual strategy/portfolio returns."""
    price = _d(max_entry_price)
    if (type(split_us) is not int or split_us <= 0 or not 0 < price <= 1
            or type(min_remaining_seconds) is not int or min_remaining_seconds < 0):
        raise ValueError('invalid split/filter parameters')
    if not report['reconciliation']['ok']:
        raise ValueError('evaluation requires reconciled full-history ledger')
    with localcontext(ARITHMETIC):
        partitions = {'train': [], 'test': []}
        excluded = Counter(open_or_unknown=len(report['open_trades']), boundary_or_missing_time=0)
        for trade in report['trades']:
            start, end = trade['market_start_us'], trade['market_end_us']
            decision = trade['decision_us']
            if (start is None or end is None or decision is None or end <= start
                    or not start <= decision <= trade['opened_us'] < end):
                excluded['boundary_or_missing_time'] += 1
                continue
            # Conservative leakage-free chronology gate: non-strict on market
            # `end` (a market ending exactly at split_us may train) but strict
            # on `closed_us` (a trade settling exactly at the boundary is held
            # out). Any episode spanning the split is excluded regardless.
            if end <= split_us and trade['closed_us'] < split_us:
                partitions['train'].append(trade)
            elif start >= split_us and decision >= split_us:
                partitions['test'].append(trade)
            else:
                excluded['boundary_or_missing_time'] += 1
        results = {}
        for name, trades in partitions.items():
            baseline = [dict(t, net_pnl=_d(t['net_pnl'])) for t in trades]
            candidate, missing = [], 0
            for trade in baseline:
                # ponytail: multi-entry episodes excluded; causal per-entry replay belongs in Phase 3B.
                if (trade['entry_count'] != 1 or trade['decision_ask'] is None
                        or trade['decision_remaining_seconds'] is None):
                    missing += 1
                elif (_d(trade['decision_ask']) <= price
                      and _d(trade['decision_remaining_seconds']) >= min_remaining_seconds):
                    candidate.append(trade)
            results[name] = dict(baseline=_stats(baseline), candidate=_stats(candidate),
                                 missing_entry_inputs=missing,
                                 rejected_by_filter=len(baseline) - len(candidate) - missing)
        return _jsonable(dict(schema='strategy-cohorts-v1', kind='DESCRIPTIVE_COHORT_NOT_BACKTEST',
                             snapshot_sha256=report['snapshot_sha256'], split_us=split_us,
                             parameters=dict(max_decision_ask=price, min_remaining_seconds=min_remaining_seconds),
                             excluded=dict(excluded), **results,
                             limitations=[
                                 'Freeze filters and cutoff before examining test; reusing test invalidates holdout.',
                                 'Recorded baseline fills/fees retained; skipped trades do not free capital or create new trades.',
                                 'No changed exits, sizing, risk gates, threshold or holding-period replay.',
                                 'Closed-only selection excludes unresolved outcomes; counts are not portfolio returns.',
                                 'Decision quotes require source/receive time no later than decision; no fill-price filtering.',
                                 'No optimization, live parameter update, profitability or significance claim.']))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--bot-id')
    parser.add_argument('--source-label', default='UNVERIFIED_SOURCE')
    parser.add_argument('--split-us', type=int, help='fixed chronological holdout cutoff, UTC epoch microseconds')
    parser.add_argument('--max-entry-price', default='1', help='maximum decision-time ask, not realized fill price')
    parser.add_argument('--min-remaining-seconds', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        report = analyze(args.database, bot_id=args.bot_id, source_label=args.source_label)
        if args.split_us is not None:
            report['evaluation'] = evaluate(report, split_us=args.split_us,
                                            max_entry_price=_d(args.max_entry_price),
                                            min_remaining_seconds=args.min_remaining_seconds)
        elif args.max_entry_price != '1' or args.min_remaining_seconds != 0:
            raise ValueError('filters require --split-us')
    except (ValueError, sqlite3.Error, KeyError) as exc:
        parser.exit(2, f'analytics: {exc}\n')
    print(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))
    return 0 if report['reconciliation']['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
