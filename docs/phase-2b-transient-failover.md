# Phase 2B — Transient Service Failover Resolution

## 1. Selected Architecture and Why

**Candidate Selected: Uniquely named, per-invocation systemd transient service (Candidate A).**

### Comparison with Narrowly Scoped AppArmor (Candidate B):
- **Candidate B (AppArmor / User Namespaces)**: Under Ubuntu 24.04 with `kernel.apparmor_restrict_unprivileged_userns=1`, unprivileged user and PID namespace creation was blocked (`EPERM`, exit code 2). Enabling it requires adding custom AppArmor profiles to `/etc/apparmor.d/`, altering sysctls, or modifying system security policies. This violates hard constraints: no global AppArmor changes, no sysctl alterations, no packages, and no root bypass.
- **Candidate A (Per-Invocation Transient Service)**: Leverages native systemd 255 and cgroup v2 features already fully operational on Ubuntu 24.04 VPS (`systemd-run --unit=...`). Every service invocation receives a cryptographically unique unit name (e.g. `polymarket-btc-infra-<trial_id>-<uuid>.service` or template instance `polymarket-btc-infra@<trial_id>-<uuid>.service`).
  - Systemd allocates an exclusive, dedicated cgroup `/sys/fs/cgroup/system.slice/<unit-name>`.
  - Emergency shutdown targets ONLY that specific unit name.
  - A newer replacement invocation is spawned under a fresh, distinct unit name with its own separate cgroup.
  - Requires zero systemd upgrades, zero AppArmor changes, zero sysctl edits, and zero package installations.

---

## 2. Safety and Replacement-Race Evidence

The disposable validation probe (`tests/check_transient_compat.py`) was executed under WSL with systemd and cgroup v2, producing verifiable evidence:

1. **Cgroup Disjointness**:
   - `orig` unit cgroup: `/system.slice/p2b-transient-5227fc1ccb83426bb14f8bdd3cbac474-orig.service`
   - `repl` unit cgroup: `/system.slice/p2b-transient-5227fc1ccb83426bb14f8bdd3cbac474-repl.service`
   - Kernel cgroup paths are strictly disjoint.
2. **Descendant Cleanup**:
   - In `orig`, both main process (PID 4114) and forked child process (PID 4115) were running in the unit cgroup.
   - Emergency stop of `orig` terminated both the main process and all descendants (`KillMode=control-group`). Both `/proc/4114` and `/proc/4115` ceased to exist (`orig_descendants_killed: true`).
3. **Replacement Isolation**:
   - During and after the emergency shutdown of `orig`, the replacement invocation (`repl`, PID 4119 and child PID 4120) remained active, unpaused, and completely unaffected (`replacement_survived: true`, `replacement_descendants_intact: true`).
4. **Stale Stop Safety**:
   - Calling emergency stop a second time on the already-dead `orig` unit was a safe no-op. It did not touch `repl` (`stale_stop_safe: true`).
5. **PID Reuse Immunity**:
   - Systemd cgroup v2 signal delivery operates via kernel cgroup interfaces (`cgroup.kill` or cgroup membership), not global PID scanning. If an exited PID is recycled on the host, the recycled process resides in a different cgroup and cannot be signaled.
6. **Canary / Unrelated Service Isolation**:
   - The unrelated canary service remained active with unchanged PID throughout (`canary_survived: true`).
7. **Fail-Closed Identity Verification**:
   - Calling `emergency_stop` with an unverified or mismatched token immediately raised `ValueError` without issuing any systemd command (`fail_closed_verified: true`).
8. **Deadline & Backstop Preservation**:
   - The independent B+1860 monotonic timer (`deadline.timer` / `deadline.service`) remains decoupled.
   - In addition, each transient unit enforces `RuntimeMaxSec` directly in systemd as an un-bypassable fallback backstop.

---

## 3. Cross-Platform SHA256 Acceptance Resolution

### Root Cause:
On Windows machines, Git default configuration (`core.autocrlf=true`) converted line endings from LF (`\n`) to CRLF (`\r\n`) in the working copy. Consequently:
- `sha256sum` on Windows working copy calculated hash across CRLF bytes.
- `sha256sum` on Linux / Ubuntu VPS calculated hash across LF bytes.

### Resolution:
1. **`.gitattributes` added**:
   Enforces `eol=lf` across all platforms for `*.py`, `*.sh`, and `*.md`, preventing line-ending divergence.
2. **Canonical Git Identity**:
   Authoritative identity is the Git commit SHA (`git rev-parse HEAD`), which guarantees tree and blob parity regardless of host checkout settings.
   The canonical Git blob object SHA is verified via `git hash-object`.
3. **Documented Hashes**:
   - Exact commit SHA: `0c8ebd2...` / published branch SHA
   - Git object blob SHA: `git hash-object tests/check_transient_compat.py`
   - Linux native LF byte SHA256: `sha256sum tests/check_transient_compat.py`

---

## 4. Exact Hermes Disposable Test Instructions

Run from an isolated operator checkout on the Ubuntu VPS (never `/opt` production release):

```bash
# 1. Fetch and checkout verification branch
git fetch origin
git checkout operator/phase2b-transient-failover
git rev-parse HEAD

# 2. Verify file integrity (both canonical Git blob SHA and Linux LF byte SHA)
git hash-object tests/check_transient_compat.py
sha256sum tests/check_transient_compat.py

# 3. Run offline safety regressions (no systemd commands)
python3 -m unittest discover -s tests -p test_transient_probe.py

# 4. Execute disposable compatibility probe as root with explicit acknowledgement
sudo /usr/bin/python3 -I -B tests/check_transient_compat.py --disposable-only
```

### Acceptance Checks for Hermes:
- Script must output `{"verdict": "PASS", "isolated_replacement_proven": true}` with exit code `0`.
- Verify `orig_descendants_killed: true` and `replacement_survived: true`.
- Verify `stale_stop_safe: true` and `canary_survived: true`.
- Verify `fail_closed_verified: true`.
- Verify `cleanup_errors: []`.
- Production service (`polymarket-btc-infrastructure.service`) is never touched.

---

## 5. Remaining Risks and Production Path

1. **Production Controller Adoption**:
   The integration branch `operator/phase2b-transient-integration` (commit `3a0b12b6b1ec1bb8a954d2ccb97895d3582ba13a`) implements `invocation_cgroup_kill` into `deploy/infrastructure-trial.py`. Any VPS deployment requires a new release fingerprint and fresh normal-pilot acceptance before any observer-failure trial.
2. **Release Fingerprint**:
   Changes to `deploy/infrastructure-trial.py` change the controller fingerprint. Existing approvals reference prior fingerprints and will not validate against this integration.
3. **Transient Unit GC**:
   With `RemainAfterExit=yes`, transient units stay in memory until explicitly reset. Post-trial cleanup must run `systemctl reset-failed <unit>` to prevent systemd manager memory leakage over repeated soaks.

---

## 6. Integration File Hashes (Post-Integration Branch)

**Branch:** `operator/phase2b-transient-integration`
**Integration commit:** `3a0b12b6b1ec1bb8a954d2ccb97895d3582ba13a`

**Probe (`tests/check_transient_compat.py`) post-fix hashes:**
- Git blob SHA: `d79e66439d88c6ba9fb75908271d16dbe7f2bcf0`
- Byte SHA256 (Linux LF, identical on Windows after `.gitattributes`): `b613facc3aab50bcc24624a086f0d5b037a5844a1d2f77f46736f46bb533e859`

**Files changed by integration (branch diff vs `a894fbb`):**
- `deploy/infrastructure-trial.py`: `CGROUP_ROOT`, `invocation_cgroup_kill`, `invocation_cgroup` in `start_observation_timing`, cgroup kill before `stop_units` in `record_main_shutdown`, cgroup kill in `deadline()` fallback
- `tests/test_phase2b_transient_integration.py`: 10 targeted regressions for cgroup kill identity, ordering, fallback, and fail-closed
- `tests/check_transient_compat.py`: mandatory token (capturing UNIT_RE), non-blocking FIFO `read_fifo`, `pwd` import guard, cgroup path `lstrip`
- `tests/test_transient_probe.py`: 4 new offline regressions for token strictness, FIFO timeout, pwd guard
- `tests/test_phase2b_timing.py`: `ControlGroup` added to service fixtures
