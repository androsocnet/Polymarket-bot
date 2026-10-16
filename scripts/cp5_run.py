import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Callable

import httpx


from polymarket_bot.recorder.service import Recorder


class CP5Recorder(Recorder):
    _audit_events: Any = ()

    def _timing(self, phase, **detail):
        if not hasattr(self, '_timing_buffer'):
            self._timing_buffer = []
        if phase == 'SELECTION':
            self._timing_identity = {key: detail[key] for key in ('market_id', 'slot_index', 'target_ns')}
        elif phase.startswith('CONTROL'):
            detail = dict(detail, operation='CONTROL')
        else:
            if ('target_ns' in detail and detail['target_ns'] !=
                    getattr(self, '_timing_identity', {}).get('target_ns')):
                self._timing_identity = {}
            detail = dict(getattr(self, '_timing_identity', {}), **detail)
        self._timing_buffer.append((wall_us(), dict(type='SCHEDULER_TIMING', version=1,
            diagnostic_only=True, time_domain='RUN_MONOTONIC', phase=phase,
            mono_ns=self.monotonic_ns(), **detail)))
        if phase == 'FINALIZATION_END':
            self._timing_identity = {}

    def _flush_timing(self):
        started = self.monotonic_ns()
        pending, self._timing_buffer = getattr(self, '_timing_buffer', []), []
        if pending:
            try:
                with self.store._transaction():
                    self.store._run(self.run_id)
                    for observed_wall, detail in pending:
                        self.store._insert('recorder_events', dict(run_id=self.run_id, market_id=None,
                            kind='DISCOVERY_GAP', started_us=observed_wall, ended_us=None,
                            detail_json=json.dumps(detail)))
                    self.store._insert('recorder_events', dict(run_id=self.run_id, market_id=None,
                        kind='DISCOVERY_GAP', started_us=wall_us(), ended_us=None,
                        detail_json=json.dumps(dict(type='SCHEDULER_TIMING', version=1,
                            diagnostic_only=True, time_domain='RUN_MONOTONIC', phase='DIAGNOSTIC_PERSISTENCE',
                            mono_ns=self.monotonic_ns(), start_mono_ns=started,
                            end_mono_ns=self.monotonic_ns(), event_count=len(pending),
                            excludes_commit=True))))
            except BaseException:
                self._timing_buffer = pending + self._timing_buffer
                raise

    def _flush_audit(self):
        persisted = getattr(self, '_audit_persisted_attempts', set())
        self._audit_persisted_attempts = persisted
        for event in tuple(getattr(self, '_audit_events', ())):
            if event['attempt'] not in persisted:
                self.store.record_event(self.run_id, 'DISCOVERY_GAP', started_us=event['started_wall_us'],
                    detail=dict(event, type='ACTUAL_SEND_ATTEMPT', version=2))
                persisted.add(event['attempt'])
        retained = {event['attempt'] for event in tuple(getattr(self, '_audit_events', ()))}
        persisted.intersection_update(retained)

    def _final_persistence(self):
        self._flush_audit()
        self._flush_timing()

    def _approximate_drain(self, **kwargs):
        result = super()._approximate_drain(**kwargs)
        self._flush_audit()
        return result

    def _housekeeping(self, target_ns, **kwargs):
        if len(getattr(self, '_timing_buffer', [])) >= 64:
            self._flush_timing()
        self._timing('HOUSEKEEPING_START', target_ns=target_ns)
        try:
            return super()._housekeeping(target_ns, **kwargs)
        finally:
            self._timing('HOUSEKEEPING_END', target_ns=target_ns)

    def _wait(self, deadline_ns):
        self._timing('WAIT_START', target_ns=deadline_ns)
        super()._wait(deadline_ns)
        self._timing('WAKE', target_ns=deadline_ns)

    def _control(self, operation):
        self._timing('CONTROL_START')
        try:
            return super()._control(operation)
        finally:
            self._timing('CONTROL_END')

    def _reconcile(self, deadline_ns):
        return None


_retained_runs = []


def wall_us():
    return time.time_ns() // 1000


class AuditStream(httpx.SyncByteStream):
    def __init__(self, inner, event, finish):
        self.inner, self.event, self.finish = inner, event, finish
        self.finished = False

    def __iter__(self):
        try:
            for chunk in self.inner:
                self.event['response_bytes'] += len(chunk)
                yield chunk
            self.event['complete'] = True
        except BaseException as exc:
            self.event['error'] = type(exc).__name__
            raise

    def close(self):
        try:
            self.inner.close()
        except BaseException as exc:
            self.event['error'] = type(exc).__name__
            raise
        finally:
            if not self.finished:
                self.finished = True
                self.event['stream_closed'] = True
                self.finish(self.event)


class AuditTransport(httpx.BaseTransport):
    def __init__(self, inner, emit):
        self.inner, self.emit = inner, emit
        self.attempt = 0
        self.print_events = False
        self.lock = threading.Lock()
        self.closed = False
        self.capture_identity: Callable[[httpx.Request], dict] | None = None

    def handle_request(self, request):
        url = request.url
        routes = {
            'api.exchange.coinbase.com': r'/products/BTC-USD/ticker',
            'api.kraken.com': r'/0/public/Ticker',
            'gamma-api.polymarket.com': r'/events|/events/slug/[A-Za-z0-9_-]+|/markets/[0-9]+',
            'clob.polymarket.com': r'/time|/book|/books|/clob-markets/0x[0-9a-fA-F]{64}',
        }
        method = 'POST' if url.host == 'clob.polymarket.com' and url.path == '/books' else 'GET'
        forbidden = any(any(word in key.lower().replace('_', '-') for word in
            ('authorization', 'cookie', 'api-key', 'apikey', 'signature', 'passphrase', 'secret', 'token'))
            for key in request.headers)
        keys = {key.lower() for key in url.params.keys()}
        allowed_keys = {'pair', 'assetVersion'} if url.host == 'api.kraken.com' else {'token_id'} if url.path == '/book' else {
            'active', 'closed', 'end_date_min', 'end_date_max', 'limit', 'offset', 'order', 'ascending'} if url.path == '/events' else set()
        if (url.scheme != 'https' or url.port not in (None, 443) or url.userinfo or url.fragment
                or url.host not in routes or re.fullmatch(routes.get(url.host, ''), url.path) is None
                or request.method != method or forbidden or not keys <= {key.lower() for key in allowed_keys}):
            raise ValueError('CP5 rejected unapproved or authenticated request')
        selectors = list(url.params.multi_items())
        if len(selectors) != len({key for key, value in selectors}):
            raise ValueError('CP5 duplicate query selector')
        params = dict(selectors)
        if method == 'GET' and request.content:
            raise ValueError('CP5 GET body forbidden')
        if url.path == '/book' and (set(params) != {'token_id'} or
                re.fullmatch(r'[0-9]{1,128}', params['token_id']) is None):
            raise ValueError('CP5 invalid public token selector')
        if url.host == 'api.kraken.com' and params != {'pair': 'XBTUSD', 'assetVersion': '1'}:
            raise ValueError('CP5 invalid public pair')
        if url.path == '/events':
            for key, value in selectors:
                pattern = r'true|false' if key in ('active', 'closed', 'ascending') else r'[0-9]{1,6}' if key in ('limit', 'offset') else r'[A-Za-z_]{1,64}' if key == 'order' else r'[0-9TZ:+.\-]{1,64}'
                if re.fullmatch(pattern, value) is None:
                    raise ValueError('CP5 invalid public event selector')
        if method == 'POST':
            try:
                body = json.loads(request.content)
            except (ValueError, UnicodeError) as exc:
                raise ValueError('CP5 invalid public books body') from exc
            if (not isinstance(body, list) or len(body) != 2 or any(not isinstance(item, dict)
                    or set(item) != {'token_id'} or not isinstance(item['token_id'], str)
                    or re.fullmatch(r'[0-9]{1,128}', item['token_id']) is None for item in body)
                    or body[0]['token_id'] == body[1]['token_id']):
                raise ValueError('CP5 invalid public books body')
        with self.lock:
            self.attempt += 1
            attempt = self.attempt
        event = dict(attempt=attempt, host=url.host, path=url.path, method=request.method,
            started_wall_us=wall_us(), started_mono_ns=time.monotonic_ns(), status=None,
            request_bytes=len(request.content), response_bytes=0, redirect=False,
            request_header_presence=sorted(request.headers.keys()), response_header_presence=[], error=None)
        capture = getattr(self, 'capture_identity', None)
        if capture is not None:
            event.update(capture(request))
        event.update(complete=False, stream_closed=False, set_cookie_present=False)
        try:
            guard = request.extensions.get('send_guard')
            if guard is not None:
                guard()
            response = self.inner.handle_request(request)
            event.update(status=response.status_code, redirect=300 <= response.status_code < 400,
                response_header_presence=sorted(response.headers.keys()),
                set_cookie_present='set-cookie' in response.headers)
            if 'set-cookie' in response.headers:
                del response.headers['set-cookie']
            if event['redirect']:
                response.close()
                raise ValueError('CP5 rejected redirect')
            if response.is_stream_consumed:
                response.stream = httpx.ByteStream(response.content)
                event['response_bytes'] = len(response.content)
                event['complete'] = True
                event['stream_closed'] = True
                self.finish(event)
            else:
                response.stream = AuditStream(response.stream, event, self.finish)
            return response
        except BaseException as exc:
            event['error'] = type(exc).__name__
            self.finish(event)
            raise

    def finish(self, event):
        event.update(ended_wall_us=wall_us(), ended_mono_ns=time.monotonic_ns())
        self.emit(event)
        if getattr(self, 'print_events', False):
            print(json.dumps({'actual_send': event}), flush=True)

    def close(self):
        self.inner.close()
        self.closed = True


def align(now=time.time, sleep=time.sleep):
    delay = (-now()) % 900
    if not 0 <= delay <= 900:
        raise ValueError('CP5 alignment wait exceeds bound')
    sleep(delay)
    return delay


def supervise(command, timeout):
    started = time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    killed = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        killed = True
        process.kill()
        stdout, stderr = process.communicate(timeout=15)
    return dict(returncode=process.returncode, watchdog_killed=killed,
        elapsed_seconds=time.monotonic() - started, stdout=stdout, stderr=stderr)


def phase_verdict(supervisor, evidence):
    if supervisor.get('watchdog_killed') or supervisor.get('returncode') != 0:
        return dict(verdict='FAIL', reason='SUPERVISOR_FAILURE')
    if supervisor.get('stderr'):
        return dict(verdict='FAIL', reason='UNEXPECTED_STDERR')
    if evidence.get('result', {}).get('reason') == 'opening_blocked':
        return dict(verdict='BLOCKED', reason='PREPARATION_UNAVAILABLE')
    valid = (evidence.get('result', {}).get('state') == 'STOPPED'
        and evidence.get('result', {}).get('exit_code') == 0
        and evidence.get('integrity') in ([('ok',)], [['ok']]) and not evidence.get('foreign_keys')
        and evidence.get('user_version') == 2 and evidence.get('writer_reacquired')
        and evidence.get('client_closed') and evidence.get('transport_closed')
        and not evidence.get('worker_active') and not evidence.get('remaining_threads')
        and evidence.get('intended_phase', {}).get('verdict') == 'PASS')
    return dict(verdict='PASS' if valid else 'FAIL', reason='INTENDED_PHASE_EVIDENCE')


def opening_plan(mode, *, now):
    import math
    if mode not in ('rollover', 'pilot'):
        raise ValueError('CP5 opening mode required')
    boundary = math.ceil((now+60)/900)*900
    start = boundary-60
    wait = start-now
    if not 0 <= wait <= 900:
        raise ValueError('CP5 opening alignment exceeds bound')
    return dict(mode=mode, boundary=boundary, start=start, wait=wait,
        seconds=70 if mode == 'rollover' else 1200,
        supervisor=1015 if mode == 'rollover' else 2145)


def child(path, seconds, *, mode=None, intended_boundary=None, wait_evidence=None):
    from polymarket_bot.market_data.http import PublicGet
    from polymarket_bot.recorder.free_reference import ApproximateReferenceSession, CoinbaseRestProvider, KrakenRestProvider
    from polymarket_bot.recorder.models import RunConfig
    from polymarket_bot.recorder.service import Recorder
    from polymarket_bot.recorder.store import RecorderStore
    if (path.exists() or path.name == 'cp5_smoke.sqlite3'
            or (mode is None and seconds not in (120, 1200))
            or (mode is not None and (mode not in ('rollover', 'pilot')
                or seconds != (70 if mode == 'rollover' else 1200)
                or type(intended_boundary) is not int or intended_boundary % 900 != 0))):
        raise ValueError('CP5 requires new bounded database')
    acquisition_start_ns = time.monotonic_ns()
    fixed_deadline_ns = acquisition_start_ns+seconds*1_000_000_000
    if mode is not None:
        assert isinstance(intended_boundary, int)
        if time.time() >= intended_boundary-22:
            raise ValueError('CP5 BLOCKED: intended preparation admission missed')
        fixed_deadline_ns = min(fixed_deadline_ns,
            acquisition_start_ns+round((intended_boundary+(10 if mode == 'rollover' else 1140)-time.time())*1e9))
    from collections import deque
    events = deque(maxlen=4096)
    transport = AuditTransport(httpx.HTTPTransport(retries=0, trust_env=False), events.append)
    transport.print_events = True
    client = httpx.Client(transport=transport, trust_env=False, auth=None, follow_redirects=False)
    store = recorder = result = None
    try:
        store = RecorderStore.open_approximate(path)
        public = PublicGet(client, monotonic=time.monotonic, monotonic_ns=time.monotonic_ns,
            sleep=time.sleep, utcnow=lambda: datetime.now(timezone.utc), max_attempts=2)
        providers = {name: provider(client, monotonic_ns=time.monotonic_ns, wall_us=wall_us)
            for name, provider in [('coinbase', CoinbaseRestProvider), ('kraken', KrakenRestProvider)]}
        session = ApproximateReferenceSession(providers=providers, monotonic_ns=time.monotonic_ns,
            wall_us=wall_us, sleep=time.sleep)
        recorder = CP5Recorder(store, public, monotonic_ns=time.monotonic_ns,
            wall_us=wall_us, sleep=time.sleep, approximate_reference=session)
        recorder._audit_events = events
        recorder.opening_wait_evidence = wait_evidence
        recorder.intended_boundary_us = intended_boundary*1_000_000 if isinstance(intended_boundary, int) else None
        if mode is not None:
            assert isinstance(intended_boundary, int)
            recorder.intended_end_us = (intended_boundary+(10 if mode == 'rollover' else 1140))*1_000_000
        def capture_identity(request):
            identity = dict(getattr(recorder, '_book_identity', {})) if request.url.path in ('/book', '/books') else {}
            if request.url.path == '/book' and recorder.revision is not None:
                token = request.url.params.get('token_id')
                identity['leg'] = 'UP' if token == recorder.revision.market.up.token_id else 'DOWN'
            if request.url.host in ('api.exchange.coinbase.com', 'api.kraken.com'):
                pending = getattr(recorder, '_research_pending', None)
                if pending is not None:
                    identity.update(pending[1], opportunity_id=pending[0])
                identity['provider'] = 'coinbase' if request.url.host == 'api.exchange.coinbase.com' else 'kraken'
            return dict(identity, run_id=recorder.run_id, version=2)
        transport.capture_identity = capture_identity

        result = recorder.run(RunConfig(mode='APPROXIMATE_REFERENCE'), should_stop=lambda: False,
            deadline_ns=fixed_deadline_ns)
    finally:
        active = recorder is not None and recorder.approximate_worker is not None and recorder.approximate_worker._thread is not None and recorder.approximate_worker._thread.is_alive()
        try:
            if active:
                _retained_runs.append((recorder, client, store))
            else:
                try:
                    client.close()
                finally:
                    if store is not None:
                        store.close()
        finally:
            print(json.dumps({'lifecycle': dict(worker_active=active,
                client_closed=bool(client.is_closed), transport_closed=transport.closed,
                store_closed=store is None or store._closed,
                remaining_threads=[thread.name for thread in threading.enumerate()
                    if thread is not threading.main_thread()])}), flush=True)
        if active:
            raise RuntimeError('CP5 worker still alive; owners retained until watchdog')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        integrity = connection.execute('PRAGMA integrity_check').fetchall()
        foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchall()
        version = connection.execute('PRAGMA user_version').fetchone()[0]
    reopened = RecorderStore.open_approximate(path)
    reopened.close()
    evidence = dict(result=asdict(result), sends=list(events), integrity=integrity,
        foreign_keys=foreign_keys, user_version=version, client_closed=client.is_closed,
        transport_closed=transport.closed, worker_active=active,
        remaining_threads=[thread.name for thread in threading.enumerate() if thread is not threading.main_thread()],
        writer_reacquired=True)
    if mode is not None:
        assert isinstance(intended_boundary, int)
        from cp5_analyze import analyze_db, intended_phase
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            markets = [dict(row) for row in connection.execute('SELECT * FROM markets')]
            slots = [dict(row) for row in connection.execute('SELECT * FROM sample_slots')]
            trace = [dict(json.loads(row['detail_json']), run_id=row['run_id'])
                for row in connection.execute('SELECT * FROM recorder_events')]
        evidence['intended_phase'] = intended_phase(analyze_db(path), markets, slots, trace,
            intended_boundary*1_000_000, mode)
    print(json.dumps(evidence))
    return evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--seconds', type=int, required=True)
    parser.add_argument('--mode', choices=('rollover', 'pilot'))
    parser.add_argument('--intended-boundary', type=int)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--aligned-child', action='store_true')
    args = parser.parse_args()
    if args.child or args.aligned_child:
        wait_evidence = None
        if args.aligned_child:
            wait_started = time.time()
            delay = args.intended_boundary-60-wait_started
            if not 0 <= delay <= 900:
                raise ValueError('CP5 BLOCKED: alignment window missed')
            time.sleep(delay)
            wait_evidence = dict(intended_boundary=args.intended_boundary,
                wait_started=wait_started, wait_ended=time.time(), wait_end_ns=time.monotonic_ns())
            print(json.dumps(wait_evidence), flush=True)
        child(args.db, args.seconds, mode=args.mode, intended_boundary=args.intended_boundary, wait_evidence=wait_evidence)
    elif args.mode is not None:
        plan = opening_plan(args.mode, now=time.time())
        if args.seconds != plan['seconds'] or args.db.exists():
            raise ValueError('CP5 fixed duration/new database required')
        print(json.dumps(plan), flush=True)
        supervised = supervise([sys.executable, __file__, '--aligned-child', '--db', str(args.db),
            '--seconds', str(args.seconds), '--mode', args.mode,
            '--intended-boundary', str(plan['boundary'])], plan['supervisor'])
        evidence = {}
        for line in str(supervised['stdout']).splitlines():
            try:
                decoded = json.loads(line)
            except ValueError:
                continue
            if isinstance(decoded, dict) and 'result' in decoded:
                evidence = decoded
        decision = phase_verdict(supervised, evidence)
        print(json.dumps(dict(supervisor=supervised, **decision)))
        sys.exit(0 if decision['verdict'] == 'PASS' else 2 if decision['verdict'] == 'BLOCKED' else 1)
    else:
        if args.seconds not in (120, 1200):
            raise ValueError('CP5 ordinary duration rejected')
        print(json.dumps(supervise([sys.executable, __file__, '--child', '--db', str(args.db),
            '--seconds', str(args.seconds)], args.seconds + 45)))
