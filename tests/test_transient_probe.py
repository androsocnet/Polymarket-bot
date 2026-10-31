"""Offline safety regressions for transient service failover probe."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('check_transient_compat.py')


class TransientSafetyTests(unittest.TestCase):
    def test_probe_exists(self):
        self.assertTrue(SCRIPT.exists(), 'check_transient_compat.py must exist')

    def load(self):
        spec = importlib.util.spec_from_file_location('transient_probe', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_missing_acknowledgement_fails_closed(self):
        if not SCRIPT.exists():
            self.skipTest('probe not implemented yet')
        result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--disposable-only', result.stderr)

    def test_cleanup_rejects_foreign_unit(self):
        if not SCRIPT.exists():
            self.skipTest('probe not implemented yet')
        m = self.load()
        with patch.object(m, 'run') as mock_run:
            with self.assertRaises(ValueError):
                m.cleanup(['polymarket-btc-infrastructure.service'])
        mock_run.assert_not_called()

    def test_cleanup_attempts_all_units_on_error(self):
        if not SCRIPT.exists():
            self.skipTest('probe not implemented yet')
        m = self.load()
        calls = []
        def mock_cmd(*args, **kw):
            calls.append(args)
            if args[1] == 'stop' and 'orig' in args[2]:
                raise subprocess.TimeoutExpired(args, 1)
            return ''
        units = ['p2b-transient-' + 'a' * 32 + suffix for suffix in ('-orig.service', '-repl.service')]
        with patch.object(m, 'run', side_effect=mock_cmd), patch.object(m, 'empty', return_value=True):
            errors = m.cleanup(units)
        self.assertTrue(errors)
        self.assertTrue(any(args[:2] == ('systemctl', 'stop') and args[2] == units[1] for args in calls))
        self.assertTrue(any('reset-failed' in args for args in calls))

    def test_cgroup_empty_fails_closed_when_events_missing(self):
        if not SCRIPT.exists():
            self.skipTest('probe not implemented yet')
        m = self.load()
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(FileNotFoundError):
                m.cgroup_empty(Path(temp_dir))

    def test_fail_closed_on_unverified_identity(self):
        if not SCRIPT.exists():
            self.skipTest('probe not implemented yet')
        m = self.load()
        with self.assertRaises(ValueError):
            m.emergency_stop('p2b-transient-wrong-name.service', expected_token='valid_token')


if __name__ == '__main__':
    unittest.main()
