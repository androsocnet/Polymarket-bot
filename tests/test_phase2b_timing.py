"""Observable timing contract; simulated systemd boundary, real controller paths."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('timing_trial', Path(__file__).resolve().parents[1] / 'deploy/infrastructure-trial.py')
n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)


def timer(expiry, target):
    values = dict(Unit=target, TimersMonotonic=[['OnBootUSec', expiry, expiry]],
                  TimersCalendar=[], NextElapseUSecMonotonic=expiry, NextElapseUSecRealtime=0,
                  AccuracyUSec=1000000, RandomizedDelayUSec=0, RandomizedOffsetUSec=0,
                  WakeSystem=False, RemainAfterElapse=False, OnClockChange=False, OnTimezoneChange=False)
    return json.dumps({'type': 'a{sv}', 'data': [{k: {'data': v} for k, v in values.items()}]})


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.data = dict(trial_id='pilot-20261030T000000Z', seconds=1800, fingerprint='hash',
                         boot_id='boot', launch_mono=100., units=n.owned('pilot-20261030T000000Z'))
        self.service = dict(ActiveState='active', MainPID='123', InvocationID='inv',
                            ActiveEnterTimestampMonotonic='200000000', RuntimeMaxUSec='34min',
                            RuntimeRandomizedExtraUSec='0', Result='success', NRestarts='0')

    def schedule(self, lag=5, mutate=None):
        now = 200. + lag
        expiry = round((now + 1860) * 1e6)
        raw = json.loads(timer(expiry, self.data['units'][3]))
        if mutate:
            mutate(raw['data'][0])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            def system_command(*args):
                if args[:2] == ('busctl', 'get-property'):
                    value = self.service[args[-1]]
                    return 't ' + ('2040000000' if value == '34min' else value)
                return json.dumps(raw)
            with patch.object(n, 'boot_id', return_value='boot'), patch.object(n.time, 'monotonic', return_value=now), patch.object(n.time, 'clock_gettime', return_value=now), patch.object(n, 'command', side_effect=system_command) as command, patch.object(n, 'properties', return_value={'ActiveState': 'active'}):
                result = n.start_observation_timing(folder, self.data, self.service)
                self.assertEqual(json.loads((folder / 'timing.json').read_text()), result)
                calls = command.call_args_list
            return result, calls

    def test_real_scheduling_fast_and_maximum_startup_lag(self):
        self.assertTrue(hasattr(n, 'start_observation_timing'), 'baseline scheduling missing')
        for lag in (0, 5, 120):
            with self.subTest(lag=lag):
                result, calls = self.schedule(lag)
                self.assertEqual(result['deadline_mono'], result['baseline_mono'] + 1860)
                self.assertGreaterEqual(result['runtime_expiry_mono'] - result['deadline_mono'], 60)
                self.assertTrue(any('--on-boot=' + str(round(result['deadline_mono'] * 1e6)) + 'us' in c.args for c in calls))

    def test_clock_mismatch_and_late_arming_reject(self):
        with patch.object(n, 'boot_id', return_value='boot'), patch.object(n.time, 'monotonic', return_value=205), patch.object(n.time, 'clock_gettime', return_value=300):
            with self.assertRaisesRegex(ValueError, 'clock'):
                n.monotonic_now(self.data)
        with tempfile.TemporaryDirectory() as directory, patch.object(n, 'monotonic_now', side_effect=[205, 2100]), patch.object(n, 'properties', return_value={'ActiveState': 'active'}), patch.object(n, 'command', return_value=timer(2065000000, self.data['units'][3])):
            with self.assertRaisesRegex(ValueError, 'arming'):
                n.arm_timer(self.data, self.data['units'][2], 2065, 'deadline', Path(directory))

    def test_excessive_lag_and_effective_timer_errors_fail_closed(self):
        self.assertTrue(hasattr(n, 'start_observation_timing'), 'baseline scheduling missing')
        with self.assertRaisesRegex(ValueError, 'margin'):
            self.schedule(120.01)
        for key, value in [('WakeSystem', True), ('AccuracyUSec', 2000000), ('Unit', 'wrong.service'),
                           ('NextElapseUSecMonotonic', 1), ('RandomizedDelayUSec', 1),
                           ('TimersCalendar', [['OnCalendar', 'daily', 1]])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.schedule(mutate=lambda p: p[key].update(data=value))

    def test_wrong_boot_runtime_and_activation_reject(self):
        self.assertTrue(hasattr(n, 'start_observation_timing'), 'baseline scheduling missing')
        for key, value in [('RuntimeMaxUSec', '1'), ('ActiveEnterTimestampMonotonic', '0'),
                           ('RuntimeRandomizedExtraUSec', '1')]:
            old = copy.deepcopy(self.service)
            self.service[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.schedule()
            self.service = old
        self.data['boot_id'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'boot'):
            self.schedule()


class LifecycleTests(TimingTests):
    def test_real_observer_full_window_and_truncation(self):
        from contextlib import ExitStack
        class DB:
            def execute(self, *args): return self
            def fetchone(self): return ('run', 100000000, 'RUNNING')
            def fetchall(self): return [('ok',)]
            def close(self): pass
        for crash in (False, True):
            with self.subTest(crash=crash), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                folder = Path(directory)
                (folder / 'db').touch()
                (folder / 'host.json.jsonl').touch()
                now = [205.]
                def sleep(seconds):
                    now[0] += seconds
                    if crash and now[0] >= 250: raise RuntimeError('observer interrupted')
                state = dict(self.service, ControlGroup='', ControlPID='0', SubState='running')
                states = iter([dict(state, ActiveState='inactive'), dict(state, InvocationID='first')])
                def props(unit): return next(states, state)
                data = dict(self.data, phase='pilot', launch_at={'utc': '2026-10-30T00:00:00Z'})
                values = dict(trial_metadata=data, check_approval=None, require_service_policy=None,
                              environment={'MVP_DB_PATH': str(folder / 'db'), 'MVP_STATE_DIR': str(folder), 'MVP_BOT_ID': 'bot'},
                              snapshot={'trading': {}, 'observations': 1}, connect=DB(), fresh_run=True,
                              read_host={}, memory_events={}, hash_due=False,
                              ingestion={'count': 1, 'last_received_us': 100000000}, command='',
                              stop_units=([], []))
                for name, value in values.items(): stack.enter_context(patch.object(n, name, return_value=value))
                stack.enter_context(patch.object(n, 'properties', side_effect=props))
                stack.enter_context(patch.object(n, 'wait_run', side_effect=['first', 'run']))
                stack.enter_context(patch.object(n, 'boot_id', return_value='boot'))
                stack.enter_context(patch.object(n.time, 'monotonic', side_effect=lambda: now[0]))
                stack.enter_context(patch.object(n.time, 'clock_gettime', side_effect=lambda *a: now[0]))
                stack.enter_context(patch.object(n.time, 'time', side_effect=lambda: now[0]))
                stack.enter_context(patch.object(n, 'read_db_heartbeat', return_value=('run', 1, 'RUNNING')))
                stack.enter_context(patch.object(n, 'ingestion', side_effect=lambda *a: {'count': 1, 'last_received_us': int(now[0] * 1e6)}))
                stack.enter_context(patch.object(n, 'connect', side_effect=lambda *a: DB()))
                DB.fetchone = lambda self: ('run', int(now[0] * 1e6), 'RUNNING')
                stack.enter_context(patch.object(n.time, 'sleep', side_effect=sleep))
                stack.enter_context(patch.object(n, 'runtime_bound', side_effect=lambda name='RuntimeMaxUSec': 2040000000 if name == 'RuntimeMaxUSec' else 0))
                stack.enter_context(patch.object(n, 'arm_timer', return_value={'verified': True}))
                stack.enter_context(patch.object(n, 'stop_main', return_value={'errors': [], 'units': []}))
                code = n.observe(folder)
                result = json.loads((folder / 'result.json').read_text())
                self.assertEqual(code, int(crash), result['errors'])
                self.assertEqual(result['continuous'], not crash)
                self.assertIn('observation_end_mono', result)
                self.assertEqual((folder / 'timing.json').exists(), True)
                if not crash:
                    self.assertGreaterEqual(result['observation_end_mono'] - 205, 1800)
                    self.assertGreaterEqual(result['samples'], 180)

    def test_launch_arms_audit_then_startup_without_launch_relative_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / 'evidence'
            state = dict(ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0', ControlGroup='', Restart='no')
            def command(*args, **kwargs):
                if 'infrastructure-host-heartbeat.sh' in ' '.join(args):
                    (next(evidence.iterdir()) / 'host.json').write_text('{}')
                return ''
            with patch.object(n, 'STATE', root), patch.object(n, 'EVIDENCE', evidence), patch.object(n, 'secure_bytes', return_value=b''), patch.object(n, 'check_approval', return_value=('hash', {'git_sha': 'a' * 40})), patch.object(n, 'environment', return_value={'MVP_STATE_DIR': str(root)}), patch.object(n, 'require_service_policy'), patch.object(n, 'properties', return_value=state), patch.object(n, 'runtime_bound', return_value=2040000000), patch.object(n, 'command', side_effect=command), patch.object(n, 'arm_timer', return_value={'verified': True}) as arm:
                n.launch('pilot', root / 'approval.json')
            self.assertEqual([c.args[3] for c in arm.call_args_list], ['audit', 'startup-timeout'])
            data = json.loads((next(evidence.iterdir()) / 'launch.json').read_text())
            self.assertEqual(data['startup_expiry_mono'], data['launch_mono'] + 360)
            self.assertEqual(data['heartbeat_expiry_mono'], data['launch_mono'] + 2280)
            self.assertEqual(data['audit_expiry_mono'], data['launch_mono'] + 2640)

    def test_main_shutdown_requires_inactive_no_timeout_no_pids_before_runtime(self):
        self.assertTrue(hasattr(n, 'stop_main'), 'observed shutdown proof missing')
        timing, _ = self.schedule()
        pre = {'unit': n.UNIT, 'properties': dict(self.service, ActiveState='active'), 'pids': ['123']}
        state = dict(self.service, ActiveState='inactive', MainPID='0', ControlPID='0', SubState='dead', InvocationID='')
        evidence = [{'unit': n.UNIT, 'properties': state, 'pids': []}]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            restart = dict(trial_id=self.data['trial_id'], errors=[], first_run_id='first',
                           continuous_run_id='run', second=self.service)
            (folder / 'restart-verification.json').write_text(json.dumps(restart))
            base_timing = timing
            with patch.object(n, 'secure_bytes', side_effect=lambda p: p.read_bytes()), patch.object(n, 'boot_id', return_value='boot'), patch.object(n, 'unit_evidence', return_value=pre), patch.object(n.time, 'monotonic', side_effect=[2005, 2007]), patch.object(n.time, 'clock_gettime', side_effect=[2005, 2007]), patch.object(n, 'stop_units', return_value=([], evidence)):
                proof = n.stop_main(folder, self.data, base_timing)
                self.assertEqual(proof['errors'], [])
                self.assertEqual(proof['quiescence_mono'], 2007)
                self.assertEqual(proof['pre_states'][0]['properties']['InvocationID'], 'inv')
                self.assertEqual((folder / 'shutdown.json').exists(), True)
            for change in ('timeout', 'pids', 'late', 'active', 'missing-pre', 'mismatched-pre', 'empty-timing', 'post-invocation'):
                folder = Path(directory) / change
                folder.mkdir()
                (folder / 'restart-verification.json').write_text(json.dumps(restart))
                bad = copy.deepcopy(evidence)
                current_timing = copy.deepcopy(base_timing)
                current_pre = copy.deepcopy(pre)
                now = 2010
                if change == 'timeout': bad[0]['properties']['Result'] = 'timeout'
                if change == 'pids': bad[0]['pids'] = [9]
                if change == 'late': now = base_timing['runtime_expiry_mono']
                if change == 'active': bad[0]['properties']['ActiveState'] = 'active'
                if change == 'missing-pre':
                    current_pre['properties'].update(ActiveState='inactive', InvocationID='')
                if change == 'mismatched-pre':
                    current_pre['properties'].update(InvocationID='other')
                if change == 'empty-timing': current_timing['invocation_id'] = ''
                if change == 'post-invocation': bad[0]['properties']['InvocationID'] = 'other'
                with self.subTest(change=change), patch.object(n, 'secure_bytes', side_effect=lambda p: p.read_bytes()), patch.object(n, 'boot_id', return_value='boot'), patch.object(n, 'unit_evidence', return_value=current_pre), patch.object(n.time, 'monotonic', side_effect=[2005, now]), patch.object(n.time, 'clock_gettime', side_effect=[2005, now]), patch.object(n, 'stop_units', return_value=([], bad)):
                    self.assertTrue(n.stop_main(folder, self.data, current_timing)['errors'])

    def test_late_deadline_fails_despite_valid_completed_shutdown(self):
        timing, _ = self.schedule()
        result = dict(continuous=True, observation_end_mono=2005, errors=[])
        shutdown = dict(errors=[], quiescence_mono=2007, stop_request_mono=2006, runtime_fallback=False, boot_id='boot',
                        pre_states=[{'unit': n.UNIT, 'properties': dict(self.service, ActiveState='active'), 'pids': []}],
                        units=[{'unit': n.UNIT, 'properties': dict(self.service, ActiveState='inactive', MainPID='0', ControlPID='0', SubState='dead', InvocationID=''), 'pids': []}])
        for trigger, expected in ((2064.99, 1), (2065, 0), (2095, 0), (2095.01, 1), (3000, 1)):
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                result['run_id'] = 'run'
                shutdown['run_id'] = 'run'
                values = {'timing.json': timing, 'result.json': result, 'shutdown.json': shutdown,
                          'restart-verification.json': dict(trial_id=self.data['trial_id'], errors=[],
                              first_run_id='first', continuous_run_id='run', second=self.service),
                          'shutdown-pre.json': dict(trial_id=self.data['trial_id'], boot_id='boot',
                              run_id='run', at_mono=2006, units=shutdown['pre_states'], errors=[])}
                for name, value in values.items():
                    (folder / name).write_text(json.dumps(value))
                with patch.object(n, 'trial_metadata', return_value=self.data), patch.object(n, 'monotonic_now', return_value=trigger), patch.object(n, 'secure_bytes', side_effect=lambda p: p.read_bytes()), patch.object(n, 'unit_evidence', side_effect=lambda u: {'unit': u, 'properties': dict(ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0', Result='success', ExecMainCode='1', ExecMainStatus='0', LoadState='loaded'), 'pids': []}), patch.object(n, 'stop_units', return_value=([], [])), patch.object(n.subprocess, 'run', return_value=None):
                    self.assertEqual(n.deadline(folder), expected)
                proof = json.loads((folder / 'deadline-stop.json').read_text())
                self.assertEqual(proof['trigger_mono'], trigger)

    def test_deadline_missing_timing_still_stops_main_and_records_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(n, 'trial_metadata', return_value=self.data), patch.object(n, 'monotonic_now', return_value=2100), patch.object(n, 'secure_bytes', side_effect=FileNotFoundError('missing timing')), patch.object(n, 'unit_evidence', side_effect=lambda u: {'unit': u, 'properties': dict(ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0', Result='success', ExecMainCode='1', ExecMainStatus='0', LoadState='loaded'), 'pids': []}), patch.object(n, 'stop_units', return_value=([], [])) as stop, patch.object(n.subprocess, 'run', return_value=None):
                code = n.deadline(folder)
            self.assertEqual(code, 1)
            self.assertTrue(any(n.UNIT in c.args[0] for c in stop.call_args_list))
            self.assertTrue(json.loads((folder / 'deadline.json').read_text())['errors'])

    def test_current_main_timeout_rejected_despite_historical_shutdown(self):
        evidence = [{'unit': unit, 'properties': dict(ActiveState='inactive', SubState='dead', MainPID='0',
                    ControlPID='0', Result='success', ExecMainStatus='0'), 'pids': []}
                    for unit in [n.UNIT, *self.data['units']]]
        evidence[0]['properties'].update(ActiveState='failed', Result='timeout')
        with self.assertRaisesRegex(ValueError, 'main'):
            n.validate_helper_exits(self.data, evidence, include_audit=True)

    def test_post_exit_rejects_failed_audit_and_helper_results(self):
        for failed in (None, n.UNIT, self.data['units'][3], self.data['units'][5], self.data['units'][7]):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                (folder / 'cleanup.json').write_text('{"errors": []}')
                def evidence(unit):
                    state = dict(ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0', Result='success', ExecMainStatus='0', ExecMainCode='1', LoadState='loaded')
                    if unit == self.data['units'][0]:
                        state.update(ExecMainCode='1', ExecMainStatus='0')  # Heartbeat self-exits successfully.
                    if unit == failed:
                        state.update(ActiveState='failed', SubState='failed', Result='timeout')
                    return {'unit': unit, 'properties': state, 'pids': []}
                with patch.object(n, 'trial_metadata', return_value=self.data), patch.object(n, 'unit_evidence', side_effect=evidence), patch.object(n, 'command', return_value=''), patch.object(n, 'stop_units', return_value=([], [])):
                    report = n.inspect_cleanup(folder)
                self.assertEqual(report['verdict'], 'BLOCKED' if failed else 'PENDING_REVIEW')

    def test_audit_never_stops_itself_and_missing_timing_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(n, 'trial_metadata', return_value=self.data), patch.object(n, 'stop_units', return_value=([], [])) as stop, patch.object(n, 'runtime_identity', return_value=('hash', {})):
                self.assertEqual(n.audit(folder), 1)
            self.assertNotIn(self.data['units'][5], stop.call_args.args[0])
            self.assertIn(self.data['units'][7], stop.call_args.args[0])
            self.assertTrue(json.loads((folder / 'cleanup.json').read_text())['errors'])

    def test_startup_watchdog_stops_controller_and_services_even_without_baseline(self):
        self.assertTrue(hasattr(n, 'startup_timeout'), 'startup protection missing')
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(n, 'trial_metadata', return_value=self.data), patch.object(n, 'stop_units', return_value=([], [])) as stop:
                self.assertEqual(n.startup_timeout(folder), 1)
            self.assertIn(n.UNIT, stop.call_args.args[0])
            self.assertIn(self.data['units'][1], stop.call_args.args[0])
            self.assertTrue((folder / 'startup-stop.json').exists())

    def test_observation_proof_rejects_truncation_missing_end_and_failed_shutdown(self):
        self.assertTrue(hasattr(n, 'validate_timing'), 'final timing validation missing')
        timing, _ = self.schedule()
        result = dict(continuous=True, observation_end_mono=timing['baseline_mono'] + 1800, errors=[])
        shutdown = dict(errors=[], quiescence_mono=2010, stop_request_mono=2005, runtime_fallback=False, boot_id='boot',
                        pre_states=[{'unit': n.UNIT, 'properties': dict(self.service, ActiveState='active'), 'pids': []}],
                        units=[{'unit': n.UNIT, 'properties': dict(self.service, ActiveState='inactive', MainPID='0', ControlPID='0', SubState='dead', InvocationID=''), 'pids': []}])
        n.validate_timing(self.data, timing, result, shutdown)
        for key, val in [('observation_end_mono', timing['baseline_mono'] + 1799), ('continuous', False)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                n.validate_timing(self.data, timing, dict(result, **{key: val}), shutdown)
        with self.assertRaises(ValueError):
            n.validate_timing(self.data, timing, result, dict(shutdown, runtime_fallback=True))


if __name__ == '__main__':
    unittest.main()
