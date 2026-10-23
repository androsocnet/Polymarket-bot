"""Offline guard proofs. No external network allowed, including subprocess fixtures."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from polymarket_bot.market_data.http import PublicGet
from tests.recorder_support import BookFixture, down_book
from tests.support import CONDITION, TOKEN_UP, TOKEN_DOWN, make_book


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name in ('getaddrinfo', 'create_connection'):
            self.stack.enter_context(patch.object(socket, name, side_effect=AssertionError('network denied')))
        for name in ('connect', 'connect_ex'):
            self.stack.enter_context(patch.object(socket.socket, name, side_effect=AssertionError('network denied')))
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('polymarket_bot.recorder.live_guard'),
                             'shared transport budget missing')
        return importlib.import_module('polymarket_bot.recorder.live_guard')

    def guard(self, handler):
        mod = self.module()
        journal = self.stack.enter_context(open(Path(self.directory)/'sends.jsonl', 'x', encoding='utf8'))
        guard = mod.BudgetTransport(httpx.MockTransport(handler), event_id='2001', market_id='1001',
                                    journal=journal, stop=threading.Event())
        self.stack.callback(guard.close)
        return guard

    def events(self):
        return [json.loads(line) for line in (Path(self.directory)/'sends.jsonl').read_text().splitlines()]

class BudgetTests(GuardTestCase):
    def test_sixty_allowed_sixty_first_refused_all_routes_share_budget(self):
        sent = []
        guard = self.guard(lambda request: (sent.append(str(request.url)), httpx.Response(200, json={}))[1])
        routes = [('GET', 'https://gamma-api.polymarket.com/events/2001'),
                  ('GET', 'https://gamma-api.polymarket.com/markets/1001'),
                  ('GET', 'https://clob.polymarket.com/time'),
                  ('GET', 'https://clob.polymarket.com/clob-markets/'+CONDITION),
                  ('POST', 'https://clob.polymarket.com/books'),
                  ('GET', 'https://clob.polymarket.com/book?token_id='+TOKEN_UP)]
        with httpx.Client(transport=guard, trust_env=False) as client:
            for i in range(60):
                method, url = routes[i % 6]
                client.request(method, url, json=[{'token_id':TOKEN_UP},{'token_id':TOKEN_DOWN}] if method=='POST' else None)
            for _ in range(2):
                with self.assertRaises(KeyboardInterrupt):
                    client.get('https://clob.polymarket.com/time')
        self.assertEqual(len(sent), 60)
        self.assertEqual(guard.sent, 60)
        self.assertEqual(guard.reason, 'http_budget_exhausted')
        self.assertEqual(len([e for e in self.events() if e['event']=='dispatch']), 60)
        self.assertEqual(len([e for e in self.events() if e['event']=='denied']), 2)

    def test_real_collector_retry_and_fallback_each_count(self):
        fixture = BookFixture(status=404, singles={TOKEN_UP:(200,make_book()), TOKEN_DOWN:(200,down_book())})
        self.addCleanup(fixture.close)
        calls = []
        def respond(request):
            calls.append(request.url.path)
            if len(calls)==1:
                return httpx.Response(503, stream=httpx.ByteStream(b'busy'))
            return fixture._respond(request)
        guard = self.guard(respond)
        client = httpx.Client(transport=guard, trust_env=False)
        self.addCleanup(client.close)
        fixture.public = PublicGet(client, monotonic=fixture.clock.monotonic, sleep=fixture.clock.sleep,
                                   utcnow=fixture.clock.utcnow, max_attempts=2)
        result = fixture.collect(deadline=10)
        self.assertEqual(calls, ['/books','/books','/book','/book'])
        self.assertEqual(guard.sent, 4)
        self.assertEqual([a.http_status for a in result.attempts], [503,404,200,200])
        self.assertEqual([e['status'] for e in self.events() if e['event']=='response'], [503,404,200,200])

    def test_trade_auth_discovery_wrong_identity_denied_before_send(self):
        sent = []
        guard = self.guard(lambda r: (sent.append(r), httpx.Response(200))[1])
        forbidden = [httpx.Request('POST','https://clob.polymarket.com/order'),
                     httpx.Request('GET','https://clob.polymarket.com/time',headers={'Authorization':'forbidden'}),
                     httpx.Request('GET','https://clob.polymarket.com/time',headers={'Cookie':'forbidden'}),
                     httpx.Request('GET','https://clob.polymarket.com/time',headers={'POLY_API_KEY':'forbidden'}),
                     httpx.Request('GET','https://clob.polymarket.com/time',headers={'X-Signature':'forbidden'}),
                     httpx.Request('GET','https://gamma-api.polymarket.com/events'),
                     httpx.Request('GET','https://gamma-api.polymarket.com/events/9999'),
                     httpx.Request('GET','https://gamma-api.polymarket.com/markets/9999'),
                     httpx.Request('GET','https://evil.test/time'),
                     httpx.Request('GET','http://clob.polymarket.com/time'),
                     httpx.Request('GET','https://clob.polymarket.com/time?x=1'),
                     httpx.Request('POST','https://clob.polymarket.com/books',json={'order':'forbidden'})]
        for request in forbidden:
            with self.subTest(url=str(request.url)), self.assertRaises(KeyboardInterrupt):
                guard.handle_request(request)
        self.assertEqual(sent, [])
        self.assertEqual(guard.sent, 0)

    def test_exhaustion_real_recorder_keeps_completed_sends_and_raw_evidence(self):
        from tests.test_recorder_selected import SelectedTests
        from polymarket_bot.recorder.service import Recorder
        from polymarket_bot.recorder.store import decode_evidence
        support = SelectedTests()
        support.setUp()
        self.addCleanup(support.doCleanups)
        f = support.fixture(offset=0)
        guard = self.guard(f.handler)
        # Replace only innermost mock. AdmissionTransport/service guards stay real.
        f.client._transport.inner = guard
        result = Recorder(f.store, f.public, monotonic_ns=f.monotonic_ns,
                          wall_us=f.wall_us, sleep=f.sleep).run(support.config(),
                          should_stop=lambda: False, deadline_ns=f.monotonic_ns()+150_000_000_000)
        self.assertNotEqual(result.exit_code,0)
        self.assertEqual(guard.reason,'http_budget_exhausted')
        self.assertEqual(guard.sent,60)
        self.assertEqual(len(f.requests),60)
        rows = f.rows('SELECT * FROM request_attempts ORDER BY start_mono_ns, request_id')
        completed = [row for row in rows if row['http_status'] is not None]
        self.assertEqual(len(completed),60)
        for row in completed:
            raw = decode_evidence(row['response_raw_z'],row['raw_size_bytes'],row['codec_version'])
            self.assertTrue(raw)
            self.assertTrue(row['raw_complete'])
        denied = [row for row in rows if row['http_status'] is None]
        self.assertEqual(len(denied),1)
        self.assertFalse(denied[0]['raw_complete'])
        self.assertEqual(len([e for e in self.events() if e['event']=='denied']),1)
        self.assertEqual(f.rows('PRAGMA integrity_check')[0]['integrity_check'],'ok')
        self.assertEqual(f.rows('PRAGMA foreign_key_check'),[])

    def test_stop_and_journal_failure_refuse_before_send(self):
        sent = []
        guard = self.guard(lambda r: sent.append(r))
        guard.stop.set()
        with self.assertRaises(KeyboardInterrupt):
            guard.handle_request(httpx.Request('GET','https://clob.polymarket.com/time'))
        self.assertEqual(sent, [])
        guard.stop.clear()
        guard.journal.close()
        with self.assertRaises(KeyboardInterrupt):
            guard.handle_request(httpx.Request('GET','https://clob.polymarket.com/time'))
        self.assertEqual(sent, [])

    def test_cli_transport_injection_preserves_cooperative_audit(self):
        from tests.test_recorder_selected import SelectedTests
        from polymarket_bot.recorder import cli
        support = SelectedTests()
        support.setUp()
        self.addCleanup(support.doCleanups)
        f = support.fixture()
        handler = f.handler
        guard = self.guard(handler)
        # Exhaust just before first book: real service must retain denial separately,
        # while every completed send remains represented in SQLite.
        def respond(request):
            response = handler(request)
            if request.url.path.startswith('/clob-markets/'):
                guard.stop.set()
            return response
        guard.inner = httpx.MockTransport(respond)
        f.store.close()
        f.db.path.unlink()
        original_open = cli.RecorderStore.open
        def opened(path, **kwargs):
            f.store = original_open(path, **kwargs)
            return f.store
        with patch.object(cli.RecorderStore, 'open', side_effect=opened), \
             patch.object(cli.time,'monotonic',f.monotonic), patch.object(cli.time,'monotonic_ns',f.monotonic_ns), \
             patch.object(cli.time,'sleep',f.sleep), patch.object(cli,'_wall_us',f.wall_us):
            code = cli.main(['record','--mode','book-only','--db',str(f.db.path),
                             '--event-id','2001','--market-id','1001','--duration-seconds','30'], transport=guard)
        self.assertNotEqual(code, 0)
        self.assertEqual(guard.sent, len(f.requests))
        self.assertGreater(guard.sent, 0)
        self.assertEqual(f.book_count, 0)
        self.assertEqual(f.rows('PRAGMA integrity_check')[0]['integrity_check'], 'ok')
        self.assertEqual(f.rows('PRAGMA foreign_key_check'), [])
        self.assertTrue(f.rows('SELECT * FROM recording_runs'))


class RunnerTests(GuardTestCase):
    def test_child_reuses_cli_single_run_mock_only_and_budget_summary(self):
        mod = self.module()
        self.assertTrue(callable(getattr(mod, '_child', None)), 'guarded CLI child missing')
        # Exercise real child entry + real CLI: MockTransport time fails closed.
        # Controller blocks until test ends; no signal delivered outside child call.
        release = threading.Event()
        control_done = threading.Event()
        class Input:
            calls = 0
            def readline(self):
                self.calls += 1
                if self.calls == 1:
                    return 'GO\n'
                release.wait(5)
                control_done.set()
                return 'STOP\n'
        sends = []
        def respond(request):
            sends.append(request.url.path)
            return httpx.Response(400, stream=httpx.ByteStream(b'offline invalid clock'))
        journal_dir = Path(self.directory)
        with patch.object(mod.sys, 'stdin', Input()), patch.object(mod, 'in_supervisor_job', return_value=True), \
             patch.object(mod.httpx, 'HTTPTransport', return_value=httpx.MockTransport(respond)) as factory, \
             patch.object(mod._thread, 'interrupt_main'):
            try:
                code = mod._child(journal_dir/'new.sqlite', '2001', '1001', journal_dir)
            finally:
                release.set()
                self.assertTrue(control_done.wait(1))
        self.assertNotEqual(code, 0)
        self.assertGreater(len(sends), 0)
        self.assertTrue(all(path == '/time' for path in sends))
        factory.assert_called_once_with(retries=0, trust_env=False)
        summary = json.loads((journal_dir/'child-summary.json').read_text())
        self.assertEqual(summary['transport_dispatches'], len(sends))
        self.assertFalse(summary['audit_completeness_proven'])

    def test_runner_rejects_invalid_input_without_launch(self):
        mod = self.module()
        self.assertTrue(callable(getattr(mod, 'main', None)), 'guarded runner missing')
        for flags in [[], ['--event-id','../bad','--market-id','1001'],
                      ['--event-id','2001','--market-id','1001','--duration-seconds','60']]:
            with patch.object(mod,'supervise',side_effect=AssertionError('must not launch')), \
                 self.assertRaises(SystemExit):
                mod.main(['--db',str(Path(self.directory)/'db.sqlite'),
                          '--evidence-dir',str(Path(self.directory)/'evidence'),*flags])

    def test_child_requires_supervisor_job_membership_before_cli(self):
        mod = self.module()
        self.assertTrue(callable(getattr(mod, 'in_supervisor_job', None)), 'child job membership check missing')
        with patch.object(mod.sys,'stdin',io.StringIO('GO\n')), \
             patch('polymarket_bot.recorder.cli.main',side_effect=AssertionError('uncontained child ran CLI')):
            self.assertEqual(mod._child(Path(self.directory)/'db.sqlite','2001','1001',
                                        Path(self.directory), job_name='nonexistent-cp5-job'),2)

    def test_cli_never_silently_drops_injected_transport_in_automatic_mode(self):
        from polymarket_bot.recorder import cli
        with patch.object(cli, '_record', side_effect=AssertionError('unguarded automatic mode')):
            self.assertEqual(cli.main(['record','--mode','book-only','--db','unused.sqlite'],
                                      transport=httpx.MockTransport(lambda r: None)), 2)

    def test_cli_rejects_injection_in_other_modes(self):
        from polymarket_bot.recorder import cli
        for mode in ('reference-required','approximate-reference'):
            with patch.object(cli,'_record',side_effect=AssertionError('other mode launched')):
                self.assertEqual(cli.main(['record','--mode',mode,'--db','unused.sqlite'],
                                          transport=httpx.MockTransport(lambda r: None)), 2)


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object API required')
class SupervisorTests(GuardTestCase):
    def child(self, body):
        import sys
        prefix = "import socket,sys,time,threading,_thread\n" \
                 "def denied(*a,**k): raise AssertionError('network denied')\n" \
                 "socket.getaddrinfo=socket.create_connection=denied\n" \
                 "socket.socket.connect=socket.socket.connect_ex=denied\n"
        script = Path(self.directory)/'child.py'
        script.write_text(prefix+body)
        return [sys.executable, '-B', str(script)]

    def supervise(self, body, **kwargs):
        mod = self.module()
        self.assertTrue(callable(getattr(mod, 'supervise', None)), 'independent supervisor missing')
        return mod.supervise(self.child(body), Path(self.directory)/'evidence',
                             grace_seconds=1.2, kill_seconds=2.4, hard_seconds=4, **kwargs)

    def test_stalled_child_killed_no_second_launch_cleanup_before_deadline(self):
        import time
        started = time.monotonic()
        result = self.supervise("assert sys.stdin.readline().strip()=='GO'\nprint('START',flush=True)\nwhile True: time.sleep(10)\n")
        self.assertTrue(result['forced'])
        self.assertEqual(result['launches'], 1)
        self.assertEqual(result['active_processes'], 0)
        self.assertLess(time.monotonic()-started, 4)
        self.assertFalse(result['audit_completeness_proven'])
        self.assertIsNone(result['actual_sends'])
        self.assertEqual((Path(self.directory)/'evidence'/'child.log').read_text().count('START'), 1)

    def test_graceful_interrupt_waits_for_cleanup(self):
        result = self.supervise("assert sys.stdin.readline().strip()=='GO'\n"
            "def control():\n sys.stdin.readline(); _thread.interrupt_main()\n"
            "threading.Thread(target=control,daemon=True).start()\n"
            "try:\n while True: time.sleep(.01)\n"
            "except KeyboardInterrupt:\n time.sleep(.2); print('CLEANED',flush=True)\n")
        self.assertFalse(result['forced'], (result, (Path(self.directory)/'evidence'/'child.log').read_text()))
        self.assertEqual(result['active_processes'], 0)
        self.assertLess(result['elapsed_seconds'], 4)
        self.assertIn('CLEANED', (Path(self.directory)/'evidence'/'child.log').read_text())

    def test_stalled_cleanup_forced_no_false_audit_completeness(self):
        result = self.supervise("assert sys.stdin.readline().strip()=='GO'\n"
            "def control():\n sys.stdin.readline(); _thread.interrupt_main()\n"
            "threading.Thread(target=control,daemon=True).start()\n"
            "try:\n while True: time.sleep(.01)\n"
            "except KeyboardInterrupt:\n print('CLEANUP_STALLED',flush=True); time.sleep(60)\n")
        self.assertTrue(result['forced'])
        self.assertEqual(result['active_processes'], 0)
        self.assertFalse(result['audit_completeness_proven'])
        self.assertLess(result['elapsed_seconds'], 4)

    def test_child_exit_does_not_leave_descendant(self):
        result = self.supervise("assert sys.stdin.readline().strip()=='GO'\n"
            "import subprocess\nsubprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(60)'])\n"
            "print('DESCENDANT',flush=True)\n")
        self.assertEqual(result['active_processes'], 0)
        self.assertTrue(result['forced'])
        self.assertFalse(result['audit_completeness_proven'])

    def test_hard_deadline_covers_stalled_supervisor_cleanup(self):
        import subprocess
        import sys
        import time
        mod = self.module()
        root = str(Path(__file__).resolve().parents[1]/'src')
        script = self.child("sys.path.insert(0,"+repr(root)+")\n"
            "from polymarket_bot.recorder import live_guard as g\n"
            "from unittest.mock import patch\n"
            "original=g.WindowsJob.close\n"
            "def stalled(job):\n original(job); time.sleep(60)\n"
            "with patch.object(g.WindowsJob,'close',stalled):\n"
            " g.supervise([sys.executable,'-B','-c',\"import sys; assert sys.stdin.readline().strip()=='GO'\"],"
            +repr(str(Path(self.directory)/'nested'))+",grace_seconds=1,kill_seconds=2,hard_seconds=3)\n")
        started = time.monotonic()
        child = subprocess.Popen(script, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            try:
                child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.fail('supervisor cleanup survived independent hard deadline')
            self.assertEqual(child.returncode, 124)
            self.assertLess(time.monotonic()-started,4)
            self.assertFalse((Path(self.directory)/'nested'/'supervisor.json').exists(),
                             'terminal summary written before cleanup finished')
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()

    def test_unconfirmed_child_summary_not_reported_as_exact_wire_send_count(self):
        result = self.supervise("assert sys.stdin.readline().strip()=='GO'\nimport json\n"
            "from pathlib import Path\nPath("+repr(str(Path(self.directory)/'evidence'/'child-summary.json'))+")"
            ".write_text(json.dumps({'transport_dispatches':1,'reason':None,'recorder_exit_code':0}))\n")
        self.assertIsNone(result['actual_sends'], 'dispatch is upper bound, not confirmed wire send')
        self.assertEqual(result['transport_dispatches'],1)

    def test_real_child_confirms_named_supervisor_job(self):
        root = str(Path(__file__).resolve().parents[1]/'src')
        result = self.supervise("sys.path.insert(0,"+repr(root)+")\nimport os\n"
            "assert sys.stdin.readline().strip()=='GO'\n"
            "from polymarket_bot.recorder.windows_job import in_supervisor_job\n"
            "assert in_supervisor_job(os.environ['CP5_SUPERVISOR_JOB'])\nprint('MEMBER',flush=True)\n")
        self.assertEqual(result['child_exit_code'],0,(Path(self.directory)/'evidence'/'child.log').read_text())
        self.assertIn('MEMBER',(Path(self.directory)/'evidence'/'child.log').read_text())

    def test_assignment_failure_never_releases_barrier(self):
        mod = self.module()
        self.assertTrue(hasattr(mod, 'WindowsJob'), 'Windows containment missing')
        marker = Path(self.directory)/'released'
        command = self.child("line=sys.stdin.readline()\nif line.strip()=='GO':\n open("+repr(str(marker)) +",'w').write('unsafe')\n")
        with patch.object(mod.WindowsJob,'assign',side_effect=OSError('assignment denied')):
            with self.assertRaisesRegex(OSError,'assignment denied'):
                mod.supervise(command,Path(self.directory)/'evidence',grace_seconds=1,kill_seconds=2,hard_seconds=4)
        self.assertFalse(marker.exists())

    def test_independent_watchdog_fires_even_when_main_wait_stalls(self):
        mod = self.module()
        self.assertTrue(hasattr(mod, 'WindowsJob'), 'Windows containment missing')
        observed = []
        original = mod.WindowsJob.terminate
        def terminate(job):
            observed.append(threading.current_thread().name)
            return original(job)
        with patch.object(mod.WindowsJob,'terminate',terminate):
            result = self.supervise("assert sys.stdin.readline().strip()=='GO'\nwhile True: time.sleep(10)\n")
        self.assertTrue(any(name != 'MainThread' for name in observed))
        self.assertEqual(result['active_processes'],0)


if __name__ == '__main__':
    unittest.main()
