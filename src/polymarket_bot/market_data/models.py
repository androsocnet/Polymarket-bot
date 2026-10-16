from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

class MarketDataError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int | None = None,
                 market: MarketDescriptor | None = None):
        super().__init__(message)
        self.code, self.http_status, self.market = code, http_status, market

    @property
    def reason(self) -> str:
        from json import JSONDecodeError
        cause: BaseException | None = self
        numeric = False
        while cause is not None:
            if isinstance(cause, (JSONDecodeError, UnicodeDecodeError)):
                return 'JSON_DECODE_ERROR'
            numeric = numeric or isinstance(cause, MarketDataError) and cause.code == 'invalid_numeric'
            cause = cause.__cause__
        if numeric:
            return 'NUMERIC_PARSE_ERROR'
        if self.code == 'transport_error' and str(self) == 'Response exceeds 10 MiB.':
            return 'RESPONSE_TOO_LARGE'
        if self.code == 'no_next_market':
            return 'NO_VALID_SUCCESSOR'
        if self.code == 'ambiguous_market':
            return 'AMBIGUOUS_SUCCESSOR'
        if str(self) == 'Successor event evidence exceeds bound.':
            return 'EVIDENCE_BOUND_EXCEEDED'
        return 'EVENT_SCHEMA_INVALID' if self.code == 'invalid_metadata' else self.code

@dataclass(frozen=True)
class TokenOutcome:
    label: str
    token_id: str
    metadata_price: Decimal | None = None

@dataclass(frozen=True)
class MarketDescriptor:
    event_id: str
    event_slug: str
    event_title: str
    market_id: str
    slug: str
    question: str
    condition_id: str
    window_start: datetime
    window_end: datetime
    active: bool
    closed: bool
    accepting_orders: bool | None
    enable_order_book: bool | None
    up: TokenOutcome
    down: TokenOutcome
    metadata: dict[str, object]
    warnings: tuple[str, ...]
    @property
    def pricing_ready(self) -> bool:
        return self.accepting_orders is True and self.enable_order_book is True

@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal

@dataclass(frozen=True)
class OutcomeBook:
    token_id: str
    condition_id: str
    source_timestamp_raw: str
    source_at: datetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    best_bid: Decimal | None
    best_ask: Decimal | None
    midpoint: Decimal | None
    spread: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None

@dataclass(frozen=True)
class BookObservation:
    outcome: TokenOutcome
    book: OutcomeBook
    request_started_at: datetime
    request_finished_at: datetime

@dataclass(frozen=True)
class MarketSnapshot:
    market: MarketDescriptor
    discovery_source: str
    up: BookObservation
    down: BookObservation
    collection_started_at: datetime
    collection_finished_at: datetime
    validated_at: datetime
    time_round_trip_seconds: float
    rolled_over: bool
