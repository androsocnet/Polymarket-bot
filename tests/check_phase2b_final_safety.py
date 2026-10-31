#!/usr/bin/env python3
"""Disposable WSL-only integration of production registration/startup/shutdown code.

No network, DB, approvals, installed infrastructure units, or production paths.
Run: sudo python3 tests/check_phase2b_final_safety.py --disposable-only --wsl-only
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=45).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-only', action='store_true', required=True)
    parser.add_argument('--wsl-only', action='store_true', required=True)
    parser.parse_args()
    if os.geteuid() != 0 or 'microsoft' not in Path('/proc/sys/kernel/osrelease').read_text().lower():
        raise SystemExit('root disposable WSL only')
    token = uuid.uuid4().hex[:8]
    base = 'p2b-final-' + token
    root = Path('/run') / base
    root.mkdir(mode=0o700)
    script = root / 'controller.py'
    source = Path(__file__).resolve().parents[1] / 'deploy/infrastructure-trial.py'
    shutil.copyfile(source, script)
    spec = importlib.util.spec_from_file_location('probe_controller', script)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.TEMPLATE_BASE = base
    m.TEMPLATE_UNIT = base + '@.service'
    m.EVIDENCE = root / 'evidence'
    m.STATE = root
    m.EVIDENCE.mkdir(mode=0o700)
    trial = 'pilot-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    folder = m.EVIDENCE / trial
    folder.mkdir(mode=0o700)
    data = {'trial_id': trial, 'boot_id': m.boot_id(), 'seconds': 1800, 'fingerprint': 'disposable',
            'units': m.owned(trial)}
    m.write_json(folder / 'launch.json', data)
    # Small bootstrap redirects only disposable namespace/policy; production registration,
    # locking, pidfds, runtime check, and cleanup execute unchanged.
    bootstrap = root / 'bootstrap.py'
    bootstrap.write_text('import importlib.util, pathlib, sys\n'
        f's=importlib.util.spec_from_file_location("controller", {str(script)!r})\n'
        'm=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n'
        f'm.TEMPLATE_BASE={base!r}; m.TEMPLATE_UNIT={m.TEMPLATE_UNIT!r}\n'
        f'm.EVIDENCE=pathlib.Path({str(m.EVIDENCE)!r})\n'
        'm.require_service_policy=lambda unit: None\n'
        'raise SystemExit(m.register_invocation(sys.argv[1]))\n')
    fragment = Path('/run/systemd/system') / m.TEMPLATE_UNIT
    fragment.write_text('[Service]\nType=simple\nRestart=no\nRuntimeMaxSec=2040\n'
        'RuntimeRandomizedExtraSec=0\nKillMode=control-group\nTimeoutStopSec=5\n'
        'User=nobody\nGroup=nogroup\nNoNewPrivileges=true\nCapabilityBoundingSet=\n'
        'PrivateTmp=true\nProtectSystem=strict\nProtectHome=true\nProtectControlGroups=true\n'
        'ProtectKernelTunables=true\nRestrictSUIDSGID=true\nLockPersonality=true\n'
        'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n'
        f'ReadWritePaths={root}\n'
        f'ExecStartPre=!{sys.executable} {bootstrap} %n\n'
        'ExecStart=/bin/sh -c "sleep 300 & wait"\n')
    units = [m.invocation_unit(trial, 'main'), base + '-replacement.service', base + '-canary.service']
    try:
        run('systemctl', 'daemon-reload')
        m.require_service_policy = lambda unit: None
        m.prepare_invocation(folder, data, 'main', units[0])
        for unit in units[1:]:
            run('systemd-run', '--unit=' + unit, '--property=RuntimeMaxSec=90', '/bin/sleep', '300')
        # Disposable observer submits start then waits for evidence; SIGKILL skips finally.
        observer = subprocess.Popen([sys.executable, '-c',
            'import subprocess,time,sys; subprocess.run(["systemctl","start",sys.argv[1]],check=True); time.sleep(300)',
            units[0]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        until = time.monotonic() + 15
        while m.properties(units[0]).get('ActiveState') != 'active':
            if time.monotonic() >= until:
                raise RuntimeError(run('journalctl', '-u', units[0], '--no-pager', '-n', '30'))
            time.sleep(.05)
        observer.kill() if observer.poll() is None else None
        observer.wait()
        assert not (folder / 'timing.json').exists()
        original = m.registered_identity(folder, data, 'main')
        assert m.runtime_bound(unit=units[0]) == 2040000000
        until = time.monotonic() + 5
        while len(m.cgroup_pids(original['invocation_cgroup'])) < 2 and time.monotonic() < until:
            time.sleep(.05)
        assert len(m.cgroup_pids(original['invocation_cgroup'])) >= 2
        m.startup_timeout(folder)
        until = time.monotonic() + 5
        while m.properties(units[0]).get('ActiveState') not in ('inactive', 'failed') and time.monotonic() < until:
            time.sleep(.1)
        assert not m.cgroup_pids(original['invocation_cgroup'])
        for unit in units[1:]:
            assert m.properties(unit)['ActiveState'] == 'active', unit
        # Same-name restart cannot pass exclusive registration/cancellation.
        restarted = subprocess.run(['systemctl', 'start', units[0]], capture_output=True)
        assert restarted.returncode != 0
        assert m.properties(units[0])['ActiveState'] == 'failed'
        try:
            m.terminate_invocation(original)
        except ValueError:
            pass
        else:
            raise AssertionError('stale identity not rejected')
        for unit in units[1:]:
            assert m.properties(unit)['ActiveState'] == 'active', unit
        print(json.dumps({'startup_without_timing': 'PASS', 'descendants_terminated': 'PASS',
                          'runtime_actual_instance': 'PASS', 'same_name_restart_rejected': 'PASS',
                          'replacement_canary_survive': 'PASS'}))
    finally:
        for unit in units:
            subprocess.run(['systemctl', 'stop', unit], capture_output=True)
            subprocess.run(['systemctl', 'reset-failed', unit], capture_output=True)
            assert not m.cgroup_pids(m.properties(unit).get('ControlGroup', ''))
            assert m.properties(unit)['ActiveState'] == 'inactive'
        fragment.unlink()
        run('systemctl', 'daemon-reload')
        shutil.rmtree(root)
    print('disposable cleanup: PASS; no active services remain')


if __name__ == '__main__':
    main()
