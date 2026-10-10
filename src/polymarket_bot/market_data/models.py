from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

class MarketDataError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int | None = None,
                 market: MarketDescriptor | None = None):
        super().__init__(message)
        self.code, self.http_status, self.market = code, http_status, market

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
