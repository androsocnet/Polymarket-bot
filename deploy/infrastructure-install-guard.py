#!/usr/bin/python3
"""Read-only, fail-closed infrastructure installer admission. Never cleans units."""
import json
from pathlib import Path
import re
import subprocess

# Deliberately exact: adding historical exceptions requires another reviewed change.
HISTORICAL_AUDITS = frozenset({'infra-pilot-20261031T022625Z-audit.service'})
PATTERNS = ('infra-*', 'polymarket-btc-infrastructure*')
FIELDS = ('Id', 'LoadState', 'ActiveState', 'SubState', 'Job', 'ControlGroup', 'MainPID',
          'ControlPID', 'Result', 'ExecMainCode', 'ExecMainStatus', 'RemainAfterExit', 'Slice')


def systemctl(*args):
    return subprocess.run(['systemctl', *args], check=True, capture_output=True,
                          text=True, timeout=15).stdout


def require(condition, message):
    if not condition:
        raise ValueError(message)


def relevant(name):
    return name.startswith(('infra-', 'polymarket-btc-infrastructure'))


def empty_tree(path):
    """A missing expected cgroup is empty; existing trees require readable kernel evidence."""
    try:
        path.stat()
    except FileNotFoundError:
        require(not path.is_symlink(), 'cgroup symlink')
        return
    require(path.is_dir() and not path.is_symlink(), 'unsafe cgroup directory')
    require(not (path / 'cgroup.procs').read_text().strip(), 'residual cgroup processes')
    events = dict(line.split() for line in (path / 'cgroup.events').read_text().splitlines())
    require(events.get('populated') == '0', 'populated or unknown cgroup')
    for child in path.iterdir():
        require(not child.is_symlink(), 'cgroup symlink')
        if child.is_dir():
            empty_tree(child)


def snapshot(cgroup_root):
    # systemd v255 list-jobs uses text even when --output=json is requested.
    jobs = systemctl('list-jobs', '--no-legend', '--plain', '--full', '--no-pager')
    for row in jobs.splitlines():
        fields = row.split()
        require(len(fields) == 4, 'invalid job row')
        job_id, unit, job_type, status = fields
        require(re.fullmatch(r'[1-9][0-9]*', job_id) and '.' in unit and
                status in ('waiting', 'running'), 'invalid job evidence')
        require(not relevant(unit), 'infrastructure job pending')
    units = json.loads(systemctl('list-units', '--all', '--output=json', '--no-pager', *PATTERNS))
    require(isinstance(units, list), 'invalid unit inventory')
    states = {}
    job_columns = [isinstance(item, dict) and 'job' in item for item in units]
    require(not any(job_columns) or all(job_columns), 'inconsistent inventory job column')
    for item in units:
        require(isinstance(item, dict) and isinstance(item.get('unit'), str), 'missing unit name')
        # v255 hides job column for entire table when no listed unit has a job.
        require(item.get('job', '') == '', 'unit inventory job pending/invalid')
        require(all(isinstance(item.get(key), str) and item[key] for key in ('load', 'active', 'sub')),
                'missing unit inventory state')
        name = item['unit']
        require(relevant(name) and name not in states, 'unexpected/duplicate unit')
        raw = systemctl('show', name, '--property=' + ','.join(FIELDS))
        pairs = [line.split('=', 1) for line in raw.splitlines()]
        require(all(len(pair) == 2 for pair in pairs), 'invalid properties')
        state = dict(pairs)
        require(len(pairs) == len(state), 'duplicate property')
        required = ('Id', 'LoadState', 'ActiveState', 'SubState', 'Job')
        require(all(key in state for key in required), 'missing unit properties')
        require(state['Id'] == name and state['LoadState'] == 'loaded' and state['Job'] == '',
                'unit identity/load/job mismatch')
        require(all(item[key] == state[prop] for key, prop in
                    (('load', 'LoadState'), ('active', 'ActiveState'), ('sub', 'SubState'))),
                'unit inventory/show mismatch')
        active, sub = state['ActiveState'], state['SubState']
        historical = name in HISTORICAL_AUDITS and re.fullmatch(r'infra-(pilot|soak)-\d{8}T\d{6}Z-audit\.service', name)
        if active == 'active':
            require(historical and sub == 'exited', 'infrastructure unit still active: ' + name)
            expected = dict(Result='success', ExecMainCode='1', ExecMainStatus='0', RemainAfterExit='yes')
            require(all(state.get(k) == v for k, v in expected.items()), 'audit not successful terminal')
        else:
            require((active, sub) in (('inactive', 'dead'), ('failed', 'failed')), 'unexpected unit state')
        if name.endswith('.service'):
            require(all(key in state for key in FIELDS), 'missing service properties')
            require(state['MainPID'] == state['ControlPID'] == '0', 'service processes remain')
            require(state['Slice'] == 'system.slice', 'unexpected service slice')
            expected_group = '/system.slice/' + name
            require(state['ControlGroup'] in ('', expected_group), 'unexpected cgroup identity')
            # Check expected directory even when systemd reports empty ControlGroup.
            group_path = cgroup_root / expected_group.lstrip('/')
            if state['ControlGroup']:
                require(group_path.is_dir(), 'reported cgroup unavailable')
            empty_tree(group_path)
        else:
            require(name.endswith('.timer') and active != 'active', 'unknown unit type')
        states[name] = state
    return states


def admit(cgroup_root=Path('/sys/fs/cgroup')):
    require((cgroup_root / 'cgroup.controllers').is_file(), 'unified cgroup evidence unavailable')
    require(snapshot(cgroup_root) == snapshot(cgroup_root), 'systemd admission changed during inspection')


if __name__ == '__main__':
    try:
        admit()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit('installer admission BLOCKED: ' + str(exc))
