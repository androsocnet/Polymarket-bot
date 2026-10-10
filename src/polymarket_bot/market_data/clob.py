"""Public CLOB reads, explicit timestamp units, and book-derived quotes."""
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from typing import Callable
from .http import JsonRead, PublicGet
from .models import BookLevel, OutcomeBook, MarketDataError
from .numeric import exact_decimal_context, parse_decimal

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

class ClobClient:
    def __init__(self, get: PublicGet):
        self.get = get
    def get_time(self, *, deadline: float) -> JsonRead:
        try:
            return self.get.get_json("clob", "/time", deadline=deadline)
        except MarketDataError as exc:
            raise MarketDataError("time_unavailable", f"CLOB server time failed: {exc.code}: {exc}",
                                  http_status=exc.http_status) from exc
    def get_book(self, token_id: str, *, deadline: float) -> JsonRead:
        if not isinstance(token_id, str) or len(token_id) > 128 or not re.fullmatch(r"[0-9]+", token_id):
            raise MarketDataError("unsupported_identifier_shape", "Invalid CLOB token_id.")
        try:
            return self.get.get_json("clob", "/book", params={"token_id":token_id}, deadline=deadline)
        except MarketDataError as exc:
            code = "book_unavailable" if exc.http_status == 404 else "invalid_book" if exc.code == "invalid_metadata" else exc.code
            raise MarketDataError(code, f"CLOB book failed: {exc}", http_status=exc.http_status) from exc

class ServerClock:
    def __init__(self, sample: JsonRead, *, monotonic: Callable[[], float]):
        try:
            if type(sample.payload) is not int or sample.payload <= 0:
                raise ValueError("Expected positive Unix seconds")
            self.anchor = EPOCH + timedelta(seconds=sample.payload)
        except (ValueError, OverflowError, TypeError) as exc:
            raise MarketDataError("time_unavailable", "CLOB /time must return valid integer Unix seconds.") from exc
        self.monotonic = monotonic
        self.midpoint = (sample.started_mono + sample.finished_mono) / 2
        self.round_trip_seconds = sample.finished_mono - sample.started_mono
    def now(self) -> datetime:
        return self.utc_at(self.monotonic())
    def utc_at(self, moment: float) -> datetime:
        return self.anchor + timedelta(seconds=moment-self.midpoint)

def parse_book(read: JsonRead, *, token_id: str, condition_id: str) -> OutcomeBook:
    data = read.payload
    if not isinstance(data, dict):
        raise MarketDataError("invalid_book", "Expected a CLOB book object.")
    if not isinstance(token_id, str) or len(token_id) > 128 or not re.fullmatch(r"[0-9]+", token_id):
        raise MarketDataError("invalid_book", "Invalid CLOB book token identifier.")
    if data.get("asset_id") != token_id or data.get("market") != condition_id:
        raise MarketDataError("book_identity_mismatch", "CLOB book token/condition does not match Gamma.")
    try:
        raw = data.get("timestamp")
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{13}", raw):
            raise ValueError("Expected Unix milliseconds as 13-digit string")
        millis = int(raw)
        source = EPOCH + timedelta(seconds=millis//1000, milliseconds=millis%1000)
        def levels(name):
            rows = data.get(name)
            if not isinstance(rows, list):
                raise ValueError(f"Missing {name} array")
            result = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Malformed book level")
                values = [row.get("price"), row.get("size")]
                if any(isinstance(v, bool) or not isinstance(v, (str, int, Decimal)) for v in values):
                    raise ValueError("Invalid Decimal shape")
                price, size = (parse_decimal(values[0], field="book price"),
                               parse_decimal(values[1], field="book size"))
                if not price.is_finite() or not size.is_finite() or not 0 <= price <= 1 or size < 0:
                    raise ValueError("Invalid price or size")
                result.append(BookLevel(price, size))
            return tuple(result)
        bids, asks = levels("bids"), levels("asks")
        bid = max((row.price for row in bids if row.size > 0), default=None)
        ask = min((row.price for row in asks if row.size > 0), default=None)
        if bid is not None and ask is not None and bid > ask:
            raise ValueError("Crossed book")
        both = bid is not None and ask is not None
        with exact_decimal_context():
            return OutcomeBook(token_id, condition_id, raw, source, bids, asks, bid, ask,
                               (bid+ask)/Decimal(2) if both else None, ask-bid if both else None,
                               sum((row.size for row in bids if row.price == bid and row.size > 0), Decimal(0)) if bid is not None else None,
                               sum((row.size for row in asks if row.price == ask and row.size > 0), Decimal(0)) if ask is not None else None)
    except (ValueError, TypeError, OverflowError, DecimalException, MarketDataError) as exc:
        raise MarketDataError("invalid_book", "Invalid CLOB book numeric or level evidence.") from exc
