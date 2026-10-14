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
from polymarket_bot.recorder.models import ClockAnchor


@dataclass(frozen=True)
class AcquisitionContext:
    revision: int
    run_id: int
    anchor: ClockAnchor
    anchor_id: int
    sampling_origin_ns: int
    discontinuity_end_ns: int | None


def _captured_projection(context: AcquisitionContext | None, moment: int | None) -> int | None:
    if not isinstance(context, AcquisitionContext) or not isinstance(context.anchor, ClockAnchor):
        return None
    anchor = context.anchor
    fields = (context.revision, context.run_id, context.anchor_id, context.sampling_origin_ns,
        anchor.request_start_mono_ns, anchor.request_end_mono_ns, anchor.midpoint_ns,
        anchor.server_us, anchor.uncertainty_us, moment)
    if any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in fields):
        return None
    if (context.revision != 2 or context.run_id <= 0 or context.anchor_id <= 0
            or anchor.status != 'VALID' or anchor.uncertainty_us < 1_000_000
            or anchor.request_start_mono_ns > anchor.midpoint_ns
            or anchor.midpoint_ns > anchor.request_end_mono_ns):
        return None
    if moment is None or moment < max(anchor.request_end_mono_ns, context.sampling_origin_ns):
        return None
    if moment - anchor.midpoint_ns > 120_000_000_000:
        return None
    if context.discontinuity_end_ns is not None:
        end = context.discontinuity_end_ns
        if type(end) is not int or not 0 <= end <= anchor.request_start_mono_ns:
            return None
    projected = anchor.server_us + (moment - anchor.midpoint_ns) // 1000
    return projected if 0 <= projected <= 2**63 - 1 else None


@runtime_checkable
class ApproximateReferenceProvider(Protocol):
    @property
    def provider_name(self) -> str: ...
    @property
    def pair(self) -> str: ...
    @property
    def quote_currency(self) -> str: ...
    def fetch_ticker(self, *, deadline: float | None = None,
                     context: AcquisitionContext | None = None) -> "ApproximateObservation": ...


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


def source_time_status(observation: ApproximateObservation, context: AcquisitionContext | None) -> str:
    projected = _captured_projection(context, observation.receipt_mono_ns)
    if projected is None or observation.receipt_server_us != projected:
        return 'CLOCK_CONTEXT_UNAVAILABLE'
    if observation.source_timestamp_us is None:
        return 'SOURCE_TIME_UNAVAILABLE'
    if type(observation.source_timestamp_us) is not int or not 0 <= observation.source_timestamp_us <= 2**63 - 1:
        return 'FUTURE_SOURCE_TIME'
    assert context is not None
    return ('SOURCE_TIME_WITHIN_CLOCK_UNCERTAINTY'
        if observation.source_timestamp_us <= projected + context.anchor.uncertainty_us
        else 'FUTURE_SOURCE_TIME')


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

    def fetch_ticker(self, *, deadline: float | None = None,
                     context: AcquisitionContext | None = None) -> ApproximateObservation:
        end_ns = (self._monotonic_ns() + 5_000_000_000 if deadline is None
                  else int(deadline * 1000000000))
        body = bytearray()
        status = None
        error = "DEADLINE_EXPIRED"
        last_attempt_ns = None
        chronology_invalid = False
        for attempt in range(2):
            send_ns = self._monotonic_ns()
            if context is not None and (_captured_projection(context, send_ns) is None
                    or (last_attempt_ns is not None and send_ns < last_attempt_ns)):
                chronology_invalid = True
                error = 'CLOCK_CONTEXT_UNAVAILABLE'
                break
            remaining = (end_ns - send_ns) / 1000000000
            if remaining <= 0:
                error = "DEADLINE_EXPIRED"
                break
            last_attempt_ns = send_ns
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
                retry_ns = self._monotonic_ns()
                if context is not None:
                    if retry_ns < last_attempt_ns:
                        chronology_invalid = True
                        error = 'CLOCK_CONTEXT_UNAVAILABLE'
                        break
                    last_attempt_ns = retry_ns
                remaining = (end_ns - retry_ns) / 1000000000
                if remaining > 0.1:
                    self._sleep(0.1)
                    continue
            break
        wall = self._wall_us()
        mono = self._monotonic_ns()
        server = (_captured_projection(context, mono) if context is not None
                  else self._project(mono) if self._project is not None else None)
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
                        if context is None and (source > wall or (server is not None and source > server)):
                            error = "FUTURE_SOURCE_TIME"
            except (ValueError, KeyError, TypeError, ArithmeticError, MarketDataError):
                error = "MALFORMED_JSON"
        if context is not None:
            observation = ApproximateObservation(self.provider_name, self.pair, self.quote_currency,
                price, bid, ask, source, wall, server, '', len(body), 'rest', self._PARSER_REVISION,
                status, 'error' if error else 'success', error, mono)
            policy_status = ('CLOCK_CONTEXT_UNAVAILABLE'
                if chronology_invalid or (last_attempt_ns is not None and mono < last_attempt_ns)
                else source_time_status(observation, context))
            if policy_status == 'CLOCK_CONTEXT_UNAVAILABLE' or (error is None and policy_status == 'FUTURE_SOURCE_TIME'):
                error = policy_status
        if error is not None:
            price = bid = ask = None
            if error not in ("FUTURE_SOURCE_TIME", 'CLOCK_CONTEXT_UNAVAILABLE'):
                source = None
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
