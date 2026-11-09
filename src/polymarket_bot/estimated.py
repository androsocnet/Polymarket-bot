"""Offline PAPER execution estimates. Never imported by strict execution engine.

First eligible local receipt is a proxy, NOT exchange matching time or proof of
upstream freshness. No interpolation, queue model, resting orders or strategy
replay. Caller must preserve strategy/risk/account feedback before another order.
"""
from dataclasses import dataclass
from decimal import Decimal, localcontext

from .linux_mvp.fees import CUTOVER_US, MODEL, STATUS, level_fee
from .linux_mvp.store import ARITHMETIC

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class Book:
    session: str
    market_id: str
    condition_id: str
    token_id: str
    received_ns: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    min_size: Decimal
    tick_size: Decimal
    fees: dict | None
    fee_received_ns: int
    received_wall_us: int


@dataclass(frozen=True)
class Order:
    session: str
    market_id: str
    condition_id: str
    token_id: str
    submitted_ns: int
    deadline_ns: int
    side: str
    quantity: Decimal
    limit: Decimal
    kind: str = 'FAK'
    cancel_requested_ns: int | None = None


@dataclass(frozen=True)
class Scenario:
    # Assumptions only: 150ms disputed crypto delay + 100ms local pipeline.
    exchange_delay_ns: int = 150_000_000
    local_delay_ns: int = 100_000_000
    depth_fraction: Decimal = ONE
    extra_cost_bps: Decimal = ZERO
    max_spread: Decimal = ONE


def _number(value, low=ZERO, high=None):
    if (not isinstance(value, Decimal) or not value.is_finite() or value < low
            or (high is not None and value > high)):
        raise ValueError('invalid finite Decimal')
    # ponytail: 50 digits, exponent +/-50; larger scales need new arithmetic bounds.
    if len(value.as_tuple().digits) > 50 or not -50 <= value.as_tuple().exponent <= 50:
        raise ValueError('unsupported Decimal precision or scale')


def _time(value):
    if type(value) is not int or value < 0:
        raise ValueError('invalid monotonic nanoseconds')


def _identity(value):
    identity = tuple(getattr(value, k) for k in ('session', 'market_id', 'condition_id', 'token_id'))
    if any(not isinstance(v, str) or not v.strip() for v in identity):
        raise ValueError('missing session/market/condition/token identity')
    return identity


def _order_identity(order):
    """Identity including side to distinguish buy vs sell for same token."""
    base = _identity(order)
    if not isinstance(order.side, str) or order.side not in ('BUY', 'SELL'):
        raise ValueError('missing or invalid order side')
    return base + (order.side,)


def _book(book, identity):
    if _identity(book) != identity:
        raise ValueError('book identity/session mismatch')
    _time(book.received_ns)
    _time(book.fee_received_ns)
    _time(book.received_wall_us)  # metadata applicability only; never execution causality
    _number(book.min_size)
    _number(book.tick_size, high=ONE)
    if book.min_size <= 0 or book.tick_size <= 0:
        raise ValueError('missing minimum size or tick')
    for levels in (book.bids, book.asks):
        if not isinstance(levels, tuple):
            raise ValueError('full visible price/size levels required')
        seen = set()
        for level in levels:
            _number(level.price, high=ONE)
            _number(level.size)
            if level.price % book.tick_size or level.price in seen:
                raise ValueError('off-tick or duplicate depth level')
            seen.add(level.price)
    if book.bids and book.asks and max(x.price for x in book.bids) >= min(x.price for x in book.asks):
        raise ValueError('crossed or locked book')


def _fees(book):
    evidence = book.fees
    try:
        if not isinstance(evidence, dict):
            return False
        tokens = evidence.get('tokens')
        raw = evidence['raw']
        rate = evidence['rate']
        if isinstance(rate, bool) or not isinstance(rate, (str, int, Decimal)):
            return False
        rate = Decimal(rate)
        _number(rate, high=ONE)
        for key in ('observed_us', 'window_start_us', 'window_end_us'):
            _time(evidence[key])
        schedule = raw.get('feeSchedule')
        schedule_valid = raw.get('feesEnabled') is False and schedule is None and rate == ZERO
        if isinstance(schedule, dict):
            raw_rate = schedule.get('rate')
            schedule_valid = (raw.get('feesEnabled') is True and not isinstance(raw_rate, bool)
                              and isinstance(raw_rate, (str, int, Decimal))
                              and Decimal(raw_rate) == rate and type(schedule.get('exponent')) is int
                              and schedule['exponent'] == 1 and schedule.get('takerOnly') is True)
        return (evidence.get('status') == STATUS and evidence.get('model') == MODEL
                and evidence.get('market_id') == book.market_id
                and evidence.get('condition_id') == book.condition_id
                and isinstance(tokens, (list, tuple)) and len(tokens) == 2
                and all(isinstance(t, str) and t for t in tokens) and len(set(tokens)) == 2
                and book.token_id in tokens
                and type(evidence.get('exponent')) is int and evidence['exponent'] == 1
                and raw.get('version') == 'v1' and schedule_valid
                and CUTOVER_US <= evidence['window_start_us'] <= evidence['observed_us']
                <= book.received_wall_us < evidence['window_end_us']
                and book.fee_received_ns <= book.received_ns)
    except (ValueError, TypeError, ArithmeticError, KeyError, AttributeError):
        return False


class Estimator:
    """Stateless per-call execution estimator for PAPER_ONLY offline replay.

    Each call to execute() is independent.  The DepthLedger in the replay
    engine owns lifetime cross-order depth consumption; this class only
    validates that receipts passed to a single execute() call are strictly
    increasing and match the order's identity.  Instantiate once and reuse
    across orders, or create a fresh instance per call — the behaviour is
    identical.
    """

    def execute(self, order, books, scenario, *, cash, held):
        """Estimate one terminal FAK/FOK; no account mutations or risk authorizations.

        Explicit deadline is model observation lifetime, not exchange GTD. Cancel
        during pending delay becomes effective at ready time; ties cancel first.
        Missing receipt evidence yields UNEVALUATED with unknown filled quantity.
        """
        with localcontext(ARITHMETIC):
            return self._execute(order, books, scenario, cash=cash, held=held)

    def _execute(self, order, books, scenario, *, cash, held):
        identity = _identity(order)
        for moment in (order.submitted_ns, order.deadline_ns,
                       scenario.exchange_delay_ns, scenario.local_delay_ns):
            _time(moment)
        if order.cancel_requested_ns is not None:
            _time(order.cancel_requested_ns)
            if order.cancel_requested_ns < order.submitted_ns:
                raise ValueError('cancel precedes submission')
        if order.deadline_ns <= order.submitted_ns:
            raise ValueError('deadline must follow submission')
        _number(order.quantity)
        _number(order.limit, high=ONE)
        _number(cash)
        _number(held)
        _number(scenario.depth_fraction, high=ONE)
        _number(scenario.extra_cost_bps, high=Decimal(10000))
        _number(scenario.max_spread, high=ONE)
        if order.quantity <= 0 or order.side not in ('BUY', 'SELL') or order.kind not in ('FAK', 'FOK'):
            raise ValueError('invalid quantity, side or unsupported order kind')
        books = tuple(books)
        previous = -1
        for book in books:
            _book(book, identity)
            if book.received_ns <= previous:
                raise ValueError('receipts must be strictly increasing within session')
            previous = book.received_ns
        ready = order.submitted_ns + scenario.local_delay_ns + scenario.exchange_delay_ns
        cancel = (max(ready, order.cancel_requested_ns)
                  if order.cancel_requested_ns is not None else None)
        stop = min(order.deadline_ns, cancel if cancel is not None else order.deadline_ns)
        result = dict(mode='PAPER_ONLY_ESTIMATED', certified=False, upstream_freshness='UNKNOWN',
                      status='UNEVALUATED', reason='MISSING_SUBSEQUENT_BOOK', ready_ns=ready,
                      terminal_ns=None, received_ns=None, filled=None, cancelled=None,
                      notional=None, fee=None, extra_cost=None, cash_delta=None,
                      position_delta=None, fills=())
        selected = next((b for b in books if ready <= b.received_ns < stop), None)
        if selected is None:
            # No matching-delay receipt is not evidence of absent exchange liquidity.
            if cancel is not None and cancel <= order.deadline_ns and cancel == ready:
                result.update(status='CANCELLED', reason='CANCEL_EFFECTIVE', terminal_ns=stop,
                              filled=ZERO, cancelled=order.quantity, notional=ZERO, fee=ZERO,
                              extra_cost=ZERO, cash_delta=ZERO, position_delta=ZERO)
            elif cancel is not None and cancel <= order.deadline_ns:
                result.update(status='CANCELLED', reason='CANCELLED_WITH_EVIDENCE_GAP', terminal_ns=stop)
            return result
        book = selected
        result.update(received_ns=book.received_ns, terminal_ns=book.received_ns)
        if not _fees(book):
            result.update(reason='UNVERIFIED_FEE_METADATA')
            return result
        result.update(status='NO_FILL', reason='LIMIT_OR_DEPTH', filled=ZERO,
                      cancelled=order.quantity, notional=ZERO, fee=ZERO,
                      extra_cost=ZERO, cash_delta=ZERO, position_delta=ZERO)
        if order.quantity < book.min_size:
            result.update(reason='MINIMUM_SIZE')
            return result
        if order.limit % book.tick_size:
            result.update(reason='INVALID_TICK')
            return result
        if book.bids and book.asks and min(x.price for x in book.asks) - max(x.price for x in book.bids) > scenario.max_spread:
            result.update(reason='SPREAD_LIMIT')
            return result
        if order.side == 'SELL' and held < order.quantity:
            result.update(reason='INSUFFICIENT_POSITION')
            return result
        levels = sorted(book.asks if order.side == 'BUY' else book.bids,
                        key=lambda x: x.price, reverse=order.side == 'SELL')
        fills = []
        filled = notional = fee = ZERO
        for level in levels:
            if (order.side == 'BUY' and level.price > order.limit
                    or order.side == 'SELL' and level.price < order.limit):
                break
            take = min(order.quantity - filled, level.size * scenario.depth_fraction)
            if take > 0:
                fills.append((level.price, take))
                filled += take
                notional += take * level.price
                fee += level_fee(book.fees, take, level.price)
            if filled == order.quantity:
                break
        if not filled or order.kind == 'FOK' and filled < order.quantity:
            return result
        if not ZERO <= filled <= order.quantity:
            raise ArithmeticError('execution quantity invariant violated')
        cost = notional * scenario.extra_cost_bps / 10000
        if order.side == 'BUY' and notional + fee + cost > cash:
            result.update(reason='INSUFFICIENT_CASH')
            return result
        if order.side == 'SELL' and fee + cost > notional + cash:
            result.update(reason='INSUFFICIENT_CASH')
            return result
        result.update(status='FILLED' if filled == order.quantity else 'PARTIAL',
                      reason='VISIBLE_DEPTH_ESTIMATE', filled=filled, cancelled=order.quantity - filled,
                      notional=notional, fee=fee, extra_cost=cost, fills=tuple(fills),
                      cash_delta=(-notional if order.side == 'BUY' else notional) - fee - cost,
                      position_delta=filled if order.side == 'BUY' else -filled)
        return result
