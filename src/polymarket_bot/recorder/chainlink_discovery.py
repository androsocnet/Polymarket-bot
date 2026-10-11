"""Chainlink Public Discovery Client & Dynamic Feed Verification."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse
import httpx

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.numeric import load_numeric_json, parse_integer
from polymarket_bot.recorder.models import CHAINLINK_FEED_ID

CHAINLINK_DISCOVERY_HOST = "api.dataengine.chain.link"
CHAINLINK_DISCOVERY_PATH = "/api/v1/feeds"
CHAINLINK_DISCOVERY_URL = "https://api.dataengine.chain.link/api/v1/feeds"


@dataclass(frozen=True)
class VerifiedFeedMetadata:
    feed_id: str
    name: str
    report_schema_version: int
    price_scale: int
    status: str
    catalog_metadata: dict


def verify_feed_identity(discovery_payload: dict | list, expected_feed_id: str = CHAINLINK_FEED_ID) -> VerifiedFeedMetadata:
    if isinstance(discovery_payload, dict):
        if "feeds" in discovery_payload and isinstance(discovery_payload["feeds"], list):
            feeds = discovery_payload["feeds"]
        elif "data" in discovery_payload and isinstance(discovery_payload["data"], list):
            feeds = discovery_payload["data"]
        else:
            feeds = [discovery_payload]
    elif isinstance(discovery_payload, list):
        feeds = discovery_payload
    else:
        raise MarketDataError("invalid_discovery_payload", "Discovery payload must be an object or list.")

    expected_clean = expected_feed_id.strip().lower()
    matches = []
    for item in feeds:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("feedId") or item.get("feed_id") or item.get("id") or "").strip().lower()
        if item_id == expected_clean:
            matches.append(item)

    if not matches:
        raise MarketDataError("feed_not_found", f"Expected feed {expected_feed_id} not found in Discovery.")
    if len(matches) > 1:
        raise MarketDataError("ambiguous_feed", f"Multiple entries found for feed {expected_feed_id}.")

    feed = matches[0]

    raw_status = str(feed.get("status") or "").strip().lower()
    if raw_status != "active":
        if raw_status in ("deprecated", "retired", "disabled"):
            raise MarketDataError("feed_deprecated", f"Feed status is '{raw_status}', 'active' required.")
        raise MarketDataError("feed_inactive", f"Feed status is '{raw_status or 'unknown'}', 'active' required.")

    schema_version_raw = (
        feed.get("schemaVersion")
        if feed.get("schemaVersion") is not None
        else feed.get("report_schema_version")
        if feed.get("report_schema_version") is not None
        else feed.get("reportSchemaVersion")
        if feed.get("reportSchemaVersion") is not None
        else feed.get("schema_version")
    )
    try:
        schema_version = parse_integer(schema_version_raw, field="schema version", minimum=1, maximum=100)
    except MarketDataError as exc:
        raise MarketDataError("schema_mismatch", "Schema version in Discovery is missing or invalid.") from exc

    if schema_version != 2:
        raise MarketDataError("schema_mismatch", f"Feed report schema version is {schema_version}, expected 2.")

    decimals_raw = (
        feed.get("decimals")
        if feed.get("decimals") is not None
        else feed.get("price_scale")
        if feed.get("price_scale") is not None
        else feed.get("priceScale")
    )
    try:
        decimals = parse_integer(decimals_raw, field="decimals", minimum=0, maximum=128)
    except MarketDataError as exc:
        raise MarketDataError("invalid_decimals", "Price scale/decimals missing or invalid in Discovery.") from exc

    name = str(feed.get("name") or feed.get("feed_name") or feed.get("feedName") or "").strip()
    if not name:
        raise MarketDataError("invalid_feed_name", "Feed name missing in Discovery.")

    return VerifiedFeedMetadata(
        feed_id=expected_feed_id,
        name=name,
        report_schema_version=schema_version,
        price_scale=decimals,
        status=raw_status,
        catalog_metadata=feed,
    )


def fetch_feed_discovery(client: httpx.Client, endpoint_url: str = CHAINLINK_DISCOVERY_URL, *, deadline: float | None = None) -> dict:
    import math
    import time
    from contextlib import closing
    if endpoint_url != CHAINLINK_DISCOVERY_URL:
        raise MarketDataError('invalid_discovery_host', 'Only fixed HTTPS Discovery route is permitted.')
    deadline = time.monotonic()+2 if deadline is None else deadline
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise MarketDataError('discovery_timeout', 'Invalid Discovery deadline.')
    def remaining():
        value = deadline-time.monotonic()
        if value <= 0:
            raise MarketDataError('discovery_timeout', 'Discovery deadline expired.')
        return min(value, 2)
    request = httpx.Request('GET', CHAINLINK_DISCOVERY_URL, headers={'Accept-Encoding': 'identity'},
        extensions={'timeout': {p: remaining() for p in ('connect','read','write','pool')}})
    try:
        with closing(client.send(request, stream=True, follow_redirects=False)) as response:
            remaining()
            if response.status_code != 200:
                raise MarketDataError('discovery_http_error', 'Discovery request rejected.', http_status=response.status_code)
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise MarketDataError('invalid_discovery_payload', 'Compressed Discovery response unsupported.')
            body = bytearray()
            chunks = iter((response.content,)) if response.is_stream_consumed else response.iter_raw()
            while True:
                request.extensions['timeout']['read'] = remaining()
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                remaining()
                if len(body)+len(chunk) > 10_485_760:
                    raise MarketDataError('response_too_large', 'Discovery response exceeds budget.')
                body.extend(chunk)
            remaining()
            payload = load_numeric_json(bytes(body))
            remaining()
            return payload
    except httpx.HTTPError:
        raise MarketDataError('discovery_http_error', 'Discovery transport failed.') from None
    finally:
        client.cookies.clear()
