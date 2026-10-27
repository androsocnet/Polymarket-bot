"""Opt-in local user-manager check; never addresses application units or VPS.

Run on local Linux: python tests/check_infrastructure_systemd.py
"""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=15).stdout


def check():
    prefix = 'remediation-check-' + uuid.uuid4().hex
    heartbeat = prefix + '-heartbeat.service'
    observer = prefix + '-observer.service'
    deadline = prefix + '-deadline'
    try:
        for unit in (heartbeat, observer):
            run('systemd-run', '--user', '--unit=' + unit, '--property=RemainAfterExit=no',
                '--property=KillMode=control-group', '/bin/sleep', '60')
        # Simulate observer crash. Independent deadline must still stop heartbeat.
        run('systemctl', '--user', 'kill', '--signal=SIGKILL', observer)
        run('systemd-run', '--user', '--unit=' + deadline, '--on-active=1s',
            '--timer-property=AccuracySec=100ms', '--timer-property=RemainAfterElapse=no',
            '--property=RemainAfterExit=no', '/usr/bin/systemctl', '--user', 'stop', heartbeat, observer)
        until = time.monotonic() + 10
        while time.monotonic() < until:
            raw = run('systemctl', '--user', 'show', heartbeat, '--property=ActiveState,MainPID,ControlPID')
            if 'ActiveState=inactive' in raw and 'MainPID=0' in raw and 'ControlPID=0' in raw:
                break
            time.sleep(.1)
        else:
            raise AssertionError('independent deadline did not stop heartbeat after observer failure')
        for unit in (deadline + '.timer', deadline + '.service'):
            state = run('systemctl', '--user', 'show', unit, '--property=LoadState')
            if 'LoadState=not-found' not in state:
                run('systemctl', '--user', 'stop', unit)
        for unit in (heartbeat, observer, deadline + '.service', deadline + '.timer'):
            raw = run('systemctl', '--user', 'show', unit, '--property=ActiveState,MainPID,ControlPID,ControlGroup')
            props = dict(line.split('=', 1) for line in raw.splitlines())
            assert props['ActiveState'] in ('inactive', 'failed'), (unit, raw)
            if not unit.endswith('.timer'):
                assert props['MainPID'] == props['ControlPID'] == '0', (unit, raw)
                if props.get('ControlGroup'):
                    group = Path('/sys/fs/cgroup') / props['ControlGroup'].lstrip('/')
                    assert not any(p.read_text().strip() for p in group.rglob('cgroup.procs')), unit
        spec = importlib.util.spec_from_file_location('trial', Path(__file__).resolve().parents[1] / 'deploy/infrastructure-trial.py')
        trial = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(trial)
        trial.command = lambda *args, **kwargs: run(args[0], '--user', *args[1:])
        errors, evidence = trial.stop_units([heartbeat, observer, deadline + '.timer', deadline + '.service'])
        assert not errors, errors
        assert all(trial.quiescent(item['properties'], item['pids']) for item in evidence)
        print('PASS: local systemd detached deadline, observer failure, production cleanup, PID/cgroup/timer evidence')
    finally:
        subprocess.run(['systemctl', '--user', 'stop', heartbeat, observer, deadline + '.timer', deadline + '.service'], capture_output=True, timeout=15)
        subprocess.run(['systemctl', '--user', 'reset-failed', heartbeat, observer, deadline + '.service'], capture_output=True, timeout=15)


if __name__ == '__main__':
    check()
