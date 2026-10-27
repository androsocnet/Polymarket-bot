"""ReplayEngine: deterministic chronological replay with settlement and full metrics."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_FLOOR, localcontext

from polymarket_bot.linux_mvp.settlement import verify_evidence
from polymarket_bot.market_data.models import MarketSnapshot
from polymarket_bot.paper import (
    ARITHMETIC, ZERO, _fresh, _number, _proof, _require, _text, simulate
)
from polymarket_bot.replay.report import BacktestReport, compute_config_hash, compute_source_fingerprint

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ReplayEngine:
    """Deterministic chronological replay engine wrapping PaperEngine logic.

    Extends baseline strategy logic with settlement integration and full backtest metrics.
    """

    def __init__(self, **config):
        """Initialize engine with strategy config.

        Config keys: initial_cash, max_order, max_exposure, fee_bps, threshold,
        lookback, holding_period, delay_us, max_wait_us, max_age_us,
        max_positions, max_trades_per_window, max_daily_loss, max_entry_price,
        min_remaining_seconds, require_reversal_confirmation, depth_scale.
        """
        self.config = config

    def run(
        self,
        snapshots: tuple[MarketSnapshot, ...],
        *,
        paper_recording,
        excluded: tuple[dict, ...] | None = None,
        opportunities: tuple[dict, ...] | None = None,
        settlement_evidence: dict | None = None,
    ) -> BacktestReport:
        """Run replay and return BacktestReport."""
        return run_replay(
            snapshots=snapshots,
            paper_recording=paper_recording,
            excluded=excluded,
            opportunities=opportunities,
            settlement_evidence=settlement_evidence,
            **self.config
        )


def run_replay(
    snapshots: tuple[MarketSnapshot, ...],
    *,
    paper_recording,
    excluded: tuple[dict, ...] | None = None,
    opportunities: tuple[dict, ...] | None = None,
    settlement_evidence: dict | None = None,
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
    max_entry_price: str | None = None,
    min_remaining_seconds: int = 0,
    require_reversal_confirmation: bool = False,
    depth_scale: str | float = '1.0',
) -> BacktestReport:
    """Run full replay and produce BacktestReport.

    Strategy decisions run on the validated paper recording directly, so every
    real anchor-verified receipt/source clock (and therefore staleness, fills,
    fees and settlement) inherits the live engine's semantics — nothing is
    re-derived or fabricated.
    """
    if paper_recording is None:
        raise ValueError('paper_recording (validated load_strategy_recording result) is required')
    if not snapshots:
        raise ValueError('snapshots (MarketSnapshot pairs for fingerprinting) are required')
    recording = paper_recording
    if excluded is None:
        excluded = recording.excluded
    if opportunities is None:
        opportunities = recording.opportunities

    scale = _number(str(depth_scale), 'depth_scale')
    _require(scale > 0, 'invalid depth_scale')
    if scale != Decimal(1):
        recording = _scale_books(recording, scale)

    # Build config for determinism (fully hashes every parameter the engine accepts)
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
        'max_entry_price': max_entry_price,
        'min_remaining_seconds': min_remaining_seconds,
        'require_reversal_confirmation': require_reversal_confirmation,
        'depth_scale': str(depth_scale),
    }
    config_hash = compute_config_hash(config)
    source_fingerprint = compute_source_fingerprint(snapshots, opportunities, excluded)

    window_end_us = _epoch_us(snapshots[0].market.window_end)

    # Run the replay strategy
    with localcontext(ARITHMETIC):
        strategy_result = _run_replay_strategy(
            recording, config, max_positions, lookback, holding_period,
            delay_us, max_age_us, max_wait_us, window_end_us
        )

    # Apply settlement if evidence available; the engine re-verifies evidence
    # against the replay's own identity before honoring any resolution.
    settlement = None
    if settlement_evidence is not None:
        verified = _verify_settlement(recording, settlement_evidence)
        settlement = _apply_settlement(strategy_result, verified)

    # Build BacktestReport
    report = _build_backtest_report(
        strategy_result, settlement, config_hash, source_fingerprint
    )
    return report


def _epoch_us(moment: datetime) -> int:
    delta = moment - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _scale_books(recording, scale: Decimal):
    """Hypothetical liquidity sensitivity: scale every recorded book level size."""
    def scale_book(book):
        if book.bid_size is not None and book.ask_size is not None:
            bids = tuple(replace(level, size=level.size * scale) for level in book.bids)
            asks = tuple(replace(level, size=level.size * scale) for level in book.asks)
            return replace(book, bids=bids, asks=asks,
                           bid_size=book.bid_size * scale, ask_size=book.ask_size * scale)
        return book
    from polymarket_bot.paper import Snapshot
    scaled = tuple(
        replace(snapshot, book=scale_book(snapshot.book))
        for snapshot in recording.snapshots if isinstance(snapshot, Snapshot)
    )
    return replace(recording, snapshots=scaled)


def _run_replay_strategy(
    recording,
    config: dict,
    max_positions: int,
    lookback: int,
    holding_period: int,
    delay_us: int,
    max_age_us: int,
    max_wait_us: int,
    window_end_us: int | None,
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

    # Review-remediated risk/variant parameters (all fail closed on bad values)
    max_entry = config.get('max_entry_price')
    if max_entry is not None:
        max_entry = _number(max_entry, 'max_entry_price')
        _require(Decimal('0') < max_entry <= Decimal('1'), 'invalid max_entry_price')
    min_remaining = config.get('min_remaining_seconds', 0)
    _require(type(min_remaining) is int and min_remaining >= 0, 'invalid min_remaining_seconds')
    if min_remaining > 0 and window_end_us is None:
        raise ValueError('min_remaining_seconds requires a real window end')
    confirmation_required = config.get('require_reversal_confirmation', False)
    _require(isinstance(confirmation_required, bool), 'invalid require_reversal_confirmation')
    max_trades = config.get('max_trades_per_window', 6)
    _require(type(max_trades) is int and max_trades >= 0, 'invalid max_trades_per_window')
    max_daily_loss = _number(config.get('max_daily_loss', '10'), 'max_daily_loss')
    _require(max_daily_loss >= 0, 'invalid max_daily_loss')

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

    confirm_signal, confirm_count = None, 0

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

            # Real epoch decision time from the recorded anchor clocks.
            decision_us = max(s.receipt_us + (now - s.receipt_ns) // 1000 for s in pair.values())

            executed_buys = sum(
                1 for t in state['ledger']
                if t.get('side') == 'BUY' and t.get('status') in ('FILLED', 'PARTIAL')
                and Decimal(t.get('filled_quantity', '0')) > 0
            )
            daily_realized = _day_realized_usd(state['ledger'], decision_us)

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
                    if confirmation_required and held_age < holding_period and d['signal'] not in ('HOLD', owned):
                        if confirm_signal == d['signal']:
                            confirm_count += 1
                        else:
                            confirm_signal, confirm_count = d['signal'], 1
                        if confirm_count < 2:
                            d['reason'] = 'REVERSAL_UNCONFIRMED'
                        else:
                            confirm_signal, confirm_count = None, 0
                            d['action'] = 'SELL'
                            d['reason'] = 'REVERSAL'
                            outcome, side, quantity = owned, 'SELL', positions[owned]['quantity']
                    elif held_age >= holding_period or d['signal'] not in ('HOLD', owned):
                        confirm_signal, confirm_count = None, 0
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

                    entry_ask = pair[d['signal']].book.best_ask
                    remaining_us = None if window_end_us is None else window_end_us - decision_us
                    reason = (
                        'POSITION_LIMIT' if max_positions == 0 else
                        'ZERO_QUANTITY' if quantity <= 0 else
                        'INSUFFICIENT_CASH_RESERVE' if reserve > Decimal(state['cash']) else
                        'EXPOSURE_LIMIT' if exposure + reserve > settings['max_exposure'] else
                        'TRADE_LIMIT' if max_trades is not None and executed_buys >= max_trades else
                        'DAILY_LOSS_LIMIT' if daily_realized <= -max_daily_loss else
                        'MIN_REMAINING' if (remaining_us is not None and
                                            remaining_us < min_remaining * 1_000_000) else
                        'ENTRY_PRICE_CAP' if max_entry is not None and
                                               (entry_ask is None or entry_ask > max_entry) else
                        None
                    )
                    d['reason'] = reason

                    if reason is None:
                        outcome, side, quantity = d['signal'], 'BUY', _text(quantity)
                        d['action'] = f'BUY {outcome}'
                        confirm_signal, confirm_count = None, 0

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


def _day_realized_usd(ledger, decision_us: int) -> Decimal:
    """Realized P&L accumulated on the decision's UTC day (limit guard input)."""
    day = datetime.fromtimestamp(decision_us / 1_000_000, tz=timezone.utc).date()
    total = Decimal('0')
    for trade in ledger:
        if trade.get('status') not in ('FILLED', 'PARTIAL'):
            continue
        snap = trade.get('snapshot') or {}
        receipt_us = snap.get('receipt_server_us')
        if not receipt_us:
            continue
        try:
            trade_day = datetime.fromtimestamp(int(receipt_us) / 1_000_000, tz=timezone.utc).date()
        except (OSError, OverflowError, ValueError):
            continue
        if trade_day == day and trade.get('side') == 'SELL':
            total += Decimal(trade.get('realized_pnl', '0') or '0')
    return total


def _verify_settlement(recording, evidence: dict) -> dict:
    """Re-verify resolution evidence against the replay's own persisted identity.

    Never trusts caller-provided resolution results: the replay pays out only
    what the independent verifier accepts against this exact market.
    """
    try:
        _require(evidence.get('market_id') == recording.market_id and
                 evidence.get('condition_id') == recording.condition_id,
                 'replay market identity mismatch')
        window = {
            'market_id': recording.market_id,
            'condition_id': recording.condition_id,
            'start_us': int(evidence['window_start_us']),
            'end_us': int(evidence['window_end_us']),
        }
        return verify_evidence(evidence, window=window, tokens=recording.tokens,
                               at_us=int(evidence['at_us']))
    except (KeyError, TypeError, ValueError) as exc:
        return dict(status='UNKNOWN_SETTLEMENT', winning_outcome=None, diagnostic=str(exc))


def _apply_settlement(
    strategy_result: dict,
    settlement_result: dict,
) -> dict:
    """Apply a verify-accepted settlement to the strategy result.

    Cash accounting: the execution cash already excludes committed cost basis,
    so settled holders receive their coupon payout on top. Realized P&L folds
    the execution run's own trade-level realized P&L together with the
    settlement's payout-minus-cost for remaining holders, so the totals satisfy
    final_cash + basis == initial_cash + realized_pnl.
    """
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
    exec_realized = Decimal(execution.get('realized_pnl') or '0')

    settled, realized, cash = {}, exec_realized, Decimal(execution['cash'])
    final_positions = {}
    for outcome, pos in execution['positions'].items():
        qty = Decimal(pos.get('quantity', '0'))
        cost = Decimal(pos.get('cost_basis', '0'))
        payout = qty if outcome == winning else ZERO
        if qty > 0:
            realized += payout - cost
            cash += payout
            settled[outcome] = dict(payout=_text(payout), cost_basis=_text(cost),
                                    realized_pnl=_text(payout - cost))
            final_positions[outcome] = dict(pos, quantity='0', cost_basis='0',
                                            average_cost=None, indicative_bid_mark=None,
                                            mark_evidence=None, unrealized_pnl=_text(payout - cost))
        else:
            settled[outcome] = dict(payout='0', cost_basis=_text(cost), realized_pnl='0')
            final_positions[outcome] = pos

    return dict(
        status='RESOLVED',
        winning_outcome=winning,
        resolved_at=settlement_result['resolved_at'],
        tx_hash=settlement_result['tx_hash'],
        settled=settled,
        realized_pnl=_text(realized),
        final_cash=_text(cash),
        positions=final_positions
    )


def _build_backtest_report(
    strategy_result: dict,
    settlement: dict | None,
    config_hash: str,
    source_fingerprint: str
) -> BacktestReport:
    """Build the complete BacktestReport from strategy result.

    A RESOLVED settlement is folded into the headline cash/realized/total so the
    report never contradicts its own settlement section.
    """
    execution = strategy_result['execution']
    decisions = strategy_result['decisions']
    ledger = execution.get('ledger', [])

    resolved = bool(settlement and settlement.get('status') == 'RESOLVED')

    # Exit reason breakdown from SELL trades
    exit_reasons = {}
    for trade in ledger:
        if trade['status'] in ('FILLED', 'PARTIAL') and trade.get('side') == 'SELL':
            reason = trade.get('reason', 'UNKNOWN')
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    # Daily P&L from ledger execution times (executed trades only)
    daily_pnl = {}
    for trade in ledger:
        if trade['status'] in ('FILLED', 'PARTIAL'):
            snap = trade.get('snapshot') or {}
            receipt_us = snap.get('receipt_server_us', 0)
            if receipt_us and int(receipt_us) > 0:
                try:
                    day = datetime.fromtimestamp(int(receipt_us) / 1_000_000, tz=timezone.utc).date().isoformat()
                except (OSError, OverflowError, ValueError):
                    continue
                pnl = Decimal(trade.get('realized_pnl', '0') or '0')
                daily_pnl[day] = daily_pnl.get(day, Decimal('0')) + pnl

    daily_pnl = {k: str(v) for k, v in sorted(daily_pnl.items())}

    if resolved:
        cash = settlement['final_cash']
        positions = settlement['positions']
        realized_pnl = settlement['realized_pnl']
        unrealized_pnl = '0'
        total_pnl = settlement['realized_pnl']
        max_drawdown = _settlement_drawdown(settlement, execution)
    else:
        cash = execution.get('cash', '0')
        positions = execution.get('positions', {})
        realized_pnl = execution.get('realized_pnl')
        unrealized_pnl = execution.get('unrealized_pnl')
        total_pnl = execution.get('total_pnl')
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
        cash=cash,
        fees_paid=execution.get('fees_paid', '0'),
        positions=positions,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        total_pnl=total_pnl,
        max_drawdown=max_drawdown,
        daily_pnl=daily_pnl,
        settlement=settlement,
        exit_reason_breakdown=exit_reasons,
        diagnostics=diagnostics
    )


def _settlement_drawdown(settlement: dict, execution: dict) -> Decimal:
    """Post-settlement drawdown: shortfall of final equity against initial cash."""
    initial = Decimal(execution.get('initial_cash', '100'))
    equity = Decimal(settlement['final_cash'])
    return max(Decimal('0'), initial - equity)


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
            snap = t.get('snapshot') or {}
            decision = t.get('decision') or {}
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
