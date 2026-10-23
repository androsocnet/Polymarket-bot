"""Single-trial public HTTP admission and Windows supervision. Never trading."""
import _thread
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4

from .windows_job import WindowsJob, in_supervisor_job

import httpx


class BudgetTransport(httpx.BaseTransport):
    """Count dispatches, not PublicGet callbacks (which also include refusals)."""

    def __init__(self, inner, *, event_id, market_id, journal, stop):
        if not all(isinstance(value, str) and re.fullmatch(r'[0-9]{1,256}', value)
                   for value in (event_id, market_id)):
            raise ValueError('exact_numeric_ids_required')
        self.inner, self.journal, self.stop = inner, journal, stop
        self.event_id, self.market_id = event_id, market_id
        self.sent, self.reason = 0, None
        self._lock = threading.Lock()
        self._requests = threading.Lock()

    def request_stop(self):
        # Commit shares lower-entry gate. Blocked I/O can delay this commit;
        # independent parent job termination never acquires this child lock.
        with self._lock:
            self.stop.set()

    def _write(self, event, **fields):
        self.journal.write(json.dumps(dict(event=event, mono_ns=time.monotonic_ns(), **fields))+'\n')
        self.journal.flush()

    def _allowed(self, request):
        url = request.url
        if (url.scheme != 'https' or url.port not in (None, 443) or url.userinfo or any(
                key.lower().replace('_', '-') in {'authorization', 'proxy-authorization', 'cookie',
                    'poly-api-key', 'poly-passphrase', 'x-api-key'} or 'signature' in key.lower()
                for key in request.headers)):
            return False
        path = url.path
        if url.host == 'gamma-api.polymarket.com':
            return (request.method == 'GET' and not url.query and path in
                    ('/events/'+self.event_id, '/markets/'+self.market_id))
        if url.host != 'clob.polymarket.com':
            return False
        if request.method == 'GET':
            if path == '/book':
                return (list(url.params.keys()) == ['token_id'] and
                        len(url.params.get_list('token_id')) == 1 and
                        re.fullmatch(r'[0-9]{1,128}', url.params['token_id']) is not None)
            return not url.query and (path == '/time' or
                re.fullmatch(r'/clob-markets/0x[0-9a-fA-F]{64}', path) is not None)
        if request.method != 'POST' or path != '/books' or url.query:
            return False
        try:
            rows = json.loads(request.content)
            return (type(rows) is list and len(rows) == 2 and all(type(row) is dict and
                    set(row) == {'token_id'} and type(row['token_id']) is str and
                    re.fullmatch(r'[0-9]{1,128}', row['token_id']) for row in rows) and
                    rows[0]['token_id'] != rows[1]['token_id'])
        except (ValueError, TypeError):
            return False

    def handle_request(self, request):
        # Existing AdmissionTransport runs PublicGet's stop/deadline guard first.
        # This innermost boundary runs once per actual transport dispatch.
        with self._requests:
            return self._handle_request(request)

    def _handle_request(self, request):
        reason = self.reason or ('guard_stop_requested' if self.stop.is_set() else
                     'public_request_refused' if not self._allowed(request) else
                     'http_budget_exhausted' if self.sent >= 60 else None)
        if reason:
            self.reason = reason
            try:
                self._write('denied', reason=reason, sent=self.sent)
            finally:
                raise KeyboardInterrupt(reason)
        try:
            # Intent before gate: STOP must be able to commit during flush.
            self._write('dispatch', number=self.sent+1, method=request.method,
                        url=str(request.url))
        except (OSError, ValueError):
            self.reason = 'guard_journal_failure'
            raise KeyboardInterrupt(self.reason) from None
        with self._lock:
            if self.stop.is_set():
                self.reason = 'guard_stop_requested'
                try:
                    self._write('dispatch_cancelled', number=self.sent+1, reason=self.reason)
                    self._write('denied', reason=self.reason, sent=self.sent)
                finally:
                    raise KeyboardInterrupt(self.reason)
            self.sent += 1
            try:
                # Hold gate through lower return: no check-then-call gap with
                # request_stop. Response body may still stream after release.
                response = self.inner.handle_request(request)
            except BaseException:
                self._write('transport_error', number=self.sent)
                raise
        try:
            self._write('response', number=self.sent, status=response.status_code)
        except (OSError, ValueError):
            response.close()
            self.reason = 'guard_journal_failure'
            raise KeyboardInterrupt(self.reason) from None
        return response

    def close(self):
        self.inner.close()


def supervise(command, evidence_dir, *, grace_seconds=90, kill_seconds=110, hard_seconds=120):
    """One launch only. Independent timers include child startup and cleanup."""
    if not 0 < grace_seconds < kill_seconds < hard_seconds <= 120:
        raise ValueError('invalid_watchdog_deadlines')
    started = time.monotonic()
    evidence_dir = Path(evidence_dir)
    forced, stopped = threading.Event(), threading.Event()
    process = job = None
    timers = []
    # Exit kills job through KILL_ON_JOB_CLOSE even if supervisor cleanup stalls.
    # Missing terminal summary + exit 124 means forced exit, unknown audit completeness.
    hard_timer = threading.Timer(max(0, started+hard_seconds-0.25-time.monotonic()), os._exit, args=(124,))
    hard_timer.daemon = True
    hard_timer.start()
    try:
        evidence_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        (evidence_dir/'deadline-policy.json').write_text(json.dumps(dict(
            hard_seconds=hard_seconds, forced_exit_code=124, missing_summary_means='audit_unknown')),
            encoding='utf8')
        job_name = 'Local\\CP5-'+uuid4().hex
        job = WindowsJob(job_name)
        environment = dict(os.environ, CP5_SUPERVISOR_JOB=job_name)
        if command[0] == sys.executable:
            # Windows venv shim spawns Python before assignment; launch Python
            # directly while retaining venv so delayed assignment contains it.
            command = [sys._base_executable, *command[1:]]
            environment['__PYVENV_LAUNCHER__'] = sys.executable
        with (evidence_dir/'child.log').open('xb') as output:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, env=environment,
                                       stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            # Child entry waits for GO. Assignment failure never releases it.
            job.assign(process)

            def graceful():
                stopped.set()
                try:
                    process.stdin.write(b'STOP\n')
                    process.stdin.flush()
                except (OSError, ValueError):
                    pass

            def hard_stop():
                forced.set()
                job.terminate()

            for deadline, callback in ((grace_seconds, graceful), (kill_seconds, hard_stop)):
                timer = threading.Timer(max(0, started+deadline-time.monotonic()), callback)
                timer.daemon = True
                timer.start()
                timers.append(timer)
            if time.monotonic() >= started+grace_seconds:
                raise TimeoutError('startup_deadline_exhausted')
            process.stdin.write(b'GO\n')
            process.stdin.flush()
            process.wait(timeout=max(0, started+hard_seconds-1-time.monotonic()))
            drain_until = min(time.monotonic()+0.1, started+hard_seconds-0.5)
            while job.active() and time.monotonic() < drain_until:
                time.sleep(0.005)
            if job.active():
                forced.set()
                job.terminate()
            while job.active() and time.monotonic() < started+hard_seconds-0.5:
                time.sleep(0.01)
            active = job.active()
            if active:
                raise TimeoutError('job_cleanup_deadline_exhausted')
        result = dict(launches=1, child_exit_code=process.returncode, forced=forced.is_set(),
                      stop_requested=stopped.is_set(), active_processes=active,
                      elapsed_seconds=time.monotonic()-started, actual_sends=None,
                      audit_completeness_proven=False,
                      reason='forced_termination' if forced.is_set() else
                             'watchdog_graceful_stop' if stopped.is_set() else 'child_exited')
        summary = evidence_dir/'child-summary.json'
        if not forced.is_set() and summary.exists():
            child = json.loads(summary.read_text(encoding='utf8'))
            result['child'] = child
            result['transport_dispatches'] = child['transport_dispatches']
            result['reason'] = child['reason'] or result['reason']
    finally:
        unwinding = sys.exc_info()[0] is not None
        for timer in timers:
            timer.cancel()
        # Terminate descendants even when root exits or supervision raises.
        try:
            if process is not None:
                if process.poll() is None:
                    job.terminate()
                    process.kill()  # Also covers failed assignment before barrier release.
                process.wait(timeout=max(.01, started+hard_seconds-time.monotonic()))
                process.stdin.close()
        finally:
            if job is not None:
                job.close()  # KILL_ON_JOB_CLOSE also protects against supervisor process loss.
            if unwinding:
                hard_timer.cancel()
    try:
        result['elapsed_seconds'] = time.monotonic()-started
        (evidence_dir/'supervisor.json').write_text(json.dumps(result, indent=2), encoding='utf8')
        return result
    finally:
        hard_timer.cancel()


def _child(db, event_id, market_id, evidence_dir, *, job_name=None):
    if sys.stdin.readline().strip() != 'GO':
        return 2
    if not in_supervisor_job(job_name or os.environ.get('CP5_SUPERVISOR_JOB')):
        return 2
    stop = threading.Event()

    def control():
        # EOF means supervisor lost too. Stop admission and interrupt recorder.
        sys.stdin.readline()
        guard.request_stop()
        _thread.interrupt_main()

    from .cli import main as recorder_main
    with (evidence_dir/'http.jsonl').open('x', encoding='utf8') as journal:
        guard = BudgetTransport(httpx.HTTPTransport(retries=0, trust_env=False),
                                event_id=event_id, market_id=market_id, journal=journal, stop=stop)
        code = 1
        try:
            threading.Thread(target=control, daemon=True).start()
            code = recorder_main(['record', '--mode', 'book-only', '--db', str(db),
                                 '--event-id', event_id, '--market-id', market_id,
                                 '--duration-seconds', '30'], transport=guard)
        except KeyboardInterrupt:
            code = 130
        finally:
            guard.close()
            reason = guard.reason or ('watchdog_stop_requested' if stop.is_set() else None)
            summary = dict(transport_dispatches=guard.sent, reason=reason, recorder_exit_code=code,
                           audit_completeness_proven=False)
            (evidence_dir/'child-summary.json').write_text(json.dumps(summary, indent=2), encoding='utf8')
    return code if code else (2 if reason else 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description='One guarded Windows BOOK_ONLY trial; independent review required.')
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--event-id', required=True)
    parser.add_argument('--market-id', required=True)
    parser.add_argument('--evidence-dir', required=True, type=Path)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.name != 'nt':
        parser.error('windows_job_required')
    if not all(re.fullmatch(r'[0-9]{1,256}', value) for value in (args.event_id, args.market_id)):
        parser.error('exact_numeric_ids_required')
    if args.db.exists():
        parser.error('selected_database_not_fresh')
    if args.child:
        return _child(args.db, args.event_id, args.market_id, args.evidence_dir)
    command = [sys.executable, '-B', str(Path(__file__).resolve().parents[2]/'guarded_record.py'),
               '--child', '--db', str(args.db.resolve()), '--event-id', args.event_id,
               '--market-id', args.market_id, '--evidence-dir', str(args.evidence_dir.resolve())]
    result = supervise(command, args.evidence_dir)
    print(json.dumps(result, sort_keys=True))
    return 124 if result['forced'] else result['child_exit_code']
