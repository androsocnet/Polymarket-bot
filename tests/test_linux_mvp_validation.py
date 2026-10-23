"""Validation monitor continuity rules; synthetic clocks, no systemd/network."""
import importlib.util
from pathlib import Path
import unittest


def monitor():
    spec = importlib.util.spec_from_file_location('paper_validation', Path(__file__).parents[1] / 'deploy' / 'paper-validation.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContinuityTests(unittest.TestCase):
    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows host heartbeat clock')
    def test_host_heartbeat_uses_available_monotonic_clock(self):
        import subprocess
        script = (Path(__file__).parents[1] / 'deploy' / 'paper-host-heartbeat.ps1').read_text()
        # Exercise actual clock expressions without starting one-hour host loop.
        lines = [line.strip() for line in script.splitlines() if line.strip().startswith(('$lastTick =', '$tick ='))]
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                 '\n'.join(lines) + '\nif ($null -eq $lastTick -or $null -eq $tick) { exit 7 }; if ($tick -lt $lastTick) { exit 8 }'],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_normal_cycle(self):
        self.assertEqual(monitor().continuity_error(
            dict(wall=100, mono=50, boot='a'), dict(wall=105, mono=55, boot='a')), '')

    def test_sleep_shutdown_clock_jump_invalidate(self):
        previous = dict(wall=100, mono=50, boot='a')
        for current in [dict(wall=200, mono=150, boot='a'),
                        dict(wall=200, mono=55, boot='a'),
                        dict(wall=90, mono=55, boot='a'),
                        dict(wall=105, mono=55, boot='b')]:
            with self.subTest(current=current):
                self.assertTrue(monitor().continuity_error(previous, current))

    def test_host_replacement_and_sequence_regression_are_invalid(self):
        module = monitor()
        baseline = dict(session='one', boot='boot', sequence=1, wall=100,
                        interrupted=False, power_events_readable=True)
        good = dict(baseline, sequence=2, wall=105)
        self.assertEqual(module.host_error(baseline, good, 105), '')
        for changed in [dict(good, session='two'), dict(good, boot='two'),
                        dict(good, sequence=0), dict(good, wall=0),
                        dict(good, interrupted=True), dict(good, power_events_readable=False)]:
            self.assertTrue(module.host_error(baseline, changed, 105))

    def test_failed_stop_sentinel_cannot_suppress_systemctl_stop(self):
        from unittest import mock
        module = monitor()
        with mock.patch.object(module.Path, 'touch', side_effect=PermissionError('disk')):
            with mock.patch.object(module, 'command', return_value='ActiveState=inactive\\nMainPID=0\\n') as run:
                errors = module.stop_service(Path('sentinel'))
        self.assertTrue(errors)
        self.assertIn(mock.call('systemctl', 'stop', module.UNIT), run.call_args_list)
        self.assertIn(mock.call('systemctl', 'show', module.UNIT, '--property=ActiveState,MainPID'), run.call_args_list)

    def test_soak_cannot_run_before_explicit_integration_acceptance(self):
        with self.assertRaises(ValueError):
            monitor().require_integration_acceptance('soak', None)
        with self.assertRaises(ValueError):
            monitor().require_integration_acceptance('soak', {'verdict':'PENDING_ACCEPTANCE'})
        monitor().require_integration_acceptance('integration', None)

    def run_observer(self, *, fail_start=False):
        """Run real observer loop with synthetic clocks/systemd; never start service."""
        from contextlib import ExitStack
        import json
        import subprocess
        import tempfile
        from unittest import mock
        module = monitor()
        now = [100.0]
        saved = {}
        active = [False]
        def advance(seconds):
            now[0] += seconds
        def read(path, *args, **kwargs):
            if str(path).endswith('paper.env'):
                return ('MODE=PAPER_ONLY\nMVP_DB_PATH=' + str(module.STATE / 'paper.sqlite')
                        + '\nMVP_STATE_DIR=' + str(module.STATE / 'state'))
            if str(path).endswith('host.json'):
                return json.dumps(dict(session='one', boot='host', sequence=int(now[0]), wall=now[0],
                                       interrupted=False, power_events_readable=True))
            if path.name == 'loadavg':
                return '0 0 0 1/100 1'
            if path.name == 'memory.events':
                return 'oom 0\noom_kill 0\nmax 0\n'
            raise AssertionError(str(path))
        def command(*args):
            if args[:2] == ('systemctl', 'start'):
                active[0] = True
                if fail_start:
                    raise subprocess.TimeoutExpired(args, 45)
            if '--property=Restart' in args:
                return 'Restart=no\n'
            if args[0] == 'busctl':
                return 't 3720000000'
            return ''
        def properties():
            return dict(ActiveState='active' if active[0] else 'inactive', MainPID='123',
                        InvocationID='invocation', NRestarts='0', ControlGroup='/test')
        def backup(path):
            if path.name == 'before.sqlite':
                advance(10)
        database = mock.Mock()
        def execute(sql):
            row = ('ok',) if 'quick_check' in sql else (int(now[0] * 1e6), 'RUNNING', int(now[0]), 'run')
            return mock.Mock(fetchone=lambda: row)
        database.execute.side_effect = execute
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            output = Path(folder) / 'evidence'
            stack.enter_context(mock.patch.object(module.sys if hasattr(module, 'sys') else __import__('sys'), 'argv',
                ['paper-validation.py', 'integration', '--seconds', '3600', '--output', str(output), '--host-heartbeat', str(Path(folder) / 'host.json')]))
            stack.enter_context(mock.patch.object(module.Path, 'read_text', read))
            stack.enter_context(mock.patch.object(module, 'clock', side_effect=lambda: dict(wall=now[0], mono=now[0], boot='guest')))
            stack.enter_context(mock.patch.object(module.time, 'time', side_effect=lambda: now[0]))
            stack.enter_context(mock.patch.object(module.time, 'monotonic', side_effect=lambda: now[0]))
            stack.enter_context(mock.patch.object(module.time, 'sleep', side_effect=advance))
            stack.enter_context(mock.patch.object(module, 'code_hash', return_value='hash'))
            stack.enter_context(mock.patch.object(module, 'command', side_effect=command))
            stack.enter_context(mock.patch.object(module, 'properties', side_effect=properties))
            stack.enter_context(mock.patch.object(module, 'backup', side_effect=backup))
            stack.enter_context(mock.patch.object(module.sqlite3, 'connect', return_value=database))
            stack.enter_context(mock.patch.object(module.os, 'statvfs', create=True, return_value=mock.Mock(f_bavail=1000, f_frsize=4096)))
            stack.enter_context(mock.patch.object(module, 'write_json', side_effect=lambda path, value: saved.update({path.name: dict(value)})))
            stop = stack.enter_context(mock.patch.object(module, 'stop_service', return_value=[]))
            exit_code = module.main()
        return saved['result.json'], exit_code, stop.call_count

    def test_setup_delay_cannot_count_as_service_runtime(self):
        result, code, stops = self.run_observer()
        self.assertEqual(code, 0)
        self.assertTrue(result['continuous'])
        self.assertGreaterEqual(result['ended_wall'] - 110, 3600)
        self.assertEqual(stops, 1)

    def test_timed_out_start_still_stops_service(self):
        result, code, stops = self.run_observer(fail_start=True)
        self.assertEqual(code, 1)
        self.assertFalse(result['continuous'])
        self.assertEqual(stops, 1)

    def test_runtime_bound_must_match_requested_window(self):
        from unittest import mock
        module = monitor()
        for bound in [0, 10, 10 * 365 * 86400, 3600 + 121]:
            with self.subTest(bound=bound), mock.patch.object(module, 'command', side_effect=[
                    'Restart=no\n', 't ' + str(bound * 1_000_000)]):
                with self.assertRaises(RuntimeError):
                    module.require_runtime_bound(3600)
        with mock.patch.object(module, 'command', side_effect=['Restart=no\n', 't 3720000000']):
            module.require_runtime_bound(3600)

    def test_approval_hash_excludes_only_phase_runtime_but_records_full_unit(self):
        from unittest import mock
        module = monitor()
        unit = '[Service]\nRestart=no\nRuntimeMaxSec=3720\nMemoryMax=512M\n'
        with mock.patch.object(module, 'command', return_value=unit), mock.patch.object(module.Path, 'read_bytes', return_value=b'config'):
            integration = module.code_hash()
        with mock.patch.object(module, 'command', return_value=unit.replace('3720', '86520')), mock.patch.object(module.Path, 'read_bytes', return_value=b'config'):
            self.assertEqual(module.code_hash(), integration)
        for changed in [unit.replace('512M', '1G'), unit.replace('Restart=no', 'Restart=always')]:
            with mock.patch.object(module, 'command', return_value=changed), mock.patch.object(module.Path, 'read_bytes', return_value=b'config'):
                self.assertNotEqual(module.code_hash(), integration)

    def test_phase_duration_cannot_be_unbounded(self):
        for seconds in [0, 3601, 86401]:
            with self.assertRaises(ValueError):
                monitor().validate_duration('integration', seconds)
        monitor().validate_duration('integration', 3600)
        monitor().validate_duration('soak', 86400)


if __name__ == '__main__':
    unittest.main()
