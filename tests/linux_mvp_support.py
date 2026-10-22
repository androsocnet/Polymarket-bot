"""SYNTHETIC Linux MVP fixtures. Fake market/time only; no network, no live data."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_bot.market_data.models import (BookLevel, BookObservation, MarketDataError,
                                               MarketDescriptor, MarketSnapshot, OutcomeBook,
                                               TokenOutcome)

START = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
CONDITION = '0x' + 'ab' * 32
UP_TOKEN, DOWN_TOKEN = '111111', '222222'
EVENT_ID, MARKET_ID = '2001', '1001'
ZERO = Decimal(0)
MIB = 1024 * 1024


def us(moment):
    return int(moment.timestamp() * 1_000_000)


def descriptor(*, market_id=MARKET_ID, start=START, up=UP_TOKEN, down=DOWN_TOKEN,
               condition=CONDITION, event=EVENT_ID, pricing_ready=True):
    return MarketDescriptor(
        event, 'btc-updown-15m-' + str(int(start.timestamp())), 'Bitcoin Up or Down?',
        market_id, 'slug-' + market_id, 'BTC Up or Down?', condition, start,
        start + timedelta(seconds=900), True, False,
        True if pricing_ready else None, True if pricing_ready else None,
        TokenOutcome('Up', up), TokenOutcome('Down', down), {}, ())


def book(token, *, bid='0.30', ask='0.32', condition=CONDITION, at=None, size='100'):
    at = at or START
    bid, ask = (None if bid is None else Decimal(bid)), (None if ask is None else Decimal(ask))
    bids = () if bid is None else (BookLevel(bid, Decimal(size)),)
    asks = () if ask is None else (BookLevel(ask, Decimal(size)),)
    both = bid is not None and ask is not None
    return OutcomeBook(
        token, condition, str(int(at.timestamp() * 1000)).zfill(13), at, bids, asks, bid, ask,
        (bid + ask) / 2 if both else None, ask - bid if both else None,
        Decimal(size) if bid is not None else None, Decimal(size) if ask is not None else None)


def snapshot(*, slot, start=START, market=None, up_bid='0.30', up_ask='0.32',
             down_bid='0.68', down_ask='0.70', lag=0.1, size='100'):
    """One coherent pair. `lag` is validated_at minus each book completion."""
    market = market or descriptor(start=start, condition=CONDITION)
    at = start + timedelta(seconds=2 * slot + 1)
    up = BookObservation(market.up, book(market.up.token_id, bid=up_bid, ask=up_ask,
                                         condition=market.condition_id, at=at, size=size), at, at)
    down = BookObservation(market.down, book(market.down.token_id, bid=down_bid, ask=down_ask,
                                             condition=market.condition_id, at=at, size=size), at, at)
    validated = at + timedelta(seconds=lag)
    return MarketSnapshot(market, 'fast_path', up, down, at, at, validated, 0.01, False)


def rising(slot, *, start=START, market=None, step='0.02', base='0.30'):
    up = Decimal(base) + Decimal(step) * slot
    return snapshot(slot=slot, start=start, market=market, up_bid=str(up), up_ask=str(up + Decimal('0.02')),
                    down_bid=str(1 - up - Decimal('0.02')), down_ask=str(1 - up))


class Collector:
    """Deterministic queue; an Exception instance is raised instead of returned."""

    def __init__(self, samples):
        self.samples = list(samples)
        self.calls = 0

    def collect(self):
        self.calls += 1
        if not self.samples:
            raise MarketDataError('transport_error', 'synthetic samples exhausted')
        sample = self.samples.pop(0)
        if isinstance(sample, BaseException):
            raise sample
        return sample
