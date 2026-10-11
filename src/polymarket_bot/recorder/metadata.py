"""Recorder bridge for strict Gamma discovery and original metadata evidence."""
from dataclasses import replace

from polymarket_bot.market_data.discovery import discover_current
from polymarket_bot.market_data.models import MarketDataError, MarketDescriptor
from polymarket_bot.market_data.numeric import decimal_text, load_numeric_json, parse_decimal
from polymarket_bot.market_data.parsing import parse_market
from .models import MarketRevision


def _selected_market(raw: bytes, market_id: str) -> dict:
    payload = load_numeric_json(raw)
    if isinstance(payload, dict) and isinstance(payload.get("markets"), list):
        matches = [item for item in payload["markets"]
                   if isinstance(item, dict) and item.get("id") == market_id]
        if len(matches) != 1:
            raise MarketDataError("invalid_metadata", "Selected Gamma event has no unique market row.")
        return matches[0]
    if isinstance(payload, dict) and payload.get("id") == market_id:
        return payload
    raise MarketDataError("invalid_metadata", "Selected Gamma raw response does not match discovery.")


def _index(market: dict, label: str) -> int:
    raw = market.get("outcomes")
    labels = load_numeric_json(raw) if isinstance(raw, str) else None
    if not isinstance(labels, list) or len(labels) != 2:
        raise MarketDataError("invalid_metadata", "Original Gamma outcomes are unavailable.")
    matches = [index for index, value in enumerate(labels)
               if isinstance(value, str) and value.strip().lower() == label]
    if len(matches) != 1:
        raise MarketDataError("invalid_metadata", "Original Gamma outcome mapping is ambiguous.")
    return matches[0]


def _validate_clob(config: object, market: MarketDescriptor) -> dict:
    if not isinstance(config, dict):
        raise MarketDataError("invalid_metadata", "CLOB market configuration must be an object.")
    for field in ("condition_id", "conditionId"):
        if field in config and config[field] != market.condition_id:
            raise MarketDataError("identity_conflict", "CLOB configuration condition differs from Gamma.")
    if "t" in config:
        rows = config["t"]
        if not isinstance(rows, list) or len(rows) != 2 or not all(isinstance(row, dict) for row in rows):
            raise MarketDataError("invalid_metadata", "CLOB configuration token mapping is malformed.")
        tokens = [row.get("t") for row in rows]
        if not all(isinstance(token, str) for token in tokens):
            raise MarketDataError("invalid_metadata", "CLOB configuration token identifier is malformed.")
        if sorted(tokens) != sorted((market.up.token_id, market.down.token_id)):
            raise MarketDataError("identity_conflict", "CLOB tokens differ from Gamma clobTokenIds.")
        for row in rows:
            label = row.get("o")
            if isinstance(label, str) and label.strip().lower() in ("up", "down"):
                expected = market.up.token_id if label.strip().lower() == "up" else market.down.token_id
                if row["t"] != expected:
                    raise MarketDataError("identity_conflict", "CLOB token outcome differs from Gamma.")
    return config


def _normalize_clob_numbers(config: dict) -> tuple[dict, tuple[str, ...]]:
    """Normalize named numeric configuration; unknown vendor fields stay raw data."""
    normalized = dict(config)
    warnings = []
    def number(mapping, key, field, *, nonnegative=False):
        if key not in mapping:
            return
        try:
            value = parse_decimal(mapping[key], field="CLOB "+field)
            if nonnegative and value < 0:
                raise MarketDataError("invalid_numeric", "Unsupported CLOB numeric configuration.")
            mapping[key] = decimal_text(value)
        except MarketDataError:
            del mapping[key]
            warnings.append("invalid_clob_numeric:"+field)
    for key in ("mts", "mos", "mbf", "tbf"):
        number(normalized, key, key, nonnegative=key in ("mts", "mos"))
    if "fd" in normalized:
        if isinstance(normalized["fd"], dict):
            normalized["fd"] = dict(normalized["fd"])
            number(normalized["fd"], "r", "fd.r")
        else:
            del normalized["fd"]
            warnings.append("invalid_clob_numeric:fd")
    return normalized, tuple(warnings)


def _revision(market: MarketDescriptor, source: str, raw: bytes, public, clock, deadline: float) -> MarketRevision:
    selected = _selected_market(raw, market.market_id)
    up_index, down_index = _index(selected, "up"), _index(selected, "down")
    config_read = public.get_json("clob", "/clob-markets/" + market.condition_id, deadline=deadline)
    config = _validate_clob(config_read.payload, market)
    config, numeric_warnings = _normalize_clob_numbers(config)
    return MarketRevision(market, up_index, down_index,
                          selected.get("description") if isinstance(selected.get("description"), str) else "",
                          selected.get("resolutionSource") if isinstance(selected.get("resolutionSource"), str) else "",
                          raw, config_read.raw_body,
                          clock.server_us_at(clock.monotonic_ns()), config, source,
                          market.warnings + numeric_warnings, market.pricing_ready and not numeric_warnings)


def acquire_market(gamma, public, *, clock, deadline: float, use_fast_path: bool) -> MarketRevision:
    if gamma.get is not public:
        raise ValueError("Gamma discovery and recorder evidence must share PublicGet.")
    attempts = []
    original_callback = public.on_attempt

    def observe(attempt):
        attempts.append(attempt)
        if original_callback is not None:
            original_callback(attempt)

    public.on_attempt = observe
    try:
        market, source = discover_current(gamma, clock=clock, deadline=deadline,
                                          use_fast_path=use_fast_path)
    finally:
        public.on_attempt = original_callback
    prefix = "/events/slug/" if source == "fast_path" else "/markets/" + market.market_id
    selected = [attempt for attempt in attempts if attempt.result == "success"
                and attempt.raw_complete and
                (attempt.path.startswith(prefix) if source == "fast_path" else attempt.path == prefix)]
    if not selected:
        raise MarketDataError("invalid_metadata", "Selected Gamma raw response was not observed.")
    return _revision(market, source, selected[-1].raw_body, public, clock, deadline)


def _identity(revision: MarketRevision) -> tuple:
    market = revision.market
    return (market.market_id, market.condition_id, market.window_start, market.window_end,
            market.up.token_id, market.down.token_id, revision.up_index, revision.down_index)


def refresh_revision(previous: MarketRevision, gamma, public, *, clock, deadline: float) -> MarketRevision:
    """Refresh one pinned market; failures retain its original observation age."""
    if gamma.get is not public:
        raise ValueError("Gamma refresh and recorder evidence must share PublicGet.")
    now_us = clock.server_us_at(clock.monotonic_ns())
    last_attempt_us = previous.last_refresh_attempt_us or previous.observed_us
    if now_us < last_attempt_us + 60_000_000:
        return previous
    attempted = replace(previous, last_refresh_attempt_us=now_us)
    path = "/markets/" + previous.market.market_id
    attempts = []
    original_callback = public.on_attempt

    def observe(attempt):
        attempts.append(attempt)
        if original_callback is not None:
            original_callback(attempt)

    public.on_attempt = observe
    try:
        gamma.market_by_id(previous.market.market_id, deadline=deadline)
    except MarketDataError as exc:
        return replace(attempted, warnings=previous.warnings +
                       (f"metadata_refresh_failed:{exc.code}; age_us={now_us-previous.observed_us}",))
    finally:
        public.on_attempt = original_callback
    selected = [attempt for attempt in attempts if attempt.path == path and
                attempt.result == "success" and attempt.raw_complete]
    if not selected:
        return replace(attempted, warnings=previous.warnings +
                       (f"metadata_refresh_failed:no_raw_response; age_us={now_us-previous.observed_us}",))
    raw = selected[-1].raw_body
    try:
        raw_object = load_numeric_json(raw)
        if isinstance(raw_object, dict) and raw_object.get("id") != previous.market.market_id:
            raise MarketDataError("identity_conflict", "Gamma market detail ID changed.")
        market_row = _selected_market(raw, previous.market.market_id)
        event = dict(id=previous.market.event_id, slug=previous.market.event_slug,
                     title=previous.market.event_title, seriesSlug="btc-up-or-down-15m",
                     active=True, closed=False)
        normalized = dict(market_row, active=True, closed=False, archived=False)
        parsed = parse_market(event, normalized, now=clock.now())
        if parsed is None:
            raise MarketDataError("identity_conflict", "Market window or family changed.")
        flags = {key: market_row.get(key) for key in ("active", "closed", "acceptingOrders", "enableOrderBook")}
        for key in ("active", "closed"):
            if type(flags[key]) is not bool:
                raise MarketDataError("invalid_metadata", "Market active/closed state is missing.")
        parsed = replace(parsed, active=flags["active"], closed=flags["closed"])
        up_index, down_index = _index(market_row, "up"), _index(market_row, "down")
        candidate = MarketRevision(parsed, up_index, down_index, "", "", raw, None,
                                   now_us, {}, previous.discovery_source)
        if _identity(candidate) != _identity(previous):
            raise MarketDataError("identity_conflict", "Pinned market identity changed.")
        public.on_attempt = observe
        try:
            revision = _revision(parsed, previous.discovery_source, raw, public, clock, deadline)
        finally:
            public.on_attempt = original_callback
        return replace(revision, capture_ready=(revision.capture_ready and parsed.active and not parsed.closed and parsed.pricing_ready
                                                and market_row.get("archived") is not True),
                       last_refresh_attempt_us=now_us)
    except MarketDataError as exc:
        if exc.code == "identity_conflict":
            config_path = "/clob-markets/" + previous.market.condition_id
            config_attempts = [attempt for attempt in attempts if attempt.path == config_path
                               and attempt.result == "success" and attempt.raw_complete]
            config_raw = (config_attempts[-1].raw_body if config_attempts
                          else previous.clob_config_raw)
            return replace(attempted, gamma_raw=raw, clob_config_raw=config_raw, capture_ready=False,
                           warnings=previous.warnings + ("identity_conflict",))
        return replace(attempted, warnings=previous.warnings +
                       (f"metadata_refresh_failed:{exc.code}; age_us={now_us-previous.observed_us}",))
