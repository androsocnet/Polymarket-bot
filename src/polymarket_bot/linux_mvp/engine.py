"""One causal PAPER cycle at a time. No network is imported here; collectors are injected."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
import json
import time

from ..market_data.models import MarketDataError
from .config import Settings
from .store import ZERO, MvpStore

UP, DOWN = 'UP', 'DOWN'


@dataclass(frozen=True)
class StepResult:
    status: str
    market_id: str | None = None
    slot_index: int | None = None
    action: str = 'HOLD'
    reason: str = ''


def _us(moment) -> int:
    return int(moment.timestamp() * 1_000_000)


class PaperEngine:
    """Deterministic state machine over injected snapshots.

    Invariants: at most one hypothetical position; an order is queued by exactly one decision;
    a PENDING order is executed against the first fresh slot strictly after its decision slot;
    nothing is ever settled or inferred.
    """

    def __init__(self, store: MvpStore, collector, settings: Settings, *, run_id: str,
                 now_us=None):
        self.store = store
        self.collector = collector
        self.settings = settings
        self.run_id = run_id
        self.bot_id = settings.bot_id
        self.now_us = now_us or (lambda: time.time_ns() // 1000)
        self.consecutive_errors = 0
        self.cycles = 0
        self._loss_event_day = None

    # -- cycle ---------------------------------------------------------------
    def step(self) -> StepResult:
        at = self.now_us()
        self.cycles += 1
        heartbeat = self.store.heartbeat_row(self.bot_id) or {}
        if (heartbeat.get('status') == 'FAILED_STOP'
                and heartbeat.get('run_id') == self.run_id):
            return StepResult('FAILED_STOP', reason=heartbeat.get('last_error') or 'FAILED_STOP')

        if self.settings.stop_path.exists():
            if heartbeat.get('status') != 'STOPPED':
                self.store.event(self.bot_id, self.run_id, at_us=at, kind='emergency_stop',
                                 detail=str(self.settings.stop_path))
                self._beat(at, heartbeat.get('market_id'), 'STOPPED', None)
            return StepResult('STOPPED', reason='EMERGENCY_STOP')

        day_start = (at // 86_400_000_000) * 86_400_000_000
        if self.store.realized(self.bot_id, since_us=day_start) <= -self.settings.max_daily_loss:
            day = at // 86_400_000_000
            if self._loss_event_day != day:
                self.store.event(self.bot_id, self.run_id, at_us=at, kind='daily_loss_limit',
                                 detail=str(self.settings.max_daily_loss))
                self._loss_event_day = day
            self._beat(at, heartbeat.get('market_id'), 'RISK_STOPPED', 'DAILY_LOSS_LIMIT')
            return StepResult('RISK_STOPPED', reason='DAILY_LOSS_LIMIT')

        try:
            snapshot = self.collector.collect()
        except MarketDataError as exc:
            self.consecutive_errors += 1
            self.store.event(self.bot_id, self.run_id, at_us=at, kind='data_error',
                             detail=f'{exc.reason}: {str(exc)[:300]}')
            status = 'FAILED_STOP' if self.consecutive_errors >= self.settings.max_consecutive_errors else 'ERROR'
            self._beat(at, heartbeat.get('market_id'), status, exc.reason)
            return StepResult(status, reason=exc.reason)
        self.consecutive_errors = 0

        market = snapshot.market
        start_us, end_us = _us(market.window_start), _us(market.window_end)
        validated = _us(snapshot.validated_at)
        active = self.store.active_window(self.bot_id)

        if active is not None and active['market_id'] == market.market_id and validated >= active['end_us']:
            self._abandon(active, at, 'ABORTED', 'window ended while active', 'window_expired')
            self._beat(at, market.market_id, 'RUNNING', None)
            return StepResult('WINDOW_EXPIRED', market.market_id, reason='WINDOW_EXPIRED')

        if active is None:
            if self.store.ensure_window(self.bot_id, self.run_id, market_id=market.market_id,
                                        condition_id=market.condition_id, event_id=market.event_id,
                                        start_us=start_us, end_us=end_us, at_us=at):
                self.store.event(self.bot_id, self.run_id, at_us=at, kind='window_start',
                                 detail=market.market_id)
            else:
                existing = self.store.window(self.bot_id, market.market_id)
                if existing is not None and existing['status'] != 'ACTIVE':
                    self._beat(at, market.market_id, 'RUNNING', 'WINDOW_ABANDONED')
                    return StepResult('WINDOW_ABANDONED', market.market_id, reason='WINDOW_ABANDONED')
        elif active['market_id'] != market.market_id:
            self._abandon(active, at, 'UNKNOWN_SETTLEMENT', f'rollover to {market.market_id}', 'rollover')
            self._reject_pending_of(active['market_id'], at)
            self.store.ensure_window(self.bot_id, self.run_id, market_id=market.market_id,
                                     condition_id=market.condition_id, event_id=market.event_id,
                                     start_us=start_us, end_us=end_us, at_us=at)

        if not start_us <= validated < end_us:
            self.store.event(self.bot_id, self.run_id, at_us=at, kind='out_of_window',
                             detail=f'{validated} not in [{start_us},{end_us})')
            self._beat(at, market.market_id, 'RUNNING', 'OUT_OF_WINDOW')
            return StepResult('OUT_OF_WINDOW', market.market_id, reason='OUT_OF_WINDOW')

        slot_index = int((validated - start_us) // (self.settings.sample_seconds * 1_000_000))
        slot_id = f'{market.market_id}:{slot_index}'
        fresh = {}
        stale = False
        one_sided = False
        for outcome, observation in ((UP, snapshot.up), (DOWN, snapshot.down)):
            book = observation.book
            self.store.observe(
                self.bot_id, self.run_id, market.market_id, slot_index, outcome,
                observation.outcome.token_id, book.best_bid, book.best_ask, book.midpoint,
                _us(book.source_at), _us(observation.request_finished_at))
            age = (snapshot.validated_at - observation.request_finished_at).total_seconds()
            complete = (book.best_bid is not None and book.best_ask is not None
                        and book.best_bid > 0 and book.best_ask > 0)
            if not complete:
                one_sided = True
            elif not 0 <= age <= self.settings.max_source_age_seconds:
                stale = True
            else:
                fresh[outcome] = book

        self._execute_pending(market.market_id, slot_index, fresh, at)

        if self.store.decision(self.bot_id, market.market_id, slot_id) is not None:
            self._mark(market.market_id, fresh, at)
            self._beat(at, market.market_id, 'RUNNING', None)
            return StepResult('DUPLICATE_SLOT', market.market_id, slot_index, reason='SLOT_ALREADY_DECIDED')

        reason = 'ONE_SIDED_BOOK' if one_sided else 'STALE_PAIR' if stale else ''
        result = self._decide(market, snapshot, slot_index, slot_id, fresh, reason, at)
        self._mark(market.market_id, fresh, at)
        self._beat(at, market.market_id, 'RUNNING', None)
        return StepResult('OK', market.market_id, slot_index, result[0], result[1])

    # -- decisions -----------------------------------------------------------
    def _decide(self, market, snapshot, slot_index, slot_id, fresh, stale_reason, at):
        positions = self.store.positions(self.bot_id, market.market_id)
        open_positions = self.store.open_positions(self.bot_id)
        total_exposure = sum((p['cost_basis'] for p in open_positions), ZERO)
        owned = next((outcome for outcome, position in positions.items() if position['quantity'] > 0), None)
        other_open = any(p['market_id'] != market.market_id for p in open_positions)
        cash = self.store.cash(self.bot_id)
        decision = dict(bot_id=self.bot_id, run_id=self.run_id, market_id=market.market_id,
                        slot_index=slot_index, slot_id=slot_id, action='HOLD', reason='HOLD',
                        signal='HOLD', momentum=None, mid_up=None, cash=cash, exposure=total_exposure,
                        inputs={}, at_us=at)
        order = None

        if stale_reason:
            decision['reason'] = stale_reason
            decision['inputs'] = {'fresh': sorted(fresh), 'slot': slot_id}
        else:
            mid_up = (fresh[UP].best_bid + fresh[UP].best_ask) / 2
            recent = self.store.recent_decisions(self.bot_id, market.market_id,
                                                 self.settings.lookback - 1)
            history = [Decimal(row['mid_up']) for row in recent if row['mid_up'] not in (None, '')]
            history = (history + [mid_up])[-self.settings.lookback:]
            decision['mid_up'] = mid_up
            decision['inputs'] = {'lookback': len(history), 'slot': slot_id,
                                  'mid_down': (fresh[DOWN].best_bid + fresh[DOWN].best_ask) / 2}
            signal = 'HOLD'
            momentum = None
            if len(history) < self.settings.lookback:
                decision['reason'] = 'WARMUP'
            else:
                momentum = history[-1] - history[0]
                signal = ('UP' if momentum >= self.settings.threshold else
                          'DOWN' if momentum <= -self.settings.threshold else 'HOLD')
            decision.update(signal=signal, momentum=momentum)

            pending = self.store.pending_order(self.bot_id)
            if pending is not None:
                decision['reason'] = 'PENDING_EXECUTION'
            elif owned is not None:
                entry = self.store.entry_order(self.bot_id, market.market_id, owned)
                held_age = self._held_age(market.market_id, entry)
                if held_age >= self.settings.holding_period or decision['signal'] not in ('HOLD', owned):
                    decision['reason'] = ('HOLDING_PERIOD' if held_age >= self.settings.holding_period
                                          else 'REVERSAL')
                    decision['action'] = 'SELL'
                    order = dict(outcome=owned, side='SELL', quantity=positions[owned]['quantity'],
                                 reason=decision['reason'])
            elif other_open:
                decision['reason'] = 'POSITION_LIMIT'
            elif signal != 'HOLD':
                quantity = ((self.settings.max_order / (1 + self.settings.fee_bps / 10000))
                            / Decimal('0.01')).to_integral_value(rounding=ROUND_FLOOR) * Decimal('0.01')
                reserve = quantity * (1 + self.settings.fee_bps / 10000)
                if self.store.trade_count(self.bot_id, market.market_id) >= self.settings.max_trades_per_window:
                    decision['reason'] = 'WINDOW_TRADE_LIMIT'
                elif quantity <= 0:
                    decision['reason'] = 'ZERO_QUANTITY'
                elif reserve > cash:
                    decision['reason'] = 'INSUFFICIENT_CASH_RESERVE'
                elif total_exposure + reserve > self.settings.max_exposure:
                    decision['reason'] = 'EXPOSURE_LIMIT'
                else:
                    decision['action'] = 'BUY ' + signal
                    order = dict(outcome=signal, side='BUY', quantity=quantity, reason='SIGNAL_' + signal)

        with self.store.transaction():
            inserted = self.store.decide(**decision)
            if inserted and order is not None:
                token_id = market.up.token_id if order['outcome'] == UP else market.down.token_id
                order_id = f'{self.bot_id}:{market.market_id}:{slot_index}:{order["side"]}:{order["outcome"]}'
                self.store.queue_order(order_id, self.bot_id, self.run_id, market.market_id, token_id,
                                       order['outcome'], order['side'], order['quantity'], slot_id,
                                       order['reason'], at)
        return decision['action'], decision['reason']

    def _held_age(self, market_id: str, entry: dict | None) -> int:
        if entry is None:
            return 0
        entry_slot = self._slot_of(entry['decision_slot_id'])
        if entry_slot is None:
            return 0
        return sum(1 for row in self.store.decisions_for_market(self.bot_id, market_id)
                   if row['slot_index'] > entry_slot)

    # -- execution / marks ---------------------------------------------------
    def _execute_pending(self, market_id: str, slot_index: int, fresh: dict, at: int) -> None:
        pending = self.store.pending_order(self.bot_id)
        if pending is None:
            return
        if pending['market_id'] != market_id:
            self._reject(pending, 'WINDOW_CLOSED', at)
            return
        decision_slot = self._slot_of(pending['decision_slot_id'])
        if decision_slot is None or slot_index <= decision_slot:
            return
        if slot_index > decision_slot + 1:
            self._reject(pending, 'EXPIRED_EXECUTION_WINDOW', at)
            return
        if pending['outcome'] not in fresh:
            return
        result = self.store.execute_pending(self.bot_id, fresh, self.settings.fee_bps, at_us=at)
        if result is not None:
            self.store.event(self.bot_id, self.run_id, at_us=at, kind='order_executed',
                             detail=json.dumps({k: str(v) for k, v in result.items()}, sort_keys=True))

    def _reject_pending_of(self, market_id: str, at: int) -> None:
        pending = self.store.pending_order(self.bot_id)
        if pending is not None and pending['market_id'] == market_id:
            self._reject(pending, 'WINDOW_CLOSED', at)

    def _reject(self, pending: dict, reason: str, at: int) -> None:
        if self.store.reject_order(pending['order_id'], reason, at_us=at):
            self.store.event(self.bot_id, self.run_id, at_us=at, kind='order_expired',
                             detail=f'{pending["order_id"]}:{reason}')

    def _mark(self, market_id: str, fresh: dict, at: int) -> None:
        positions = self.store.open_positions(self.bot_id)
        exposure = sum((position['cost_basis'] for position in positions), ZERO)
        unrealized = ZERO
        missing = 0
        for position in positions:
            if position['quantity'] <= 0:
                continue
            book = fresh.get(position['outcome']) if position['market_id'] == market_id else None
            if book is None or book.best_bid is None:
                missing += 1
                continue
            unrealized += position['quantity'] * book.best_bid - position['cost_basis']
        self.store.equity(self.bot_id, self.run_id, at_us=at, market_id=market_id,
                          cash=self.store.cash(self.bot_id), exposure=exposure,
                          realized=self.store.realized(self.bot_id),
                          unrealized=None if missing else unrealized, missing_marks=missing)

    def _abandon(self, window: dict, at: int, status: str, note: str, kind: str) -> None:
        self.store.close_window(self.bot_id, window['market_id'], status, at_us=at, note=note)
        self.store.event(self.bot_id, self.run_id, at_us=at, kind=kind,
                         detail=f'{window["market_id"]}:{note}')

    def _beat(self, at: int, market_id: str | None, status: str, error: str | None) -> None:
        self.store.heartbeat(self.bot_id, self.run_id, at_us=at, market_id=market_id,
                             status=status, cycles=self.cycles, last_error=error)

    @staticmethod
    def _slot_of(slot_id: str | None) -> int | None:
        if not slot_id:
            return None
        suffix = str(slot_id).rsplit(':', 1)[-1]
        return int(suffix) if suffix.isdigit() else None
