"""Fixed public GET surface; no generic HTTP client."""
import re
from dataclasses import dataclass
from typing import Callable, Literal
from datetime import datetime
from email.utils import parsedate_to_datetime
import httpx
from .models import MarketDataError
from .numeric import load_numeric_json

@dataclass(frozen=True)
class JsonRead:
    payload: object
    started_mono: float
    finished_mono: float

class PublicGet:
    def __init__(self, client: httpx.Client, *, monotonic: Callable[[], float],
                 sleep: Callable[[float], None], utcnow: Callable[[], datetime]):
        self.client, self.monotonic, self.sleep, self.utcnow = client, monotonic, sleep, utcnow

    def get_json(self, source: Literal["gamma", "clob"], path: str, *,
                 params: dict[str, str | int] | None = None, deadline: float) -> JsonRead:
        hosts = {"gamma":"https://gamma-api.polymarket.com", "clob":"https://clob.polymarket.com"}
        allowed = ((source == "clob" and path in ("/time", "/book")) or
                   (source == "gamma" and (path == "/events" or
                    re.fullmatch(r"/events/slug/[A-Za-z0-9_-]+|/markets/[0-9]+", path))))
        if not allowed:
            raise MarketDataError("transport_error", "Only approved public GET endpoints are allowed.")
        for attempt in range(3):
            remaining = self._remaining(deadline)
            start = self.monotonic()
            retry_after = None
            try:
                with self.client.stream("GET", hosts[source]+path, params=params, follow_redirects=False,
                                        headers={"Accept-Encoding":"identity"},
                                        timeout=httpx.Timeout(min(10.0, remaining))) as response:
                    self._remaining(deadline)
                    status = response.status_code
                    if status == 429 or 500 <= status <= 599:
                        error = MarketDataError("rate_limited" if status == 429 else "transport_error",
                                                f"{source} GET {path} returned HTTP {status}.", http_status=status)
                        retry_after = response.headers.get("Retry-After")
                    elif status != 200:
                        raise MarketDataError("transport_error", f"{source} GET {path} returned HTTP {status}.",
                                              http_status=status)
                    else:
                        encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                        if encoding != "identity":
                            raise MarketDataError("transport_error", "Unexpected encoded response; identity encoding required.")
                        cap = 10 * 1024 * 1024
                        length = response.headers.get("Content-Length")
                        if length is not None:
                            if len(length) > 20 or not length.isascii() or not length.isdecimal():
                                raise MarketDataError("transport_error", "Invalid Content-Length header.")
                            if int(length) > cap:
                                raise MarketDataError("transport_error", "Response exceeds 10 MiB.")
                        body = bytearray()
                        for chunk in response.iter_raw():
                            self._remaining(deadline)
                            if len(body) + len(chunk) > cap:
                                raise MarketDataError("transport_error", "Response exceeds 10 MiB.")
                            body.extend(chunk)
                            self._remaining(deadline)
                        self._remaining(deadline)
                        try:
                            payload = load_numeric_json(body)
                        except MarketDataError as exc:
                            raise MarketDataError("invalid_metadata", f"Invalid JSON from {source} GET {path}.",
                                                  http_status=status) from exc
                        self._remaining(deadline)
                        return JsonRead(payload, start, self.monotonic())
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                error = MarketDataError("transport_error", f"{source} GET {path}: {type(exc).__name__}.")
            except httpx.HTTPError as exc:
                raise MarketDataError("transport_error", f"{source} GET {path}: {type(exc).__name__}.") from exc
            if attempt == 2:
                raise error
            remaining = self._remaining(deadline)
            delay = (0.5, 1.0)[attempt]
            if retry_after is not None:
                try:
                    if re.fullmatch(r"[0-9]+", retry_after.strip()):
                        delay = float(int(retry_after.strip()))
                    else:
                        parsed = parsedate_to_datetime(retry_after)
                        if parsed.tzinfo is None:
                            raise ValueError("Retry-After date requires timezone")
                        delay = max(0.0, (parsed-self.utcnow()).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    delay = (0.5, 1.0)[attempt]
            if delay >= remaining:
                raise error
            self.sleep(delay)
        raise AssertionError("Unreachable retry state")

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise MarketDataError("transport_error", "120-second run deadline exhausted; no late snapshot.")
        return remaining
