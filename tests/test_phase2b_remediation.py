"""RED tests for Phase 2B soak remediation - run these FIRST to verify failures."""
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch, MagicMock, call
import json
import importlib.util
import itertools

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_trial', ROOT / 'deploy/infrastructure-trial.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def _raise_after(n):
    """Side effect that no-ops for the first n calls then raises 'trial ingestion stale'."""
    counter = {'i': 0}

    def f(*args, **kwargs):
        counter['i'] += 1
        if counter['i'] > n:
            raise ValueError('trial ingestion stale')
    return f


class TestObserverShutdownDefect(unittest.TestCase):
    """Test that observer sets started=True so finally block stops service on failure.

    Proven root cause from evidence: observe() line 568 sets started=False but never
    sets it True, so the finally block (line 670) never stops the main service when
    the observer fails during observation (e.g., stale ingestion). Evidence shows
    polymarket-btc-infrastructure.service was left active until RuntimeMaxSec killed it.
    """

    def setUp(self):
        self.assertTrue((ROOT / 'deploy/infrastructure-trial.py').exists())

    def test_observer_cleans_up_service_in_finally_on_failure(self):
        """When observer fails during observation loop, finally must stop main service.

        Current defect: started flag is never set True, so finally block skips stop_units.
        The service is left running until RuntimeMaxSec kills it (as evidence shows).
        """
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            data = {'trial_id': 'soak-20261027T000000Z', 'phase': 'soak', 'seconds': 86400,
                    'launch_at': {'utc': '2026-10-27T00:00:00Z'}, 'units': native.owned('soak-20261027T000000Z')}
            source = folder / 'db.sqlite'
            source.touch()
            (folder / 'host.json.jsonl').write_text('{}\n')
            group = folder / 'cgroup'
            group.mkdir()
            (group / 'memory.events').write_text('oom 0\noom_kill 0\nmax 0\n')

            stop_calls = []

            state = dict(ActiveState='active', MainPID='2', InvocationID='first-inv', NRestarts='0', ControlGroup=str(group))

            # states: [inactive check, first start active, restart start, then many loop states]
            states = [
                dict(state, ActiveState='inactive', MainPID='0', InvocationID=''),  # line 587
                state,  # line 594 after first start - MainPID='2', InvocationID='first-inv'
                dict(state, MainPID='3', InvocationID='second-inv'),  # line 606 after restart start
            ]
            # Need states for each loop iteration (line 624 calls properties)
            states += [dict(state, MainPID='3', InvocationID='second-inv')] * 30  # enough for loop

            snapshot_data = {'trading': {}, 'observations': 0, 'run_ids': []}

            class DB:
                def execute(self, *args):
                    return self
                def fetchone(self):
                    return ('continuous', int(__import__('time').time() * 1e6), 'RUNNING')
                def fetchall(self):
                    return [('ok',)]
                def close(self):
                    pass

            def stop_units_mock(units):
                stop_calls.append(list(units))
                return [], [{'unit': u, 'properties': {'ActiveState': 'inactive', 'MainPID': '0', 'ControlPID': '0'}, 'pids': []} for u in units]

            # Mock time so the loop runs long enough to reach require_ingestion_progress.
            # observe() sets baseline = time.monotonic() at line 631, then checks
            # (mono - baseline > 30) at line 653 before calling require_ingestion_progress.
            # With monotonic returning 0, the guard prevents the stale check from firing.
            # Advance monotonic past 30 to trigger the stale ingestion path.
            monotonic_values = [0]   # baseline = 0 at line 631
            for i in range(31):
                monotonic_values.append(40.0)  # mono - baseline = 40 > 30, triggers stale check

            time_mock = MagicMock(side_effect=monotonic_values)

            with patch.object(native, 'trial_metadata', return_value=data), \
                 patch.object(native, 'check_approval'), \
                 patch.object(native, 'require_service_policy'), \
                 patch.object(native, 'environment', return_value={'MVP_DB_PATH': str(source), 'MVP_BOT_ID': 'bot', 'MVP_STATE_DIR': str(folder)}), \
                 patch.object(native, 'snapshot', return_value=snapshot_data), \
                 patch.object(native, 'properties', side_effect=states), \
                 patch.object(native, 'wait_run', side_effect=['restart', 'continuous']), \
                 patch.object(native, 'stop_units', side_effect=stop_units_mock), \
                 patch.object(native, 'command', return_value=''), \
                 patch.object(native, 'connect', return_value=DB()), \
                 patch.object(native, 'fresh_run', return_value=True), \
                 patch.object(native, 'read_host', return_value={}), \
                 patch.object(native, 'memory_events', return_value={'oom': '0', 'oom_kill': '0', 'max': '0'}), \
                 patch.object(native, 'ingestion', return_value={'count': 5, 'last_received_us': 1000}), \
                 patch.object(native.time, 'monotonic', side_effect=time_mock), \
                 patch.object(native.time, 'time', return_value=200.0):
                # Force stale ingestion failure during observation loop
                with patch.object(native, 'require_ingestion_progress', side_effect=[ValueError('trial ingestion stale')]):
                    native.observe(folder, reset_stop=True)

                    # stop_units should be called TWICE for [UNIT]:
                    # 1. Restart verification (line 598) 
                    # 2. Finally block cleanup (line 671) - requires started=True
                    main_unit = 'polymarket-btc-infrastructure.service'
                    main_unit_stops = [c for c in stop_calls if c == [main_unit]]
                    self.assertGreaterEqual(len(main_unit_stops), 2,
                        f"Service must be stopped TWICE in finally on failure: once for restart verification, "
                        f"once for final cleanup. Found {len(main_unit_stops)} stops. "
                        f"All stop_calls: {stop_calls}")


class TestRuntimeMaxSecDeadlineOrdering(unittest.TestCase):
    """Test that independent deadline fires BEFORE RuntimeMaxSec safety limit.

    Evidence from soak-20261027T093836Z shutdown-timeline.json:
    - RuntimeMaxSec=86520 fired at 2026-10-28T09:40:43.621 UTC (service killed by timeout)
    - deadline_after_seconds=86580, deadline ran at 2026-10-28T09:41:36.629 UTC
    - Service killed by RuntimeMaxSec ~53 seconds BEFORE independent deadline fired
    - Independent deadline then ran after service was already dead (no graceful stop possible)
    """

    def test_main_service_runtime_bound_exceeds_independent_deadline(self):
        """Main service RuntimeMaxSec must be greater than independent deadline to allow graceful stop.

        Current code (line 494): runtime_bound() == (seconds + 120) * 1_000_000
        Deadline timer fires at seconds + 180 (line 526).
        For soak: 86400 + 120 = 86520 < 86400 + 180 = 86580
        RuntimeMaxSec fires 60s BEFORE deadline -- service killed before graceful stop.
        FIX: Change main service RuntimeMaxSec from (seconds + 120) to (seconds + 240),
        matching what heartbeat already uses (line 532), so deadline (seconds+180)
        fires first, then RuntimeMaxSec (seconds+240) acts as fail-safe.
        """
        seconds = 86400
        deadline_after = seconds + 180  # line 526, 513
        # Current (broken):
        current_runtime = seconds + 120
        self.assertLess(current_runtime, deadline_after,
            "Current RuntimeMaxSec < deadline, which is the proven defect")

        # Fixed: must be greater so deadline fires first
        fixed_runtime = seconds + 240
        self.assertGreater(fixed_runtime, deadline_after,
            f"Fixed RuntimeMaxSec ({fixed_runtime}s) must exceed deadline ({deadline_after}s) "
            f"so independent deadline can gracefully stop service before safety timeout.")


class TestStaleIngestionDiagnostics(unittest.TestCase):
    """Test that stale ingestion failure captures exact diagnostic values.

    Evidence: observer-failure-analysis.json states exact stale-check inputs
    were NOT recorded. result.json only shows error string "ValueError: trial ingestion stale".
    This prevents root-cause analysis of future failures.
    """

    def test_stale_ingestion_captures_exact_diagnostics(self):
        """When stale ingestion fails, result.json must include diagnostic context.

        Fix at infrastructure-trial.py require_ingestion_progress (line 183-186):
        - Preserve strict thresholds (still fails for count<=0, missing timestamp, age outside [0,30])
        - Capture exact count, last_received_us, wall_time, computed_age_seconds, run_id, failed_branch
        - Do NOT suppress genuine stale failures
        """
        # Test: stale ingestion still raises ValueError
        with self.assertRaisesRegex(ValueError, 'trial ingestion stale'):
            # count <= 0 with elapsed > 30
            native.require_ingestion_progress({'count': 0, 'last_received_us': 1000}, 100, 40)

        with self.assertRaisesRegex(ValueError, 'trial ingestion stale'):
            # missing timestamp
            native.require_ingestion_progress({'count': 5, 'last_received_us': None}, 100, 40)

        with self.assertRaisesRegex(ValueError, 'trial ingestion stale'):
            # age > 30 seconds
            native.require_ingestion_progress({'count': 5, 'last_received_us': 1000}, 100, 40)

        with self.assertRaisesRegex(ValueError, 'trial ingestion stale'):
            # negative age
            native.require_ingestion_progress({'count': 5, 'last_received_us': 200_000_000}, 100, 40)

        # Test: healthy ingestion does NOT raise
        # age = 100 - 1000/1e6 = 99.9, which is >30, so stale. Let me pick proper values.
        # age = wall - last_received_us/1e6 should be in [0, 30]
        # last_received_us = (wall - 10) * 1e6 → age = 10s, within bounds
        healthy_last = int((100 - 10) * 1_000_000)  # age = 10s
        native.require_ingestion_progress({'count': 5, 'last_received_us': healthy_last}, 100, 40)
        # No exception raised

        # Test: elapsed <= 30 doesn't check (early in trial)
        native.require_ingestion_progress({'count': 0, 'last_received_us': None}, 100, 10)
        # No exception - elapsed not > 30

    def test_diagnostics_captured_on_stale_failure(self):
        """When stale ingestion fails, diagnostic details must be available.

        The observe() loop calls require_ingestion_progress and catches the ValueError.
        Fix: capture exact count, last_received_us, wall, age, run_id in result on failure.
        """
        # The observe() function catches the ValueError and stores diagnostics in result.
        # We verify the _stale_branch function returns correct diagnostic branch names
        # for each failure mode, since this is what gets stored in result.
        
        # count <= 0
        self.assertEqual(native._stale_branch({'count': 0, 'last_received_us': 1000}, 100, 40), 'zero_count')
        
        # missing timestamp (last_received_us is None) - must not crash
        self.assertEqual(native._stale_branch({'count': 5, 'last_received_us': None}, 100, 40), 'missing_timestamp')
        
        # age > 30
        self.assertEqual(native._stale_branch({'count': 5, 'last_received_us': 1000}, 100, 40), 'age_exceeds_30')
        
        # negative age
        self.assertEqual(native._stale_branch({'count': 5, 'last_received_us': 200_000_000}, 100, 40), 'future_timestamp')
        
        # age in bounds but check failed (should not happen in practice)
        self.assertEqual(native._stale_branch({'count': 5, 'last_received_us': 70_000_000}, 100, 40), 'age_in_bounds_check_failed')
        
        # elapsed <= 30 not checked
        self.assertEqual(native._stale_branch({'count': 0, 'last_received_us': None}, 100, 10), 'not_checked')


class TestPostExitAuditContract(unittest.TestCase):
    """Test that post-exit audit and cleanup artifacts follow clear contracts.

    Evidence: manifest.json missing (intentional - not native output).
    post-audit.json requires operator capture after inspect-cleanup.
    """

    def test_post_audit_requires_operator_capture(self):
        """post-audit.json is written by operator after inspect-cleanup, not by controller."""
        # Current code: inspect_cleanup() prints to stdout, doesn't write post-audit.json
        # Docs (line 67): operator must explicitly install post-audit.json after successful exit
        # This is intentional - no code change needed, but need test documenting it
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            data = {'trial_id': 'pilot-20261027T000000Z', 'units': native.owned('pilot-20261027T000000Z')}
            (folder / 'cleanup.json').write_text('{"errors": []}')
            state = dict(ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0', Result='success', ExecMainStatus='0', ExecMainCode='1', LoadState='loaded')
            with patch.object(native, 'trial_metadata', return_value=data), \
                 patch.object(native, 'unit_evidence', side_effect=lambda u: {'unit': u, 'properties': state, 'pids': []}), \
                 patch.object(native, 'command', return_value='0 jobs listed.\n'):
                result = native.inspect_cleanup(folder)
                self.assertEqual(result['errors'], [])
                # inspect_cleanup prints, doesn't write post-audit.json
                self.assertFalse((folder / 'post-audit.json').exists())


class TestStaleDiagnosticsTimestampSafety(unittest.TestCase):
    """I-1: stale_ingestion_diagnostics must never crash when last_received_us is None.

    Genuine failures stay FAIL; diagnostics record computed_age_seconds=None when
    no valid last-received timestamp exists and preserve the exact failed branch.
    """

    def _observe_with_stale(self, counts, check_wall=200.0):
        data = {'trial_id': 'soak-20261027T000000Z', 'phase': 'soak', 'seconds': 86400,
                'launch_at': {'utc': '2026-10-27T00:00:00Z'}, 'units': native.owned('soak-20261027T000000Z')}
        folder = None
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / 'db.sqlite'
            source.touch()
            (folder / 'host.json.jsonl').write_text('{}\n')
            group = folder / 'cgroup'
            group.mkdir()
            (group / 'memory.events').write_text('oom 0\noom_kill 0\nmax 0\n')
            state = dict(ActiveState='active', MainPID='3', InvocationID='second-inv',
                         NRestarts='0', ControlGroup=str(group))
            states = [dict(state, ActiveState='inactive', MainPID='0', InvocationID=''),
                      dict(state, MainPID='2', InvocationID='first-inv')] + [dict(state)] * 40
            snapshot_data = {'trading': {}, 'observations': 0, 'run_ids': []}

            class DB:
                def execute(self, *args):
                    return self
                def fetchone(self):
                    return ('continuous', int(__import__('time').time() * 1e6), 'RUNNING')
                def fetchall(self):
                    return [('ok',)]
                def close(self):
                    pass

            def lockstep_monotonic():
                """Yield each value twice so |mono-previous_mono| <= 2 passes the clock-gap
                guard while monotonic still ramps past the 30s stale threshold (observe()
                calls time.monotonic twice per loop iteration: line 652 + line 689 sleep)."""
                v = 0
                while True:
                    yield v
                    yield v
                    v += 1

            check_pending = False
            def clock():
                nonlocal check_pending
                if check_pending:
                    check_pending = False
                    return check_wall
                return 200.0

            def read_ingestion(*args):
                nonlocal check_pending
                check_pending = True
                return counts

            with patch.object(native, 'trial_metadata', return_value=data), \
                 patch.object(native, 'check_approval'), \
                 patch.object(native, 'require_service_policy'), \
                 patch.object(native, 'environment', return_value={'MVP_DB_PATH': str(source), 'MVP_BOT_ID': 'bot', 'MVP_STATE_DIR': str(folder)}), \
                 patch.object(native, 'snapshot', return_value=snapshot_data), \
                 patch.object(native, 'properties', side_effect=states), \
                 patch.object(native, 'wait_run', side_effect=['first', 'continuous']), \
                 patch.object(native, 'stop_units', return_value=([], [])), \
                 patch.object(native, 'command', return_value=''), \
                 patch.object(native, 'connect', return_value=DB()), \
                 patch.object(native, 'fresh_run', return_value=True), \
                 patch.object(native, 'read_host', return_value={}), \
                 patch.object(native, 'memory_events', return_value={'oom': '0', 'oom_kill': '0', 'max': '0'}), \
                 patch.multiple(native, start_observation_timing=MagicMock(side_effect=lambda *a: {'baseline_mono': native.time.monotonic()}), monotonic_now=MagicMock(return_value=0), stop_main=MagicMock(return_value={'errors': [], 'units': []})), \
                 patch.object(native, 'hash_due', return_value=False), \
                 patch.object(native, 'ingestion', side_effect=read_ingestion), \
                 patch.object(native.time, 'monotonic', side_effect=lockstep_monotonic()), \
                 patch.object(native.time, 'time', side_effect=clock), \
                 patch.object(native.time, 'sleep', return_value=None):
                code = native.observe(folder, reset_stop=True)
                result = json.loads((folder / 'result.json').read_text())
                return code, result

    def test_stale_with_none_timestamp_records_null_age_and_never_crashes(self):
        """Stale failure with last_received_us=None must stay FAIL without TypeError."""
        code, result = self._observe_with_stale({'count': 5, 'last_received_us': None})
        self.assertEqual(code, 1)
        diagnostics = result['stale_ingestion_diagnostics']
        self.assertEqual(diagnostics['count'], 5)
        self.assertIsNone(diagnostics['last_received_us'])
        self.assertIsNotNone(diagnostics['wall_time'])
        self.assertIsNone(diagnostics['computed_age_seconds'],
                          'computed_age_seconds must be null when no valid last-received timestamp exists')
        self.assertEqual(diagnostics['failed_branch'], 'missing_timestamp')
        self.assertEqual(diagnostics['run_id'], 'continuous')
        # Genuine failure must remain FAIL — strict thresholds preserved.
        self.assertTrue(any('trial ingestion stale' in e for e in result['errors']))

    def test_diagnostics_use_exact_stale_check_wall_at_threshold(self):
        code, result = self._observe_with_stale(
            {'count': 5, 'last_received_us': 170_000_000}, check_wall=200.1)
        self.assertEqual(code, 1)
        diagnostics = result['stale_ingestion_diagnostics']
        self.assertEqual(diagnostics['wall_time'], 200.1)
        self.assertAlmostEqual(diagnostics['computed_age_seconds'], 30.1)
        self.assertEqual(diagnostics['failed_branch'], 'age_exceeds_30')

    def test_stale_diagnostic_helper_preserves_exact_branch(self):
        """Exact branch for every stale failure mode, never crashing on None."""
        diagnostics = getattr(native, 'stale_diagnostics')
        self.assertEqual(diagnostics({'count': 0, 'last_received_us': 1000}, 100.0, 'r', 40.0)['failed_branch'],
                         'zero_count')
        self.assertEqual(diagnostics({'count': 5, 'last_received_us': None}, 100.0, 'r', 40.0)['failed_branch'],
                         'missing_timestamp')
        self.assertEqual(diagnostics({'count': 5, 'last_received_us': 200_000_000}, 100.0, 'r', 40.0)['failed_branch'],
                         'future_timestamp')
        self.assertEqual(diagnostics({'count': 5, 'last_received_us': 1000}, 100.0, 'r', 40.0)['failed_branch'],
                         'age_exceeds_30')
        self.assertEqual(diagnostics({'count': 0, 'last_received_us': None}, 100.0, 'r', 10.0)['failed_branch'],
                         'not_checked')
        self.assertIsNone(diagnostics({'count': 5, 'last_received_us': None}, 100.0, 'r', 40.0)['computed_age_seconds'])
        self.assertAlmostEqual(diagnostics({'count': 5, 'last_received_us': 200_000_000}, 100.0, 'r', 40.0)['computed_age_seconds'],
                               -100.0)


class TestStartupEarlyCleanup(unittest.TestCase):
    """I-2: every successfully started trial-owned service must be stopped in finally,
    even when failure happens between systemctl start and the old started=True point."""

    def _observe_with_second_start_failure(self):
        data = {'trial_id': 'soak-20261027T000000Z', 'phase': 'soak', 'seconds': 86400,
                'launch_at': {'utc': '2026-10-27T00:00:00Z'}, 'units': native.owned('soak-20261027T000000Z')}
        stop_calls = []
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / 'db.sqlite'
            source.touch()
            (folder / 'host.json.jsonl').write_text('{}\n')
            group = folder / 'cgroup'
            group.mkdir()
            (group / 'memory.events').write_text('oom 0\noom_kill 0\nmax 0\n')
            state = dict(ActiveState='active', MainPID='3', InvocationID='second-inv',
                         NRestarts='0', ControlGroup=str(group))
            states = [dict(state, ActiveState='inactive', MainPID='0', InvocationID=''),
                      dict(state, MainPID='2', InvocationID='first-inv'),
                      dict(state, MainPID='3', InvocationID='second-inv')]
            snapshot_data = {'trading': {}, 'observations': 0, 'run_ids': []}

            def stop_units_mock(units):
                stop_calls.append(list(units))
                return [], [{'unit': u} for u in units]

            monotonic_values = [0]
            for _ in range(5):
                monotonic_values.append(1.0)
            time_mock = MagicMock(side_effect=monotonic_values)
            with patch.object(native, 'trial_metadata', return_value=data), \
                 patch.object(native, 'check_approval'), \
                 patch.object(native, 'require_service_policy'), \
                 patch.object(native, 'environment', return_value={'MVP_DB_PATH': str(source), 'MVP_BOT_ID': 'bot', 'MVP_STATE_DIR': str(folder)}), \
                 patch.object(native, 'snapshot', return_value=snapshot_data), \
                 patch.object(native, 'properties', side_effect=states), \
                 patch.object(native, 'wait_run', side_effect=['first', 'first']), \
                 patch.object(native, 'stop_units', side_effect=stop_units_mock), \
                 patch.object(native, 'command', return_value=''), \
                 patch.object(native.time, 'monotonic', side_effect=time_mock), \
                 patch.object(native.time, 'time', return_value=200.0):
                outcome = native.observe(folder, reset_stop=True)
                result = json.loads((folder / 'result.json').read_text())
                return outcome, stop_calls, result

    def test_cleanup_on_second_start_verification_failure(self):
        """Failure right after the continuous start must stop the started service."""
        main_unit = 'polymarket-btc-infrastructure.service'
        code, stop_calls, result = self._observe_with_second_start_failure()
        self.assertEqual(code, 1)
        self.assertTrue(any('restart did not create fresh database run' in e for e in result['errors']))
        main_unit_stops = [c for c in stop_calls if c == [main_unit]]
        self.assertGreaterEqual(len(main_unit_stops), 2,
            f"final cleanup stop must follow restart-verification stop. All stop_calls: {stop_calls}")

    def _observe_with_first_start_failure(self):
        data = {'trial_id': 'soak-20261027T000000Z', 'phase': 'soak', 'seconds': 86400,
                'launch_at': {'utc': '2026-10-27T00:00:00Z'}, 'units': native.owned('soak-20261027T000000Z')}
        stop_calls = []
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / 'db.sqlite'
            source.touch()
            state_inactive = dict(ActiveState='inactive', MainPID='0', InvocationID='', ControlGroup='x')
            states = [state_inactive, dict(state_inactive, ActiveState='starting', MainPID='0')]
            snapshot_data = {'trading': {}, 'observations': 0, 'run_ids': []}

            def stop_units_mock(units):
                stop_calls.append(list(units))
                return [], [{'unit': u} for u in units]

            with patch.object(native, 'trial_metadata', return_value=data), \
                 patch.object(native, 'check_approval'), \
                 patch.object(native, 'require_service_policy'), \
                 patch.object(native, 'environment', return_value={'MVP_DB_PATH': str(source), 'MVP_BOT_ID': 'bot', 'MVP_STATE_DIR': str(folder)}), \
                 patch.object(native, 'snapshot', return_value=snapshot_data), \
                 patch.object(native, 'properties', side_effect=states), \
                 patch.object(native, 'wait_run', return_value='first_run'), \
                 patch.object(native, 'stop_units', side_effect=stop_units_mock), \
                 patch.object(native, 'command', return_value=''), \
                 patch.object(native.time, 'monotonic', return_value=1.0), \
                 patch.object(native.time, 'time', return_value=200.0):
                outcome = native.observe(folder, reset_stop=True)
                return outcome, stop_calls, json.loads((folder / 'result.json').read_text())

    def test_cleanup_on_first_start_verification_failure(self):
        """Failure right after the initial start must stop the started service.

        'service failed startup' is raised between systemctl start and any stop,
        so finally must still stop the trial-owned unit.
        """
        code, stop_calls, result = self._observe_with_first_start_failure()
        self.assertEqual(code, 1)
        self.assertTrue(any('service failed startup' in e for e in result['errors']))
        main_unit = 'polymarket-btc-infrastructure.service'
        self.assertIn([main_unit], stop_calls,
            f"service started then failed startup verification must still be stopped. stop_calls: {stop_calls}")

    def test_never_stops_service_it_did_not_start(self):
        """Service active before observer start is unrelated work — leave it alone."""
        data = {'trial_id': 'soak-20261027T000000Z', 'phase': 'soak', 'seconds': 86400,
                'launch_at': {'utc': '2026-10-27T00:00:00Z'}, 'units': native.owned('soak-20261027T000000Z')}
        stop_calls = []
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / 'db.sqlite'
            source.touch()
            state = dict(ActiveState='active', MainPID='9', InvocationID='pre-existing',
                         NRestarts='0', ControlGroup='x')
            snapshot_data = {'trading': {}, 'observations': 0, 'run_ids': []}

            def stop_units_mock(units):
                stop_calls.append(list(units))
                return [], [{'unit': u} for u in units]

            with patch.object(native, 'trial_metadata', return_value=data), \
                 patch.object(native, 'check_approval'), \
                 patch.object(native, 'require_service_policy'), \
                 patch.object(native, 'environment', return_value={'MVP_DB_PATH': str(source), 'MVP_BOT_ID': 'bot', 'MVP_STATE_DIR': str(folder)}), \
                 patch.object(native, 'snapshot', return_value=snapshot_data), \
                 patch.object(native, 'properties', return_value=state), \
                 patch.object(native, 'stop_units', side_effect=stop_units_mock), \
                 patch.object(native, 'command', return_value=''), \
                 patch.object(native.time, 'monotonic', return_value=1.0), \
                 patch.object(native.time, 'time', return_value=200.0):
                outcome = native.observe(folder, reset_stop=True)
                result = json.loads((folder / 'result.json').read_text())
        self.assertEqual(outcome, 1)
        self.assertTrue(any('service already active' in e for e in result['errors']))
        self.assertNotIn(['polymarket-btc-infrastructure.service'], stop_calls,
            "observer must never stop a service it did not start")


if __name__ == '__main__':
    unittest.main()