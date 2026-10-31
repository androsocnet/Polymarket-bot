# Phase 2B — Transient Service Failover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver smallest independently verified disposable solution for per-invocation transient service replacement-race isolation and emergency shutdown on Ubuntu 24.04 / systemd 255.

**Architecture:** Each service invocation receives a cryptographically unique unit name (`p2b-transient-<token>.service`) allocating an invocation-specific cgroup v2. Emergency shutdown targets only that unique unit / cgroup, guaranteeing that replacement invocations and unrelated services are never touched.

**Tech Stack:** Python 3 stdlib, systemd 255 (`systemd-run`, `systemctl`), cgroup v2, Linux pidfd.

**Spec:** Phase 2B VPS Failover Blocker prompt and `docs/phase-2b-watchdog-blocked.md`.

## Global Constraints
- No production deployment, watchdog activation, or live trading.
- No global AppArmor or sysctl modifications.
- No systemd upgrade.
- Support existing Ubuntu 24.04 / systemd 255 environment.
- Fails closed when unit identity is missing or unverified.
- All disposable fixtures must be cleaned up with verified empty cgroups.

---

### Task 1: Safety Regressions & Cross-Platform Line Endings

**Files:**
- Create: `tests/test_transient_probe.py`
- Create: `.gitattributes`
- Test: `tests/test_transient_probe.py`

- [ ] **Step 1: Create `.gitattributes` ensuring LF line endings across platforms**
- [ ] **Step 2: Write failing offline safety tests in `tests/test_transient_probe.py`**
  - Verify probe refuses non-disposable units
  - Verify cleanup handles errors across multiple units
  - Verify missing `--disposable-only` flag exits non-zero without systemd calls
  - Verify fail-closed behavior on unverified identity
- [ ] **Step 3: Run offline tests to verify failure (RED)**
- [ ] **Step 4: Commit baseline test**

### Task 2: Disposable Transient Service Compatibility Probe

**Files:**
- Create: `tests/check_transient_compat.py`
- Modify: `tests/test_transient_probe.py`

- [ ] **Step 1: Implement `tests/check_transient_compat.py`**
  - Staging in `/run/p2b-transient-<token>`
  - Launch `orig` transient service as unprivileged user with full hardening
  - Launch `repl` transient service with unique token and dedicated cgroup
  - Launch `canary` service
  - Test descendant cleanup in `orig`
  - Test `repl` survival and canary survival during `orig` shutdown
  - Test stale stop / stale `cgroup.kill` against `orig` does not harm `repl`
  - Enforce fail-closed identity checks
  - Clean up and verify hierarchical cgroups are empty
- [ ] **Step 2: Run offline safety tests to verify PASS (GREEN)**
- [ ] **Step 3: Run disposable integration test under WSL**
- [ ] **Step 4: Verify pre-existing test suites (85 phase2b tests, 51 infrastructure tests)**

### Task 3: Independent Superpowers Review & Documentation

**Files:**
- Create: `docs/phase-2b-transient-failover.md`

- [ ] **Step 1: Write runbook and technical evidence in `docs/phase-2b-transient-failover.md`**
  - Selected architecture rationale
  - Safety and replacement-race proof
  - Cross-platform SHA256 resolution
  - Exact Hermes instructions
  - Remaining risks
- [ ] **Step 2: Dispatch independent code reviewer subagent**
- [ ] **Step 3: Resolve any reviewer findings**
- [ ] **Step 4: Final verification and publication readiness**
