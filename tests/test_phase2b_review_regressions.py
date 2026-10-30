"""Independent review counterexamples. Offline only; no systemd operations."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_phase2b_lifecycle_fixes import (
    n, trial_data, timing, pre_unit, post_unit, full_audit_folder, run_audit,
    helper_evidence, run_stop_main,
)


class ReviewRegressions(unittest.TestCase):
    def test_heartbeat_invalid_records_and_missing_contract_fail(self):
        for mutation in ('nan', 'infinity', 'wrong-boot', 'wrong-session', 'gap',
                         'interrupted', 'missing-contract', 'truncated', 'missing-sequence'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                full_audit_folder(folder)
                data = trial_data()
                path = folder / 'host.json.jsonl'
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                if mutation == 'nan': rows[-1]['mono'] = float('nan')
                if mutation == 'infinity': rows[-1]['mono'] = float('inf')
                if mutation == 'wrong-boot': rows[-1]['boot'] = 'other'
                if mutation == 'wrong-session': rows[-1]['session'] = 'other'
                if mutation == 'gap': rows = [rows[0], rows[-1]]
                if mutation == 'interrupted': rows[-1]['interrupted'] = 1
                if mutation == 'missing-contract': data.pop('heartbeat_expiry_mono')
                if mutation == 'missing-sequence': rows[-1]['sequence'] = 999
                path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
                if mutation == 'truncated':
                    with path.open('a') as stream: stream.write('{')
                code, report = run_audit(folder, audit_data=data)
                self.assertEqual(code, 1, report)
                self.assertTrue(any('heartbeat' in error for error in report['errors']), report)

    def test_audit_does_not_certify_heartbeat_it_stopped(self):
        for mutation in ('active', 'sigterm', 'missing'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                full_audit_folder(folder)
                heartbeat = helper_evidence([trial_data()['units'][0]])[1][0]
                if mutation == 'active': heartbeat['properties'].update(ActiveState='active', MainPID='123')
                if mutation == 'sigterm': heartbeat['properties'].update(ExecMainCode='2', ExecMainStatus='15')
                if mutation == 'missing': heartbeat['properties']['LoadState'] = 'not-found'
                code, report = run_audit(folder, heartbeat_before=heartbeat)
                self.assertEqual(code, 1, report)

    def test_original_target_cgroup_remains_part_of_shutdown_proof(self):
        proof, _ = run_stop_main([pre_unit()], post_unit(), remaining_pids=[123])
        self.assertTrue(proof['errors'], proof)

    def test_prestop_evidence_exists_when_stop_begins_and_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            data = trial_data()
            (folder / 'restart-verification.json').write_text(json.dumps(restart_record()))
            snapshots = []
            def stopping(units):
                path = folder / 'shutdown-pre.json'
                self.assertTrue(path.exists(), 'pre-stop proof must be durable before controlled stop')
                snapshots.append(path.read_bytes())
                return [], [post_unit()]
            with patch.object(n, 'monotonic_now', return_value=2005.), \
                 patch.object(n, 'unit_evidence', return_value=pre_unit()), \
                 patch.object(n, 'secure_bytes', side_effect=lambda p: p.read_bytes()), \
                 patch.object(n, 'stop_units', side_effect=stopping):
                first = n.stop_main(folder, data, timing())
                original = (folder / 'shutdown.json').read_bytes()
                second = n.stop_main(folder, data, timing())
            self.assertEqual(first, second)
            self.assertEqual((folder / 'shutdown.json').read_bytes(), original)
            self.assertEqual(len(snapshots), 1, 'retry must not issue second stop or replace proof')

    def test_saved_shutdown_rejects_invalid_unit_boot_active_state_and_run_binding(self):
        for mutation in ('inactive-pre', 'wrong-pre-unit', 'wrong-post-unit', 'wrong-boot',
                         'empty-invocation', 'wrong-run', 'missing-restart', 'wrong-restart-invocation'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                full_audit_folder(folder)
                shutdown = json.loads((folder / 'shutdown.json').read_text())
                result = json.loads((folder / 'result.json').read_text())
                contract = timing()
                if mutation == 'inactive-pre': shutdown['pre_states'][0]['properties']['ActiveState'] = 'inactive'
                if mutation == 'wrong-pre-unit': shutdown['pre_states'][0]['unit'] = 'other.service'
                if mutation == 'wrong-post-unit': shutdown['units'][0]['unit'] = 'other.service'
                if mutation == 'wrong-boot': shutdown['boot_id'] = 'other'
                if mutation == 'empty-invocation':
                    contract['invocation_id'] = ''
                    shutdown['pre_states'][0]['properties']['InvocationID'] = ''
                if mutation == 'wrong-run': result['run_id'] = 'unrelated'
                if mutation == 'missing-restart':
                    (folder / 'restart-verification.json').unlink(missing_ok=True)
                if mutation == 'wrong-restart-invocation':
                    restart = restart_record()
                    restart['second']['InvocationID'] = 'other'
                    (folder / 'restart-verification.json').write_text(json.dumps(restart))
                (folder / 'timing.json').write_text(json.dumps(contract))
                (folder / 'shutdown.json').write_text(json.dumps(shutdown))
                (folder / 'result.json').write_text(json.dumps(result))
                code, report = run_audit(folder)
                self.assertEqual(code, 1, report)


    def test_saved_invalid_shutdown_cannot_pass_observer(self):
        from tests.test_phase2b_lifecycle_fixes import observe_full_window
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            invalid = dict(errors=[], boot_id='other', run_id='unrelated', runtime_fallback=True,
                           stop_request_mono=1., quiescence_mono=2., units=[post_unit()])
            (folder / 'shutdown.json').write_text(json.dumps(invalid))
            original = (folder / 'shutdown.json').read_bytes()
            code, result, _ = observe_full_window(folder)
            self.assertEqual(code, 1, result)
            self.assertEqual((folder / 'shutdown.json').read_bytes(), original)

    def test_deadline_nested_shutdown_must_match_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder)
            path = folder / 'deadline-stop.json'
            proof = json.loads(path.read_text())
            proof['shutdown'] = {'errors': ['different failed proof']}
            path.write_text(json.dumps(proof))
            code, report = run_audit(folder)
            self.assertEqual(code, 1, report)

    def test_activation_bound_to_restart_and_finite(self):
        for activation in (300., float('inf')):
            with self.subTest(activation=activation), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                full_audit_folder(folder)
                contract = timing()
                contract.update(activation_mono=activation, runtime_expiry_mono=activation + 2040)
                (folder / 'timing.json').write_text(json.dumps(contract))
                code, report = run_audit(folder)
                self.assertEqual(code, 1, report)


def restart_record():
    second = copy.deepcopy(pre_unit()['properties'])
    second.update(NRestarts='0', ActiveEnterTimestampMonotonic='200000000')
    return dict(trial_id=trial_data()['trial_id'], errors=[], first_run_id='first',
                continuous_run_id='continuous', second=second)


if __name__ == '__main__':
    unittest.main()
