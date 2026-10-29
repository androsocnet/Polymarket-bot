"""Behavior regressions for native infrastructure evidence, no VPS access."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('observer', ROOT / 'deploy/paper-validation.py')
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


class HeartbeatRegression(unittest.TestCase):
    def test_current_sample_also_reaches_append_only_log(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'host.json'
            # Negative duration makes one real iteration, without waiting.
            result = subprocess.run(['bash', str(ROOT / 'deploy/infrastructure-host-heartbeat.sh'),
                                     str(output), '-120'], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            sample = json.loads(output.read_text())
            records = [json.loads(line) for line in Path(str(output) + '.jsonl').read_text().splitlines()]
            self.assertEqual(records, [sample])
            self.assertEqual(sample['sequence'], 1)


class NativeEvidenceRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / 'deploy/infrastructure-trial.py'
        if not path.exists():
            return
        spec = importlib.util.spec_from_file_location('native_trial', path)
        cls.native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.native)

    def setUp(self):
        self.assertTrue((ROOT / 'deploy/infrastructure-trial.py').exists(),
                        'native trial controller missing; paper observer cannot validate infrastructure')

    def test_evidence_and_database_have_distinct_trust_parents(self):
        self.assertEqual(self.native.EVIDENCE, Path('/var/lib/polymarket-btc-infrastructure/evidence'))
        self.assertEqual(self.native.DATA, Path('/var/lib/polymarket-btc-infrastructure/data'))
        self.assertNotIn(self.native.DATA, self.native.EVIDENCE.parents)

    def test_approval_rejects_writable_ancestor_before_reading_json(self):
        with tempfile.TemporaryDirectory() as directory:
            approval = Path(directory) / 'approval.json'
            approval.write_text('{"authorized": true}')
            approval.chmod(0o600)
            with self.assertRaisesRegex(ValueError, 'trusted|root|writable'):
                self.native.secure_bytes(approval)

    def test_approval_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            approval = Path(directory) / 'approval.json'
            approval.symlink_to('/etc/hostname')
            with self.assertRaises((ValueError, OSError)):
                self.native.secure_bytes(approval)

    def test_secure_descriptor_reads_root_owned_regular_file(self):
        self.assertEqual(self.native.secure_bytes(Path('/etc/hostname')), Path('/etc/hostname').read_bytes())

    def test_unmanifested_release_root_import_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'polymarket_bot'
            package.mkdir()
            (package / '__init__.py').write_text('unreviewed = True')
            with self.assertRaisesRegex(ValueError, 'untracked'):
                self.native.verify_artifacts(root, {'files': {}})

    def test_installer_manifest_accepts_real_native_venv(self):
        import os
        import shutil
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, release = root / 'source', root / 'release'
            (source / 'src').mkdir(parents=True)
            (source / 'deploy').mkdir()
            (source / 'src/example.py').write_text('VALUE = 1\n')
            (source / 'deploy/helper.sh').write_text('#!/bin/bash\nexit 0\n')
            (source / 'pyproject.toml').write_text('[project]\nname="fixture"\n')
            (source / 'uv.lock').write_text('version = 1\n')
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'source'], check=True)
            sha = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
            shutil.copytree(source, release, ignore=shutil.ignore_patterns('.git'))
            subprocess.run([sys.executable, '-m', 'venv', '--copies', '--without-pip', str(release / '.venv')], check=True)
            script = (ROOT / 'deploy/install-infrastructure.sh').read_text().split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
            subprocess.run([sys.executable, '-', str(release), sha, str(source)], input=script, text=True, check=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
            manifest = json.loads((release / 'runtime-manifest.json').read_text())
            self.native.verify_artifacts(release, manifest)
            self.native.verify_host_runtime(manifest['host_python'])
            self.assertEqual(manifest['symlinks'].get('.venv/lib64'), 'lib')
            self.assertTrue(any('__pycache__' in name for name in manifest['host_python']['files']))

    def test_native_venv_lib64_alias_is_bound_not_blanket_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.venv/lib').mkdir(parents=True)
            alias = root / '.venv/lib64'
            alias.symlink_to('lib', target_is_directory=True)
            manifest = {'files': {}, 'symlinks': {'.venv/lib64': 'lib'}}
            self.native.verify_artifacts(root, manifest)
            alias.unlink()
            alias.symlink_to('/usr/lib', target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'artifact'):
                self.native.verify_artifacts(root, manifest)

    def test_host_bytecode_injection_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / '__pycache__'
            cache.mkdir()
            (cache / 'sqlite3.cpython-314.pyc').write_bytes(b'injected')
            with self.assertRaisesRegex(ValueError, 'host runtime'):
                self.native.verify_host_runtime({'roots': [str(root)], 'files': {}})

    def test_external_python_runtime_mutation_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sqlite3.py'
            path.write_text('approved')
            import hashlib
            baseline = {'files': {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}}
            self.native.verify_host_runtime(baseline)
            path.write_text('modified')
            with self.assertRaisesRegex(ValueError, 'host runtime'):
                self.native.verify_host_runtime(baseline)

    def test_invalid_approval_launch_has_no_systemd_or_filesystem_effect(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'approval.json'
            path.write_text('{}')
            with patch.object(self.native, 'secure_bytes', return_value=b'{}'), patch.object(self.native, 'runtime_identity', return_value=('expected', {'git_sha': 'a' * 40})), patch.object(self.native, 'command', return_value='/etc/polymarket-btc/infrastructure.env (ignore_errors=no)') as call:
                with self.assertRaisesRegex(ValueError, 'fingerprint'):
                    self.native.launch('pilot', path, True)
                self.assertTrue(all(c.args[:2] == ('systemctl', 'show') for c in call.call_args_list))
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ['approval.json'])

    def test_pilot_requires_exact_1800_second_approval_before_effects(self):
        from unittest.mock import patch
        approval_path = Path('/approved-pilot.json')
        with patch.object(self.native, 'check_approval', side_effect=ValueError('approval rejected')) as check:
            with self.assertRaisesRegex(ValueError, 'approval rejected'):
                self.native.launch('pilot', approval_path)
        check.assert_called_once_with(approval_path, 'pilot', 1800)

    def test_garbage_collected_transient_unit_is_already_clean(self):
        from unittest.mock import patch
        state = dict(LoadState='not-found', ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0', ControlGroup='')
        with patch.object(self.native, 'properties', return_value=state), patch.object(self.native, 'command', side_effect=subprocess.CalledProcessError(5, 'stop')):
            errors, evidence = self.native.stop_units(['infra-pilot-20261027T000000Z-deadline.service'])
        self.assertEqual(errors, [])
        self.assertEqual(evidence[0]['pids'], [])

    def test_stop_failure_and_child_processes_remain_blocked(self):
        from unittest.mock import patch
        state = dict(ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0', ControlGroup='/trial')
        with patch.object(self.native, 'properties', return_value=state), patch.object(self.native, 'command', side_effect=subprocess.CalledProcessError(1, 'stop')), patch.object(self.native, 'cgroup_pids', return_value=[123]):
            errors, evidence = self.native.stop_units(['infra-pilot-20261027T000000Z-observer.service'])
        self.assertEqual(len(errors), 2)
        self.assertEqual(evidence[0]['pids'], [123])

    def test_host_sample_written_during_systemd_query_is_not_future_clock_fault(self):
        import io
        sample = dict(session='s', boot='b', sequence=1, wall=100.05, mono=50, boottime=60, interrupted=0)
        self.assertEqual(self.native.read_host(io.StringIO(json.dumps(sample) + '\n'), None, 100), sample)

    def test_incremental_host_reader_preserves_partial_tail(self):
        import io
        first = dict(session='s', boot='b', sequence=1, wall=100, mono=50, boottime=60, interrupted=0)
        stream = io.StringIO(json.dumps(first) + '\n' + '{"partial"')
        self.assertEqual(self.native.read_host(stream, None, 100), first)
        position = stream.tell()
        self.assertEqual(self.native.read_host(stream, first, 100), first)
        self.assertEqual(stream.tell(), position)

    def test_launch_submits_native_jobs_with_exact_owned_names(self):
        from unittest.mock import patch
        import os
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            evidence_dir = state_dir / 'evidence'
            approval_path = state_dir / 'approval.json'
            approval_path.write_text('{}')
            state = dict(ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0', ControlGroup='', Restart='no')
            commands = []
            def run(*args):
                commands.append(args)
                if 'infrastructure-host-heartbeat.sh' in ' '.join(args):
                    (next(evidence_dir.iterdir()) / 'host.json').write_text('{}')
                return ''
            def props(unit):
                return dict(state, ActiveState='active') if unit.endswith('.timer') else state
            with patch.object(self.native, 'secure_bytes', return_value=b''), patch.object(self.native, 'STATE', state_dir), patch.object(self.native, 'EVIDENCE', evidence_dir), patch.object(self.native, 'check_approval', return_value=('hash', {'git_sha': 'a' * 40})), patch.object(self.native, 'environment', return_value={'MVP_STATE_DIR': str(state_dir)}), patch.object(self.native, 'properties', side_effect=props), patch.object(self.native, 'runtime_bound', return_value=2040000000), patch.object(self.native, 'require_service_policy'), patch.object(self.native, 'command', side_effect=run):
                self.assertEqual(self.native.launch('pilot', approval_path), 0)
            data = json.loads((next(evidence_dir.iterdir()) / 'launch.json').read_text())
            self.assertEqual([c[1] for c in commands], ['--unit=' + data['units'][4][:-6], '--unit=' + data['units'][3][:-8], '--unit=' + data['units'][0], '--unit=' + data['units'][1]])
            for call in commands:
                self.assertIn('--property=RemainAfterExit=no', call)
                self.assertNotIn('--remain-after-exit=no', call)
            self.assertEqual(data['seconds'], 1800)
            self.assertEqual(data['deadline_after_seconds'], 1980)
            self.assertEqual(data['audit_after_seconds'], 2400)
            self.assertTrue(any('--on-active=1980s' in c for c in commands))
            self.assertTrue(any('--on-active=2400s' in c for c in commands))
            for call in commands[2:]:
                self.assertIn('--property=RuntimeMaxSec=2040', call)

    def test_launch_failure_disarms_timers_after_verified_cleanup(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            approval_path = state_dir / 'approval.json'
            approval_path.write_text('{}')
            state = dict(ActiveState='inactive', SubState='dead', MainPID='0', ControlPID='0', ControlGroup='', Restart='no')
            stopped = []
            def stop(units):
                stopped.extend(units)
                return [], []
            with patch.object(self.native, 'secure_bytes', return_value=b''), patch.object(self.native, 'STATE', state_dir), patch.object(self.native, 'EVIDENCE', state_dir / 'evidence'), patch.object(self.native, 'check_approval', return_value=('hash', {'git_sha': 'a' * 40})), patch.object(self.native, 'environment', return_value={'MVP_STATE_DIR': str(state_dir)}), patch.object(self.native, 'properties', return_value=state), patch.object(self.native, 'runtime_bound', return_value=2040000000), patch.object(self.native, 'require_service_policy'), patch.object(self.native, 'command', side_effect=RuntimeError('submission failed')), patch.object(self.native, 'stop_units', side_effect=stop):
                with self.assertRaisesRegex(RuntimeError, 'submission failed'):
                    self.native.launch('pilot', approval_path)
            self.assertTrue(any(u.endswith('-deadline.timer') for u in stopped))
            self.assertTrue(any(u.endswith('-audit.timer') for u in stopped))

    def test_timer_without_service_pid_properties_is_quiescent(self):
        from unittest.mock import patch
        with patch.object(self.native, 'properties', return_value={'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead'}):
            item = self.native.unit_evidence('infra-pilot-20261027T000000Z-deadline.timer')
        self.assertTrue(self.native.quiescent(item['properties'], item['pids']))

    def test_final_audit_inspection_is_read_only_and_includes_audit_service(self):
        from unittest.mock import patch
        data = {'trial_id': 'pilot-20261027T000000Z', 'units': self.native.owned('pilot-20261027T000000Z')}
        def item(unit):
            return {'unit': unit, 'properties': {'ActiveState': 'inactive', 'SubState': 'dead', 'MainPID': '0', 'ControlPID': '0'}, 'pids': []}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'cleanup.json').write_text('{"errors": []}')
            with patch.object(self.native, 'trial_metadata', return_value=data), patch.object(self.native, 'unit_evidence', side_effect=item), patch.object(self.native, 'command', return_value='0 jobs listed.\n'):
                result = self.native.inspect_cleanup(folder)
            self.assertEqual(result['errors'], [])
            self.assertIn(data['units'][-1], [i['unit'] for i in result['units']])
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ['cleanup.json'])

    def test_three_file_pilot_bundle_cannot_authorize_soak(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'pilot-20261027T000000Z'
            folder.mkdir()
            for name, value in [('result.json', {'continuous': True, 'errors': []}), ('cleanup.json', {'errors': []}), ('restart-verification.json', {'errors': []})]:
                (folder / name).write_text(json.dumps(value))
            import hashlib
            files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
            approval = {'pilot_trial_id': folder.name, 'pilot_evidence_sha256': hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
            with self.assertRaisesRegex(ValueError, 'pilot'):
                self.native.verify_pilot(approval, root)

    def test_soak_valid_pilot_bundle_is_bound_and_mutation_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'pilot-20261027T000000Z'
            folder.mkdir()
            import hashlib
            values = {
                'launch.json': {'trial_id': folder.name, 'phase': 'pilot', 'seconds': 1800, 'units': self.native.owned(folder.name)},
                'approval.json': {},
                'result.json': {'trial_id': folder.name, 'continuous': True, 'errors': [], 'window_seconds': 1800, 'elapsed_seconds': 1800, 'run_id': 'fresh', 'samples': 180, 'ingestion': {'count': 1}},
                'restart-verification.json': {'errors': [], 'continuous_run_id': 'fresh', 'first_run_id': 'restart'},
            }
            for name in ('cleanup.json', 'post-audit.json', 'deadline.json', 'deadline-stop.json'):
                values[name] = {'errors': []}
            for label in ('before', 'after', 'deadline'):
                (folder / (label + '.sqlite')).write_bytes(b'synthetic snapshot')
                values[label + '-db.json'] = {'integrity': 'ok', 'sha256': hashlib.sha256(b'synthetic snapshot').hexdigest(), 'trading': {}}
            sample = dict(session='s', boot='b', sequence=1, wall=100, mono=50, boottime=60, interrupted=0)
            values['host.json'] = sample
            (folder / 'host.json.jsonl').write_text(json.dumps(sample) + '\n')
            (folder / 'samples.jsonl').write_text((json.dumps({'run_id': 'fresh'}) + '\n') * 180)
            (folder / 'journal.log').write_text('synthetic journal\n')
            for name, value in values.items():
                (folder / name).write_text(json.dumps(value))
            files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
            approval = {'pilot_trial_id': folder.name, 'pilot_evidence_sha256': hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
            self.native.verify_pilot(approval, root)
            for old_seconds in (180, 1100):
                with self.subTest(old_seconds=old_seconds):
                    values['launch.json']['seconds'] = old_seconds
                    (folder / 'launch.json').write_text(json.dumps(values['launch.json']))
                    files['launch.json'] = hashlib.sha256((folder / 'launch.json').read_bytes()).hexdigest()
                    approval['pilot_evidence_sha256'] = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                    with self.assertRaisesRegex(ValueError, 'pilot trial/run metadata mismatch'):
                        self.native.verify_pilot(approval, root)
            values['launch.json']['seconds'] = 1800
            (folder / 'launch.json').write_text(json.dumps(values['launch.json']))
            files['launch.json'] = hashlib.sha256((folder / 'launch.json').read_bytes()).hexdigest()
            (folder / 'samples.jsonl').write_text((json.dumps({'run_id': 'fresh'}) + '\n') * 179)
            files['samples.jsonl'] = hashlib.sha256((folder / 'samples.jsonl').read_bytes()).hexdigest()
            values['result.json']['samples'] = 179
            (folder / 'result.json').write_text(json.dumps(values['result.json']))
            files['result.json'] = hashlib.sha256((folder / 'result.json').read_bytes()).hexdigest()
            approval['pilot_evidence_sha256'] = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            with self.assertRaisesRegex(ValueError, 'pilot samples incomplete'):
                self.native.verify_pilot(approval, root)
            (folder / 'cleanup.json').write_text('{"errors":["stop failed"]}')
            with self.assertRaisesRegex(ValueError, 'pilot'):
                self.native.verify_pilot(approval, root)

    def test_observer_lifecycle_keeps_approval_before_each_start(self):
        from unittest.mock import patch
        import io
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            data = {'trial_id': 'pilot-20261027T000000Z', 'phase': 'pilot', 'seconds': 0,
                    'launch_at': {'utc': '2026-10-27T00:00:00Z'}}
            source = folder / 'db.sqlite'
            source.touch()
            (folder / 'host.json.jsonl').write_text('{}\n')
            group = folder / 'cgroup'
            group.mkdir()
            (group / 'memory.events').write_text('oom 0\noom_kill 0\nmax 0\n')
            state = dict(ActiveState='active', MainPID='1', InvocationID='first', NRestarts='0', ControlGroup=str(group))
            states = [dict(state, ActiveState='inactive'), state, dict(state, MainPID='2', InvocationID='second'), dict(state, MainPID='2', InvocationID='second')]
            snapshot_data = {'trading': {}, 'observations': 0, 'run_ids': []}
            actions = []
            def approve(*args):
                actions.append('approval')
                return 'hash', {}
            def cmd(*args, **kwargs):
                if args[:2] == ('systemctl', 'start'):
                    actions.append('start')
                return ''
            class DB:
                def execute(self, *args):
                    return self
                def fetchall(self):
                    return [('ok',)]
                def fetchone(self):
                    return ('continuous', int(__import__('time').time() * 1e6), 'RUNNING')
                def close(self):
                    pass
            with patch.object(self.native, 'trial_metadata', return_value=data), patch.object(self.native, 'check_approval', side_effect=approve), patch.object(self.native, 'require_service_policy'), patch.object(self.native, 'environment', return_value={'MVP_DB_PATH': str(source), 'MVP_BOT_ID': 'bot', 'MVP_STATE_DIR': str(folder)}), patch.object(self.native, 'snapshot', return_value=snapshot_data), patch.object(self.native, 'properties', side_effect=states), patch.object(self.native, 'wait_run', side_effect=['restart', 'continuous']), patch.object(self.native, 'stop_units', return_value=([], [])), patch.object(self.native, 'command', side_effect=cmd), patch.object(self.native, 'connect', return_value=DB()), patch.object(self.native, 'fresh_run', return_value=True), patch.object(self.native, 'read_host', return_value={}), patch.object(self.native, 'memory_events', return_value={'oom': '0', 'oom_kill': '0', 'max': '0'}), patch.object(self.native, 'ingestion', return_value={'count': 1, 'first_received_us': 1, 'last_received_us': 1}):
                self.assertEqual(self.native.observe(folder), 0, (folder / 'result.json').read_text())
            self.assertEqual(actions[:5], ['approval', 'approval', 'start', 'approval', 'start'])
            result = json.loads((folder / 'result.json').read_text())
            self.assertEqual(result['run_id'], 'continuous')
            self.assertEqual(result['verdict'], 'PENDING_REVIEW')
            self.assertTrue((folder / 'before-db.json').exists())
            self.assertTrue((folder / 'after-db.json').exists())

    def test_observe_rejects_or_clears_stop_sentinel(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            data = {'trial_id': 'pilot-20261027T000000Z', 'phase': 'pilot', 'seconds': 0,
                    'launch_at': {'utc': '2026-10-27T00:00:00Z'}}
            stop = Path(directory) / 'state' / 'STOP'
            stop.parent.mkdir()
            stop.write_text('')
            db = folder / 'db.sqlite'
            from polymarket_bot.linux_mvp.store import MvpStore
            store = MvpStore.open(db)
            store.close()
            with patch.object(self.native, 'trial_metadata', return_value=data), \
                 patch.object(self.native, 'check_approval'), \
                 patch.object(self.native, 'environment', return_value={'MVP_DB_PATH': str(db), 'MVP_BOT_ID': 'bot', 'MVP_STATE_DIR': str(stop.parent)}), \
                 patch.object(self.native, 'properties', return_value={'ActiveState': 'inactive', 'MainPID': '0', 'ControlPID': '0', 'ControlGroup': ''}), \
                 patch.object(self.native, 'snapshot', return_value={'trading': {}, 'observations': 0, 'sha256': '', 'integrity': 'ok'}), \
                 patch.object(self.native, 'require_service_policy'), \
                 patch.object(self.native, 'command', return_value=''), \
                 patch.object(self.native, 'wait_run', return_value='run'), \
                 patch.object(self.native, 'stop_units', return_value=([], [])):
                # Without reset, STOP blocks observe and records error in result.json
                result = self.native.observe(folder, reset_stop=False)
                self.assertEqual(result, 1)
                self.assertTrue(stop.exists())
                result_json = json.loads((folder / 'result.json').read_text())
                self.assertIn('STOP present', str(result_json['errors']))
                # With reset, STOP is cleared before observation proceeds
                self.native.observe(folder, reset_stop=True)
            self.assertFalse(stop.exists())

    def test_cleanup_audits_main_helpers_and_both_timers(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            data = {'trial_id': 'pilot-20261027T000000Z', 'fingerprint': 'hash',
                    'units': self.native.owned('pilot-20261027T000000Z')}
            (folder / 'deadline.json').write_text('{"errors": []}')
            stopped = []
            def stop(units):
                stopped.extend(units)
                return [], [{'unit': u, 'properties': {'ActiveState': 'inactive', 'MainPID': '0', 'ControlPID': '0'}, 'pids': []} for u in units]
            with patch.object(self.native, 'trial_metadata', return_value=data), patch.object(self.native, 'stop_units', side_effect=stop), patch.object(self.native, 'runtime_identity', return_value=('hash', {})):
                self.assertEqual(self.native.audit(folder), 0)
            self.assertEqual(stopped, [self.native.UNIT, *data['units'][:-1]])
            self.assertEqual(json.loads((folder / 'cleanup.json').read_text())['errors'], [])

    def test_deadline_persists_stop_proof_before_bounded_snapshot(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            data = {'trial_id': 'pilot-20261027T000000Z', 'units': self.native.owned('pilot-20261027T000000Z')}
            def backup(*args, **kwargs):
                self.assertTrue((folder / 'deadline-stop.json').exists())
                raise subprocess.TimeoutExpired('snapshot', 60)
            with patch.object(self.native, 'trial_metadata', return_value=data), patch.object(self.native, 'stop_units', return_value=([], [])), patch.object(self.native.subprocess, 'run', side_effect=backup):
                self.assertEqual(self.native.deadline(folder), 1)
            self.assertTrue((folder / 'deadline-stop.json').exists())
            self.assertIn('deadline backup', json.loads((folder / 'deadline.json').read_text())['errors'][0])

    def test_deadline_records_failure_even_if_final_query_succeeds(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            data = {'trial_id': 'pilot-20261027T000000Z', 'units': self.native.owned('pilot-20261027T000000Z')}
            with patch.object(self.native, 'trial_metadata', return_value=data), patch.object(self.native, 'stop_units', return_value=(['stop failed'], [])), patch.object(self.native, 'environment', return_value={'MVP_DB_PATH': '/unused'}), patch.object(self.native.subprocess, 'run', return_value=None):
                self.assertEqual(self.native.deadline(folder), 1)
            self.assertEqual(json.loads((folder / 'deadline.json').read_text())['errors'], ['stop failed'])

    def test_additional_effective_environment_file_rejected(self):
        self.native.require_environment_files('/etc/polymarket-btc/infrastructure.env (ignore_errors=no)')
        with self.assertRaisesRegex(ValueError, 'environment'):
            self.native.require_environment_files('/etc/polymarket-btc/infrastructure.env (ignore_errors=no) /tmp/extra.env (ignore_errors=no)')

    def test_owned_inventory_contains_audit_service(self):
        self.assertIn('infra-pilot-20261027T000000Z-audit.service', self.native.owned('pilot-20261027T000000Z'))

    def test_config_database_parent_is_service_owned_state(self):
        env = dict(line.split('=', 1) for line in (ROOT / 'deploy/polymarket-btc-infrastructure.env.example').read_text().splitlines() if line and not line.startswith('#'))
        self.assertEqual(Path(env['MVP_DB_PATH']).parent, Path('/var/lib/polymarket-btc-infrastructure/data'))

    def test_readiness_rejects_missing_ingest_command_and_wrong_limits(self):
        from unittest.mock import patch
        state = {'ExecStart': 'run', 'Restart': 'no', 'KillMode': 'control-group', 'MemoryMax': '536870912',
                 'CPUQuotaPerSecUSec': '500ms', 'TasksMax': '64', 'User': 'polymarket-btc'}
        with patch.object(self.native, 'command', side_effect=['\n'.join(k + '=' + v for k, v in state.items()), '/etc/polymarket-btc/infrastructure.env (ignore_errors=no)']):
            with self.assertRaisesRegex(ValueError, 'ingest'):
                self.native.require_service_policy()

    def test_old_restart_heartbeat_cannot_become_continuous_run(self):
        with tempfile.TemporaryDirectory() as directory:
            from polymarket_bot.linux_mvp.store import MvpStore
            source = Path(directory) / 'db.sqlite'
            store = MvpStore.open(source)
            self.addCleanup(store.close)
            from decimal import Decimal
            store.start_run('restart', 'bot', initial_cash=Decimal('100'), config={}, started_us=100)
            store.start_run('continuous', 'bot', initial_cash=Decimal('100'), config={}, started_us=200)
            self.assertFalse(self.native.fresh_run(source, 'bot', 'restart', 150))
            self.assertTrue(self.native.fresh_run(source, 'bot', 'continuous', 150))

    def test_runtime_hash_not_recomputed_on_every_sample(self):
        # Startup/final hashes remain exact; periodic full checks bounded to once/minute.
        self.assertFalse(self.native.hash_due(59, 0))
        self.assertTrue(self.native.hash_due(60, 0))

    def test_runtime_start_fingerprint_stable_despite_exec_pid_fields(self):
        self.assertEqual(self.native.stable_effective_unit('ExecStart={ path=/python ; argv[]=python -m ingest ; start_time=[Mon] ; stop_time=[n/a] ; pid=42 ; code=(null) ; status=0/0 }\n'),
                         'ExecStart={ path=/python ; argv[]=python -m ingest }\n')
        self.assertEqual(self.native.stable_effective_unit('ExecStart={ path=/python ; argv[]=python -m ingest ; start_time=[Tue] ; stop_time=[n/a] ; pid=99 ; code=(null) ; status=0/0 }\n'),
                         'ExecStart={ path=/python ; argv[]=python -m ingest }\n')

    def test_restart_wait_rejects_old_or_terminal_heartbeat(self):
        from unittest.mock import patch
        with patch.object(self.native, 'read_db_heartbeat', return_value=('old', 1, 'RUNNING')), patch.object(self.native, 'fresh_run', return_value=False), patch.object(self.native.time, 'monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(TimeoutError, 'heartbeat'):
                self.native.wait_run(Path('/unused'), 'bot', 100)
        with patch.object(self.native, 'read_db_heartbeat', return_value=('fresh', 200, 'RUNNING')), patch.object(self.native, 'fresh_run', return_value=True), patch.object(self.native.time, 'monotonic', side_effect=[0, 1]):
            self.assertEqual(self.native.wait_run(Path('/unused'), 'bot', 100), 'fresh')

    def test_systemd_runtime_bound_reads_numeric_dbus_value(self):
        from unittest.mock import patch
        with patch.object(self.native, 'command', return_value='t 300000000\n'):
            self.assertEqual(self.native.runtime_bound(), 300000000)

    def test_large_snapshot_hash_does_not_read_entire_file(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            from polymarket_bot.linux_mvp.store import MvpStore
            source = Path(directory) / 'source.sqlite'
            MvpStore.open(source).close()
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('unbounded read')):
                self.assertEqual(self.native.snapshot(source, Path(directory) / 'safe.sqlite')['integrity'], 'ok')

    def test_soak_rejects_missing_pilot_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'pilot'):
                self.native.verify_pilot({'pilot_trial_id': 'pilot-20261027T000000Z', 'pilot_evidence_sha256': '0' * 64}, Path(directory))

    def test_stale_ingestion_fails_even_with_fresh_db_heartbeat(self):
        with self.assertRaisesRegex(ValueError, 'ingestion'):
            self.native.require_ingestion_progress({'count': 1, 'last_received_us': 1000000}, 100, 99)
        self.native.require_ingestion_progress({'count': 1, 'last_received_us': 99000000}, 100, 99)

    def test_runner_requires_approval_before_creating_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / 'evidence'
            env = dict(__import__('os').environ, EVIDENCE_DIR=str(evidence))
            result = subprocess.run(['bash', str(ROOT / 'deploy/infrastructure-runner.sh')],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('approval', result.stderr.lower())
            self.assertFalse(evidence.exists())

    def test_installer_refuses_dirty_source_before_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'deploy').mkdir()
            installer = root / 'deploy/install-infrastructure.sh'
            installer.write_bytes((ROOT / 'deploy/install-infrastructure.sh').read_bytes())
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base'], check=True)
            installer.write_text(installer.read_text() + '\n# unapproved\n')
            env = dict(__import__('os').environ, APP_DIR=str(root / 'installed'))
            result = subprocess.run(['bash', str(installer)], env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('dirty source', result.stderr)
            self.assertFalse((root / 'installed').exists())

    def test_utc_wib_rollover_has_correct_offsets(self):
        self.assertEqual(self.native.timestamps(1793037599),
                         {'utc': '2026-10-26T17:59:59Z', 'wib': '2026-10-27T00:59:59+07:00'})

    def test_approval_mismatch_rejected_without_mutation(self):
        approval = dict(scope='INFRASTRUCTURE_ONLY', phase='pilot', seconds=1800,
                        fingerprint='wrong', authorized=True, approved_by='owner',
                        approved_at_utc='2026-10-27T00:00:00Z', git_sha='a' * 40)
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            self.native.validate_approval(approval, 'right', 'pilot', 1800, 'a' * 40)
        approval['fingerprint'] = 'right'
        self.native.validate_approval(approval, 'right', 'pilot', 1800, 'a' * 40)
        for old_seconds in (180, 1100):
            with self.subTest(old_seconds=old_seconds):
                approval['seconds'] = old_seconds
                with self.assertRaisesRegex(ValueError, 'explicit phase/code approval required'):
                    self.native.validate_approval(approval, 'right', 'pilot', 1800, 'a' * 40)
        approval.update(phase='soak', seconds=86400)
        with self.assertRaisesRegex(ValueError, 'pilot'):
            self.native.validate_approval(approval, 'right', 'soak', 86400, 'a' * 40)

    def test_fingerprint_binds_helpers_config_and_effective_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'deploy').mkdir()
            helper = root / 'deploy/heartbeat.sh'
            helper.write_text('approved\n')
            manifest = {'git_sha': 'a' * 40, 'files': {'deploy/heartbeat.sh': __import__('hashlib').sha256(helper.read_bytes()).hexdigest()}}
            self.native.verify_artifacts(root, manifest)
            first = self.native.fingerprint(root, manifest, b'MODE=PAPER_ONLY', 'ExecStart=ingest', 'RuntimeMaxUSec=300000000')
            self.assertNotEqual(first, self.native.fingerprint(root, manifest, b'MODE=PAPER_ONLY\n', 'ExecStart=ingest', 'RuntimeMaxUSec=300000000'))
            self.assertNotEqual(first, self.native.fingerprint(root, manifest, b'MODE=PAPER_ONLY', 'ExecStart=run', 'RuntimeMaxUSec=300000000'))
            self.assertNotEqual(first, self.native.fingerprint(root, manifest, b'MODE=PAPER_ONLY', 'ExecStart=ingest', 'RuntimeMaxUSec=86520000000'))
            helper.write_text('unreviewed\n')
            with self.assertRaisesRegex(ValueError, 'artifact'):
                self.native.verify_artifacts(root, manifest)

    def test_unmanifested_bytecode_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'src/__pycache__'
            cache.mkdir(parents=True)
            (cache / 'rogue.cpython-312.pyc').write_bytes(b'unapproved')
            with self.assertRaisesRegex(ValueError, 'untracked'):
                self.native.verify_artifacts(root, {'files': {}})

    def test_missing_before_snapshot_cannot_be_inferred(self):
        # Snapshot on nonexistent read-only source must not initialize source DB.
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'missing.sqlite'
            with self.assertRaises(sqlite3.OperationalError):
                self.native.snapshot(source, Path(directory) / 'before.sqlite')
            self.assertFalse(source.exists())

    def test_extra_python_artifact_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'src').mkdir()
            (root / 'src/extra.py').write_text('print("unapproved")')
            with self.assertRaisesRegex(ValueError, 'untracked'):
                self.native.verify_artifacts(root, {'git_sha': 'a' * 40, 'files': {}})

    def test_safe_wal_snapshot_counts_only_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            from polymarket_bot.linux_mvp.store import MvpStore
            path = Path(directory) / 'shared.sqlite'
            store = MvpStore.open(path)
            self.addCleanup(store.close)
            db = store._conn
            db.execute("INSERT INTO observations VALUES('bot','old','m',0,'UP','t',NULL,NULL,NULL,1,2)")
            before = self.native.snapshot(path, Path(directory) / 'before.sqlite')
            db.execute("INSERT INTO observations VALUES('bot','fresh','m',1,'UP','t',NULL,NULL,NULL,3,4)")
            after = self.native.snapshot(path, Path(directory) / 'after.sqlite')
            counts = self.native.ingestion(path, 'bot', 'fresh')
            self.assertEqual(counts, {'count': 1, 'first_received_us': 4, 'last_received_us': 4})
            self.assertEqual(before['observations'], 1)
            self.assertEqual(after['observations'], 2)
            self.assertEqual(before['trading'], after['trading'])
            with self.assertRaises(FileExistsError):
                self.native.snapshot(path, Path(directory) / 'before.sqlite')
            with closing(sqlite3.connect(Path(directory) / 'before.sqlite')) as saved:
                self.assertEqual(saved.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)

    def test_active_exited_without_process_is_not_running(self):
        self.assertTrue(self.native.quiescent({'ActiveState': 'active', 'SubState': 'exited', 'MainPID': '0', 'ControlPID': '0'}, []))
        self.assertFalse(self.native.quiescent({'ActiveState': 'active', 'SubState': 'running', 'MainPID': '42', 'ControlPID': '0'}, [42]))
        self.assertFalse(self.native.quiescent({'ActiveState': 'inactive', 'SubState': 'dead', 'MainPID': '0', 'ControlPID': '0'}, [43]))
        self.assertFalse(self.native.quiescent({'ActiveState': 'deactivating', 'SubState': 'stop', 'MainPID': '0', 'ControlPID': '44'}, []))

    def test_native_continuity_rejects_suspend_and_sequence_gaps(self):
        previous = dict(session='s', boot='b', sequence=1, wall=100, mono=50, boottime=60, interrupted=0)
        current = dict(previous, sequence=2, wall=105, mono=55, boottime=65)
        self.assertEqual(self.native.host_error(previous, current, 105), '')
        for change in [dict(sequence=4), dict(boottime=80), dict(boot='other'), dict(wall=99), dict(interrupted=1)]:
            with self.subTest(change=change):
                self.assertTrue(self.native.host_error(previous, dict(current, **change), 105))


if __name__ == '__main__':
    unittest.main()
