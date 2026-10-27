"""ReplayEngine: deterministic chronological replay with settlement and full metrics."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_FLOOR, localcontext
from hashlib import sha256
from typing import Any

from polymarket_bot.market_data.models import MarketSnapshot
from polymarket_bot.paper import (
    ARITHMETIC, ZERO, _fresh, _number, _proof, _require, _text, simulate
)
from polymarket_bot.replay.report import BacktestReport, compute_config_hash, compute_source_fingerprint


class ReplayEngine:
    """Deterministic chronological replay engine wrapping PaperEngine logic.

    Extends baseline strategy logic with settlement integration and full backtest metrics.
    """

    def __init__(self, **config):
        """Initialize engine with strategy config.

        Config keys: initial_cash, max_order, max_exposure, fee_bps, threshold,
        lookback, holding_period, delay_us, max_wait_us, max_age_us,
        max_positions, max_trades_per_window, max_daily_loss, sample_seconds.
        """
        self.config = config

    def run(
        self,
        snapshots: tuple[MarketSnapshot, ...],
        excluded: tuple[dict, ...],
        opportunities: tuple[dict, ...],
        settlement_evidence: dict | None,
        settlement_result: dict | None,
    ) -> BacktestReport:
        """Run replay and return BacktestReport."""
        return run_replay(
            snapshots=snapshots,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            settlement_result=settlement_result,
            **self.config
        )


def run_replay(
    snapshots: tuple[MarketSnapshot, ...],
    excluded: tuple[dict, ...],
    opportunities: tuple[dict, ...],
    settlement_evidence: dict | None,
    settlement_result: dict | None,
    *,
    initial_cash: str = '100',
    max_order: str = '10',
    max_exposure: str = '10',
    fee_bps: str = '10',
    threshold: str = '0.02',
    lookback: int = 3,
    holding_period: int = 3,
    delay_us: int = 0,
    max_wait_us: int = 5_000_000,
    max_age_us: int = 5_000_000,
    max_positions: int = 1,
    max_trades_per_window: int = 6,
    max_daily_loss: str = '10',
    sample_seconds: int = 2,
) -> BacktestReport:
    """Run full replay and produce BacktestReport.

    Wraps the baseline strategy logic with settlement integration and full metrics.
    Uses opportunity timing (start_ns, available_ns) for causal chronological replay.
    """
    # Build config for determinism
    config = {
        'initial_cash': initial_cash,
        'max_order': max_order,
        'max_exposure': max_exposure,
        'fee_bps': fee_bps,
        'threshold': threshold,
        'lookback': lookback,
        'holding_period': holding_period,
        'delay_us': delay_us,
        'max_wait_us': max_wait_us,
        'max_age_us': max_age_us,
        'max_positions': max_positions,
        'max_trades_per_window': max_trades_per_window,
        'max_daily_loss': max_daily_loss,
        'sample_seconds': sample_seconds,
    }
    config_hash = compute_config_hash(config)
    source_fingerprint = compute_source_fingerprint(snapshots, opportunities, excluded)

    # Build paper.Recording from adapter data
    recording = _build_recording(snapshots, excluded, opportunities)

    # Run the replay strategy
    with localcontext(ARITHMETIC):
        strategy_result = _run_replay_strategy(
            recording, config, max_positions, lookback, holding_period,
            delay_us, max_age_us, max_wait_us
        )

    # Apply settlement if evidence available
    settlement = None
    if settlement_evidence is not None and settlement_result is not None:
        settlement = _apply_settlement(
            strategy_result, settlement_result, recording, initial_cash, fee_bps
        )

    # Build BacktestReport
    report = _build_backtest_report(
        strategy_result, settlement, config_hash, source_fingerprint
    )
    return report


def _build_recording(
    snapshots: tuple[MarketSnapshot, ...],
    excluded: tuple[dict, ...],
    opportunities: tuple[dict, ...],
) -> Any:
    """Build paper.Recording from adapter data.

    Uses opportunities for timing (start_ns, available_ns) and MarketSnapshots for books.
    """
    from polymarket_bot.paper import Recording, Snapshot

    if snapshots:
        up_token = snapshots[0].up.outcome.token_id
        down_token = snapshots[0].down.outcome.token_id
        condition_id = snapshots[0].market.condition_id
        market_id = snapshots[0].market.market_id
    else:
        up_token = '0x' + '11' * 32
        down_token = '0x' + '22' * 32
        condition_id = '0x' + '00' * 32
        market_id = '1001'

    tokens = {'UP': up_token, 'DOWN': down_token}

    # Build per-outcome paper Snapshots from opportunities + MarketSnapshot books
    # MarketSnapshots are ordered by slot; opportunities have per-outcome timing
    # Match: opportunities with reason=None and valid slot are the "revealed" snapshots
    
    # Index MarketSnapshots by slot_index for book lookup
    ms_by_idx = {i: ms for i, ms in enumerate(snapshots)}
    
    # Collect paper snapshots for valid opportunities (reason=None)
    valid_opps_by_slot = {}  # slot_index -> {outcome: opportunity}
    for opp in opportunities:
        if opp.get('reason') is None:
            slot_index = opp['slot_index']
            outcome = opp['outcome']
            valid_opps_by_slot.setdefault(slot_index, {})[outcome] = opp
    
    paper_snapshots = []
    slot_count = 0
    for slot_index in sorted(valid_opps_by_slot):
        opps = valid_opps_by_slot[slot_index]
        if set(opps.keys()) != {'UP', 'DOWN'}:
            continue
        
        ms = ms_by_idx.get(slot_count)
        slot_count += 1
        if ms is None:
            continue
        
        for outcome in ('UP', 'DOWN'):
            opp = opps[outcome]
            obs = ms.up if outcome == 'UP' else ms.down
            book = obs.book
            
            # source_us from book source timestamp
            source_us = int(float(book.source_timestamp_raw) * 1_000_000)
            # receipt_us = source_us + 100_000 (typical response lag in paper tests)
            receipt_us = source_us + 100_000
            
            snap = Snapshot(
                slot_id=opp['slot_id'],
                slot_index=opp['slot_index'],
                outcome=outcome,
                request_id=opp['slot_id'],
                start_ns=opp['start_ns'],
                available_ns=opp['available_ns'],
                receipt_us=receipt_us,
                source_us=source_us,
                uncertainty_us=1_000_000,
                raw_sha256=sha256(book.source_timestamp_raw.encode()).hexdigest()[:64],
                book=book,
                receipt_ns=opp['available_ns']
            )
            paper_snapshots.append(snap)

    paper_snapshots = tuple(paper_snapshots)
    
    # Horizon = max available_ns
    horizon_ns = max(s.available_ns for s in paper_snapshots) if paper_snapshots else 0
    horizon_us = horizon_ns // 1000 if horizon_ns else 0
    
    # db_sha256 placeholder (no DB access in replay)
    db_sha256 = sha256(b'replay').hexdigest()

    return Recording(
        run_id='replay',
        market_id=market_id,
        condition_id=condition_id,
        tokens=tokens,
        db_sha256=db_sha256,
        snapshots=paper_snapshots,
        excluded=excluded,
        horizon_ns=horizon_ns,
        horizon_us=horizon_us,
        opportunities=opportunities
    )


def _run_replay_strategy(
    recording,
    config: dict,
    max_positions: int,
    lookback: int,
    holding_period: int,
    delay_us: int,
    max_age_us: int,
    max_wait_us: int,
) -> dict:
    """Run the core replay strategy logic (adapted from strategy._run)."""
    # Convert config values to Decimal for numeric comparisons
    settings = {
        'initial_cash': _number(config['initial_cash'], 'initial_cash', positive=True),
        'max_order': _number(config['max_order'], 'max_order', positive=True),
        'max_exposure': _number(config['max_exposure'], 'max_exposure', positive=True),
        'fee_bps': _number(config['fee_bps'], 'fee_bps'),
        'threshold': _number(config['threshold'], 'threshold', positive=True),
    }
    _require(settings['fee_bps'] <= 10000, 'invalid fee_bps')
    for name, value, minimum in [('lookback', lookback, 3), ('holding_period', holding_period, 1),
                                 ('delay_us', delay_us, 0), ('max_age_us', max_age_us, 1)]:
        _require(type(value) is int and value >= minimum, 'invalid ' + name)
    _require(type(max_positions) is int and max_positions in (0, 1), 'max_positions must be 0 or 1')

    options = dict(
        initial_cash=_text(settings['initial_cash']),
        fee_bps=_text(settings['fee_bps']),
        delay_us=delay_us,
        max_age_us=max_age_us
    )

    orders, decisions, history = [], [], []
    pending, held_age, held_since, last_index = None, 0, None, -1

    groups = {}
    for s in recording.snapshots:
        groups.setdefault(s.slot_id, {})[s.outcome] = s

    availability = {(s.slot_id, s.outcome): s.available_ns for s in recording.snapshots}
    pairs = {slot: pair for slot, pair in groups.items() if set(pair) == {'UP', 'DOWN'}}
    pair_times = {slot: max(s.available_ns for s in pair.values()) for slot, pair in pairs.items()}

    # Build chronological timeline
    times = sorted({
        s.available_ns for s in recording.snapshots
    } | {
        o['available_ns'] for o in recording.opportunities if o.get('available_ns') is not None
    } | set(pair_times.values()) | {
        o['start_ns'] for o in recording.opportunities if o['start_ns'] is not None and o['reason'] is not None
    })

    empty = replace(recording, snapshots=(), opportunities=(), excluded=())
    state = simulate(empty, [], **options)

    for now in times:
        revealed = tuple(s for s in recording.snapshots if s.available_ns <= now)
        index = max((s.slot_index for s in revealed), default=-1)

        visible = {(s.slot_id, s.outcome) for s in revealed}
        opportunities = tuple(
            dict(o, reason='AWAITING_EXECUTION_AVAILABILITY')
            if (o['reason'] is None and (o['slot_id'], o['outcome']) not in visible) or
               (o.get('available_ns', availability.get((o['slot_id'], o['outcome']))) is not None and
                o.get('available_ns', availability.get((o['slot_id'], o['outcome']))) > now)
            else o
            for o in recording.opportunities
            if (o['start_ns'] is not None and o['start_ns'] <= now) or
               (o['start_ns'] is None and o['slot_index'] <= index)
        )

        prefix = replace(recording, snapshots=revealed, opportunities=opportunities, excluded=(), horizon_ns=now)

        # Check pending order execution
        if pending is not None:
            candidate = simulate(prefix, orders, **options)
            trade = next(t for t in candidate['ledger'] if t['id'] == pending)
            if trade['reason'] not in ('NO_FUTURE_SNAPSHOT', 'AWAITING_EXECUTION_AVAILABILITY'):
                state, pending = candidate, None
                if trade.get('side') == 'BUY' and Decimal(trade['filled_quantity']) > 0:
                    held_age, held_since = 0, now

        for slot in sorted(
            (slot for slot, t in pair_times.items() if t == now),
            key=lambda s: pairs[s]['UP'].slot_index
        ):
            pair = pairs[slot]

            positions = {
                o: {k: p[k] for k in ('quantity', 'cost_basis')}
                for o, p in state['positions'].items()
            }
            exposure = sum((Decimal(p['cost_basis']) for p in positions.values()), ZERO)

            d = dict(
                slot_id=slot,
                slot_index=pairs[slot]['UP'].slot_index,
                available_mono_ns=now,
                action='HOLD',
                reason=None,
                signal='HOLD',
                inputs=[_proof(pair[o]) for o in ('UP', 'DOWN')],
                midpoints={},
                momentum=None,
                cash=state['cash'],
                exposure=_text(exposure),
                positions=positions,
                instruction=None
            )

            fresh = all(
                _fresh(s, now_us=s.receipt_us + (now - s.receipt_ns) // 1000, max_age_us=max_age_us) and
                s.book.best_bid is not None and s.book.best_ask is not None
                for s in pair.values()
            )

            if pair['UP'].slot_index <= last_index:
                d['reason'] = 'OUT_OF_ORDER_PAIR'
            elif not fresh:
                history = []
                d['reason'] = 'STALE_PAIR'
            else:
                last_index = pair['UP'].slot_index

                if held_since is not None and now > held_since and any(
                    Decimal(p['quantity']) > 0 for p in positions.values()
                ):
                    held_age += 1

                d['midpoints'] = {
                    o: _text((s.book.best_bid + s.book.best_ask) / 2)
                    for o, s in pair.items()
                }
                history.append(dict(
                    slot_id=slot,
                    midpoint=d['midpoints']['UP'],
                    inputs=d['inputs']
                ))
                history = history[-lookback:]
                d['lookback_inputs'] = list(history)

                if len(history) < lookback:
                    d['reason'] = 'WARMUP'
                else:
                    momentum = Decimal(history[-1]['midpoint']) - Decimal(history[0]['midpoint'])
                    d['momentum'] = _text(momentum)
                    d['signal'] = (
                        'UP' if momentum >= settings['threshold'] else
                        'DOWN' if momentum <= -settings['threshold'] else 'HOLD'
                    )

                owned = next((o for o, p in positions.items() if Decimal(p['quantity']) > 0), None)

                if pending is not None:
                    d['reason'] = 'PENDING_EXECUTION'
                elif owned:
                    if held_age >= holding_period or d['signal'] not in ('HOLD', owned):
                        d['action'] = 'SELL'
                        d['reason'] = 'HOLDING_PERIOD' if held_age >= holding_period else 'REVERSAL'
                        outcome, side, quantity = owned, 'SELL', positions[owned]['quantity']
                elif d['signal'] != 'HOLD':
                    quantity = (
                        Decimal(settings['max_order']) /
                        (Decimal(1) + Decimal(settings['fee_bps']) / Decimal(10000)) /
                        Decimal('0.01')
                    ).to_integral_value(rounding=ROUND_FLOOR) * Decimal('0.01')
                    reserve = quantity * (Decimal(1) + Decimal(settings['fee_bps']) / Decimal(10000))

                    reason = (
                        'POSITION_LIMIT' if max_positions == 0 else
                        'ZERO_QUANTITY' if quantity <= 0 else
                        'INSUFFICIENT_CASH_RESERVE' if reserve > Decimal(state['cash']) else
                        'EXPOSURE_LIMIT' if exposure + reserve > settings['max_exposure'] else None
                    )
                    d['reason'] = reason

                    if reason is None:
                        outcome, side, quantity = d['signal'], 'BUY', _text(quantity)
                        d['action'] = f'BUY {outcome}'

                if d['action'] != 'HOLD':
                    instruction = dict(
                        id=f'strategy-{len(orders) + 1}',
                        market_id=recording.market_id,
                        token_id=recording.tokens[outcome],
                        side=side,
                        quantity=quantity,
                        decision_slot_id=slot
                    )
                    orders.append(instruction)
                    d['instruction'] = instruction.copy()
                    pending = instruction['id']

            decisions.append(d)

    # Final execution pass with full horizon
    execution = simulate(recording, orders, **options)

    return dict(
        decisions=decisions,
        orders=orders,
        execution=execution,
        final_state=state,
        config=config
    )


def _apply_settlement(
    strategy_result: dict,
    settlement_result: dict,
    recording,
    initial_cash: str,
    fee_bps: str
) -> dict:
    """Apply settlement to the strategy result."""
    if settlement_result.get('status') != 'RESOLVED':
        return dict(
            status=settlement_result.get('status', 'UNKNOWN_SETTLEMENT'),
            winning_outcome=settlement_result.get('winning_outcome'),
            resolved_at=settlement_result.get('resolved_at'),
            tx_hash=settlement_result.get('tx_hash'),
            diagnostic=settlement_result.get('diagnostic')
        )

    winning = settlement_result['winning_outcome']
    execution = strategy_result['execution']

    final_positions = execution.get('positions', {})
    realized_pnl = Decimal('0')

    for outcome, pos in final_positions.items():
        qty = Decimal(pos.get('quantity', '0'))
        if qty > 0:
            cost_basis = Decimal(pos.get('cost_basis', '0'))
            if outcome == winning:
                payout = qty * Decimal('1')
                realized = payout - cost_basis
            else:
                realized = -cost_basis
            realized_pnl += realized

    cash = Decimal(execution.get('cash', initial_cash))
    fees_paid = Decimal(execution.get('fees_paid', '0'))
    cash += realized_pnl

    return dict(
        status='RESOLVED',
        winning_outcome=winning,
        resolved_at=settlement_result['resolved_at'],
        tx_hash=settlement_result['tx_hash'],
        realized_pnl=_text(realized_pnl),
        final_cash=_text(cash)
    )


def _build_backtest_report(
    strategy_result: dict,
    settlement: dict | None,
    config_hash: str,
    source_fingerprint: str
) -> BacktestReport:
    """Build the complete BacktestReport from strategy result."""
    execution = strategy_result['execution']
    decisions = strategy_result['decisions']
    ledger = execution.get('ledger', [])

    # Exit reason breakdown from SELL trades
    exit_reasons = {}
    for trade in ledger:
        if trade['status'] in ('FILLED', 'PARTIAL') and trade.get('side') == 'SELL':
            reason = trade.get('reason', 'UNKNOWN')
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    # Daily P&L from ledger execution times
    daily_pnl = {}
    for trade in ledger:
        if trade['status'] in ('FILLED', 'PARTIAL'):
            snap = trade.get('snapshot', {})
            receipt_us = snap.get('receipt_server_us', 0)
            if receipt_us and int(receipt_us) > 0:
                day = datetime.fromtimestamp(int(receipt_us) / 1_000_000, tz=timezone.utc).date().isoformat()
                pnl = Decimal(trade.get('realized_pnl', '0') or '0')
                daily_pnl[day] = daily_pnl.get(day, Decimal('0')) + pnl

    daily_pnl = {k: str(v) for k, v in sorted(daily_pnl.items())}

    # Max drawdown from equity curve
    max_drawdown = _compute_max_drawdown(execution)

    # Diagnostics
    diagnostics = dict(
        order_statuses=_count_statuses(ledger),
        decision_reasons=_count_decision_reasons(decisions),
        event_kinds=_count_event_kinds(ledger),
        execution_delay_us=_execution_delays(ledger),
        missing_decision_quotes=_count_missing_quotes(decisions)
    )

    return BacktestReport(
        config_hash=config_hash,
        source_fingerprint=source_fingerprint,
        decisions=tuple(decisions),
        ledger=tuple(ledger),
        counts=execution.get('counts', {}),
        cash=execution.get('cash', '0'),
        fees_paid=execution.get('fees_paid', '0'),
        positions=execution.get('positions', {}),
        realized_pnl=execution.get('realized_pnl'),
        unrealized_pnl=execution.get('unrealized_pnl'),
        total_pnl=execution.get('total_pnl'),
        max_drawdown=max_drawdown,
        daily_pnl=daily_pnl,
        settlement=settlement,
        exit_reason_breakdown=exit_reasons,
        diagnostics=diagnostics
    )


def _compute_max_drawdown(execution: dict) -> Decimal | None:
    """Compute max drawdown from equity curve (single sample at horizon)."""
    cash = Decimal(execution.get('cash', '0'))
    positions = execution.get('positions', {})

    exposure = Decimal('0')
    unrealized = Decimal('0')
    for pos in positions.values():
        exposure += Decimal(pos.get('cost_basis', '0'))
        if pos.get('unrealized_pnl') is not None:
            unrealized += Decimal(pos['unrealized_pnl'])

    equity = cash + exposure + unrealized
    initial = Decimal(execution.get('initial_cash', '100'))

    if equity < initial:
        return initial - equity
    return Decimal('0')


def _count_statuses(ledger: list) -> dict:
    counts = {}
    for t in ledger:
        s = t.get('status') or 'UNKNOWN'
        counts[str(s)] = counts.get(str(s), 0) + 1
    return counts


def _count_decision_reasons(decisions: list) -> dict:
    counts = {}
    for d in decisions:
        r = d.get('reason') or 'NONE'
        counts[str(r)] = counts.get(str(r), 0) + 1
    return counts


def _count_event_kinds(ledger: list) -> dict:
    counts = {}
    for t in ledger:
        k = t.get('reason') or ('FILLED' if t.get('status') == 'FILLED' else t.get('status')) or 'NONE'
        counts[str(k)] = counts.get(str(k), 0) + 1
    return counts


def _execution_delays(ledger: list) -> list[int]:
    delays = []
    for t in ledger:
        if t['status'] in ('FILLED', 'PARTIAL'):
            snap = t.get('snapshot', {})
            decision = t.get('decision', {})
            receipt = snap.get('receipt_server_us', 0)
            decision_us = decision.get('receipt_server_us', 0)
            if receipt and decision_us:
                delays.append(int(receipt) - int(decision_us))
    return delays


def _count_missing_quotes(decisions: list) -> int:
    count = 0
    for d in decisions:
        mids = d.get('midpoints', {})
        if not mids or mids.get('UP') is None or mids.get('DOWN') is None:
            count += 1
    return count
