"""CP6.3 offline guard checks. No public runner launch or network."""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

import httpx
from unittest.mock import patch

from tests.recorder_support import WINDOW_START_US
from tests.support import CONDITION, TOKEN_UP, TOKEN_DOWN


def module():
    return importlib.import_module('polymarket_bot.recorder.full_window_guard')


def pin():
    return module().WindowPin('2001', '1001', CONDITION, TOKEN_UP, TOKEN_DOWN,
                              WINDOW_START_US, WINDOW_START_US+900_000_000)


class PolicyTests(unittest.TestCase):
    def test_exact_identity_and_450_targets(self):
        p = pin()
        self.assertEqual(len(p.targets), 450)
        self.assertEqual(p.targets[0], WINDOW_START_US)
        self.assertEqual(p.targets[-1], p.end_us-2_000_000)
        for changes in ({'event_id': None}, {'market_id': '../1001'}, {'condition_id': 'bad'},
                        {'up_token': 1}, {'down_token': TOKEN_UP}, {'start_us': True},
                        {'end_us': p.end_us+1}, {'start_us': -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(p, **changes)

    def test_launch_close_contract_and_deadlines_never_extend(self):
        p = pin()
        d = module().Deadlines(p, launch_ns=10_000_000_000,
                               launch_wall_us=p.start_us-30_000_000)
        self.assertEqual((d.stop_ns, d.kill_ns, d.hard_ns),
                         (940_000_000_000, 950_000_000_000, 960_000_000_000))
        d.tighten_close(939_000_000_000)
        self.assertEqual(d.stop_ns, 939_000_000_000)
        d.tighten_close(999_000_000_000)
        self.assertEqual(d.stop_ns, 939_000_000_000)
        self.assertEqual(d.kill_ns-d.stop_ns, 10_000_000_000)
        self.assertEqual(d.hard_ns-d.kill_ns, 10_000_000_000)
        for wall in (p.start_us-30_000_001, p.start_us+1):
            with self.assertRaises(ValueError):
                module().Deadlines(p, launch_ns=0, launch_wall_us=wall)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def journal(self):
        j = module().DurableJournal(self.root/'http.jsonl')
        self.stack.callback(j.close)
        return j

    def test_durable_bounded_chain_and_corruption_rejected(self):
        j = self.journal()
        with patch.object(os, 'fsync', wraps=os.fsync) as sync:
            j.write('dispatch', number=1)
            j.write('response', number=1, bytes=12)
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(len(module().read_journal(j.path)), 2)
        data = j.path.read_bytes()
        j.close()
        j.path.write_bytes(data.replace(b'"bytes":12', b'"bytes":13'))
        with self.assertRaises(ValueError):
            module().read_journal(j.path)

    def test_noninteger_sequence_and_duplicate_keys_rejected(self):
        j = self.journal(); j.write('one'); j.close()
        row = json.loads(j.path.read_bytes())
        row.pop('digest'); row['sequence'] = True
        row['digest'] = hashlib.sha256(module()._encoded(row)).hexdigest()
        j.path.write_bytes(module()._encoded(row)+b'\n')
        with self.assertRaises(ValueError):
            module().read_journal(j.path)
        row['sequence'] = 1; row.pop('digest')
        row['digest'] = hashlib.sha256(module()._encoded(row)).hexdigest()
        data = module()._encoded(row)
        j.path.write_bytes(data.replace(b'"sequence":1', b'"sequence":2,"sequence":1')+b'\n')
        with self.assertRaises(ValueError):
            module().read_journal(j.path)

    def test_exact_limit_then_failure_latches_without_more_writes(self):
        j = self.journal()
        j.write('one')
        size = j.path.stat().st_size
        with patch.object(module(), 'JOURNAL_LIMIT', size):
            with self.assertRaises(OSError):
                j.write('two')
        self.assertEqual(j.path.stat().st_size, size)
        with self.assertRaises(OSError):
            j.write('three')

    def test_fsync_short_write_and_missing_disk_fail_closed(self):
        for fault in ('fsync', 'short', 'space'):
            with self.subTest(fault=fault):
                path = self.root/(fault+'.jsonl')
                j = module().DurableJournal(path)
                self.stack.callback(j.close)
                if fault == 'fsync':
                    target = patch.object(os, 'fsync', side_effect=OSError('disk full'))
                elif fault == 'short':
                    target = patch.object(os, 'write', return_value=1)
                else:
                    target = patch.object(module().shutil, 'disk_usage', side_effect=OSError('no volume'))
                with target, self.assertRaises(OSError):
                    j.write('dispatch')
                with self.assertRaises(OSError):
                    j.write('again')

    def test_truncated_reordered_and_oversized_journal_rejected(self):
        j = self.journal()
        j.write('one'); j.write('two'); j.close()
        data = j.path.read_bytes()
        for mutation in (data[:-1], b''.join(reversed(data.splitlines(keepends=True))), b'{}\n'):
            j.path.write_bytes(mutation)
            with self.assertRaises(ValueError):
                module().read_journal(j.path)
        j.path.write_bytes(data)
        with patch.object(module(), 'JOURNAL_LIMIT', len(data)-1), self.assertRaises(ValueError):
            module().read_journal(j.path)


class StorageClosureTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def test_combined_log_journal_budget_charges_before_write_and_latches(self):
        g = module(); budget = g.ArtifactBudget(capture_limit=32, audit_limit=5)
        log = g.BoundedLog(self.root/'child.log', budget); self.stack.callback(log.close)
        log.write(b'abc'); budget.claim(2, audit=True)
        self.assertEqual((budget.capture_bytes, budget.audit_bytes), (5, 5))
        with self.assertRaises(OSError):
            log.write(b'd')
        self.assertEqual((self.root/'child.log').read_bytes(), b'abc')
        with self.assertRaises(OSError):
            budget.claim(0)
        self.assertFalse(budget.hard_all_artifact_quota)

    def test_journal_and_log_share_one_budget_and_failure_prevents_more_writes(self):
        g = module(); budget = g.ArtifactBudget(capture_limit=1024, audit_limit=1024)
        j = g.DurableJournal(self.root/'journal', budget=budget); self.stack.callback(j.close)
        log = g.BoundedLog(self.root/'log', budget); self.stack.callback(log.close)
        j.write('one'); size = j.path.stat().st_size
        self.assertEqual(budget.audit_bytes, size)
        log.write(b'log')
        self.assertEqual(budget.audit_bytes, size+3)
        with patch.object(os, 'write', return_value=1), self.assertRaises(OSError):
            log.write(b'bad')
        self.assertTrue(budget.failed)
        before = j.path.read_bytes()
        with self.assertRaises(OSError):
            j.write('two')
        self.assertEqual(j.path.read_bytes(), before)

    def test_fixed_terminal_reserve_survives_capture_exhaustion_and_detects_torn_write(self):
        g = module()
        with patch.object(g, 'TERMINAL_RESERVE', 4096):
            terminal = g.TerminalReserve(self.root/'terminal'); self.stack.callback(terminal.close)
            self.assertEqual(terminal.path.stat().st_size, 4096)
            budget = g.ArtifactBudget(capture_limit=1, audit_limit=1)
            with self.assertRaises(OSError):
                budget.claim(2)
            with self.assertRaises(ValueError):
                g.read_terminal(terminal.path)
            terminal.commit({'outcome': 'disk_full', 'runtime_evidence_verified': False})
            self.assertEqual(g.read_terminal(terminal.path)['outcome'], 'disk_full')
            self.assertEqual(terminal.path.stat().st_size, 4096)
            with self.assertRaises(OSError):
                terminal.commit({'outcome': 'overwrite'})
            terminal.close()
            data = bytearray(terminal.path.read_bytes()); data[48] ^= 1
            terminal.path.write_bytes(data)
            with self.assertRaises(ValueError):
                g.read_terminal(terminal.path)

    def test_reserve_disk_full_and_interrupted_commit_never_validate(self):
        g = module()
        with patch.object(g, 'TERMINAL_RESERVE', 4096):
            with patch.object(os, 'write', side_effect=OSError('disk full')), self.assertRaises(OSError):
                g.TerminalReserve(self.root/'full')
            terminal = g.TerminalReserve(self.root/'torn'); self.stack.callback(terminal.close)
            with patch.object(os, 'fsync', side_effect=OSError('interrupted')), self.assertRaises(OSError):
                terminal.commit({'outcome': 'unknown'})
            with self.assertRaises(ValueError):
                g.read_terminal(terminal.path)
            with self.assertRaises(OSError):
                terminal.commit({'outcome': 'retry forbidden'})

    def test_storage_contract_constants_and_invalid_allocations(self):
        g = module()
        self.assertEqual((g.ARTIFACT_LIMIT, g.CAPTURE_LIMIT, g.TERMINAL_RESERVE, g.AUDIT_LIMIT),
                         (1024*g.MIB, 960*g.MIB, 64*g.MIB, 16*g.MIB))
        for limits in ((True, 1), (0, 1), (g.CAPTURE_LIMIT+1, 1), (1, 2)):
            with self.assertRaises(ValueError):
                g.ArtifactBudget(capture_limit=limits[0], audit_limit=limits[1])
        budget = g.ArtifactBudget(capture_limit=2, audit_limit=2)
        for amount in (-1, True, 1.0):
            with self.assertRaises(ValueError):
                budget.claim(amount)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.now = 0
        self.calls = []

    def guard(self, handler=None):
        g = module()
        j = g.DurableJournal(self.root/(str(len(list(self.root.iterdir())))+'.jsonl'))
        self.stack.callback(j.close)
        def default(request):
            self.calls.append(request.url.path)
            return httpx.Response(200, stream=httpx.ByteStream(b'ok'))
        guard = g.WindowTransport(httpx.MockTransport(handler or default), pin=pin(), journal=j,
                                  monotonic_ns=lambda: self.now)
        self.stack.callback(guard.close)
        return guard

    def send(self, guard, *, book=False, consume=True):
        r = httpx.Request('POST' if book else 'GET', 'https://clob.polymarket.com/'+('books' if book else 'time'),
                         json=[{'token_id': TOKEN_UP}, {'token_id': TOKEN_DOWN}] if book else None)
        response = guard.handle_request(r)
        if consume:
            try:
                response.read()
            finally:
                response.close()
        return response

    def test_exact_dispatch_control_book_and_total_boundaries(self):
        for kind, count in (('control', 300), ('book', 2700), ('total', 3000)):
            with self.subTest(kind=kind):
                guard = self.guard()
                # Budget counters represent completed lower entries, not refusals.
                guard.sent = 2999 if kind == 'total' else count-1
                guard.controls = 299 if kind == 'control' else 0
                guard.books = 2699 if kind == 'book' else 0
                with patch.object(guard, '_book_check'):
                    self.send(guard, book=kind == 'book')
                    before = len(self.calls)
                    with self.assertRaises(KeyboardInterrupt):
                        self.send(guard, book=kind == 'book')
                self.assertEqual(len(self.calls), before)
                self.assertEqual(guard.sent, 3000 if kind == 'total' else count)

    def test_exact_selectors_and_unbound_books_refused(self):
        guard = self.guard()
        forbidden = [httpx.Request('GET', 'https://clob.polymarket.com/book?token_id=9'),
                     httpx.Request('GET', 'https://clob.polymarket.com/clob-markets/0x'+'ff'*32),
                     httpx.Request('GET', 'https://gamma-api.polymarket.com/events'),
                     httpx.Request('GET', 'https://data-api.polymarket.com/v2/resolutions'),
                     httpx.Request('GET', 'https://clob.polymarket.com/time', headers={'Cookie': 'secret'}),
                     httpx.Request('POST', 'https://clob.polymarket.com/order')]
        for request in forbidden:
            with self.subTest(url=str(request.url)), self.assertRaises(KeyboardInterrupt):
                self.guard().handle_request(request)
        with self.assertRaises(KeyboardInterrupt):
            self.send(guard, book=True)
        self.assertEqual(self.calls, [])

    def test_monotonic_rollback_and_transport_failure_stop(self):
        guard = self.guard()
        self.now = 100
        self.send(guard)
        self.now = 99
        with self.assertRaises(KeyboardInterrupt):
            self.send(guard)
        self.assertEqual(guard.sent, 1)
        self.assertEqual(guard.reason, 'monotonic_discontinuity')
        def broken(request):
            raise httpx.ConnectError('offline failure')
        guard = self.guard(broken)
        with self.assertRaises(httpx.ConnectError):
            self.send(guard)
        self.assertEqual(guard.sent, 1)
        self.assertTrue(guard.stopped)

    def test_partial_failed_stream_counts_ingress_and_latches_stop(self):
        class Broken(httpx.SyncByteStream):
            def __iter__(self):
                yield b'123'
                raise httpx.ReadError('broken')
        guard = self.guard(lambda r: httpx.Response(503, stream=Broken()))
        with self.assertRaises(httpx.ReadError):
            self.send(guard)
        self.assertEqual((guard.sent, guard.ingress), (1, 3))
        self.assertTrue(guard.stopped)
        rows = module().read_journal(guard.journal.path)
        self.assertEqual([r['event'] for r in rows[-2:]], ['stream_error', 'body_incomplete'])
        self.assertEqual(rows[-2]['bytes'], 3)

    def test_body_and_aggregate_exact_boundaries_and_overshoot_measured(self):
        for field, limit in (('BODY_LIMIT', 10), ('INGRESS_LIMIT', 10)):
            for length in (10, 11):
                with self.subTest(field=field, length=length):
                    guard = self.guard(lambda r: httpx.Response(200, stream=httpx.ByteStream(b'x'*length)))
                    with patch.object(module(), field, limit):
                        if length == 10:
                            self.send(guard)
                        else:
                            with self.assertRaises(KeyboardInterrupt):
                                self.send(guard)
                    self.assertEqual(guard.ingress, length)
                    self.assertEqual(guard.stopped, length > 10)
                    self.assertFalse(guard.hard_ingress_enforced)

    def test_missing_counter_or_journal_failure_never_enters_lower(self):
        for fault in ('counter', 'journal'):
            guard = self.guard()
            if fault == 'counter':
                guard.ingress = None
            else:
                guard.journal.close()
            with self.assertRaises(KeyboardInterrupt):
                self.send(guard)
        self.assertEqual(self.calls, [])

    def test_whole_body_serialization_and_early_close_unknown(self):
        guard = self.guard()
        response = self.send(guard, consume=False)
        with self.assertRaises(KeyboardInterrupt):
            self.send(guard)
        response.close()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(guard.stopped)
        self.assertEqual(module().read_journal(guard.journal.path)[-1]['event'], 'body_incomplete')

    def test_real_public_retry_and_fallback_lower_entries_all_count(self):
        from tests.recorder_support import BookFixture, down_book
        from tests.support import make_book
        from polymarket_bot.market_data.http import PublicGet
        f = BookFixture(status=404, singles={TOKEN_UP: (200, make_book()), TOKEN_DOWN: (200, down_book())})
        self.addCleanup(f.close)
        attempts = []
        def respond(request):
            attempts.append(request.url.path)
            if len(attempts) == 1:
                return httpx.Response(503, stream=httpx.ByteStream(b'busy'))
            return f._respond(request)
        guard = self.guard(respond)
        with httpx.Client(transport=guard, trust_env=False) as client:
            f.public = PublicGet(client, monotonic=f.clock.monotonic, sleep=f.clock.sleep,
                                 utcnow=f.clock.utcnow, max_attempts=2)
            with patch.object(guard, '_book_check'):
                result = f.collect(deadline=10)
        self.assertEqual(attempts, ['/books', '/books', '/book', '/book'])
        self.assertEqual((guard.sent, guard.books, guard.controls), (4, 4, 0))
        self.assertEqual([a.http_status for a in result.attempts], [503, 404, 200, 200])

    def test_stop_during_intent_cancels_without_lower_entry(self):
        guard = self.guard()
        original = guard.journal.write
        def write(event, **fields):
            original(event, **fields)
            if event == 'dispatch':
                guard.request_stop()
        with patch.object(guard.journal, 'write', write), self.assertRaises(KeyboardInterrupt):
            self.send(guard)
        self.assertEqual(guard.sent, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(module().read_journal(guard.journal.path)[-1]['event'], 'dispatch_cancelled')

    def test_stop_cannot_commit_inside_lower_entry_then_blocks_next(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def lower(request):
            entered.set()
            assert release.wait(2)
            return httpx.Response(200, stream=httpx.ByteStream(b'ok'))
        guard = self.guard(lower)
        def send():
            try:
                self.send(guard)
            except BaseException as exc:
                errors.append(type(exc).__name__)
        sender = threading.Thread(target=send); sender.start()
        self.assertTrue(entered.wait(2))
        stopper = threading.Thread(target=lambda: (guard.request_stop(), done.set())); stopper.start()
        try:
            self.assertFalse(done.wait(.05))
        finally:
            release.set(); sender.join(2); stopper.join(2)
        self.assertFalse(sender.is_alive() or stopper.is_alive())
        self.assertTrue(done.is_set())
        with self.assertRaises(KeyboardInterrupt):
            self.send(guard)
        self.assertEqual(guard.sent, 1)
        self.assertTrue(set(errors) <= {'KeyboardInterrupt'})


class RecorderIntegrationTests(unittest.TestCase):
    def fixture(self, **kwargs):
        from tests.full_window_support import FullWindowFixture
        f = FullWindowFixture(**kwargs); self.addCleanup(f.close)
        j = module().DurableJournal(f.db.path.parent/'http.jsonl'); self.addCleanup(j.close)
        guard = module().WindowTransport(httpx.MockTransport(f.respond), pin=pin(), journal=j,
                                         monotonic_ns=f.monotonic_ns)
        f.client._transport.inner = guard
        from polymarket_bot.recorder.service import Recorder
        recorder = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns,
                            wall_us=f.wall_us, sleep=f.sleep)
        guard.bind(recorder)
        return f, guard, recorder

    def run_recorder(self, recorder):
        from polymarket_bot.recorder.models import RunConfig
        return recorder.run(RunConfig(selected_event_id='2001', selected_market_id='1001'),
                            should_stop=lambda: False)

    def test_real450_guarded_targets_assessor_and_replay_unchanged(self):
        from polymarket_bot.full_window import assess_recording
        from polymarket_bot.strategy import load_strategy_recording, run_strategy
        f, guard, recorder = self.fixture()
        result = self.run_recorder(recorder)
        self.assertEqual((result.state, result.reason), ('STOPPED', 'SELECTED_MARKET_EXPIRED'))
        self.assertEqual((guard.sent, guard.books, guard.controls), (512, 450, 62))
        rows = module().read_journal(guard.journal.path)
        dispatches = [(r['method'], r['path'], r['mono_ns']) for r in rows if r['event']=='dispatch']
        assessment = assess_recording(f.db.path, result.run_id, '1001', dispatches=dispatches)
        self.assertEqual((assessment['status'], assessment['valid_pairs']), ('PROVISIONAL', 450))
        self.assertFalse(assessment['runtime_evidence_verified'] or assessment['cleanup_proven'])
        self.assertEqual(guard.ingress, assessment['ingress_bytes'])
        digest = hashlib.sha256(f.db.path.read_bytes()).hexdigest()
        replay = run_strategy(load_strategy_recording(f.db.path, result.run_id, '1001'))
        self.assertEqual(len(replay['decisions']), 450)
        self.assertIsNone(replay['execution']['settlement'])
        self.assertEqual(digest, hashlib.sha256(f.db.path.read_bytes()).hexdigest())
        self.assertTrue(all(WINDOW_START_US <= r['started_server_us'] < pin().end_us
                            for r in f.rows("SELECT * FROM request_attempts WHERE operation='BOOK_BATCH'")))

    def test_clock_discontinuity_stops_without_recovery_suffix(self):
        f, guard, recorder = self.fixture()
        original = f.respond
        def shifted(request):
            response = original(request)
            if request.url.path == '/time' and f.wall_us() >= WINDOW_START_US+60_000_000:
                response.close()
                return httpx.Response(200, stream=httpx.ByteStream(str(f.wall_us()//1_000_000+10).encode()))
            return response
        guard.inner = httpx.MockTransport(shifted)
        result = self.run_recorder(recorder)
        self.assertTrue(guard.stopped)
        self.assertEqual(guard.reason, 'clock_discontinuity')
        self.assertLess(guard.books, 100)
        self.assertNotEqual(result.exit_code, 0)

    def test_metadata_faults_identity_and_selected_expiry_fail_closed(self):
        for fault in ('metadata_failure', 'identity_conflict', 'closed'):
            with self.subTest(fault=fault):
                f, guard, recorder = self.fixture(faults={30: fault})
                result = self.run_recorder(recorder)
                self.assertNotEqual(result.exit_code, 0)
                self.assertLess(guard.books, 100)

    def test_expiry_at_dispatch_intent_refuses_lower_book(self):
        f, guard, recorder = self.fixture()
        original = guard.journal.write
        def write(event, **fields):
            original(event, **fields)
            if event == 'dispatch' and fields['path']=='/books':
                f.advance(901)
        with patch.object(guard.journal, 'write', write):
            result = self.run_recorder(recorder)
        self.assertEqual(guard.books, 0)
        self.assertTrue(guard.stopped)
        self.assertNotEqual(result.exit_code, 0)


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object API required')
class WindowsContainmentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def child(self, body, name):
        import subprocess
        import sys
        root = str(Path(__file__).resolve().parents[1]/'src')
        prefix = ('import socket,sys,os,time,subprocess\n'
                  'def denied(*a,**k): raise AssertionError("network denied")\n'
                  'socket.getaddrinfo=socket.create_connection=denied\n'
                  'socket.socket.connect=socket.socket.connect_ex=denied\n'
                  'sys.path.insert(0,'+repr(root)+')\n'
                  'from polymarket_bot.recorder.windows_job import in_supervisor_job\n'
                  'assert sys.stdin.readline().strip()=="PROVE"\n'
                  'if not in_supervisor_job('+repr(name)+'): raise SystemExit(2)\n'
                  'print("MEMBER",flush=True)\n'
                  'if sys.stdin.readline().strip()!="GO": raise SystemExit(2)\n')
        script = self.root/'child.py'; script.write_text(prefix+body)
        environment = dict(os.environ, __PYVENV_LAUNCHER__=sys.executable)
        process = subprocess.Popen([sys._base_executable, '-B', str(script)], env=environment,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        self.stack.callback(cleanup)
        return process

    def test_native_limits_readback_and_membership_before_go(self):
        name = 'Local\\CP63-test-'+str(os.getpid())
        job = module().FullWindowJob(name); self.stack.callback(job.close)
        limits = job.evidence()
        self.assertEqual(limits['flags'], 0x220c)
        self.assertEqual(limits['job_memory_bytes'], 512*1024*1024)
        self.assertEqual(limits['job_user_100ns'], 120*10_000_000)
        self.assertEqual(limits['active_limit'], 1)
        child = self.child('print("RAN",flush=True)\n', name)
        job.assign(child)
        child.stdin.write(b'PROVE\n'); child.stdin.flush()
        self.assertEqual(child.stdout.readline(), b'MEMBER\r\n')
        self.assertIsNone(child.poll())
        child.stdin.write(b'GO\n'); child.stdin.flush()
        self.assertEqual(child.wait(timeout=5), 0, child.stderr.read())
        self.assertEqual(job.active(), 0)

    def test_no_descendant_and_forced_termination_drains(self):
        name = 'Local\\CP63-descendant-'+str(os.getpid())
        job = module().FullWindowJob(name); self.stack.callback(job.close)
        body = ('try:\n subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n'
                'except OSError:\n print("DENIED",flush=True)\n'
                'else:\n print("UNSAFE",flush=True)\n'
                'while True: time.sleep(.01)\n')
        child = self.child(body, name); job.assign(child)
        child.stdin.write(b'PROVE\n'); child.stdin.flush()
        self.assertEqual(child.stdout.readline().strip(), b'MEMBER')
        child.stdin.write(b'GO\n'); child.stdin.flush()
        self.assertEqual(child.stdout.readline().strip(), b'DENIED')
        self.assertEqual(job.active(), 1)
        job.terminate(); child.wait(timeout=5)
        self.assertEqual(job.active(), 0)

    def test_wrong_membership_never_releases_go(self):
        name = 'Local\\CP63-unassigned-'+str(os.getpid())
        job = module().FullWindowJob(name); self.stack.callback(job.close)
        child = self.child('raise AssertionError("GO released")\n', name)
        child.stdin.write(b'PROVE\nGO\n'); child.stdin.flush()
        self.assertEqual(child.wait(timeout=5), 2, child.stderr.read())
        self.assertEqual(job.active(), 0)

    def test_kill_on_close_reaps_waiting_child_and_handles_close(self):
        name = 'Local\\CP63-close-'+str(os.getpid())
        job = module().FullWindowJob(name); self.stack.callback(job.close)
        child = self.child('while True: time.sleep(.01)\n', name); job.assign(child)
        child.stdin.write(b'PROVE\n'); child.stdin.flush()
        self.assertEqual(child.stdout.readline().strip(), b'MEMBER')
        job.close(); child.wait(timeout=5)
        self.assertIsNotNone(child.returncode)


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object API required')
class SupervisorClosureTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def run_parent(self, body, *, fault=None):
        import subprocess
        import sys
        root = str(Path(__file__).resolve().parents[1]/'src')
        child = self.root/'worker.py'
        child.write_text('import sys,time,os,socket,subprocess\n'
                         'sys.path.insert(0,'+repr(root)+')\n'
                         'from polymarket_bot.recorder.windows_job import in_supervisor_job\n'
                         'assert sys.stdin.readline().strip()=="PROVE"\n'
                         'assert in_supervisor_job(os.environ["CP63_SUPERVISOR_JOB"])\n'
                         'print("MEMBER",flush=True)\n'
                         'assert sys.stdin.readline().strip()=="GO"\n'+body)
        parent = self.root/'parent.py'
        script = ('import sys,time,os,socket\nfrom types import SimpleNamespace\n'
                  'sys.path.insert(0,'+repr(root)+')\n'
                  'def denied(*a,**k): raise AssertionError("network denied")\n'
                  'socket.getaddrinfo=socket.create_connection=denied\n'
                  'socket.socket.connect=socket.socket.connect_ex=denied\n'
                  'from polymarket_bot.recorder import full_window_supervisor as s\n'
                  'now=time.monotonic_ns()\n'
                  'd=SimpleNamespace(stop_ns=now+3_000_000_000,kill_ns=now+4_000_000_000,hard_ns=now+7_000_000_000)\n')
        if fault == 'terminal_hang':
            script += ('def commit(self,value):\n import ctypes\n ctypes.PyDLL("kernel32").Sleep(10000)\n'
                       's.TerminalReserve.commit=commit\n')
        if fault in ('hang', 'crash'):
            script += ('old=s.FullWindowJob.assign\n'
                       'def assign(self,p):\n old(self,p)\n'
                       ' from pathlib import Path\n'
                       ' Path('+repr(str(self.root/'pid'))+').write_text(str(p.pid))\n')
            if fault == 'hang':
                script += ' import ctypes\n ctypes.PyDLL("kernel32").Sleep(10000)\n'
            else:
                script += ' os._exit(77)\n'
            script += 's.FullWindowJob.assign=assign\n'
        if fault == 'memory':
            script += ('old=s._self_memory_budget\n'
                       'def memory():\n'
                       ' job=old()\n blocks=[]\n'
                       ' try:\n  while True: blocks.append(bytearray(1024*1024))\n'
                       ' except MemoryError:\n  payload=len(blocks)*1024*1024\n  blocks.clear()\n'
                       '  globals()["memory_result"]={"memory_refused":True,"payload_bytes":payload}\n'
                       ' return job\n'
                       's._self_memory_budget=memory\n')
        script += ('result=s.supervise([sys.executable,"-B",'+repr(str(child))+'], '+
                   repr(str(self.root/'run'))+', deadlines=d)\n'
                   'result.update(globals().get("memory_result",{}))\n'
                   'print(__import__("json").dumps(result),flush=True)\n')
        parent.write_text(script)
        process = subprocess.Popen([sys._base_executable, '-B', str(parent)],
                                   env=dict(os.environ, __PYVENV_LAUNCHER__=sys.executable),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, errors = process.communicate(timeout=12)
        except BaseException:
            process.kill(); process.communicate(); raise
        finally:
            process.stdout.close(); process.stderr.close()
        return process.returncode, output, errors

    def test_actual_stop_timer_terminal_and_closed_logs(self):
        code, output, errors = self.run_parent('assert sys.stdin.readline().strip()=="STOP"\nprint("CLOSED",flush=True)\n')
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertTrue(result['stop_requested'])
        self.assertFalse(result['forced'])
        self.assertEqual(result['active_processes'], 0)
        self.assertFalse(result['runtime_evidence_verified'])
        terminal = module().read_terminal(self.root/'run'/'terminal.bin')
        self.assertEqual(terminal['child_exit_code'], 0)
        self.assertTrue(terminal['membership_before_go'])
        self.assertIn(b'CLOSED', (self.root/'run'/'child.log').read_bytes())
        self.assertIsNone(terminal['watchdog_exit_code'])
        self.assertEqual(result['watchdog_exit_code'], 0)
        self.assertTrue(terminal['exclusive_reopen'])

    def test_actual_kill_timer_for_stubborn_worker(self):
        code, output, errors = self.run_parent('while True: time.sleep(.01)\n')
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertTrue(result['forced'])
        self.assertEqual(result['active_processes'], 0)
        self.assertLess(result['elapsed_seconds'], 7)
        self.assertEqual(result['cpu_policy'], 'sampled_user_plus_kernel_native_user_limit')

    def test_independent_watchdog_survives_supervisor_gil_hang(self):
        code, output, errors = self.run_parent('while True: time.sleep(.01)\n', fault='hang')
        self.assertEqual(code, 124, errors)
        self.assertEqual(output, b'')
        with self.assertRaises(ValueError):
            module().read_terminal(self.root/'run'/'terminal.bin')
        s = importlib.import_module('polymarket_bot.recorder.full_window_supervisor')
        self.assertFalse(s.process_alive(int((self.root/'pid').read_text())))

    def test_watchdog_remains_armed_through_terminal_commit(self):
        code, output, errors = self.run_parent('print("DONE",flush=True)\n', fault='terminal_hang')
        self.assertEqual(code, 124, errors)
        self.assertEqual(output, b'')
        with self.assertRaises(ValueError):
            module().read_terminal(self.root/'run'/'terminal.bin')

    def test_parent_crash_watchdog_kills_waiting_worker(self):
        code, output, errors = self.run_parent('raise AssertionError("GO after parent crash")\n', fault='crash')
        self.assertEqual(code, 77, errors)
        s = importlib.import_module('polymarket_bot.recorder.full_window_supervisor')
        # Watchdog completion is asynchronous. Wait on child process handle, not sleep/retry cleanup.
        self.assertTrue(s.wait_process_exit(int((self.root/'pid').read_text()), 5000))
        with self.assertRaises(ValueError):
            module().read_terminal(self.root/'run'/'terminal.bin')

    def test_rapid_lifecycle_and_parent_owned_handle_after_job_zero(self):
        import ctypes
        s = importlib.import_module('polymarket_bot.recorder.full_window_supervisor')
        root = self.root
        for i in range(4):
            self.root = root/str(i); self.root.mkdir()
            code, output, errors = self.run_parent('print("DONE",flush=True)\n')
            self.assertEqual(code, 0, errors)
            result = json.loads(output)
            self.assertEqual(result['active_processes'], 0)
            path = self.root/'run'/'child.log'
            with path.open('rb'):
                with self.assertRaises(OSError) as caught:
                    s.exclusive_reopen(path)
                self.assertEqual(caught.exception.winerror, 32)
                self.assertIn(os.getpid(), s.locking_processes(path))
            s.exclusive_reopen(path)
            # No retry, sleep, or exception suppression: remove every file now.
            for item in (self.root/'run').iterdir():
                item.unlink()
            (self.root/'run').rmdir()
        self.root = root

    def test_supervisor_native_memory_budget_refuses_allocation(self):
        code, output, errors = self.run_parent('print("DONE",flush=True)\n', fault='memory')
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertTrue(result['memory_refused'])
        self.assertGreater(result['payload_bytes'], 0)
        self.assertLess(result['payload_bytes'], 128*module().MIB)
        self.assertEqual(result['supervisor_memory_limit'], 128*module().MIB)

    def test_supervised_descendant_denial_closes_pipe_and_journal(self):
        body = ('try:\n subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\n'
                'except OSError:\n print("DENIED",flush=True)\n'
                'else:\n raise AssertionError("descendant escaped")\n')
        code, output, errors = self.run_parent(body)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)['active_processes'], 0)
        self.assertIn(b'DENIED', (self.root/'run'/'child.log').read_bytes())
        self.assertEqual(module().read_journal(self.root/'run'/'supervisor.jsonl')[-1]['event'], 'terminal_intent')

    def test_child_crash_is_terminal_failure_not_complete_recording(self):
        code, output, errors = self.run_parent('raise SystemExit(23)\n')
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertEqual(result['child_exit_code'], 23)
        self.assertFalse(result['runtime_evidence_verified'])
        self.assertEqual(result['active_processes'], 0)


class EntryTests(unittest.TestCase):
    def test_separate_entry_exists_without_launch(self):
        import ast
        path = Path(__file__).resolve().parents[1]/'src'/'guarded_full_window.py'
        tree = ast.parse(path.read_text(encoding='utf8'))
        self.assertTrue(any(isinstance(node, ast.ImportFrom) and
                            node.module=='polymarket_bot.recorder.full_window_guard' for node in tree.body))

    def test_backend_presence_does_not_certify_production_gates(self):
        result = module().runtime_readiness()
        self.assertNotIn('LONG_WINDOW_SUPERVISOR_NOT_IMPLEMENTED', result['blockers'])
        self.assertIn('PRODUCTION_RECORDER_SUPERVISION_UNVERIFIED', result['blockers'])
        self.assertIn('STRICT_TOTAL_USER_KERNEL_CPU_UNPROVEN', result['blockers'])
        self.assertIn('CLEANUP_RUNTIME_PROVENANCE_UNVERIFIED', result['blockers'])
        self.assertFalse(result['ready'])

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object API required')
    def test_unenforceable_runtime_refuses_before_child_or_http_factory(self):
        with patch('subprocess.Popen', side_effect=AssertionError('child launched')), \
             patch('httpx.HTTPTransport', side_effect=AssertionError('live transport made')):
            result = module().runtime_readiness()
            self.assertFalse(result['ready'])
            self.assertIn('HARD_ALL_ARTIFACT_QUOTA_UNAVAILABLE', result['blockers'])
            self.assertIn('BOUNDED_SOCKET_INGRESS_UNAVAILABLE', result['blockers'])
            self.assertNotIn('SUPERVISOR_128_MIB_HARD_BUDGET_UNAVAILABLE', result['blockers'])
            self.assertNotIn('LONG_WINDOW_SUPERVISOR_NOT_IMPLEMENTED', result['blockers'])
            code = module().main(['--event-id', '2001', '--market-id', '1001', '--condition-id', CONDITION,
                                  '--up-token', TOKEN_UP, '--down-token', TOKEN_DOWN,
                                  '--start', '2026-01-01T00:00:00Z', '--end', '2026-01-01T00:15:00Z',
                                  '--run-dir', 'never-created-cp63'])
            self.assertEqual(code, 2)
            self.assertFalse(Path('never-created-cp63').exists())
