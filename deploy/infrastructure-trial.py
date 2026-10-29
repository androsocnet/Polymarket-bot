#!/usr/bin/env python3
"""Native ingest-only trial evidence. Never grants acceptance or writes approvals."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
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
              'CPUUsageNSec,CPUQuotaPerSecUSec,TasksMax,ExecMainStatus')

# Phase durations (seconds) — separate approval required when these change.
PILOT_SECONDS = 1800
SOAK_SECONDS = 86400
# Offset constants shared across launch/observe/verify to avoid scattered literals.
DEADLINE_AFTER_SECONDS = 180   # Independent deadline timer fires at seconds + 180.
AUDIT_AFTER_SECONDS = 600      # Independent audit timer fires at seconds + 600.
RUNTIME_MAX_PADDING = 240      # Service RuntimeMaxSec = seconds + 240 (fail-safe after deadline).
HOST_LIVENESS_SECONDS = 60     # Host heartbeat liveness window = seconds + 60.
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


def runtime_bound():
    object_path = '/org/freedesktop/systemd1/unit/' + UNIT.replace('-', '_2d').replace('.', '_2e')
    raw = command('busctl', 'get-property', 'org.freedesktop.systemd1', object_path,
                  'org.freedesktop.systemd1.Service', 'RuntimeMaxUSec').split()
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
                    'host.json', 'host.json.jsonl', 'journal.log'}
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
        if (launch['trial_id'] != trial_id or launch['phase'] != 'pilot' or launch['seconds'] != PILOT_SECONDS
                or launch['units'] != owned(trial_id) or not result['continuous']
                or result['window_seconds'] != PILOT_SECONDS or result['elapsed_seconds'] < PILOT_SECONDS
                or result['run_id'] != restart['continuous_run_id']
                or result['run_id'] == restart['first_run_id'] or result['ingestion']['count'] <= 0):
            raise ValueError('pilot trial/run metadata mismatch')
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
    return hashlib.sha256(json.dumps([manifest, environment.hex(), unit_text, effective],
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
            prefix + '-audit.service']


def trial_metadata(folder):
    data = json.loads(secure_bytes(folder / 'launch.json'))
    if folder != EVIDENCE / data['trial_id'] or owned(data['trial_id']) != data['units']:
        raise ValueError('trial ownership mismatch')
    return data


def deadline(folder):
    data = trial_metadata(folder)
    # This oneshot cannot stop itself and still write evidence; audit runs after exit.
    errors, evidence = stop_units([UNIT, *data['units'][:3]])
    write_json(folder / 'deadline-stop.json', {'at': timestamps(time.time()), 'units': evidence, 'errors': errors})
    try:
        # Whole snapshot subprocess includes integrity/digests/hash under one hard timeout.
        subprocess.run([sys.executable, str(ROOT / 'deploy/infrastructure-trial.py'),
                        'snapshot', '--folder', str(folder)], check=True, timeout=60)
    except Exception as exc:
        errors.append('deadline backup: ' + str(exc))
    write_json(folder / 'deadline.json', {'at': timestamps(time.time()), 'units': evidence, 'errors': errors})
    return 1 if errors else 0


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
    jobs = command('systemctl', 'list-jobs', '--no-legend', '--plain')
    for unit in [UNIT, *data['units']]:
        if unit in jobs.split():
            errors.append(unit + ': pending job')
    return {'trial_id': data['trial_id'], 'at': timestamps(time.time()), 'units': evidence,
            'errors': errors, 'verdict': 'BLOCKED' if errors else 'PENDING_REVIEW'}


def audit(folder):
    data = trial_metadata(folder)
    errors, evidence = stop_units([UNIT, *data['units'][:-1]])
    for item in evidence:
        if item['unit'].endswith('.timer') and item['properties']['ActiveState'] != 'inactive':
            errors.append(item['unit'] + ': timer not inactive')
    try:
        identity, _ = runtime_identity()
        if identity != data['fingerprint']:
            errors.append('final runtime fingerprint changed')
        if not (folder / 'deadline.json').exists():
            errors.append('independent deadline evidence missing')
        elif json.loads((folder / 'deadline.json').read_text())['errors']:
            errors.append('independent deadline failed')
    except Exception as exc:
        errors.append(str(exc))
    write_json(folder / 'cleanup.json', {'at': timestamps(time.time()), 'units': evidence,
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
    trial_id = phase + '-' + datetime.fromtimestamp(launched, timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder = EVIDENCE / trial_id
    folder.mkdir(parents=True, exist_ok=False)
    units = owned(trial_id)
    data = {'trial_id': trial_id, 'phase': phase, 'seconds': seconds, 'launch_at': timestamps(launched),
            'fingerprint': identity, 'git_sha': approval['git_sha'], 'units': units,
            'deadline_after_seconds': seconds + DEADLINE_AFTER_SECONDS, 'audit_after_seconds': seconds + AUDIT_AFTER_SECONDS}
    write_json(folder / 'approval.json', approval)
    write_json(folder / 'launch.json', data)
    script = str(ROOT / 'deploy/infrastructure-trial.py')
    common = ['--property=Restart=no', '--property=KillMode=control-group', '--property=RemainAfterExit=no',
              '--property=TimeoutStopSec=10',
              '--setenv=PYTHONDONTWRITEBYTECODE=1', '--setenv=PYTHONPATH=' + str(ROOT / 'src'),
              '--property=MemoryMax=256M', '--property=CPUQuota=50%']
    try:
        # Audit is armed first; still runs if launch fails halfway through.
        command('systemd-run', '--unit=' + units[4][:-6], '--on-active=' + str(seconds + AUDIT_AFTER_SECONDS) + 's',
                '--timer-property=RemainAfterElapse=no', '--timer-property=AccuracySec=1s',
                *common, '--property=RuntimeMaxSec=480', sys.executable, script, 'audit', '--folder', str(folder))
        command('systemd-run', '--unit=' + units[3][:-8], '--on-active=' + str(seconds + DEADLINE_AFTER_SECONDS) + 's',
                '--timer-property=RemainAfterElapse=no', '--timer-property=AccuracySec=1s',
                *common, '--property=RuntimeMaxSec=360', sys.executable, script, 'deadline', '--folder', str(folder))
        for timer in (units[2], units[4]):
            if properties(timer)['ActiveState'] != 'active':
                raise ValueError('independent timer not active: ' + timer)
        command('systemd-run', '--unit=' + units[0], *common, '--property=RuntimeMaxSec=' + str(seconds + RUNTIME_MAX_PADDING),
                '--setenv=PATH=' + str(ROOT / '.venv/bin') + ':/usr/bin:/bin',
                '/bin/bash', str(ROOT / 'deploy/infrastructure-host-heartbeat.sh'), str(folder / 'host.json'), str(seconds + HOST_LIVENESS_SECONDS))
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
        command('systemd-run', '--unit=' + units[1], *common, '--property=RuntimeMaxSec=' + str(seconds + RUNTIME_MAX_PADDING),
                sys.executable, script, 'observe', '--folder', str(folder))
    except BaseException as exc:
        errors, evidence = stop_units([UNIT, units[0], units[1]])
        if not errors:
            timer_errors, timer_evidence = stop_units(units[2:])
            errors.extend(timer_errors)
            evidence.extend(timer_evidence)
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
        baseline = time.monotonic()
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
        result.update(continuous=True, elapsed_seconds=mono - baseline, run_id=run_id,
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
        if started:
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
    parser.add_argument('action', choices=('fingerprint', 'launch', 'observe', 'deadline', 'audit', 'inspect-cleanup', 'snapshot'))
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
    return {'deadline': deadline, 'audit': audit}[args.action](args.folder)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
