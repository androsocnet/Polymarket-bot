import argparse
import importlib.util
import io
import json
import os
import signal
from unittest.mock import patch
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'cp5_supervise_durable.py'


class DurableSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root / 'execution.jsonl'
        self.db = self.root / 'toy.sqlite3'

    def launch(self, code='pass', timeout=2.0, journal=None):
        return subprocess.Popen([sys.executable, str(SCRIPT), 'run', '--journal', str(journal or self.journal), '--db', str(self.db), '--boundary', '123', '--watchdog', str(timeout), '--', sys.executable, '-c', code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def rows(self):
        if not self.journal.exists():
            return []
        return [json.loads(line) for line in self.journal.read_text().splitlines()]

    def wait_event(self, event):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            rows = self.rows()
            if any(row['event'] == event for row in rows):
                return rows
            time.sleep(.02)
        self.fail(f'missing {event}: {self.rows()}')

    def finish(self, code='pass', timeout=2.0):
        self.assertTrue(SCRIPT.exists(), 'durable supervisor missing')
        process = self.launch(code, timeout)
        self.assertEqual(process.wait(timeout=10), 0)
        return self.rows()

    def test_01_prepared_before_child(self):
        rows = self.finish("import pathlib; assert pathlib.Path(" + repr(str(self.journal)) + ").read_text().splitlines()[0].find('PREPARED') >= 0")
        self.assertEqual(rows[0]['event'], 'PREPARED')
        for key in ('execution_uuid', 'command', 'db', 'boundary', 'parent_pid', 'watchdog', 'planned_child', 'utc', 'monotonic'):
            self.assertIn(key, rows[0])

    def test_02_started_pid(self):
        rows = self.finish()
        self.assertGreater(next(r for r in rows if r['event'] == 'STARTED')['child_pid'], 0)

    def test_03_incremental_stdout(self):
        self.assertTrue(SCRIPT.exists(), 'durable supervisor missing')
        p = self.launch("import time; print('early',flush=True); time.sleep(.5)")
        self.wait_event('STDOUT')
        self.assertIsNone(p.poll())
        self.assertEqual(p.wait(timeout=8), 0)

    def test_04_incremental_stderr(self):
        rows = self.finish("import sys; print('error',file=sys.stderr,flush=True)")
        self.assertTrue(any(r['event'] == 'STDERR' and 'error' in r['data'] for r in rows))

    def test_05_success(self):
        self.assertEqual(self.finish()[-1]['event'], 'COMPLETED')

    def test_blocked_semantics(self):
        rows = self.finish("print('{\"result\":{\"reason\":\"opening_blocked\"}}')")
        self.assertEqual(rows[-1]['event'], 'BLOCKED')

    def test_06_failure(self):
        rows = self.finish('raise SystemExit(7)')
        self.assertEqual(rows[-1]['event'], 'FAILED')
        self.assertEqual(rows[-1]['returncode'], 7)

    def test_07_watchdog(self):
        rows = self.finish('import time; time.sleep(20)', .2)
        self.assertEqual(rows[-1]['event'], 'WATCHDOG_KILLED')
        self.assertTrue(any(r['event'] == 'KILL_REQUESTED' for r in rows))

    def test_08_interrupt(self):
        rows = self.finish(f"from pathlib import Path; Path({str(self.journal) + '.interrupt'!r}).touch(); import time; time.sleep(20)")
        self.assertEqual(rows[-1]['event'], 'SUPERVISOR_INTERRUPTED')
        self.assertIsNotNone(rows[-1]['returncode'])

    def test_09_existing_evidence(self):
        self.assertTrue(SCRIPT.exists(), 'durable supervisor missing')
        self.journal.write_text('original')
        self.assertNotEqual(self.launch().wait(timeout=8), 0)
        self.assertEqual(self.journal.read_text(), 'original')

    def test_10_existing_db(self):
        self.assertTrue(SCRIPT.exists(), 'durable supervisor missing')
        self.db.write_bytes(b'original')
        self.assertNotEqual(self.launch().wait(timeout=8), 0)
        self.assertEqual(self.db.read_bytes(), b'original')
        self.assertFalse(self.journal.exists())

    def test_11_exact_once(self):
        self.assertTrue(SCRIPT.exists(), 'durable supervisor missing')
        p = self.launch('import time; time.sleep(.4)')
        self.wait_event('STARTED')
        self.assertNotEqual(self.launch(journal=self.root / 'other.jsonl').wait(timeout=8), 0)
        self.assertEqual(p.wait(timeout=8), 0)

    def test_12_no_auto_retry(self):
        marker = self.root / 'count'
        self.finish(f"from pathlib import Path; p=Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); raise SystemExit(1)")
        self.assertEqual(marker.read_text(), 'x')

    def test_13_partial_offline(self):
        self.assertTrue(SCRIPT.exists(), 'durable supervisor missing')
        self.journal.write_text('{"event":"STARTED","child_pid":1}\n{"broken"')
        result = subprocess.run([sys.executable, str(SCRIPT), 'inspect', str(self.journal)], capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['outcome'], 'UNKNOWN')
        self.assertIn('broken', self.journal.read_text())

    def test_14_consumer_loss(self):
        self.assertTrue(SCRIPT.exists(), 'durable supervisor missing')
        args = [sys.executable, str(SCRIPT), 'launch', '--journal', str(self.journal), '--db', str(self.db), '--boundary', '123', '--watchdog', '.5', '--', sys.executable, '-c', "import time; print('retained',flush=True); time.sleep(20)"]
        controller = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        controller.communicate(timeout=8)
        self.assertEqual(controller.returncode, 0)
        self.wait_event('WATCHDOG_KILLED')
        self.assertTrue(any(r['event'] == 'STDOUT' for r in self.rows()))

    def module(self):
        spec = importlib.util.spec_from_file_location('durable', SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_h1_bounded_classification(self):
        rows = self.finish("import sys; sys.stdout.write('x'*2000000+'\\n'+ '{\"result\":'+ '\"'+'y'*2000000+'\"}\\n')")
        self.assertEqual(rows[-1].get('classification'), 'UNAVAILABLE_OVERSIZE')
        self.assertGreater(Path(str(self.journal)+'.stdout').stat().st_size, 400000)

    def test_m1_interrupt_during_drain(self):
        descendant = "import time; time.sleep(.7)"
        code = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{descendant!r}]); print('parent-exiting',flush=True)"
        p = self.launch(code)
        self.wait_event('STDOUT')
        time.sleep(.15)
        Path(str(self.journal)+'.interrupt').touch()
        self.assertEqual(p.wait(timeout=8), 0)
        self.assertEqual(self.rows()[-1]['event'], 'SUPERVISOR_INTERRUPTED')
        self.assertEqual(self.rows()[-1]['returncode'], 0)

    def test_m1_finalization_keyboard_interrupt(self):
        module = self.module()
        args = argparse.Namespace(journal=str(self.journal), db=str(self.db), boundary=123,
                                  watchdog=2, command=[sys.executable, '-c', 'pass'])
        original = module.os.fsync
        calls = 0
        def interrupt_finalization(fd):
            nonlocal calls
            calls += 1
            if calls == 5:
                raise KeyboardInterrupt
            return original(fd)
        with patch.object(module.os, 'fsync', side_effect=interrupt_finalization):
            module.run(args)
        self.assertEqual(self.rows()[-1]['event'], 'SUPERVISOR_INTERRUPTED')

    def test_m2_last_result_controls(self):
        rows = self.finish("print('{\"result\":{\"reason\":\"opening_blocked\"}}'); print('{\"result\":{\"state\":\"STOPPED\",\"exit_code\":0,\"reason\":\"done\"}}')")
        self.assertEqual(rows[-1]['event'], 'COMPLETED')
        self.assertEqual(rows[-1].get('semantic_reason'), 'done')

    def test_m4_invalid_inspection(self):
        module = self.module()
        for tail in (None, [], 1, {'event':'COMPLETED'}, {'event':'COMPLETED','execution_uuid':'foreign','returncode':0,'child_stopped':True}):
            with self.subTest(tail=tail):
                self.journal.write_text(json.dumps({'event':'PREPARED','execution_uuid':'bad'})+'\n'+json.dumps(tail)+'\n')
                before = self.journal.read_bytes()
                try:
                    result = module.inspect(self.journal)
                except (AttributeError, TypeError):
                    result = {'outcome':'CRASH'}
                self.assertEqual(result['outcome'], 'UNKNOWN')
                self.assertTrue(result.get('invalid_record'))
                self.assertEqual(self.journal.read_bytes(), before)

    def test_h1_parser_never_receives_oversize(self):
        module = self.module()
        original = module.json.loads
        def bounded(value, *args, **kwargs):
            self.assertLessEqual(len(value), 65536)
            return original(value, *args, **kwargs)
        args = argparse.Namespace(journal=str(self.journal), db=str(self.db), boundary=123,
                                  watchdog=5, command=[sys.executable, '-c', "import sys; sys.stdout.write('x'*2000000)"])
        with patch.object(module.json, 'loads', side_effect=bounded):
            module.run(args)
        self.assertEqual(Path(str(self.journal)+'.stdout').stat().st_size, 2000000)
        self.assertEqual(self.rows()[-1]['classification'], 'UNAVAILABLE_OVERSIZE')

    def test_m1_caught_handler_during_finalization(self):
        module = self.module()
        original = module.os.fsync
        calls = 0
        def deliver(fd):
            nonlocal calls
            calls += 1
            if calls == 5:
                handler = signal.getsignal(signal.SIGINT)
                assert callable(handler)
                handler(signal.SIGINT, None)
            return original(fd)
        args = argparse.Namespace(journal=str(self.journal), db=str(self.db), boundary=123,
                                  watchdog=2, command=[sys.executable, '-c', 'pass'])
        with patch.object(module.os, 'fsync', side_effect=deliver):
            module.run(args)
        self.assertEqual(self.rows()[-1]['event'], 'SUPERVISOR_INTERRUPTED')
        self.assertTrue(any(r['event']=='SIGNAL' for r in self.rows()))

    def test_m2_order_and_nonzero_controls(self):
        cases = [
            ("print('{\"result\":{\"reason\":\"done\"}}'); print('{\"result\":{\"reason\":\"opening_blocked\"}}')", 'BLOCKED'),
            ("print('{\"result\":{\"reason\":\"opening_blocked\"}}'); print('not json'); print('{\"wrapped\":{\"result\":{\"reason\":\"done\"}}}')", 'BLOCKED'),
            ("print('{\"result\":{\"reason\":\"opening_blocked\"}}'); raise SystemExit(7)", 'FAILED'),
            ("print('{\"result\":{\"reason\":\"opening_blocked\"}}'); print('x'*70000)", 'COMPLETED')]
        for index, (code, expected) in enumerate(cases):
            with self.subTest(index=index):
                journal = self.root / f'control{index}.jsonl'
                db = self.root / f'control{index}.db'
                module = self.module()
                module.run(argparse.Namespace(journal=str(journal),db=str(db),boundary=123,watchdog=5,command=[sys.executable,'-c',code]))
                self.assertEqual(json.loads(journal.read_text().splitlines()[-1])['event'], expected)

    def test_m4_valid_and_foreign_terminal(self):
        self.finish()
        module = self.module()
        self.assertEqual(module.inspect(self.journal)['outcome'], 'COMPLETED')
        rows = self.rows()
        rows[-1]['execution_uuid'] = '00000000-0000-0000-0000-000000000000'
        self.journal.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        self.assertEqual(module.inspect(self.journal)['outcome'], 'UNKNOWN')
        rows[-1]['execution_uuid'] = rows[0]['execution_uuid']
        del rows[-1]['returncode']
        self.journal.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        self.assertEqual(module.inspect(self.journal)['outcome'], 'UNKNOWN')

    def test_m3_spawn_ack_not_prepared(self):
        self.db.write_bytes(b'existing')
        args = [sys.executable,str(SCRIPT),'launch','--journal',str(self.journal),'--db',str(self.db),'--boundary','123','--watchdog','1','--',sys.executable,'-c','pass']
        self.assertEqual(subprocess.run(args,timeout=5).returncode, 0)
        time.sleep(.3)
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.db.read_bytes(), b'existing')

    def test_round2_terminal_append_interrupt(self):
        module = self.module()
        original = module.os.fsync
        delivered = False
        def deliver(fd):
            nonlocal delivered
            rows = self.rows()
            if not delivered and rows and rows[-1]['event'] == 'COMPLETED':
                delivered = True
                handler = signal.getsignal(signal.SIGINT)
                assert callable(handler)
                handler(signal.SIGINT, None)
            return original(fd)
        args = argparse.Namespace(journal=str(self.journal),db=str(self.db),boundary=123,watchdog=2,command=[sys.executable,'-c','pass'])
        with patch.object(module.os,'fsync',side_effect=deliver):
            module.run(args)
        self.assertTrue(delivered)
        self.assertEqual(module.inspect(self.journal)['outcome'], 'SUPERVISOR_INTERRUPTED')
        self.assertEqual(sum(r['event'] in module.TERMINAL for r in self.rows()), 1)
        self.assertTrue(any(r['event']=='LATE_SIGNAL' for r in self.rows()))

    def test_round2_decoder_recursion_retains_streams(self):
        import hashlib
        nested = b'['*20000+b'0'+b']'*20000+b'\n'
        stdout = nested+b'trailing-stdout\n'
        stderr = b'trailing-stderr\n'
        code = "import os,time; os.write(1,b'['*20000+b'0'+b']'*20000+b'\\n'); time.sleep(.15); os.write(1,b'trailing-stdout\\n'); os.write(2,b'trailing-stderr\\n')"
        rows = self.finish(code)
        self.assertEqual(Path(str(self.journal)+'.stdout').read_bytes(), stdout)
        self.assertEqual(Path(str(self.journal)+'.stderr').read_bytes(), stderr)
        self.assertEqual(rows[-1]['event'],'COMPLETED')
        self.assertEqual(rows[-1]['returncode'],0)
        self.assertEqual(rows[-1]['classification'],'UNAVAILABLE_DECODER_RECURSION')
        for suffix, data in (('.stdout',stdout),('.stderr',stderr)):
            self.assertEqual(rows[-1]['evidence_hashes'][str(self.journal.resolve())+suffix],hashlib.sha256(data).hexdigest())

    def test_m3_abrupt_controller_death(self):
        child = "import time; time.sleep(.6); print('after-controller-death',flush=True); time.sleep(20)"
        args = [sys.executable, str(SCRIPT), 'launch', '--journal', str(self.journal), '--db', str(self.db), '--boundary', '123', '--watchdog', '1.2', '--', sys.executable, '-c', child]
        controller = subprocess.Popen([sys.executable, '-c', f'import subprocess,time; subprocess.run({args!r},check=True); time.sleep(20)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.wait_event('STARTED')
            controller.kill()
            controller.wait(timeout=5)
            self.wait_event('WATCHDOG_KILLED')
            self.assertTrue(any('after-controller-death' in r.get('data','') for r in self.rows()))
            self.assertTrue(self.rows()[-1]['child_stopped'])
        finally:
            if controller.poll() is None:
                controller.kill()
                controller.wait(timeout=5)

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object control')
    def test_m3_windows_job_close(self):
        import ctypes
        from ctypes import wintypes
        class BASIC(ctypes.Structure):
            _fields_ = [('ProcessTime',ctypes.c_int64),('JobTime',ctypes.c_int64),('Flags',wintypes.DWORD),('Min',ctypes.c_size_t),('Max',ctypes.c_size_t),('Active',wintypes.DWORD),('Affinity',ctypes.c_size_t),('Priority',wintypes.DWORD),('Scheduling',wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name,ctypes.c_uint64) for name in ('ReadOp','WriteOp','OtherOp','ReadBytes','WriteBytes','OtherBytes')]
        class EXTENDED(ctypes.Structure):
            _fields_ = [('Basic',BASIC),('Io',IO),('ProcessMemory',ctypes.c_size_t),('JobMemory',ctypes.c_size_t),('PeakProcess',ctypes.c_size_t),('PeakJob',ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p,wintypes.LPCWSTR]
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel.CreateJobObjectW(None,None)
        self.assertTrue(job)
        limits = EXTENDED()
        limits.Basic.Flags = 0x2000 | 0x800
        self.assertTrue(kernel.SetInformationJobObject(job,9,ctypes.byref(limits),ctypes.sizeof(limits)))
        ready = self.root/'job-ready'
        child = "import time; time.sleep(.5); print('survived-job-close',flush=True); time.sleep(20)"
        args = [sys.executable,str(SCRIPT),'launch','--journal',str(self.journal),'--db',str(self.db),'--boundary','123','--watchdog','1.2','--',sys.executable,'-c',child]
        code = f"from pathlib import Path; import subprocess,time; p=Path({str(ready)!r}); exec('while not p.exists(): time.sleep(.01)'); subprocess.run({args!r},check=True); time.sleep(20)"
        controller = subprocess.Popen([sys.executable,'-c',code],creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(kernel.AssignProcessToJobObject(job,wintypes.HANDLE(getattr(controller, '_handle'))))
            ready.touch()
            self.wait_event('STARTED')
            self.assertTrue(kernel.CloseHandle(job))
            job = None
            controller.wait(timeout=5)
            self.wait_event('WATCHDOG_KILLED')
            self.assertTrue(any('survived-job-close' in r.get('data','') for r in self.rows()))
        finally:
            if job:
                kernel.CloseHandle(job)
            if controller.poll() is None:
                controller.kill()
                controller.wait(timeout=5)

    def test_m3_breakaway_denied_no_fallback(self):
        module = self.module()
        argv = [str(SCRIPT), 'launch', '--journal', str(self.journal), '--db', str(self.db), '--boundary', '123', '--watchdog', '1', '--', sys.executable, '-c', 'pass']
        with patch.object(sys, 'argv', argv), patch.object(module.subprocess, 'Popen', side_effect=PermissionError('breakaway denied')) as spawn:
            with self.assertRaises(PermissionError):
                module.main()
            self.assertEqual(spawn.call_count, 1)
        self.assertFalse(self.journal.exists())

    def test_15_no_network_or_acquisition_import(self):
        rows = self.finish("import socket,sys; assert not any(k.startswith('polymarket_bot') or k=='cp5_run' for k in sys.modules); socket.socket=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network forbidden')); print('offline',flush=True)")
        self.assertEqual(rows[-1]['event'], 'COMPLETED')


if __name__ == '__main__':
    unittest.main()
