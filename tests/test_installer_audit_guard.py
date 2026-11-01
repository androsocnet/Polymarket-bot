"""Read-only installer admission regressions; never contacts systemd."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = 'infra-pilot-20261031T022625Z-audit.service'


class Admission(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'deploy/infrastructure-install-guard.py'
        self.assertTrue(path.exists(), 'read-only guard missing')
        spec = importlib.util.spec_from_file_location('installer_guard', path)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cg = Path(self.tmp.name)
        (self.cg / 'cgroup.controllers').write_text('cpu memory pids')
        (self.cg / 'system.slice').mkdir()
        self.state = dict(Id=HISTORICAL, LoadState='loaded', ActiveState='active', SubState='exited',
            Result='success', MainPID='0', ControlPID='0', ControlGroup='', Job='',
            ExecMainCode='1', ExecMainStatus='0', RemainAfterExit='yes', Slice='system.slice')
        self.units = [HISTORICAL]
        self.inventory = dict(load='loaded', active='active', sub='exited', description='Retained audit')
        self.jobs = ''
        self.calls = []

    def boundary(self, *args):
        self.calls.append(args)
        if args[0] == 'list-units':
            return __import__('json').dumps([dict(self.inventory, unit=n) for n in self.units])
        if args[0] == 'list-jobs':
            return self.jobs
        if args[0] == 'show':
            state = dict(self.state, Id=args[1])
            return '\n'.join(k + '=' + v for k, v in state.items())
        raise AssertionError('historical mutation attempted: ' + repr(args))

    def check(self):
        with patch.object(self.m, 'systemctl', side_effect=self.boundary):
            self.m.admit(self.cg)

    def test_reported_retained_audit_admitted_without_mutation(self):
        self.check()
        self.assertTrue(self.calls)
        self.assertEqual({args[0] for args in self.calls}, {'list-units', 'list-jobs', 'show'})

    def test_unrelated_text_job_admitted(self):
        self.jobs = '  42 unrelated.service start waiting\n43 other.timer stop running\n'
        self.check()

    def test_malformed_job_rows_block(self):
        for row in ('[]', '42 unrelated.service start', '42 unrelated.service start waiting extra',
                    'bad unrelated.service start waiting', '0 unrelated.service start waiting',
                    '42 unrelated.service start unknown', '\n'):
            self.jobs = row
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.check()

    def test_missing_or_contradictory_inventory_blocks(self):
        baseline = self.inventory.copy()
        cases = [dict(baseline, **{key: value}) for key, value in
                 [('load', 'not-found'), ('active', 'inactive'), ('sub', 'running'),
                  ('job', 'start'), ('job', None), ('active', None)]]
        cases += [{k: v for k, v in baseline.items() if k != key} for key in ('load', 'active', 'sub')]
        for inventory in cases:
            self.inventory = inventory
            with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                self.check()
        self.state.update(ActiveState='inactive', SubState='dead')
        self.inventory = dict(baseline, sub='running', job='start')
        with self.assertRaises(ValueError):
            self.check()  # review counterexample: stable contradictory list/show

    def test_partially_missing_job_column_blocks(self):
        self.state.update(ActiveState='inactive', SubState='dead')
        self.inventory.update(active='inactive', sub='dead')
        original = self.boundary
        rows = [dict(self.inventory, unit=HISTORICAL, job=''),
                dict(self.inventory, unit='infra-other.service')]
        with patch.object(self.m, 'systemctl', side_effect=lambda *a:
                __import__('json').dumps(rows) if a[0] == 'list-units' else original(*a)):
            with self.assertRaises(ValueError):
                self.m.admit(self.cg)

    def test_visible_empty_job_column_admitted(self):
        self.inventory['job'] = ''
        self.check()

    def test_inactive_service_and_timer_admitted(self):
        self.state.update(ActiveState='inactive', SubState='dead')
        self.inventory.update(active='inactive', sub='dead')
        self.units.append('infra-pilot-20261031T022625Z-main.timer')
        self.check()

    def test_active_running_and_transitions_block(self):
        for active, sub in [('active', 'running'), ('activating', 'start'), ('deactivating', 'stop'), ('unknown', 'unknown')]:
            with self.subTest(active=active):
                self.state.update(ActiveState=active, SubState=sub)
                self.inventory.update(active=active, sub=sub)
                with self.assertRaises(ValueError):
                    self.check()

    def test_nested_residual_child_and_populated_group_block(self):
        group = self.cg / 'system.slice' / HISTORICAL
        leaf = group / 'child'
        leaf.mkdir(parents=True)
        for path in (group, leaf):
            (path / 'cgroup.procs').write_text('')
            (path / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
        (leaf / 'cgroup.procs').write_text('1234\n')
        with self.assertRaises(ValueError):
            self.check()
        (leaf / 'cgroup.procs').write_text('')
        (group / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
        with self.assertRaises(ValueError):
            self.check()

    def test_empty_retained_cgroup_admitted(self):
        group = self.cg / 'system.slice' / HISTORICAL
        group.mkdir()
        (group / 'cgroup.procs').write_text('')
        (group / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
        self.state['ControlGroup'] = '/system.slice/' + HISTORICAL
        self.check()

    def test_reported_nonempty_group_must_exist(self):
        self.state['ControlGroup'] = '/system.slice/' + HISTORICAL
        with self.assertRaises(ValueError):
            self.check()

    def test_pending_jobs_and_active_timers_block(self):
        for jobs in ('42 ' + HISTORICAL + ' start waiting\n',
                     '43 infra-soak-20261030T000000Z-main.service stop running\n'):
            self.jobs = jobs
            with self.assertRaises(ValueError):
                self.check()
        self.jobs = ''
        self.state['Job'] = '42'
        with self.assertRaises(ValueError):
            self.check()
        self.state['Job'] = ''
        self.units.append(HISTORICAL.replace('-audit.service', '-audit.timer'))
        with self.assertRaises(ValueError):
            self.check()

    def test_unrecognized_exited_helpers_and_production_units_block(self):
        for name in ('infra-pilot-20261031T022625Z-observer.service',
                     'infra-pilot-20261031T022626Z-audit.service',
                     'infra-pilot-20261399T022625Z-audit.service',
                     'polymarket-btc-infrastructure@pilot.service'):
            self.units = [name]
            with self.assertRaises(ValueError):
                self.check()

    def test_missing_inconsistent_properties_fail_closed(self):
        baseline = self.state.copy()
        for key in baseline:
            with self.subTest(missing=key):
                self.state = baseline.copy()
                del self.state[key]
                # boundary normally substitutes Id; remove from output explicitly.
                with patch.object(self.m, 'systemctl', side_effect=lambda *a: self.boundary(*a).replace('Id=' + HISTORICAL, '') if a[0] == 'show' and key == 'Id' else self.boundary(*a)):
                    with self.assertRaises(ValueError):
                        self.m.admit(self.cg)
        for key, value in [('Result', 'exit-code'), ('MainPID', '1'), ('ControlPID', '2'),
                           ('ExecMainCode', '0'), ('ExecMainStatus', '1'), ('RemainAfterExit', 'no'),
                           ('ControlGroup', '/system.slice/other.service'), ('Slice', 'other.slice')]:
            self.state = dict(baseline, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.check()

    def test_unreadable_systemd_and_cgroup_evidence_block(self):
        with patch.object(self.m, 'systemctl', side_effect=subprocess.CalledProcessError(1, 'systemctl')):
            with self.assertRaises(subprocess.CalledProcessError):
                self.m.admit(self.cg)
        group = self.cg / 'system.slice' / HISTORICAL
        group.mkdir()
        (group / 'cgroup.procs').write_text('')
        with self.assertRaises(OSError):
            self.check()  # cgroup.events absent
        with patch.object(self.m, 'systemctl', return_value='not json'):
            with self.assertRaises(ValueError):
                self.m.admit(self.cg)

    def test_permission_error_cannot_look_like_absent_cgroup(self):
        with patch.object(Path, 'stat', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.m.empty_tree(self.cg / 'system.slice' / HISTORICAL)

    def test_admission_change_and_unreadable_lists_block(self):
        original = self.boundary
        shows = 0
        def changing(*args):
            nonlocal shows
            if args[0] == 'show':
                shows += 1
                if shows > 1:
                    self.state['Job'] = '42'
            return original(*args)
        with patch.object(self.m, 'systemctl', side_effect=changing), self.assertRaises(ValueError):
            self.m.admit(self.cg)

    def test_installer_guard_precedes_every_mutation(self):
        text = (ROOT / 'deploy/install-infrastructure.sh').read_text()
        guard = text.index('infrastructure-install-guard.py')
        for mutation in ('groupadd --system', 'useradd --system', 'install -d', 'controller-owner', 'systemctl daemon-reload'):
            self.assertLess(guard, text.index(mutation))
        self.assertNotIn('systemctl stop', text)
        self.assertNotIn('systemctl reset-failed', text)

    def test_release_guard_rejects_harness_and_changed_controller(self):
        import os
        text = (ROOT / 'deploy/install-infrastructure.sh').read_text()
        fragment = text.split('APPROVED_BASE=', 1)[1].split('# Includes legacy', 1)[0]
        fragment = 'APPROVED_BASE=' + fragment
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            git = folder / 'git'
            git.write_text('#!/bin/sh\ncase "$*" in\n*merge-base*) exit 0;;\n*diff*) exit "${DIFF_STATUS:-0}";;\n*ls-tree*) printf "%s" "${HARNESS:-}"; exit "${TREE_STATUS:-0}";;\nesac\n')
            git.chmod(0o755)
            for diff, harness, tree_status, expected in [('0', '', '0', 0), ('1', '', '0', 1),
                    ('0', '100644 blob fake tests/check_phase2b_vps.py', '0', 1), ('0', '', '1', 1)]:
                env = dict(os.environ, PATH=td + ':' + os.environ['PATH'], DIFF_STATUS=diff, HARNESS=harness, TREE_STATUS=tree_status)
                result = subprocess.run(['bash', '-c', 'SOURCE_DIR=/unused; SHA=unused\n' + fragment],
                                        env=env, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_release_guard_pins_base_and_excludes_harness(self):
        text = (ROOT / 'deploy/install-infrastructure.sh').read_text()
        self.assertIn('69079bbce0c4a02ed1a7f81f3c5128b430841aab', text)
        self.assertIn('check_phase2b_vps.py', text)
        self.assertIn('git -C "${SOURCE_DIR}" diff --quiet', text)


if __name__ == '__main__':
    unittest.main()
