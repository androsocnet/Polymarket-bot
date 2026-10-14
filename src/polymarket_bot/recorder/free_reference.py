from contextlib import closing
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, Context, localcontext, ROUND_HALF_EVEN
from typing import Callable, Protocol, Sequence, runtime_checkable
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
    run_id: str
    anchor: ClockAnchor
    anchor_id: int
    sampling_origin_ns: int
    discontinuity_end_ns: int | None


def _captured_projection(context: AcquisitionContext | None, moment: int | None) -> int | None:
    if not isinstance(context, AcquisitionContext) or not isinstance(context.anchor, ClockAnchor):
        return None
    anchor = context.anchor
    fields = (context.revision, context.anchor_id, context.sampling_origin_ns,
        anchor.request_start_mono_ns, anchor.request_end_mono_ns, anchor.midpoint_ns,
        anchor.server_us, anchor.uncertainty_us, moment)
    if any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in fields):
        return None
    if (context.revision != 2 or type(context.run_id) is not str or not context.run_id.strip()
            or context.anchor_id <= 0
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
        self.request_attempts = []

    def fetch_ticker(self, *, deadline: float | None = None,
                     context: AcquisitionContext | None = None) -> ApproximateObservation:
        end_ns = (self._monotonic_ns() + 5_000_000_000 if deadline is None
                  else int(deadline * 1000000000))
        self.request_attempts = []
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
            self.request_attempts.append(dict(provider=self.provider_name, attempt=attempt + 1,
                started_mono_ns=send_ns, ended_mono_ns=self._monotonic_ns(),
                http_status=status, error_code=error))
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


@dataclass(frozen=True)
class ResearchWindowEvidence:
    revision: int
    run_id: str
    sampling_origin_ns: int
    observation_ids: tuple[int, ...]
    contexts: tuple[AcquisitionContext, ...]
    scheduled_opportunities: tuple[tuple[int, int, str], ...]
    request_outcomes: tuple[tuple[int, str, int, int, str], ...]
    discontinuity_ends_ns: tuple[int, ...]
    validated_window_start_ns: int | None = None
    validated_window_end_ns: int | None = None
    expected_requests: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class ResearchAssessment:
    revision: int
    selected_provider: str
    contributing_ids: tuple[int, ...]
    value_status: str
    coverage_quality: str
    warmup_status: str
    strict_gap: bool
    reasons: tuple[str, ...]
    value: Decimal | None
    occupied_cells: tuple[int, ...]
    empty_cells: tuple[int, ...]
    empty_pairs: tuple[int, ...]
    exposed_pairs: tuple[int, ...]
    partial_pair: int | None
    provisional: bool
    max_wall_gap_us: int
    max_mono_gap_ns: int
    head_wall_us: int | None
    tail_wall_us: int | None
    head_mono_ns: int | None
    tail_mono_ns: int | None
    failed_requests: int
    excluded_observations: int
    missing_requests: int
    source_statuses: tuple[tuple[int, str], ...]
    cell_phase_offsets_ns: tuple[int, ...]
    provider_availability: tuple[tuple[str, int], ...]
    scheduled_receipt_jitter_ns: tuple[tuple[int, int], ...]
    same_fetch_disagreements: tuple[tuple[int, int | None, int | None,
        Decimal | None, Decimal | None, Decimal | None, int | None], ...]


def assess_provider(observations: Sequence[ApproximateObservation], *, provider: str,
        report_wall_us: int, report_ns: int, evidence: ResearchWindowEvidence,
        was_fallback: bool = False) -> ResearchAssessment:
    reasons = []
    start = report_ns - 60_000_000_000
    wall_start = report_wall_us - _WINDOW_US
    origin = evidence.sampling_origin_ns
    provenance = (evidence.revision == 2 and provider in ('coinbase', 'kraken')
        and type(evidence.run_id) is str and bool(evidence.run_id.strip())
        and type(origin) is int and 0 <= origin <= report_ns
        and len(observations) == len(evidence.observation_ids) == len(evidence.contexts)
        and all(type(i) is int and i > 0 for i in evidence.observation_ids)
        and len(set(evidence.observation_ids)) == len(evidence.observation_ids))
    valid = []
    statuses = []
    occupied = set()
    failed = excluded = 0
    available = {}
    for index, obs in enumerate(observations):
        context = evidence.contexts[index] if index < len(evidence.contexts) else None
        identity = (obs.provider in ('coinbase', 'kraken') and obs.pair == 'BTC/USD'
            and obs.quote_currency == 'USD' and obs.transport_type == 'rest'
            and obs.parser_revision == obs.provider + '-ticker-v1')
        numeric = (identity and obs.result == 'success' and obs.error_code is None
            and obs.http_status == 200 and isinstance(obs.price, Decimal)
            and obs.price.is_finite() and obs.price > 0)
        wall_member = wall_start <= obs.receipt_wall_us <= report_wall_us
        mono = obs.receipt_mono_ns
        mono_member = type(mono) is int and start <= mono <= report_ns
        status = source_time_status(obs, context)
        if (context is None or context.run_id != evidence.run_id
                or context.sampling_origin_ns != origin or status == 'CLOCK_CONTEXT_UNAVAILABLE'):
            provenance = False
        policy_ok = (status == 'SOURCE_TIME_WITHIN_CLOCK_UNCERTAINTY'
            if obs.provider == 'coinbase' else status == 'SOURCE_TIME_UNAVAILABLE'
                and obs.source_timestamp_us is None)
        if numeric and wall_member and mono_member and policy_ok and index < len(evidence.observation_ids):
            available[evidence.observation_ids[index]] = obs
        if obs.provider != provider:
            continue
        if numeric and wall_member != mono_member:
            reasons.append('WINDOW_MEMBERSHIP_DISAGREEMENT')
        policy_ok = (status == 'SOURCE_TIME_WITHIN_CLOCK_UNCERTAINTY'
            if provider == 'coinbase' else status == 'SOURCE_TIME_UNAVAILABLE'
                and obs.source_timestamp_us is None)
        if not numeric or not wall_member or not mono_member or not policy_ok:
            if wall_member or mono_member:
                excluded += 1
            continue
        assert mono is not None
        if mono < origin:
            provenance = False
            continue
        observation_id = evidence.observation_ids[index] if index < len(evidence.observation_ids) else 0
        statuses.append((observation_id, status))
        valid.append((observation_id, obs))
        occupied.add(min(29, (mono - start) // 2_000_000_000))
    ends = evidence.discontinuity_ends_ns + tuple(
        c.discontinuity_end_ns for c in evidence.contexts if c.discontinuity_end_ns is not None)
    if any(type(end) is not int or not 0 <= end <= report_ns for end in ends):
        provenance = False
    elif any(report_ns - end < 60_000_000_000 for end in ends):
        reasons.append('CLOCK_DISCONTINUITY')
    opportunity_ids = [row[0] for row in evidence.scheduled_opportunities]
    if (len(set(opportunity_ids)) != len(opportunity_ids)
            or any(type(i) is not int or i <= 0 for i in opportunity_ids)):
        provenance = False
    if report_ns > origin and not evidence.scheduled_opportunities:
        provenance = False
    missing = 0
    for _, target, disposition in evidence.scheduled_opportunities:
        if type(target) is not int or disposition not in ('completed', 'submitted', 'skipped', 'busy', 'unsubmitted'):
            provenance = False
        elif max(start, origin) <= target <= report_ns and disposition != 'completed':
            missing += 1
    if (evidence.validated_window_start_ns != max(start, origin)
            or evidence.validated_window_end_ns != report_ns):
        provenance = False
    opportunities = {i: (target, disposition) for i, target, disposition in evidence.scheduled_opportunities}
    members = dict(zip(evidence.observation_ids, observations))
    requests = {}
    seen_observations = set()
    jitter = []
    for row in evidence.request_outcomes:
        if len(row) != 5:
            provenance = False
            continue
        opportunity_id, name, observation_id, receipt_ns, outcome = row
        key = (opportunity_id, name)
        obs = members.get(observation_id)
        opportunity = opportunities.get(opportunity_id)
        if (type(opportunity_id) is not int or opportunity_id <= 0
                or name not in ('coinbase', 'kraken') or outcome not in ('success', 'error')
                or type(receipt_ns) is not int or receipt_ns < 0
                or key in requests or observation_id in seen_observations
                or obs is None or opportunity is None or opportunity[1] != 'completed'
                or obs.provider != name or obs.receipt_mono_ns != receipt_ns
                or outcome != ('success' if obs.result == 'success' and obs.error_code is None else 'error')):
            provenance = False
            continue
        requests[key] = observation_id
        seen_observations.add(observation_id)
        if start <= receipt_ns <= report_ns:
            if name == provider and outcome == 'error':
                failed += 1
            if observation_id in available:
                jitter.append((observation_id, receipt_ns - opportunity[0]))
    expected = evidence.expected_requests
    if (len(set(expected)) != len(expected) or set(expected) != set(requests)
            or seen_observations != set(evidence.observation_ids)):
        provenance = False
    for opportunity_id, (_, disposition) in opportunities.items():
        if disposition == 'completed' and not any(key[0] == opportunity_id for key in requests):
            provenance = False
    disagreements = []
    for opportunity_id in sorted({key[0] for key in requests}):
        coinbase_id = requests.get((opportunity_id, 'coinbase'))
        kraken_id = requests.get((opportunity_id, 'kraken'))
        coinbase = available.get(coinbase_id)
        kraken = available.get(kraken_id)
        if coinbase is None and kraken is None:
            continue
        difference = separation = None
        if coinbase is not None and kraken is not None:
            assert coinbase.price is not None and kraken.price is not None
            assert coinbase.receipt_mono_ns is not None and kraken.receipt_mono_ns is not None
            with localcontext(Context(prec=512, rounding=ROUND_HALF_EVEN)):
                difference = kraken.price - coinbase.price
            separation = kraken.receipt_mono_ns - coinbase.receipt_mono_ns
        disagreements.append((opportunity_id, coinbase_id, kraken_id,
            coinbase.price if coinbase else None, kraken.price if kraken else None,
            difference, separation))
    if not provenance:
        reasons.append('INCOMPLETE_PROVENANCE')
    warming = origin > start
    exposed = tuple(i for i in range(15) if start + i * 4_000_000_000 >= origin)
    partial = next((i for i in range(15)
        if start + i * 4_000_000_000 < origin < start + (i + 1) * 4_000_000_000), None)
    empty_pairs = tuple(i for i in exposed if not ({2 * i, 2 * i + 1} & occupied))
    ordered = sorted(valid, key=lambda item: (item[1].receipt_wall_us,
        item[1].receipt_mono_ns or 0, item[1].raw_payload_hash))
    arithmetic = compute_approximate_sample_mean_60s([
        replace(obs, source_timestamp_us=None, receipt_server_us=None) for _, obs in ordered],
        receipt_server_us=None, receipt_wall_us=report_wall_us, receipt_mono_ns=report_ns)
    walls = sorted(obs.receipt_wall_us for _, obs in valid)
    monos = sorted(obs.receipt_mono_ns for _, obs in valid if obs.receipt_mono_ns is not None)
    wall_gap = max((b - a for a, b in zip(walls, walls[1:])), default=0)
    mono_gap = max((b - a for a, b in zip(monos, monos[1:])), default=0)
    value_status = 'VALID' if arithmetic.value is not None else 'INVALID'
    if value_status == 'INVALID':
        reasons.append('INVALID_VALUE')
    if empty_pairs or not exposed:
        reasons.append('EMPTY_EXPOSED_PAIRS')
    quality = ('INSUFFICIENT' if reasons else 'COMPLETE'
        if len(occupied) == 30 and not (failed or excluded or missing or was_fallback)
        else 'DEGRADED')
    if failed:
        reasons.append('FAILED_PROVIDER_REQUESTS')
    if excluded:
        reasons.append('EXCLUDED_PROVIDER_OBSERVATIONS')
    if missing:
        reasons.append('MISSING_SCHEDULED_REQUESTS')
    if was_fallback:
        reasons.append('FALLBACK_SELECTED')
    return ResearchAssessment(2, provider, tuple(i for i, _ in ordered), value_status,
        quality, 'WARMING_UP' if warming else 'FULL', 'OBSERVATION_GAP' in arithmetic.diagnostics,
        tuple(dict.fromkeys(reasons)), arithmetic.value, tuple(sorted(occupied)),
        tuple(i for i in range(30) if i not in occupied), empty_pairs, exposed, partial, warming,
        wall_gap, mono_gap, walls[0] - wall_start if walls else None,
        report_wall_us - walls[-1] if walls else None, monos[0] - start if monos else None,
        report_ns - monos[-1] if monos else None, failed, excluded, missing, tuple(statuses),
        tuple(obs.receipt_mono_ns - (start + cell * 2_000_000_000)
            for _, obs in ordered if obs.receipt_mono_ns is not None
            for cell in (min(29, (obs.receipt_mono_ns - start) // 2_000_000_000),)),
        tuple((name, sum(obs.provider == name for obs in available.values()))
            for name in ('coinbase', 'kraken')), tuple(jitter), tuple(disagreements))


@dataclass(frozen=True)
class ResearchFetchResult:
    observations: tuple[ApproximateObservation, ...]
    report: ApproximateReferenceReport
    assessments: tuple[ResearchAssessment, ...]
    selected_assessment: ResearchAssessment
    context: AcquisitionContext
    completion_ns: int
    evidence: ResearchWindowEvidence
    window_observations: tuple[ApproximateObservation, ...]


class ApproximateReferenceSession:
    def __init__(self, *, providers, monotonic_ns, wall_us, sleep, project=None):
        self._providers = dict(providers)
        self._monotonic_ns = monotonic_ns
        self._wall_us = wall_us
        self._project = project
        self._history = {name: deque(maxlen=64) for name in ("coinbase", "kraken")}
        self._started = False
        self.sampling_origin_ns: int | None = None
        self._research_observations: tuple[ApproximateObservation, ...] = ()
        self._next_observation_id = 1

    def start(self) -> None:
        if self.sampling_origin_ns is None:
            self.sampling_origin_ns = self._monotonic_ns()
        self._started = True

    def stop(self):
        self._started = False

    def check_health(self):
        pass

    def fetch_research(self, *, context: AcquisitionContext, evidence: ResearchWindowEvidence,
            deadline: float | None = None,
            validate_evidence: Callable[[ResearchWindowEvidence, int], ResearchWindowEvidence] | None = None
            ) -> ResearchFetchResult:
        if (not self._started or self.sampling_origin_ns != context.sampling_origin_ns
                or evidence.sampling_origin_ns != self.sampling_origin_ns
                or evidence.run_id != context.run_id or context.revision != 2 or evidence.revision != 2):
            raise ValueError('Research session context mismatch or inactive session.')
        pending = [row for row in evidence.scheduled_opportunities if row[2] == 'submitted']
        if len(pending) != 1 or len(self._research_observations) != len(evidence.observation_ids):
            raise ValueError('Research fetch requires one submitted opportunity and aligned history.')
        opportunity_id = pending[0][0]
        if not all((opportunity_id, name) in evidence.expected_requests for name in ('coinbase', 'kraken')):
            raise ValueError('Research fetch requires explicit provider request membership.')
        observations = []
        for name in ('coinbase', 'kraken'):
            try:
                obs = self._providers[name].fetch_ticker(deadline=deadline, context=context)
            except Exception as exc:
                mono = self._monotonic_ns()
                obs = ApproximateObservation(name, 'BTC/USD', 'USD', None, None, None, None,
                    self._wall_us(), _captured_projection(context, mono), hashlib.sha256(b'').hexdigest(),
                    0, 'rest', name + '-ticker-v1', None, 'error',
                    'MISSING_PROVIDER' if isinstance(exc, KeyError) else 'PROVIDER_FAILURE', mono)
            observations.append(obs)
        mono, wall = self._monotonic_ns(), self._wall_us()
        first_id = max(self._next_observation_id, max(evidence.observation_ids, default=0) + 1)
        ids = (first_id, first_id + 1)
        self._next_observation_id = first_id + 2
        window = self._research_observations + tuple(observations)
        accumulated = replace(evidence,
            observation_ids=evidence.observation_ids + ids,
            contexts=evidence.contexts + (context, context),
            scheduled_opportunities=tuple((i, target, 'completed' if i == opportunity_id else disposition)
                for i, target, disposition in evidence.scheduled_opportunities),
            request_outcomes=evidence.request_outcomes + tuple(
                (opportunity_id, obs.provider, i, obs.receipt_mono_ns,
                    'success' if obs.result == 'success' and obs.error_code is None else 'error')
                for i, obs in zip(ids, observations)))
        if validate_evidence is not None:
            accumulated = validate_evidence(accumulated, mono)
        primary, secondary = tuple(assess_provider(window, provider=name,
            report_wall_us=wall, report_ns=mono, evidence=accumulated)
            for name in ('coinbase', 'kraken'))
        warming = primary.warmup_status == 'WARMING_UP'
        use_fallback = (primary.value_status != 'VALID' if warming
            else primary.value_status != 'VALID' or primary.coverage_quality == 'INSUFFICIENT')
        use_fallback = use_fallback and secondary.value_status == 'VALID' and (
            warming or secondary.coverage_quality != 'INSUFFICIENT')
        if use_fallback:
            secondary = assess_provider(window, provider='kraken', report_wall_us=wall,
                report_ns=mono, evidence=accumulated, was_fallback=True)
            secondary = replace(secondary, reasons=secondary.reasons + ('PRIMARY_INSUFFICIENT',))
        selected = secondary if use_fallback else primary
        contributions = set(selected.contributing_ids)
        report = compute_approximate_sample_mean_60s([
            replace(obs, source_timestamp_us=None, receipt_server_us=None)
            for i, obs in zip(accumulated.observation_ids, window) if i in contributions],
            receipt_server_us=None, receipt_wall_us=wall, receipt_mono_ns=mono)
        report = replace(report, source_provider=selected.selected_provider, was_fallback=use_fallback,
            receipt_server_us=_captured_projection(context, mono),
            diagnostics=report.diagnostics + (('FALLBACK_COINBASE_INSUFFICIENT',) if use_fallback else ()))
        self._research_observations = window
        return ResearchFetchResult(tuple(observations), report, (primary, secondary), selected,
            context, self._monotonic_ns(), accumulated, window)

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
