#!/usr/bin/env python3
"""Infrastructure reliability test launcher — pilot (180s) and soak (86400s) INFRASTRUCTURE mode.
Uses ingest-only service, no paper trades, preserves historical accounting.
"""
import argparse
import json
import os
import subprocess
import time
import datetime
from pathlib import Path

EVIDENCE_ROOT = Path('/var/lib/polymarket-btc/phase2b-evidence')
PREFIX = 'phase2b-final-234452'
INFRA_UNIT = 'polymarket-btc-infrastructure.service'
PS_SCRIPT = r'C:\Users\Public\phase2b-final-234452-host.ps1'
OBS_PATH = '/opt/polymarket-btc/deploy/paper-validation.py'
PYTHON_BIN = '/opt/polymarket-btc/.venv/bin/python'

PHASE_CONFIG = {
    'pilot': {'seconds': 180, 'runtime_max': 300, 'require_approval': False},
    'soak': {'seconds': 86400, 'runtime_max': 86520, 'require_approval': True},
}

def run(*args, check=True, timeout=30):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=timeout).stdout.strip()

def main():
    parser = argparse.ArgumentParser(description='Launch infrastructure reliability test')
    parser.add_argument('phase', choices=['pilot', 'soak'], help='Test phase: pilot (180s) or soak (86400s)')
    parser.add_argument('--output', type=Path, required=True, help='Evidence output directory')
    parser.add_argument('--host-heartbeat', type=Path, required=True, help='Windows host heartbeat JSON path')
    parser.add_argument('--integration-approval', type=Path, help='Integration approval JSON (required for soak)')
    args = parser.parse_args()

    cfg = PHASE_CONFIG[args.phase]
    seconds = cfg['seconds']
    runtime_max = cfg['runtime_max']
    require_approval = cfg['require_approval']

    if require_approval and not args.integration_approval:
        raise SystemExit("soak phase requires --integration-approval")

    evidence_dir = args.output
    host_heartbeat = args.host_heartbeat

    suffix = datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ')
    print(f"INFRASTRUCTURE {args.phase.upper()} evidence directory: {evidence_dir}")
    print(f"Host heartbeat: {host_heartbeat}")
    print(f"Duration: {seconds}s")
    print(f"RuntimeMaxSec: {runtime_max}s")

    if require_approval:
        approval = json.loads(args.integration_approval.read_text())
        if approval.get('verdict') != 'PHASE_2B_PROVISIONAL':
            raise SystemExit(f"soak requires PHASE_2B_PROVISIONAL approval, got {approval.get('verdict')}")

    print("\n--- PREFLIGHT CHECKS ---")
    assert run('systemctl', 'is-active', INFRA_UNIT, check=False) == 'inactive', f"service {INFRA_UNIT} not inactive"
    assert run('systemctl', 'is-enabled', INFRA_UNIT, check=False) == 'disabled', f"service {INFRA_UNIT} not disabled"
    print(f"  Service {INFRA_UNIT} inactive + disabled: OK")

    # Infrastructure test does NOT use STOP sentinel
    stop_path = Path('/var/lib/polymarket-btc/state/STOP')
    if stop_path.exists():
        print(f"  WARNING: STOP sentinel exists at {stop_path} - infrastructure test ignores it")

    print("\n--- CONFIGURING RUNTIME BOUND ---")
    override = Path('/etc/systemd/system') / f'{INFRA_UNIT}.d' / 'override.conf'
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(f'[Service]\nRestart=no\nRuntimeMaxSec={runtime_max}\n')
    subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=15)
    print(f"  RuntimeMaxSec={runtime_max}, Restart=no: OK")

    import importlib.util
    spec = importlib.util.spec_from_file_location('guard', '/mnt/c/Users/Public/phase2b-guard-tests.py')
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)

    os.environ['PYTHONPATH'] = '/opt/polymarket-btc/src'
    spec2 = importlib.util.spec_from_file_location('pv', '/opt/polymarket-btc/deploy/paper-validation.py')
    module = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(module)
    identity = module.code_hash()
    print(f"\n--- CODE HASH ---  {identity}")

    if require_approval:
        if approval.get('code_config_sha256') != identity:
            raise SystemExit(f"code_config_sha256 mismatch: {identity}")

    launch = {
        'phase': args.phase,
        'suffix': suffix,
        'started_wall': time.time(),
        'duration': seconds,
        'runtime_max': runtime_max,
        'host_heartbeat': str(host_heartbeat),
        'ps_script': PS_SCRIPT,
        'code_config_sha256': identity,
        'evidence_dir': str(evidence_dir),
    }
    (EVIDENCE_ROOT / f'infrastructure-{args.phase}-launch-{suffix}.json').write_text(json.dumps(launch, indent=2))

    print("\n--- STARTING HOST HEARTBEAT ---")
    hb_unit = f'{PREFIX}-infra-{args.phase}-heartbeat-{suffix}'
    hb_cmd = [
        'powershell.exe', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass',
        '-File', PS_SCRIPT,
        str(host_heartbeat), str(seconds + 120)
    ]
    run('systemd-run', '--unit', hb_unit, '--property', 'Restart=no',
        '--property', 'Type=simple', '--', *hb_cmd)
    print(f"  Heartbeat unit: {hb_unit}")

    time.sleep(3)
    assert host_heartbeat.exists(), f"Host heartbeat not created at {host_heartbeat}"
    hb_props = run('systemctl', 'show', hb_unit, '-p', 'ActiveState,MainPID')
    print(f"  Heartbeat running: {hb_props}")

    print("\n--- ARMING DEADLINE TIMER ---")
    deadline_seconds = seconds + 120
    guard_unit, folder = g.arm(f'infra-{args.phase}-{suffix}', INFRA_UNIT, deadline_seconds,
                               Path('/var/lib/polymarket-btc/paper.sqlite'))
    print(f"  Guard: {guard_unit}, folder: {folder}")
    timer_props = run('systemctl', 'show', guard_unit + '.timer', '-p', 'ActiveState,NextElapseUSecMonotonic')
    print(f"  Timer: {timer_props}")

    print("\n--- STARTING OBSERVER ---")
    obs_unit = f'{PREFIX}-infra-{args.phase}-observer-{suffix}'
    obs_cmd = [
        PYTHON_BIN, OBS_PATH, args.phase,
        '--seconds', str(seconds),
        '--output', str(evidence_dir),
        '--host-heartbeat', str(host_heartbeat),
    ]
    if args.integration_approval:
        obs_cmd.extend(['--integration-approval', str(args.integration_approval)])
    run('systemd-run', '--unit', obs_unit, '--property', 'Restart=no',
        '--property', 'Type=simple', '--', *obs_cmd)
    print(f"  Observer unit: {obs_unit}")

    time.sleep(5)
    obs_props = run('systemctl', 'show', obs_unit, '-p', 'ActiveState,MainPID')
    svc_props = run('systemctl', 'show', INFRA_UNIT, '-p', 'ActiveState,MainPID')
    print(f"  Observer: {obs_props}")
    print(f"  Service: {svc_props}")

    print("\n--- VERIFICATION ---")
    assert 'ActiveState=active' in obs_props, f"Observer not running: {obs_props}"
    assert 'ActiveState=active' in svc_props, f"Service not running: {svc_props}"
    print("  All processes running: OK")

    verify = {
        'phase': args.phase,
        'heartbeat_unit': hb_unit,
        'heartbeat_props': hb_props,
        'observer_unit': obs_unit,
        'observer_props': obs_props,
        'service_props': svc_props,
        'timer_unit': guard_unit + '.timer',
        'timer_props': timer_props,
        'all_running': True,
        'verified_wall': time.time(),
    }
    (EVIDENCE_ROOT / f'infrastructure-{args.phase}-verify-{suffix}.json').write_text(json.dumps(verify, indent=2))

    print(f"\n=== INFRASTRUCTURE {args.phase.upper()} LAUNCH COMPLETE ===")
    print(f"Evidence: {evidence_dir}")
    print(f"Heartbeat: {hb_unit}")
    print(f"Observer: {obs_unit}")
    print(f"Service: {INFRA_UNIT}")
    fire_at = datetime.datetime.fromtimestamp(time.time() + deadline_seconds, datetime.UTC).isoformat()
    print(f"Deadline timer: {guard_unit}.timer (fires at {fire_at})")
    print()
    print("Launcher will now exit. All processes run independently via systemd.")
    print("Do NOT manually stop the service, timer, or heartbeat.")

if __name__ == '__main__':
    main()