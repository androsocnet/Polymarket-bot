"""CP6.3 offline guard components. Public launch blocked pending hard quotas."""
import argparse
import ctypes
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import threading

import httpx
from .live_guard import BudgetTransport
from .windows_job import WindowsJob, _Extended

MIB = 1024 * 1024
JOURNAL_LIMIT = 16 * MIB
ARTIFACT_LIMIT = 1024 * MIB
CAPTURE_LIMIT = 960 * MIB
TERMINAL_RESERVE = 64 * MIB
AUDIT_LIMIT = 16 * MIB


class ArtifactBudget:
    """Conservative cumulative allocation for mediated writers, not filesystem quota.

    ponytail: SQLite/WAL/temp bypass this counter; GO needs native volume quota.
    """
    hard_all_artifact_quota = False

    def __init__(self, *, capture_limit=CAPTURE_LIMIT, audit_limit=AUDIT_LIMIT):
        if (type(capture_limit) is not int or type(audit_limit) is not int or
                not 0 < audit_limit <= capture_limit <= CAPTURE_LIMIT):
            raise ValueError('invalid_artifact_budget')
        self.capture_limit, self.audit_limit = capture_limit, audit_limit
        self.capture_bytes = self.audit_bytes = 0
        self.failed = False
        self._lock = threading.Lock()

    def claim(self, amount, *, audit=False):
        if type(amount) is not int or amount < 0:
            raise ValueError('invalid_artifact_allocation')
        with self._lock:
            if (self.failed or self.capture_bytes+amount > self.capture_limit or
                    audit and self.audit_bytes+amount > self.audit_limit):
                self.failed = True
                raise OSError('artifact_budget_exhausted')
            self.capture_bytes += amount
            self.audit_bytes += amount if audit else 0


class BoundedLog:
    """No direct child file inheritance; pipe reader must call write in <=64KiB blocks."""
    def __init__(self, path, budget):
        self.budget = budget
        self._lock = threading.Lock()
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)

    def write(self, data):
        if type(data) is not bytes or len(data) > 65536:
            raise ValueError('invalid_log_block')
        with self._lock:
            try:
                if self._fd is None:
                    raise OSError('log_closed')
                self.budget.claim(len(data), audit=True)
                if os.write(self._fd, data) != len(data):
                    raise OSError('log_short_write')
                os.fsync(self._fd)
            except OSError:
                self.budget.failed = True
                raise

    def close(self):
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None


class TerminalReserve:
    """Physically written fixed-size file, single terminal commit, no growth.

    Not ACL protection against another writer or storage-device durability proof.
    """
    def __init__(self, path):
        self.path = Path(path)
        self._size = TERMINAL_RESERVE
        self._fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        self._committed = False
        try:
            block = bytes(min(MIB, self._size))
            remaining = self._size
            while remaining:
                data = block[:remaining]
                if os.write(self._fd, data) != len(data):
                    raise OSError('terminal_reserve_short_write')
                remaining -= len(data)
            os.fsync(self._fd)
        except BaseException:
            self.close()
            raise

    def commit(self, value):
        if self._fd is None or self._committed:
            raise OSError('terminal_commit_unavailable')
        self._committed = True
        data = _encoded(value)
        if type(value) is not dict or len(data) > min(65536, self._size-56):
            raise ValueError('terminal_record_invalid')
        header = b'CP63TERM'+len(data).to_bytes(8, 'big')+sha256(data).digest()
        os.lseek(self._fd, 0, os.SEEK_SET)
        if os.write(self._fd, header+data) != len(header)+len(data):
            raise OSError('terminal_short_write')
        os.fsync(self._fd)
        os.lseek(self._fd, self._size-8, os.SEEK_SET)
        if os.write(self._fd, b'COMMIT63') != 8:
            raise OSError('terminal_marker_short_write')
        os.fsync(self._fd)

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def read_terminal(path):
    """Structural integrity only; never authenticates runtime or cleanup."""
    with Path(path).open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        header = stream.read(48)
        if len(header) != 48 or header[:8] != b'CP63TERM':
            raise ValueError('terminal_missing_or_torn')
        length = int.from_bytes(header[8:16], 'big')
        if not 0 < length <= min(65536, size-56):
            raise ValueError('terminal_length_invalid')
        data = stream.read(length)
        stream.seek(size-8)
        if stream.read(8) != b'COMMIT63' or sha256(data).digest() != header[16:]:
            raise ValueError('terminal_missing_or_torn')
        value = json.loads(data)
        if type(value) is not dict:
            raise ValueError('terminal_record_invalid')
        return value


@dataclass(frozen=True)
class WindowPin:
    event_id: str
    market_id: str
    condition_id: str
    up_token: str
    down_token: str
    start_us: int
    end_us: int

    def __post_init__(self):
        if (any(type(v) is not str or re.fullmatch(r'[0-9]{1,256}', v) is None
                for v in (self.event_id, self.market_id)) or
                type(self.condition_id) is not str or
                re.fullmatch(r'0x[0-9a-fA-F]{64}', self.condition_id) is None or
                any(type(v) is not str or re.fullmatch(r'[0-9]{1,128}', v) is None
                    for v in (self.up_token, self.down_token)) or
                self.up_token == self.down_token or
                any(type(v) is not int or not 0 <= v <= 253_402_300_799_000_000
                    for v in (self.start_us, self.end_us)) or
                self.end_us-self.start_us != 900_000_000):
            raise ValueError('exact_selected_identity_and_900_second_window_required')

    @property
    def targets(self):
        return tuple(self.start_us+2_000_000*i for i in range(450))


class Deadlines:
    """Launch-relative maxima; selected close can only shorten STOP and cleanup."""
    def __init__(self, pin, *, launch_ns, launch_wall_us):
        if (type(launch_ns) is not int or not 0 <= launch_ns <= 2**63-1-950_000_000_000 or
                type(launch_wall_us) is not int or
                not pin.start_us-30_000_000 <= launch_wall_us <= pin.start_us):
            raise ValueError('launch_must_be_within_30_seconds_before_open')
        self.stop_ns = launch_ns+min(930_000_000_000, (pin.end_us-launch_wall_us)*1000)
        self.kill_ns = self.stop_ns+10_000_000_000
        self.hard_ns = self.kill_ns+10_000_000_000

    def tighten_close(self, close_ns):
        if type(close_ns) is not int or close_ns < 0:
            raise ValueError('invalid_close_deadline')
        self.stop_ns = min(self.stop_ns, close_ns)
        self.kill_ns = min(self.kill_ns, self.stop_ns+10_000_000_000)
        self.hard_ns = min(self.hard_ns, self.kill_ns+10_000_000_000)


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf8')


class DurableJournal:
    """Exclusive bounded JSONL, fsync per record. Chain detects damage, not forgery."""
    def __init__(self, path, *, budget=None):
        self.path = Path(path)
        self.budget = budget
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        self._lock = threading.Lock()
        self._size = self._sequence = 0
        self._digest = '0'*64
        self.failed = False

    def write(self, event, **fields):
        with self._lock:
            if self.failed or self._fd is None:
                raise OSError('journal_unavailable')
            try:
                if type(event) is not str or not event or {'sequence', 'previous', 'digest', 'event'} & fields.keys():
                    raise ValueError('invalid_journal_record')
                record = dict(fields, sequence=self._sequence+1, previous=self._digest, event=event)
                digest = sha256(_encoded(record)).hexdigest()
                data = _encoded(dict(record, digest=digest))+b'\n'
                if len(data) > 4096 or self._size+len(data) > JOURNAL_LIMIT:
                    raise OSError('journal_limit')
                if shutil.disk_usage(self.path.parent).free < len(data):
                    raise OSError('journal_disk_space')
                if self.budget is not None:
                    self.budget.claim(len(data), audit=True)
                if os.write(self._fd, data) != len(data):
                    raise OSError('journal_short_write')
                os.fsync(self._fd)
                self._sequence += 1
                self._size += len(data)
                self._digest = digest
            except (OSError, ValueError, TypeError):
                self.failed = True
                if self.budget is not None:
                    self.budget.failed = True
                raise

    def close(self):
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None


def read_journal(path):
    """Strict read-only structural check. Missing records/runtime provenance unknown."""
    path = Path(path)
    if path.stat().st_size > JOURNAL_LIMIT:
        raise ValueError('journal_limit')
    def unique(pairs):
        row = {}
        for key, value in pairs:
            if key in row:
                raise ValueError('journal_duplicate_key')
            row[key] = value
        return row
    records, previous = [], '0'*64
    with path.open('rb') as stream:
        for number, line in enumerate(iter(lambda: stream.readline(4097), b''), 1):
            if len(line) > 4096 or not line.endswith(b'\n'):
                raise ValueError('journal_truncated_or_oversized')
            try:
                row = json.loads(line, object_pairs_hook=unique)
                if type(row) is not dict:
                    raise ValueError('journal_invalid')
                digest = row.pop('digest')
                if (type(row['sequence']) is not int or row['sequence'] != number or row['previous'] != previous or
                        type(row['event']) is not str or not row['event'] or
                        digest != sha256(_encoded(row)).hexdigest()):
                    raise ValueError('journal_chain_invalid')
            except (KeyError, TypeError, UnicodeError) as exc:
                raise ValueError('journal_invalid') from exc
            row['digest'] = previous = digest
            records.append(row)
    if not records:
        raise ValueError('journal_empty')
    return records


BODY_LIMIT = 10 * MIB
INGRESS_LIMIT = 256 * MIB


class WindowTransport(httpx.BaseTransport):
    """Serial lower-entry accounting. Yielded ingress measured, not socket-capped.

    ponytail: HTTPX exposes already-read chunks; live GO requires bounded lower
    socket reads and hard artifact/supervisor budgets, not this observer alone.
    """
    hard_ingress_enforced = False

    def __init__(self, inner, *, pin, journal, monotonic_ns):
        self.inner, self.pin, self.journal = inner, pin, journal
        self.monotonic_ns = monotonic_ns
        self.sent = self.books = self.controls = self.ingress = 0
        self.stopped, self.reason = False, None
        self.recorder = None
        self._last_ns = None
        self._gate = threading.Lock()
        self._inflight = threading.Lock()

    def bind(self, recorder):
        self.recorder = recorder

    def _fail(self, reason):
        self.stopped, self.reason = True, self.reason or reason
        raise KeyboardInterrupt(self.reason)

    def _write(self, event, **fields):
        try:
            self.journal.write(event, mono_ns=self.monotonic_ns(), **fields)
        except (OSError, ValueError, TypeError):
            self._fail('guard_journal_failure')

    def request_stop(self):
        with self._gate:
            self.stopped = True
            self.reason = self.reason or 'guard_stop_requested'
        self._write('stop_committed', reason=self.reason)

    def _allowed(self, request):
        # Reuse accepted public/auth boundary, then narrow every lower selector.
        if not BudgetTransport._allowed(self, request):
            return False
        path = request.url.path
        if path.startswith('/clob-markets/'):
            return path == '/clob-markets/'+self.pin.condition_id
        if path == '/book':
            return request.url.params['token_id'] in (self.pin.up_token, self.pin.down_token)
        if path == '/books':
            return [row['token_id'] for row in json.loads(request.content)] == [self.pin.up_token, self.pin.down_token]
        return True

    @property
    def event_id(self):
        return self.pin.event_id

    @property
    def market_id(self):
        return self.pin.market_id

    def _book_check(self):
        recorder = self.recorder
        if recorder is None:
            self._fail('unbound_recorder')
        clock = recorder.clock
        if (not clock.usable() or clock.anchor.uncertainty_us > 1_250_000 or
                clock.anchor.status != 'VALID'):
            self._fail('clock_unusable')
        now = clock.server_us_at(self.monotonic_ns())
        if not self.pin.start_us <= now < self.pin.end_us:
            self._fail('selected_window_closed')
        revision = getattr(recorder, 'revision', None)
        if revision is None or not revision.capture_ready:
            self._fail('metadata_unavailable')
        market = revision.market
        from .service import _epoch_us
        if ((market.event_id, market.market_id, market.condition_id, market.up.token_id,
             market.down.token_id, _epoch_us(market.window_start), _epoch_us(market.window_end)) !=
                (self.pin.event_id, self.pin.market_id, self.pin.condition_id, self.pin.up_token,
                 self.pin.down_token, self.pin.start_us, self.pin.end_us)):
            self._fail('selected_identity_conflict')
        age = now-(revision.last_refresh_attempt_us or revision.observed_us)
        if not 0 <= age <= 65_000_000:
            self._fail('metadata_stale')
        armed = getattr(recorder, '_book_guard', None)
        if (armed is None or armed[1] not in self.pin.targets or
                not armed[1] <= now < min(armed[1]+2_000_000, self.pin.end_us)):
            self._fail('capture_target_not_armed')
        recorder._guard_book_attempt()

    def _check(self, book):
        now = self.monotonic_ns()
        if type(now) is not int or now < 0 or self._last_ns is not None and now < self._last_ns:
            self._fail('monotonic_discontinuity')
        self._last_ns = now
        if self.stopped:
            self._fail('guard_stop_requested')
        if any(type(n) is not int or n < 0 for n in (self.sent, self.books, self.controls, self.ingress)):
            self._fail('resource_counter_missing')
        if self.recorder is not None:
            anchor = self.recorder.clock.anchor
            if anchor is not None and anchor.status == 'DISCONTINUITY':
                self._fail('clock_discontinuity')
        if book:
            self._book_check()

    def handle_request(self, request):
        if not self._inflight.acquire(blocking=False):
            self._fail('request_already_inflight')
        handed_off = False
        try:
            book = request.url.path in ('/book', '/books')
            self._check(book)
            if not self._allowed(request):
                self._fail('public_request_refused')
            if self.sent >= 3000 or self.books >= 2700 and book or self.controls >= 300 and not book:
                self._fail('http_budget_exhausted')
            if self.ingress >= INGRESS_LIMIT:
                self._fail('ingress_exhausted')
            number = self.sent+1
            self._write('dispatch', number=number, method=request.method, path=request.url.path,
                        url=str(request.url))
            with self._gate:
                try:
                    self._check(book)
                except KeyboardInterrupt:
                    self._write('dispatch_cancelled', number=number, reason=self.reason)
                    raise
                self.sent += 1
                self.books += int(book)
                self.controls += int(not book)
                try:
                    response = self.inner.handle_request(request)
                except BaseException:
                    self.stopped = True
                    self.reason = self.reason or 'transport_error'
                    self._write('transport_error', number=number, ingress=self.ingress)
                    raise
            try:
                self._write('response', number=number, status=response.status_code)
                response.stream = _MeasuredStream(self, response.stream, number, book)
                handed_off = True
                return response
            except BaseException:
                response.close()
                raise
        finally:
            if not handed_off:
                self._inflight.release()

    def close(self):
        self.inner.close()


class _MeasuredStream(httpx.SyncByteStream):
    def __init__(self, guard, inner, number, book):
        self.guard, self.inner, self.number, self.book = guard, inner, number, book
        self.received, self.complete, self.closed = 0, False, False

    def __iter__(self):
        g = self.guard
        try:
            for chunk in self.inner:
                # Account first: even a late/error/oversized yielded chunk arrived.
                self.received += len(chunk)
                g.ingress += len(chunk)
                g._write('ingress', number=self.number, bytes=len(chunk), total=g.ingress)
                if self.received > BODY_LIMIT or g.ingress > INGRESS_LIMIT:
                    g._fail('stream_budget_exceeded')
                g._check(self.book)
                yield chunk
            g._check(self.book)
            self.complete = True
            g._write('body_complete', number=self.number, bytes=self.received, total=g.ingress)
        except BaseException:
            g.stopped = True
            g.reason = g.reason or 'stream_error'
            g._write('stream_error', number=self.number, bytes=self.received, total=g.ingress)
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.inner.close()
            if not self.complete:
                self.guard.stopped = True
                self.guard.reason = self.guard.reason or 'body_incomplete'
                self.guard._write('body_incomplete', number=self.number, bytes=self.received)
        finally:
            self.guard._inflight.release()


class FullWindowJob(WindowsJob):
    """Separate native limits. Accepted CP5 wrapper remains byte-identical."""
    def __init__(self, name):
        super().__init__(name)
        limits = _Extended()
        limits.basic.flags = 0x220c  # KILL_ON_CLOSE | JOB_MEMORY | ACTIVE_PROCESS | JOB_TIME
        limits.basic.active_limit = 1
        limits.basic.job_user = 120*10_000_000
        limits.job_memory = 512*MIB
        try:
            self._check(self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            if self.evidence() != dict(flags=0x220c, active_limit=1,
                                       job_user_100ns=120*10_000_000, job_memory_bytes=512*MIB):
                raise OSError('job_limits_readback_mismatch')
        except BaseException:
            self.close()
            raise

    def evidence(self):
        limits = _Extended()
        self._check(self.api.QueryInformationJobObject(self.handle, 9, ctypes.byref(limits),
                                                       ctypes.sizeof(limits), None))
        return dict(flags=limits.basic.flags, active_limit=limits.basic.active_limit,
                    job_user_100ns=limits.basic.job_user, job_memory_bytes=limits.job_memory)


def runtime_readiness():
    """No caller boolean, sampled size or unauthenticated manifest can grant GO."""
    return dict(ready=False, blockers=[
        'HARD_ALL_ARTIFACT_QUOTA_UNAVAILABLE',
        'BOUNDED_SOCKET_INGRESS_UNAVAILABLE',
        'STRICT_TOTAL_USER_KERNEL_CPU_UNPROVEN',
        'PRODUCTION_RECORDER_SUPERVISION_UNVERIFIED',
        'CLEANUP_RUNTIME_PROVENANCE_UNVERIFIED'],
        supervisor_present=True,
        supervisor_memory_limit=128*MIB,
        runtime_evidence_verified=False, cleanup_proven=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description='CP6.3 offline implementation; live launch BLOCKED.')
    for flag in ('event-id', 'market-id', 'condition-id', 'up-token', 'down-token', 'start', 'end'):
        parser.add_argument('--'+flag, required=True)
    parser.add_argument('--run-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    from .cli import _utc_us
    try:
        WindowPin(args.event_id, args.market_id, args.condition_id, args.up_token, args.down_token,
                  _utc_us(args.start), _utc_us(args.end))
    except (ValueError, argparse.ArgumentTypeError) as exc:
        parser.error(str(exc))
    if os.name != 'nt':
        parser.error('windows_job_required')
    print(json.dumps(dict(verdict='CP6.3 BLOCKED', **runtime_readiness()), sort_keys=True))
    return 2
