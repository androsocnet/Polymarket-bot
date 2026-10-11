"""Chainlink HMAC Authentication & Request Signer."""
from __future__ import annotations

import hashlib
import hmac

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.numeric import parse_integer


def validate_credentials(client_id: str | None, client_secret: str | None) -> tuple[str, str]:
    if not isinstance(client_id, str) or not client_id.strip():
        raise MarketDataError("invalid_credentials", "Client ID is missing or not a non-empty string.")
    if not isinstance(client_secret, str) or not client_secret.strip():
        raise MarketDataError("invalid_credentials", "Client secret is missing or not a non-empty string.")

    clean_cid = client_id.strip()
    clean_sec = client_secret.strip()

    if len(clean_cid) > 256:
        raise MarketDataError("invalid_credentials", "Client ID exceeds maximum allowed length (256).")
    if len(clean_sec) > 256:
        raise MarketDataError("invalid_credentials", "Client secret exceeds maximum allowed length (256).")

    return clean_cid, clean_sec


def compute_chainlink_signature(
    client_id: str,
    client_secret: str,
    method: str,
    path: str,
    body: bytes | None,
    timestamp_ms: int,
) -> str:
    clean_cid, clean_sec = validate_credentials(client_id, client_secret)

    if not isinstance(method, str) or not method.strip():
        raise MarketDataError("invalid_method", "HTTP method must be a non-empty string.")
    clean_method = method.strip().upper()

    if not isinstance(path, str) or not path.startswith("/"):
        raise MarketDataError("invalid_path", "Path must start with '/'.")
    clean_path = path.strip()

    valid_ts = parse_integer(timestamp_ms, field="timestamp_ms", minimum=1, maximum=253_402_300_799_000)

    raw_body = body if body is not None else b""
    if not isinstance(raw_body, bytes):
        raise MarketDataError("invalid_body", "Body must be bytes or None.")

    body_hash = hashlib.sha256(raw_body).hexdigest()
    string_to_sign = f"{clean_method} {clean_path} {body_hash} {clean_cid} {valid_ts}"

    signature = hmac.new(
        clean_sec.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return signature


def verify_chainlink_signature(
    signature: str,
    client_id: str,
    client_secret: str,
    method: str,
    path: str,
    body: bytes | None,
    timestamp_ms: int,
) -> bool:
    try:
        expected = compute_chainlink_signature(
            client_id=client_id,
            client_secret=client_secret,
            method=method,
            path=path,
            body=body,
            timestamp_ms=timestamp_ms,
        )
        return hmac.compare_digest(signature, expected)
    except MarketDataError:
        return False


def build_chainlink_auth_headers(
    client_id: str,
    client_secret: str,
    method: str,
    path: str,
    body: bytes | None,
    timestamp_ms: int,
) -> dict[str, str]:
    clean_cid, clean_sec = validate_credentials(client_id, client_secret)
    valid_ts = parse_integer(timestamp_ms, field="timestamp_ms", minimum=1, maximum=253_402_300_799_000)

    sig = compute_chainlink_signature(
        client_id=clean_cid,
        client_secret=clean_sec,
        method=method,
        path=path,
        body=body,
        timestamp_ms=valid_ts,
    )

    return {
        "Authorization": clean_cid,
        "X-Authorization-Timestamp": str(valid_ts),
        "X-Authorization-Signature-SHA256": sig,
    }
