"""Dormant, credential-free reference evidence and research eligibility.

Future runtime activation requires exact-feed entitlement, a real subscription
decision, credentials with explicit use permission, successful authenticated
retrieval, verified decoding/scaling, and verified boundary selection. Synthetic
offline booleans do not authorize runtime activation. Future authentication must
verify Chainlink's actual timestamp units, freshness, clock source and skew
requirements independently of the CLOB window clock. This module implements
neither authentication nor binary decoding nor signature verification.
"""
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from hashlib import sha256
import re
from typing import Sequence

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.numeric import parse_decimal, parse_integer, exact_decimal_context
from .models import CHAINLINK_FEED_ID, ReferenceAssociationDraft, ReferenceReportDraft


@dataclass(frozen=True)
class ActivationEvidence:
    entitlement: bool
    price_decision: bool
    credential_use_authorized: bool
    authenticated_retrieval: bool
    decoder_verified: bool
    boundary_verified: bool


def require_reference_activation(mode: str, evidence: ActivationEvidence | None) -> None:
    if mode == "BOOK_ONLY":
        return
    if (mode != "REFERENCE_REQUIRED" or not isinstance(evidence, ActivationEvidence)
            or not all(getattr(evidence, field) is True for field in evidence.__dataclass_fields__)):
        raise MarketDataError("activation_required", "All six reference activation gates are required.")


def disabled_reference(market_id: str, target_us: int, *, learned_wall_us: int,
                       learned_server_us: int | None) -> ReferenceAssociationDraft:
    return ReferenceAssociationDraft(market_id, "SLOT_CURRENT", target_us,
        "DISABLED", "NOT_FETCHED", False, learned_wall_us, learned_server_us)


def normalize_reference_fields(raw_signed_report: bytes, fields: dict, *,
                               price_scale: int | None, decoder_revision: str | None,
                               received_wall_us: int,
                               received_server_us: int | None) -> ReferenceReportDraft:
    """Normalize explicitly supplied offline decoder output, never verify it.

    A scale is the number of decimal places in the supplied integer price.
    Neither a supplied scale/revision nor successful conversion verifies the
    exact feed's live decoder, signatures or boundary semantics.
    """
    if type(raw_signed_report) is not bytes or len(raw_signed_report) > 10_485_760:
        raise MarketDataError("invalid_reference", "Raw report exceeds the evidence budget.")
    received_wall_us = parse_integer(received_wall_us, field="receipt wall",
                                     minimum=-(2**63), maximum=2**63-1)
    if received_server_us is not None:
        received_server_us = parse_integer(received_server_us, field="receipt server",
                                           minimum=-(2**63), maximum=2**63-1)
    times = dict(valid_from_seconds=None, observations_seconds=None, expires_at_seconds=None)
    price, status, diagnostics = None, "UNVERIFIED", ()
    try:
        if (type(fields) is not dict or fields.get("feed_id") != CHAINLINK_FEED_ID
                or type(fields.get("report_schema_version")) is not int
                or fields["report_schema_version"] != 2):
            raise MarketDataError("invalid_reference_identity", "Unexpected feed/schema.")
        for name in times:
            times[name] = parse_integer(fields.get(name), field=name, minimum=0, maximum=253_402_300_799)
        if not times["valid_from_seconds"] <= times["observations_seconds"] <= times["expires_at_seconds"]:
            raise MarketDataError("invalid_reference_interval", "Invalid report time interval.")
        integer_price = fields.get("price_integer")
        parsed = parse_decimal(integer_price, field="reference price integer")
        if (type(integer_price) not in (int, str)
                or re.fullmatch(r"[+]?[0-9]+", str(integer_price)) is None or parsed <= 0):
            raise MarketDataError("invalid_numeric", "Reference price is not a positive integer.")
        if price_scale is not None:
            if type(price_scale) is not int or not 0 <= price_scale <= 128:
                raise MarketDataError("invalid_numeric", "Unsupported reference price scale.")
        if decoder_revision is not None and (type(decoder_revision) is not str or not 0 < len(decoder_revision) <= 128):
            raise MarketDataError("invalid_reference_decoder", "Unsupported decoder revision.")
        if price_scale is not None and decoder_revision is not None:
            with exact_decimal_context():
                price = parsed / (Decimal(10) ** price_scale)
            status = "DECODED"
    except (MarketDataError, DecimalException) as exc:
        price, status = None, "INVALID"
        diagnostics = (exc.code if isinstance(exc, MarketDataError) else "invalid_numeric",)
    return ReferenceReportDraft(CHAINLINK_FEED_ID, 2, raw_signed_report,
        sha256(raw_signed_report).hexdigest(), **times, decoded_price=price,
        decoder_revision=decoder_revision if type(decoder_revision) is str and len(decoder_revision) <= 128 else None,
        decode_status=status, verification_status="UNVERIFIED",
        first_received_wall_us=received_wall_us, first_received_server_us=received_server_us,
        diagnostics=diagnostics)


def eligible_as_of(association: ReferenceAssociationDraft, report: ReferenceReportDraft | None,
                   *, decision_us: int, role: str) -> bool:
    """Current/opening input eligibility, never closing-label eligibility.

    VERIFIED is externally supplied evidence, not produced by this normalizer.
    Decision times use the recorded server/window time domain. Missing calibrated
    receipt/learning times cannot silently fall back to local wall timestamps.
    """
    if (role not in ("SLOT_CURRENT", "OPENING") or association.role != role
            or association.status != "AVAILABLE" or report is None
            or report.feed_id != CHAINLINK_FEED_ID or report.report_schema_version != 2
            or report.decode_status != "DECODED"
            or not isinstance(report.decoded_price, Decimal) or not report.decoded_price.is_finite()
            or report.decoded_price <= 0 or report.decoder_revision is None
            or report.first_received_server_us is None or association.learned_server_us is None
            or report.first_received_server_us > decision_us or association.learned_server_us > decision_us
            or association.target_us > decision_us
            or report.observations_seconds is None
            or report.observations_seconds * 1_000_000 > decision_us
            or (association.report_hash is not None and association.report_hash != report.report_hash)):
        return False
    if role == "OPENING":
        return (association.origin in ("LIVE_LATEST", "HISTORICAL_LOOKUP")
                and bool(association.selection_policy_revision))
    return (association.origin == "LIVE_LATEST" and association.available_live is True
            and report.valid_from_seconds is not None and report.expires_at_seconds is not None
            and report.valid_from_seconds * 1_000_000 <= decision_us <= report.expires_at_seconds * 1_000_000
            and decision_us - report.observations_seconds * 1_000_000 <= 5_000_000)


def reference_status_projection(rows: Sequence[ReferenceAssociationDraft]) -> tuple[ReferenceAssociationDraft, ...]:
    """Latest learned status per association target, preserving all input rows."""
    latest = {}
    for row in rows:
        key = (row.market_id, row.role, row.slot_id, row.target_us)
        if key not in latest or row.learned_wall_us >= latest[key].learned_wall_us:
            latest[key] = row
    return tuple(latest.values())


def reference_due_work(rows: Sequence[ReferenceAssociationDraft], *, now_us: int,
                       limit: int) -> tuple[ReferenceAssociationDraft, ...]:
    """Return bounded dormant due work; no dispatch, retrieval or credentials."""
    if type(limit) is not int or limit < 0:
        raise ValueError("Reference due limit must be a nonnegative integer.")
    pending = (row for row in reference_status_projection(rows)
               if row.status in ("PENDING", "UNAVAILABLE", "AMBIGUOUS", "CONFLICT")
               and row.next_due_us is not None and row.next_due_us <= now_us)
    return tuple(sorted(pending, key=lambda row: (row.next_due_us, row.market_id, row.role,
                                                  row.target_us))[:limit])


def boundary_reference(market_id: str, role: str, *, window_start_us: int,
                       window_end_us: int, learned_wall_us: int,
                       learned_server_us: int | None) -> ReferenceAssociationDraft:
    """A future activation plan, not an instruction to run a lookup now."""
    if role not in ("OPENING", "CLOSING") or window_end_us - window_start_us != 900_000_000:
        raise ValueError("Boundary plan requires an exact 900-second market window.")
    target = window_start_us if role == "OPENING" else window_end_us
    return ReferenceAssociationDraft(market_id, role, target, "PENDING", "NOT_FETCHED",
        False, learned_wall_us, learned_server_us, next_due_us=target)


def boundary_retry_due(target_us: int, *, attempt_count: int, now_us: int) -> int:
    """After a failed attempt, choose the next future boundary retry time.

    The initial attempt is at the target. Retries one through four target
    +2/+5/+15/+30 seconds. Later retries wait 60, 120, ... seconds from now,
    capped at six hours. Passed target times are skipped, never burst replayed.
    """
    if type(attempt_count) is not int or attempt_count < 1:
        raise ValueError("Boundary retry requires a positive completed attempt count.")
    for offset in (2, 5, 15, 30)[min(attempt_count - 1, 4):]:
        due = target_us + offset * 1_000_000
        if due > now_us:
            return due
    delay_seconds = min(21_600, 60 * (2 ** min(max(attempt_count - 5, 0), 9)))
    return now_us + delay_seconds * 1_000_000


def select_boundary_report(reports, target_us):
    if type(target_us) is not int or target_us % 1_000_000:
        return None
    target = target_us // 1_000_000
    candidates = {r.report_hash: r for r in reports if r.feed_id == CHAINLINK_FEED_ID
        and r.report_schema_version == 2 and r.decode_status == 'DECODED'
        and r.valid_from_seconds is not None and r.observations_seconds is not None
        and r.valid_from_seconds <= target <= r.observations_seconds}
    if len(candidates) > 1:
        raise MarketDataError('BOUNDARY_AMBIGUOUS', 'Distinct signed reports contain boundary.')
    return next(iter(candidates.values()), None)


def decode_signed_report(raw: bytes) -> dict:
    def invalid():
        raise MarketDataError('invalid_reference_abi', 'Noncanonical or truncated signed report.')
    if type(raw) is not bytes or len(raw) < 224 or len(raw) > 10_485_760 or len(raw) % 32:
        invalid()
    def word(offset):
        if offset < 0 or offset + 32 > len(raw):
            invalid()
        return int.from_bytes(raw[offset:offset+32], 'big')
    cursor = 224
    segments = []
    for position, is_blob in ((96, True), (128, False), (160, False)):
        if word(position) != cursor:
            invalid()
        count = word(cursor)
        if (is_blob and count != 224) or (not is_blob and not 1 <= count <= 32):
            invalid()
        size = count if is_blob else count * 32
        end = cursor + 32 + size
        if end > len(raw):
            invalid()
        segments.append(raw[cursor+32:end])
        cursor = end
    if cursor != len(raw) or len(segments[1]) != len(segments[2]):
        invalid()
    blob = segments[0]
    values = [int.from_bytes(blob[i:i+32], 'big') for i in range(0, 224, 32)]
    for index, bits in ((1, 32), (2, 32), (3, 192), (4, 192), (5, 32)):
        if values[index] >= 1 << bits:
            invalid()
    price = int.from_bytes(blob[192:224], 'big', signed=True)
    if not -(1 << 191) <= price < (1 << 191):
        invalid()
    feed = '0x' + blob[:32].hex()
    if feed != CHAINLINK_FEED_ID:
        raise MarketDataError('invalid_reference_identity', 'Signed feed identity mismatch.')
    return dict(feed_id=feed, valid_from_seconds=values[1], observations_seconds=values[2],
        expires_at_seconds=values[5], price_integer=price)


def decode_schema_v2_report(payload: object, *, price_scale: int | None,
                            decoder_revision: str | None,
                            receipt_wall_us: int,
                            receipt_server_us: int | None) -> ReferenceReportDraft:
    """Decode a Chainlink Data Streams API report response envelope.

    Accepts both nested ``{"report": {...}}`` and flat ``{...}`` layouts.
    Extracts raw signed report bytes from the hex ``fullReport`` field,
    validates feed identity and timestamp interval, and applies exact Decimal
    scaling when both *price_scale* and *decoder_revision* are supplied.

    This function does NOT verify DON threshold ECDSA signatures.
    """
    receipt_wall_us = parse_integer(receipt_wall_us, field="receipt wall",
                                    minimum=-(2**63), maximum=2**63 - 1)
    if receipt_server_us is not None:
        receipt_server_us = parse_integer(receipt_server_us, field="receipt server",
                                         minimum=-(2**63), maximum=2**63 - 1)

    raw_signed_report = b""
    times = dict(valid_from_seconds=None, observations_seconds=None, expires_at_seconds=None)
    price, status, diagnostics = None, "UNVERIFIED", ()

    try:
        if not isinstance(payload, dict):
            raise MarketDataError("invalid_reference_payload", "Report payload must be a dict.")

        report = payload.get("report", payload)
        if not isinstance(report, dict):
            raise MarketDataError("invalid_reference_payload", "Inner report must be a dict.")

        full_report_hex = report.get("fullReport") or report.get("full_report")
        if not isinstance(full_report_hex, str) or not full_report_hex:
            raise MarketDataError("invalid_reference_payload", "Missing fullReport hex field.")

        full_report_hex = full_report_hex.strip()
        if full_report_hex.startswith("0x") or full_report_hex.startswith("0X"):
            full_report_hex = full_report_hex[2:]

        if (len(full_report_hex) > 20_971_520 or len(full_report_hex) % 2
                or re.fullmatch('[0-9a-fA-F]+', full_report_hex) is None):
            raise MarketDataError('invalid_reference_payload', 'Noncanonical or oversized report hex.')
        try:
            raw_signed_report = bytes.fromhex(full_report_hex)
        except ValueError as exc:
            raise MarketDataError("invalid_reference_payload", "fullReport is not valid hex.") from exc

        if len(raw_signed_report) > 10_485_760:
            raise MarketDataError("invalid_reference", "Raw report exceeds the evidence budget.")

        signed = decode_signed_report(raw_signed_report)
        for aliases, name in (
            (('feedId', 'feed_id'), 'feed_id'),
            (('validFromTimestamp', 'valid_from_timestamp', 'valid_from'), 'valid_from_seconds'),
            (('observationsTimestamp', 'observations_timestamp', 'observations'), 'observations_seconds'),
            (('expiresAt', 'expires_at', 'expires'), 'expires_at_seconds'),
            (('benchmarkPrice', 'benchmark_price', 'price'), 'price_integer'),
        ):
            for key in aliases:
                if key in report and str(report[key]).lower() != str(signed[name]).lower():
                    raise MarketDataError('invalid_reference_identity', 'Envelope conflicts with signed content.')
        times.update({name: signed[name] for name in times})

        if not times["valid_from_seconds"] <= times["observations_seconds"] <= times["expires_at_seconds"]:
            raise MarketDataError("invalid_reference_interval", "Invalid report time interval.")

        price_str = signed['price_integer']
        if price_str is None:
            raise MarketDataError("invalid_numeric", "Missing benchmark price in report.")

        parsed = parse_decimal(price_str, field="reference price integer")
        if (type(price_str) not in (int, str)
                or re.fullmatch(r"[+]?[0-9]+", str(price_str)) is None or parsed <= 0):
            raise MarketDataError("invalid_numeric", "Reference price is not a positive integer.")

        if price_scale is not None:
            if type(price_scale) is not int or not 0 <= price_scale <= 128:
                raise MarketDataError("invalid_numeric", "Unsupported reference price scale.")
        if decoder_revision is not None and (type(decoder_revision) is not str or not 0 < len(decoder_revision) <= 128):
            raise MarketDataError("invalid_reference_decoder", "Unsupported decoder revision.")

        if price_scale is not None and decoder_revision is not None:
            with exact_decimal_context():
                price = parsed / (Decimal(10) ** price_scale)
            status = "DECODED"

    except (MarketDataError, DecimalException) as exc:
        price, status = None, "INVALID"
        diagnostics = (exc.code if isinstance(exc, MarketDataError) else "invalid_numeric",)

    if raw_signed_report:
        report_hash = sha256(raw_signed_report).hexdigest()
    else:
        import json as _json
        try:
            fallback_bytes = _json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        except Exception:
            fallback_bytes = b""
        report_hash = sha256(fallback_bytes).hexdigest()

    return ReferenceReportDraft(
        CHAINLINK_FEED_ID, 2, raw_signed_report, report_hash,
        **times,
        decoded_price=price,
        decoder_revision=decoder_revision if type(decoder_revision) is str and len(decoder_revision) <= 128 else None,
        decode_status=status,
        verification_status="UNVERIFIED",
        first_received_wall_us=receipt_wall_us,
        first_received_server_us=receipt_server_us,
        diagnostics=diagnostics,
    )


class ReferenceRestEvidence:
    def __init__(self, client, *, run_id, price_scale, decoder_revision, project, anchor_id):
        self.client, self.run_id = client, run_id
        self.price_scale, self.decoder_revision = price_scale, decoder_revision
        self.project, self.anchor_id = project, anchor_id

    def fetch_boundary(self, row, deadline):
        return self.fetch(row, deadline)

    def fetch(self, row, deadline):
        from dataclasses import replace
        from .models import ReferenceFetchDraft
        attempts, reports, error = [], (), None
        previous = self.client.on_attempt
        self.client.on_attempt = attempts.append
        latest = row.role == 'SLOT_CURRENT'
        try:
            if latest:
                raw, fields = self.client.get_latest_report(CHAINLINK_FEED_ID, deadline)
            else:
                if row.target_us % 1_000_000:
                    raise MarketDataError('boundary_timestamp_unsupported', 'Fractional boundary cannot be rounded.')
                raw, fields = self.client.get_report_at_timestamp(CHAINLINK_FEED_ID, row.target_us//1_000_000, deadline)
            attempt = attempts[-1]
            report = decode_schema_v2_report(fields, price_scale=self.price_scale,
                decoder_revision=self.decoder_revision, receipt_wall_us=attempt.ended_wall_us,
                receipt_server_us=self.project(attempt.ended_mono_ns))
            if report.raw_signed_report == raw:
                reports = (report,)
        except MarketDataError as exc:
            error = exc.code
            if not attempts:
                raise
        finally:
            self.client.on_attempt = previous
        attempt = attempts[-1]
        if reports and attempt.result != 'success':
            reports = ()
        fetch = ReferenceFetchDraft(attempt, self.run_id, CHAINLINK_FEED_ID, row.role,
            'LIVE_LATEST' if latest else 'HISTORICAL_LOOKUP', row.target_us,
            'UNAVAILABLE', error, attempt.raw_body if attempt.raw_complete else None,
            self.project(attempt.ended_mono_ns), row.market_id, self.anchor_id)
        return fetch, reports


class OfflineReferenceSession:
    def __init__(self, *, price_scale, decoder_revision, discovery_payload, evidence,
                 latest, monotonic_ns, wall_us, boundary=None, live=None,
                 selection_policy=None, policy_revision=None):
        from .chainlink_discovery import verify_feed_identity
        require_reference_activation('REFERENCE_REQUIRED', evidence)
        metadata = verify_feed_identity(discovery_payload)
        if metadata.price_scale != price_scale or metadata.name != 'BTC/USD-Streams-TWAP-60s-mainnet-production':
            raise MarketDataError('identity_conflict', 'Offline feed metadata conflicts.')
        if type(decoder_revision) is not str or not 0 < len(decoder_revision) <= 128:
            raise MarketDataError('decoder_verification_failed', 'Structural decoder revision required.')
        self.price_scale, self.decoder_revision = price_scale, decoder_revision
        self.metadata, self.evidence = metadata, evidence
        self.latest, self.boundary, self.live = latest, boundary, live
        self.monotonic_ns, self.wall_us = monotonic_ns, wall_us
        self.selection_policy, self.policy_revision = selection_policy, policy_revision

    def fetch(self, row, deadline, *, run_id, anchor_id, project, buffer_only=False, payload_override=None):
        import json
        from dataclasses import replace
        from polymarket_bot.market_data.http import HttpAttempt
        from .models import ReferenceFetchDraft
        started_ns, started_wall = self.monotonic_ns(), self.wall_us()
        raw, reports, error, http_status, complete = b'', (), None, None, False
        try:
            if started_ns / 1_000_000_000 >= deadline:
                raise MarketDataError('chainlink_timeout', 'Reference budget expired.')
            if row.role == 'SLOT_CURRENT':
                payload = payload_override if payload_override is not None else (self.live(5, project(started_ns)/1_000_000) if self.live else None)
                if payload is None and not buffer_only:
                    payload = self.latest(deadline)
                elif type(payload) is tuple and len(payload) == 2:
                    signed, fields = payload
                    if type(signed) is not bytes or type(fields) is not dict:
                        raise MarketDataError('invalid_reference_payload', 'Invalid live buffer evidence.')
                    payload = dict(fields)
                    payload['fullReport'] = signed.hex()
                    for key in ('raw_message', 'receipt_monotonic', 'receipt_wall_us', 'receipt_server_us'):
                        payload.pop(key, None)
            else:
                payload = self.boundary(row.target_us, deadline) if self.boundary else None
            if payload is not None:
                raw = json.dumps(payload, separators=(',', ':'), allow_nan=False).encode()
                if len(raw) > 10_485_760:
                    raw = b''
                    raise MarketDataError('response_too_large', 'Reference envelope exceeds budget.')
                http_status, complete = 200, True
                ended_ns, ended_wall = self.monotonic_ns(), self.wall_us()
                report = decode_schema_v2_report(payload, price_scale=self.price_scale,
                    decoder_revision=self.decoder_revision, receipt_wall_us=ended_wall,
                    receipt_server_us=project(ended_ns))
                if report.raw_signed_report:
                    reports = (report,)
                if self.monotonic_ns()/1_000_000_000 >= deadline:
                    error = 'LATE_REFERENCE'
            else:
                error = 'REFERENCE_PENDING'
        except MarketDataError as exc:
            error = exc.code
        except (ConnectionError, TimeoutError, OSError):
            error = 'reference_transport_failed'
        except (ValueError, TypeError, UnicodeError, RecursionError):
            error = 'invalid_reference_payload'
        ended_ns, ended_wall = self.monotonic_ns(), self.wall_us()
        if reports:
            reports = tuple(replace(r, first_received_wall_us=ended_wall,
                first_received_server_us=project(ended_ns)) for r in reports)
        latest = row.role == 'SLOT_CURRENT'
        attempt = HttpAttempt('REFERENCE_LATEST' if latest else 'REFERENCE_LOOKUP', 'GET',
            '/api/v1/reports/latest' if latest else '/api/v1/reports',
            ('feed_id=' + CHAINLINK_FEED_ID,), 1, started_ns, ended_ns,
            started_wall, ended_wall, http_status, 'success' if reports else 'error',
            error, raw, complete)
        fetch = ReferenceFetchDraft(attempt, run_id, CHAINLINK_FEED_ID, row.role,
            'LIVE_LATEST' if latest else 'HISTORICAL_LOOKUP', row.target_us,
            'UNAVAILABLE', error, raw if complete else None, project(ended_ns),
            row.market_id, anchor_id)
        return row, fetch, reports, deadline


class ManagedReferenceSession(OfflineReferenceSession):
    def __init__(self, *, evidence, discovery, credentials, monotonic_ns, wall_us,
                 decoder_revision, rest_factory=None, live_factory=None):
        self.evidence, self.discovery, self.credentials = evidence, discovery, credentials
        self.monotonic_ns, self.wall_us = monotonic_ns, wall_us
        self.decoder_revision = decoder_revision
        self.rest_factory, self.live_factory = rest_factory, live_factory
        self.rest = self.consumer = None
        self.live = None
        self.selection_policy, self.policy_revision = select_boundary_report, 'signed-inclusive-seconds-v1'
        self.started = False

    def start(self, mode, deadline, *, persisted_pin=None):
        if mode == 'BOOK_ONLY':
            return
        require_reference_activation(mode, self.evidence)
        if self.started:
            return
        from .chainlink_discovery import verify_feed_identity
        from .chainlink_auth import validate_credentials
        from .chainlink_rest import ChainlinkRestClient
        from .chainlink_ws import ChainlinkLiveConsumer
        self.metadata = verify_feed_identity(self.discovery(deadline))
        if self.metadata.name != 'BTC/USD-Streams-TWAP-60s-mainnet-production':
            raise MarketDataError('identity_conflict', 'Discovery feed name conflicts.')
        self.price_scale = self.metadata.price_scale
        if persisted_pin is not None and persisted_pin['price_scale'] is not None and (
                persisted_pin['price_scale'] != self.price_scale or persisted_pin['decoder_revision'] != self.decoder_revision):
            raise MarketDataError('identity_conflict', 'Discovery conflicts with persisted reference pin.')
        if type(self.decoder_revision) is not str or not 0 < len(self.decoder_revision) <= 128:
            raise MarketDataError('decoder_verification_failed', 'Decoder revision required.')
        if self.monotonic_ns()/1_000_000_000 >= deadline:
            raise MarketDataError('chainlink_timeout', 'Startup budget expired.')
        require_reference_activation(mode, self.evidence)
        identity, secret = validate_credentials(*self.credentials())
        try:
            self.rest = (self.rest_factory or (lambda i, s: ChainlinkRestClient(
                ChainlinkRestClient._BASE, i, s)))(identity, secret)
            self.consumer = (self.live_factory or (lambda i, s: ChainlinkLiveConsumer(
                'wss://ws.dataengine.chain.link', CHAINLINK_FEED_ID, i, s)))(identity, secret)
            self.consumer.start()
            self.live = self.get_live_report
            self.gaps = self.consumer.get_connection_gaps
            self.started = True
        except BaseException:
            self.stop()
            raise

    def get_live_report(self, max_age_s, now_server_s):
        health = getattr(self.consumer, 'check_health', None)
        if health is not None:
            health()
        return self.consumer.get_freshest_report(max_age_s, now_server_s)

    def stop(self):
        error = None
        if self.consumer is not None:
            try:
                self.consumer.stop()
            except Exception as exc:
                error = exc
            else:
                self.consumer = None
        if self.rest is not None:
            try:
                self.rest.close()
            except Exception as exc:
                error = error or exc
            else:
                self.rest = None
        self.started = False
        self.live = None
        if error is not None:
            raise MarketDataError('reference_shutdown_failed', 'Reference resource cleanup failed.') from error

    def fetch(self, row, deadline, *, run_id, anchor_id, project, buffer_only=False, payload_override=None):
        if buffer_only or payload_override is not None:
            return super().fetch(row, deadline, run_id=run_id, anchor_id=anchor_id,
                project=project, buffer_only=buffer_only, payload_override=payload_override)
        if not self.started or self.rest is None:
            raise MarketDataError('activation_required', 'Managed reference session not started.')
        reader = ReferenceRestEvidence(self.rest, run_id=run_id, price_scale=self.price_scale,
            decoder_revision=self.decoder_revision, project=project, anchor_id=anchor_id)
        fetch, reports = reader.fetch(row, deadline)
        return row, fetch, reports, deadline


def due_reference_boundaries(store, now_us, limit=2):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Boundary batch limit must be 1..20.')
    rows = store._rows("""SELECT a.* FROM reference_associations a
        WHERE a.role IN ('OPENING','CLOSING') AND a.association_id=(
            SELECT MAX(b.association_id) FROM reference_associations b
            WHERE b.market_id=a.market_id AND b.role=a.role)
        AND a.status IN ('PENDING','UNAVAILABLE','AMBIGUOUS','CONFLICT')
        AND a.next_due_us<=? ORDER BY a.next_due_us,a.market_id,a.role LIMIT ?""", (now_us, limit))
    return tuple(ReferenceAssociationDraft(**{name: row[name] for name in
        ReferenceAssociationDraft.__dataclass_fields__ if name != 'report_hash'}) for row in rows)


def reconcile_due_reference_boundaries(store, rest_client, now_server_us, deadline, *,
                                       monotonic=None, selection_policy=None,
                                       policy_revision=None, limit=2):
    import time
    from dataclasses import replace
    monotonic = monotonic or time.monotonic
    if monotonic() >= deadline:
        return 0
    completed = 0
    for row in due_reference_boundaries(store, now_server_us, limit):
        if monotonic() >= deadline:
            break
        fetch, reports = rest_client.fetch_boundary(row, deadline)
        selected, status, reason = None, 'PENDING', 'BOUNDARY_PENDING'
        try:
            selected = select_boundary_report(reports, row.target_us)
        except MarketDataError as exc:
            if exc.code != 'BOUNDARY_AMBIGUOUS':
                raise
            status, reason = 'AMBIGUOUS', exc.code
        if selected is not None:
            status, reason = 'AVAILABLE', None
        if monotonic() >= deadline:
            status, reason = 'UNAVAILABLE', 'LATE_REFERENCE'
        learned = fetch.received_server_us
        association = replace(row, status=status, origin='HISTORICAL_LOOKUP', available_live=False,
            learned_wall_us=fetch.attempt.ended_wall_us, learned_server_us=learned,
            fetch_id=None, report_id=None, report_hash=selected.report_hash if selected else None,
            selection_policy_revision='signed-inclusive-seconds-v1', reason=reason, attempt_count=row.attempt_count + 1,
            next_due_us=None if status == 'AVAILABLE' else boundary_retry_due(row.target_us,
                attempt_count=row.attempt_count + 1, now_us=max(now_server_us, learned or now_server_us)))
        store.save_reference_evidence(fetch, reports, (association,))
        completed += 1
    return completed


def reference_page_plan(*, reports_per_call: int = 100, calls: int = 2) -> tuple[int, ...]:
    """Bounded inert page sizes for later short-gap repair, with no requests."""
    if (type(reports_per_call) is not int or not 1 <= reports_per_call <= 100
            or type(calls) is not int or not 1 <= calls <= 2):
        raise ValueError("Short-gap repair permits at most 100 reports/call and two calls.")
    return (reports_per_call,) * calls
