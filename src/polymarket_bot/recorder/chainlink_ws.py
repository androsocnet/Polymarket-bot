from __future__ import annotations

from collections import deque
from hashlib import sha256
import math
import threading
import time

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.sync.client import reconnect


class _NoRedirectConnect(reconnect):
    def process_redirect(self, exc):
        return exc


def connect(uri, **kwargs):
    return _NoRedirectConnect(uri, **kwargs).connect()

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.numeric import load_numeric_json
from polymarket_bot.recorder.chainlink_auth import build_chainlink_auth_headers, validate_credentials
from polymarket_bot.recorder.chainlink_rest import parse_report, validate_endpoint, validate_feed


class ChainlinkLiveConsumer:
    _MAX_REPORT_BYTES = 10 * 1024 * 1024

    def __init__(self, ws_url, feed_id, client_id, client_secret, *,
                 clock_seconds=time.time, monotonic=time.monotonic, connector=None):
        validate_endpoint(ws_url, 'wss', 'ws.dataengine.chain.link', ('', '/', '/ws/v1'))
        validate_feed(feed_id)
        self._client_id, self._client_secret = validate_credentials(client_id, client_secret)
        self._target = '/ws/v1?feedIDs=' + feed_id
        self._clock_seconds = clock_seconds
        self._monotonic = monotonic
        self._connector = connector
        self._lock = threading.Lock()
        self._lifecycle = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._ws = None
        self._connected = False
        self._latest_report = None
        self._gaps = deque(maxlen=128)
        self._failure = None

    def check_health(self):
        with self._lock:
            if self._failure is not None:
                raise MarketDataError('websocket_failed', 'Reference stream worker failed.')

    def is_connected(self):
        with self._lock:
            return self._connected

    def report_count(self):
        with self._lock:
            return int(self._latest_report is not None)

    def start(self):
        with self._lifecycle:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._failure is not None:
                raise MarketDataError('websocket_failed', 'Consumer stopped after unexpected failure.')
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name='chainlink-live', daemon=False)
            self._thread.start()

    def stop(self):
        with self._lifecycle:
            self._stop_event.set()
            with self._lock:
                ws = self._ws
            close_failed = False
            try:
                if ws is not None:
                    ws.close()
            except Exception:
                close_failed = True
            finally:
                thread = self._thread
                if thread is not None:
                    thread.join(timeout=5)
                with self._lock:
                    self._connected = False
                if thread is not None and thread.is_alive():
                    raise MarketDataError('websocket_shutdown_failed', 'Consumer did not stop within shutdown bound.')
                self._thread = None
                with self._lock:
                    self._ws = None
            if close_failed:
                self._failure = 'close_failed'
                raise MarketDataError('websocket_shutdown_failed', 'Connection close failed; consumer joined.') from None

    def _connect(self):
        headers = build_chainlink_auth_headers(self._client_id, self._client_secret, 'GET',
                                               self._target, None, int(self._clock_seconds() * 1000))
        ws = (self._connector or connect)('wss://ws.dataengine.chain.link' + self._target,
                                         additional_headers=headers, proxy=None,
                                         open_timeout=2, close_timeout=1,
                                         max_size=self._MAX_REPORT_BYTES, max_queue=1,
                                         compression=None)
        with self._lock:
            self._ws = ws
            self._connected = ws is not None and not self._stop_event.is_set()
        if self._stop_event.is_set() and ws is not None:
            ws.close()

    def _run(self):
        backoff = 1
        while not self._stop_event.is_set():
            try:
                self._connect()
                self._message_loop()
            except InvalidStatus as exc:
                if exc.response.status_code not in (429, 500, 502, 503, 504):
                    self._failure = 'authentication_or_handshake'
                    self._stop_event.set()
            except InvalidHandshake:
                self._failure = 'authentication_or_handshake'
                self._stop_event.set()
            except MarketDataError:
                self._failure = 'invalid_message'
                self._stop_event.set()
            except (ConnectionClosed, ConnectionError, OSError, TimeoutError):
                pass
            except Exception:
                with self._lock:
                    self._failure = 'unexpected_worker_failure'
                self._stop_event.set()
                raise
            finally:
                with self._lock:
                    ws = self._ws
                    self._ws = None
                    self._connected = False
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        with self._lock:
                            self._failure = 'close_failed'
                        self._stop_event.set()
                        raise
            if not self._stop_event.is_set():
                start = self._monotonic()
                self._stop_event.wait(backoff)
                with self._lock:
                    self._gaps.append((start, self._monotonic()))
                backoff = min(backoff * 2, 30)

    def _message_loop(self):
        with self._lock:
            ws = self._ws
        if ws is None:
            return
        while not self._stop_event.is_set():
            try:
                raw = ws.recv(timeout=1)
            except TimeoutError:
                continue
            if raw is None:
                return
            if isinstance(raw, str):
                raw = raw.encode('utf-8')
            self._handle_websocket_message(raw)

    def _handle_websocket_message(self, raw):
        if not isinstance(raw, bytes) or not raw or len(raw) > self._MAX_REPORT_BYTES:
            raise MarketDataError('invalid_response', 'Invalid Chainlink message size.')
        receipt_wall_us = int(self._clock_seconds() * 1_000_000)
        receipt_monotonic = self._monotonic()
        signed, fields, draft = parse_report(load_numeric_json(raw))
        digest = sha256(signed).hexdigest()
        key = (draft.observations_seconds, digest)
        fields.update(receipt_wall_us=receipt_wall_us, receipt_monotonic=receipt_monotonic,
                      receipt_server_us=None, raw_message=raw, report_hash=digest,
                      verification_status='UNVERIFIED')
        with self._lock:
            if self._latest_report is not None and digest == self._latest_report[0][1]:
                previous = self._latest_report[2]
                evidence_keys = ('receipt_wall_us', 'receipt_monotonic', 'receipt_server_us',
                                 'raw_message', 'report_hash', 'verification_status')
                if {k: v for k, v in fields.items() if k not in evidence_keys} != {
                        k: v for k, v in previous.items() if k not in evidence_keys}:
                    raise MarketDataError('reference_metadata_conflict', 'Signed report metadata changed.')
                return
            if self._latest_report is None or key > self._latest_report[0]:
                self._latest_report = (key, signed, fields, draft)

    def get_freshest_report(self, max_age_s, now_server_s):
        if type(max_age_s) not in (int, float) or not math.isfinite(max_age_s) or not 0 < max_age_s <= 5:
            raise MarketDataError('invalid_age', 'Maximum report age must be within five seconds.')
        if type(now_server_s) not in (int, float) or not math.isfinite(now_server_s) or now_server_s < 0:
            raise MarketDataError('invalid_timestamp', 'Invalid decision server time.')
        with self._lock:
            if self._latest_report is None or self._failure is not None:
                return None
            _, signed, fields, draft = self._latest_report
            age = now_server_s - draft.observations_seconds
            if not (0 <= age <= max_age_s and draft.valid_from_seconds <= now_server_s <= draft.expires_at_seconds):
                return None
            return signed, dict(fields)

    def get_connection_gaps(self):
        with self._lock:
            return list(self._gaps)
