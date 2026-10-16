"""Dynamic discovery: validated slug fast path and independent list fallback."""
from datetime import datetime, timedelta
from .gamma import GammaClient
from .clob import ServerClock
from .models import MarketDataError, MarketDescriptor
from .parsing import parse_market

def _candidates(event: dict[str, object], now: datetime) -> list[MarketDescriptor]:
    if not isinstance(event, dict) or not isinstance(event.get("markets"), list):
        raise MarketDataError("invalid_metadata", "Gamma event must contain a markets array.")
    result = []
    for market in event["markets"]:
        if not isinstance(market, dict):
            raise MarketDataError("invalid_metadata", "Gamma market row must be an object.")
        parsed = parse_market(event, market, now=now)
        if parsed is not None:
            result.append(parsed)
    return result

def _identity(market: MarketDescriptor) -> tuple:
    return (market.market_id, market.condition_id, market.up.token_id, market.down.token_id,
            market.window_start, market.window_end)

def discover_current(gamma: GammaClient, *, clock: ServerClock, deadline: float,
                     use_fast_path: bool = True) -> tuple[MarketDescriptor, str]:
    captured = clock.now()
    if use_fast_path:
        slug = f"btc-updown-15m-{int(captured.timestamp())//900*900}"
        try:
            candidates = _candidates(gamma.event_by_slug(slug, deadline=deadline), clock.now())
            if len(candidates) == 1:
                return candidates[0], "fast_path"
        except MarketDataError as exc:
            if exc.http_status != 404 and exc.code not in ("invalid_metadata", "unsupported_identifier_shape"):
                raise
    events, markets, candidates = {}, {}, {}
    for offset in range(0, 500, 100):
        page = gamma.list_events(end_min=captured, end_max=captured+timedelta(minutes=30),
                                 offset=offset, deadline=deadline)
        if len(page) > 100:
            raise MarketDataError("invalid_metadata", "Gamma page exceeded requested limit.")
        for event in page:
            event_id = event.get("id")
            if not isinstance(event_id, str) or not event_id:
                raise MarketDataError("invalid_metadata", "Gamma event has invalid ID.")
            if event_id in events:
                if events[event_id] != event:
                    raise MarketDataError("invalid_metadata", "Conflicting repeated Gamma event.")
                continue
            events[event_id] = event
            for candidate in _candidates(event, clock.now()):
                if candidate.market_id in markets:
                    if _identity(markets[candidate.market_id]) != _identity(candidate):
                        raise MarketDataError("invalid_metadata", "Conflicting repeated Gamma market.")
                    continue
                markets[candidate.market_id] = candidate
                candidates[candidate.market_id] = (event, candidate)
        if len(page) < 100:
            break
    else:
        raise MarketDataError("discovery_incomplete", "Five full Gamma pages; enumeration was not proven complete.")
    if not candidates:
        raise MarketDataError("no_current_market", "No validated current BTC 15-minute market found.")
    if len(candidates) != 1:
        raise MarketDataError("ambiguous_market", "Multiple current BTC 15-minute markets; refusing to guess.")
    event, selected = next(iter(candidates.values()))
    refreshed = parse_market(event, gamma.market_by_id(selected.market_id, deadline=deadline), now=clock.now())
    if refreshed is None or _identity(refreshed) != _identity(selected):
        raise MarketDataError("invalid_metadata", "Selected Gamma market changed or became ineligible during refresh.")
    return refreshed, "filtered_list"


def discover_next(gamma: GammaClient, *, clock: ServerClock, deadline: float,
                  current: MarketDescriptor) -> tuple[MarketDescriptor, str]:
    from dataclasses import replace
    from .numeric import load_numeric_json
    captured = clock.now()
    events, rows, candidates = {}, {}, {}
    def identity(event, row, parsed):
        labels = load_numeric_json(row.get('outcomes'))
        if not isinstance(labels, list):
            raise MarketDataError('invalid_metadata', 'Successor outcomes unavailable.')
        return (parsed.event_id, *_identity(parsed), tuple(str(label).strip().lower() for label in labels))
    for offset in range(0, 500, 100):
        page = gamma.list_events(end_min=captured, end_max=captured+timedelta(minutes=30),
                                 offset=offset, deadline=deadline)
        if len(page) > 100:
            raise MarketDataError('invalid_metadata', 'Gamma page exceeded requested limit.')
        for event in page:
            if not isinstance(event, dict) or not isinstance(event.get('id'), str) or not event['id']:
                raise MarketDataError('invalid_metadata', 'Gamma event has invalid ID.')
            event_id = event['id']
            if event_id in events:
                if events[event_id] != event:
                    raise MarketDataError('invalid_metadata', 'Conflicting repeated Gamma event.')
                continue
            events[event_id] = event
            children = event.get('markets')
            if not isinstance(children, list):
                raise MarketDataError('invalid_metadata', 'Gamma event must contain markets.')
            for row in children:
                if not isinstance(row, dict):
                    raise MarketDataError('invalid_metadata', 'Gamma market must be an object.')
                parsed = parse_market(event, row, now=clock.now(), expected_start=current.window_end)
                if parsed is None:
                    continue
                key = parsed.market_id
                signature = identity(event, row, parsed)
                if key in rows and rows[key] != signature:
                    raise MarketDataError('identity_conflict', 'Conflicting repeated successor.')
                rows[key] = signature
                candidates[key] = (event, parsed, signature)
        if len(page) < 100:
            break
    else:
        raise MarketDataError('discovery_incomplete', 'Successor enumeration incomplete.')
    if len(candidates) != 1:
        raise MarketDataError('ambiguous_market' if candidates else 'no_next_market', 'No unique successor.')
    event, selected, signature = next(iter(candidates.values()))
    if (selected.market_id == current.market_id or selected.condition_id == current.condition_id
            or {selected.up.token_id, selected.down.token_id} & {current.up.token_id, current.down.token_id}):
        raise MarketDataError('identity_conflict', 'Successor shares predecessor identity.')
    detail = gamma.market_by_id(selected.market_id, deadline=deadline)
    if 'events' in detail and (not isinstance(detail['events'], list) or len(detail['events']) != 1
            or not isinstance(detail['events'][0], dict) or detail['events'][0].get('id') != selected.event_id):
        raise MarketDataError('identity_conflict', 'Refreshed event identity changed.')
    refreshed = parse_market(event, detail, now=clock.now(), expected_start=current.window_end)
    if refreshed is None or identity(event, detail, refreshed) != signature:
        raise MarketDataError('identity_conflict', 'Successor identity changed during refresh.')
    return replace(refreshed, metadata=dict(refreshed.metadata, prepared_event=event)), 'filtered_next_list'
