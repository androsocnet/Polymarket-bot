"""FREE_REFERENCE approximate observation model and provider protocol.

Approximate observations are credential-free, never VERIFIED, and carry no
Chainlink signature or boundary semantics. They provide sample-mean evidence
for research and fallback only.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable, Callable
from datetime import datetime, timezone
import hashlib
import httpx
import time

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.numeric import parse_decimal


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


class KrakenRestProvider:
    """Kraken REST provider implementing ApproximateReferenceProvider.

    Fetches ticker from GET /0/public/Ticker?pair=XBTUSD&assetVersion=1
    which returns result key 'BTC/USD'. Per trade price parsed from 'c' field.
    source_timestamp_us is ALWAYS None — Kraken Ticker has no trustworthy
    per-trade exchange timestamp.
    """

    _ENDPOINT = "https://api.kraken.com/0/public/Ticker"
    _TIMEOUT_SECONDS = 5.0
    _MAX_PAYLOAD_BYTES = 1 * 1024 * 1024
    _PARSER_REVISION = "kraken-ticker-v1"

    def __init__(self, client: httpx.Client, *, monotonic_ns, wall_us):
        self._client = client
        self._monotonic_ns = monotonic_ns
        self._wall_us = wall_us

    @property
    def provider_name(self) -> str:
        return "kraken"

    @property
    def pair(self) -> str:
        return "BTC/USD"

    @property
    def quote_currency(self) -> str:
        return "USD"

    @property
    def _endpoint_url(self) -> str:
        return self._ENDPOINT

    def fetch_ticker(self, *, deadline: float) -> ApproximateObservation:
        now_wall_us = self._wall_us()
        now_server_us = self._monotonic_ns() // 1000
        now_epoch = now_wall_us / 1_000_000
        if now_epoch > deadline:
            return self._error_obs(
                now_wall_us, now_server_us, None, 0, 0, "DEADLINE_EXPIRED"
            )

        remaining = deadline - now_epoch
        if remaining <= 0:
            return self._error_obs(
                now_wall_us, now_server_us, None, 0, 0, "DEADLINE_EXPIRED"
            )

        timeout = min(self._TIMEOUT_SECONDS, remaining)
        try:
            response = self._client.get(
                self._endpoint_url,
                params={"pair": "XBTUSD", "assetVersion": "1"},
                timeout=timeout,
            )
        except httpx.TimeoutException:
            return self._error_obs(
                now_wall_us, now_server_us, None, 0, 0, "TRANSPORT_TIMEOUT"
            )
        except httpx.HTTPError:
            return self._error_obs(
                now_wall_us, now_server_us, None, 0, 0, "TRANSPORT_ERROR"
            )

        body = response.content
        raw_payload_size = len(body)
        if raw_payload_size > self._MAX_PAYLOAD_BYTES:
            return self._error_obs(
                now_wall_us, now_server_us, None, response.status_code, raw_payload_size, "PAYLOAD_TOO_LARGE"
            )

        raw_payload_hash = hashlib.sha256(body).hexdigest()[:16]

        if response.status_code != 200:
            return self._error_obs(
                now_wall_us, now_server_us, raw_payload_hash, response.status_code, raw_payload_size, "HTTP_ERROR"
            )

        try:
            payload = response.json()
        except Exception:
            return self._error_obs(
                now_wall_us, now_server_us, raw_payload_hash, response.status_code, raw_payload_size, "MALFORMED_JSON"
            )

        error_array = payload.get("error", [])
        if error_array:
            return self._error_obs(
                now_wall_us, now_server_us, raw_payload_hash, response.status_code, raw_payload_size, "KRAKEN_API_ERROR"
            )

        result = payload.get("result", {})
        if not isinstance(result, dict) or "BTC/USD" not in result:
            return self._error_obs(
                now_wall_us, now_server_us, raw_payload_hash, response.status_code, raw_payload_size, "WRONG_PAIR_KEY"
            )

        ticker = result["BTC/USD"]
        try:
            price_str = ticker["c"][0]
            bid_str = ticker["b"][0]
            ask_str = ticker["a"][0]
            price = Decimal(price_str)
            bid_price = Decimal(bid_str)
            ask_price = Decimal(ask_str)
        except (KeyError, IndexError, TypeError, ValueError, ArithmeticError):
            return self._error_obs(
                now_wall_us, now_server_us, raw_payload_hash, response.status_code, raw_payload_size, "MALFORMED_JSON"
            )

        if price <= 0:
            return self._error_obs(
                now_wall_us, now_server_us, raw_payload_hash, response.status_code, raw_payload_size, "NON_POSITIVE_PRICE"
            )

        return ApproximateObservation(
            provider="kraken",
            pair="BTC/USD",
            quote_currency="USD",
            price=price,
            bid_price=bid_price,
            ask_price=ask_price,
            source_timestamp_us=None,
            receipt_wall_us=now_wall_us,
            receipt_server_us=now_server_us,
            raw_payload_hash=raw_payload_hash,
            raw_payload_size=raw_payload_size,
            transport_type="rest",
            parser_revision=self._PARSER_REVISION,
            http_status=response.status_code,
            result="success",
            error_code=None,
        )

    def _error_obs(
        self,
        receipt_wall_us: int,
        receipt_server_us: int | None,
        raw_payload_hash: str | None,
        http_status: int | None,
        raw_payload_size: int,
        error_code: str,
    ) -> ApproximateObservation:
        return ApproximateObservation(
            provider="kraken",
            pair="BTC/USD",
            quote_currency="USD",
            price=Decimal("0"),
            bid_price=None,
            ask_price=None,
            source_timestamp_us=None,
            receipt_wall_us=receipt_wall_us,
            receipt_server_us=receipt_server_us,
            raw_payload_hash=raw_payload_hash or "",
            raw_payload_size=raw_payload_size,
            transport_type="rest",
            parser_revision=self._PARSER_REVISION,
            http_status=http_status,
            result="error",
            error_code=error_code,
        )


class CoinbaseRestProvider:
    """Coinbase REST provider implementing ApproximateReferenceProvider.

    Fetches ticker from GET /products/BTC-USD/ticker.
    Per-trade price parsed from 'price' field. Exchange timestamp from 'time' field.
    source_timestamp_us is parsed from the 'time' ISO 8601 field (trustworthy).
    """

    _ENDPOINT = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
    _TIMEOUT_SECONDS = 5.0
    _MAX_PAYLOAD_BYTES = 1 * 1024 * 1024
    _PARSER_REVISION = "coinbase-ticker-v1"

    def __init__(
        self,
        client: httpx.Client,
        *,
        monotonic_ns: Callable[[], int],
        wall_us: Callable[[], int],
        clock_seconds: Callable[[], float] | None = None,
    ):
        self._client = client
        self._monotonic_ns = monotonic_ns
        self._wall_us = wall_us
        self._clock_seconds = clock_seconds

    @property
    def provider_name(self) -> str:
        return "coinbase"

    @property
    def pair(self) -> str:
        return "BTC/USD"

    @property
    def quote_currency(self) -> str:
        return "USD"

    def fetch_ticker(self, *, deadline: float) -> ApproximateObservation:
        now_wall_us = self._wall_us()
        now_server_us = self._monotonic_ns() // 1000
        now_epoch = now_wall_us / 1_000_000
        if now_epoch > deadline:
            return self._error_obs(
                now_wall_us, now_server_us, None, 0, 0, "DEADLINE_EXPIRED"
            )

        remaining = deadline - now_epoch
        if remaining <= 0:
            return self._error_obs(
                now_wall_us, now_server_us, None, 0, 0, "DEADLINE_EXPIRED"
            )

        timeout = min(self._TIMEOUT_SECONDS, remaining)
        try:
            response = self._client.get(
                self._ENDPOINT,
                timeout=timeout,
            )
        except httpx.TimeoutException:
            return self._error_obs(
                now_wall_us, now_server_us, None, 0, 0, "TRANSPORT_TIMEOUT"
            )
        except httpx.HTTPError:
            return self._error_obs(
                now_wall_us, now_server_us, None, 0, 0, "TRANSPORT_ERROR"
            )

        body = response.content
        raw_payload_size = len(body)
        if raw_payload_size > self._MAX_PAYLOAD_BYTES:
            return self._error_obs(
                now_wall_us, now_server_us, None, response.status_code, raw_payload_size, "PAYLOAD_TOO_LARGE"
            )

        raw_payload_hash = hashlib.sha256(body).hexdigest()

        if response.status_code != 200:
            return self._error_obs(
                now_wall_us, now_server_us, raw_payload_hash, response.status_code, raw_payload_size, "HTTP_ERROR"
            )

        try:
            payload = response.json()
        except Exception:
            return self._error_obs(
                now_wall_us, now_server_us, raw_payload_hash, response.status_code, raw_payload_size, "MALFORMED_JSON"
            )

        try:
            price = parse_decimal(payload["price"], field="price")
            bid_price = parse_decimal(payload["bid"], field="bid")
            ask_price = parse_decimal(payload["ask"], field="ask")
            time_str = payload["time"]
        except (KeyError, TypeError, MarketDataError):
            return self._error_obs(
                now_wall_us, now_server_us, raw_payload_hash, response.status_code, raw_payload_size, "MALFORMED_JSON"
            )

        if price <= 0:
            return self._error_obs(
                now_wall_us, now_server_us, raw_payload_hash, response.status_code, raw_payload_size, "NON_POSITIVE_PRICE"
            )

        try:
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            if dt.tzinfo is None or dt.utcoffset() is None:
                raise ValueError("Timezone required")
            dt = dt.astimezone(timezone.utc)
            delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
            source_timestamp_us = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
        except (ValueError, TypeError, OverflowError):
            return self._error_obs(
                now_wall_us, now_server_us, raw_payload_hash, response.status_code, raw_payload_size, "MALFORMED_JSON"
            )

        if source_timestamp_us > now_wall_us:
            return self._error_obs(
                now_wall_us, now_server_us, raw_payload_hash, response.status_code, raw_payload_size, "FUTURE_SOURCE_TIME"
            )

        return ApproximateObservation(
            provider="coinbase",
            pair="BTC/USD",
            quote_currency="USD",
            price=price,
            bid_price=bid_price,
            ask_price=ask_price,
            source_timestamp_us=source_timestamp_us,
            receipt_wall_us=now_wall_us,
            receipt_server_us=now_server_us,
            raw_payload_hash=raw_payload_hash,
            raw_payload_size=raw_payload_size,
            transport_type="rest",
            parser_revision=self._PARSER_REVISION,
            http_status=response.status_code,
            result="success",
            error_code=None,
        )

    def _error_obs(
        self,
        receipt_wall_us: int,
        receipt_server_us: int | None,
        raw_payload_hash: str | None,
        http_status: int | None,
        raw_payload_size: int,
        error_code: str,
    ) -> ApproximateObservation:
        return ApproximateObservation(
            provider="coinbase",
            pair="BTC/USD",
            quote_currency="USD",
            price=Decimal("0"),
            bid_price=None,
            ask_price=None,
            source_timestamp_us=None,
            receipt_wall_us=receipt_wall_us,
            receipt_server_us=receipt_server_us,
            raw_payload_hash=raw_payload_hash or "",
            raw_payload_size=raw_payload_size,
            transport_type="rest",
            parser_revision=self._PARSER_REVISION,
            http_status=http_status,
            result="error",
            error_code=error_code,
        )