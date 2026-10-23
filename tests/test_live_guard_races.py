"""Offline deterministic STOP/entry and real Windows startup scheduling proofs."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import httpx
from polymarket_bot.recorder import live_guard as g
from polymarket_bot.recorder.windows_job import WindowsJob
from tests.test_live_guard import GuardTestCase
from tests import test_live_guard as support


class StopRaceTests(GuardTestCase):
    def test_stop_during_dispatch_journal_cancels_before_lower_entry(self):
        journal_entered, stop_done = threading.Event(), threading.Event()
        calls, errors = [], []
        guard = self.guard(lambda r: (calls.append(r), httpx.Response(200))[1])
        self.assertTrue(callable(getattr(guard, 'request_stop', None)), 'synchronized STOP missing')
        original = guard._write
        def write(event, **fields):
            original(event, **fields)
            if event == 'dispatch':
                journal_entered.set()
                self.assertTrue(stop_done.wait(2))
        def stop():
            try:
                assert journal_entered.wait(2)
                guard.request_stop()
                stop_done.set()
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=stop)
        with patch.object(guard, '_write', write):
            worker.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    guard.handle_request(httpx.Request('GET','https://clob.polymarket.com/time'))
            finally:
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(guard.stop.is_set())
        self.assertEqual(calls, [])
        self.assertEqual(guard.sent, 0)
        events = self.events()
        self.assertEqual([e['event'] for e in events], ['dispatch','dispatch_cancelled','denied'])

    def test_entered_lower_blocks_stop_commit_but_no_following_dispatch(self):
        entered, release, stop_started, committed = [threading.Event() for _ in range(4)]
        calls, errors = [], []
        def lower(request):
            calls.append(guard.stop.is_set())
            entered.set()
            assert release.wait(3)
            return httpx.Response(200)
        guard = self.guard(lower)
        self.assertTrue(callable(getattr(guard, 'request_stop', None)), 'synchronized STOP missing')
        def send():
            try:
                guard.handle_request(httpx.Request('GET','https://clob.polymarket.com/time'))
            except BaseException as exc:
                errors.append(exc)
        def stop():
            stop_started.set()
            guard.request_stop()
            committed.set()
        sender = threading.Thread(target=send)
        stopper = threading.Thread(target=stop)
        sender.start()
        try:
            self.assertTrue(entered.wait(2))
            stopper.start()
            self.assertTrue(stop_started.wait(2))
            self.assertFalse(committed.wait(.1), 'STOP committed while lower entry gate still held')
            self.assertFalse(guard.stop.is_set())
        finally:
            release.set()
            sender.join(3)
            if stopper.ident is not None:
                stopper.join(3)
        self.assertFalse(sender.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(committed.is_set())
        with self.assertRaises(KeyboardInterrupt):
            guard.handle_request(httpx.Request('GET','https://clob.polymarket.com/time'))
        self.assertEqual(calls, [False])
        self.assertEqual(guard.sent, 1)

    def test_stop_at_book_intent_preserves_selected_sqlite_evidence(self):
        from tests.test_recorder_selected import SelectedTests
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.store import decode_evidence
        fixture = SelectedTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        f = fixture.fixture(offset=0)
        guard = self.guard(f.handler)
        f.client._transport.inner = guard
        self.assertTrue(callable(getattr(guard,'request_stop',None)))
        original = guard._write
        def write(event, **fields):
            original(event, **fields)
            if event == 'dispatch' and fields['url'].endswith('/books'):
                guard.request_stop()
        with patch.object(guard,'_write',write):
            result = Recorder(f.store,f.public,monotonic_ns=f.monotonic_ns,
                              wall_us=f.wall_us,sleep=f.sleep).run(fixture.config(),
                              should_stop=lambda: False,deadline_ns=f.monotonic_ns()+30_000_000_000)
        self.assertNotEqual(result.exit_code,0)
        self.assertEqual(f.book_count,0)
        self.assertEqual(guard.reason,'guard_stop_requested')
        self.assertEqual(guard.sent,len(f.requests))
        rows = f.rows('SELECT * FROM request_attempts')
        done = [row for row in rows if row['http_status'] is not None]
        self.assertEqual(len(done),guard.sent)
        self.assertGreater(len(done),0)
        for row in done:
            self.assertTrue(decode_evidence(row['response_raw_z'],row['raw_size_bytes'],row['codec_version']))
        self.assertEqual(len(rows)-len(done),1)
        self.assertEqual(f.rows('PRAGMA integrity_check')[0]['integrity_check'],'ok')
        self.assertEqual(f.rows('PRAGMA foreign_key_check'),[])
        self.assertEqual(len([e for e in self.events() if e['event']=='dispatch_cancelled']),1)

    def test_child_stop_commits_through_guard_before_interrupt(self):
        reached, finished = threading.Event(), threading.Event()
        seen = []
        class Input:
            reads = 0
            def readline(self):
                self.reads += 1
                if self.reads == 1:
                    return 'GO\n'
                assert reached.wait(2)
                return 'STOP\n'
        def interrupt():
            finished.set()
        def cli(argv, *, transport):
            seen.append(transport)
            reached.set()
            assert finished.wait(2)
            self.assertTrue(transport.stop.is_set())
            with self.assertRaises(KeyboardInterrupt):
                transport.handle_request(httpx.Request('GET','https://clob.polymarket.com/time'))
            return 0
        self.assertTrue(callable(getattr(g.BudgetTransport, 'request_stop', None)), 'synchronized STOP missing')
        original = g.BudgetTransport.request_stop
        commits = []
        def commit(guard):
            original(guard)
            commits.append(guard)
        with patch.object(g, 'in_supervisor_job', return_value=True), \
             patch.object(g.sys, 'stdin', Input()), patch.object(g._thread, 'interrupt_main', interrupt), \
             patch.object(g.BudgetTransport, 'request_stop', commit), \
             patch.object(g.httpx, 'HTTPTransport', return_value=httpx.MockTransport(lambda r: self.fail('sent'))), \
             patch('polymarket_bot.recorder.cli.main', cli):
            code = g._child(Path(self.directory)/'db', '2001', '1001', Path(self.directory))
        self.assertNotEqual(code, 0)
        self.assertEqual(commits, seen)


class StartupRaceTests(GuardTestCase):
    def launch(self, job_name):
        # Child announces arrival at GO read. Any CLI/transport before proof fails.
        root = str(Path(__file__).resolve().parents[1]/'src')
        script = Path(self.directory)/'startup.py'
        script.write_text("""import socket,sys
from pathlib import Path
from unittest.mock import patch
def denied(*a,**k): raise AssertionError('network denied')
for n in ('getaddrinfo','gethostbyname','gethostbyname_ex','getnameinfo','create_connection'): setattr(socket,n,denied)
for n in ('connect','connect_ex','sendto'): setattr(socket.socket,n,denied)
sys.path.insert(0,ROOT)
from polymarket_bot.recorder import live_guard as g
real_input=sys.stdin
class Input:
 def readline(self):
  if not Path(sys.argv[1]+'/waiting').exists(): Path(sys.argv[1]+'/waiting').touch()
  line=real_input.readline()
  return line
def cli(argv,*,transport):
 assert g.in_supervisor_job(None) is False
 Path(sys.argv[1]+'/authorized').touch()
 return 0
with patch.object(g.sys,'stdin',Input()), patch('polymarket_bot.recorder.cli.main',cli), patch.object(g.httpx,'HTTPTransport',return_value=g.httpx.MockTransport(denied)):
 raise SystemExit(g._child(Path(sys.argv[1])/'unused.db','2001','1001',Path(sys.argv[1])))
""".replace('ROOT', repr(root)))
        env = dict(os.environ)
        env.pop('CP5_SUPERVISOR_JOB', None)
        if job_name is not None:
            env['CP5_SUPERVISOR_JOB'] = job_name
        # Direct interpreter preserves venv but avoids its process-spawning shim.
        env['__PYVENV_LAUNCHER__'] = sys.executable
        process = subprocess.Popen([sys._base_executable,'-B',str(script),self.directory], env=env,
                                   stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup_process, process)
        return process

    @staticmethod
    def cleanup_process(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        for stream in (process.stdin,process.stdout,process.stderr):
            stream.close()

    def wait_for_barrier(self, process):
        until = time.monotonic()+3
        while not (Path(self.directory)/'waiting').exists() and process.poll() is None and time.monotonic()<until:
            time.sleep(.01)
        self.assertIsNone(process.poll(), 'child exited before assignment/GO')
        self.assertTrue((Path(self.directory)/'waiting').exists())

    def test_supervisor_delays_assignment_until_actual_child_waits(self):
        # Use production launch command/env, including Windows venv shim handling.
        process = self.launch('Local\\CP5-unused')
        # Reuse script after first unassigned fixture refuses on EOF.
        process.stdin.close()
        process.wait(timeout=3)
        (Path(self.directory)/'waiting').unlink(missing_ok=True)
        script = Path(self.directory)/'startup.py'
        original = WindowsJob.assign
        def delayed(job, child):
            self.wait_for_barrier(child)
            self.assertFalse((Path(self.directory)/'http.jsonl').exists())
            original(job, child)
        with patch.object(WindowsJob, 'assign', delayed):
            result = g.supervise([sys.executable,'-B',str(script),self.directory],
                                 Path(self.directory)/'supervised',grace_seconds=2,kill_seconds=3,hard_seconds=5)
        self.assertEqual(result['child_exit_code'],0,
                         (Path(self.directory)/'supervised'/'child.log').read_text())
        self.assertEqual(result['active_processes'],0)
        self.assertTrue((Path(self.directory)/'authorized').exists())

    def test_delayed_assignment_after_child_waits_then_go_succeeds(self):
        name = 'Local\\CP5-race-'+str(os.getpid())
        job = WindowsJob(name)
        self.addCleanup(job.close)
        process = self.launch(name)
        self.wait_for_barrier(process)
        self.assertFalse((Path(self.directory)/'http.jsonl').exists())
        self.assertFalse((Path(self.directory)/'authorized').exists())
        job.assign(process)
        self.assertEqual(job.active(),1)
        process.stdin.write(b'GO\n'); process.stdin.flush()
        code = process.wait(timeout=4)
        summary = Path(self.directory)/'child-summary.json'
        self.assertEqual(code, 0, (process.stderr.read().decode(), process.stdout.read().decode(),
                                  summary.read_text() if summary.exists() else 'no summary',
                                  (Path(self.directory)/'authorized').exists()))
        self.assertTrue((Path(self.directory)/'authorized').exists())

    def refused(self, *, job_name=None, payload=b'GO\n'):
        process = self.launch(job_name)
        self.wait_for_barrier(process)
        if payload:
            process.stdin.write(payload); process.stdin.flush()
        else:
            process.stdin.close()
        self.assertEqual(process.wait(timeout=4), 2, process.stderr.read().decode())
        for name in ('authorized','http.jsonl','unused.db'):
            self.assertFalse((Path(self.directory)/name).exists())

    def test_missing_job_go_refuses(self):
        self.refused()

    def test_wrong_job_go_refuses(self):
        self.refused(job_name='Local\\CP5-not-real')

    def test_premature_go_existing_but_unassigned_job_refuses(self):
        name = 'Local\\CP5-premature-'+str(os.getpid())
        job = WindowsJob(name)
        self.addCleanup(job.close)
        self.refused(job_name=name)

    def test_parent_eof_before_go_refuses(self):
        self.refused(payload=b'')


class BlockedTransportDeadlineTests(GuardTestCase):
    def test_real_job_kills_child_while_stop_waits_on_blocked_transport(self):
        # Reuse harmless-child launcher only, not inherited test cases.
        root = str(Path(__file__).resolve().parents[1]/'src')
        body = "sys.path.insert(0,"+repr(root)+")\n" + """from pathlib import Path
from polymarket_bot.recorder import live_guard as g
assert sys.stdin.readline().strip()=='GO'
stop=threading.Event()
with open(sys.argv[1] if len(sys.argv)>1 else __file__+'.jsonl','x') as journal:
 def lower(request):
  print('LOWER_ENTERED',flush=True)
  while True: time.sleep(.1)
 guard=g.BudgetTransport(g.httpx.MockTransport(lower),event_id='2001',market_id='1001',journal=journal,stop=stop)
 def control():
  sys.stdin.readline()
  print('STOP_RECEIVED',flush=True)
  guard.request_stop()
  print('STOP_COMMITTED',flush=True)
 threading.Thread(target=control,daemon=True).start()
 guard.handle_request(g.httpx.Request('GET','https://clob.polymarket.com/time'))
"""
        self.assertTrue(callable(getattr(g.BudgetTransport, 'request_stop', None)), 'synchronized STOP missing')
        command = support.SupervisorTests.child(self, body)
        result = g.supervise(command,Path(self.directory)/'evidence',grace_seconds=1.2,kill_seconds=2.4,hard_seconds=4)
        text = (Path(self.directory)/'evidence'/'child.log').read_text()
        self.assertIn('LOWER_ENTERED',text)
        self.assertIn('STOP_RECEIVED',text)
        self.assertNotIn('STOP_COMMITTED',text)
        self.assertTrue(result['forced'])
        self.assertEqual(result['active_processes'],0)
        self.assertLess(result['elapsed_seconds'],4)
        self.assertFalse(result['audit_completeness_proven'])
