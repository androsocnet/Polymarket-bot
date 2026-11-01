"""Focused safety checks; no systemd or privileged side effects."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).with_name('check_phase2b_vps.py')


class HarnessSafety(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PATH.exists(), 'separate portable harness missing')
        spec = importlib.util.spec_from_file_location('portable_probe', PATH)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_absolute_checkout_required(self):
        with self.assertRaisesRegex(ValueError, 'absolute'):
            self.m.frozen_source(Path('.'))

    def test_staged_controller_mismatch_refused_before_import(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'controller.py').write_text('raise RuntimeError("executed unverified code")')
            with patch.object(self.m, 'root_path', return_value=root):
                with self.assertRaisesRegex(ValueError, 'staged controller SHA256'):
                    self.m.load_controller('a' * 32)

    def test_source_mismatch_refused_before_execution(self):
        with tempfile.TemporaryDirectory() as td, patch.object(self.m, 'git_bytes', return_value=b'raise SystemExit("unsafe")'):
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                self.m.frozen_source(Path(td))

    def test_exact_allowlist_rejects_production_and_prefix_siblings(self):
        names = self.m.unit_names('a' * 32)
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.m.allowed_unit('a' * 32, name)
        for name in ('polymarket-btc-infrastructure.service', names[0] + '-evil', '*', '--all'):
            with self.assertRaises(ValueError):
                self.m.allowed_unit('a' * 32, name)
        for token in ('../bad', 'a' * 31, 'G' * 32):
            with self.assertRaises(ValueError):
                self.m.unit_names(token)

    def test_target_platform_strict_and_wsl_explicit(self):
        self.m.platform_check({'ID': 'ubuntu', 'VERSION_ID': '24.04'}, '255', False, False)
        for release, version, wsl, dev in [({'ID': 'debian'}, '255', False, False),
                ({'ID': 'ubuntu', 'VERSION_ID': '24.04'}, '259', False, False),
                ({'ID': 'ubuntu', 'VERSION_ID': '24.04'}, '255', True, False),
                ({'ID': 'ubuntu', 'VERSION_ID': '24.04'}, '255', False, True)]:
            with self.assertRaises(ValueError):
                self.m.platform_check(release, version, wsl, dev)
        self.m.platform_check({'ID': 'ubuntu'}, '259', True, True)

    def test_missing_unit_detection_fails_closed_on_manager_error(self):
        import subprocess
        name = self.m.unit_names('a' * 32)[1]
        with patch.object(self.m, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'bus unavailable')):
            with self.assertRaisesRegex(ValueError, 'LoadState'):
                self.m.unit_absent('a' * 32, name)
        with patch.object(self.m, 'run', return_value=subprocess.CompletedProcess([], 0, 'not-found\n', '')):
            self.assertTrue(self.m.unit_absent('a' * 32, name))
        with patch.object(self.m, 'run', return_value=subprocess.CompletedProcess([], 0, 'loaded\n', '')):
            self.assertFalse(self.m.unit_absent('a' * 32, name))

    def test_units_are_run_only_inert_network_isolated_and_bounded(self):
        definitions = self.m.definitions('a' * 32)
        names = self.m.unit_names('a' * 32)
        self.assertTrue(set(definitions) < set(names))
        self.assertIn(self.m.base('a' * 32) + '-observer.service', definitions)
        self.assertIn(self.m.base('a' * 32) + '-startup.timer', definitions)
        text = '\n'.join(definitions.values())
        self.assertNotIn('/opt/', text)
        self.assertNotIn('/var/lib/', text)
        self.assertNotIn('EnvironmentFile=', text)
        self.assertIn('PrivateNetwork=yes', text)
        self.assertIn('ExecStartPre=!/usr/bin/python3 -I -B', text)
        self.assertIn('RuntimeMaxSec=2040', text)
        timer = definitions[self.m.base('a' * 32) + '-cleanup.timer']
        self.assertNotIn('OnActiveSec=', timer)
        self.assertIn('OnBootSec=', timer)
        self.assertIn('RemainAfterElapse=no', timer)
        self.assertIn('/usr/bin/timeout --signal=TERM --kill-after=10 120', text)


if __name__ == '__main__':
    unittest.main()
