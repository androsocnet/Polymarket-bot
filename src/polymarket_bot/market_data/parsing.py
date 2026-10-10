"""Strict normalization of Gamma's current BTC market metadata."""
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from .models import MarketDataError, MarketDescriptor, TokenOutcome

def _invalid(message: str) -> MarketDataError:
    return MarketDataError("invalid_metadata", message)

def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"Missing or invalid {field}.")
    return value

def _time(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value, field).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Timezone required")
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError) as exc:
        raise _invalid(f"Invalid aware UTC timestamp: {field}.") from exc

def _array(value: object, field: str, code: str = "invalid_metadata") -> list:
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
        if not isinstance(parsed, list) or len(parsed) != 2:
            raise ValueError("Expected two-entry JSON array")
        return parsed
    except (ValueError, TypeError, RecursionError) as exc:
        raise MarketDataError(code, f"Invalid {field}: expected a two-entry JSON-encoded array.") from exc

def is_btc_family(event: dict[str, object], market: dict[str, object]) -> bool:
    series = event.get("series")
    series = series if isinstance(series, list) else []
    slugs = [event.get("seriesSlug")] + [s.get("slug") for s in series if isinstance(s, dict)]
    texts = [event.get("title"), market.get("question")] + [s.get("title") for s in series if isinstance(s, dict)]
    texts = [t for t in texts if isinstance(t, str)]
    if any(re.search(r"\b(ethereum|eth|solana|sol|xrp|dogecoin|doge|bch|bsv)\b|\bbitcoin\s+(cash|sv|gold)\b", t, re.I) for t in texts):
        return False
    for slug in slugs:
        if not isinstance(slug, str):
            continue
        family = re.match(r"^([a-z0-9]+)-up-or-down(?:-|$)", slug, re.I)
        if (family and family[1].lower() != "btc") or re.match(r"^(eth|sol|xrp|doge|bch|bsv)-", slug, re.I):
            return False
    return ("btc-up-or-down-15m" in slugs or
            (any(re.search(r"\b(bitcoin|btc)\b", t, re.I) for t in texts) and
             isinstance(market.get("question"), str) and
             bool(re.search(r"\bup\s+or\s+down\b", market["question"], re.I))))

def parse_market(event: dict[str, object], market: dict[str, object], *, now: datetime) -> MarketDescriptor | None:
    if not is_btc_family(event, market):
        return None
    for row in (event, market):
        if row.get("archived") is True or row.get("active") is False or row.get("closed") is True:
            return None
        if type(row.get("active")) is not bool or type(row.get("closed")) is not bool:
            raise _invalid("Event/market active and closed flags must be booleans.")
    start, end = _time(market.get("eventStartTime"), "eventStartTime"), _time(market.get("endDate"), "endDate")
    if (end-start).total_seconds() != 900 or not start <= now < end:
        return None
    for row in (event, market):
        slug = row.get("slug", "")
        match = re.fullmatch(r"btc-updown-15m-(\d+)", slug) if isinstance(slug, str) else None
        if match and (start.microsecond != 0 or int(match[1]) != int(start.timestamp())):
            return None
    event_id = _text(event.get("id"), "event id")
    market_id = _text(market.get("id"), "market id")
    condition = _text(market.get("conditionId"), "conditionId")
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition):
        raise _invalid("Invalid conditionId.")
    labels = _array(market.get("outcomes"), "outcomes")
    if not all(isinstance(label, str) for label in labels):
        raise _invalid("Invalid outcome labels.")
    labels = [label.strip().lower() for label in labels]
    if sorted(labels) != ["down", "up"]:
        raise _invalid("Expected exactly one Up and one Down.")
    code = "unsupported_identifier_shape"
    tokens = _array(market.get("clobTokenIds"), "clobTokenIds", code)
    if not all(isinstance(token, str) and re.fullmatch(r"[0-9]+", token) for token in tokens) or tokens[0] == tokens[1]:
        raise MarketDataError(code, "Expected two distinct decimal-string clobTokenIds; positionIds are metadata only.")
    warnings, prices = [], [None, None]
    try:
        raw_prices = _array(market.get("outcomePrices"), "outcomePrices")
        prices = [Decimal(p) if isinstance(p, (str, int, Decimal)) and not isinstance(p, bool) else Decimal("NaN") for p in raw_prices]
        if any(not p.is_finite() or not 0 <= p <= 1 for p in prices):
            raise ValueError("Invalid metadata prices")
    except (MarketDataError, InvalidOperation, ValueError):
        prices = [None, None]
        warnings.append("Gamma outcomePrices unavailable or malformed; book pricing is unaffected.")
    mapped = {label: TokenOutcome(label.title(), token, price) for label, token, price in zip(labels, tokens, prices)}
    metadata = {k: market[k] for k in ("version", "positionIds", "outcomePrices", "startDate") if k in market}
    def flag(name):
        value = market.get(name)
        return value if type(value) is bool else None
    return MarketDescriptor(event_id, _text(event.get("slug"), "event slug"), _text(event.get("title"), "event title"),
                            market_id, _text(market.get("slug"), "market slug"), _text(market.get("question"), "question"),
                            condition, start, end, True, False, flag("acceptingOrders"), flag("enableOrderBook"),
                            mapped["up"], mapped["down"], metadata, tuple(warnings))
