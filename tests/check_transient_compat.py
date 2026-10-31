#!/usr/bin/env python3
"""Disposable systemd-255 per-invocation transient service compatibility probe.

Verifies:
1. Replacement-race isolation: stopping original transient unit cannot hit replacement unit.
2. Descendant cleanup: KillMode=control-group kills main and forked descendants in original unit.
3. Dedicated cgroup v2 allocation per unique unit name.
4. PID reuse immunity: signals scoped strictly to target unit cgroup.
5. Stale stop safety: subsequent stop calls against dead original do not touch replacement.
6. Fail-closed identity verification.
7. Unrelated canary service isolation.
"""
import argparse
import json
import os
from pathlib import Path
import pwd
import re
import select
import signal
import subprocess
import sys
import time
import uuid

UNIT_RE = re.compile(r'p2b-transient-[0-9a-f]{32}-(?:orig|repl|canary)\.service\Z')
PROPS = ('LoadState,ActiveState,SubState,Result,MainPID,ControlPID,ControlGroup,ExecMainStatus,'
         'User,Group,NoNewPrivileges,CapabilityBoundingSet,RestrictNamespaces,KillMode,Restart,'
         'ProtectSystem,ProtectHome,ProtectControlGroups,PrivateTmp,RestrictSUIDSGID,LockPersonality,'
         'RestrictAddressFamilies,RuntimeMaxUSec,TimeoutStopUSec')


def emit(**record):
    print(json.dumps(record, sort_keys=True), flush=True)


def run(*args, timeout=10):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
    return result.stdout.strip()


def properties(unit):
    return dict(line.split('=', 1) for line in run('systemctl', 'show', unit, '--property=' + PROPS).splitlines())


def cgroup_empty(path):
    try:
        events = path / 'cgroup.events'
        values = dict(line.split() for line in events.read_text().splitlines())
        return values.get('populated') == '0'
    except FileNotFoundError:
        # If directory exists but cgroup.events is missing, fail closed.
        # If directory itself is absent, cgroup was completely unlinked and removed.
        path.stat()
        raise


def empty(unit, retained_group=None):
    state = properties(unit)
    for group in {state.get('ControlGroup'), retained_group} - {None, ''}:
        path = Path('/sys/fs/cgroup' + group)
        try:
            if not cgroup_empty(path):
                return False
        except FileNotFoundError:
            if path.exists():
                raise
    return (state.get('MainPID', '0') == state.get('ControlPID', '0') == '0'
            and (state['ActiveState'] in ('inactive', 'failed')
                 or (state['ActiveState'] == 'active' and state['SubState'] == 'exited')))


def emergency_stop(unit, expected_token=None):
    """Emergency shutdown of a specific transient unit. Fails closed on invalid identity."""
    if not UNIT_RE.fullmatch(unit):
        raise ValueError(f'refuse non-disposable or invalid unit name: {unit}')
    if expected_token is not None and expected_token not in unit:
        raise ValueError(f'identity mismatch: token {expected_token} not in unit {unit}')
    try:
        run('systemctl', 'stop', unit)
    except subprocess.CalledProcessError as exc:
        # If unit is already dead/unloaded (code 5 or not-loaded), safe no-op
        if 'not loaded' not in exc.stderr and 'not found' not in exc.stderr:
            raise


def cleanup(units, groups=None):
    if not all(UNIT_RE.fullmatch(unit) for unit in units):
        raise ValueError('refuse non-disposable unit in cleanup')
    groups = groups or {}
    errors = []
    for unit in units:
        for action in ('stop', 'reset-failed'):
            try:
                run('systemctl', action, unit)
            except subprocess.CalledProcessError as exc:
                if 'not loaded' not in exc.stderr and 'not found' not in exc.stderr:
                    errors.append(repr(exc) + ': ' + exc.stderr)
            except Exception as exc:
                errors.append(repr(exc))
        try:
            if not empty(unit, groups.get(unit)):
                errors.append(unit + ': residual process or active state')
        except Exception as exc:
            errors.append(unit + ': cleanup verification ' + repr(exc))
    return errors


def payload(fifo_path, role):
    """Worker payload: spawns main process and child fork, reports readiness."""
    child_pid = os.fork()
    if child_pid == 0:
        # Child process: runs in same cgroup, stays alive
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        time.sleep(120)
        os._exit(0)
    # Main process
    report = {'role': role, 'main_pid': os.getpid(), 'child_pid': child_pid,
              'cgroup': Path('/proc/self/cgroup').read_text().strip()}
    with open(fifo_path, 'w') as fifo:
        json.dump(report, fifo)
        fifo.write('\n')
    time.sleep(120)


def coordinator(wsl=False):
    if os.geteuid() != 0:
        raise ValueError('root required for staging and disposable systemd unit management')
    if wsl:
        if 'microsoft' not in os.uname().release.lower():
            raise RuntimeError('WSL only on Microsoft kernel')
        user = os.environ.get('SUDO_USER', '')
        if not user or pwd.getpwnam(user).pw_uid == 0:
            raise RuntimeError('WSL requires explicit SUDO_USER nonroot account')
    else:
        user = 'polymarket-btc'
    account = pwd.getpwnam(user)
    if account.pw_uid == 0:
        raise RuntimeError('target user must not be host root')

    token = uuid.uuid4().hex
    stage = Path('/run/p2b-transient-' + token)
    stage.mkdir(mode=0o755)
    os.chmod(stage, 0o755)

    script = stage / 'probe.py'
    script.write_bytes(Path(__file__).read_bytes())
    os.chmod(script, 0o644)

    units = {
        'orig': f'p2b-transient-{token}-orig.service',
        'repl': f'p2b-transient-{token}-repl.service',
        'canary': f'p2b-transient-{token}-canary.service'
    }

    emit(stage=str(stage), token=token, units=units, user=user, uid=account.pw_uid,
         kernel=os.uname().release, systemd=run('systemctl', '--version').splitlines()[0],
         wsl_only=wsl)

    base_props = [
        'Type=exec', 'RemainAfterExit=yes', f'User={user}', f'Group={account.pw_gid}',
        'Restart=no', 'KillMode=control-group', 'TimeoutStopSec=3', 'RuntimeMaxSec=60',
        'NoNewPrivileges=yes', 'CapabilityBoundingSet=', 'RestrictNamespaces=no',
        'ProtectSystem=strict', 'ProtectHome=yes', 'ProtectControlGroups=yes',
        'ProtectKernelTunables=yes', 'PrivateTmp=yes', 'RestrictSUIDSGID=yes',
        'LockPersonality=yes', 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6',
        'MemoryMax=128M', 'TasksMax=32', 'CPUQuota=50%', 'UMask=0077'
    ]

    groups = {}
    reports = {}
    code = 1
    unit_list = list(units.values())

    def interrupted(signum, _frame):
        raise RuntimeError(f'coordinator interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)

    try:
        # 1. Launch Canary
        run('systemd-run', '--quiet', f'--unit={units["canary"]}',
            *('--property=' + p for p in base_props), '--', '/usr/bin/sleep', '60')
        canary_init = properties(units['canary'])
        groups[units['canary']] = canary_init.get('ControlGroup')
        assert canary_init['ActiveState'] == 'active'

        # 2. Launch Original Invocation
        fifo_orig = stage / 'orig.fifo'
        os.mkfifo(fifo_orig, 0o666)
        os.chmod(fifo_orig, 0o666)
        run('systemd-run', '--quiet', f'--unit={units["orig"]}',
            *('--property=' + p for p in base_props), '--',
            '/usr/bin/python3', '-I', '-B', str(script), '--payload', str(fifo_orig), 'orig')
        with open(fifo_orig, 'r') as f:
            reports['orig'] = json.loads(f.readline())
        orig_props = properties(units['orig'])
        groups[units['orig']] = orig_props.get('ControlGroup')
        assert orig_props['ActiveState'] == 'active'
        emit(orig_running=reports['orig'], orig_cgroup=groups[units['orig']])

        # 3. Launch Replacement Invocation with distinct unit name
        fifo_repl = stage / 'repl.fifo'
        os.mkfifo(fifo_repl, 0o666)
        os.chmod(fifo_repl, 0o666)
        run('systemd-run', '--quiet', f'--unit={units["repl"]}',
            *('--property=' + p for p in base_props), '--',
            '/usr/bin/python3', '-I', '-B', str(script), '--payload', str(fifo_repl), 'repl')
        with open(fifo_repl, 'r') as f:
            reports['repl'] = json.loads(f.readline())
        repl_props = properties(units['repl'])
        groups[units['repl']] = repl_props.get('ControlGroup')
        assert repl_props['ActiveState'] == 'active'
        emit(repl_running=reports['repl'], repl_cgroup=groups[units['repl']])

        # Verify Cgroup Disjointness
        assert groups[units['orig']] != groups[units['repl']], 'cgroups must be strictly disjoint'
        assert units['orig'] in groups[units['orig']]
        assert units['repl'] in groups[units['repl']]

        # 4. Perform Emergency Stop strictly against Original Unit
        emit(action='emergency_stop_orig', target=units['orig'])
        emergency_stop(units['orig'], expected_token=token)

        # 5. Verify Original Invocation and Descendants are Dead
        time.sleep(0.5)
        orig_after = properties(units['orig'])
        assert empty(units['orig'], groups[units['orig']]), 'original unit not quiescent after stop'
        assert not os.path.exists(f'/proc/{reports["orig"]["main_pid"]}'), 'orig main PID still alive'
        assert not os.path.exists(f'/proc/{reports["orig"]["child_pid"]}'), 'orig descendant PID still alive'
        emit(orig_quiesced=True, orig_descendants_killed=True)

        # 6. Verify Replacement Invocation is completely Unaffected and Alive
        repl_after = properties(units['repl'])
        assert repl_after['ActiveState'] == 'active', 'replacement killed by orig stop'
        assert repl_after['MainPID'] == str(reports['repl']['main_pid'])
        assert os.path.exists(f'/proc/{reports["repl"]["main_pid"]}'), 'replacement main PID missing'
        assert os.path.exists(f'/proc/{reports["repl"]["child_pid"]}'), 'replacement child PID missing'
        emit(replacement_survived=True, replacement_descendants_intact=True)

        # 7. Verify Stale Stop against Dead Original does not touch Replacement
        emit(action='stale_stop_orig', target=units['orig'])
        emergency_stop(units['orig'], expected_token=token)
        repl_after_stale = properties(units['repl'])
        assert repl_after_stale['ActiveState'] == 'active', 'replacement affected by stale stop'
        assert repl_after_stale['MainPID'] == str(reports['repl']['main_pid'])
        emit(stale_stop_safe=True)

        # 8. Verify Canary Unaffected
        canary_after = properties(units['canary'])
        assert canary_after['ActiveState'] == 'active'
        assert canary_after['MainPID'] == canary_init['MainPID']
        emit(canary_survived=True)

        # 9. Stop Replacement Cleanly
        emergency_stop(units['repl'], expected_token=token)
        assert empty(units['repl'], groups[units['repl']])
        emit(replacement_quiesced=True)

        # 10. Fail-Closed Identity Check Verification
        try:
            emergency_stop(units['orig'], expected_token='invalid_token_1234')
        except ValueError as exc:
            emit(fail_closed_verified=True, error=str(exc))
        else:
            raise AssertionError('emergency_stop did not fail closed on mismatched token')

        code = 0
    finally:
        cleanup_errors = cleanup(unit_list, groups)
        emit(cleanup_errors=cleanup_errors)
        if cleanup_errors:
            code = 1
        emit(verdict='PASS' if code == 0 else 'FAIL',
             evidence=str(stage),
             isolated_replacement_proven=(code == 0))

    return code


if __name__ == '__main__':
    if len(sys.argv) >= 4 and sys.argv[1] == '--payload':
        payload(sys.argv[2], sys.argv[3])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--disposable-only', action='store_true', required=True)
        parser.add_argument('--wsl-only', action='store_true')
        args = parser.parse_args()
        try:
            sys.exit(coordinator(args.wsl_only))
        except Exception as exc:
            emit(verdict='FAIL', error=repr(exc))
            sys.exit(1)
