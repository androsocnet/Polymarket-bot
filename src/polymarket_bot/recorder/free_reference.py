from contextlib import closing
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, Context, localcontext, ROUND_HALF_EVEN
from typing import Callable, Protocol, runtime_checkable
import hashlib
import json
import time

import httpx

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.numeric import parse_decimal


@runtime_checkable
class ApproximateReferenceProvider(Protocol):
    @property
    def provider_name(self) -> str: ...
    @property
    def pair(self) -> str: ...
    @property
    def quote_currency(self) -> str: ...
    def fetch_ticker(self, *, deadline: float) -> "ApproximateObservation": ...


@dataclass(frozen=True)
class ApproximateObservation:
    provider: str
    pair: str
    quote_currency: str
    price: Decimal | None
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
    receipt_mono_ns: int | None = None


@dataclass(frozen=True)
class ApproximateReferenceReport:
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
    receipt_mono_ns: int | None = None
    window_time_domain: str = "LOCAL_WALL"


class CoinbaseRestProvider:
    _ENDPOINT = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
    _TIMEOUT_SECONDS = 5.0
    _MAX_PAYLOAD_BYTES = 1048576
    _PARSER_REVISION = "coinbase-ticker-v1"
    provider_name = "coinbase"
    pair = "BTC/USD"
    quote_currency = "USD"

    def __init__(self, client: httpx.Client, *, monotonic_ns: Callable[[], int],
                 wall_us: Callable[[], int], clock_seconds=None, project=None, sleep=time.sleep):
        self._client = client
        self._monotonic_ns = monotonic_ns
        self._wall_us = wall_us
        self._project = project
        self._sleep = sleep

    def fetch_ticker(self, *, deadline: float) -> ApproximateObservation:
        end_ns = int(deadline * 1000000000)
        body = bytearray()
        status = None
        error = "DEADLINE_EXPIRED"
        for attempt in range(2):
            remaining = (end_ns - self._monotonic_ns()) / 1000000000
            if remaining <= 0:
                error = "DEADLINE_EXPIRED"
                break
            body = bytearray()
            status = None
            request = httpx.Request("GET", self._ENDPOINT,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                extensions={"timeout": dict.fromkeys(("connect", "read", "write", "pool"), min(5.0, remaining))})
            try:
                response = self._client.send(request, stream=True, auth=None, follow_redirects=False)
                self._client.cookies.clear()
                with closing(response):
                    status = response.status_code
                    chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
                    for chunk in chunks:
                        if self._monotonic_ns() >= end_ns:
                            error = "DEADLINE_EXPIRED"
                            break
                        available = self._MAX_PAYLOAD_BYTES - len(body)
                        body.extend(chunk[:available])
                        if len(chunk) > available:
                            error = "PAYLOAD_TOO_LARGE"
                            break
                    else:
                        error = None if status == 200 else "HTTP_ERROR"
                    if self._monotonic_ns() >= end_ns:
                        error = "DEADLINE_EXPIRED"
            except httpx.TimeoutException:
                error = "TRANSPORT_TIMEOUT"
            except httpx.HTTPError:
                error = "TRANSPORT_ERROR"
            if attempt == 0 and (status == 429 or error in ("TRANSPORT_TIMEOUT", "TRANSPORT_ERROR")):
                remaining = (end_ns - self._monotonic_ns()) / 1000000000
                if remaining > 0.1:
                    self._sleep(0.1)
                    continue
            break
        wall = self._wall_us()
        mono = self._monotonic_ns()
        server = self._project(mono) if self._project is not None else None
        price = bid = ask = source = None
        if error is None:
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError()
                values = []
                if self.provider_name == "kraken":
                    if not isinstance(payload.get("error"), list):
                        raise ValueError()
                    if payload["error"]:
                        error = "KRAKEN_API_ERROR"
                    result = payload.get("result")
                    if not isinstance(result, dict) or "BTC/USD" not in result:
                        error = error or "WRONG_PAIR_KEY"
                    if error is None:
                        if not isinstance(result, dict):
                            raise ValueError()
                        ticker = result["BTC/USD"]
                        if not isinstance(ticker, dict):
                            raise ValueError()
                        values = []
                        for key in ("c", "b", "a"):
                            value = ticker[key]
                            if not isinstance(value, list) or not value:
                                raise ValueError()
                            values.append(value[0])
                else:
                    values = [payload[key] for key in ("price", "bid", "ask")]
                if error is None:
                    price, bid, ask = (parse_decimal(value, field="ticker price") for value in values)
                    if any(value <= 0 for value in (price, bid, ask)):
                        error = "NON_POSITIVE_PRICE"
                    if self.provider_name == "coinbase":
                        stamp = payload["time"]
                        if not isinstance(stamp, str):
                            raise ValueError()
                        instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        if instant.tzinfo is None:
                            raise ValueError()
                        delta = instant.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
                        source = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
                        if source > wall or (server is not None and source > server):
                            error = "FUTURE_SOURCE_TIME"
            except (ValueError, KeyError, TypeError, ArithmeticError, MarketDataError):
                error = "MALFORMED_JSON"
        if error is not None:
            price = bid = ask = source = None
        return ApproximateObservation(self.provider_name, self.pair, self.quote_currency,
            price, bid, ask, source, wall, server, hashlib.sha256(body).hexdigest(), len(body),
            "rest", self._PARSER_REVISION, status, "error" if error else "success", error, mono)


class KrakenRestProvider(CoinbaseRestProvider):
    _ENDPOINT = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD&assetVersion=1"
    _PARSER_REVISION = "kraken-ticker-v1"
    provider_name = "kraken"


_WINDOW_US = 60000000


def compute_approximate_sample_mean_60s(observations: list[ApproximateObservation], *,
        receipt_server_us: int | None, receipt_wall_us: int,
        receipt_mono_ns: int | None = None) -> ApproximateReferenceReport:
    valid = []
    diagnostics = []
    for obs in observations:
        reason = None
        if obs.result != "success" or obs.error_code is not None or obs.price is None:
            reason = "EXCLUDED_ERROR_OBSERVATIONS"
        elif not obs.price.is_finite() or obs.price <= 0:
            reason = "EXCLUDED_ERROR_OBSERVATIONS"
        elif (obs.receipt_wall_us > receipt_wall_us
              or (obs.source_timestamp_us is not None and obs.source_timestamp_us > receipt_wall_us)
              or (receipt_server_us is not None and obs.receipt_server_us is not None
                  and obs.receipt_server_us > receipt_server_us)
              or (receipt_mono_ns is not None and obs.receipt_mono_ns is not None
                  and obs.receipt_mono_ns > receipt_mono_ns)):
            reason = "EXCLUDED_FUTURE_OBSERVATIONS"
        elif obs.receipt_wall_us < receipt_wall_us - _WINDOW_US:
            reason = "EXCLUDED_STALE_OBSERVATIONS"
        elif (receipt_mono_ns is not None and obs.receipt_mono_ns is not None
              and obs.receipt_mono_ns < receipt_mono_ns - _WINDOW_US * 1000):
            reason = "EXCLUDED_STALE_OBSERVATIONS"
        if reason:
            if reason not in diagnostics:
                diagnostics.append(reason)
        else:
            valid.append(obs)
    valid.sort(key=lambda obs: (obs.receipt_wall_us, obs.receipt_mono_ns or 0, obs.raw_payload_hash))
    if not valid:
        diagnostics.append("MISSING_OBSERVATIONS")
    elif (valid[0].receipt_wall_us > receipt_wall_us - _WINDOW_US + 2000000
          or any(b.receipt_wall_us - a.receipt_wall_us > 4000000 for a, b in zip(valid, valid[1:]))
          or receipt_wall_us - valid[-1].receipt_wall_us > 4000000):
        diagnostics.append("OBSERVATION_GAP")
    with localcontext(Context(prec=512, rounding=ROUND_HALF_EVEN)):
        mean = sum((obs.price for obs in valid), Decimal(0)) / len(valid) if valid else None
    return ApproximateReferenceReport("APPROXIMATE_SAMPLE_MEAN_60S",
        receipt_wall_us - _WINDOW_US, receipt_wall_us, len(valid),
        ",".join(sorted({obs.provider for obs in valid})), False, receipt_wall_us,
        mean,
        receipt_server_us=receipt_server_us, diagnostics=tuple(diagnostics), receipt_mono_ns=receipt_mono_ns)


class ApproximateReferenceSession:
    def __init__(self, *, providers, monotonic_ns, wall_us, sleep, project=None):
        self._providers = dict(providers)
        self._monotonic_ns = monotonic_ns
        self._wall_us = wall_us
        self._project = project
        self._history = {name: deque(maxlen=64) for name in ("coinbase", "kraken")}
        self._started = False

    def start(self):
        self._started = True

    def stop(self):
        self._started = False

    def check_health(self):
        pass

    def fetch(self, *, run_id, anchor_id, project, deadline):
        observations = []
        for name in ("coinbase", "kraken"):
            try:
                obs = self._providers[name].fetch_ticker(deadline=deadline)
            except Exception as exc:
                error = "MISSING_PROVIDER" if isinstance(exc, KeyError) else "PROVIDER_FAILURE"
                obs = ApproximateObservation(name, "BTC/USD", "USD", None, None, None, None,
                    self._wall_us(), None, hashlib.sha256(b"").hexdigest(), 0, "rest",
                    name + "-ticker-v1", None, "error", error, self._monotonic_ns())
            if obs.receipt_mono_ns is not None:
                obs = replace(obs, receipt_server_us=project(obs.receipt_mono_ns) if project else None)
            observations.append(obs)
            self._history[name].append(obs)
        mono = self._monotonic_ns()
        wall = self._wall_us()
        server = project(mono) if project else None
        reports = {}
        for name, history in self._history.items():
            reports[name] = compute_approximate_sample_mean_60s(list(history),
                receipt_wall_us=wall, receipt_server_us=server, receipt_mono_ns=mono)
            while history and history[0].receipt_wall_us < wall - _WINDOW_US:
                history.popleft()
        primary, fallback = reports["coinbase"], reports["kraken"]
        if primary.value is not None:
            report = replace(primary, source_provider="coinbase")
        elif fallback.value is not None:
            reason = observations[0].error_code or "STALE_OBSERVATION"
            report = replace(fallback, source_provider="kraken", was_fallback=True,
                diagnostics=fallback.diagnostics + ("FALLBACK_COINBASE_" + reason,))
        else:
            report = replace(primary, was_fallback=True, diagnostics=tuple(dict.fromkeys(
                primary.diagnostics + fallback.diagnostics + tuple(
                    "FALLBACK_" + obs.provider.upper() + "_" + (obs.error_code or "STALE_OBSERVATION")
                    for obs in observations))))
        return report, tuple(observations)
