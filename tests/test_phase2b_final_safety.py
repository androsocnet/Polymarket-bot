"""Safety regressions: no name stops; pidfds pinned before final identity check."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('final_safety', Path(__file__).parents[1] / 'deploy/infrastructure-trial.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def fake_membership(path, *args, **kwargs):
    if str(path).startswith('/proc/') and path.name == 'cgroup':
        return '0::/original.service\n'
    return real_read_text(path, *args, **kwargs)


real_read_text = Path.read_text


class DescriptorShutdown(unittest.TestCase):
    def test_identity_changes_after_pin_never_signals_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            group = root / 'original.service'
            group.mkdir()
            (group / 'cgroup.procs').write_text(str(os.getpid()))
            identity = {'unit': 'original.service', 'invocation_id': 'original',
                        'invocation_cgroup': '/original.service'}
            original = {'InvocationID': 'original', 'ControlGroup': '/original.service'}
            replacement = dict(original, InvocationID='replacement')
            with patch.object(m, 'CGROUP_ROOT', root), \
                 patch.object(m, 'properties', side_effect=[original, original, replacement]), \
                 patch.object(Path, 'read_text', fake_membership), \
                 patch('signal.pidfd_send_signal') as signal_pid, \
                 patch.object(m, 'command') as command:
                with self.assertRaisesRegex(ValueError, 'identity'):
                    m.terminate_invocation(identity)
                signal_pid.assert_not_called()
                command.assert_not_called()

    def test_replacement_after_final_check_cannot_retarget_pidfd(self):
        original = subprocess.Popen(['sleep', '60'])
        replacement = subprocess.Popen(['sleep', '60'])
        try:
            with tempfile.TemporaryDirectory() as td:
                group = Path(td) / 'original.service'
                group.mkdir()
                (group / 'cgroup.procs').write_text(str(original.pid))
                identity = {'unit': 'original.service', 'invocation_id': 'original', 'invocation_cgroup': '/original.service'}
                state = dict(InvocationID='original', ControlGroup='/original.service', ActiveState='active')
                real_signal = m.signal.pidfd_send_signal
                def replace_then_signal(fd, sig):
                    # Same-name group now lists replacement after last identity check.
                    (group / 'cgroup.procs').write_text(str(replacement.pid))
                    state['InvocationID'] = 'replacement'
                    real_signal(fd, sig)
                with patch.object(m, 'CGROUP_ROOT', Path(td)), patch.object(m, 'properties', side_effect=lambda u: dict(state)), \
                     patch.object(Path, 'read_text', fake_membership), \
                     patch.object(m.signal, 'pidfd_send_signal', side_effect=replace_then_signal):
                    with self.assertRaisesRegex(ValueError, 'identity'):
                        m.terminate_invocation(identity)
                original.wait(timeout=2)
                self.assertIsNone(replacement.poll())
        finally:
            for child in (original, replacement):
                if child.poll() is None:
                    child.terminate()
                child.wait()

    def test_original_process_terminated_canary_survives(self):
        original = subprocess.Popen(['sleep', '60'])
        canary = subprocess.Popen(['sleep', '60'])
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                group = root / 'original.service'
                group.mkdir()
                (group / 'cgroup.procs').write_text(str(original.pid))
                identity = {'unit': 'original.service', 'invocation_id': 'original',
                            'invocation_cgroup': '/original.service'}
                state = {'InvocationID': 'original', 'ControlGroup': '/original.service'}
                def reap():
                    original.wait()
                    (group / 'cgroup.procs').write_text('')
                waiter = threading.Thread(target=reap)
                waiter.start()
                with patch.object(m, 'CGROUP_ROOT', root), \
                     patch.object(Path, 'read_text', fake_membership), \
                     patch.object(m, 'properties', return_value=state), \
                     patch.object(m, 'command') as command:
                    m.terminate_invocation(identity)
                    original.wait(timeout=2)
                    self.assertIsNone(canary.poll())
                    command.assert_not_called()
        finally:
            for child in (original, canary):
                if child.poll() is None:
                    child.terminate()
                child.wait()


class ReviewRegressions(unittest.TestCase):
    def test_main_launch_after_hook_empty_scan_is_still_terminated(self):
        hook = subprocess.Popen(['sleep', '0.1'])
        main = None
        waiter = None
        try:
            with tempfile.TemporaryDirectory() as td:
                group = Path(td) / 'original.service'
                group.mkdir()
                procs = group / 'cgroup.procs'
                procs.write_text(str(hook.pid))
                identity = dict(unit='original.service', invocation_id='original', invocation_cgroup='/original.service')
                calls = 0
                def state(unit):
                    nonlocal calls, main, waiter
                    calls += 1
                    if calls == 3:
                        hook.wait(timeout=2)
                        procs.write_text('')
                    if calls == 4:
                        main = subprocess.Popen(['sleep', '60'])
                        procs.write_text(str(main.pid))
                        def reap():
                            main.wait()
                            try:
                                procs.write_text('')
                            except FileNotFoundError:
                                pass  # Failed assertion already removed temporary cgroup.
                        waiter = threading.Thread(target=reap)
                        waiter.start()
                    return dict(InvocationID='original', ControlGroup='/original.service',
                                ActiveState='active' if calls >= 4 else 'activating')
                with patch.object(m, 'CGROUP_ROOT', Path(td)), patch.object(m, 'properties', side_effect=state), \
                     patch.object(Path, 'read_text', fake_membership):
                    m.terminate_invocation(identity)
                self.assertIsNotNone(main)
                self.assertIsNotNone(main.poll(), 'successful hook exit must not hide later main launch')
                waiter.join(timeout=2)
        finally:
            for child in (hook, main):
                if child is not None:
                    if child.poll() is None:
                        child.terminate()
                    child.wait()
            if waiter is not None:
                waiter.join(timeout=2)

    def test_nested_member_terminated_without_signaling_sibling_canary(self):
        child = subprocess.Popen(['sleep', '60'])
        canary = subprocess.Popen(['sleep', '60'])
        waiter = None
        try:
            with tempfile.TemporaryDirectory() as td:
                group = Path(td) / 'original.service'
                nested = group / 'nested' / 'leaf'
                nested.mkdir(parents=True)
                for folder in (group, nested.parent):
                    (folder / 'cgroup.procs').write_text('')
                procs = nested / 'cgroup.procs'
                procs.write_text(str(child.pid))
                sibling = Path(td) / 'original.service-replacement'
                sibling.mkdir()
                (sibling / 'cgroup.procs').write_text(str(canary.pid))
                def reap():
                    child.wait()
                    try:
                        procs.write_text('')
                    except FileNotFoundError:
                        pass  # Failed assertion already removed temporary cgroup.
                waiter = threading.Thread(target=reap)
                waiter.start()
                def membership(path, *args, **kwargs):
                    if str(path) == f'/proc/{child.pid}/cgroup':
                        return '0::/original.service/nested/leaf\n'
                    return real_read_text(path, *args, **kwargs)
                identity = dict(unit='original.service', invocation_id='original', invocation_cgroup='/original.service')
                state = dict(InvocationID='original', ControlGroup='/original.service', ActiveState='active')
                with patch.object(m, 'CGROUP_ROOT', Path(td)), patch.object(m, 'properties', return_value=state), \
                     patch.object(Path, 'read_text', membership):
                    m.terminate_invocation(identity)
                self.assertIsNotNone(child.poll(), 'nested original member survived shutdown')
                self.assertIsNone(canary.poll())
                waiter.join(timeout=2)
        finally:
            for process in (child, canary):
                if process.poll() is None:
                    process.terminate()
                process.wait()
            if waiter is not None:
                waiter.join(timeout=2)

    def test_cancel_reports_nonquiescent_registered_target(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / 'main-intent.json').write_text('{}')
            identity = dict(unit='original.service', invocation_id='original', invocation_cgroup='/original.service')
            state = dict(ActiveState='active', SubState='running', MainPID='123', ControlPID='0', Result='success')
            item = {'unit': 'original.service', 'properties': state, 'pids': [123]}
            with patch.object(m, 'registered_identity', return_value=identity), \
                 patch.object(m, 'properties', return_value=state), patch.object(m, 'cgroup_pids', return_value=[123]), \
                 patch.object(m, 'terminate_invocation'), patch.object(m, 'unit_evidence', return_value=item), \
                 patch.object(m.time, 'perf_counter', side_effect=[0, 10]):
                errors, evidence = m.cancel_invocations(folder, {'trial_id': 'trial', 'boot_id': 'boot'})
            self.assertTrue(errors, 'cleanup must not report success with registered processes alive')
            self.assertEqual(evidence[0]['pids'], [123])

    def test_empty_activating_group_not_treated_as_terminated(self):
        child = subprocess.Popen(['sleep', '60'])
        try:
            with tempfile.TemporaryDirectory() as td:
                group = Path(td) / 'original.service'
                group.mkdir()
                procs = group / 'cgroup.procs'
                procs.write_text('')
                identity = {'unit': 'original.service', 'invocation_id': 'original', 'invocation_cgroup': '/original.service'}
                calls = []
                def state(unit):
                    calls.append(unit)
                    if len(calls) == 4:
                        procs.write_text(str(child.pid))
                    return dict(InvocationID='original', ControlGroup='/original.service',
                                ActiveState='activating' if len(calls) < 4 else 'active')
                def reap():
                    child.wait()
                    procs.write_text('')
                waiter = threading.Thread(target=reap)
                waiter.start()
                with patch.object(m, 'CGROUP_ROOT', Path(td)), patch.object(m, 'properties', side_effect=state), \
                     patch.object(Path, 'read_text', fake_membership):
                    m.terminate_invocation(identity)
                self.assertIsNotNone(child.poll(), 'empty start-pre transition must not skip future main')
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait()

    def test_disappearing_proc_does_not_skip_live_original(self):
        with tempfile.TemporaryDirectory() as td:
            group = Path(td) / 'original.service'
            group.mkdir()
            (group / 'cgroup.procs').write_text('101 102')
            identity = {'unit': 'original.service', 'invocation_id': 'original', 'invocation_cgroup': '/original.service'}
            state = dict(InvocationID='original', ControlGroup='/original.service', ActiveState='active')
            signaled = []
            def membership(path, *args, **kwargs):
                if str(path) == '/proc/101/cgroup':
                    raise FileNotFoundError('exited child')
                return fake_membership(path, *args, **kwargs)
            def send(fd, sig):
                signaled.append(fd)
                (group / 'cgroup.procs').write_text('')
            with patch.object(m, 'CGROUP_ROOT', Path(td)), patch.object(m, 'properties', return_value=state), \
                 patch.object(m.os, 'pidfd_open', side_effect=lambda pid: os.open('/dev/null', os.O_RDONLY)), \
                 patch.object(m.select, 'poll') as poll, patch.object(Path, 'read_text', membership), \
                 patch.object(m.signal, 'pidfd_send_signal', side_effect=send):
                poll.return_value.poll.return_value = []
                m.terminate_invocation(identity)
            self.assertEqual(len(signaled), 1)


class ActualRuntime(unittest.TestCase):
    def test_timing_reads_runtime_of_actual_main(self):
        with tempfile.TemporaryDirectory() as td:
            data = {'seconds': 1800, 'fingerprint': 'fp', 'boot_id': 'boot',
                    'launch_mono': 90, 'units': m.owned('pilot-20261101T000000Z')}
            service = {'ActiveEnterTimestampMonotonic': '95000000', 'InvocationID': 'id',
                       'ControlGroup': '/main.service'}
            def bound(property_name='RuntimeMaxUSec', unit=None):
                if unit != 'actual-main.service':
                    return 2040000000 if property_name == 'RuntimeMaxUSec' else 0
                return 0  # actual instance has no backstop
            with patch.object(m, 'monotonic_now', return_value=100), \
                 patch.object(m, 'runtime_bound', side_effect=bound), \
                 patch.object(m, 'arm_timer', return_value={}) as timer:
                with self.assertRaisesRegex(ValueError, 'runtime'):
                    m.start_observation_timing(Path(td), data, service, 'actual-main.service')
                timer.assert_not_called()


class HookAdmission(unittest.TestCase):
    def test_missing_registration_hook_rejected(self):
        policy = '\n'.join(['ExecStart=' + str(m.ROOT / '.venv/bin/python') + ' -m polymarket_bot.linux_mvp ingest',
            'Restart=no', 'KillMode=control-group', 'MemoryMax=536870912',
            'CPUQuotaPerSecUSec=500ms', 'TasksMax=64', 'User=polymarket-btc', 'ExecStartPre='])
        def command(*args):
            return str(m.ENV) + ' (ignore_errors=no)' if '--value' in args else policy
        with patch.object(m, 'command', side_effect=command):
            with self.assertRaisesRegex(ValueError, 'registration hook'):
                m.require_service_policy('actual.service')

    def test_exact_root_registration_hook_accepted(self):
        unit = 'actual.service'
        python = str(m.ROOT / '.venv/bin/python')
        hook = '{ path=' + python + ' ; argv[]=' + python + ' ' + str(m.ROOT / 'deploy/infrastructure-trial.py') + ' register --unit ' + unit + ' ; flags=no-setuid ; }'
        policy = '\n'.join(['ExecStart=' + python + ' -m polymarket_bot.linux_mvp ingest',
            'Restart=no', 'KillMode=control-group', 'MemoryMax=536870912',
            'CPUQuotaPerSecUSec=500ms', 'TasksMax=64', 'User=polymarket-btc', 'ExecStartPreEx=' + hook])
        def command(*args):
            return str(m.ENV) + ' (ignore_errors=no)' if '--value' in args else policy
        with patch.object(m, 'command', side_effect=command):
            m.require_service_policy(unit)

    def test_missing_main_registration_rejected_before_timer(self):
        with tempfile.TemporaryDirectory() as td:
            data = {'seconds': 1800, 'fingerprint': 'fp', 'boot_id': 'boot', 'launch_mono': 90,
                    'units': m.owned('pilot-20261101T000000Z')}
            service = {'ActiveEnterTimestampMonotonic': '95000000', 'InvocationID': 'id', 'ControlGroup': '/main'}
            with patch.object(m, 'monotonic_now', return_value=100), \
                 patch.object(m, 'runtime_bound', side_effect=[2040000000, 0]), \
                 patch.object(m, 'registered_identity', side_effect=ValueError('missing registration')), \
                 patch.object(m, 'arm_timer', return_value={}) as timer:
                with self.assertRaisesRegex(ValueError, 'registration'):
                    m.start_observation_timing(Path(td), data, service, 'actual.service')
                timer.assert_not_called()


class Registration(unittest.TestCase):
    def test_intent_durable_before_start_and_registration_exclusive(self):
        trial = 'pilot-20261101T000000Z'
        data = {'trial_id': trial, 'boot_id': 'boot', 'seconds': 1800, 'fingerprint': 'fp'}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / 'evidence' / trial
            folder.mkdir(parents=True)
            unit = m.invocation_unit(trial, 'main')
            with patch.object(m, 'STATE', root), patch.object(m, 'EVIDENCE', root / 'evidence'), \
                 patch.object(m, 'secure_bytes', side_effect=lambda p: p.read_bytes()), \
                 patch.object(m, 'boot_id', return_value='boot'), \
                 patch.object(m, 'properties', return_value={'InvocationID': 'a' * 32, 'ControlGroup': '/original.service'}), \
                 patch.object(m, 'runtime_bound', side_effect=lambda property_name='RuntimeMaxUSec', unit=None: 2040000000 if property_name == 'RuntimeMaxUSec' else 0), \
                 patch.object(m, 'require_service_policy'), \
                 patch.object(m.os, 'geteuid', return_value=0), \
                 patch.object(m, 'command', return_value='template'), \
                 patch.dict(os.environ, {'INVOCATION_ID': 'a' * 32}):
                m.prepare_invocation(folder, data, 'main', unit)
                intent = json.loads((folder / 'main-intent.json').read_text())
                self.assertEqual(intent['unit'], unit)
                m.register_invocation(unit)
                registration = m.registered_identity(folder, data, 'main')
                self.assertEqual(registration['invocation_id'], 'a' * 32)
                with self.assertRaises(FileExistsError):
                    m.register_invocation(unit)

    def test_cancelled_pending_start_cannot_register(self):
        trial = 'pilot-20261101T000000Z'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / trial
            folder.mkdir()
            unit = m.invocation_unit(trial, 'main')
            (folder / 'main-intent.json').write_text(json.dumps({'unit': unit}))
            (folder / 'cancelled.json').write_text('{}')
            with patch.object(m, 'EVIDENCE', root), patch.object(m.os, 'geteuid', return_value=0), \
                 patch.object(m, 'secure_bytes', side_effect=lambda p: p.read_bytes()), \
                 patch.object(m, 'properties', side_effect=AssertionError('cancelled launch must not inspect/start')):
                with self.assertRaisesRegex(ValueError, 'cancelled'):
                    m.register_invocation(unit)
                self.assertFalse((folder / 'main-identity.json').exists())

    def test_startup_without_timing_terminates_registered_original(self):
        data = {'trial_id': 'pilot-20261101T000000Z', 'boot_id': 'boot', 'units': m.owned('pilot-20261101T000000Z')}
        identity = {'unit': 'original', 'invocation_id': 'id', 'invocation_cgroup': '/original'}
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            with patch.object(m, 'trial_metadata', return_value=data), \
                 patch.object(m, 'cancel_invocations', return_value=([], [identity])) as cleanup, \
                 patch.object(m, 'stop_units', return_value=([], [])) as stops:
                m.startup_timeout(folder)
                cleanup.assert_called_once_with(folder, data)
                self.assertTrue((folder / 'startup-stop.json').exists())
                self.assertNotIn('original', [u for call in stops.call_args_list for u in call.args[0]])


if __name__ == '__main__':
    unittest.main()
