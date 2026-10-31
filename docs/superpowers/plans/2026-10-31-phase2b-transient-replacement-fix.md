# Phase 2B — Per-Invocation Transient Service Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the replacement-race blocker by converting the main ingest service to a uniquely named, per-invocation systemd template instance (`polymarket-btc-infrastructure@<trial_id>-<role>-<token>.service`), giving every invocation an exclusive cgroup, binding all shutdown paths exclusively to the owned invocation unit, and ensuring missing/corrupted timing fails closed without broad unit stops.

**Architecture:** 
1. Service definition is installed as a template unit `/etc/systemd/system/polymarket-btc-infrastructure@.service`.
2. Every invocation gets a cryptographically unique unit name:
   - Verification invocation (first start): `polymarket-btc-infrastructure@<trial_id>-verify-<token>.service`
   - Continuous invocation (second start): `polymarket-btc-infrastructure@<trial_id>-main-<token>.service`
3. Each invocation is allocated an exclusive, dedicated cgroup by systemd (`/sys/fs/cgroup/system.slice/system-polymarket\x2dbtc\x2dinfrastructure.slice/polymarket-btc-infrastructure@...`).
4. Shutdown paths (`record_main_shutdown`, `deadline`, `startup_timeout`, `observe` finally, `launch` error) target ONLY the specific owned invocation unit name. No broad `stop_units([UNIT])` or shared static unit stops.
5. Missing or corrupted timing in `deadline()` fails closed: records error in `deadline-stop.json` and refuses untargeted unit stops.
6. A newer replacement invocation running under a fresh unit name has a distinct cgroup and distinct unit name: any stale stop, stale `cgroup.kill`, or observer failure targeting the original invocation is physically isolated and cannot touch the replacement.

**Tech Stack:** Python 3.12 stdlib, systemd 255 (`systemctl`, `busctl`, `systemd-analyze`), cgroup v2, Linux pidfd.

**Spec:** Prompt requirements and `docs/phase-2b-transient-failover.md`.

## Global Constraints
- NO VPS production operations or deployment.
- NO real-money trading; `MODE=PAPER_ONLY`.
- NO weakening AppArmor or security settings.
- NO PID-namespace approach.
- Preserve original 8 pilot gates and timing contract: B+1800 observation, B+1860 deadline, B+1890 latest entry, A+2040 runtime fallback, T+2280 heartbeat, T+2640 audit.
- Preserve all existing historical evidence.
- No unrelated refactoring.

## Review Focus
1. Replacement invocation survival when original invocation undergoes emergency shutdown or stale shutdown.
2. Missing/corrupted `timing.json` in `deadline()` must never stop any static unit or replacement; must fail closed.
3. Descendants of the original invocation must be terminated by `cgroup.kill` and `KillMode=control-group`.
4. Stale stop called twice on the original unit must be a safe no-op and never touch a replacement.
5. Inspection-to-shutdown identity changes (TOCTOU) must fail closed without stopping the replacement.

---

### Task 1: Unit Template and Installer Adaptation

**Files:**
- Create: `deploy/polymarket-btc-infrastructure@.service` (copied from `deploy/polymarket-btc-infrastructure.service` with description `%I`)
- Modify: `deploy/install-infrastructure.sh`
- Modify: `deploy/infrastructure-trial.py` (`TEMPLATE_UNIT`, `INSPECTION_UNIT`, `runtime_identity`, `require_service_policy`, `runtime_bound`)
- Test: `tests/test_phase2b_transient_integration.py`

**Interfaces:**
- Consumes: `deploy/polymarket-btc-infrastructure@.service`
- Produces: `TEMPLATE_UNIT = 'polymarket-btc-infrastructure@.service'`, `INSPECTION_UNIT = 'polymarket-btc-infrastructure@default.service'`, `invocation_unit(trial_id: str, role: str) -> str` returning `f'polymarket-btc-infrastructure@{trial_id}-{role}-{token}.service'`.

- [ ] **Step 1: Write failing tests in `tests/test_phase2b_transient_integration.py` for `invocation_unit` and template naming**
- [ ] **Step 2: Run tests, verify RED**
- [ ] **Step 3: Create `deploy/polymarket-btc-infrastructure@.service` and update `deploy/install-infrastructure.sh`**
- [ ] **Step 4: Update `deploy/infrastructure-trial.py` constants and policy/runtime_bound inspection functions**
- [ ] **Step 5: Run tests, verify GREEN**
- [ ] **Step 6: Commit**

### Task 2: Controller Invocations and Targeted Shutdown

**Files:**
- Modify: `deploy/infrastructure-trial.py` (`observe`, `stop_main`, `record_main_shutdown`, `deadline`, `startup_timeout`, `audit`, `inspect_cleanup`, `validate_timing`, `validate_helper_exits`)
- Test: `tests/test_phase2b_transient_integration.py`

**Interfaces:**
- Consumes: `invocation_unit(trial_id, role)`
- Produces: All lifecycle and shutdown paths bound strictly to invocation-specific unit names; fail-closed deadline on missing/corrupt timing.

- [ ] **Step 1: Write failing tests in `tests/test_phase2b_transient_integration.py` for:
  - Distinct unit and cgroup names for verify vs main
  - Replacement survival when original is stopped
  - Stale stop safety against dead original
  - Observer-death deadline targeting only original
  - Missing/corrupted timing in deadline failing closed without stopping any unit
  - Identity change between pre-stop and stop failing closed**
- [ ] **Step 2: Run tests, verify RED**
- [ ] **Step 3: Implement per-invocation unit launching in `observe()` (verify run + continuous run)**
- [ ] **Step 4: Update `record_main_shutdown()` to stop only `timing['unit']` and fail closed if missing**
- [ ] **Step 5: Update `deadline()` to stop only `timing['unit']` and fail closed on missing timing without calling `stop_units`**
- [ ] **Step 6: Update `startup_timeout()` and `launch()` error cleanup to stop only trial-owned helper units**
- [ ] **Step 7: Update `audit()` and `inspect_cleanup()` to target only owned units**
- [ ] **Step 8: Run targeted tests, verify GREEN**
- [ ] **Step 9: Run full regression suites (phase2b, infrastructure trial, probe)**
- [ ] **Step 10: Commit**

### Task 3: Disposable WSL Integration Verification & Superpowers Review

**Files:**
- Modify: `tests/check_transient_compat.py` (if needed for template validation)
- Modify: `docs/phase-2b-transient-failover.md`

- [ ] **Step 1: Run disposable WSL integration test demonstrating replacement survival and descendant kill**
- [ ] **Step 2: Update documentation and recorded hashes**
- [ ] **Step 3: Dispatch independent Superpowers code review subagent**
- [ ] **Step 4: Resolve any reviewer findings; rerun affected tests**
- [ ] **Step 5: Push branch `operator/phase2b-transient-integration` and report final verdict**
