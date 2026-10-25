"""Bounded WSL PAPER observer. Acceptance requires independent evidence review."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

UNIT = 'polymarket-btc-paper.service'
STATE = Path('/var/lib/polymarket-btc')


def validate_duration(phase, seconds):
    if (phase, seconds) not in [('integration', 3600), ('pilot', 180), ('soak', 86400)]:
        raise ValueError('integration must be 3600 seconds; pilot must be 180 seconds; soak must be 86400 seconds')


def require_integration_acceptance(phase, approval):
    if phase == 'soak' and (not isinstance(approval, dict)
                           or approval.get('verdict') != 'PHASE_2B_PROVISIONAL'
                           or approval.get('continuous') is not True
                           or approval.get('rollovers', 0) < 3
                           or approval.get('restart_stop_verified') is not True
                           or approval.get('reconciled') is not True
                           or approval.get('review_clear') is not True):
        raise ValueError('soak requires explicit reviewed integration acceptance')


def continuity_error(previous, current):
    wall, mono = current['wall'] - previous['wall'], current['mono'] - previous['mono']
    if current['boot'] != previous['boot']:
        return 'BOOT_CHANGED'
    if not 0 <= wall <= 30 or not 0 <= mono <= 30 or abs(wall - mono) > 2:
        return 'CLOCK_OR_OBSERVATION_GAP'
    return ''


def host_error(previous, current, wall):
    if (previous['session'] != current['session'] or previous['boot'] != current['boot']
            or current['sequence'] < previous['sequence'] or abs(wall - current['wall']) > 15
            or current['interrupted'] or not current['power_events_readable']):
        return 'WINDOWS_CONTINUITY_UNVERIFIED'
    return ''


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=45).stdout


def require_runtime_bound(seconds):
    restart = command('systemctl', 'show', UNIT, '--property=Restart').strip()
    raw = command('busctl', 'get-property', 'org.freedesktop.systemd1',
                  '/org/freedesktop/systemd1/unit/polymarket_2dbtc_2dpaper_2eservice',
                  'org.freedesktop.systemd1.Service', 'RuntimeMaxUSec').split()
    if restart != 'Restart=no' or len(raw) != 2 or raw[0] != 't':
        raise RuntimeError('independent systemd runtime bound/restart policy missing')
    if not (seconds + 30) * 1_000_000 <= int(raw[1]) <= (seconds + 120) * 1_000_000:
        raise RuntimeError('systemd runtime must allow requested window plus 30–120 seconds')


def properties():
    return dict(line.split('=', 1) for line in command('systemctl', 'show', UNIT,
        '--property=ActiveState,MainPID,InvocationID,NRestarts,MemoryCurrent,MemoryPeak,MemoryMax,CPUUsageNSec,CPUQuotaPerSecUSec,ControlGroup').splitlines())


def stop_service(stop_path):
    errors = []
    try:
        stop_path.touch()
    except OSError as exc:
        errors.append('sentinel: ' + str(exc))
    try:
        command('systemctl', 'stop', UNIT)
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append('stop: ' + str(exc))
    try:
        terminal = command('systemctl', 'show', UNIT, '--property=ActiveState,MainPID')
        if set(terminal.splitlines()) != {'ActiveState=inactive', 'MainPID=0'}:
            errors.append('service not confirmed inactive: ' + terminal)
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append('terminal query: ' + str(exc))
    return errors


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as output:
        json.dump(value, output, sort_keys=True, default=str)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def backup(path):
    started = time.monotonic()
    def progress(*unused):
        if time.monotonic() - started > 30:
            raise TimeoutError('SQLite backup deadline')
    with closing(sqlite3.connect('file:' + str(STATE / 'paper.sqlite') + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(path)) as destination:
            source.backup(destination, pages=256, progress=progress)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('backup integrity failure')


def clock():
    return dict(wall=time.time(), mono=time.monotonic(),
                boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip())


def code_hash():
    digest = hashlib.sha256()
    root = Path('/opt/polymarket-btc')
    for folder in ['src/polymarket_bot/linux_mvp', 'src/polymarket_bot/market_data', 'deploy']:
        for path in sorted((root / folder).glob('*.py')):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    digest.update(Path('/etc/polymarket-btc/paper.env').read_bytes())
    # Phase-specific bound is checked independently; every other unit byte stays approved.
    unit = command('systemctl', 'cat', UNIT)
    digest.update('\n'.join(line for line in unit.splitlines()
                            if not line.strip().startswith('RuntimeMaxSec=')).encode())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['integration', 'pilot', 'soak'])
    parser.add_argument('--seconds', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--host-heartbeat', type=Path, required=True)
    parser.add_argument('--integration-approval', type=Path)
    args = parser.parse_args()
    validate_duration(args.phase, args.seconds)
    approval = json.loads(args.integration_approval.read_text()) if args.integration_approval else None
    require_integration_acceptance(args.phase, approval)
    args.output.mkdir(parents=True, exist_ok=False)
    setup = clock()
    host_previous = json.loads(args.host_heartbeat.read_text(encoding='utf-8-sig'))
    invalid, samples = [], 0
    result = dict(phase=args.phase, setup_started_wall=setup['wall'],
                  verdict='BLOCKED', continuous=False, mode='PAPER_ONLY', boot=setup['boot'])
    # Runtime bound and no automatic service restart must be installed by operator before invoking.
    stop_path = None
    service_started = False
    try:
        unit = command('systemctl', 'cat', UNIT)
        (args.output / 'unit.txt').write_text(unit)
        result['unit_sha256'] = hashlib.sha256(unit.encode()).hexdigest()
        identity = code_hash()
        result['code_config_sha256'] = identity
        if args.phase == 'soak' and approval.get('code_config_sha256') != identity:
            raise RuntimeError('integration code/config differs')
        env = dict(line.split('=', 1) for line in Path('/etc/polymarket-btc/paper.env').read_text().splitlines()
                   if line and not line.startswith('#') and '=' in line)
        if env.get('MODE') != 'PAPER_ONLY' or env.get('MVP_DB_PATH') != str(STATE / 'paper.sqlite'):
            raise RuntimeError('unexpected PAPER configuration')
        stop_path = Path(env['MVP_STATE_DIR']) / 'STOP'
        if stop_path.exists():
            raise RuntimeError('STOP exists; explicit operator cleanup required')
        if properties()['ActiveState'] != 'inactive':
            raise RuntimeError('service already running')
        require_runtime_bound(args.seconds)
        if host_error(host_previous, host_previous, time.time()):
            raise RuntimeError('host continuity unavailable')
        write_json(args.output / 'manifest.json', result)
        backup(args.output / 'before.sqlite')
        service_started = True  # submitted start can succeed even when client times out
        command('systemctl', 'start', UNIT)
        initial_service = properties()
        if initial_service['ActiveState'] != 'active' or int(initial_service['MainPID']) <= 0:
            raise RuntimeError('service failed to start')
        baseline = previous = clock()
        deadline = baseline['mono'] + args.seconds
        result.update(started_wall=baseline['wall'], deadline_wall=baseline['wall'] + args.seconds)
        write_json(args.output / 'manifest.json', result)
        while True:
            current = clock()
            error = continuity_error(previous, current)
            if error:
                invalid.append(error)
            host = json.loads(args.host_heartbeat.read_text(encoding='utf-8-sig'))
            error = host_error(host_previous, host, current['wall'])
            if error:
                invalid.append(error)
            service = properties()
            if (service['ActiveState'] != 'active' or service['MainPID'] != initial_service['MainPID']
                    or service['InvocationID'] != initial_service['InvocationID']
                    or service['NRestarts'] != initial_service['NRestarts']):
                invalid.append('SERVICE_IDENTITY_CHANGED')
            current['service'], current['host'] = service, host
            current['loadavg'] = Path('/proc/loadavg').read_text().strip()
            current['disk_available'] = os.statvfs(STATE).f_bavail * os.statvfs(STATE).f_frsize
            cgroup = Path('/sys/fs/cgroup') / service['ControlGroup'].lstrip('/')
            current['memory_events'] = (cgroup / 'memory.events').read_text()
            if any(int(line.split()[1]) > 0 for line in current['memory_events'].splitlines()
                   if line.split()[0] in ('oom', 'oom_kill', 'max')):
                invalid.append('RESOURCE_LIMIT_EVENT')
            with closing(sqlite3.connect('file:' + str(STATE / 'paper.sqlite') + '?mode=ro', uri=True)) as db:
                current['heartbeat'] = db.execute('SELECT at_us,status,cycles,run_id FROM heartbeat').fetchone()
                current['integrity'] = db.execute('PRAGMA quick_check').fetchone()[0]
            beat = current['heartbeat']
            if current['wall'] - baseline['wall'] > 30 and (not beat or
                    beat[1] not in ('RUNNING', 'ERROR') or not 0 <= current['wall'] - beat[0] / 1e6 <= 30):
                invalid.append('STALE_OR_TERMINAL_HEARTBEAT')
            if current['integrity'] != 'ok':
                invalid.append('DB_INTEGRITY_FAILED')
            with (args.output / 'samples.jsonl').open('a') as output:
                output.write(json.dumps(current, sort_keys=True) + '\n')
                output.flush()
                os.fsync(output.fileno())
            samples += 1
            write_json(args.output / 'heartbeat.json', current)
            previous, host_previous = current, host
            if invalid or current['mono'] >= deadline:
                break
            time.sleep(min(5, max(0, deadline - time.monotonic())))
        result['continuous'] = (not invalid and previous['mono'] - baseline['mono'] >= args.seconds
                                and samples >= args.seconds // 10)
        result['verdict'] = 'PENDING_ACCEPTANCE' if result['continuous'] else 'BLOCKED'
    except BaseException as exc:
        invalid.append(type(exc).__name__ + ': ' + str(exc))
    finally:
        if service_started:
            invalid.extend(stop_service(stop_path))
        for name, capture in [
            ('backup', lambda: backup(args.output / 'after.sqlite')),
            ('journal', lambda: (args.output / 'journal.log').write_text(command('journalctl', '-u', UNIT,
                '--since=@' + str(int(setup['wall'])), '--no-pager', '-o', 'short-iso-precise'))),
        ]:
            try:
                capture()
            except BaseException as exc:
                invalid.append(name + ': ' + str(exc))
        if invalid:
            result.update(continuous=False, verdict='BLOCKED')
        result.update(ended_wall=time.time(), samples=samples, invalid_reasons=invalid)
        write_json(args.output / 'result.json', result)
    return 0 if result['verdict'] == 'PENDING_ACCEPTANCE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
