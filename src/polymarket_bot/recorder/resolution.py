"""Independent, public resolution evidence and payout classification."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal
import json

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.numeric import parse_decimal
from polymarket_bot.market_data.models import MarketDescriptor, TokenOutcome
from polymarket_bot.market_data.http import PublicGet
from .models import MarketRevision, ResolutionObservation
from .store import RecorderStore


def _reported_time(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > 64:
        raise ValueError("invalid reported time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("naive reported time")
        delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        result = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
        if not 0 <= result <= 2**63 - 1:
            raise ValueError("out of range reported time")
        return result
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid reported time") from exc


def _raw_payouts_json(value: object) -> str | None:
    """Keep a bounded source-shaped vector summary; full bytes remain evidence."""
    if type(value) is not list or len(value) > 32:
        return None
    items = []
    for item in value:
        if type(item) is int:
            if item.bit_length() > 426:
                return None
            part = str(item)
        elif isinstance(item, Decimal):
            if len(item.as_tuple().digits) > 128 or abs(item.as_tuple().exponent) > 128:
                return None
            part = str(item)
        elif type(item) in (str, bool) or item is None:
            if type(item) is str and len(item) > 256:
                return None
            part = json.dumps(item, ensure_ascii=True, separators=(",", ":"))
        else:
            return None
        items.append(part)
    summary = "[" + ",".join(items) + "]"
    return summary if len(summary) <= 512 else None


def parse_resolution(payload: object, market: MarketRevision, *, raw_response: bytes,
                     learned_wall_us: int, learned_server_us: int | None) -> ResolutionObservation:
    """Classify one condition row without quote, expiry, or reference inputs."""
    condition = market.market.condition_id
    provenance: dict = {"source": "data_api_v2_resolutions"}
    status = None
    payouts = None
    reported = None
    mapped = None
    classification = "ABSENT"
    raw_payouts_json = None
    if payload is not None:
        if not isinstance(payload, dict):
            classification, provenance["diagnostic"] = "INVALID", "invalid_row"
        else:
            raw_payouts_json = _raw_payouts_json(payload.get("payouts"))
            candidate_status = payload.get("status")
            status = candidate_status if type(candidate_status) is str and len(candidate_status) <= 32 else None
            if "status" in payload and status is None:
                provenance["diagnostic"] = "invalid_status"
            raw_time = payload.get("resolved_at")
            provenance["reported_time_raw"] = raw_time if type(raw_time) is str and len(raw_time) <= 64 else None
            if payload.get("condition_id") != condition:
                classification, provenance["diagnostic"] = "INVALID", "condition_mismatch"
            elif status is None:
                classification, provenance["diagnostic"] = "INVALID", "invalid_status"
            elif status != "resolved":
                classification = "PENDING"
            else:
                vector = payload.get("payouts")
                if type(vector) is not list or len(vector) != 2:
                    classification, provenance["diagnostic"] = "INVALID", "invalid_payout_vector"
                else:
                    try:
                        payouts = tuple(parse_decimal(value, field="resolution payout") for value in vector)
                        if any(value < 0 for value in payouts):
                            raise ValueError("negative payout")
                        reported = _reported_time(payload.get("resolved_at"))
                        positive = [index for index, value in enumerate(payouts) if value > 0]
                        if len(positive) == 1:
                            classification = "RESOLVED_DIRECTIONAL"
                            mapped = "UP" if positive[0] == market.up_index else "DOWN"
                        else:
                            classification = "RESOLVED_OTHER"
                    except (MarketDataError, ValueError):
                        classification, provenance["diagnostic"] = "INVALID", "invalid_payout_or_time"
                        payouts = None
                        reported = None
    return ResolutionObservation(market.market.market_id, condition, status, payouts,
                                 reported, learned_wall_us, learned_server_us,
                                 classification, mapped, raw_response, provenance,
                                 raw_payouts_json)


def _revision_from_row(row: dict) -> MarketRevision:
    """Rebuild only immutable persisted identity needed for payout mapping."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    start = epoch + timedelta(microseconds=row["window_start_us"])
    end = epoch + timedelta(microseconds=row["window_end_us"])
    descriptor = MarketDescriptor(row["event_id"], "", "", row["market_id"],
        row["slug"], row["question"], row["condition_id"], start, end,
        False, True, None, None, TokenOutcome("UP", row["up_token_id"]),
        TokenOutcome("DOWN", row["down_token_id"]), {}, ())
    return MarketRevision(descriptor, row["up_outcome_index"], row["down_outcome_index"],
                          "", "", b"", None, row["first_seen_us"], {})


def reconcile_due(public: PublicGet, store: RecorderStore, *, now_us: int,
                  deadline: float) -> int:
    """Poll at most twenty persisted due conditions through the fixed public route."""
    due = store.due_resolutions(now_us)
    if not due:
        return 0
    conditions = tuple(revision.market.condition_id for revision in due)
    attempts = []
    prior_callback = public.on_attempt
    def record(attempt):
        attempts.append(attempt)
        if prior_callback is not None:
            prior_callback(attempt)
    public.on_attempt = record
    payload = None
    raw = b""
    error_code = None
    try:
        result = public.get_json("data", "/v2/resolutions",
                                 params={"condition": ",".join(conditions)}, deadline=deadline)
        payload, raw = result.payload, result.raw_body
    except MarketDataError as exc:
        error_code = exc.code
    finally:
        public.on_attempt = prior_callback
    if not attempts:
        # No HTTP attempt means the deadline was already exhausted; retain due state.
        return 0
    rows = None
    if error_code is None:
        if type(payload) is dict and type(payload.get("data")) is list:
            rows = payload["data"]
        else:
            error_code = "invalid_resolution_envelope"
    learned_wall_us = attempts[-1].ended_wall_us
    unrequested = rows is not None and any(
        type(row) is not dict or row.get("condition_id") not in conditions for row in rows)
    observations = []
    for revision in due:
        condition = revision.market.condition_id
        if error_code is not None:
            observation = ResolutionObservation(revision.market.market_id, condition,
                None, None, None, learned_wall_us, None, "INVALID", None,
                raw or attempts[-1].raw_body,
                {"source": "data_api_v2_resolutions", "diagnostic": error_code})
        else:
            matched = [row for row in rows if type(row) is dict and row.get("condition_id") == condition]
            if len(matched) > 1:
                observation = ResolutionObservation(revision.market.market_id, condition,
                    None, None, None, learned_wall_us, None, "INVALID", None, raw,
                    {"source": "data_api_v2_resolutions", "diagnostic": "duplicate_condition",
                     "match_count": len(matched)})
            elif not matched and unrequested:
                observation = ResolutionObservation(revision.market.market_id, condition,
                    None, None, None, learned_wall_us, None, "INVALID", None, raw,
                    {"source": "data_api_v2_resolutions", "diagnostic": "unrequested_condition"})
            else:
                observation = parse_resolution(matched[0] if matched else None, revision,
                    raw_response=raw, learned_wall_us=learned_wall_us, learned_server_us=None)
                if unrequested:
                    observation = replace(observation, provenance=observation.provenance |
                                          {"batch_diagnostic": "unrequested_condition"})
        observations.append(observation)
    store.save_resolutions(tuple(observations), requests=tuple(attempts))
    return len(observations)
