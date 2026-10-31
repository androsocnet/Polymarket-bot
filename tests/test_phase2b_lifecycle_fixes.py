"""Phase 2B lifecycle/evidence defect fixes: shutdown identity proof + heartbeat full lifetime.

Historical failure (pilot-20261030T013400Z): post-stop InvocationID empty ->
observer finalization failed -> deadline reused failed proof -> deadline helper
stopped heartbeat before required T+2280 coverage -> host continuity failed ->
independent audit BLOCKED. These tests drive the RED/GREEN remediation.
All synthetic; no VPS, no real systemctl, no historical evidence touched.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lifecycle_trial', ROOT / 'deploy/infrastructure-trial.py')
n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)

TRIAL = 'pilot-20261030T013400Z'  # Historical failed trial ID.


def trial_data(seconds=1800, heartbeat_expiry=2380.):
    return {'trial_id': TRIAL, 'phase': 'pilot', 'seconds': seconds,
            'launch_at': {'utc': '2026-10-30T01:34:00Z'}, 'units': n.owned(TRIAL),
            'fingerprint': 'hash', 'boot_id': 'boot', 'launch_mono': 100.,
            'startup_expiry_mono': 460., 'heartbeat_expiry_mono': heartbeat_expiry,
            'audit_expiry_mono': 2925., 'deadline_after_seconds': seconds + 60}


def restart_record():
    return dict(trial_id=TRIAL, errors=[], first_run_id='first', continuous_run_id='continuous',
                second=dict(pre_unit()['properties'], NRestarts='0', ActiveEnterTimestampMonotonic='200000000'))


def timing():
    return {'fingerprint': 'hash', 'boot_id': 'boot', 'activation_mono': 200.,
            'baseline_mono': 205., 'deadline_mono': 2065., 'runtime_expiry_mono': 2240.,
            'invocation_id': 'inv', 'invocation_cgroup': 'g'}


def pre_unit(invocation='inv', active=True, unit=None):
    return {'unit': unit or n.UNIT, 'properties': {'ActiveState': 'active' if active else 'inactive',
            'SubState': 'running' if active else 'dead', 'MainPID': '123', 'ControlPID': '0',
            'InvocationID': invocation, 'ControlGroup': 'g'}, 'pids': ['123']}


def post_unit(invocation='', result='success', state='inactive', pids=()):
    return {'unit': n.UNIT, 'properties': {'ActiveState': state,
            'SubState': 'dead' if state == 'inactive' else 'running', 'MainPID': '0',
            'ControlPID': '0', 'Result': result, 'InvocationID': invocation}, 'pids': list(pids)}


def run_stop_main(prestop, poststop, timing_contract=None, requested=2005., completed=2007.,
                  stop_effects=None, remaining_pids=()):
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        (folder / 'restart-verification.json').write_text(json.dumps(restart_record()))
        with patch.object(n, 'secure_bytes', side_effect=lambda p: p.read_bytes()), \
             patch.object(n, 'cgroup_pids', return_value=list(remaining_pids)), \
             patch.object(n, 'boot_id', return_value='boot'), \
             patch.object(n, 'unit_evidence', side_effect=prestop), \
             patch.object(n.time, 'monotonic', side_effect=[requested, completed]), \
             patch.object(n.time, 'clock_gettime', side_effect=[requested, completed]), \
             patch.object(n, 'stop_units', side_effect=stop_effects or
                          (lambda units: ([], [poststop]))):
            proof = n.stop_main(folder, trial_data(), timing_contract or timing())
        path = folder / 'shutdown.json'
        persisted = json.loads(path.read_text()) if path.exists() else None
    return proof, persisted


def observe_full_window(folder):
    """Run one complete 1800s observation with real stop_main against a temp folder."""
    from contextlib import ExitStack
    with ExitStack() as stack:
        (folder / 'db').touch()
        (folder / 'host.json.jsonl').touch()
        now = [205.]
        data = trial_data()

        state = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '123',
                 'ControlPID': '0', 'InvocationID': 'inv', 'NRestarts': '0',
                 'Result': 'success', 'ControlGroup': 'g',
                 'ActiveEnterTimestampMonotonic': '200000000'}
        states = iter([dict(state, ActiveState='inactive'), dict(state, InvocationID='first')])

        class DB:
            def execute(self, *args): return self
            def fetchone(self): return ('run', int(now[0] * 1e6), 'RUNNING')
            def fetchall(self): return [('ok',)]
            def close(self): pass

        values = dict(trial_metadata=data, check_approval=None, require_service_policy=None,
                      environment={'MVP_DB_PATH': str(folder / 'db'), 'MVP_STATE_DIR': str(folder),
                                   'MVP_BOT_ID': 'bot'},
                      snapshot={'trading': {}, 'observations': 1}, fresh_run=True,
                      read_host={}, hash_due=False, command='')
        for name, value in values.items():
            stack.enter_context(patch.object(n, name, return_value=value))
        stack.enter_context(patch.object(n, 'properties', side_effect=lambda u: next(states, dict(state))))
        stack.enter_context(patch.object(n, 'wait_run', side_effect=['first', 'run']))
        stack.enter_context(patch.object(n, 'boot_id', return_value='boot'))
        stack.enter_context(patch.object(n.time, 'monotonic', side_effect=lambda: now[0]))
        stack.enter_context(patch.object(n.time, 'clock_gettime', side_effect=lambda *a: now[0]))
        stack.enter_context(patch.object(n.time, 'time', side_effect=lambda: now[0]))
        stack.enter_context(patch.object(n.time, 'sleep', side_effect=lambda s: now.__setitem__(0, now[0] + s)))
        stack.enter_context(patch.object(n, 'connect', side_effect=lambda *a: DB()))
        stack.enter_context(patch.object(n, 'ingestion',
                                         side_effect=lambda *a: {'count': 1, 'last_received_us': int(now[0] * 1e6)}))
        stack.enter_context(patch.object(n, 'memory_events',
                                         return_value={'oom': '0', 'oom_kill': '0', 'max': '0'}))
        stack.enter_context(patch.object(n, 'runtime_bound',
                                         side_effect=lambda name='RuntimeMaxUSec':
                                             2040000000 if name == 'RuntimeMaxUSec' else 0))
        stack.enter_context(patch.object(n, 'arm_timer', return_value={'verified': True}))
        stack.enter_context(patch.object(n, 'unit_evidence', return_value=pre_unit()))
        stack.enter_context(patch.object(n, 'secure_bytes', side_effect=lambda p: p.read_bytes()))
        stack.enter_context(patch.object(n, 'cgroup_pids', return_value=[]))
        stop_calls = []
        def stopping(units):
            stop_calls.append(list(units))
            return [], [post_unit()]
        stack.enter_context(patch.object(n, 'stop_units', side_effect=stopping))
        code = n.observe(folder)
        result = json.loads((folder / 'result.json').read_text())
        shutdown_path = folder / 'shutdown.json'
        shutdown = json.loads(shutdown_path.read_text()) if shutdown_path.exists() else result
    return code, result, shutdown


def write_observer_artifacts(folder, shutdown_errors=False, prestop=True, coverage_mono=1868.):
    shutdown = dict(errors=['shutdown not proven before runtime backstop'] if shutdown_errors else [],
                    runtime_fallback=False, stop_request_mono=2005., quiescence_mono=2007.,
                    pre_states=[pre_unit()] if prestop else [], units=[post_unit()], boot_id='boot')
    result = dict(trial_id=TRIAL, continuous=True, observation_end_mono=2005.,
                  elapsed_seconds=1800., window_seconds=1800, run_id='continuous',
                  samples=180, ingestion={'count': 5}, errors=[], verdict='PENDING_REVIEW')
    (folder / 'timing.json').write_text(json.dumps(timing()))
    (folder / 'shutdown.json').write_text(json.dumps(shutdown))
    (folder / 'result.json').write_text(json.dumps(result))
    (folder / 'restart-verification.json').write_text(json.dumps(restart_record()))
    shutdown.update(trial_id=TRIAL, run_id='continuous')
    (folder / 'shutdown.json').write_text(json.dumps(shutdown))
    (folder / 'shutdown-pre.json').write_text(json.dumps(dict(
        trial_id=TRIAL, boot_id='boot', at_mono=2005., run_id='continuous',
        units=shutdown['pre_states'], errors=[])))
    return shutdown, result


def helper_evidence(units, override=None):
    items = []
    for u in units:
        state = dict(ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0',
                     Result='success', ExecMainStatus='0', ExecMainCode='1', LoadState='loaded')
        if override:
            state = override(u, state)
        items.append({'unit': u, 'properties': state, 'pids': []})
    return [], items


def write_host_records(folder, coverage_mono=1868., raw=None):
    records = [dict(session='s', boot='boot', sequence=i + 1, wall=100. + 5 * i, mono=100. + 5 * i,
                    boottime=110. + 5 * i, interrupted=0)
               for i in range(int((coverage_mono - 100.) / 5) + 1)]
    if records[-1]['mono'] < coverage_mono:
        records.append(dict(records[-1], sequence=len(records) + 1, wall=coverage_mono,
                            mono=coverage_mono, boottime=coverage_mono + 10))
    path = folder / 'host.json.jsonl'
    if raw is None:
        path.write_text(''.join(json.dumps(r) + '\n' for r in records))
    else:
        path.write_text(raw)
    return records


def run_audit(folder, audit_data=None, stopping=None, heartbeat_before=None,
              identity=('hash', {}), evidence_effect=None):
    if evidence_effect is not None:
        effect = evidence_effect
    elif heartbeat_before is not None:
        effect = lambda u: heartbeat_before if u == trial_data()['units'][0] else \
            helper_evidence([u])[1][0]
    else:
        effect = lambda u: helper_evidence([u])[1][0]
    with patch.object(n, 'unit_evidence', side_effect=effect), \
         patch.object(n, 'trial_metadata', return_value=audit_data or trial_data()), \
         patch.object(n, 'secure_bytes', side_effect=lambda p: p.read_bytes()), \
         patch.object(n, 'boot_id', return_value='boot'), \
         patch.object(n, 'stop_units', side_effect=stopping or
                      (lambda units: helper_evidence(units))), \
         patch.object(n, 'runtime_identity', return_value=identity):
        code = n.audit(folder)
    return code, json.loads((folder / 'cleanup.json').read_text())


AUDIT_SERVICE = 5   # Owned-units index of the running audit.service.
OBSERVER = 1
DEADLINE_SERVICE = 3
HEARTBEAT = 0


def running_audit_service(unit):
    """Real audit-service state while audit() executes (cannot capture itself)."""
    return {'unit': unit, 'properties': dict(
        LoadState='loaded', ActiveState='active', SubState='running', MainPID='99',
        ControlPID='0', Result='', ExecMainCode='0', ExecMainStatus='0',
        InvocationID='audit-service-inv'), 'pids': []}


def bind_deadline_observer_record(folder, data, state=None, trial_id=None, boot_id='boot',
                                  include_binding=True):
    """Persist bound observer pre-stop snapshot into deadline-stop.json (as deadline() does)."""
    proof = json.loads((folder / 'deadline-stop.json').read_text())
    if include_binding:
        proof['trial_id'] = trial_id if trial_id is not None else data['trial_id']
        proof['boot_id'] = boot_id
    proof['helper_exits'] = [state or retained_success(data['units'][OBSERVER])]
    (folder / 'deadline-stop.json').write_text(json.dumps(proof))
    return proof


def full_audit_folder(folder, coverage_mono=2385., prestop=True, shutdown_errors=False):
    """Complete pre-audit evidence set for a correct lifecycle (or mutated variant)."""
    write_observer_artifacts(folder, prestop=prestop, shutdown_errors=shutdown_errors)
    (folder / 'deadline.json').write_text(json.dumps({'at': '2026', 'units': [], 'errors': []}))
    shutdown = json.loads((folder / 'shutdown.json').read_text())
    (folder / 'deadline-stop.json').write_text(json.dumps(
        {'at': '2026', 'trigger_mono': 2066., 'units': [], 'shutdown': shutdown, 'errors': []}))
    (folder / 'startup-handoff.json').write_text(json.dumps({'at_mono': 400., 'errors': [], 'units': []}))
    write_host_records(folder, coverage_mono=coverage_mono)


class TestAShutdownIdentityProof(unittest.TestCase):
    """FIX 1: valid pre-stop InvocationID with empty post-stop InvocationID: ACCEPT."""

    def test_valid_prestop_with_empty_poststop_accepted(self):
        proof, persisted = run_stop_main([pre_unit()], post_unit())
        self.assertEqual(proof['errors'], [])
        self.assertFalse(proof['runtime_fallback'])
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted['pre_states'][0]['properties']['InvocationID'], 'inv')
        self.assertEqual(persisted['units'][0]['properties']['InvocationID'], '')
        self.assertFalse(persisted['runtime_fallback'])

    def test_validate_timing_accepts_empty_poststop_with_prestop_proof(self):
        shutdown = dict(errors=[], runtime_fallback=False, stop_request_mono=2005.,
                        quiescence_mono=2007., pre_states=[pre_unit()],
                        units=[post_unit()], boot_id='boot')
        n.validate_timing(trial_data(), timing(),
                          dict(continuous=True, observation_end_mono=2005., errors=[]), shutdown)

    def test_validate_timing_still_rejects_changed_poststop(self):
        shutdown = dict(errors=[], runtime_fallback=False, stop_request_mono=2005.,
                        quiescence_mono=2007., pre_states=[pre_unit()],
                        units=[post_unit(invocation='new')], boot_id='boot')
        with self.assertRaisesRegex(ValueError, 'identity'):
            n.validate_timing(trial_data(), timing(),
                              dict(continuous=True, observation_end_mono=2005., errors=[]), shutdown)


class TestBInvalidPrestopIdentityFails(unittest.TestCase):
    """FIX 1 point 8: missing, stale, mismatched or unverifiable PRE-STOP identity FAILs."""

    def test_missing_prestop_active_identity_fails(self):
        proof, _ = run_stop_main([pre_unit(invocation='', active=False)], post_unit(invocation='inv'))
        self.assertTrue(any('pre-stop' in e for e in proof['errors']))

    def test_mismatched_prestop_invocation_fails(self):
        proof, _ = run_stop_main([pre_unit(invocation='stale-inv')], post_unit(invocation='inv'))
        self.assertTrue(any('pre-stop identity mismatch' in e for e in proof['errors']))

    def test_unverifiable_prestop_identity_fails(self):
        proof, _ = run_stop_main(OSError('systemctl unavailable'), post_unit(invocation='inv'))
        self.assertTrue(any('pre-stop identity unavailable' in e for e in proof['errors']))

    def test_missing_timing_invocation_record_fails(self):
        contract = timing(); contract.pop('invocation_id')
        proof, _ = run_stop_main([pre_unit()], post_unit(invocation='inv'), timing_contract=contract)
        self.assertTrue(any('pre-stop' in e for e in proof['errors']))


class TestCWrongServiceOrRunIdentityFails(unittest.TestCase):
    """FIX 1 point 3: unit and run identity verified against existing trustworthy records."""

    def test_wrong_service_unit_prestop_fails(self):
        proof, _ = run_stop_main([pre_unit(unit='other.service')], post_unit())
        self.assertTrue(any('pre-stop' in e for e in proof['errors']))

    def test_wrong_run_identity_fails(self):
        contract = timing(); contract['invocation_id'] = 'other-run-inv'
        proof, _ = run_stop_main([pre_unit(invocation='inv')], post_unit(), timing_contract=contract)
        self.assertTrue(any('pre-stop identity mismatch' in e for e in proof['errors']))


class TestDShutdownRequirements(unittest.TestCase):
    """FIX 1 point 5: timeout, remaining cgroup processes, runtime fallback still FAIL."""

    def test_shutdown_timeout_fails(self):
        proof, _ = run_stop_main([pre_unit()], post_unit(result='timeout'))
        self.assertTrue(proof['runtime_fallback'])
        self.assertTrue(proof['errors'], 'timeout shutdown must fail')

    def test_remaining_cgroup_processes_fail(self):
        proof, _ = run_stop_main([pre_unit()], post_unit(pids=[9]))
        self.assertTrue(proof['errors'], 'remaining cgroup processes must fail')

    def test_poststop_invocation_change_fails(self):
        proof, _ = run_stop_main([pre_unit()], post_unit(invocation='new'))
        self.assertTrue(any('post-stop invocation identity changed' in e for e in proof['errors']))


class TestEFullObservationAndValidCleanup(unittest.TestCase):
    """Test E: full 1800s observation followed by valid cleanup completes PENDING_REVIEW."""

    def test_full_window_observer_finalization_accepts(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            code, result, shutdown = observe_full_window(folder)
            self.assertEqual(code, 0, result['errors'])
            self.assertEqual(result['verdict'], 'PENDING_REVIEW')
            self.assertTrue(result['continuous'])
            self.assertGreaterEqual(result['elapsed_seconds'], 1800)
            self.assertEqual(shutdown['pre_states'][0]['properties']['InvocationID'], 'inv')
            self.assertEqual(shutdown['units'][0]['properties']['InvocationID'], '')
            self.assertEqual(shutdown['errors'], [])
            # Pre-stop and post-stop snapshots both preserved in immutable evidence.
            self.assertIn('ControlPID', shutdown['pre_states'][0]['properties'])
            self.assertIn('MainPID', shutdown['units'][0]['properties'])


class TestFDeadlineNeverStopsHeartbeat(unittest.TestCase):
    """FIX 2 point 1: deadline at B+1860 must not terminate heartbeat before T+2280."""

    def test_deadline_does_not_stop_heartbeat_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_observer_artifacts(folder)
            write_host_records(folder, coverage_mono=1868.)
            stopped = []
            def stopping(units):
                record_path = folder / 'deadline-stop.json'
                self.assertTrue(record_path.exists(), 'observer proof must be durable before stop')
                record = json.loads(record_path.read_text())
                self.assertEqual(record['helper_exits'][0]['unit'], trial_data()['units'][1])
                self.assertEqual(record['trial_id'], TRIAL)
                self.assertTrue(record['errors'], 'in-progress deadline must not appear successful')
                stopped.append(list(units))
                return helper_evidence(units)
            with patch.object(n, 'trial_metadata', return_value=trial_data()), \
                 patch.object(n, 'monotonic_now', return_value=2066.), \
                 patch.object(n, 'secure_bytes', side_effect=lambda p: p.read_bytes()), \
                 patch.object(n, 'unit_evidence',
                              side_effect=lambda u: retained_success(u)), \
                 patch.object(n, 'boot_id', return_value='boot'), \
                 patch.object(n, 'stop_units', side_effect=stopping), \
                 patch.object(n.subprocess, 'run', return_value=None):
                self.assertEqual(n.deadline(folder), 0)
            heartbeat = trial_data()['units'][0]
            self.assertNotIn(heartbeat, [u for call in stopped for u in call])
            persisted = json.loads((folder / 'deadline-stop.json').read_text())
            self.assertNotIn(heartbeat, [item['unit'] for item in persisted['units']])
            # FIX 3: deadline persists the observer pre-stop snapshot, bound to trial/boot.
            self.assertEqual(persisted['helper_exits'][0]['unit'],
                             trial_data()['units'][1])
            self.assertEqual(persisted['helper_exits'][0]['properties']['ExecMainCode'], '1')
            self.assertEqual(persisted['trial_id'], TRIAL)

    def test_deadline_stops_active_main_without_observer_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_observer_artifacts(folder)
            (folder / 'shutdown.json').unlink()
            (folder / 'shutdown-pre.json').unlink()
            stopped = []
            def stopping(units):
                stopped.extend(units)
                return helper_evidence(units)
            with patch.object(n, 'trial_metadata', return_value=trial_data()), \
                 patch.object(n, 'monotonic_now', side_effect=[2066., 2066.1, 2067.]), \
                 patch.object(n, 'secure_bytes', side_effect=lambda p: p.read_bytes()), \
                 patch.object(n, 'unit_evidence', return_value=pre_unit()), \
                 patch.object(n, 'cgroup_pids', return_value=[]), \
                 patch.object(n, 'stop_units', side_effect=stopping), \
                 patch.object(n.subprocess, 'run', return_value=None):
                self.assertEqual(n.deadline(folder), 0)
            self.assertEqual(stopped[0], n.UNIT)
            self.assertNotIn(trial_data()['units'][0], stopped)
            proof = json.loads((folder / 'shutdown.json').read_text())
            self.assertEqual(proof['pre_states'][0]['properties']['ActiveState'], 'active')
            self.assertEqual(proof['pre_states'][0]['properties']['InvocationID'], 'inv')
            self.assertEqual(proof['errors'], [])
            self.assertEqual(proof['stop_request_mono'], 2066.1)
            self.assertEqual(proof['quiescence_mono'], 2067.)
            self.assertEqual(json.loads((folder / 'deadline-stop.json').read_text())['shutdown'], proof)

    def test_deadline_reuses_observer_shutdown_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            code, result, shutdown = observe_full_window(folder)
            self.assertEqual(code, 0)
            with patch.object(n, 'trial_metadata', return_value=trial_data()), \
                 patch.object(n, 'monotonic_now', return_value=2066.), \
                 patch.object(n, 'secure_bytes', side_effect=lambda p: p.read_bytes()), \
                 patch.object(n, 'unit_evidence',
                              side_effect=lambda u: retained_success(u)), \
                 patch.object(n, 'boot_id', return_value='boot'), \
                 patch.object(n, 'stop_units', side_effect=lambda units: helper_evidence(units)), \
                 patch.object(n.subprocess, 'run', return_value=None):
                self.assertEqual(n.deadline(folder), 0)
            deadline_proof = json.loads((folder / 'deadline.json').read_text())
            self.assertEqual(deadline_proof['errors'], [])


class TestGHeartbeatCoverageAcceptedAtAudit(unittest.TestCase):
    """FIX 2 points 2/3/4: independent T+2640 audit accepts full required coverage."""

    def test_full_coverage_heartbeat_not_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder, coverage_mono=2385.)
            code, report = run_audit(folder)
            self.assertEqual(code, 0, report['errors'])
            self.assertEqual(report['verdict'], 'PENDING_REVIEW')

    def test_missing_coverage_record_fails_open(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder)
            write_host_records(folder, raw='')
            code, report = run_audit(folder)
            self.assertEqual(code, 1)
            self.assertEqual(report['verdict'], 'BLOCKED')

    def test_obsolete_evidence_without_coverage_field_not_retrograded(self):
        """Legacy evidence lacking heartbeat_expiry_mono keeps its historical verdict path."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder, coverage_mono=100.)
            audit_data = trial_data(); audit_data.pop('heartbeat_expiry_mono')
            code, report = run_audit(folder, audit_data=audit_data)
            self.assertEqual(code, 1, report['errors'])


class TestHEarlyHeartbeatExitFails(unittest.TestCase):
    """FIX 2 point 5: heartbeat crash, early stop or missing records remain fail-closed."""

    def test_early_stopped_heartbeat_blocks_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder, coverage_mono=1868.)
            code, report = run_audit(folder)
            self.assertEqual(code, 1)
            self.assertEqual(report['verdict'], 'BLOCKED')
            self.assertIn('heartbeat required coverage incomplete', report['errors'])

    def test_corrupt_coverage_record_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder)
            write_host_records(folder, raw='not json\n')
            code, report = run_audit(folder)
            self.assertEqual(code, 1, report)
            self.assertTrue(any('heartbeat' in e for e in report['errors']))


class TestIAuditEvaluatesAllHelpers(unittest.TestCase):
    """Test I: unchanged audit blocks every unverified helper result."""

    def test_failed_heartbeat_helper_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder)
            heartbeat = trial_data()['units'][0]
            def failing(units):
                return helper_evidence(units, override=lambda u, s:
                                       dict(s, ActiveState='failed', Result='failed',
                                            ExecMainCode='1', ExecMainStatus='1') if u == heartbeat else s)
            def failing_prestop(u):
                return failing([u])[1][0]
            code, report = run_audit(folder, stopping=failing, evidence_effect=failing_prestop)
            self.assertEqual(code, 1)
            self.assertTrue(any('terminal helper result unavailable' in e for e in report['errors']))

    def test_timeout_with_full_coverage_still_blocks_audit_and_post_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder)
            data = trial_data()
            heartbeat = data['units'][0]
            def timed_out(units):
                return helper_evidence(units, override=lambda u, s:
                    dict(s, ActiveState='failed', Result='timeout', ExecMainCode='2',
                         ExecMainStatus='15') if u == heartbeat else s)
            before = timed_out([heartbeat])[1][0]
            code, report = run_audit(folder, stopping=timed_out, heartbeat_before=before)
            self.assertEqual(code, 1)
            self.assertEqual(report['verdict'], 'BLOCKED')
            self.assertIn('heartbeat did not self-terminate successfully before audit', report['errors'])
            self.assertIsNone(n.heartbeat_coverage_error(folder, data))
            with patch.object(n, 'trial_metadata', return_value=data), \
                 patch.object(n, 'unit_evidence', side_effect=lambda u: timed_out([u])[1][0]), \
                 patch.object(n, 'stop_units', return_value=([], [])), \
                 patch.object(n, 'command', return_value=''):
                inspected = n.inspect_cleanup(folder)
            self.assertEqual(inspected['verdict'], 'BLOCKED')
            self.assertTrue(any('terminal helper' in e for e in inspected['errors']))

    def test_missing_deadline_blocks_even_with_stopped_main(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder)
            (folder / 'deadline.json').unlink()
            code, report = run_audit(folder)
            self.assertEqual(code, 1)
            self.assertIn('independent deadline evidence missing', report['errors'])

    def test_late_deadline_blocks_even_with_stopped_main(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder)
            proof = json.loads((folder / 'deadline-stop.json').read_text())
            proof['trigger_mono'] = 2096.  # Deadline 2065 + 31, outside accepted window.
            (folder / 'deadline-stop.json').write_text(json.dumps(proof))
            code, report = run_audit(folder)
            self.assertEqual(code, 1)
            self.assertIn('deadline trigger outside accepted 30-second window', report['errors'])

    def test_failed_deadline_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder)
            (folder / 'deadline.json').write_text(
                json.dumps({'at': '2026', 'units': [], 'errors': ['stop failed']}))
            code, report = run_audit(folder)
            self.assertEqual(code, 1)
            self.assertTrue(any('deadline failed' in e for e in report['errors']))


class TestJHistoricalRegressionFixture(unittest.TestCase):
    """Test J: reproduce pilot-20261030T013400Z without modifying original evidence."""

    def _historical_fixture(self, folder):
        shutdown = dict(errors=['shutdown not proven before runtime backstop'],
                        runtime_fallback=False, stop_request_mono=2005.5, quiescence_mono=2008.,
                        units=[post_unit()], boot_id='boot')  # pre_states absent: old format.
        result = dict(trial_id=TRIAL, continuous=True, observation_end_mono=2005.,
                      elapsed_seconds=1800.0000859, window_seconds=1800, run_id='run-cont',
                      samples=180, ingestion={'count': 5},
                      errors=['ValueError: shutdown not proven before runtime backstop'],
                      verdict='BLOCKED')
        (folder / 'timing.json').write_text(json.dumps(timing()))
        (folder / 'shutdown.json').write_text(json.dumps(shutdown))
        (folder / 'result.json').write_text(json.dumps(result))
        (folder / 'deadline.json').write_text(json.dumps({'at': '2026', 'units': [],
            'errors': ['shutdown not proven before runtime backstop']}))
        (folder / 'deadline-stop.json').write_text(json.dumps(
            {'at': '2026', 'trigger_mono': 2066., 'units': [], 'shutdown': shutdown,
             'errors': ['shutdown not proven before runtime backstop']}))
        (folder / 'startup-handoff.json').write_text(
            json.dumps({'at_mono': 400., 'errors': [], 'units': []}))
        write_host_records(folder, coverage_mono=1868.)
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in folder.glob('*') if p.is_file()}

    def test_historical_failure_reproduces_blocked_audit_and_evidence_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            digests = self._historical_fixture(folder)
            code, report = run_audit(folder)
            self.assertEqual(code, 1, report)
            self.assertEqual(report['verdict'], 'BLOCKED')
            self.assertIn('incomplete or failed observation/shutdown timing proof', report['errors'])
            self.assertIn('heartbeat required coverage incomplete', report['errors'])
            remaining = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in folder.glob('*') if p.is_file() and p.name != 'cleanup.json'}
            self.assertEqual(remaining, digests, 'historical evidence bytes must remain untouched')

    def test_restored_evidence_passes(self):
        """Same trial, but evidence as the fixed code would have produced it: audit passes."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder, coverage_mono=2385., prestop=True, shutdown_errors=False)
            code, report = run_audit(folder)
            self.assertEqual(code, 0, report['errors'])


def retained_success(unit, invocation='helper-inv'):
    """Terminal state a successful transient helper leaves under RemainAfterExit=yes retention."""
    state = dict(LoadState='loaded', ActiveState='active', SubState='exited', MainPID='0',
                 ControlPID='0', Result='success', ExecMainCode='1', ExecMainStatus='0',
                 NRestarts='0', InvocationID=invocation, ControlGroup='', ExecMainPID='123',
                 ExecMainStartTimestampMonotonic='100000000',
                 ExecMainExitTimestampMonotonic='2000000000',
                 ActiveEnterTimestampMonotonic='100000000',
                 InactiveEnterTimestampMonotonic='0')
    return {'unit': unit, 'properties': state, 'pids': []}


def gc_placeholder(unit):
    """Post-garbage-collection 'show' output: unit unloaded, properties are defaults."""
    state = dict(LoadState='not-found', ActiveState='inactive', SubState='dead', MainPID='0',
                 ControlPID='0', Result='success', ExecMainCode='0', ExecMainStatus='0',
                 NRestarts='0', InvocationID='', ControlGroup='')
    return {'unit': unit, 'properties': state, 'pids': []}


class TestKTerminalRetentionValidation(unittest.TestCase):
    """FIX 3: systemd-backed terminal retention is the only accepted successful-helper proof."""

    def test_retained_successful_helper_accepted(self):
        data = trial_data()
        evidence = [post_unit()] + [retained_success(u) for u in data['units']]
        n.validate_helper_exits(data, evidence)

    def test_unloaded_successful_helper_fails(self):
        data = trial_data()
        evidence = [post_unit()] + [gc_placeholder(u) for u in data['units']]
        with self.assertRaisesRegex(ValueError, 'successful terminal helper evidence missing'):
            n.validate_helper_exits(data, evidence)

    def test_sigterm_killed_helper_fails(self):
        data = trial_data()
        evidence = [post_unit()] + [retained_success(u) for u in data['units']]
        heartbeat = data['units'][0]
        item = next(i for i in evidence if i['unit'] == heartbeat)
        item['properties'].update(ExecMainCode='2', ExecMainStatus='15', Result='signal')
        with self.assertRaisesRegex(ValueError, 'successful terminal helper result unavailable'):
            n.validate_helper_exits(data, evidence)

    def test_runtime_limit_helper_fails(self):
        data = trial_data()
        evidence = [post_unit()] + [retained_success(u) for u in data['units']]
        heartbeat = data['units'][0]
        item = next(i for i in evidence if i['unit'] == heartbeat)
        item['properties'].update(ActiveState='failed', SubState='failed',
                                  Result='timeout', ExecMainCode='2', ExecMainStatus='15')
        with self.assertRaisesRegex(ValueError, 'successful terminal helper result unavailable'):
            n.validate_helper_exits(data, evidence)

    def test_residual_cgroup_pids_fails(self):
        data = trial_data()
        evidence = [post_unit()] + [retained_success(u) for u in data['units']]
        heartbeat = data['units'][0]
        item = next(i for i in evidence if i['unit'] == heartbeat)
        item['pids'] = [4242]
        with self.assertRaisesRegex(ValueError, 'successful terminal helper result unavailable'):
            n.validate_helper_exits(data, evidence)

    def test_contradictory_exit_status_fails(self):
        data = trial_data()
        evidence = [post_unit()] + [retained_success(u) for u in data['units']]
        heartbeat = data['units'][0]
        item = next(i for i in evidence if i['unit'] == heartbeat)
        item['properties']['ExecMainStatus'] = '1'  # Result=success claimed but exit code nonzero.
        with self.assertRaisesRegex(ValueError, 'successful terminal helper result unavailable'):
            n.validate_helper_exits(data, evidence)

    def test_wrong_unit_snapshot_fails(self):
        data = trial_data()
        evidence = [post_unit()] + [retained_success(u) for u in data['units']
                                    if u != data['units'][0]]
        with self.assertRaisesRegex(ValueError, 'successful terminal helper evidence missing'):
            n.validate_helper_exits(data, evidence)


def post_pilot_units(u):
    """Realistic post-pilot state: audit.service still running, observer already
    stopped/unloaded by deadline(), timers elapsed, helpers retained-success."""
    data = trial_data()
    timers = {data['units'][2], data['units'][4], data['units'][6]}
    if u == n.UNIT:
        return post_unit()
    if u == data['units'][5]:
        return running_audit_service(u)
    if u in timers or u in (data['units'][OBSERVER], data['units'][7]):
        return gc_placeholder(u)
    return retained_success(u)


class TestLAuditUsesPreStopTerminalSnapshot(unittest.TestCase):
    """FIX 3: audit validates the pre-stop terminal snapshot, persisted in cleanup.json."""

    def test_audit_accepts_real_completed_pilot(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder, coverage_mono=2385.)
            data = trial_data()
            bind_deadline_observer_record(folder, data)
            def stopping(units):
                record_path = folder / 'cleanup.json'
                self.assertTrue(record_path.exists(), 'helper proof must be durable before stop')
                record = json.loads(record_path.read_text())
                self.assertTrue(record['errors'], 'in-progress audit must stay blocked')
                return [], [gc_placeholder(u) for u in units]
            code, report = run_audit(folder, stopping=stopping,
                                     evidence_effect=post_pilot_units)
            self.assertEqual(code, 0, report['errors'])
            self.assertEqual(report['verdict'], 'PENDING_REVIEW')
            exit_units = [item['unit'] for item in report['helper_exits']]
            self.assertNotIn(data['units'][5], exit_units)  # Running audit.service never captured.
            # Only helpers unloaded before audit resolve through persisted records.
            self.assertEqual(report['audit_verified_helpers'], [data['units'][1]])
            self.assertEqual(report['trial_id'], TRIAL)
            self.assertEqual(report['boot_id'], 'boot')

    def test_missing_deadline_observer_record_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder, coverage_mono=2385.)
            data = trial_data()
            bind_deadline_observer_record(folder, data, include_binding=False)
            def stopping(units):
                return [], [gc_placeholder(u) for u in units]
            code, report = run_audit(folder, stopping=stopping,
                                     evidence_effect=post_pilot_units)
            self.assertEqual(code, 1, report['errors'])
            self.assertEqual(report['verdict'], 'BLOCKED')
            self.assertIn(data['units'][OBSERVER] + ': successful terminal helper evidence missing',
                          report['errors'])

    def test_wrong_trial_deadline_record_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder, coverage_mono=2385.)
            data = trial_data()
            bind_deadline_observer_record(folder, data, trial_id='soak-20250101T000000Z')
            def stopping(units):
                return [], [gc_placeholder(u) for u in units]
            code, report = run_audit(folder, stopping=stopping,
                                     evidence_effect=post_pilot_units)
            self.assertEqual(code, 1)
            self.assertTrue(any('successful terminal helper evidence missing' in e
                                for e in report['errors']))

    def test_stale_or_unbound_deadline_record_rejected(self):
        for field, value in (('InvocationID', ''), ('ExecMainPID', '0'),
                             ('ExecMainStartTimestampMonotonic', '1'),
                             ('ExecMainExitTimestampMonotonic', '0')):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                full_audit_folder(folder)
                data = trial_data()
                observer = retained_success(data['units'][OBSERVER])
                observer['properties'][field] = value
                bind_deadline_observer_record(folder, data, state=observer)
                code, report = run_audit(folder, evidence_effect=post_pilot_units)
                self.assertEqual(code, 1, report)

    def test_contradictory_deadline_record_rejected(self):
        """Observer snapshot that cannot prove genuine success makes audit fail."""
        for mutation in (lambda state: state['properties'].update(
                             ExecMainCode='2', ExecMainStatus='15', ActiveState='active',
                             SubState='exited'),
                         lambda state: state['properties'].update(
                             ActiveState='active', SubState='running', MainPID='7')):
            with self.subTest(), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                full_audit_folder(folder, coverage_mono=2385.)
                data = trial_data()
                observer = retained_success(data['units'][OBSERVER])
                mutation(observer)
                bind_deadline_observer_record(folder, data, state=observer)
                def stopping(units):
                    return [], [gc_placeholder(u) for u in units]
                code, report = run_audit(folder, stopping=stopping,
                                         evidence_effect=post_pilot_units)
                self.assertEqual(code, 1)
                self.assertEqual(report['verdict'], 'BLOCKED')

    def test_audit_blocks_when_heartbeat_gc_before_audit(self):
        """Reproduce pilot-20261030T200555Z: unretained helper gone when audit runs."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder, coverage_mono=2385.)
            data = trial_data()
            bind_deadline_observer_record(folder, data)
            def evidence_effect(u):
                if u == n.UNIT:
                    return post_unit()
                if u == data['units'][5]:
                    return running_audit_service(u)
                item = post_pilot_units(u)
                if u == data['units'][0]:
                    item = gc_placeholder(u)
                return item
            def stopping(units):
                return [], [gc_placeholder(u) for u in units]
            code, report = run_audit(folder, stopping=stopping, evidence_effect=evidence_effect)
            self.assertEqual(code, 1)
            self.assertEqual(report['verdict'], 'BLOCKED')
            self.assertIn('heartbeat did not self-terminate successfully before audit',
                          report['errors'])

    def test_audit_recomputes_identity_despite_helper_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder)
            data = trial_data()
            heartbeat = data['units'][0]
            def evidence_effect(u):
                item = retained_success(u)
                if u == heartbeat:
                    item['properties'].update(Result='timeout', ExecMainCode='2',
                                              ExecMainStatus='15', ActiveState='failed',
                                              SubState='failed')
                return item
            identity = 'mismatched-fingerprint'
            code, report = run_audit(folder, identity=(identity, {}),
                                     evidence_effect=evidence_effect)
            self.assertEqual(code, 1)
            self.assertIn('heartbeat did not self-terminate successfully before audit',
                          report['errors'])
            self.assertIn('final runtime fingerprint changed', report['errors'])
            self.assertEqual(report['final_runtime_identity']['fingerprint'], identity)
            self.assertFalse(report['final_runtime_identity']['matches_launch'])

    def test_audit_reports_identity_unavailable_despite_helper_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder)
            data = trial_data()
            heartbeat = data['units'][0]
            def evidence_effect(u):
                item = retained_success(u)
                if u == heartbeat:
                    item['properties'].update(Result='timeout', ExecMainCode='2',
                                              ExecMainStatus='15', ActiveState='failed',
                                              SubState='failed')
                return item
            with patch.object(n, 'trial_metadata', return_value=data), \
                 patch.object(n, 'unit_evidence', side_effect=evidence_effect), \
                 patch.object(n, 'secure_bytes', side_effect=lambda p: p.read_bytes()), \
                 patch.object(n, 'stop_units', side_effect=lambda units: helper_evidence(units)), \
                 patch.object(n, 'runtime_identity',
                              side_effect=ValueError('systemctl unavailable')):
                code = n.audit(folder)
            report = json.loads((folder / 'cleanup.json').read_text())
            self.assertEqual(code, 1)
            self.assertTrue(any('final runtime identity recompute' in e for e in report['errors']))
            self.assertTrue(any('heartbeat did not self-terminate' in e for e in report['errors']))


class TestMInspectCleanupPostExit(unittest.TestCase):
    """FIX 3: post-exit inspection read-only: retained audit-service success + audit-verified helpers."""

    def inspect(self, folder, audit_service_success=True, wrong_trial=False):
        data = trial_data()
        timer_units = {data['units'][2], data['units'][4], data['units'][6]}
        gone_units = {data['units'][0], data['units'][1], data['units'][3]}  # Stopped by audit.
        record = {'trial_id': TRIAL if not wrong_trial else 'pilot-20250101T000000Z',
                  'boot_id': 'boot', 'errors': [], 'verdict': 'PENDING_REVIEW',
                  'helper_exits': [retained_success(u) for u in
                                   [data['units'][0], data['units'][1], data['units'][3]]]}
        (folder / 'cleanup.json').write_text(json.dumps(record))
        bind_deadline_observer_record(folder, data,
                                      trial_id=record['trial_id'])
        def evidence_effect(u):
            if u == n.UNIT:
                return {'unit': n.UNIT, 'properties': dict(
                    LoadState='loaded', ActiveState='inactive', SubState='dead', MainPID='0',
                    ControlPID='0', Result='success', ExecMainCode='0', ExecMainStatus='0',
                    InvocationID='', ControlGroup=''), 'pids': []}
            if u in timer_units or u in gone_units:
                return gc_placeholder(u)
            if u == data['units'][5] and not audit_service_success:
                return gc_placeholder(u)
            return retained_success(u)
        with patch.object(n, 'trial_metadata', return_value=data), \
             patch.object(n, 'unit_evidence', side_effect=evidence_effect), \
             patch.object(n, 'secure_bytes', side_effect=lambda p: p.read_bytes()), \
             patch.object(n, 'command', return_value=''), \
             patch.object(n, 'boot_id', return_value='boot'):
            result = n.inspect_cleanup(folder)
        return result

    def test_retained_audit_service_and_audit_verified_helpers_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder)
            result = self.inspect(folder)
            self.assertEqual(result['verdict'], 'PENDING_REVIEW', result['errors'])
            self.assertEqual(result['errors'], [])
            data = trial_data()
            self.assertEqual(result['audit_verified_helpers'],
                             [data['units'][0], data['units'][1], data['units'][3]])
            self.assertEqual(sorted(p.name for p in folder.iterdir()),
                             sorted(['cleanup.json', 'deadline-stop.json', 'deadline.json',
                                     'restart-verification.json', 'result.json', 'shutdown.json',
                                     'shutdown-pre.json', 'startup-handoff.json', 'timing.json',
                                     'host.json.jsonl']),
                             'post-exit inspection must remain read-only')

    def test_wrong_trial_binding_rejected_as_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder)
            result = self.inspect(folder, wrong_trial=True)
            self.assertEqual(result['verdict'], 'BLOCKED')
            self.assertTrue(any('successful terminal helper evidence missing' in e
                                for e in result['errors']))

    def test_audit_service_state_unavailable_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full_audit_folder(folder)
            result = self.inspect(folder, audit_service_success=False)
            self.assertEqual(result['verdict'], 'BLOCKED')
            self.assertTrue(any('successful terminal helper evidence missing' in e
                                for e in result['errors']))


def terminal_heartbeat(expiry):
    """Run real Bash loop across expiry, with 0.1s command cost and real 5s cadence."""
    import os
    import sys
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        clock = folder / 'clock'
        clock.write_text(str(expiry - 1.179))
        python = folder / 'python3'
        python.write_text(f'#!{Path(sys.executable).resolve()}\n' + '''import os, sys, time
from pathlib import Path
clock = Path(os.environ['TEST_CLOCK'])
now = float(clock.read_text())
clock.write_text(str(now + .1))
time.time = lambda: now
time.clock_gettime = lambda clock_id: now
code = sys.argv[2]
sys.argv = ['-c', *sys.argv[3:]]
exec(compile(code, '<heartbeat-clock>', 'exec'))
''')
        sleep = folder / 'sleep'
        sleep.write_text(f'#!{Path(sys.executable).resolve()}\n' + '''import os, sys
from pathlib import Path
assert sys.argv[1:] == ['5'], 'normal heartbeat sampling must remain five seconds'
clock = Path(os.environ['TEST_CLOCK'])
clock.write_text(str(float(clock.read_text()) + 5))
''')
        python.chmod(0o755)
        sleep.chmod(0o755)
        output = folder / 'host.json'
        result = subprocess.run(['bash', str(ROOT / 'deploy/infrastructure-host-heartbeat.sh'),
                                 str(output), str(expiry)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PATH=str(folder) + ':' + os.environ['PATH'],
                                         TEST_CLOCK=str(clock)))
        if result.returncode:
            raise AssertionError(result.stderr)
        records = [json.loads(line) for line in Path(str(output) + '.jsonl').read_text().splitlines()]
        assert json.loads(output.read_text()) == records[-1]
        return records, float(clock.read_text())


class TestHeartbeatScriptTerminalPath(unittest.TestCase):
    """FIX 2 point 3: controlled, bounded terminal path after required coverage."""

    def test_five_second_sampling_crosses_expiry_and_exits_before_hard_limit(self):
        for expiry, hard_limit in ((2280, 2340), (86880, 86940)):
            with self.subTest(expiry=expiry):
                records, exited = terminal_heartbeat(expiry)
                self.assertEqual(len(records), 2)
                self.assertLess(records[0]['mono'], expiry)
                self.assertGreaterEqual(records[-1]['mono'], expiry)
                self.assertLessEqual(records[-1]['mono'], expiry + 20)
                self.assertGreater(exited, records[-1]['mono'])
                self.assertLess(exited, hard_limit)
                self.assertEqual(n.host_error(records[0], records[1], records[1]['wall']), '')

    def test_heartbeat_terminates_after_required_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'host.json'
            result = subprocess.run(['bash', str(ROOT / 'deploy/infrastructure-host-heartbeat.sh'),
                                     str(output), '0.1'], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            sample = json.loads(output.read_text())
            self.assertEqual(sample['sequence'], 1)
            records = [json.loads(line) for line in
                       Path(str(output) + '.jsonl').read_text().splitlines()]
            self.assertEqual(records[-1]['sequence'], 1)


if __name__ == '__main__':
    unittest.main()
