# Final Soak Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Track steps here.

**Goal:** Repair native ingest-only trial provenance and independent cleanup without accepting Phase 2B.
**Architecture:** Preserve legacy paper observer. Native infrastructure controller uses stdlib SQLite backups, exact artifact manifests, phase-specific approval fingerprints and explicit trial-owned systemd units. Installer builds isolated immutable release from Git objects, never local working bytes.
**Tech Stack:** Python >=3.12 stdlib, Bash, systemd, existing project dependencies.
**Spec:** User's PHASE 2B FINAL SOAK REMEDIATION request; existing eight gates in docs/phase-2b-infrastructure-acceptance.md.

## Global Constraints
No VPS access. No strategy/risk/trading/wallet changes. Preserve historical evidence and existing untracked work. No merge from feat/strategy-learning, no force-push. Only linux-mvp publication after tests and independent review. Do not approve historical or fresh trial automatically.

## Review Focus
- Invalid/missing approval must fail before STOP/config/service mutation.
- Partial launch, observer failure, failed stop, active/exited unit and child cgroup processes.
- Installed shell helpers, extra Python files, drop-ins, environment and runtime edits must change fingerprint or fail manifest validation.
- Historical SQLite runs must never count as fresh ingestion; WAL backups must remain consistent.
- UTC/WIB date rollover and launch/setup/service start timestamps must stay distinct.

## Investigation ledger
- Base HEAD f8ccedc5fe8d0b7465cb65aa87ee0e15b0737f4a; linked worktree linux-mvp. Existing untracked helpers left untouched.
- Deadline shell stops only main service; semicolon hides stop error behind final show; no helper/timer/cgroup proof.
- Heartbeat moves temporary file then cats removed path under set -e, exiting after first sample.
- Native runner creates observer output directory that legacy observer refuses; invokes legacy paper paths/Windows host schema and omits soak approval.
- Hash covers Python subset and paper config, excluding runner/heartbeat shell and installed extras. Installer copies working tree recursively and editable-installs source checkout.
- No local 20261026 trial evidence or Hermes transport exposed via MCP. Historical exact runtime delta cannot be inferred; read-only Hermes export required.
- Initial Windows baseline invocation failed import discovery and bash Windows paths. Correct baseline: local Linux, PYTHONPATH=src.
- Ruling: User explicitly requests no questions and execution; proceed inline, use phase-specific exact fingerprints (no normalization). Cost: new approval for pilot-to-soak duration change.
- Ruling: Existing paper deployment shares /opt/polymarket-btc. Use separate versioned infrastructure release; never overwrite shared files. Cost: new reviewed infrastructure unit/config path.

## Task 1: Regression reproduction and native evidence primitives
Files: tests/test_infrastructure_trial.py, deploy/infrastructure-trial.py, deploy/infrastructure-host-heartbeat.sh.
- [x] Write and run failing behavioral tests: heartbeat JSONL, native continuity, exact fingerprint rejection, approval-before-side-effects, UTC/WIB rollover, historical-run exclusion, safe WAL snapshots, cleanup process detection.
- [x] Implement stdlib native controller; keep paper observer unchanged.
- [x] Run targeted tests green.

## Task 2: Deployment and detached lifecycle
Files: deploy/install-infrastructure.sh, deploy/infrastructure-runner.sh, deploy/polymarket-btc-infrastructure.service.
- [x] Write/run failing installer and lifecycle tests with controlled systemd boundary.
- [x] Implement Git-object-only isolated releases, hash verification and external approval prerequisite; independent deadline targets exact owned units; audit plus read-only post-exit inspection records all units/cgroups. No real install performed.
- [x] Run targeted tests green.

## Task 3: Evidence handoff, independent review, publication
Files: docs/phase-2b-final-soak-remediation.md and this ledger.
- [x] Document root causes, read-only Hermes export, exact redeploy, fresh 180s pilot, eight gates and soak conditions.
- [x] Run full suite, Bash checks, local systemd checks when safely available.
  - Fresh targeted run: 72 tests passed (`tests.test_infrastructure_trial`, `tests.test_linux_mvp_infrastructure`, `tests.check_infrastructure_systemd`). Fresh Linux MVP discovery: 159 run, 1 platform skip, no failures. Bash syntax and diff whitespace checks passed. Local user-systemd fault/deadline/production-cleanup check passed; not VPS acceptance.
  - Frozen full run preserved: 1267 tests, 4 errors (Windows Job Object tests on Linux), 29 skipped. Five-error disposition documented: 4 Windows Job Object `windows_job_required` errors (pass on Windows); 1 retained DB fixture missing in snapshot (passes with hash-verified fixture `a488583880d10507a24e82251d0870433c1d39160b77429820a2d339a54ca409`). No test assertions failed.
  - Independent static review (ses_e83c53248ffek7a61zgP1Ysmon) returned REQUEST_CHANGES: 2 HIGH (test path mismatch, empty controller-owner file) + 3 MEDIUM (DATA_DIR parent 0755 tightened to 0750 root:polymarket-btc; sed replacement verified for all three unit paths; `--reset-stop` added to observe).
  - Fixes applied and re-reviewed (ses_e83b1d38cffehSkw2Xf6SXapdI): **ACCEPT**. All 72 tests pass, bash syntax OK, Python syntax OK, systemd check PASS.
- [x] Fresh independent review; fix important findings with RED/GREEN tests.
  - Independent CLI review completed BLOCKED: seven Important findings (service-writable evidence ancestry, untrusted approval provenance, release-root import injection, external stdlib outside manifest, paper-owned parent traversal, deadline budget mismatch, incomplete pilot bundle). Static review only; no Critical confirmed. Findings verified against current paths/code before fix pass.
  - [x] Separate root-controlled evidence/lock from service-owned DB/state; isolate infrastructure storage from paper-owned parent.
  - [x] Descriptor-based root-owned/non-writable/symlink-free approval validation.
  - [x] Reject unmanifested root imports and bind separately verified host Python runtime baseline including readable bytecode; narrow OS trust claims explicitly.
  - [x] Persist stop proof before bounded DB subprocess and budget aggregate cleanup (deadline 360s, audit 480s; audit at pilot launch +780s).
  - [x] Require complete pilot inventory and consistent trial/run metadata.
  - Second asynchronous static review overlapped fix pass: noted native `.venv/lib64` symlink, external bytecode, then-outdated tests/docs. Fixed standard lib64 alias with exact manifest target; external readable bytecode now hashed and new files rejected. Real native venv/installer-manifest integration test passed. Latest combined remediation/artifact run 70 passed before this extra integration test. Full final-source run still required; no publication yet.
  - Ruling: Move fresh infrastructure storage to sibling `/var/lib/polymarket-btc-infrastructure`, root-controlled parent with service-owned `data` child — avoids granting paper-directory access or changing paper permissions — cost: explicit reviewed config path migration; historical DB/evidence stay untouched.
- [x] Fresh verification, explicit-path staging, commit/push linux-mvp only; verify remote SHA/main unchanged.
