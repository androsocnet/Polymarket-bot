"""Collect one independently validated public book per outcome."""
from datetime import datetime, timezone
from typing import Callable

from polymarket_bot.market_data.clob import parse_book
from polymarket_bot.market_data.http import JsonRead, PublicGet
from polymarket_bot.market_data.models import MarketDataError, MarketDescriptor
from polymarket_bot.market_data.numeric import parse_decimal
from .models import OutcomeResult, PairedBooks

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _epoch_us(value: datetime) -> int:
    delta = value - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def collect_books(public: PublicGet, market: MarketDescriptor, *, deadline: float,
                  receipt_server_us: Callable[[float], int], uncertainty_us: int) -> PairedBooks:
    if type(uncertainty_us) is not int or uncertainty_us < 0:
        raise ValueError("uncertainty_us must be nonnegative")
    attempts = []
    previous = public.on_attempt
    previous_max_attempts = public.max_attempts
    end_us = _epoch_us(market.window_end)

    def observe(attempt):
        attempts.append(attempt)
        if previous is not None:
            previous(attempt)

    def can_start():
        now = public.monotonic()
        return now < deadline and receipt_server_us(now) < end_us

    def failed(label, token, status, code, index, quality=None):
        return OutcomeResult(label, token, market.condition_id, status, None,
                             index, code, quality or {}, {})

    def validate(label, token, row, read, index, *, batch, extra_count=0, malformed_count=0):
        quality = {"batch_not_guaranteed_atomic": True} if batch else {}
        if extra_count:
            quality["unexpected_token"] = min(extra_count, 10_000)
        if malformed_count:
            quality["malformed_batch_elements"] = min(malformed_count, 10_000)
        if not can_start():
            return failed(label, token, "LATE", "late_response", index, quality)
        try:
            book = parse_book(JsonRead(row, read.started_mono, read.finished_mono),
                              token_id=token, condition_id=market.condition_id)
            source_us = _epoch_us(book.source_at)
            receipt_us = receipt_server_us(read.finished_mono)
            quality["source_age_us"] = receipt_us - source_us
            if source_us > receipt_us + 5_000_000 + uncertainty_us:
                raise MarketDataError("future_source_time", "Book source time exceeds receipt uncertainty.")
            metadata = {}
            raw_hash = row.get("hash")
            if raw_hash is not None:
                if not isinstance(raw_hash, str) or len(raw_hash) > 256:
                    raise MarketDataError("invalid_book", "Invalid book hash.")
                metadata["hash"] = raw_hash
            for key in ("tick_size", "min_order_size"):
                raw = row.get(key)
                if raw is not None:
                    value = parse_decimal(raw, field=key)
                    if value < 0:
                        raise MarketDataError("invalid_book", "Negative book metadata.")
                    metadata[key] = value
            if not can_start():
                return failed(label, token, "LATE", "late_response", index, quality)
            return OutcomeResult(label, token, market.condition_id, "VALID", book,
                                 index, None, quality, metadata)
        except MarketDataError as exc:
            return failed(label, token, "INVALID", exc.code, index, quality)

    def individual(label, token):
        if not can_start():
            return failed(label, token, "LATE", "deadline_exhausted", None)
        first_attempt = len(attempts)

        def last_matching_attempt():
            return next((index for index in range(len(attempts) - 1, first_attempt - 1, -1)
                         if attempts[index].operation == "BOOK_SINGLE"
                         and attempts[index].selectors == ("token_id=" + token,)), None)

        try:
            read = public.get_json("clob", "/book", params={"token_id": token}, deadline=deadline)
        except MarketDataError as exc:
            index = last_matching_attempt()
            status = ("LATE" if not can_start() else
                      "UNAVAILABLE" if exc.code in ("transport_error", "rate_limited") else "INVALID")
            return failed(label, token, status, exc.code, index)
        index = last_matching_attempt()
        return validate(label, token, read.payload, read, index, batch=False)

    public.on_attempt = observe
    public.max_attempts = min(previous_max_attempts, 2)
    try:
        labels = (("UP", market.up.token_id), ("DOWN", market.down.token_id))
        if not can_start():
            return PairedBooks(*(failed(label, token, "LATE", "deadline_exhausted", None)
                                 for label, token in labels), tuple(attempts))
        try:
            read = public.get_books((market.up.token_id, market.down.token_id), deadline=deadline)
        except MarketDataError as exc:
            index = len(attempts) - 1 if attempts else None
            if exc.http_status in (404, 405):
                return PairedBooks(*(individual(label, token) for label, token in labels), tuple(attempts))
            status = ("LATE" if not can_start() else
                      "UNAVAILABLE" if exc.code in ("transport_error", "rate_limited") else "INVALID")
            return PairedBooks(*(failed(label, token, status, exc.code, index)
                                 for label, token in labels), tuple(attempts))
        index = len(attempts) - 1
        payload = read.payload
        if not isinstance(payload, list):
            return PairedBooks(*(failed(label, token, "INVALID", "invalid_batch_envelope", index)
                                 for label, token in labels), tuple(attempts))
        rows = [row for row in payload if isinstance(row, dict) and
                isinstance(row.get("asset_id"), str) and len(row["asset_id"]) <= 128]
        malformed_count = len(payload) - len(rows)
        expected = {token for _, token in labels}
        extras = sum(row["asset_id"] not in expected for row in rows)
        results = {}
        missing = []
        for label, token in labels:
            matches = [row for row in rows if row["asset_id"] == token]
            if len(matches) > 1:
                result = failed(label, token, "INVALID", "duplicate_token", index,
                                {"batch_not_guaranteed_atomic": True})
            elif not matches:
                if malformed_count:
                    result = failed(label, token, "INVALID", "invalid_batch_element", index,
                                    {"batch_not_guaranteed_atomic": True,
                                     "malformed_batch_elements": min(malformed_count, 10_000)})
                else:
                    missing.append((label, token))
                    continue
            else:
                result = validate(label, token, matches[0], read, index, batch=True,
                                  extra_count=extras, malformed_count=malformed_count)
            results[label] = result
        for label, token in missing:
            results[label] = individual(label, token)
        return PairedBooks(results["UP"], results["DOWN"], tuple(attempts))
    finally:
        public.max_attempts = previous_max_attempts
        public.on_attempt = previous
