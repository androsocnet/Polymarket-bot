"""Per-invocation integration: destructive targets never come from unit names."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('transient_trial', Path(__file__).parents[1] / 'deploy/infrastructure-trial.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

TRIAL = 'pilot-20261031T000000Z'


class TestTemplateInvocationNaming(unittest.TestCase):
    def test_template_unit_constants(self):
        self.assertEqual(m.TEMPLATE_UNIT, 'polymarket-btc-infrastructure@.service')
        self.assertEqual(m.INSPECTION_UNIT, 'polymarket-btc-infrastructure@default.service')

    def test_invocation_unit_format_and_uniqueness(self):
        units = [m.invocation_unit(TRIAL, role) for role in ('main', 'main', 'verify')]
        self.assertEqual(len(set(units)), 3)
        for unit in units:
            self.assertRegex(unit, r'^polymarket-btc-infrastructure@pilot-20261031T000000Z-(main|verify)-[0-9a-f]{8}\.service$')

    def test_invocation_unit_validation(self):
        for trial, role in [('invalid', 'main'), (TRIAL, 'invalid')]:
            with self.assertRaises(ValueError):
                m.invocation_unit(trial, role)


class TestRecordMainShutdown(unittest.TestCase):
    def shutdown(self, mutation=None):
        data = {'trial_id': TRIAL, 'boot_id': 'boot'}
        unit = m.invocation_unit(TRIAL, 'main')
        timing = {'unit': unit, 'invocation_id': 'original', 'invocation_cgroup': '/original',
                  'runtime_expiry_mono': 9999}
        pre = {'unit': unit, 'properties': {'ActiveState': 'active', 'InvocationID': 'original',
               'ControlGroup': '/original'}, 'pids': [123]}
        post = {'unit': unit, 'properties': {'ActiveState': 'inactive', 'SubState': 'dead',
                'MainPID': '0', 'ControlPID': '0', 'Result': 'success', 'InvocationID': ''}, 'pids': []}
        if mutation == 'replacement':
            pre['properties']['InvocationID'] = 'replacement'
        if mutation == 'missing':
            pre['properties']['InvocationID'] = ''
        if mutation == 'cgroup':
            timing['invocation_cgroup'] = '/replacement'
        with tempfile.TemporaryDirectory() as td:
            with patch.object(m, 'monotonic_now', return_value=100), \
                 patch.object(m, 'unit_evidence', side_effect=[pre, post]), \
                 patch.object(m, 'validate_run_identity', return_value='run'), \
                 patch.object(m, 'registered_identity', return_value=dict(timing, invocation_cgroup='/original')), \
                 patch.object(m, 'cgroup_pids', return_value=[]), \
                 patch.object(m, 'terminate_invocation') as terminate, \
                 patch.object(m, 'stop_units') as stop:
                proof = m.record_main_shutdown(Path(td), data, timing)
                stop.assert_not_called()
                if mutation:
                    terminate.assert_not_called()
                    self.assertTrue(proof['errors'])
                else:
                    terminate.assert_called_once_with(timing)
                    self.assertEqual(proof['errors'], [])

    def test_original_shutdown_never_uses_name_stop(self):
        self.shutdown()

    def test_identity_mismatch_no_destructive_stop(self):
        self.shutdown('replacement')

    def test_missing_identity_no_destructive_stop(self):
        self.shutdown('missing')

    def test_timing_registration_cgroup_mismatch_no_destructive_stop(self):
        self.shutdown('cgroup')


class TestDeadlineFallback(unittest.TestCase):
    def test_corrupt_timing_uses_registered_cleanup_only(self):
        data = {'trial_id': TRIAL, 'units': m.owned(TRIAL), 'boot_id': 'boot'}
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            with patch.object(m, 'trial_metadata', return_value=data), \
                 patch.object(m, 'monotonic_now', return_value=100), \
                 patch.object(m, 'secure_bytes', side_effect=ValueError('corrupt timing')), \
                 patch.object(m, 'cancel_invocations', return_value=([], [])) as cleanup, \
                 patch.object(m, 'unit_evidence', return_value={}), \
                 patch.object(m, 'stop_units', return_value=([], [])) as stop, \
                 patch.object(m.subprocess, 'run'):
                self.assertEqual(m.deadline(folder), 1)
                cleanup.assert_called_once_with(folder, data)
                for call in stop.call_args_list:
                    self.assertTrue(set(call.args[0]) <= set(data['units']))
                self.assertTrue(json.loads((folder / 'deadline.json').read_text())['errors'])


if __name__ == '__main__':
    unittest.main()
