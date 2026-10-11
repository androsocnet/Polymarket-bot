"""Fixed public market-data reads; no generic HTTP client."""
import re
from dataclasses import dataclass
from typing import Callable, Literal
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import httpx
from .models import MarketDataError
from .numeric import load_numeric_json

@dataclass(frozen=True)
class JsonRead:
    payload: object
    started_mono: float
    finished_mono: float
    raw_body: bytes = b""
    started_mono_ns: int | None = None
    finished_mono_ns: int | None = None
    started_wall_us: int | None = None
    finished_wall_us: int | None = None


def _wall_us(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds

@dataclass(frozen=True)
class HttpAttempt:
    operation: str
    method: str
    path: str
    selectors: tuple[str, ...]
    attempt_number: int
    started_mono_ns: int
    ended_mono_ns: int
    started_wall_us: int
    ended_wall_us: int
    http_status: int | None
    result: str
    error_code: str | None
    raw_body: bytes
    raw_complete: bool

class PublicGet:
    def __init__(self, client: httpx.Client, *, monotonic: Callable[[], float],
                 sleep: Callable[[float], None], utcnow: Callable[[], datetime],
                 max_attempts: int = 3, phase_timeout: float = 10.0,
                 monotonic_ns: Callable[[], int] | None = None,
                 on_attempt: Callable[[HttpAttempt], None] | None = None,
                 before_attempt: Callable[[], None] | None = None):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be 1, 2, or 3")
        if not 0 < phase_timeout <= 10:
            raise ValueError("phase_timeout must be positive and at most 10 seconds")
        self.client, self.monotonic, self.sleep, self.utcnow = client, monotonic, sleep, utcnow
        self.max_attempts, self.phase_timeout = max_attempts, phase_timeout
        self.monotonic_ns = monotonic_ns or (lambda: int(self.monotonic() * 1_000_000_000))
        self.on_attempt = on_attempt
        self.before_attempt = before_attempt

    def get_json(self, source: Literal["gamma", "clob", "data"], path: str, *,
                 params: dict[str, str | int] | None = None, deadline: float) -> JsonRead:
        hosts = {"gamma":"https://gamma-api.polymarket.com", "clob":"https://clob.polymarket.com",
                 "data":"https://data-api.polymarket.com"}
        allowed = ((source == "clob" and (path in ("/time", "/book") or
                    re.fullmatch(r"/clob-markets/0x[0-9a-fA-F]{64}", path))) or
                   (source == "data" and path == "/v2/resolutions") or
                   (source == "gamma" and (path == "/events" or
                    re.fullmatch(r"/events/slug/[A-Za-z0-9_-]+|/markets/[0-9]+", path))))
        if not allowed:
            raise MarketDataError("transport_error", "Only approved public GET endpoints are allowed.")
        allowed_keys = ({"active", "closed", "end_date_min", "end_date_max", "limit", "offset",
                         "order", "ascending"} if source == "gamma" and path == "/events" else
                        {"token_id"} if source == "clob" and path == "/book" else
                        {"condition"} if source == "data" else set())
        if (params is not None and
                (type(params) is not dict or not set(params) <= allowed_keys or
                 any(type(value) not in (str, int) or
                     len(str(value)) > (1339 if source == "data" else 256)
                     for value in params.values()))):
            raise MarketDataError("transport_error", "Unsupported public GET selector.")
        if source == "data":
            condition = None if params is None else params.get("condition")
            if (type(condition) is not str or
                    re.fullmatch(r"0x[0-9a-fA-F]{64}(?:,0x[0-9a-fA-F]{64}){0,19}", condition) is None):
                raise MarketDataError("transport_error", "Expected 1 to 20 condition IDs.")
        selectors = tuple(f"{key}={value}" for key, value in sorted((params or {}).items()))
        if path.startswith("/clob-markets/"):
            selectors += ("condition=" + path.removeprefix("/clob-markets/"),)
        elif path.startswith("/events/slug/"):
            selectors += ("slug=" + path.removeprefix("/events/slug/"),)
        elif path.startswith("/markets/"):
            selectors += ("market_id=" + path.removeprefix("/markets/"),)
        return self._read_json("GET", source, path, hosts[source]+path,
                               deadline=deadline, params=params, selectors=selectors)

    def get_books(self, token_ids: tuple[str, str], *, deadline: float) -> JsonRead:
        if (type(token_ids) is not tuple or len(token_ids) != 2 or
                any(type(token) is not str or re.fullmatch(r"[0-9]{1,128}", token) is None
                    for token in token_ids) or token_ids[0] == token_ids[1]):
            raise MarketDataError("unsupported_identifier_shape", "Expected two distinct decimal-string token IDs.")
        return self._read_json("POST", "clob", "/books", "https://clob.polymarket.com/books",
                               deadline=deadline, json_body=[{"token_id": token_ids[0]},
                                                             {"token_id": token_ids[1]}],
                               selectors=token_ids)

    def _read_json(self, method: str, source: str, path: str, url: str, *,
                   deadline: float, params: dict[str, str | int] | None = None,
                   json_body: object = None, selectors: tuple[str, ...] = ()) -> JsonRead:
        operation = ("BOOK_BATCH" if method == "POST" else
                     "BOOK_SINGLE" if path == "/book" else
                     "CLOB_TIME" if path == "/time" else
                     "CLOB_CONFIG" if path.startswith("/clob-markets/") else
                     "RESOLUTION" if source == "data" else
                     "GAMMA_EVENTS" if path == "/events" else
                     "GAMMA_EVENT" if path.startswith("/events/slug/") else
                     "GAMMA_MARKET")
        had_pre_existing_cookies = bool(self.client.cookies)
        try:
            for attempt in range(self.max_attempts):
                if self.before_attempt is not None:
                    self.before_attempt()
                if (self.client.auth is not None or self.client.cookies or self.client.params or any(
                        key.lower().replace("_", "-") in {"authorization", "cookie", "poly-api-key",
                                        "poly-signature", "poly-passphrase", "x-api-key"}
                        or "signature" in key.lower() for key in self.client.headers)):
                    raise MarketDataError("transport_error", "Authenticated clients cannot make public market-data reads.")
                remaining = self._remaining(deadline)
                start = self.monotonic()
                started_ns = self.monotonic_ns()
                started_wall_us = _wall_us(self.utcnow())
                finished_ns = finished_wall_us = None
                retry_after = None
                retryable = False
                status = None
                body = bytearray()
                raw_complete = False
                error = None
                try:
                    with self.client.stream(method, url, params=params, json=json_body, follow_redirects=False,
                                            headers={"Accept-Encoding":"identity"},
                                            timeout=httpx.Timeout(min(self.phase_timeout, remaining))) as response:
                        self._remaining(deadline)
                        status = response.status_code
                        if status == 429 or 500 <= status <= 599:
                            error = MarketDataError("rate_limited" if status == 429 else "transport_error",
                                                    f"{source} {method} {path} returned HTTP {status}.", http_status=status)
                            retry_after = response.headers.get("Retry-After")
                            retryable = True
                        elif status != 200:
                            error = MarketDataError("transport_error",
                                                    f"{source} {method} {path} returned HTTP {status}.",
                                                    http_status=status)
                        self._receive_body(response, deadline, body)
                        raw_complete = True
                        if status == 200:
                            try:
                                payload = load_numeric_json(body)
                            except MarketDataError as exc:
                                raise MarketDataError("invalid_metadata", f"Invalid JSON from {source} {method} {path}.",
                                                      http_status=status) from exc
                            self._remaining(deadline)
                            finished_mono = self.monotonic()
                            finished_ns = self.monotonic_ns()
                            finished_wall_us = _wall_us(self.utcnow())
                            return JsonRead(payload, start, finished_mono, bytes(body),
                                            started_ns, finished_ns, started_wall_us, finished_wall_us)
                except MarketDataError as exc:
                    error = exc
                    retryable = False
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    error = MarketDataError("transport_error", f"{source} {method} {path}: {type(exc).__name__}.")
                    retryable = True
                except httpx.HTTPError as exc:
                    error = MarketDataError("transport_error", f"{source} {method} {path}: {type(exc).__name__}.")
                except BaseException:
                    error = MarketDataError("transport_error", f"{source} {method} {path}: interrupted.")
                    raise
                finally:
                    if not had_pre_existing_cookies:
                        self.client.cookies.clear()
                    if self.on_attempt is not None:
                        self.on_attempt(HttpAttempt(
                            operation, method, path, selectors, attempt + 1,
                            started_ns, finished_ns if finished_ns is not None else self.monotonic_ns(),
                            started_wall_us, finished_wall_us if finished_wall_us is not None else _wall_us(self.utcnow()), status,
                            "success" if error is None else "failure",
                            None if error is None else error.code, bytes(body), raw_complete))

                if not retryable or attempt == self.max_attempts - 1:
                    raise error
                remaining = self._remaining(deadline)
                delay = (0.5, 1.0)[attempt]
                if retry_after is not None:
                    try:
                        header = retry_after.strip()
                        if len(header) > 128:
                            raise ValueError("Oversized Retry-After")
                        if re.fullmatch(r"[0-9]+", header):
                            delay = float(int(header))
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
        finally:
            if not had_pre_existing_cookies:
                self.client.cookies.clear()


    def _receive_body(self, response: httpx.Response, deadline: float,
                      body: bytearray) -> None:
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
        for chunk in response.iter_raw():
            self._remaining(deadline)
            if len(body) + len(chunk) > cap:
                raise MarketDataError("transport_error", "Response exceeds 10 MiB.")
            body.extend(chunk)
            self._remaining(deadline)
        self._remaining(deadline)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise MarketDataError("transport_error", "120-second run deadline exhausted; no late snapshot.")
        return remaining
