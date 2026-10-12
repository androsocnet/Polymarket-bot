"""FREE_REFERENCE approximate observation model and provider protocol.

Approximate observations are credential-free, never VERIFIED, and carry no
Chainlink signature or boundary semantics. They provide sample-mean evidence
for research and fallback only.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable


@runtime_checkable
class ApproximateReferenceProvider(Protocol):
    """Provider of approximate reference observations without credentials."""

    @property
    def provider_name(self) -> str: ...

    @property
    def pair(self) -> str: ...

    @property
    def quote_currency(self) -> str: ...

    def fetch_ticker(self, *, deadline: float) -> "ApproximateObservation": ...


@dataclass(frozen=True)
class ApproximateObservation:
    """Single approximate ticker observation from a public exchange endpoint.

    All monetary fields are raw strings from the exchange, parsed to Decimal.
    source_timestamp_us may be None for exchanges that do not provide one.
    receipt_server_us may be None if clock calibration is unavailable.
    """
    provider: str
    pair: str
    quote_currency: str
    price: Decimal
    bid_price: Decimal | None
    ask_price: Decimal | None
    source_timestamp_us: int | None
    receipt_wall_us: int
    receipt_server_us: int | None
    raw_payload_hash: str
    raw_payload_size: int
    transport_type: str
    parser_revision: str
    http_status: int | None
    result: str
    error_code: str | None


@dataclass(frozen=True)
class ApproximateReferenceReport:
    """Sample-mean reference report computed from 60-second approximate window.

    mode is hard-coded to APPROXIMATE. verification_status is always UNVERIFIED.
    value is None if computation failed; diagnostics contains error codes.
    """
    reference_kind: str
    window_start_us: int
    window_end_us: int
    observation_count: int
    source_provider: str
    was_fallback: bool
    receipt_wall_us: int
    value: Decimal | None = None
    mode: str = "APPROXIMATE"
    verification_status: str = "UNVERIFIED"
    receipt_server_us: int | None = None
    diagnostics: tuple[str, ...] = ()