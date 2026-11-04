"""One bounded, public-only full-depth capture. No engine, DB, or service changes."""
from __future__ import annotations

import argparse
import base64
from collections import deque
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
import math
from pathlib import Path
import signal
import sys
import threading
import time
import uuid
import zlib

import httpx

from .linux_mvp.collector import CollectionStopped, LiveCollector
from .market_data import models
from .market_data.freshness import evaluate, wall_us
from .market_data.http import AdmissionTransport, HttpAttempt, JsonRead

MAX_SIZE_BYTES = 262_144_000
FOOTER_RESERVE = 65_536
_CLASSES = {cls.__name__: cls for cls in (
    models.TokenOutcome, models.MarketDescriptor, models.BookLevel, models.OutcomeBook,
    models.BookObservation, models.MarketSnapshot, JsonRead, HttpAttempt)}


def encode(value):
    """JSON-compatible tags. Fixed dataclass allowlist; never import from archive."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError('non-finite float')
        return value
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError('non-finite Decimal')
        return {'@t': 'Decimal', 'v': str(value)}
    if type(value) is datetime:
        if value.utcoffset() is None:
            raise ValueError('aware datetime required')
        return {'@t': 'datetime', 'v': value.isoformat()}
    if type(value) is bytes:
        return {'@t': 'bytes', 'v': base64.b64encode(value).decode('ascii')}
    if type(value) is tuple:
        return {'@t': 'tuple', 'v': [encode(item) for item in value]}
    if type(value).__name__ in _CLASSES and _CLASSES[type(value).__name__] is type(value):
        return {'@d': type(value).__name__, '@f': {
            field.name: encode(getattr(value, field.name)) for field in fields(value)}}
    if type(value) is list:
        return [encode(item) for item in value]
    if type(value) is dict:
        if '@t' in value or '@d' in value:
            raise ValueError('reserved codec tag in untyped dict')
        if any(type(key) is not str for key in value):
            raise ValueError('string dict keys required')
        return {key: encode(item) for key, item in value.items()}
    raise ValueError(f'unsupported type: {type(value).__name__}')


def decode(value):
    """Restore only allowlisted dataclasses and safe scalar/container types."""
    if type(value) is list:
        return [decode(item) for item in value]
    if type(value) is not dict:
        return value
    if '@d' in value:
        if set(value) != {'@d', '@f'} or type(value['@d']) is not str:
            raise ValueError('invalid dataclass tag')
        cls = _CLASSES.get(value['@d'])
        payload = value['@f']
        if cls is None or type(payload) is not dict or set(payload) != {f.name for f in fields(cls)}:
            raise ValueError('unknown dataclass or field mismatch')
        return cls(**{key: decode(item) for key, item in payload.items()})
    if '@t' in value:
        if set(value) != {'@t', 'v'}:
            raise ValueError('invalid scalar tag')
        kind, payload = value['@t'], value['v']
        if kind == 'tuple' and type(payload) is list:
            return tuple(decode(item) for item in payload)
        if type(payload) is not str:
            raise ValueError('invalid scalar payload')
        if kind == 'Decimal':
            result = Decimal(payload)
            if not result.is_finite():
                raise ValueError('non-finite Decimal')
            return result
        if kind == 'datetime':
            result = datetime.fromisoformat(payload)
            if result.utcoffset() is None:
                raise ValueError('aware datetime required')
            return result
        if kind == 'bytes':
            return base64.b64decode(payload, validate=True)
        raise ValueError('unknown scalar tag')
    return {key: decode(item) for key, item in value.items()}


@dataclass(frozen=True)
class Budget:
    target_seconds: float = 2100.0
    hard_seconds: float = 2700.0
    max_cycles: int = 850
    max_attempts: int = 4500
    max_sends_per_window: int = 180
    size_cap_bytes: int = MAX_SIZE_BYTES
    min_cycle_gap_seconds: float = 3.0
    resolution_every_seconds: float = 60.0

    def __post_init__(self):
        for name, minimum, maximum in (
            ('target_seconds', 0, 2100), ('hard_seconds', 0.001, 2700),
            ('min_cycle_gap_seconds', 3, 2700), ('resolution_every_seconds', 60, 2700)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError(f'invalid {name}')
        for name, minimum, maximum in (
            ('max_cycles', 1, 850), ('max_attempts', 1, 4500),
            ('max_sends_per_window', 1, 180), ('size_cap_bytes', FOOTER_RESERVE + 1024, MAX_SIZE_BYTES)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f'invalid {name}')
        if self.target_seconds > self.hard_seconds:
            raise ValueError('target exceeds hard timeout')


class CaptureStopped(CollectionStopped):
    """Terminal cap; not a retryable transport failure."""


class HardTimeout(BaseException):
    """SIGALRM interrupts even a stuck blocking network operation."""


class EventRecorder:
    """Exclusive new file; one complete gzip member per JSONL record."""

    def __init__(self, path, *, size_cap_bytes=MAX_SIZE_BYTES, footer_reserve_bytes=FOOTER_RESERVE):
        if (type(size_cap_bytes) is not int or not 0 < size_cap_bytes <= MAX_SIZE_BYTES or
                type(footer_reserve_bytes) is not int or not 0 <= footer_reserve_bytes < size_cap_bytes):
            raise ValueError('invalid compressed size cap/reserve')
        self.path = Path(path)
        self.size_cap_bytes = size_cap_bytes
        self.footer_reserve_bytes = footer_reserve_bytes
        self.bytes_written = self.records_written = 0
        self.full = False
        self._file = self.path.open('xb')

    def write(self, record, *, terminal=False):
        data = json.dumps(record, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        blob = gzip.compress(data, compresslevel=6, mtime=0)
        reserve = 0 if terminal else self.footer_reserve_bytes
        if self.bytes_written + len(blob) + reserve > self.size_cap_bytes:
            self.full = True
            return False
        self._file.write(blob)
        self._file.flush()
        self.bytes_written += len(blob)
        self.records_written += 1
        return True

    def close(self):
        self._file.close()


def read_records(path, *, strict=False):
    """Yield typed records only after complete gzip member CRC/trailer validation.

    Default tolerates incomplete final member, discarding it entirely. Corruption
    and malformed complete JSON always raise; strict also rejects truncated tail.
    Max decompressed member 128 MiB prevents unbounded archive decompression.
    """
    rest = b''
    member = bytearray()
    stream = zlib.decompressobj(31)
    with Path(path).open('rb') as handle:
        while True:
            if not rest:
                rest = handle.read(65_536)
                if not rest:
                    if (member or not stream.eof) and strict:
                        raise ValueError('incomplete gzip tail')
                    return
            if stream.eof:
                stream = zlib.decompressobj(31)
                member.clear()
            piece = stream.decompress(rest, 128 * 1024 * 1024 + 1 - len(member))
            member.extend(piece)
            if len(member) > 128 * 1024 * 1024:
                raise ValueError('gzip member exceeds 128 MiB')
            rest = stream.unused_data if stream.eof else stream.unconsumed_tail
            if stream.eof:
                if not member.endswith(b'\n'):
                    raise ValueError('member missing JSONL newline')
                for line in member.splitlines():
                    record = decode(json.loads(line))
                    if type(record) is not dict:
                        raise ValueError('record must be dict')
                    yield record
                member.clear()


class _SendGuardTransport(httpx.BaseTransport):
    def __init__(self, inner, guard):
        self.inner, self.guard = inner, guard

    def handle_request(self, request):
        self.guard(request)
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


def wire_capture(collector, session):
    """Guard AdmissionTransport's inner send, not PublicGet's duplicate hooks."""
    public = collector.clob.get
    previous = public.on_attempt

    def on_attempt(attempt):
        session._attempt_sink(attempt)
        if previous is not None:
            previous(attempt)

    public.on_attempt = on_attempt
    client = collector.client
    transports = [client._transport] + [t for t in client._mounts.values() if t is not None]
    for transport in {id(t): t for t in transports}.values():
        if not isinstance(transport, AdmissionTransport):
            raise ValueError('PublicGet admission transport required')
        transport.inner = _SendGuardTransport(transport.inner, session._guard_send)


class CaptureSession:
    """Recording never stops for strict freshness rejection or data errors."""

    def __init__(self, collector, recorder, budget=Budget(), *, session_id=None,
                 monotonic=time.monotonic, monotonic_ns=None,
                 utcnow=lambda: datetime.now(timezone.utc), sleep=time.sleep,
                 window_table=None):
        self.collector, self.recorder, self.budget = collector, recorder, budget
        self.monotonic, self.sleep, self.utcnow = monotonic, sleep, utcnow
        self.monotonic_ns = monotonic_ns or (lambda: int(monotonic() * 1e9))
        self.session_id = session_id or uuid.uuid4().hex
        self.window_table = {} if window_table is None else window_table
        self._resolution_attempts = {}
        self._send_times = deque()
        self.attempts = self.cycles = self.failed_cycles = self.attempt_records = 0
        self._last_send = None
        self._pending_send = False
        self._started = None
        self.reason = None
        wire_capture(collector, self)

    def _check_time(self):
        if self.monotonic() - self._started >= self.budget.hard_seconds:
            raise CaptureStopped('HARD_TIMEOUT')

    def _guard_send(self, request):
        self._check_time()
        if self.recorder.full:
            raise CaptureStopped('SIZE_LIMIT')
        if self.attempts >= self.budget.max_attempts:
            raise CaptureStopped('ATTEMPT_LIMIT')
        # Serial >=340ms separation implies <=177 sends in any rolling 60s.
        while True:
            now = self.monotonic()
            while self._send_times and self._send_times[0] <= now - 60.0:
                self._send_times.popleft()
            wait = max(0.0, (self._last_send + .34 - now) if self._last_send is not None else 0)
            if len(self._send_times) >= self.budget.max_sends_per_window:
                wait = max(wait, self._send_times[0] + 60.000001 - now)
            if wait <= 0:
                break
            self.sleep(min(wait, max(0, self._started + self.budget.hard_seconds - now)))
            self._check_time()
        # Recheck original stop and phase/run deadline AFTER pacing, before send.
        guard = request.extensions.get('send_guard')
        if guard is not None:
            guard()
        self._check_time()
        now = self.monotonic()
        self._send_times.append(now)
        self._last_send = now
        self.attempts += 1
        self._pending_send = True

    def _write(self, record, *, terminal=False):
        if not self.recorder.write(record, terminal=terminal):
            raise CaptureStopped('SIZE_LIMIT')

    def _attempt_sink(self, attempt):
        actual = self._pending_send
        self._pending_send = False
        self.attempt_records += 1
        self._write({'kind': 'attempt', 'cycle': self.cycles, 'actual_send': actual,
                     'attempt': encode(attempt)})

    def _stamps(self):
        return self.monotonic_ns(), wall_us(self.utcnow())

    def _resolutions(self):
        for condition, window in list(self.window_table.items()):
            now = self.monotonic()
            if window['end_us'] > wall_us(self.utcnow()):
                continue
            if now - self._resolution_attempts.get(condition, -math.inf) < self.budget.resolution_every_seconds:
                continue
            self._resolution_attempts[condition] = now
            evidence = {}
            try:
                evidence = self.collector.get_resolution(window, deadline=min(
                    now + 10, self._started + self.budget.hard_seconds)) or {}
            except (CaptureStopped, CollectionStopped, HardTimeout):
                raise
            except Exception as exc:
                evidence = {'error': type(exc).__name__, 'message': str(exc)[:300]}
            received_ns, received_us = self._stamps()
            self._write({'kind': 'resolution', 'window': window, 'evidence': encode(evidence),
                         'received_mono_ns': received_ns, 'received_wall_us': received_us})

    def run(self):
        self._started = self.monotonic()
        start_ns, start_us = self._stamps()
        self._write({'kind': 'session', 'session': self.session_id, 'mode': 'PAPER_ONLY',
                     'started_mono_ns': start_ns, 'started_wall_us': start_us,
                     'caps': asdict(self.budget), 'send_spacing_seconds': .34})
        try:
            while self.monotonic() - self._started < self.budget.target_seconds:
                self._check_time()
                if self.cycles >= self.budget.max_cycles:
                    raise CaptureStopped('CYCLE_LIMIT')
                if self.attempts >= self.budget.max_attempts:
                    raise CaptureStopped('ATTEMPT_LIMIT')
                self.cycles += 1
                snapshot, error = None, None
                terminal = None
                try:
                    snapshot = self.collector.collect()
                except (CaptureStopped, CollectionStopped, HardTimeout) as exc:
                    terminal = exc
                    error = {'code': type(exc).__name__, 'message': str(exc)[:300]}
                except Exception as exc:
                    error = {'code': getattr(exc, 'code', type(exc).__name__),
                             'message': str(exc)[:300],
                             'market': encode(getattr(exc, 'market', None))}
                ns, us = self._stamps()
                freshness = None if snapshot is None else evaluate(
                    snapshot, decision_mono_ns=ns, max_age_seconds=5, sample_seconds=2)
                if snapshot is not None:
                    market = snapshot.market
                    self.window_table.setdefault(market.condition_id, {
                        'market_id': market.market_id, 'condition_id': market.condition_id,
                        'event_id': market.event_id, 'start_us': wall_us(market.window_start),
                        'end_us': wall_us(market.window_end)})
                else:
                    self.failed_cycles += 1
                record = {'kind': 'cycle', 'cycle': self.cycles, 'decision_mono_ns': ns,
                          'decision_wall_us': us, 'snapshot': encode(snapshot), 'error': error,
                          'freshness': freshness}
                # Small terminal incomplete-cycle record may use footer reserve.
                if isinstance(terminal, CaptureStopped) and str(terminal) == 'SIZE_LIMIT':
                    self.recorder.write(record, terminal=True)
                else:
                    self._write(record)
                if terminal is not None:
                    raise terminal
                self._resolutions()
                # Always full completion-to-next-start gap, never scheduled catch-up.
                remaining = self.budget.target_seconds - (self.monotonic() - self._started)
                if remaining > 0:
                    self.sleep(min(self.budget.min_cycle_gap_seconds, remaining))
            self.reason = 'TARGET_REACHED'
        except CaptureStopped as exc:
            self.reason = str(exc)
        except CollectionStopped:
            self.reason = 'STOP_REQUESTED'
        except HardTimeout:
            self.reason = 'HARD_TIMEOUT'
        except KeyboardInterrupt:
            self.reason = 'INTERRUPTED'
        finally:
            ns, us = self._stamps()
            summary = {'kind': 'summary', 'session': self.session_id, 'reason': self.reason,
                       'cycles': self.cycles, 'failed_cycles': self.failed_cycles,
                       'attempts': self.attempts, 'attempt_records': self.attempt_records,
                       'started_mono_ns': start_ns, 'ended_mono_ns': ns,
                       'started_wall_us': start_us, 'ended_wall_us': us,
                       'bytes_before_summary': self.recorder.bytes_written,
                       'caps': asdict(self.budget)}
            try:
                if not self.recorder.write(summary, terminal=True):
                    raise ValueError('reserved terminal summary does not fit')
            finally:
                self.recorder.close()
        return summary


def cli(argv=None, *, collector_factory=None, monotonic=time.monotonic,
        monotonic_ns=time.monotonic_ns, utcnow=lambda: datetime.now(timezone.utc), sleep=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--out', required=True, help='new disposable output directory')
    parser.add_argument('--target-seconds', type=float, default=2100.0)
    parser.add_argument('--hard-seconds', type=float, default=2700.0)
    parser.add_argument('--max-cycles', type=int, default=850)
    parser.add_argument('--max-attempts', type=int, default=4500)
    parser.add_argument('--size-cap-bytes', type=int, default=MAX_SIZE_BYTES)
    args = parser.parse_args(argv)
    try:
        budget = Budget(**{key: value for key, value in vars(args).items() if key != 'out'})
        if not hasattr(signal, 'SIGALRM') or threading.current_thread() is not threading.main_thread():
            raise ValueError('Linux main thread SIGALRM required; capture refused without hard timeout')
        root = Path(args.out).expanduser()
        root.mkdir(parents=True, exist_ok=False)
    except (ValueError, OSError) as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 2

    def alarm_handler(_signum, _frame):
        raise HardTimeout()

    previous = signal.signal(signal.SIGALRM, alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, budget.hard_seconds)
    collector = recorder = None
    try:
        # Default collector owns an unauthenticated trust_env=False/no-redirects client.
        collector = collector_factory() if collector_factory else LiveCollector()
        recorder = EventRecorder(root / 'events.jsonl.gz', size_cap_bytes=budget.size_cap_bytes)
        capture = CaptureSession(collector, recorder, budget, monotonic=monotonic,
                                 monotonic_ns=monotonic_ns, utcnow=utcnow, sleep=sleep)
        summary = capture.run()
        print(json.dumps(summary, sort_keys=True))
        return 124 if summary['reason'] == 'HARD_TIMEOUT' else 0
    except HardTimeout:
        print('hard timeout during startup/finalization', file=sys.stderr)
        return 124
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if recorder is not None:
            recorder.close()
        if collector is not None:
            collector.client.close()
            collector.close()


if __name__ == '__main__':
    sys.exit(cli())
