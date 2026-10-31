"""Disposable Linux/systemd safety investigation, NOT an emergency watchdog.

Run as root with --disposable-only. No caller-supplied unit/path/PID accepted.
Only random wd-proof-* transient services created here may be stopped.
"""
import errno
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import uuid


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=15).stdout.strip()


def wait_for(predicate):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('disposable observation timed out')


def properties(unit):
    return dict(line.split('=', 1) for line in run(
        'systemctl', 'show', unit, '-p', 'MainPID', '-p', 'InvocationID',
        '-p', 'ControlGroup').splitlines())


def populated(fd):
    return dict(line.split() for line in os.pread(fd, 4096, 0).decode().splitlines())['populated']


def cleanup(unit, fds):
    errors = []
    def attempt(action):
        try:
            action()
        except subprocess.CalledProcessError as exc:
            if 'not loaded' not in exc.stderr and 'not found' not in exc.stderr:
                errors.append(repr(exc))
        except Exception as exc:
            errors.append(repr(exc))
    if fds[2] is not None:
        try:
            os.write(fds[2], b'1')
        except OSError as exc:
            if exc.errno not in (errno.ENODEV, errno.ENOENT):
                errors.append(repr(exc))
    # Only this random disposable unit is named. Also covers setup failures
    # before killfd acquisition and children surviving KillMode=process.
    attempt(lambda: run('systemctl', 'kill', '--signal=SIGKILL', '--kill-whom=all', unit))
    attempt(lambda: run('systemctl', 'stop', unit))
    attempt(lambda: run('systemctl', 'reset-failed', unit))
    for fd in fds:
        if fd is not None:
            attempt(lambda fd=fd: os.close(fd))
    return errors


def prove(keep_child):
    unit = 'wd-proof-' + uuid.uuid4().hex + '.service'
    print(json.dumps(dict(fixture=unit, keep_child=keep_child)), flush=True)
    killfd = eventsfd = pidfd = None
    # Child remains in original cgroup after main exits. It also forks during
    # shutdown; cgroup.kill must handle descendants, not only original main.
    code = '''import os, time
if os.fork() == 0:
    while True:
        if os.fork() == 0:
            time.sleep(60)
            os._exit(0)
        time.sleep(.05)
else:
    time.sleep(60)
''' if keep_child else 'import time; time.sleep(60)'
    try:
        run('systemd-run', '--unit=' + unit, '--property=Type=exec',
            '--property=Restart=no', '--property=RuntimeMaxSec=60',
            '--property=TasksMax=64', '--property=TimeoutStopSec=1',
            '--property=KillMode=' + ('process' if keep_child else 'control-group'),
            '--', sys.executable, '-c', code)
        first = properties(unit)
        path = Path('/sys/fs/cgroup' + first['ControlGroup'])
        inode = path.stat().st_ino
        killfd = os.open(path / 'cgroup.kill', os.O_WRONLY | os.O_CLOEXEC)
        eventsfd = os.open(path / 'cgroup.events', os.O_RDONLY | os.O_CLOEXEC)
        original_pid = int(first['MainPID'])
        pidfd = os.pidfd_open(original_pid)
        if keep_child:
            wait_for(lambda: len((path / 'cgroup.procs').read_text().split()) >= 3)
        assert populated(eventsfd) == '1'
        run('systemctl', 'restart', unit)
        second = properties(unit)
        assert first['InvocationID'] and second['InvocationID']
        assert first['InvocationID'] != second['InvocationID']
        assert int(second['MainPID']) > 0
        poll = select.poll()
        poll.register(pidfd, select.POLLIN)
        assert poll.poll(1000), 'original main did not exit'
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError('exited original unexpectedly accepted signal')
        assert properties(unit)['MainPID'] == second['MainPID']
        same_object = path.stat().st_ino == inode
        assert same_object == keep_child, 'unexpected systemd cgroup lifecycle'
        record = dict(case='same-live-object' if keep_child else 'removed-recreated',
                      unit=unit, original=first, replacement=second,
                      original_inode=inode, replacement_inode=path.stat().st_ino,
                      original_main_exited=True, pidfd_did_not_hit_replacement=True)
        if keep_child:
            assert populated(eventsfd) == '1', 'surviving descendants not demonstrated'
            replacement_pidfd = os.pidfd_open(int(second['MainPID']))
            try:
                assert os.write(killfd, b'1') == 1
                poll.register(replacement_pidfd, select.POLLIN)
                wait_for(lambda: any(fd == replacement_pidfd for fd, _ in poll.poll(0)))
                def quiescent():
                    try:
                        return populated(eventsfd) == '0'
                    except OSError as exc:
                        if exc.errno != errno.ENODEV:
                            raise
                        # Removal requires no live tasks. This controlled fixture
                        # has no migration out of its cgroup; not a general audit.
                        record['events_removed_after_kill'] = True
                        return True
                wait_for(quiescent)
            finally:
                os.close(replacement_pidfd)
            record.update(retained_fd_killed_replacement=True,
                          complete_original_cgroup_quiescence=True)
        else:
            try:
                os.write(killfd, b'1')
            except OSError as exc:
                assert exc.errno in (errno.ENODEV, errno.ENOENT), exc
                record['retained_fd_errno'] = exc.errno
            else:
                raise AssertionError('removed original cgroup accepted write')
            assert properties(unit)['MainPID'] == second['MainPID']
            record['replacement_untouched'] = True
        print(json.dumps(record, sort_keys=True), flush=True)
    finally:
        failing = sys.exc_info()[0] is not None
        errors = cleanup(unit, (pidfd, eventsfd, killfd))
        if errors:
            print(json.dumps(dict(fixture=unit, cleanup_errors=errors)), file=sys.stderr, flush=True)
            if not failing:
                raise RuntimeError('disposable cleanup incomplete; inspect printed fixture')


if __name__ == '__main__':
    if sys.argv[1:] != ['--disposable-only']:
        raise SystemExit('requires --disposable-only; creates and kills disposable services')
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise SystemExit('requires disposable Linux systemd host and root')
    print(json.dumps(dict(kernel=os.uname().release,
                          systemd=run('systemctl', '--version').splitlines()[0])), flush=True)
    prove(False)
    prove(True)
    print('PASS: retained FD survives path reuse safely but NOT invocation reuse; watchdog BLOCKED')
