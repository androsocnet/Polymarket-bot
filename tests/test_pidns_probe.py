"""Offline safety regressions; no systemd actions."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('check_pidns_compat.py')


class SafetyTests(unittest.TestCase):
    def test_probe_exists(self):
        self.assertTrue(SCRIPT.exists(), 'self-contained compatibility probe missing')

    def load(self):
        spec = importlib.util.spec_from_file_location('pidns_probe', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_cleanup_attempts_all_units_after_first_error(self):
        if not SCRIPT.exists():
            self.skipTest('probe not yet implemented')
        m = self.load()
        calls = []
        def command(*args, **kw):
            calls.append(args)
            if args[1] == 'stop' and args[2].endswith('-probe.service'):
                raise subprocess.TimeoutExpired(args, 1)
            return ''
        units = ['p2b-pidns-' + 'a' * 32 + suffix for suffix in ('-probe.service', '-canary.service')]
        with patch.object(m, 'run', side_effect=command), patch.object(m, 'empty', return_value=True):
            errors = m.cleanup(units)
        self.assertTrue(errors)
        self.assertTrue(any(args[:2] == ('systemctl', 'stop') and args[2] == units[1] for args in calls))
        self.assertTrue(any('reset-failed' in args for args in calls))

    def test_cleanup_rejects_foreign_unit_before_commands(self):
        if not SCRIPT.exists():
            self.skipTest('probe not yet implemented')
        m = self.load()
        with patch.object(m, 'run') as command:
            with self.assertRaises(ValueError):
                m.cleanup(['polymarket-btc-infrastructure.service'])
        command.assert_not_called()

    def test_permission_denial_never_retries_as_root(self):
        if not SCRIPT.exists():
            self.skipTest('probe not yet implemented')
        m = self.load()
        baseline = {'NoNewPrivs': '1', 'CapEff': '0', 'CapPrm': '0', 'CapBnd': '0'}
        def denied(procfd, mapped):
            return [sys.executable, '-c',
                    "import sys; sys.stderr.write('unshare: Operation not permitted\\n'); sys.exit(1)"]
        with patch.object(m.os, 'getuid', return_value=1000), \
             patch.object(m, 'status', return_value=baseline), \
             patch.object(m, 'namespace_command', side_effect=denied) as command:
            self.assertEqual(m.worker(), 2)
        self.assertEqual([call.args[1] for call in command.call_args_list], [False, True])

    def test_existing_cgroup_without_events_is_not_empty(self):
        m = self.load()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                m.cgroup_empty(Path(directory))

    def test_populated_retained_cgroup_is_not_ignored_after_unload(self):
        m = self.load()
        unit = 'p2b-pidns-' + 'a' * 32 + '-probe.service'
        state = dict(ControlGroup='', MainPID='0', ControlPID='0', ActiveState='inactive')
        with patch.object(m, 'properties', return_value=state), \
             patch.object(m, 'cgroup_empty', return_value=False) as check:
            self.assertFalse(m.empty(unit, '/system.slice/' + unit))
        check.assert_called_once()

    def test_optimized_worker_refuses_host_root(self):
        code = ("import importlib.util; from unittest.mock import patch; "
                f"s=importlib.util.spec_from_file_location('probe', {str(SCRIPT)!r}); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "p=patch.object(m.os, 'getuid', return_value=0); p.start(); m.worker()")
        result = subprocess.run([sys.executable, '-O', '-c', code],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must not run as host root', result.stderr)

    def test_missing_acknowledgement_has_no_service_effect(self):
        if not SCRIPT.exists():
            self.skipTest('probe not yet implemented')
        result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--disposable-only', result.stderr)


if __name__ == '__main__':
    unittest.main()
