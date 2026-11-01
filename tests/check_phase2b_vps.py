#!/usr/bin/python3
"""Disposable-only frozen-controller compatibility probe. Never production acceptance."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys
import time
import uuid

CONTROLLER = '69079bbce0c4a02ed1a7f81f3c5128b430841aab'
SOURCE_SHA256 = '43a75dad91210a47ec92972fe7c2881d5f0b37f79cf0edcaccfcf3560c6d6799'
TRIAL = 'pilot-20261101T000000Z'
PYTHON = '/usr/bin/python3'
SYSTEM = Path('/run/systemd/system')
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}


def run(*args, timeout=20, check=True):
    return subprocess.run(args, env=ENV, check=check, capture_output=True, text=True, timeout=timeout)


def base(token):
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('invalid disposable token')
    return 'p2b-vps-' + token


def root_path(token):
    return Path('/run') / base(token)


def helpers(token):
    b = base(token)
    return [b + '-' + name for name in ('heartbeat.service', 'observer.service', 'deadline.timer',
        'deadline.service', 'audit.timer', 'audit.service', 'startup.timer', 'startup.service')]


def unit_names(token):
    b = base(token)
    return [b + '@.service', b + '@' + TRIAL + '-main-' + token[:8] + '.service',
            *[b + '-' + name for name in ('replacement.service', 'canary.service', 'worker.service',
                'cleanup.timer', 'cleanup.service')], *helpers(token)]


def allowed_unit(token, name):
    if name not in unit_names(token):
        raise ValueError('unit outside exact disposable allowlist: ' + name)
    return name


def ctl(token, action, unit, *options, check=True):
    return run('/usr/bin/systemctl', action, allowed_unit(token, unit), *options, check=check).stdout.strip()


def unit_absent(token, name):
    # systemctl show returns useful not-found data even with status 1. Empty output is not proof.
    if name.endswith('@.service'):
        return not (SYSTEM / allowed_unit(token, name)).exists()
    state = ctl(token, 'show', name, '--property=LoadState', '--value', check=False)
    if state not in ('not-found', 'loaded', 'masked', 'error', 'bad-setting', 'merged', 'stub'):
        raise ValueError('LoadState unavailable: ' + name)
    return state == 'not-found'


def git_bytes(checkout):
    # Empty environment blocks GIT_DIR, config injection, replace-object and protocol helpers.
    env = dict(ENV, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_NO_REPLACE_OBJECTS='1',
               GIT_ALLOW_PROTOCOL='', GIT_TERMINAL_PROMPT='0')
    return subprocess.run(['/usr/bin/git', '--no-replace-objects', '-C', str(checkout), 'show',
        CONTROLLER + ':deploy/infrastructure-trial.py'], env=env, check=True,
        capture_output=True, timeout=20).stdout


def frozen_source(checkout):
    if not checkout.is_absolute():
        raise ValueError('explicit absolute checkout required')
    source = git_bytes(checkout)
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise ValueError('frozen controller SHA256 mismatch')
    return source


def platform_check(release, version, wsl, development):
    if development:
        if not wsl or release.get('ID') != 'ubuntu' or int(version) < 255:
            raise ValueError('development mode requires Ubuntu WSL systemd >=255')
    elif wsl or release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '24.04' or version != '255':
        raise ValueError('target requires non-WSL Ubuntu 24.04 systemd 255')


def definitions(token):
    b, root = base(token), root_path(token)
    call = f'{PYTHON} -I -B {root}/harness.py --internal'
    bounded = ('[Service]\nType=simple\nRestart=no\nKillMode=control-group\nTimeoutStopSec=5\n'
               'RuntimeMaxSec=90\nPrivateNetwork=yes\nNoNewPrivileges=yes\n')
    payload = ('[Service]\nType=simple\nRestart=no\nKillMode=control-group\nTimeoutStopSec=5\n'
        'RuntimeMaxSec=2040\nRuntimeRandomizedExtraSec=0\nUser=nobody\nGroup=nogroup\n'
        'NoNewPrivileges=yes\nCapabilityBoundingSet=\nPrivateNetwork=yes\nPrivateTmp=yes\n'
        'ProtectSystem=strict\nProtectHome=yes\nProtectControlGroups=yes\n'
        'ProtectKernelTunables=yes\nRestrictSUIDSGID=yes\nLockPersonality=yes\n'
        'RestrictAddressFamilies=AF_UNIX\nMemoryMax=128M\nTasksMax=16\n'
        f'ReadWritePaths={root}\nExecStartPre=!{call} register {token} %n\n'
        'ExecStart=/bin/sh -c "sleep 300 & wait"\n')
    return {b + '@.service': payload,
        b + '-replacement.service': bounded + 'User=nobody\nExecStart=/bin/sleep 300\n',
        b + '-canary.service': bounded + 'User=nobody\nExecStart=/bin/sleep 300\n',
        b + '-worker.service': bounded.replace('RuntimeMaxSec=90', 'RuntimeMaxSec=130') +
            f'ExecStart=/usr/bin/timeout --signal=TERM --kill-after=10 120 {call} worker {token}\n',
        b + '-cleanup.service': '[Service]\nType=oneshot\nTimeoutStartSec=90\n' +
            f'ExecStart={call} cleanup {token}\n',
        b + '-cleanup.timer': f'[Timer]\nOnBootSec={time.monotonic() + 150:.6f}\nAccuracySec=1\nRemainAfterElapse=no\n' + f'Unit={b}-cleanup.service\n',
        b + '-startup.service': '[Service]\nType=oneshot\nTimeoutStartSec=50\n' +
            f'ExecStart={call} startup {token}\n',
        b + '-startup.timer': '[Timer]\nOnActiveSec=3\nAccuracySec=1\n' + f'Unit={b}-startup.service\n',
        b + '-observer.service': bounded + f'ExecStart={call} observer {token}\n'}


def load_controller(token):
    root = root_path(token)
    source = root / 'controller.py'
    if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError('staged controller SHA256 mismatch')
    spec = importlib.util.spec_from_file_location('frozen_controller', source)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.TEMPLATE_BASE, m.TEMPLATE_UNIT = base(token), base(token) + '@.service'
    m.EVIDENCE, m.STATE = root / 'evidence', root
    m.owned = lambda trial: helpers(token) if trial == TRIAL else (_ for _ in ()).throw(ValueError('trial mismatch'))
    real_command = m.command
    def command(*args, **kwargs):
        if args[0] == 'systemctl' and args[1] in ('show', 'cat', 'stop'):
            allowed_unit(token, args[2])
        elif args[0] == 'busctl' and args[1:4] == ('get-property', 'org.freedesktop.systemd1',
                '/org/freedesktop/systemd1/unit/' + unit_names(token)[1].replace('@', '_40').replace('-', '_2d').replace('.', '_2e')):
            pass
        else:
            raise ValueError('controller command outside disposable allowlist')
        return real_command(*args, **kwargs)
    m.command = command
    def policy(unit):
        allowed_unit(token, unit)
        hook = ctl(token, 'show', unit, '--property=ExecStartPreEx', '--value')
        expected = f'{PYTHON} -I -B {root}/harness.py --internal register {token} {unit}'
        if hook.count('{ path=') != 1 or 'argv[]=' + expected + ' ;' not in hook or 'flags=no-setuid ;' not in hook:
            raise ValueError('disposable root hook policy mismatch')
    # ponytail: inert policy only; production ingest/user/environment admission remains unit-test evidence.
    m.require_service_policy = policy
    return m


def wait_for(predicate, seconds=15):
    end = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= end:
            raise TimeoutError('disposable state deadline')
        time.sleep(.1)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def worker(token):
    m, root, names = load_controller(token), root_path(token), unit_names(token)
    main, replacement, canary = names[1:4]
    folder = root / 'evidence' / TRIAL
    folder.mkdir(parents=True, mode=0o700)
    data = dict(trial_id=TRIAL, boot_id=m.boot_id(), seconds=1800, fingerprint='disposable', units=helpers(token))
    m.write_json(folder / 'launch.json', data)
    m.prepare_invocation(folder, data, 'main', main)
    for name in (replacement, canary, base(token) + '-observer.service'):
        ctl(token, 'start', name)
    wait_for(lambda: m.properties(main).get('ActiveState') == 'active')
    original = m.registered_identity(folder, data, 'main')
    state = m.properties(main)
    require(original['invocation_id'] == state['InvocationID'] and
            original['invocation_cgroup'] == state['ControlGroup'], 'persisted exact invocation differs')
    require(m.runtime_bound(unit=main) == 2040000000 and
            m.runtime_bound('RuntimeRandomizedExtraUSec', unit=main) == 0, 'actual runtime differs')
    identity_path = folder / 'main-identity.json'
    saved = identity_path.read_bytes()
    for bad in (None, b'{broken'):
        if bad is None:
            identity_path.unlink()
        else:
            identity_path.write_bytes(bad)
        errors, _ = m.cancel_invocations(folder, data)
        require(bool(errors) and m.properties(main)['ActiveState'] == 'active', 'bad identity did not fail closed')
        identity_path.write_bytes(saved)
    wait_for(lambda: len(m.cgroup_pids(original['invocation_cgroup'])) >= 2)
    pids = m.cgroup_pids(original['invocation_cgroup'])
    descriptors = [os.pidfd_open(pid) for pid in pids]
    try:
        nested = m.CGROUP_ROOT / original['invocation_cgroup'].lstrip('/') / 'probe-child'
        nested.mkdir()
        child = next(pid for pid in pids if str(pid) != state['MainPID'])
        (nested / 'cgroup.procs').write_text(str(child))
        ctl(token, 'start', base(token) + '-startup.timer')
        ctl(token, 'kill', base(token) + '-observer.service', '--kill-whom=all', '--signal=KILL')
        wait_for(lambda: (folder / 'startup-stop.json').exists(), 50)
        proof = json.loads((folder / 'startup-stop.json').read_text())
        require(not (folder / 'timing.json').exists(), 'unexpected timing record')
        require(not m.cgroup_pids(original['invocation_cgroup']), 'original descendants remain')
        for descriptor in descriptors:
            poll = select.poll()
            poll.register(descriptor, select.POLLIN)
            require(bool(poll.poll(1000)), 'original process still alive')
        terminal = m.properties(main)
        require(terminal['ActiveState'] in ('inactive', 'failed') and terminal['MainPID'] == '0' and
                terminal['ControlPID'] == '0', 'original unit not terminal')
        expected_errors = ['startup watchdog expired']
        conservative = 'main registered cleanup: invocation identity changed; refuse signaling'
        require(proof['errors'] in (expected_errors, expected_errors + [conservative]),
                'startup cleanup errors: ' + repr(proof))
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    require(run('/usr/bin/systemctl', 'start', main, check=False).returncode != 0, 'cancelled same-name restart admitted')
    # Simulate legitimate new same-name invocation only inside this disposable evidence root.
    identity_path.rename(folder / 'original-identity.json')
    (folder / 'cancelled.json').unlink()
    ctl(token, 'reset-failed', main)
    ctl(token, 'start', main)
    current = m.registered_identity(folder, data, 'main')
    require(current['invocation_id'] != original['invocation_id'], 'replacement identity reused')
    try:
        m.terminate_invocation(original)
    except ValueError as exc:
        require(str(exc) == 'invocation identity changed; refuse signaling', 'stale shutdown wrong error: ' + str(exc))
    else:
        raise ValueError('stale shutdown not refused')
    for name in (main, replacement, canary):
        require(m.properties(name)['ActiveState'] == 'active', 'replacement/canary did not survive')
    report = dict(controller_commit=CONTROLLER, controller_sha256=SOURCE_SHA256,
        startup_errors=proof['errors'], root_registration='PASS', durable_identity='PASS', actual_runtime='PASS',
        missing_corrupt_identity='PASS', observer_death_before_timing='PASS',
        main_nested_termination='PASS', same_name_restart_refusal='PASS', stale_shutdown='PASS',
        replacement_canary_survival='PASS')
    (root / 'result.json').write_text(json.dumps(report))


def cleanup(token):
    root, b = root_path(token), base(token)
    names = unit_names(token)
    # Stop worker first; no concurrent unit creation can race final removal.
    ordered = [b + '-worker.service', b + '-observer.service', b + '-startup.timer', b + '-startup.service']
    ordered += [n for n in names if n not in ordered and n not in (b + '-cleanup.service', b + '-cleanup.timer', b + '@.service')]
    for name in ordered:
        if unit_absent(token, name):
            continue
        ctl(token, 'stop', name)
        state = ctl(token, 'show', name, '--property=MainPID,ControlPID,ActiveState,ControlGroup')
        props = dict(line.split('=', 1) for line in state.splitlines())
        require(props.get('ActiveState') in ('inactive', 'failed') and props.get('MainPID', '0') == '0'
                and props.get('ControlPID', '0') == '0' and not props.get('ControlGroup'), 'cleanup not quiescent: ' + name)
        ctl(token, 'reset-failed', name, check=False)
    ctl(token, 'stop', b + '-cleanup.timer')
    ctl(token, 'reset-failed', b + '-cleanup.timer', check=False)
    for name in definitions(token):
        (SYSTEM / name).unlink(missing_ok=True)
    run('/usr/bin/systemctl', 'daemon-reload')
    shutil.rmtree(root)


def internal(action, token, unit):
    root = root_path(token)
    require(os.geteuid() == 0 and Path(__file__).resolve() == root / 'harness.py' and
            root.stat().st_uid == 0 and root.stat().st_mode & 0o077 == 0, 'untrusted internal staging')
    if action == 'cleanup':
        cleanup(token)
    elif action == 'worker':
        worker(token)
    elif action == 'observer':
        ctl(token, 'start', unit_names(token)[1])
        time.sleep(300)
    elif action == 'startup':
        load_controller(token).startup_timeout(root / 'evidence' / TRIAL)
    elif action == 'register':
        load_controller(token).register_invocation(allowed_unit(token, unit))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-only', action='store_true')
    parser.add_argument('--checkout', type=Path)
    parser.add_argument('--wsl-development', action='store_true')
    parser.add_argument('--internal', choices=('worker', 'observer', 'register', 'startup', 'cleanup'))
    parser.add_argument('token', nargs='?')
    parser.add_argument('unit', nargs='?')
    args = parser.parse_args()
    if args.internal:
        internal(args.internal, args.token, args.unit)
        return
    require(args.disposable_only and args.checkout is not None, '--disposable-only and explicit --checkout required')
    require(os.geteuid() == 0 and sys.executable == PYTHON, 'root /usr/bin/python3 required')
    source = frozen_source(args.checkout)
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().replace('"', '').splitlines() if '=' in line)
    version = run('/usr/bin/systemctl', '--version').stdout.split()[1]
    platform_check(release, version, 'microsoft' in Path('/proc/sys/kernel/osrelease').read_text().lower(), args.wsl_development)
    require(Path('/run/systemd/system').is_dir() and Path('/sys/fs/cgroup/cgroup.controllers').is_file(), 'systemd/cgroup v2 required')
    require(hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'), 'pidfd APIs required')
    token = uuid.uuid4().hex
    root, b = root_path(token), base(token)
    names = unit_names(token)
    for name in names:
        require(unit_absent(token, name) and
                not (SYSTEM / name).exists(), 'unit collision: ' + name)
    root.mkdir(mode=0o700)
    armed = False
    result = None
    def interrupted(signum, frame):
        raise InterruptedError('interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        (root / 'controller.py').write_bytes(source)
        (root / 'harness.py').write_bytes(Path(__file__).read_bytes())
        cleanup_defs = {name: text for name, text in definitions(token).items()
                        if name in (b + '-cleanup.service', b + '-cleanup.timer')}
        for name, text in cleanup_defs.items():
            with (SYSTEM / name).open('x') as stream:
                stream.write(text)
        run('/usr/bin/systemctl', 'daemon-reload')
        ctl(token, 'start', b + '-cleanup.timer')
        require(ctl(token, 'is-active', b + '-cleanup.timer') == 'active', 'independent cleanup not armed')
        armed = True
        for name, text in definitions(token).items():
            if name not in cleanup_defs:
                with (SYSTEM / name).open('x') as stream:
                    stream.write(text)
        run('/usr/bin/systemctl', 'daemon-reload')
        ctl(token, 'start', b + '-worker.service')
        wait_for(lambda: (root / 'result.json').exists() or
                 ctl(token, 'show', b + '-worker.service', '--property=ActiveState', '--value') in ('failed', 'inactive'), 135)
        require((root / 'result.json').exists(), 'worker failed: ' + run('/usr/bin/journalctl', '-u', b + '-worker.service', '-n', '30', '--no-pager').stdout)
        result = json.loads((root / 'result.json').read_text())
    finally:
        # Service is independent of launcher signals; timer remains armed on cleanup failure.
        if armed:
            ctl(token, 'start', b + '-cleanup.service')
        else:
            cleanup(token)
    wait_for(lambda: all(unit_absent(token, name) for name in names))
    require(not root.exists() and all(not (SYSTEM / name).exists() for name in names), 'disposable files remain')
    result.update(cleanup='PASS', environment='WSL_DEVELOPMENT_NOT_VPS_ACCEPTANCE' if args.wsl_development else 'Ubuntu24.04-systemd255')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('BLOCKED: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
