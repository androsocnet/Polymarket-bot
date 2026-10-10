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
            or report.decode_status != "DECODED" or report.verification_status != "VERIFIED"
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


def reference_page_plan(*, reports_per_call: int = 100, calls: int = 2) -> tuple[int, ...]:
    """Bounded inert page sizes for later short-gap repair, with no requests."""
    if (type(reports_per_call) is not int or not 1 <= reports_per_call <= 100
            or type(calls) is not int or not 1 <= calls <= 2):
        raise ValueError("Short-gap repair permits at most 100 reports/call and two calls.")
    return (reports_per_call,) * calls
