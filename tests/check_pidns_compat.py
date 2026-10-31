#!/usr/bin/env python3
"""Disposable systemd-255/unshare compatibility probe. NOT a watchdog.

Root coordinator only stages code and manages random disposable units. Namespace
operations run as existing nonroot service user. No production unit is addressed.
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


def emit(**record):
    print(json.dumps(record, sort_keys=True), flush=True)


def run(*args, timeout=8):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
    return result.stdout.strip()


def status():
    data = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
    return {key: data[key].strip() for key in ('Uid', 'Gid', 'CapEff', 'CapPrm', 'CapBnd', 'NoNewPrivs', 'Seccomp', 'NSpid')}


def label():
    try:
        return Path('/proc/self/attr/current').read_text().strip()
    except OSError as exc:
        return repr(exc)


def ready_line(process, seconds=5):
    # Unbuffered pipe; avoid TextIO buffering swallowing later readiness bytes.
    deadline = time.monotonic() + seconds
    data = bytearray()
    while time.monotonic() < deadline and len(data) < 16384:
        if select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
            part = os.read(process.stdout.fileno(), 1)
            if not part:
                break
            data += part
            if part == b'\n':
                return json.loads(data)
    raise RuntimeError('namespace readiness missing: ' + data.decode(errors='replace'))


def exited(fd, seconds=5):
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(int(seconds * 1000)))


def init(procfd, mapped):
    assert os.getpid() == 1 and Path('/proc/self').resolve().name == '1'
    identity = dict(pid=1, host_pid=int(os.readlink('self', dir_fd=procfd)),
                    pidns=os.readlink('/proc/self/ns/pid'), userns=os.readlink('/proc/self/ns/user'),
                    status=status(), apparmor=label(), uid_map=Path('/proc/self/uid_map').read_text(),
                    gid_map=Path('/proc/self/gid_map').read_text())
    assert identity['status']['NoNewPrivs'] == '1'
    if mapped:
        assert os.getuid() == 0
    # Intermediate exits; grandchild waits for reparenting to namespace init.
    reader, writer = os.pipe()
    intermediate = os.fork()
    if intermediate == 0:
        os.close(reader)
        if os.fork() == 0:
            end = time.monotonic() + 2
            while os.getppid() != 1 and time.monotonic() < end:
                time.sleep(.01)
            os.write(writer, str(os.getppid()).encode())
            os._exit(19)
        os._exit(17)
    os.close(writer)
    assert os.read(reader, 20) == b'1', 'orphan not adopted by PID 1'
    os.close(reader)
    codes = sorted(os.waitstatus_to_exitcode(os.waitpid(-1, 0)[1]) for _ in range(2))
    assert codes == [17, 19], codes
    try:
        os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        pass
    else:
        raise AssertionError('unexpected unreaped child')
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(reader)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(23))
        os.write(writer, str(os.readlink('self', dir_fd=procfd)).encode())
        os.close(writer)
        time.sleep(30)
        os._exit(99)
    os.close(writer)
    child_host = int(os.read(reader, 30))
    os.close(reader)
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    emit(ready=True, identity=identity, child_host=child_host, orphan_reaped=True)
    end = time.monotonic() + 20
    while not stopping and time.monotonic() < end:
        time.sleep(.01)
    assert stopping, 'init signal deadline'
    os.kill(child, signal.SIGTERM)
    assert os.waitstatus_to_exitcode(os.waitpid(child, 0)[1]) == 23
    emit(forwarded=True)


def namespace_command(procfd, mapped):
    flags = ['--user', '--map-root-user'] if mapped else []
    return ['/usr/bin/unshare', *flags, '--pid', '--fork', '--mount-proc',
            '/usr/bin/python3', '-I', '-B', str(Path(__file__).resolve()),
            '--init', str(procfd), str(int(mapped))]


def worker():
    if os.getuid() == 0:
        raise RuntimeError('namespace monitor must not run as host root')
    if sys.flags.optimize:
        raise RuntimeError('optimized Python disables probe assertions')
    baseline = status()
    emit(worker=True, status=baseline, apparmor=label(), pidns=os.readlink('/proc/self/ns/pid'),
         userns=os.readlink('/proc/self/ns/user'), uid=os.getuid(), gid=os.getgid(), groups=os.getgroups(),
         unshare=run('/usr/bin/unshare', '--version'))
    assert baseline['NoNewPrivs'] == '1'
    assert all(int(baseline[key], 16) == 0 for key in ('CapEff', 'CapPrm', 'CapBnd'))
    procfd = os.open('/proc', os.O_RDONLY | os.O_DIRECTORY)
    processes, descriptors = [], []
    def launch(mapped):
        process = subprocess.Popen(namespace_command(procfd, mapped), pass_fds=(procfd,),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, bufsize=0,
                                   env={**os.environ, 'LC_ALL': 'C'})
        processes.append(process)
        try:
            record = ready_line(process)
        except Exception:
            # Reap/record denied launch before considering another route. Never retry root.
            out, err = process.communicate(timeout=3)
            emit(namespace_denied=True, mapped=mapped, returncode=process.returncode,
                 stdout=out.decode(errors='replace'), stderr=err.decode(errors='replace'))
            if process.returncode == 1 and b'Operation not permitted' in err:
                return None
            raise
        assert record.get('ready') and record['orphan_reaped']
        identity = record['identity']
        assert identity['pidns'] != os.readlink('/proc/self/ns/pid')
        for key in ('host_pid',):
            assert os.readlink('/proc/' + str(identity[key]) + '/ns/pid') == identity['pidns']
        if mapped:
            assert [int(x) for x in identity['uid_map'].split()] == [0, os.getuid(), 1]
            assert [int(x) for x in identity['gid_map'].split()] == [0, os.getgid(), 1]
        initfd = os.pidfd_open(identity['host_pid'])
        childfd = os.pidfd_open(record['child_host'])
        wrapperfd = os.pidfd_open(process.pid)
        descriptors.extend((initfd, childfd, wrapperfd))
        emit(namespace_ready=record)
        return process, record, initfd, childfd, wrapperfd
    try:
        # Plain attempt records real failure under existing capabilities. Mapped
        # route is explicit, never profile bypass or host-root namespace creation.
        first = launch(False)
        mapped = first is None
        if mapped:
            first = launch(True)
        if first is None:
            emit(verdict='BLOCKED', reason='unprivileged PID/user namespace permission denied')
            return 2
        process, record, initfd, childfd, wrapperfd = first
        # Characterize actual util-linux parent behavior, not assumed forwarding.
        signal.pidfd_send_signal(wrapperfd, signal.SIGTERM)
        wrapper_forwarded = exited(initfd, .4)
        if not wrapper_forwarded:
            signal.pidfd_send_signal(initfd, signal.SIGTERM)
        assert exited(initfd) and exited(childfd), 'graceful descendant cleanup failed'
        out, err = process.communicate(timeout=3)
        assert process.returncode == 0 and b'"forwarded": true' in out, (out, err)
        emit(graceful_signal_forwarded_by_init=True, util_linux_parent_forwarded=wrapper_forwarded)
        original = launch(mapped)
        replacement = launch(mapped)
        assert original is not None and replacement is not None
        assert original[1]['identity']['pidns'] != replacement[1]['identity']['pidns']
        signal.pidfd_send_signal(original[2], signal.SIGKILL)
        assert exited(original[2]) and exited(original[3]), 'init exit left live descendant'
        assert not exited(replacement[2], .1) and not exited(replacement[3], .1)
        try:
            signal.pidfd_send_signal(original[2], signal.SIGKILL)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError('dead original pidfd unexpectedly signaled')
        assert not exited(replacement[2], .1)
        signal.pidfd_send_signal(replacement[2], signal.SIGTERM)
        assert exited(replacement[2]) and exited(replacement[3])
        replacement[0].communicate(timeout=3)
        original[0].communicate(timeout=3)
        emit(verdict='PASS', namespace_teardown=True, sibling_replacement_survived=True,
             same_service_cgroup=True, pid_reuse_tested=False)
        return 0
    finally:
        # Namespace cleanup only through already-pinned init descriptors. Root
        # coordinator verifies cgroup cleanup separately; it cannot manufacture PASS.
        for fd in descriptors:
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                emit(worker_cleanup_error=repr(exc))
            os.close(fd)
        for process in processes:
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                emit(worker_cleanup_pending=process.pid)
        os.close(procfd)


UNIT_RE = re.compile(r'p2b-pidns-[0-9a-f]{32}-(?:probe|canary)\.service\Z')
PROPS = ('LoadState,ActiveState,SubState,Result,MainPID,ControlPID,ControlGroup,ExecMainStatus,'
         'User,Group,NoNewPrivileges,CapabilityBoundingSet,RestrictNamespaces,KillMode,Restart,'
         'ProtectSystem,ProtectHome,ProtectControlGroups,PrivateTmp,RestrictSUIDSGID,LockPersonality,'
         'RestrictAddressFamilies,RuntimeMaxUSec,TimeoutStopUSec')


def properties(unit):
    return dict(line.split('=', 1) for line in run('systemctl', 'show', unit, '--property=' + PROPS).splitlines())


def cgroup_empty(path):
    try:
        values = dict(line.split() for line in (path / 'cgroup.events').read_text().splitlines())
        return values['populated'] == '0'
    except FileNotFoundError:
        # Removed directory proves no remaining tasks; missing events in existing
        # directory is missing evidence, never an empty-cgroup proof.
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


def cleanup(units, groups=None):
    if not all(UNIT_RE.fullmatch(unit) for unit in units):
        raise ValueError('refuse non-disposable unit')
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


def coordinator(wsl=False):
    if os.geteuid() != 0:
        raise ValueError('root required ONLY for staging and disposable system-service management')
    if wsl:
        if 'microsoft' not in os.uname().release.lower():
            raise RuntimeError('WSL only on Microsoft kernel')
        user = os.environ.get('SUDO_USER', '')
        if not user or pwd.getpwnam(user).pw_uid == 0:
            raise RuntimeError('WSL requires explicit SUDO_USER nonroot account')
    else:
        user = 'polymarket-btc'
    account = pwd.getpwnam(user)
    assert account.pw_uid != 0
    token = uuid.uuid4().hex
    units = ['p2b-pidns-' + token + '-' + role + '.service' for role in ('probe', 'canary')]
    stage = Path('/run/p2b-pidns-' + token)
    stage.mkdir(mode=0o755)
    os.chmod(stage, 0o755)
    script = stage / 'probe.py'
    script.write_bytes(Path(__file__).read_bytes())
    script.chmod(0o644)
    emit(fixtures=units, evidence=str(stage), user=user, uid=account.pw_uid,
         kernel=os.uname().release, systemd=run('systemctl', '--version').splitlines()[0],
         wsl_only=wsl)
    code = 1
    logs = {}
    groups = {}
    def interrupted(signum, _frame):
        raise RuntimeError('coordinator interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    base = ['Type=exec', 'RemainAfterExit=yes', 'User=' + user, 'Group=' + str(account.pw_gid),
            'Restart=no', 'KillMode=control-group', 'TimeoutStopSec=3', 'RuntimeMaxSec=60',
            'NoNewPrivileges=yes', 'CapabilityBoundingSet=', 'RestrictNamespaces=no',
            'ProtectSystem=strict', 'ProtectHome=yes', 'ProtectControlGroups=yes',
            'ProtectKernelTunables=yes', 'PrivateTmp=yes', 'RestrictSUIDSGID=yes',
            'LockPersonality=yes', 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6',
            'MemoryMax=128M', 'TasksMax=32', 'CPUQuota=50%', 'UMask=0077']
    # Source service allows AF_UNIX; keep actual source policy (no profile change).
    try:
        for index in (1, 0):
            unit = units[index]
            log = stage / ('worker.log' if index == 0 else 'canary.log')
            logs[index] = log
            options = [*base, 'StandardOutput=append:' + str(log), 'StandardError=append:' + str(log)]
            argv = ['/usr/bin/python3', '-I', '-B', str(script), '--worker'] if index == 0 else ['/usr/bin/sleep', '60']
            run('systemd-run', '--quiet', '--unit=' + unit,
                *('--property=' + prop for prop in options), '--', *argv)
        canary = properties(units[1])
        assert canary['ActiveState'] == 'active'
        end = time.monotonic() + 45
        while time.monotonic() < end:
            state = properties(units[0])
            if state['ActiveState'] in ('inactive', 'failed') or state['SubState'] == 'exited':
                break
            time.sleep(.1)
        else:
            raise TimeoutError('worker runtime exceeded 45 seconds')
        for unit in units:
            effective = properties(unit)
            groups[unit] = effective.get('ControlGroup')
            emit(unit=unit, properties=effective)
            assert effective['User'] == user
            assert effective['NoNewPrivileges'] == 'yes' and effective['CapabilityBoundingSet'] == ''
            assert effective['KillMode'] == 'control-group' and effective['Restart'] == 'no'
            assert effective['ProtectSystem'] == 'strict' and effective['ProtectHome'] == 'yes'
            assert effective['ProtectControlGroups'] == 'yes' and effective['RestrictNamespaces'] == 'no'
        assert empty(units[0], groups.get(units[0])), 'worker residual process before coordinator cleanup'
        worker_log = logs[0].read_text()
        print(worker_log, end='', flush=True)
        assert properties(units[1])['MainPID'] == canary['MainPID']
        assert properties(units[1])['ActiveState'] == 'active', 'unrelated service affected'
        if state['ExecMainStatus'] == '2' and '"verdict": "BLOCKED"' in worker_log:
            code = 2
        else:
            assert state['ExecMainStatus'] == '0' and '"verdict": "PASS"' in worker_log
            code = 0
    finally:
        errors = cleanup(units, groups)
        final_states = {}
        for unit in units:
            try:
                final_states[unit] = properties(unit)
            except Exception as exc:
                errors.append(unit + ': final state unavailable ' + repr(exc))
        emit(cleanup_errors=errors, final_states=final_states)
        if errors:
            code = 1
        emit(verdict='PASS' if code == 0 else 'BLOCKED' if code == 2 else 'FAIL',
             vps_compatibility='UNVERIFIED' if wsl else 'candidate only; Hermes must review evidence',
             evidence=str(stage), unrelated_service_untouched=True if code in (0, 2) else None)
    return code


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--init':
        init(int(sys.argv[2]), bool(int(sys.argv[3])))
    elif sys.argv[1:] == ['--worker']:
        raise SystemExit(worker())
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--disposable-only', action='store_true', required=True)
        parser.add_argument('--wsl-only', action='store_true')
        args = parser.parse_args()
        try:
            raise SystemExit(coordinator(args.wsl_only))
        except Exception as exc:
            emit(verdict='FAIL', error=repr(exc))
            raise SystemExit(1)
