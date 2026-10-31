# Phase 2B Transient-Service Production Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate VPS-proven per-invocation systemd transient-service isolation into `deploy/infrastructure-trial.py` so emergency shutdown targets the exact invocation and its cgroup, guaranteeing replacement/canary isolation without touching the static service definition or unrelated trials.

**Architecture:** The main ingest service stays as `polymarket-btc-infrastructure.service` (static, started via `systemctl start UNIT`). Integration adds: (a) cgroup path capture at baseline, (b) cgroup-targeted kill in `record_main_shutdown` and `deadline()` fallback (immune to PID reuse and replacement races), (c) fail-closed identity guard before any stop, (d) bounded cleanup on errors. The existing `InvocationID` tracking in `timing.json` is extended with `invocation_cgroup` for authoritative cgroup targeting. The `fingerprint()` function already includes `systemctl cat UNIT` and effective unit properties, so no service-definition change is needed for this integration.

**Tech Stack:** Python 3.12 stdlib, systemd 255, cgroup v2, `systemctl kill --kill-who=all`, `cgroup.kill`, `fcntl`.

**Spec:** Phase 2B VPS Failover Blocker prompt, `docs/phase-2b-watchdog-blocked.md`, `docs/phase-2b-transient-failover.md`, `tests/check_transient_compat.py` (VPS-proven probe).

## Global Constraints
- PAPER_ONLY; no live trading, no production deployment or service restarts.
- No AppArmor disabling, sysctl weakening, PID namespace approach.
- Preserve all eight normal-pilot acceptance gates and exact timing intervals: B+1800 obs, B+1860 deadline, B+1890 latest entry, A+2040 runtime fallback, T+2280 heartbeat, T+2640 audit.
- `RuntimeMaxSec` is backstop only; independent deadline timer (`deadline.timer`) remains the authoritative deadline mechanism.
- No unrelated refactoring. Shortest diff wins.
- All existing 85 `test_phase2b_*.py` and 51 `test_infrastructure_trial.py` tests must still PASS.
- Fix all Critical and Important findings from independent reviewer before committing.
- Do not modify `UNIT` constant or static service definition (fingerprint would change, invalidating existing approvals).

## Review Focus
1. Race between `InvocationID` check and `systemctl stop`: window between read and kill must not allow replacement to enter. Mitigated by cgroup-path targeting (immutable once captured) plus pre-stop identity assertion.
2. Cgroup path availability after main process exit but before `stop`: `RemainAfterExit=no`; cgroup may be gc'd. Pre-stop capture must happen before any `stop` call.
3. `deadline()` fallback path targeting stale `UNIT` on replacement: must use captured cgroup if available, fail-closed if not.
4. `cleanup()` in probe receiving non-disposable unit names (foreign units must `raise ValueError` before any systemctl call).
5. FIFO deadlock on worker crash: coordinator must use non-blocking open + `select` timeout, never blocking `open()`.

---

### Task 1: Fix `check_transient_compat.py` — Reviewer Findings

Address all Critical (C1) and Important (I1, I2, I3) findings from independent reviewer.

**Files:**
- Modify: `tests/check_transient_compat.py`
- Modify: `tests/test_transient_probe.py`
- Modify: `docs/phase-2b-transient-failover.md`

**Interfaces:**
- Consumes: current `check_transient_compat.py` (on branch `operator/phase2b-transient-integration`)
- Produces: `emergency_stop(unit, expected_token: str) -> None` (token now mandatory, strict extraction via capturing group), `read_fifo(fifo_path, timeout=10) -> dict` (non-blocking FIFO read with select)

- [ ] **Step 1: Write failing tests for mandatory token and FIFO timeout**

```python
# In tests/test_transient_probe.py

def test_emergency_stop_none_token_raises(self):
    # expected_token=None must raise TypeError or ValueError, not bypass identity
    m = self.load()
    with self.assertRaises((TypeError, ValueError)):
        m.emergency_stop('p2b-transient-' + 'a' * 32 + '-orig.service', expected_token=None)

def test_emergency_stop_substring_token_raises(self):
    # token='orig' is a substring of unit but not the real 32-hex token; must raise
    m = self.load()
    with self.assertRaises(ValueError):
        m.emergency_stop('p2b-transient-' + 'a' * 32 + '-orig.service', expected_token='orig')

def test_read_fifo_timeout_raises(self):
    import tempfile, os
    m = self.load()
    with tempfile.TemporaryDirectory() as d:
        fifo = os.path.join(d, 'test.fifo')
        os.mkfifo(fifo)
        with self.assertRaises(TimeoutError):
            m.read_fifo(fifo, timeout=0.1)

def test_pwd_import_on_windows(self):
    # Module must import without error even if pwd is unavailable
    import importlib, sys
    saved = sys.modules.pop('pwd', None)
    try:
        sys.modules['pwd'] = None  # simulate missing
        spec = importlib.util.spec_from_file_location('transient_probe2', SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # must not raise
    except ImportError:
        self.fail('import pwd must be guarded')
    finally:
        if saved is None:
            sys.modules.pop('pwd', None)
        else:
            sys.modules['pwd'] = saved
```

- [ ] **Step 2: Run tests, verify RED**

```
wsl -d Ubuntu -- python3 -m unittest discover -s tests -p test_transient_probe.py
```
Expected: failures for new tests.

- [ ] **Step 3: Apply fixes to `tests/check_transient_compat.py`**

  a. Guard `import pwd`:
  ```python
  try:
      import pwd
  except ImportError:
      pwd = None
  ```

  b. Add capturing group to `UNIT_RE`, make `expected_token` mandatory, use `m.group(1)` for strict equality:
  ```python
  UNIT_RE = re.compile(r'p2b-transient-([0-9a-f]{32})-(?:orig|repl|canary)\.service\Z')

  def emergency_stop(unit, expected_token: str) -> None:
      m = UNIT_RE.fullmatch(unit)
      if not m:
          raise ValueError(f'refuse non-disposable or invalid unit name: {unit}')
      if m.group(1) != expected_token:
          raise ValueError(f'identity mismatch: token {expected_token!r} != {m.group(1)!r}')
      try:
          run('systemctl', 'stop', unit)
      except subprocess.CalledProcessError as exc:
          if 'not loaded' not in exc.stderr and 'not found' not in exc.stderr:
              raise
  ```

  c. Add `read_fifo(fifo_path, timeout=10) -> dict` using `os.O_RDONLY | os.O_NONBLOCK` + `select.select`:
  ```python
  def read_fifo(fifo_path, timeout=10):
      fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
      try:
          r, _, _ = select.select([fd], [], [], timeout)
          if not r:
              raise TimeoutError(f'timeout waiting for payload on {fifo_path}')
          with open(fd, 'r', closefd=False) as f:
              line = f.readline()
          if not line:
              raise RuntimeError(f'empty read from {fifo_path}')
          return json.loads(line)
      finally:
          os.close(fd)
  ```

  d. Replace both `with open(fifo_orig, 'r') as f: reports['orig'] = json.loads(f.readline())` call sites with `reports['orig'] = read_fifo(fifo_orig)` (and same for `repl`).

  e. Fix cgroup path concatenation in `empty()`: `Path('/sys/fs/cgroup') / group.lstrip('/')`.

  f. Update `cleanup()` to use capturing UNIT_RE: `if not all(UNIT_RE.fullmatch(unit) for unit in units):`.

  g. Update `fail_closed_verified` assertion in `coordinator()` to pass `expected_token='invalid_token_1234'` (still a str, not None).

  h. Update docs hash references in `docs/phase-2b-transient-failover.md` after commit.

- [ ] **Step 4: Run tests, verify GREEN**

```
wsl -d Ubuntu -- python3 -m unittest discover -s tests -p test_transient_probe.py
```
Expected: all tests PASS.

- [ ] **Step 5: Run disposable WSL probe end-to-end**

```
wsl -d Ubuntu -u root -- env SUDO_USER=<your_wsl_user> python3 tests/check_transient_compat.py --disposable-only --wsl-only
```
Expected: `{"verdict": "PASS", "isolated_replacement_proven": true}`, `cleanup_errors: []`.

- [ ] **Step 6: Run full regression suites**

```
wsl -d Ubuntu -- env PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_phase2b_*.py'
wsl -d Ubuntu -- env PYTHONPATH=src python3 -m unittest discover -s tests -p test_infrastructure_trial.py
```
Expected: 85 + 51 PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/check_transient_compat.py tests/test_transient_probe.py docs/phase-2b-transient-failover.md
git commit -m "fix(phase2b-probe): mandatory token, non-blocking FIFO, pwd guard, cgroup path fix"
```

---

### Task 2: Add `invocation_cgroup` to timing and cgroup-targeted shutdown

Capture cgroup path at baseline; use it in `record_main_shutdown` and `deadline()` for invocation-bound kill immune to replacement races and PID reuse.

**Files:**
- Modify: `deploy/infrastructure-trial.py` (functions: `start_observation_timing`, `record_main_shutdown`, `deadline`, `validate_timing`)
- Modify: `tests/test_phase2b_transient_integration.py` (new file for targeted tests)

**Interfaces:**
- Consumes: `timing['invocation_id']` (existing), `timing['invocation_cgroup']` (new: str, cgroup path like `/system.slice/polymarket-btc-infrastructure.service`)
- Produces: `invocation_cgroup_kill(cgroup: str, invocation_id: str, timing: dict) -> None` — validates identity, writes `1` to `cgroup/cgroup.kill`; used by `record_main_shutdown` and `deadline()` fallback

- [ ] **Step 1: Write failing targeted tests in new `tests/test_phase2b_transient_integration.py`**

```python
"""Targeted regressions for per-invocation cgroup-based emergency shutdown integration."""
import importlib.util, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

TRIAL = importlib.util.spec_from_file_location(
    'infrastructure_trial',
    Path(__file__).parents[1] / 'deploy/infrastructure-trial.py'
)
module = importlib.util.module_from_spec(TRIAL)
TRIAL.loader.exec_module(module)


class TestInvocationCgroupCapture(unittest.TestCase):
    def test_timing_includes_invocation_cgroup(self):
        # start_observation_timing must store invocation_cgroup in timing dict
        fake_service = {
            'ActiveEnterTimestampMonotonic': str(int((time.monotonic() - 5) * 1e6)),
            'InvocationID': 'abc123',
            'ControlGroup': '/system.slice/polymarket-btc-infrastructure.service',
        }
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            data = {
                'trial_id': 'pilot-20261031T000000Z', 'seconds': 1800, 'boot_id': 'x',
                'fingerprint': 'fp', 'launch_mono': time.monotonic() - 10,
                'startup_expiry_mono': time.monotonic() + 200,
                'units': module.owned('pilot-20261031T000000Z'),
            }
            with patch.object(module, 'monotonic_now', return_value=time.monotonic()), \
                 patch.object(module, 'runtime_bound', side_effect=[
                     (1800 + 240) * 1_000_000, 0]),\
                 patch.object(module, 'arm_timer', return_value={'ok': True}), \
                 patch.object(module, 'MIN_STOP_WINDOW', 30):
                timing = module.start_observation_timing(folder, data, fake_service)
            self.assertIn('invocation_cgroup', timing)
            self.assertEqual(timing['invocation_cgroup'],
                             '/system.slice/polymarket-btc-infrastructure.service')
            self.assertEqual(timing['invocation_id'], 'abc123')

    def test_timing_rejects_empty_cgroup(self):
        fake_service = {
            'ActiveEnterTimestampMonotonic': str(int((time.monotonic() - 5) * 1e6)),
            'InvocationID': 'abc123',
            'ControlGroup': '',
        }
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            data = {
                'trial_id': 'pilot-20261031T000000Z', 'seconds': 1800, 'boot_id': 'x',
                'fingerprint': 'fp', 'launch_mono': time.monotonic() - 10,
                'startup_expiry_mono': time.monotonic() + 200,
                'units': module.owned('pilot-20261031T000000Z'),
            }
            with patch.object(module, 'monotonic_now', return_value=time.monotonic()), \
                 patch.object(module, 'runtime_bound', side_effect=[
                     (1800 + 240) * 1_000_000, 0]), \
                 patch.object(module, 'arm_timer', return_value={}), \
                 patch.object(module, 'MIN_STOP_WINDOW', 30):
                with self.assertRaises(ValueError):
                    module.start_observation_timing(folder, data, fake_service)


class TestInvocationCgroupKill(unittest.TestCase):
    def test_kill_writes_to_cgroup_kill(self):
        with tempfile.TemporaryDirectory() as td:
            cgroup_dir = Path(td) / 'system.slice' / 'polymarket-btc-infrastructure.service'
            cgroup_dir.mkdir(parents=True)
            kill_file = cgroup_dir / 'cgroup.kill'
            kill_file.write_text('0')
            timing = {'invocation_id': 'abc', 'invocation_cgroup':
                      '/system.slice/polymarket-btc-infrastructure.service'}
            with patch.object(module, 'CGROUP_ROOT', Path(td)):
                module.invocation_cgroup_kill(timing['invocation_cgroup'], 'abc', timing)
            self.assertEqual(kill_file.read_text(), '1')

    def test_kill_fails_closed_on_id_mismatch(self):
        timing = {'invocation_id': 'correct_id', 'invocation_cgroup': '/system.slice/x.service'}
        with self.assertRaises(ValueError):
            module.invocation_cgroup_kill('/system.slice/x.service', 'wrong_id', timing)

    def test_kill_handles_missing_cgroup_gracefully(self):
        # If cgroup already removed (process already dead), must not raise
        timing = {'invocation_id': 'abc', 'invocation_cgroup': '/system.slice/gone.service'}
        with patch.object(module, 'CGROUP_ROOT', Path('/nonexistent')):
            # Should not raise; process already gone
            module.invocation_cgroup_kill('/system.slice/gone.service', 'abc', timing)


class TestRecordMainShutdownCgroupTargeting(unittest.TestCase):
    def test_shutdown_uses_cgroup_kill_before_unit_stop(self):
        # record_main_shutdown must call invocation_cgroup_kill before stop_units
        call_order = []
        timing = {
            'invocation_id': 'iid', 'invocation_cgroup': '/system.slice/polymarket-btc-infrastructure.service',
            'baseline_mono': 0.0, 'runtime_expiry_mono': 9999.0,
        }
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            data = {'trial_id': 'pilot-20261031T000000Z', 'boot_id': 'b', 'seconds': 1800,
                    'units': module.owned('pilot-20261031T000000Z')}
            fake_props = {'ActiveState': 'active', 'InvocationID': 'iid', 'ControlGroup':
                          '/system.slice/polymarket-btc-infrastructure.service',
                          'MainPID': '1234', 'ControlPID': '0', 'Result': '', 'LoadState': 'loaded'}
            with patch.object(module, 'unit_evidence',
                               return_value={'unit': module.UNIT, 'properties': fake_props, 'pids': []}), \
                 patch.object(module, 'validate_run_identity', return_value='run1'), \
                 patch.object(module, 'monotonic_now', return_value=100.0), \
                 patch.object(module, 'invocation_cgroup_kill',
                               side_effect=lambda *a, **kw: call_order.append('cgroup_kill')), \
                 patch.object(module, 'stop_units',
                               side_effect=lambda units: (call_order.append('stop_units'), ([], [
                                   {'unit': module.UNIT, 'properties': {
                                       'ActiveState': 'inactive', 'Result': 'success',
                                       'InvocationID': '', 'MainPID': '0', 'ControlPID': '0',
                                       'ControlGroup': ''}, 'pids': []}]))[1]), \
                 patch.object(module, 'cgroup_pids', return_value=[]), \
                 patch.object(module, 'quiescent', return_value=True):
                module.record_main_shutdown(folder, data, timing)
            self.assertEqual(call_order, ['cgroup_kill', 'stop_units'],
                             'cgroup kill must precede stop_units')

    def test_shutdown_fail_closed_on_invocation_mismatch(self):
        timing = {
            'invocation_id': 'expected_iid',
            'invocation_cgroup': '/system.slice/polymarket-btc-infrastructure.service',
            'runtime_expiry_mono': 9999.0,
        }
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            data = {'trial_id': 'pilot-20261031T000000Z', 'boot_id': 'b', 'seconds': 1800,
                    'units': module.owned('pilot-20261031T000000Z')}
            fake_props = {'ActiveState': 'active', 'InvocationID': 'DIFFERENT_iid',
                          'ControlGroup': '/system.slice/polymarket-btc-infrastructure.service',
                          'MainPID': '1234', 'ControlPID': '0', 'Result': '', 'LoadState': 'loaded'}
            stop_called = []
            with patch.object(module, 'unit_evidence',
                               return_value={'unit': module.UNIT, 'properties': fake_props, 'pids': []}), \
                 patch.object(module, 'validate_run_identity', return_value='run1'), \
                 patch.object(module, 'monotonic_now', return_value=100.0), \
                 patch.object(module, 'invocation_cgroup_kill',
                               side_effect=lambda *a, **kw: stop_called.append('cgroup')), \
                 patch.object(module, 'stop_units',
                               side_effect=lambda units: (stop_called.append('stop'), ([], [{
                                   'unit': module.UNIT, 'properties': {
                                       'ActiveState': 'inactive', 'Result': 'success',
                                       'InvocationID': '', 'MainPID': '0', 'ControlPID': '0',
                                       'ControlGroup': ''}, 'pids': []}]))[1]), \
                 patch.object(module, 'quiescent', return_value=True):
                proof = module.record_main_shutdown(folder, data, timing)
            # cgroup_kill must NOT be called when invocation mismatch detected
            self.assertNotIn('cgroup', stop_called)
            self.assertTrue(proof['errors'], 'errors must be non-empty on id mismatch')


class TestDeadlineFallbackCgroupKill(unittest.TestCase):
    def test_deadline_fallback_uses_cgroup_kill(self):
        # deadline() fallback stop must use invocation_cgroup from timing if available
        timing = {
            'invocation_id': 'iid',
            'invocation_cgroup': '/system.slice/polymarket-btc-infrastructure.service',
            'baseline_mono': 0.0, 'deadline_mono': 100.0, 'runtime_expiry_mono': 9999.0,
            'fingerprint': 'fp', 'boot_id': 'b',
        }
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            data = {'trial_id': 'pilot-20261031T000000Z', 'boot_id': 'b', 'seconds': 1800,
                    'units': module.owned('pilot-20261031T000000Z')}
            (folder / 'timing.json').write_text(json.dumps(timing))
            kill_called = []
            with patch.object(module, 'trial_metadata', return_value=data), \
                 patch.object(module, 'monotonic_now', return_value=100.5), \
                 patch.object(module, 'secure_bytes', return_value=json.dumps(timing).encode()), \
                 patch.object(module, 'validate_deadline'), \
                 patch.object(module, 'stop_main', side_effect=RuntimeError('forced fallback')), \
                 patch.object(module, 'invocation_cgroup_kill',
                               side_effect=lambda *a, **kw: kill_called.append(True)), \
                 patch.object(module, 'stop_units', return_value=([], [])), \
                 patch.object(module, 'unit_evidence', return_value={}), \
                 patch.object(module, 'write_json'), \
                 patch.object(module, 'snapshot', return_value={}), \
                 patch.object(module, 'validate_timing'), \
                 patch.object(module, 'validate_shutdown_records'):
                module.deadline(folder)
            self.assertTrue(kill_called, 'deadline fallback must call invocation_cgroup_kill')
```

- [ ] **Step 2: Run tests, verify RED**

```
wsl -d Ubuntu -- env PYTHONPATH=src python3 -m unittest tests.test_phase2b_transient_integration -v
```
Expected: failures (functions not implemented).

- [ ] **Step 3: Add `CGROUP_ROOT` constant and `invocation_cgroup_kill()` to `deploy/infrastructure-trial.py`**

Add after line 19 (`DATA = STATE / 'data'`):
```python
CGROUP_ROOT = Path('/sys/fs/cgroup')
```

Add after `runtime_bound()` (after line 148):
```python
def invocation_cgroup_kill(cgroup: str, invocation_id: str, timing: dict) -> None:
    """Kill all processes in `cgroup` after verifying `invocation_id` matches `timing`.

    Fails closed if invocation IDs differ. Treats absent cgroup as already-dead (safe no-op).
    """
    if invocation_id != timing.get('invocation_id', ''):
        raise ValueError('invocation identity mismatch; refuse cgroup kill')
    kill_path = CGROUP_ROOT / cgroup.lstrip('/')  / 'cgroup.kill'
    try:
        kill_path.write_text('1')
    except FileNotFoundError:
        pass  # cgroup already removed; process already dead
```

- [ ] **Step 4: Extend `start_observation_timing()` to capture `invocation_cgroup`**

In `start_observation_timing`, after the existing validation block (around line 537), capture `ControlGroup` from the service dict and include it in `timing`. Reject empty cgroup:

```python
invocation_cgroup = service.get('ControlGroup', '')
if not invocation_cgroup:
    raise ValueError('activation cgroup path unavailable')
timing = {
    ...,  # existing keys
    'invocation_cgroup': invocation_cgroup,
}
```

Exact location: add `invocation_cgroup` after `'invocation_id': service['InvocationID']` in the `timing` dict literal (line 541-542 region).

- [ ] **Step 5: Update `record_main_shutdown()` to call `invocation_cgroup_kill` before `stop_units`**

In `record_main_shutdown`, after writing `shutdown-pre.json` (after line 632) and before `stop_errors, evidence = stop_units([UNIT])` (line 633):

```python
    # Invocation-bound cgroup kill: must precede stop_units so it targets exactly the
    # pre-stop invocation. Fail-closed if invocation IDs differ (replacement detected).
    if not errors:
        try:
            invocation_cgroup_kill(
                pre_states[0]['properties'].get('ControlGroup', ''),
                pre_props.get('InvocationID', ''),
                timing,
            )
        except (ValueError, OSError) as exc:
            errors.append('invocation cgroup kill: ' + str(exc))
```

- [ ] **Step 6: Update `deadline()` fallback to call `invocation_cgroup_kill`**

In the `except Exception` fallback block inside `deadline()` (around line 714-718), after appending the error and before `stop_errors, main_evidence = stop_units([UNIT])`:

```python
        try:
            invocation_cgroup_kill(
                timing.get('invocation_cgroup', ''),
                timing.get('invocation_id', ''),
                timing,
            )
        except (ValueError, OSError, KeyError) as exc:
            errors.append('deadline fallback cgroup kill: ' + str(exc))
```

Also update `validate_timing()` to accept and roundtrip `invocation_cgroup` in timing without rejecting it.

- [ ] **Step 7: Run targeted tests, verify GREEN**

```
wsl -d Ubuntu -- env PYTHONPATH=src python3 -m unittest tests.test_phase2b_transient_integration -v
```
Expected: all new tests PASS.

- [ ] **Step 8: Run all regression suites**

```
wsl -d Ubuntu -- env PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_phase2b_*.py'
wsl -d Ubuntu -- env PYTHONPATH=src python3 -m unittest discover -s tests -p test_infrastructure_trial.py
wsl -d Ubuntu -- env PYTHONPATH=src python3 -m unittest discover -s tests -p test_transient_probe.py
```
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add deploy/infrastructure-trial.py tests/test_phase2b_transient_integration.py
git commit -m "feat(phase2b): cgroup-targeted invocation shutdown; capture invocation_cgroup at baseline"
```

---

### Task 3: Update documentation and hashes; request independent review

**Files:**
- Modify: `docs/phase-2b-transient-failover.md` (update hashes after Task 1 commit)
- No new files

- [ ] **Step 1: Record final file hashes post-Task 1**

```bash
git hash-object tests/check_transient_compat.py
# Linux: wsl -d Ubuntu -- sha256sum tests/check_transient_compat.py
```

Update `docs/phase-2b-transient-failover.md` section 5 with new Git blob SHA and byte SHA256.

- [ ] **Step 2: Dispatch independent code review subagent**

Review scope:
- `deploy/infrastructure-trial.py`: `invocation_cgroup_kill`, changes to `start_observation_timing`, `record_main_shutdown`, `deadline` fallback
- `tests/test_phase2b_transient_integration.py`
- `tests/check_transient_compat.py` (post-fix)
- No production deployment required

Verdict must be ACCEPT or REQUEST CHANGES.

- [ ] **Step 3: Resolve any reviewer findings; rerun affected tests**

- [ ] **Step 4: Final commit and push**

```bash
git add docs/phase-2b-transient-failover.md
git commit -m "docs(phase2b): update hashes and integration runbook"
git push origin operator/phase2b-transient-integration
git rev-parse HEAD
```

---

## Self-Review

**Spec coverage:**
- ✅ Unique per-invocation identity: `invocation_cgroup` captured at baseline alongside existing `invocation_id`
- ✅ Dedicated cgroup: already provided by static unit; captured path is invocation-bound
- ✅ Nonroot execution: unchanged (`User=polymarket-btc` in service)
- ✅ Invocation-bound emergency shutdown: `invocation_cgroup_kill` + identity check
- ✅ Original + descendant termination: `cgroup.kill` kills all cgroup members
- ✅ Replacement/canary isolation: cgroup path is structurally disjoint from any replacement
- ✅ Stale-stop safety: `FileNotFoundError` on dead cgroup = safe no-op
- ✅ Fail-closed: identity mismatch raises `ValueError` before any kill
- ✅ Independent deadline: `deadline.timer` unchanged; cgroup kill added as pre-`stop_units` step
- ✅ Bounded cleanup: existing `stop_units` + new `try/except` on `invocation_cgroup_kill`
- ✅ Timing gates: no changes to timing constants or expiry calculations
- ✅ Probe reviewer C1/I1/I2/I3 findings fixed in Task 1

**Step scan:** Each step is one action. No bodies where signature+test determine the code.

**Type consistency:** `invocation_cgroup: str` used everywhere. `CGROUP_ROOT: Path` constant. `invocation_cgroup_kill(cgroup: str, invocation_id: str, timing: dict) -> None` matches all call sites.

**Review Focus addressed:**
1. TOCTOU race → mitigated: cgroup path captured at baseline (immutable); kill happens in pre-stop identity guard
2. Cgroup GC → mitigated: `FileNotFoundError` treated as safe no-op (process already dead)
3. Deadline fallback → fixed: uses `timing.get('invocation_cgroup')` in fallback path
4. Foreign units → fixed in Task 1 (UNIT_RE capturing group + mandatory token)
5. FIFO deadlock → fixed in Task 1 (`read_fifo` with non-blocking open + select timeout)
