"""Targeted regressions for per-invocation cgroup-based emergency shutdown integration."""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

TRIAL_PATH = Path(__file__).parents[1] / 'deploy/infrastructure-trial.py'
spec = importlib.util.spec_from_file_location('infrastructure_trial', TRIAL_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _trial_data(trial_id='pilot-20261031T000000Z', seconds=1800):
    return {
        'trial_id': trial_id, 'seconds': seconds, 'boot_id': 'testboot',
        'fingerprint': 'fp', 'launch_mono': time.monotonic() - 10,
        'startup_expiry_mono': time.monotonic() + 200,
        'units': module.owned(trial_id),
    }


def _fake_service(invocation_id='iid123', cgroup='/system.slice/polymarket-btc-infrastructure.service'):
    return {
        'ActiveEnterTimestampMonotonic': str(int((time.monotonic() - 5) * 1e6)),
        'InvocationID': invocation_id,
        'ControlGroup': cgroup,
    }


class TestInvocationCgroupCapture(unittest.TestCase):
    def _timing_from_service(self, data, service):
        """Helper: call start_observation_timing with mocked deps."""
        runtime_usec = (data['seconds'] + module.RUNTIME_MAX_PADDING) * 1_000_000
        with patch.object(module, 'monotonic_now', return_value=time.monotonic()), \
             patch.object(module, 'runtime_bound', side_effect=[runtime_usec, 0]), \
             patch.object(module, 'arm_timer', return_value={'ok': True}):
            return module.start_observation_timing(Path(self.td), data, service)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.td = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_timing_includes_invocation_cgroup(self):
        data = _trial_data()
        service = _fake_service()
        timing = self._timing_from_service(data, service)
        self.assertIn('invocation_cgroup', timing)
        self.assertEqual(timing['invocation_cgroup'],
                         '/system.slice/polymarket-btc-infrastructure.service')
        self.assertEqual(timing['invocation_id'], 'iid123')

    def test_timing_persisted_invocation_cgroup(self):
        data = _trial_data()
        service = _fake_service()
        timing = self._timing_from_service(data, service)
        saved = json.loads((Path(self.td) / 'timing.json').read_text())
        self.assertIn('invocation_cgroup', saved)

    def test_timing_rejects_empty_cgroup(self):
        data = _trial_data()
        service = _fake_service(cgroup='')
        with self.assertRaises(ValueError):
            self._timing_from_service(data, service)


class TestInvocationCgroupKill(unittest.TestCase):
    def test_kill_writes_to_cgroup_kill(self):
        with tempfile.TemporaryDirectory() as td:
            cgroup_rel = 'system.slice/polymarket-btc-infrastructure.service'
            cgroup_dir = Path(td) / cgroup_rel
            cgroup_dir.mkdir(parents=True)
            kill_file = cgroup_dir / 'cgroup.kill'
            kill_file.write_text('0')
            timing = {'invocation_id': 'abc',
                      'invocation_cgroup': '/' + cgroup_rel}
            with patch.object(module, 'CGROUP_ROOT', Path(td)):
                module.invocation_cgroup_kill('/' + cgroup_rel, 'abc', timing)
            self.assertEqual(kill_file.read_text(), '1')

    def test_kill_fails_closed_on_id_mismatch(self):
        timing = {'invocation_id': 'correct_id',
                  'invocation_cgroup': '/system.slice/x.service'}
        with self.assertRaises(ValueError):
            module.invocation_cgroup_kill('/system.slice/x.service', 'wrong_id', timing)

    def test_kill_handles_missing_cgroup_gracefully(self):
        timing = {'invocation_id': 'abc',
                  'invocation_cgroup': '/system.slice/gone.service'}
        with patch.object(module, 'CGROUP_ROOT', Path('/nonexistent_xyz')):
            # Should not raise; process already gone (cgroup absent = dead)
            module.invocation_cgroup_kill('/system.slice/gone.service', 'abc', timing)


class TestRecordMainShutdownCgroupTargeting(unittest.TestCase):
    CGROUP = '/system.slice/polymarket-btc-infrastructure.service'

    def _shutdown(self, timing, pre_invocation='iid', active=True):
        data = _trial_data()
        call_order = []
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            props_val = {
                'ActiveState': 'active' if active else 'inactive',
                'InvocationID': pre_invocation,
                'ControlGroup': self.CGROUP,
                'MainPID': '1234', 'ControlPID': '0',
                'Result': '', 'LoadState': 'loaded',
                'SubState': 'running' if active else 'dead',
            }
            dead_evidence = [{
                'unit': module.UNIT,
                'properties': {
                    'ActiveState': 'inactive', 'Result': 'success',
                    'InvocationID': '', 'MainPID': '0', 'ControlPID': '0',
                    'ControlGroup': '', 'SubState': 'dead',
                },
                'pids': [],
            }]
            with patch.object(module, 'unit_evidence',
                              return_value={'unit': module.UNIT,
                                           'properties': props_val, 'pids': []}), \
                 patch.object(module, 'validate_run_identity', return_value='run1'), \
                 patch.object(module, 'monotonic_now', return_value=100.0), \
                 patch.object(module, 'invocation_cgroup_kill',
                              side_effect=lambda *a, **kw: call_order.append('cgroup_kill')), \
                 patch.object(module, 'stop_units',
                              side_effect=lambda units: (
                                  call_order.append('stop_units'), ([], dead_evidence))[1]), \
                 patch.object(module, 'cgroup_pids', return_value=[]), \
                 patch.object(module, 'quiescent', return_value=True):
                proof = module.record_main_shutdown(folder, data, timing)
        return proof, call_order

    def test_shutdown_uses_cgroup_kill_before_unit_stop(self):
        timing = {
            'invocation_id': 'iid', 'invocation_cgroup': self.CGROUP,
            'baseline_mono': 0.0, 'runtime_expiry_mono': 9999.0,
        }
        proof, order = self._shutdown(timing)
        self.assertEqual(order, ['cgroup_kill', 'stop_units'],
                         'cgroup kill must precede stop_units')

    def test_shutdown_fail_closed_on_invocation_mismatch(self):
        timing = {
            'invocation_id': 'expected_iid', 'invocation_cgroup': self.CGROUP,
            'baseline_mono': 0.0, 'runtime_expiry_mono': 9999.0,
        }
        # pre_invocation differs from timing['invocation_id']
        proof, order = self._shutdown(timing, pre_invocation='DIFFERENT_iid')
        self.assertNotIn('cgroup_kill', order,
                         'cgroup kill must NOT run when invocation mismatch')
        self.assertTrue(proof['errors'], 'errors must be non-empty on id mismatch')

    def test_shutdown_no_cgroup_kill_when_pre_stop_errors(self):
        """If pre-stop identity unavailable, cgroup_kill must not execute."""
        timing = {
            'invocation_id': 'iid', 'invocation_cgroup': self.CGROUP,
            'baseline_mono': 0.0, 'runtime_expiry_mono': 9999.0,
        }
        data = _trial_data()
        call_order = []
        dead_evidence = [{'unit': module.UNIT, 'properties': {
            'ActiveState': 'inactive', 'Result': 'success',
            'InvocationID': '', 'MainPID': '0', 'ControlPID': '0',
            'ControlGroup': '', 'SubState': 'dead',
        }, 'pids': []}]
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            with patch.object(module, 'unit_evidence',
                              side_effect=OSError('unit unavailable')), \
                 patch.object(module, 'validate_run_identity', return_value='run1'), \
                 patch.object(module, 'monotonic_now', return_value=100.0), \
                 patch.object(module, 'invocation_cgroup_kill',
                              side_effect=lambda *a, **kw: call_order.append('cgroup_kill')), \
                 patch.object(module, 'stop_units',
                              side_effect=lambda units: (
                                  call_order.append('stop_units'), ([], dead_evidence))[1]), \
                 patch.object(module, 'cgroup_pids', return_value=[]), \
                 patch.object(module, 'quiescent', return_value=True):
                proof = module.record_main_shutdown(folder, data, timing)
        self.assertNotIn('cgroup_kill', call_order)
        self.assertTrue(proof['errors'])


class TestDeadlineFallbackCgroupKill(unittest.TestCase):
    CGROUP = '/system.slice/polymarket-btc-infrastructure.service'

    def test_deadline_fallback_uses_cgroup_kill(self):
        timing = {
            'invocation_id': 'iid', 'invocation_cgroup': self.CGROUP,
            'baseline_mono': 0.0, 'deadline_mono': 100.0,
            'runtime_expiry_mono': 9999.0, 'fingerprint': 'fp', 'boot_id': 'b',
        }
        kill_called = []
        data = _trial_data()
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / 'timing.json').write_text(json.dumps(timing))
            with patch.object(module, 'trial_metadata', return_value=data), \
                 patch.object(module, 'monotonic_now', return_value=100.5), \
                 patch.object(module, 'secure_bytes',
                              return_value=json.dumps(timing).encode()), \
                 patch.object(module, 'validate_deadline'), \
                 patch.object(module, 'stop_main',
                              side_effect=RuntimeError('forced fallback')), \
                 patch.object(module, 'invocation_cgroup_kill',
                              side_effect=lambda *a, **kw: kill_called.append(True)), \
                 patch.object(module, 'stop_units', return_value=([], [])), \
                 patch.object(module, 'unit_evidence',
                              return_value={'unit': module.UNIT, 'properties': {
                                  'ActiveState': 'inactive', 'Result': 'success',
                                  'LoadState': 'loaded', 'MainPID': '0', 'ControlPID': '0',
                                  'ControlGroup': '', 'InvocationID': '',
                              }, 'pids': []}), \
                 patch.object(module, 'write_json'), \
                 patch.object(module, 'validate_timing'), \
                 patch.object(module, 'validate_shutdown_records'):
                module.deadline(folder)
        self.assertTrue(kill_called,
                        'deadline fallback must call invocation_cgroup_kill')


if __name__ == '__main__':
    unittest.main()
