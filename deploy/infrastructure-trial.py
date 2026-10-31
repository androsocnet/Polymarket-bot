#!/usr/bin/env python3
"""Native ingest-only trial evidence. Never grants acceptance or writes approvals."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

UNIT = 'polymarket-btc-infrastructure.service'
ROOT = Path(__file__).resolve().parents[1]
ENV = Path('/etc/polymarket-btc/infrastructure.env')
STATE = Path('/var/lib/polymarket-btc-infrastructure')
DATA = STATE / 'data'
EVIDENCE = STATE / 'evidence'
TRADING = ('orders', 'positions', 'settlements', 'decisions', 'equity')
PROPERTIES = ('LoadState,ActiveState,SubState,MainPID,ControlPID,Result,NRestarts,InvocationID,'
              'ControlGroup,Restart,RuntimeMaxUSec,MemoryCurrent,MemoryPeak,MemoryMax,'
              'CPUUsageNSec,CPUQuotaPerSecUSec,TasksMax,ExecMainCode,ExecMainStatus,ActiveEnterTimestampMonotonic,'
              'InactiveEnterTimestampMonotonic,RuntimeRandomizedExtraUSec,ExecMainPID,'
              'ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic')

# Phase durations (seconds) — separate approval required when these change.
PILOT_SECONDS = 1800
SOAK_SECONDS = 86400
# Offset constants shared across launch/observe/verify to avoid scattered literals.
DEADLINE_AFTER_SECONDS = 60    # Baseline-relative, never launch-relative.
STARTUP_SECONDS = 360         # Admission ceiling; expiration blocks trial, not a scheduling guarantee.
MIN_STOP_WINDOW = 60          # Useful nominal stop window; actual shutdown proof still required.
AUDIT_AFTER_SECONDS = 600      # Independent audit timer fires at seconds + 600.
RUNTIME_MAX_PADDING = 240      # Service RuntimeMaxSec = seconds + 240 (fail-safe after deadline).
HOST_LIVENESS_SECONDS = 60     # Host heartbeat liveness window = seconds + 60.
HEARTBEAT_EXIT_MARGIN = 60     # Hard-limit headroom for sampling and clean exit after required expiry.
STALE_AGE_LIMIT = 30           # Ingestion heartbeat considered stale after this many seconds.
SAMPLE_INTERVAL_DIVISOR = 10   # Minimum samples = seconds // 10.


def command(*args, timeout=10):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('x') as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def secure_bytes(path):
    """Read root-controlled regular file through symlink-free directory descriptors."""
    import stat
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('trusted absolute path required')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            current = os.fstat(descriptor)
            if current.st_uid != 0 or current.st_mode & 0o022:
                raise ValueError('trusted root-owned non-writable parent required')
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(path.parts) - 2:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        current = os.fstat(descriptor)
        if current.st_uid != 0 or current.st_mode & 0o022 or not stat.S_ISREG(current.st_mode):
            raise ValueError('trusted root-owned non-writable regular file required')
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def timestamps(epoch):
    utc = datetime.fromtimestamp(epoch, timezone.utc)
    return {'utc': utc.isoformat(timespec='seconds').replace('+00:00', 'Z'),
            'wib': utc.astimezone(timezone(timedelta(hours=7))).isoformat(timespec='seconds')}


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def hash_due(now, previous):
    return now - previous >= 60


def require_environment_files(value):
    if value.strip() != str(ENV) + ' (ignore_errors=no)':
        raise ValueError('unexpected effective environment files')


def fresh_run(path, bot_id, run_id, started_after_us):
    with closing(connect(path)) as db:
        return db.execute('SELECT 1 FROM runs WHERE bot_id=? AND run_id=? AND started_us>=?',
                          (bot_id, run_id, started_after_us)).fetchone() is not None


def read_db_heartbeat(path, bot_id):
    with closing(connect(path)) as db:
        return db.execute('SELECT run_id,at_us,status FROM heartbeat WHERE bot_id=?', (bot_id,)).fetchone()


def wait_run(path, bot_id, started_after_us):
    until = time.monotonic() + 30
    while time.monotonic() < until:
        beat = read_db_heartbeat(path, bot_id)
        if beat and beat[2] in ('RUNNING', 'ERROR') and fresh_run(path, bot_id, beat[0], started_after_us):
            return beat[0]
        time.sleep(.1)
    raise TimeoutError('fresh service heartbeat unavailable')


def require_service_policy():
    policy = dict(line.split('=', 1) for line in command('systemctl', 'show', UNIT,
                  '--property=ExecStart,Restart,KillMode,MemoryMax,CPUQuotaPerSecUSec,TasksMax,User').splitlines())
    expected_command = str(ROOT / '.venv/bin/python') + ' -m polymarket_bot.linux_mvp ingest'
    env_files = command('systemctl', 'show', UNIT, '--property=EnvironmentFiles', '--value').strip()
    require_environment_files(env_files)
    if expected_command not in policy.get('ExecStart', ''):
        raise ValueError('effective service must use approved ingest command')
    if any(policy.get(k) != v for k, v in {'Restart': 'no', 'KillMode': 'control-group',
            'MemoryMax': '536870912', 'CPUQuotaPerSecUSec': '500ms', 'TasksMax': '64', 'User': 'polymarket-btc'}.items()):
        raise ValueError('effective service policy/limits differ')


def runtime_bound(property_name='RuntimeMaxUSec'):
    object_path = '/org/freedesktop/systemd1/unit/' + UNIT.replace('-', '_2d').replace('.', '_2e')
    raw = command('busctl', 'get-property', 'org.freedesktop.systemd1', object_path,
                  'org.freedesktop.systemd1.Service', property_name).split()
    if len(raw) != 2 or raw[0] != 't':
        raise ValueError('numeric systemd runtime bound unavailable')
    return int(raw[1])


def verify_pilot(approval, evidence_root):
    trial_id = approval.get('pilot_trial_id', '')
    if not trial_id.startswith('pilot-'):
        raise ValueError('pilot trial ID required')
    owned(trial_id)
    folder = evidence_root / trial_id
    try:
        files = {p.relative_to(folder).as_posix(): file_hash(p) for p in sorted(folder.rglob('*')) if p.is_file()}
        digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if not files or digest != approval['pilot_evidence_sha256']:
            raise ValueError('pilot evidence fingerprint mismatch')
        required = {'launch.json', 'approval.json', 'result.json', 'cleanup.json', 'post-audit.json',
                    'restart-verification.json', 'deadline.json', 'deadline-stop.json',
                    'before-db.json', 'after-db.json', 'deadline-db.json',
                    'before.sqlite', 'after.sqlite', 'deadline.sqlite', 'samples.jsonl',
                    'host.json', 'host.json.jsonl', 'journal.log', 'timing.json', 'shutdown.json',
                    'startup-timers.json', 'startup-handoff.json', 'shutdown-pre.json'}
        if not required <= files.keys():
            raise ValueError('pilot evidence inventory incomplete')
        for name in ('result.json', 'cleanup.json', 'restart-verification.json',
                     'deadline.json', 'deadline-stop.json', 'post-audit.json'):
            value = json.loads((folder / name).read_text())
            if value.get('trial_id', trial_id) != trial_id or value.get('errors'):
                raise ValueError('pilot evidence blocked: ' + name)
        launch = json.loads((folder / 'launch.json').read_text())
        result = json.loads((folder / 'result.json').read_text())
        restart = json.loads((folder / 'restart-verification.json').read_text())
        if (folder / 'startup-stop.json').exists() or (folder / 'launch-error.json').exists():
            raise ValueError('pilot startup failed')
        if json.loads((folder / 'startup-handoff.json').read_text())['errors']:
            raise ValueError('pilot startup handoff failed')
        if (launch['trial_id'] != trial_id or launch['phase'] != 'pilot' or launch['seconds'] != PILOT_SECONDS
                or launch['units'] != owned(trial_id) or not result['continuous']
                or result['window_seconds'] != PILOT_SECONDS or result['elapsed_seconds'] < PILOT_SECONDS
                or result['run_id'] != restart['continuous_run_id']
                or result['run_id'] == restart['first_run_id'] or result['ingestion']['count'] <= 0):
            raise ValueError('pilot trial/run metadata mismatch')
        timing = json.loads((folder / 'timing.json').read_text())
        shutdown = json.loads((folder / 'shutdown.json').read_text())
        validate_timing(launch, timing, result, shutdown)
        validate_shutdown_records(folder, launch, timing, result, shutdown)
        validate_deadline(timing, json.loads((folder / 'deadline-stop.json').read_text()), shutdown)
        coverage_error = heartbeat_coverage_error(folder, launch)
        if coverage_error:
            raise ValueError(coverage_error)
        samples = [json.loads(line) for line in (folder / 'samples.jsonl').read_text().splitlines()]
        if len(samples) < PILOT_SECONDS // SAMPLE_INTERVAL_DIVISOR or len(samples) != result['samples'] or any(s['run_id'] != result['run_id'] for s in samples):
            raise ValueError('pilot samples incomplete')
        before = None
        for label in ('before', 'after', 'deadline'):
            summary = json.loads((folder / (label + '-db.json')).read_text())
            if summary['integrity'] != 'ok' or summary['sha256'] != files[label + '.sqlite']:
                raise ValueError('pilot snapshot mismatch')
            if before is not None and summary['trading'] != before['trading']:
                raise ValueError('pilot trading rows changed')
            before = summary
        with (folder / 'host.json.jsonl').open() as stream:
            read_host(stream, None, json.loads((folder / 'host.json').read_text())['wall'])
    except (OSError, KeyError) as exc:
        raise ValueError('pilot evidence unavailable') from exc


def _stale_branch(counts, wall, elapsed):
    if elapsed > STALE_AGE_LIMIT:
        if counts['count'] <= 0:
            return 'zero_count'
        if counts['last_received_us'] is None:
            return 'missing_timestamp'
        age = wall - counts['last_received_us'] / 1e6
        if age < 0:
            return 'future_timestamp'
        if age > STALE_AGE_LIMIT:
            return 'age_exceeds_30'
        return 'age_in_bounds_check_failed'
    return 'not_checked'


def require_ingestion_progress(counts, wall, elapsed):
    if elapsed > STALE_AGE_LIMIT and (counts['count'] <= 0 or counts['last_received_us'] is None
                         or not 0 <= wall - counts['last_received_us'] / 1e6 <= STALE_AGE_LIMIT):
        raise ValueError('trial ingestion stale')


def stale_diagnostics(counts, wall, run_id, elapsed):
    return {
        'count': counts['count'],
        'last_received_us': counts['last_received_us'],
        'wall_time': wall,
        'computed_age_seconds': None if counts['last_received_us'] is None
                                else wall - counts['last_received_us'] / 1e6,
        'run_id': run_id,
        'failed_branch': _stale_branch(counts, wall, elapsed),
    }


def verify_artifacts(root, manifest):
    files = manifest['files']
    for name, digest in files.items():
        path = root / name
        if Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink():
            raise ValueError('unsafe artifact: ' + name)
        if not path.is_file() or file_hash(path) != digest:
            raise ValueError('artifact mismatch: ' + name)
    aliases = manifest.get('symlinks', {})
    for name, target in aliases.items():
        path = root / name
        if (name != '.venv/lib64' or target != 'lib' or not path.is_symlink()
                or os.readlink(path) != target):
            raise ValueError('unsafe artifact alias: ' + name)
    for path in root.rglob('*'):
        name = path.relative_to(root).as_posix()
        if path.is_symlink() and name not in aliases:
            raise ValueError('unsafe artifact: ' + name)
        if path.is_file() and name != 'runtime-manifest.json' and name not in files:
            raise ValueError('untracked runtime artifact: ' + name)


def verify_host_runtime(baseline):
    for name, digest in baseline['files'].items():
        path = Path(name)
        if not path.is_absolute() or not path.is_file() or file_hash(path) != digest:
            raise ValueError('host runtime mismatch: ' + name)
    for name in baseline.get('roots', []):
        for path in Path(name).rglob('*'):
            if any(part in ('site-packages', 'dist-packages') for part in path.parts):
                continue
            if path.is_file() and str(path) not in baseline['files']:
                raise ValueError('untracked host runtime: ' + str(path))


def fingerprint(root, manifest, environment, unit_text, effective):
    verify_artifacts(root, manifest)
    contract = {'version': 'observable-baseline-v1', 'startup': STARTUP_SECONDS, 'grace': DEADLINE_AFTER_SECONDS,
                'runtime_padding': RUNTIME_MAX_PADDING, 'min_stop_window': MIN_STOP_WINDOW, 'max_deadline_lateness': 30,
                'timer_accuracy_us': 1000000, 'clock': 'CLOCK_MONOTONIC',
                'owned_roles': [u.split('20261030T000000Z-')[1] for u in owned('pilot-20261030T000000Z')]}
    return hashlib.sha256(json.dumps([manifest, environment.hex(), unit_text, effective, contract],
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_approval(approval, identity, phase, seconds, git_sha):
    if approval.get('fingerprint') != identity:
        raise ValueError('approval fingerprint mismatch')
    if (approval.get('scope') != 'INFRASTRUCTURE_ONLY' or approval.get('phase') != phase
            or approval.get('seconds') != seconds or approval.get('authorized') is not True
            or approval.get('git_sha') != git_sha or not approval.get('approved_by')):
        raise ValueError('explicit phase/code approval required')
    when = approval.get('approved_at_utc', '')
    if not when.endswith('Z') or datetime.fromisoformat(when.replace('Z', '+00:00')).timestamp() > time.time():
        raise ValueError('invalid approval timestamp')
    if phase == 'soak' and (approval.get('pilot_gates') != {str(n): True for n in range(1, 9)}
                            or not approval.get('pilot_trial_id') or not approval.get('pilot_evidence_sha256')):
        raise ValueError('soak requires independently reviewed pilot, all eight gates')


def connect(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def ingestion(path, bot_id, run_id):
    with closing(connect(path)) as db:
        row = db.execute('SELECT COUNT(*),MIN(received_us),MAX(received_us) FROM observations '
                         'WHERE bot_id=? AND run_id=?', (bot_id, run_id)).fetchone()
        return dict(zip(('count', 'first_received_us', 'last_received_us'), row))


def snapshot(source, destination):
    # Reserve exclusively; never overwrite historical snapshots, even on retries.
    with destination.open('xb'):
        pass
    started = time.monotonic()
    def progress(*unused):
        if time.monotonic() - started > 30:
            raise TimeoutError('SQLite backup deadline')
    with closing(connect(source)) as db, closing(sqlite3.connect(destination)) as backup:
        db.backup(backup, pages=256, progress=progress)
        if backup.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('snapshot integrity failure')
        trading = {}
        for table in TRADING:
            digest = hashlib.sha256()
            count = 0
            for row in backup.execute(f'SELECT * FROM {table} ORDER BY ' + ','.join(
                    str(i + 1) for i in range(len(backup.execute(f'PRAGMA table_info({table})').fetchall())))):
                digest.update(json.dumps(row, separators=(',', ':')).encode() + b'\n')
                count += 1
            trading[table] = {'count': count, 'sha256': digest.hexdigest()}
        count = backup.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
        runs = [r[0] for r in backup.execute('SELECT run_id FROM runs')]
    return {'observations': count, 'trading': trading, 'run_ids': runs,
            'sha256': file_hash(destination), 'integrity': 'ok'}


def quiescent(properties, pids):
    return (properties.get('MainPID') == '0' and properties.get('ControlPID') == '0'
            and not pids and properties.get('ActiveState') in ('inactive', 'failed', 'active')
            and not (properties.get('ActiveState') == 'active' and properties.get('SubState') != 'exited'))


def host_error(previous, current, wall):
    try:
        deltas = [current[k] - previous[k] for k in ('wall', 'mono', 'boottime')]
        if (current['boot'] != previous['boot'] or current['session'] != previous['session']
                or current['sequence'] != previous['sequence'] + 1 or current['interrupted']
                or abs(wall - current['wall']) > 15 or not all(0 < d <= 20 for d in deltas)
                or max(deltas) - min(deltas) > 2):
            return 'NATIVE_CONTINUITY_FAILED'
    except (KeyError, TypeError, ValueError):
        return 'NATIVE_CONTINUITY_UNREADABLE'
    return ''


def read_host(stream, previous, wall):
    while True:
        position = stream.tell()
        line = stream.readline()
        if not line.endswith('\n'):
            stream.seek(position)
            break
        current = json.loads(line)
        if previous is not None and host_error(previous, current, current['wall']):
            raise ValueError('host continuity failed')
        if previous is None and (current['sequence'] != 1 or current['interrupted']):
            raise ValueError('host evidence does not start at sequence 1')
        previous = current
    if previous is None or not -2 <= wall - previous['wall'] <= 15:
        raise ValueError('stale host heartbeat')
    return previous


def properties(unit):
    return dict(line.split('=', 1) for line in command('systemctl', 'show', unit,
                '--property=' + PROPERTIES).splitlines())


def memory_events(group):
    path = Path('/sys/fs/cgroup') / group.lstrip('/') / 'memory.events'
    return dict(line.split() for line in path.read_text().splitlines())


def cgroup_pids(group):
    if not group:
        return []
    root = Path('/sys/fs/cgroup') / group.lstrip('/')
    if not root.exists():
        return []
    return sorted({int(pid) for path in [root / 'cgroup.procs', *root.rglob('*/cgroup.procs')]
                   if path.exists() for pid in path.read_text().split()})


def unit_evidence(unit, old_group=''):
    state = properties(unit)
    if unit.endswith('.timer'):
        state.update(MainPID='0', ControlPID='0')  # Timer units have no process properties.
    return {'unit': unit, 'properties': state,
            'pids': sorted(set(cgroup_pids(state.get('ControlGroup', '')) + cgroup_pids(old_group)))}


def stable_effective_unit(text):
    # ExecStart includes runtime PID/timestamps/status; unit text still binds full command.
    import re
    return re.sub(r' ; start_time=.*? ; status=[^}]* }', ' }', text)


def runtime_identity():
    manifest = json.loads(secure_bytes(ROOT / 'runtime-manifest.json'))
    verify_host_runtime(manifest['host_python'])
    effective = command('systemctl', 'show', UNIT, '--property=ExecStart,Environment,EnvironmentFiles,'
                        'User,Group,WorkingDirectory,Restart,RuntimeMaxUSec,KillMode,TimeoutStopUSec,'
                        'MemoryMax,CPUQuotaPerSecUSec,TasksMax,FragmentPath,DropInPaths')
    identity = fingerprint(ROOT, manifest, ENV.read_bytes(), command('systemctl', 'cat', UNIT), stable_effective_unit(effective))
    return identity, manifest


def environment():
    result = {}
    for line in ENV.read_text().splitlines():
        if line and not line.startswith('#'):
            key, value = line.split('=', 1)
            if key in result or value.startswith(('"', "'")):
                raise ValueError('ambiguous environment')
            result[key] = value
    if (result.get('MODE') != 'PAPER_ONLY'
            or result.get('MVP_DB_PATH') != str(DATA / 'infrastructure.sqlite')
            or result.get('MVP_STATE_DIR') != str(DATA / 'state')):
        raise ValueError('unexpected infrastructure configuration')
    return result


def check_approval(path, phase, seconds):
    identity, manifest = runtime_identity()
    require_environment_files(command('systemctl', 'show', UNIT, '--property=EnvironmentFiles', '--value'))
    approval = json.loads(secure_bytes(path))
    validate_approval(approval, identity, phase, seconds, manifest['git_sha'])
    if phase == 'soak':
        verify_pilot(approval, EVIDENCE)
    return identity, approval


def stop_units(units):
    errors, evidence = [], []
    for unit in units:
        group = ''
        try:
            initial = properties(unit)
            group = initial.get('ControlGroup', '')
            if initial.get('LoadState') != 'not-found':
                command('systemctl', 'stop', unit, timeout=45)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(unit + ': ' + str(exc))
        try:
            item = unit_evidence(unit, group)
            evidence.append(item)
            if not quiescent(item['properties'], item['pids']):
                errors.append(unit + ': processes or stop job remain')
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(unit + ': evidence unavailable: ' + str(exc))
    return errors, evidence


def owned(trial_id):
    # No wildcard, caller-provided unit, or shell interpolation.
    if not __import__('re').fullmatch(r'(pilot|soak)-\d{8}T\d{6}Z', trial_id):
        raise ValueError('invalid trial ID')
    prefix = 'infra-' + trial_id
    return [prefix + '-heartbeat.service', prefix + '-observer.service',
            prefix + '-deadline.timer', prefix + '-deadline.service', prefix + '-audit.timer',
            prefix + '-audit.service', prefix + '-startup.timer', prefix + '-startup.service']


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def monotonic_now(data):
    now = time.monotonic()
    if boot_id() != data['boot_id'] or abs(now - time.clock_gettime(time.CLOCK_MONOTONIC)) > .1:
        raise ValueError('boot/monotonic clock mismatch')
    return now


def helper_options():
    # RemainAfterExit=yes: systemd retains the terminal state (Result, ExecMainCode/Status)
    # of a successful transient helper after exit instead of garbage-collecting the unit;
    # stopped/failed units retain state until stopped/reset-failed, giving audit() and
    # post-exit inspect-cleanup a genuine exited-process proof (no journal-only evidence).
    return ['--property=Restart=no', '--property=KillMode=control-group', '--property=RemainAfterExit=yes',
            '--property=TimeoutStopSec=10', '--setenv=PYTHONDONTWRITEBYTECODE=1',
            '--setenv=PYTHONPATH=' + str(ROOT / 'src'), '--property=MemoryMax=256M', '--property=CPUQuota=50%']


def arm_timer(data, timer, expiry, action, folder, runtime=360):
    if expiry - monotonic_now(data) < MIN_STOP_WINDOW:
        raise ValueError('timer expired or insufficient arming window')
    command('systemd-run', '--unit=' + timer[:-6], '--on-boot=' + str(round(expiry * 1e6)) + 'us',
            '--timer-property=AccuracySec=1s', '--timer-property=RandomizedDelaySec=0',
            '--timer-property=WakeSystem=no', '--timer-property=RemainAfterElapse=no',
            *helper_options(), '--property=RuntimeMaxSec=' + str(runtime), sys.executable,
            str(ROOT / 'deploy/infrastructure-trial.py'), action, '--folder', str(folder))
    object_path = '/org/freedesktop/systemd1/unit/' + timer.replace('-', '_2d').replace('.', '_2e')
    raw = json.loads(command('busctl', '--json=short', 'call', 'org.freedesktop.systemd1', object_path,
                             'org.freedesktop.DBus.Properties', 'GetAll', 's', 'org.freedesktop.systemd1.Timer'))
    values = {key: value['data'] for key, value in raw['data'][0].items()}
    usec = round(expiry * 1e6)
    expected = {'Unit': timer[:-6] + '.service', 'TimersMonotonic': [['OnBootUSec', usec, usec]],
                'TimersCalendar': [], 'NextElapseUSecMonotonic': usec, 'NextElapseUSecRealtime': 0,
                'AccuracyUSec': 1000000, 'RandomizedDelayUSec': 0, 'WakeSystem': False,
                'RemainAfterElapse': False, 'OnClockChange': False, 'OnTimezoneChange': False}
    if (any(values.get(k) != v for k, v in expected.items())
            or values.get('RandomizedOffsetUSec', 0) != 0
            or properties(timer)['ActiveState'] != 'active'):
        raise ValueError('effective timer policy/expiry mismatch')
    if expiry - monotonic_now(data) < MIN_STOP_WINDOW:
        raise ValueError('timer expired or insufficient arming window')
    return values


def start_observation_timing(folder, data, service):
    baseline = monotonic_now(data)
    activation = int(service['ActiveEnterTimestampMonotonic']) / 1e6
    runtime = (data['seconds'] + RUNTIME_MAX_PADDING)
    expiry = baseline + data['seconds'] + DEADLINE_AFTER_SECONDS
    if (activation <= 0 or activation > baseline or baseline - data['launch_mono'] >= STARTUP_SECONDS
            or runtime_bound() != runtime * 1000000
            or runtime_bound('RuntimeRandomizedExtraUSec') != 0
            or activation + runtime - expiry < MIN_STOP_WINDOW):
        raise ValueError('activation/runtime/startup timing margin unavailable')
    effective = arm_timer(data, data['units'][2], expiry, 'deadline', folder)
    timing = {'fingerprint': data['fingerprint'], 'boot_id': data['boot_id'],
              'activation_mono': activation, 'baseline_mono': baseline, 'deadline_mono': expiry,
              'runtime_expiry_mono': activation + runtime, 'invocation_id': service['InvocationID'],
              'timer': effective}
    write_json(folder / 'timing.json', timing)
    return timing


def trial_metadata(folder):
    data = json.loads(secure_bytes(folder / 'launch.json'))
    if folder != EVIDENCE / data['trial_id'] or owned(data['trial_id']) != data['units']:
        raise ValueError('trial ownership mismatch')
    return data


def write_once_json(path, value):
    # Exclusive creation under shutdown lock; interrupted writes remain fail-closed.
    with path.open('x') as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.flush()
        os.fsync(output.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_run_identity(folder, data, timing, result=None):
    restart = json.loads(secure_bytes(folder / 'restart-verification.json'))
    second = restart['second']
    if (restart['trial_id'] != data['trial_id'] or restart['errors']
            or not restart['continuous_run_id']
            or restart['continuous_run_id'] == restart['first_run_id']
            or not timing.get('invocation_id')
            or second['ActiveState'] != 'active' or int(second['MainPID']) <= 0
            or second['InvocationID'] != timing['invocation_id']
            or int(second['ActiveEnterTimestampMonotonic']) / 1e6 != timing['activation_mono']
            or (result is not None and result['run_id'] != restart['continuous_run_id'])):
        raise ValueError('continuous service/run identity mismatch')
    return restart['continuous_run_id']


def validate_shutdown_records(folder, data, timing, result, shutdown):
    run_id = validate_run_identity(folder, data, timing, result)
    pre = json.loads(secure_bytes(folder / 'shutdown-pre.json'))
    if (pre['units'] != shutdown['pre_states'] or pre['errors']
            or pre['boot_id'] != data['boot_id'] or pre['trial_id'] != data['trial_id']
            or pre['run_id'] != run_id or shutdown['run_id'] != run_id
            or pre['at_mono'] != shutdown['stop_request_mono']):
        raise ValueError('durable pre-stop proof mismatch')


def stop_main(folder, data, timing):
    import fcntl
    with (folder / 'shutdown.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (folder / 'shutdown.json').exists():
            proof = json.loads(secure_bytes(folder / 'shutdown.json'))
            # Observer may reuse deadline's proof only after independent validation.
            validate_timing(data, timing, dict(continuous=True, errors=[],
                observation_end_mono=timing['baseline_mono'] + data['seconds']), proof)
            validate_shutdown_records(folder, data, timing, None, proof)
            return proof
        return record_main_shutdown(folder, data, timing)


def record_main_shutdown(folder, data, timing):
    requested = monotonic_now(data)
    errors, pre_states = [], []
    try:
        # Persist the ACTIVE service identity immediately before stop. A stopped unit
        # may clear InvocationID; only the pre-stop identity proves which invocation
        # was actually stopped (run identity bound to timing.json from restart
        # verification). Missing, stale, mismatched or unverifiable pre-stop
        # identity must fail even if the post-stop state looks quiescent.
        pre_states.append(unit_evidence(UNIT))
        pre_props = pre_states[0]['properties']
        if (pre_states[0]['unit'] != UNIT
                or pre_props.get('ActiveState') != 'active'
                or not pre_props.get('InvocationID')):
            errors.append('pre-stop active service identity unavailable')
        elif pre_props['InvocationID'] != timing.get('invocation_id', ''):
            errors.append('pre-stop identity mismatch against continuous-run timing proof')
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append('pre-stop identity unavailable: ' + str(exc))
    run_id = None
    try:
        run_id = validate_run_identity(folder, data, timing)
    except Exception as exc:
        errors.append('pre-stop run identity unavailable: ' + str(exc))
    pre_proof = {'trial_id': data['trial_id'], 'boot_id': data['boot_id'],
                 'at_mono': requested, 'run_id': run_id, 'units': pre_states, 'errors': errors.copy()}
    write_once_json(folder / 'shutdown-pre.json', pre_proof)
    stop_errors, evidence = stop_units([UNIT])
    errors.extend(stop_errors)
    # systemd may clear ControlGroup after stop; inspect the captured target too.
    if pre_states and evidence:
        group = pre_states[0]['properties'].get('ControlGroup', '')
        evidence[0]['pids'] = sorted(set(evidence[0]['pids'] + cgroup_pids(group)))
    completed = monotonic_now(data)
    state = evidence[0]['properties'] if evidence else {}
    fallback = state.get('Result') == 'timeout'
    post_invocation = state.get('InvocationID', '')
    if post_invocation and post_invocation != timing.get('invocation_id', ''):
        errors.append('post-stop invocation identity changed')
    if (not evidence or state.get('ActiveState') != 'inactive' or not quiescent(state, evidence[0]['pids'])
            or state.get('Result') != 'success'
            or completed >= timing['runtime_expiry_mono']):
        errors.append('shutdown not proven before runtime backstop')
    proof = {'stop_request_mono': requested, 'quiescence_mono': completed, 'runtime_fallback': fallback,
             'pre_states': pre_states, 'units': evidence, 'errors': errors, 'boot_id': data['boot_id'],
             'trial_id': data['trial_id'], 'run_id': run_id}
    write_once_json(folder / 'shutdown.json', proof)
    return proof


def validate_timing(data, timing, result, shutdown):
    values = [timing[k] for k in ('activation_mono', 'baseline_mono', 'deadline_mono', 'runtime_expiry_mono')]
    values += [result['observation_end_mono'], shutdown['stop_request_mono'], shutdown['quiescence_mono']]
    if (any(isinstance(value, bool) or not math.isfinite(value) for value in values)
            or not 0 < timing['activation_mono'] <= timing['baseline_mono']):
        raise ValueError('invalid finite activation/shutdown timing')
    if (timing['fingerprint'] != data['fingerprint'] or timing['boot_id'] != data['boot_id']
            or timing['deadline_mono'] != timing['baseline_mono'] + data['seconds'] + DEADLINE_AFTER_SECONDS
            or timing['runtime_expiry_mono'] != timing['activation_mono'] + data['seconds'] + RUNTIME_MAX_PADDING
            or timing['runtime_expiry_mono'] - timing['deadline_mono'] < MIN_STOP_WINDOW
            or not result['continuous'] or result['errors'] or shutdown['errors'] or shutdown['runtime_fallback']
            or result['observation_end_mono'] < timing['baseline_mono'] + data['seconds']
            or not result['observation_end_mono'] <= shutdown['stop_request_mono'] <= shutdown['quiescence_mono'] < timing['runtime_expiry_mono']):
        raise ValueError('incomplete or failed observation/shutdown timing proof')
    state = shutdown['units'][0]['properties']
    pre_state = (shutdown.get('pre_states') or [{}])[0].get('properties', {})
    post_invocation = state.get('InvocationID', '')
    if (state['ActiveState'] != 'inactive' or state['Result'] != 'success'
            or not timing.get('invocation_id')
            or shutdown['boot_id'] != data['boot_id']
            or shutdown['units'][0]['unit'] != UNIT
            or shutdown['pre_states'][0]['unit'] != UNIT
            or pre_state.get('ActiveState') != 'active'
            or (post_invocation and post_invocation != timing['invocation_id'])
            or pre_state.get('InvocationID') != timing['invocation_id']
            or not quiescent(state, shutdown['units'][0]['pids'])):
        raise ValueError('shutdown state/identity proof failed')


def validate_deadline(timing, proof, shutdown=None):
    # Acceptance limit, not a promise of Linux dispatch latency; retain nominal stop budget.
    if (proof['errors'] or not timing['deadline_mono'] <= proof['trigger_mono'] <= timing['deadline_mono'] + 30
            or proof['trigger_mono'] >= timing['runtime_expiry_mono']):
        raise ValueError('deadline trigger outside accepted 30-second window')
    if shutdown is not None and proof.get('shutdown') != shutdown:
        raise ValueError('deadline/canonical shutdown proof mismatch')


def startup_timeout(folder):
    data = trial_metadata(folder)
    errors, evidence = stop_units([UNIT, data['units'][1], data['units'][0], data['units'][6]])
    write_json(folder / 'startup-stop.json', {'trigger_mono': time.monotonic(), 'units': evidence,
                                           'errors': ['startup watchdog expired', *errors]})
    return 1


def deadline(folder):
    data = trial_metadata(folder)
    trigger = monotonic_now(data)
    errors = []
    try:
        timing = json.loads(secure_bytes(folder / 'timing.json'))
        validate_deadline(timing, {'trigger_mono': trigger, 'errors': []})
        if not (folder / 'shutdown.json').exists():
            proof = stop_main(folder, data, timing)
        else:
            proof = json.loads(secure_bytes(folder / 'shutdown.json'))
        errors.extend(proof['errors'])
    except Exception as exc:
        errors.append('deadline timing/shutdown evidence: ' + str(exc))
        stop_errors, main_evidence = stop_units([UNIT])
        errors.extend(stop_errors)
        proof = {'errors': errors.copy(), 'units': main_evidence}
        timing = {}
    # Heartbeat helper (units[0]) keeps its full required lifetime (expiry =
    # launch + startup + seconds + 120; helper RuntimeMaxSec adds exit margin):
    # it self-terminates at coverage; stopping it here would truncate host
    # continuity before T+seconds+120 and fail host continuity evidence.
    # Observer terminal evidence must be captured BEFORE this stop: stopping a
    # successful retained transient unloads it, and the audit (which runs later)
    # can only resolve the observer through this persisted, trial-bound snapshot.
    try:
        observer_exit = unit_evidence(data['units'][1])
    except Exception as exc:
        observer_exit = None
        errors.append('observer terminal evidence unavailable: ' + str(exc))
    try:
        write_json(folder / 'deadline-stop.json', {'trial_id': data['trial_id'],
                   'boot_id': data.get('boot_id'), 'at': timestamps(time.time()),
                   'trigger_mono': trigger,
                   'helper_exits': [observer_exit] if observer_exit is not None else [],
                   'errors': [*errors, 'deadline stop incomplete']})
    except Exception as exc:
        errors.append('observer terminal evidence persistence: ' + str(exc))
    stop_errors, evidence = stop_units([data['units'][1], data['units'][2],
                                        *data['units'][6:]])
    errors.extend(stop_errors)
    try:
        result = json.loads(secure_bytes(folder / 'result.json'))
        validate_timing(data, timing, result, proof)
        validate_shutdown_records(folder, data, timing, result, proof)
    except Exception as exc:
        errors.append(str(exc))
    write_json(folder / 'deadline-stop.json', {'trial_id': data['trial_id'],
               'boot_id': data.get('boot_id'), 'at': timestamps(time.time()), 'trigger_mono': trigger,
               'helper_exits': [observer_exit] if observer_exit is not None else [],
               'units': evidence, 'shutdown': proof, 'errors': errors})
    try:
        # Whole snapshot subprocess includes integrity/digests/hash under one hard timeout.
        subprocess.run([sys.executable, str(ROOT / 'deploy/infrastructure-trial.py'),
                        'snapshot', '--folder', str(folder)], check=True, timeout=60)
    except Exception as exc:
        errors.append('deadline backup: ' + str(exc))
    write_json(folder / 'deadline.json', {'at': timestamps(time.time()), 'units': evidence, 'errors': errors})
    return 1 if errors else 0


def validate_helper_exits(data, evidence, include_audit=False):
    """Verify successful terminal helper results from the PRE-STOP unit snapshot.

    `evidence` must be captured before any systemctl stop: stopping unloads a
    successful transient unit, so post-stop evidence can never prove success.
    A retained (RemainAfterExit=yes) successful helper shows ActiveState=active,
    SubState=exited, ExecMainCode=1 (exited normally), ExecMainStatus=0 and an
    empty cgroup. An unloaded unit lists only placeholder properties that never
    prove a genuine successful exit and is rejected for required helpers.
    """
    main = next((item for item in evidence if item['unit'] == UNIT), None)
    if (main is None or main['properties'].get('ActiveState') != 'inactive'
            or main['properties'].get('Result') != 'success'
            or not quiescent(main['properties'], main['pids'])):
        raise ValueError('current main service not inactive/successful')
    required = {data['units'][0], data['units'][1], data['units'][3]}
    if include_audit:
        required.add(data['units'][5])
    seen = set()
    for item in evidence:
        unit, state = item['unit'], item['properties']
        if not unit.endswith('.service') or unit == UNIT:
            continue
        if state.get('LoadState') == 'not-found':
            if unit in required:
                raise ValueError(unit + ': successful terminal helper evidence missing')
            continue  # Canceled/unstarted optional transient helper.
        terminated = ((state.get('ActiveState') == 'inactive' and state.get('SubState') == 'dead')
                      or (state.get('ActiveState') == 'active' and state.get('SubState') == 'exited'))
        if (not terminated or state.get('Result') != 'success'
                or state.get('ExecMainCode') != '1'
                or state.get('ExecMainStatus') != '0'
                or not quiescent(state, item['pids'])):
            raise ValueError(unit + ': successful terminal helper result unavailable')
        seen.add(unit)
    if not required <= seen:
        raise ValueError('successful terminal helper evidence missing for required helpers')


def bound_helper_records(folder, data):
    """Trial-bound pre-stop terminal snapshots persisted by controller helpers.

    Deadline and audit stop successful retained transient helpers, and systemd
    unloads such a unit on stop, erasing its properties. The journal is not used
    as evidence; instead each controller step persists the pre-stop `systemctl
    show` snapshot (bound to the exact trial ID and boot ID) before its stops.
    """
    records = {}
    for name in ('cleanup.json', 'deadline-stop.json'):
        try:
            record = json.loads((folder / name).read_text())
        except (OSError, ValueError):
            continue
        if (record.get('trial_id') != data['trial_id']
                or record.get('boot_id') != data['boot_id']):
            continue
        allowed = {data['units'][1]} if name == 'deadline-stop.json' else {
            data['units'][0], data['units'][3]}
        for item in record.get('helper_exits') or []:
            unit = item.get('unit') if isinstance(item, dict) else None
            if unit not in allowed:
                continue
            state = item.get('properties', {})
            try:
                start = int(state['ExecMainStartTimestampMonotonic']) / 1e6
                end = int(state['ExecMainExitTimestampMonotonic']) / 1e6
                bound = (state['LoadState'] == 'loaded' and state['InvocationID']
                         and int(state['ExecMainPID']) > 0
                         and data['launch_mono'] <= start <= end
                         <= data['audit_expiry_mono'] + 600)
            except (KeyError, TypeError, ValueError):
                bound = False
            if bound and unit not in records:
                records[unit] = item
    return records


def resolve_helper_exits(folder, data, helper_exits, units):
    """Substitute unloaded required helpers with their persisted pre-stop snapshots."""
    records = bound_helper_records(folder, data)
    resolved = list(helper_exits)
    audit_verified = []
    for unit in units:
        index = next((i for i, item in enumerate(resolved) if item['unit'] == unit), None)
        if index is None:
            continue
        if (resolved[index]['properties'].get('LoadState') == 'not-found'
                and unit in records):
            resolved[index] = records[unit]
            audit_verified.append(unit)
    return resolved, audit_verified


def inspect_cleanup(folder):
    data = trial_metadata(folder)
    errors = []
    if not (folder / 'cleanup.json').exists() or json.loads((folder / 'cleanup.json').read_text()).get('errors'):
        errors.append('cleanup audit missing or blocked')
    evidence = [unit_evidence(unit) for unit in [UNIT, *data['units']]]
    for item in evidence:
        props = item['properties']
        if not quiescent(props, item['pids']) or (item['unit'].endswith('.timer') and props['ActiveState'] != 'inactive'):
            errors.append(item['unit'] + ': not cleaned')
    resolved, audit_verified = resolve_helper_exits(
        folder, data, evidence, (data['units'][0], data['units'][1], data['units'][3]))
    try:
        validate_helper_exits(data, resolved, include_audit=True)
    except ValueError as exc:
        errors.append(str(exc))
    jobs = command('systemctl', 'list-jobs', '--no-legend', '--plain')
    for unit in [UNIT, *data['units']]:
        if unit in jobs.split():
            errors.append(unit + ': pending job')
    return {'trial_id': data['trial_id'], 'boot_id': boot_id(), 'at': timestamps(time.time()),
            'audit_verified_helpers': audit_verified, 'units': evidence,
            'errors': errors, 'verdict': 'BLOCKED' if errors else 'PENDING_REVIEW'}


def heartbeat_coverage_error(folder, data):
    """Validate complete append-only continuity through monotonic expiry."""
    try:
        required = data['heartbeat_expiry_mono']
        expected = data['launch_mono'] + STARTUP_SECONDS + data['seconds'] + 120
        if not math.isfinite(required) or required != expected:
            raise ValueError('heartbeat expiry metadata mismatch')
        previous = None
        with (folder / 'host.json.jsonl').open() as stream:
            for line in stream:
                if not line.endswith('\n'):
                    raise ValueError('heartbeat truncated record')
                current = json.loads(line)
                if any(isinstance(current[k], bool) or not math.isfinite(current[k])
                       for k in ('wall', 'mono', 'boottime')):
                    raise ValueError('heartbeat non-finite timestamp')
                if (current['boot'] != data['boot_id'] or not current['session']
                        or current['interrupted'] or (previous is None and
                        (current['sequence'] != 1 or not data['launch_mono'] <= current['mono'] <= data['launch_mono'] + 15))
                        or (previous is not None and host_error(previous, current, current['wall']))):
                    raise ValueError('heartbeat continuity failed')
                previous = current
        if previous is None or not required <= previous['mono'] <= required + 20:
            return 'heartbeat required coverage incomplete'
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return 'heartbeat coverage invalid: ' + str(exc)
    return None


def audit(folder):
    data = trial_metadata(folder)
    errors, helper_exits = [], []
    # Terminal helper evidence must exist BEFORE stopping: systemctl stop unloads a
    # successful transient helper (RemainAfterExit retention keeps it inspectable),
    # so pre-stop snapshots are the only durable terminal-state proof.
    for unit in data['units']:
        if unit == data['units'][5]:
            continue  # The running audit service is validated post-exit by inspection.
        try:
            helper_exits.append(unit_evidence(unit))
        except Exception as exc:
            errors.append('helper terminal evidence unavailable for ' + unit + ': ' + str(exc))
    helper_exits, audit_verified = resolve_helper_exits(
        folder, data, helper_exits, (data['units'][0], data['units'][1], data['units'][3]))
    heartbeat_before = next((item for item in helper_exits if item['unit'] == data['units'][0]),
                            None)
    try:
        if heartbeat_before is None:
            raise ValueError('unit evidence unavailable')
        state = heartbeat_before['properties']
        terminated = ((state.get('ActiveState') == 'inactive' and state.get('SubState') == 'dead')
                      or (state.get('ActiveState') == 'active' and state.get('SubState') == 'exited'))
        if (state.get('LoadState') == 'not-found' or not terminated
                or state.get('Result') != 'success' or state.get('ExecMainCode') != '1'
                or state.get('ExecMainStatus') != '0'
                or not quiescent(state, heartbeat_before['pids'])):
            errors.append('heartbeat did not self-terminate successfully before audit')
    except Exception as exc:
        errors.append('heartbeat pre-cleanup evidence unavailable: ' + str(exc))
    try:
        write_json(folder / 'cleanup.json', {'trial_id': data['trial_id'],
                   'boot_id': boot_id(), 'at': timestamps(time.time()),
                   'helper_exits': helper_exits, 'errors': [*errors, 'audit cleanup incomplete']})
    except Exception as exc:
        errors.append('helper terminal evidence persistence: ' + str(exc))
    stop_errors, evidence = stop_units([UNIT, *(u for u in data['units'] if u != data['units'][5])])
    errors.extend(stop_errors)
    for item in evidence:
        if item['unit'].endswith('.timer') and item['properties']['ActiveState'] != 'inactive':
            errors.append(item['unit'] + ': timer not inactive')
    # Runtime identity is recomputed and reported independently of helper checks:
    # a failure above must never suppress the final fingerprint evaluation, and a
    # fingerprint mismatch must not change the BLOCKED verdict either way.
    final_identity = {'expected_fingerprint': data['fingerprint'], 'fingerprint': None,
                      'matches_launch': False}
    try:
        identity, _ = runtime_identity()
        final_identity.update(fingerprint=identity, matches_launch=identity == data['fingerprint'])
        if identity != data['fingerprint']:
            errors.append('final runtime fingerprint changed')
    except Exception as exc:
        final_identity['error'] = str(exc)
        errors.append('final runtime identity recompute: ' + str(exc))
    try:
        shown_main = [item for item in evidence if item['unit'] == UNIT]
        validate_helper_exits(data, shown_main + helper_exits)
        if not (folder / 'deadline.json').exists():
            errors.append('independent deadline evidence missing')
        elif json.loads((folder / 'deadline.json').read_text())['errors']:
            errors.append('independent deadline failed')
        timing = json.loads(secure_bytes(folder / 'timing.json'))
        validate_timing(data, timing, json.loads(secure_bytes(folder / 'result.json')),
                        json.loads(secure_bytes(folder / 'shutdown.json')))
        validate_deadline(timing, json.loads(secure_bytes(folder / 'deadline-stop.json')),
                          json.loads(secure_bytes(folder / 'shutdown.json')))
        validate_shutdown_records(folder, data, timing, json.loads(secure_bytes(folder / 'result.json')),
                                  json.loads(secure_bytes(folder / 'shutdown.json')))
        if (folder / 'startup-stop.json').exists() or (folder / 'launch-error.json').exists():
            errors.append('startup/launch failed')
        if json.loads(secure_bytes(folder / 'startup-handoff.json'))['errors']:
            errors.append('startup handoff failed')
    except Exception as exc:
        errors.append(str(exc))
    coverage_error = heartbeat_coverage_error(folder, data)
    if coverage_error:
        errors.append(coverage_error)
    write_json(folder / 'cleanup.json', {'trial_id': data['trial_id'], 'boot_id': boot_id(),
              'at': timestamps(time.time()), 'helper_exits': helper_exits,
              'audit_verified_helpers': audit_verified, 'units': evidence,
              'heartbeat_before_cleanup': heartbeat_before, 'final_runtime_identity': final_identity,
              'errors': errors, 'verdict': 'BLOCKED' if errors else 'PENDING_REVIEW'})
    return 1 if errors else 0


def launch(phase, approval_path, reset_stop=False):
    import fcntl
    seconds = {'pilot': PILOT_SECONDS, 'soak': SOAK_SECONDS}[phase]
    # Approval validation precedes even directory creation or lock acquisition.
    identity, approval = check_approval(approval_path, phase, seconds)
    env = environment()
    require_service_policy()
    stop = Path(env['MVP_STATE_DIR']) / 'STOP'
    if stop.exists() and not reset_stop:
        raise ValueError('infrastructure STOP present; explicit --reset-stop required')
    state = properties(UNIT)
    if state['ActiveState'] != 'inactive' or not quiescent(state, cgroup_pids(state['ControlGroup'])):
        raise ValueError('infrastructure service not stopped')
    if state['Restart'] != 'no' or runtime_bound() != (seconds + RUNTIME_MAX_PADDING) * 1_000_000:
        raise ValueError('install reviewed phase-specific RuntimeMaxSec and Restart=no first')
    # Installer creates root-controlled parent; never accept service-writable ancestry.
    secure_bytes(STATE / 'controller-owner')
    lock = (STATE / 'trial.lock').open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for previous in EVIDENCE.glob('*/launch.json'):
        metadata = json.loads(previous.read_text())
        for unit in metadata['units']:
            info = unit_evidence(unit)
            if not quiescent(info['properties'], info['pids']) or (unit.endswith('.timer') and info['properties']['ActiveState'] != 'inactive'):
                raise ValueError('previous trial jobs still active: ' + unit)
    launched = time.time()
    launch_mono = time.monotonic()
    trial_id = phase + '-' + datetime.fromtimestamp(launched, timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder = EVIDENCE / trial_id
    folder.mkdir(parents=True, exist_ok=False)
    units = owned(trial_id)
    data = {'trial_id': trial_id, 'phase': phase, 'seconds': seconds, 'launch_at': timestamps(launched),
            'fingerprint': identity, 'git_sha': approval['git_sha'], 'units': units,
            'boot_id': boot_id(), 'launch_mono': launch_mono,
            'startup_expiry_mono': launch_mono + STARTUP_SECONDS,
            'heartbeat_expiry_mono': launch_mono + STARTUP_SECONDS + seconds + 120,
            'audit_expiry_mono': launch_mono + STARTUP_SECONDS + seconds + DEADLINE_AFTER_SECONDS + 420,
            'deadline_after_seconds': seconds + DEADLINE_AFTER_SECONDS}
    write_json(folder / 'approval.json', approval)
    write_json(folder / 'launch.json', data)
    script = str(ROOT / 'deploy/infrastructure-trial.py')
    common = helper_options()
    try:
        # Audit survives all early failures, including failed watchdog submission.
        audit_timer = arm_timer(data, units[4], data['audit_expiry_mono'], 'audit', folder, runtime=600)
        startup_timer = arm_timer(data, units[6], data['startup_expiry_mono'], 'startup-timeout', folder)
        write_json(folder / 'startup-timers.json', {'fingerprint': identity, 'audit': audit_timer, 'startup': startup_timer})
        command('systemd-run', '--unit=' + units[0], *common, '--property=RuntimeMaxSec=' + str(STARTUP_SECONDS + seconds + 120 + HEARTBEAT_EXIT_MARGIN),
                '--setenv=PATH=' + str(ROOT / '.venv/bin') + ':/usr/bin:/bin',
                '/bin/bash', str(ROOT / 'deploy/infrastructure-host-heartbeat.sh'), str(folder / 'host.json'), str(data['heartbeat_expiry_mono']))
        until = time.monotonic() + 10
        while not (folder / 'host.json').exists():
            if time.monotonic() >= until:
                raise TimeoutError('host heartbeat unavailable')
            time.sleep(.1)
        stop = Path(env['MVP_STATE_DIR']) / 'STOP'
        check_approval(approval_path, phase, seconds)
        if stop.exists():
            if not reset_stop:
                raise ValueError('infrastructure STOP present; explicit --reset-stop required')
            stop.unlink()
        if monotonic_now(data) >= data['startup_expiry_mono']:
            raise ValueError('startup budget exhausted')
        command('systemd-run', '--unit=' + units[1], *common, '--property=RuntimeMaxSec=' + str(STARTUP_SECONDS + seconds + 120),
                sys.executable, script, 'observe', '--folder', str(folder))
    except BaseException as exc:
        errors, evidence = stop_units([UNIT, units[0], units[1]])
        # Independent audit stays armed; startup failures must retain final evidence.
        write_json(folder / 'launch-error.json', {'error': str(exc), 'cleanup_errors': errors, 'units': evidence})
        raise
    finally:
        lock.close()
    print(json.dumps(data, indent=2))
    return 0


def observe(folder, reset_stop=False):
    data = trial_metadata(folder)
    errors, samples = [], 0
    result = {'trial_id': data['trial_id'], 'verdict': 'BLOCKED', 'continuous': False}
    db_path = None
    host_stream = None
    started = False
    timing = None
    try:
        check_approval(folder / 'approval.json', data['phase'], data['seconds'])
        env = environment()
        # Defense in depth: STOP may have appeared between launch and observe.
        stop = Path(env['MVP_STATE_DIR']) / 'STOP'
        if stop.exists() and not reset_stop:
            raise ValueError('infrastructure STOP present; explicit --reset-stop required')
        if stop.exists():
            stop.unlink()
        db_path = Path(env['MVP_DB_PATH'])
        if not db_path.exists():
            # Initialize schema only, no trading engine or account writes.
            from polymarket_bot.linux_mvp.store import MvpStore
            MvpStore.open(db_path).close()
            os.chown(db_path, __import__('pwd').getpwnam('polymarket-btc').pw_uid,
                     __import__('grp').getgrnam('polymarket-btc').gr_gid)
        before = snapshot(db_path, folder / 'before.sqlite')
        write_json(folder / 'before-db.json', before)
        if properties(UNIT)['ActiveState'] != 'inactive':
            raise ValueError('service already active')
        check_approval(folder / 'approval.json', data['phase'], data['seconds'])
        require_service_policy()
        first_start_us = time.time_ns() // 1000
        command('systemctl', 'start', UNIT, timeout=45)
        started = True
        first_run_id = wait_run(db_path, env['MVP_BOT_ID'], first_start_us)
        initial = properties(UNIT)
        if initial['ActiveState'] != 'active' or int(initial['MainPID']) <= 0:
            raise ValueError('service failed startup')
        first = initial
        restart_errors, stopped = stop_units([UNIT])
        if restart_errors:
            raise ValueError('restart clean stop failed: ' + str(restart_errors))
        check_approval(folder / 'approval.json', data['phase'], data['seconds'])
        require_service_policy()
        continuous_start_us = time.time_ns() // 1000
        command('systemctl', 'start', UNIT, timeout=45)
        run_id = wait_run(db_path, env['MVP_BOT_ID'], continuous_start_us)
        initial = properties(UNIT)
        if run_id == first_run_id:
            raise ValueError('restart did not create fresh database run')
        if (initial['ActiveState'] != 'active' or int(initial['MainPID']) <= 0
                or not initial['InvocationID'] or initial['InvocationID'] == first['InvocationID']):
            raise ValueError('separate restart verification failed')
        write_json(folder / 'restart-verification.json', {'trial_id': data['trial_id'], 'at': timestamps(time.time()),
                   'first': first, 'first_run_id': first_run_id, 'stopped': stopped,
                   'second': initial, 'continuous_run_id': run_id, 'errors': []})
        started = True
        timing = start_observation_timing(folder, data, initial)
        baseline = timing['baseline_mono']
        watchdog_errors, watchdog_evidence = stop_units([data['units'][6]])
        if watchdog_errors or (folder / 'startup-stop.json').exists():
            raise ValueError('startup watchdog handoff failed: ' + str(watchdog_errors))
        write_json(folder / 'startup-handoff.json', {'at_mono': monotonic_now(data), 'errors': watchdog_errors,
                                                  'units': watchdog_evidence})
        result['service_started_at'] = timestamps(time.time())
        result['window_seconds'] = data['seconds']
        previous_wall, previous_mono = time.time(), baseline
        host_stream = (folder / 'host.json.jsonl').open()
        host_previous = None
        last_hash = baseline
        while True:
            wall, mono = time.time(), time.monotonic()
            service = properties(UNIT)
            if any(service[key] != initial[key] for key in ('MainPID', 'InvocationID', 'NRestarts')) or service['ActiveState'] != 'active':
                raise ValueError('service identity changed')
            if not 0 <= wall - previous_wall <= 30 or abs((wall - previous_wall) - (mono - previous_mono)) > 2:
                raise ValueError('observation clock gap')
            with closing(connect(db_path)) as db:
                if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('database quick_check failed')
                beat = db.execute('SELECT run_id,at_us,status FROM heartbeat WHERE bot_id=?', (env['MVP_BOT_ID'],)).fetchone()
            if beat and fresh_run(db_path, env['MVP_BOT_ID'], beat[0], continuous_start_us):
                if run_id and run_id != beat[0]:
                    raise ValueError('database run identity changed')
                run_id = beat[0]
            if mono - baseline > 30 and (not run_id or not beat or beat[0] != run_id
                    or beat[2] not in ('RUNNING', 'ERROR') or not -2 <= time.time() - beat[1] / 1e6 <= 30):
                raise ValueError('stale database heartbeat')
            host_previous = read_host(host_stream, host_previous, time.time())
            if hash_due(mono, last_hash):
                check_approval(folder / 'approval.json', data['phase'], data['seconds'])
                last_hash = mono
            counts = ingestion(db_path, env['MVP_BOT_ID'], run_id or '')
            ingestion_wall = time.time()
            require_ingestion_progress(counts, ingestion_wall, mono - baseline)
            current = {'at': timestamps(wall), 'wall': wall, 'mono': mono, 'service': service,
                       'ingestion': counts, 'run_id': run_id, 'integrity': 'ok'}
            events = memory_events(service['ControlGroup'])
            if any(int(events.get(k, 0)) for k in ('oom', 'oom_kill', 'max')):
                raise ValueError('resource limit event')
            current['memory_events'] = events
            with (folder / 'samples.jsonl').open('a') as output:
                output.write(json.dumps(current, sort_keys=True) + '\n')
                output.flush()
                os.fsync(output.fileno())
            samples += 1
            previous_wall, previous_mono = wall, mono
            if mono - baseline >= data['seconds']:
                break
            time.sleep(min(5, max(0, baseline + data['seconds'] - time.monotonic())))
        result.update(continuous=True, observation_end_mono=mono, elapsed_seconds=mono - baseline, run_id=run_id,
                      ingestion=ingestion(db_path, env['MVP_BOT_ID'], run_id or ''))
        if samples < data['seconds'] // SAMPLE_INTERVAL_DIVISOR or result['ingestion']['count'] <= 0:
            raise ValueError('insufficient samples or trial ingestion')
    except BaseException as exc:
        errors.append(type(exc).__name__ + ': ' + str(exc))
        if str(exc) == 'trial ingestion stale':
            result['stale_ingestion_diagnostics'] = stale_diagnostics(counts, ingestion_wall, run_id, mono - baseline)
    finally:
        if host_stream is not None:
            host_stream.close()
        result.setdefault('observation_end_mono', time.monotonic())
        if started:
            if timing is not None:
                try:
                    proof = stop_main(folder, data, timing)
                    errors.extend(proof['errors'])
                    result['terminal_service'] = proof['units']
                except Exception as exc:
                    errors.append('shutdown evidence: ' + str(exc))
                    stop_errors, evidence = stop_units([UNIT])
                    errors.extend(stop_errors)
                    result['terminal_service'] = evidence
            else:
                stop_errors, evidence = stop_units([UNIT])
                errors.extend(stop_errors)
                result['terminal_service'] = evidence
        if db_path is not None:
            try:
                after = snapshot(db_path, folder / 'after.sqlite')
                write_json(folder / 'after-db.json', after)
                if after['trading'] != before['trading']:
                    errors.append('trading rows changed')
                result['observation_delta'] = after['observations'] - before['observations']
            except Exception as exc:
                errors.append('after snapshot: ' + str(exc))
        try:
            check_approval(folder / 'approval.json', data['phase'], data['seconds'])
        except Exception as exc:
            errors.append('final fingerprint: ' + str(exc))
        try:
            (folder / 'journal.log').write_text(command('journalctl', '-u', UNIT, '--since=' + data['launch_at']['utc'],
                                                       '--no-pager', '-o', 'short-iso-precise', '--utc'))
        except Exception as exc:
            errors.append('journal: ' + str(exc))
        result.update(ended_at=timestamps(time.time()), samples=samples, errors=errors,
                      verdict='BLOCKED' if errors else 'PENDING_REVIEW')
        if errors:
            result['continuous'] = False
        write_json(folder / 'result.json', result)
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('fingerprint', 'launch', 'observe', 'deadline', 'audit', 'inspect-cleanup', 'snapshot', 'startup-timeout'))
    parser.add_argument('--phase', choices=('pilot', 'soak'), default='pilot')
    parser.add_argument('--approval', type=Path)
    parser.add_argument('--folder', type=Path)
    parser.add_argument('--reset-stop', action='store_true')
    args = parser.parse_args()
    if args.action == 'fingerprint':
        identity, manifest = runtime_identity()
        print(json.dumps({'fingerprint': identity, 'git_sha': manifest['git_sha'], 'scope': 'INFRASTRUCTURE_ONLY'}, indent=2))
        return 0
    if args.action == 'launch':
        if args.approval is None:
            parser.error('--approval required')
        return launch(args.phase, args.approval, args.reset_stop)
    if args.folder is None:
        parser.error('--folder required')
    if args.action == 'snapshot':
        trial_metadata(args.folder)
        backup = snapshot(Path(environment()['MVP_DB_PATH']), args.folder / 'deadline.sqlite')
        write_json(args.folder / 'deadline-db.json', backup)
        return 0
    if args.action == 'inspect-cleanup':
        result = inspect_cleanup(args.folder)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 1 if result['errors'] else 0
    if args.action == 'observe':
        return observe(args.folder, args.reset_stop)
    return {'deadline': deadline, 'audit': audit, 'startup-timeout': startup_timeout}[args.action](args.folder)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
