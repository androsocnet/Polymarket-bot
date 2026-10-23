# Phase 2B Settlement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Native execution authorized by user; no further planning approval requested.

**Goal:** Complete read-only verified settlement, reconciled paper accounting, and gated WSL2 validation.
**Architecture:** Reuse PaperEngine, SQLite transactions, and PublicGet. Verify exact Gamma market/condition/outcome-token mapping against recorded observations/orders and final Data API v2 payouts. Persist evidence and retry state; never infer payouts from quotes or BTC prices.
**Tech Stack:** Existing Python, SQLite, httpx, unittest, systemd; no added dependencies.
**Spec:** User Phase 2B request and GO authorization in current conversation (binding).

## Global Constraints
- PAPER_ONLY; no signing, credentials, real orders, VPS changes, or Git push.
- Existing linux-mvp worktree and accepted CP5/CP6 files remain untouched.
- Native TDD; independent review clears safety/accounting blockers before live execution.
- 60-minute observed integration requires >=3 rollovers, settlement attempts, STOP/restart, reconciliation.
- Separate 24-hour systemd soak starts only after short-run gates pass; no 24-hour blocking session.
- Any suspend, WSL shutdown, reboot, clock jump, or heartbeat gap invalidates uninterrupted-operation claims.

## Review Focus
- Reversed outcome ordering or token identity conflict must never pay wrong side.
- Non-finite/non-unit payout, missing finality, or contradictory sources stay UNKNOWN.
- Crash in migration/settlement must roll back and retain evidence/positions.
- Settlement loss must trigger daily risk gate before next collection/trade.
- Current report must not reuse stale pre-mutation cash/marks; maximum drawdown is historical peak-to-trough.

## Tasks / ledger
- [ ] 1. Reproduce review blockers and replace incomplete verification with exact-identity final evidence validation; preserve raw responses, bounded persistent retries, atomic settlement/recovery.
- [ ] 2. Reconcile every fill/settlement transaction; current cash/basis, sale vs settlement realized P&L, verified position wins/losses, reliable marks/UNKNOWN, historical maximum drawdown, latency evidence.
- [ ] 3. Offline adversarial tests and Phase 1/2 regressions; accepted hashes/unrelated dirty-file fingerprints; independent review and blocker fixes.
- [ ] 4. Durable bounded integration/soak tooling with continuity detection, resource samples, journal and DB backups, automatic stop. Offline test tooling before live use.
- [ ] 5. Run bounded 60-minute WSL2 integration only after offline/review gates; evaluate >=3 rollovers, reconciliation, STOP/restart, resources, integrity.
- [ ] 6. If integration passes, launch separate 24-hour systemd soak. Report PROVISIONAL until verified 24-hour evidence; otherwise BLOCKED.

## Progress / rulings
- Existing commit 2a0ccdb is incomplete: independent review found wrong default outcome index and stale report accounting. Existing seven settlement tests passed but omit trust-boundary validation. Not accepted for live execution.
- Ruling: derive outcome index from exact read-only Gamma identity checked against persisted tokens, not a default or inferred index. Missing identity keeps UNKNOWN.
- Ruling: earlier session manually rebuilt WSL windows CHECK constraint before offline gates. No ledger data deleted; phase1b backup remains. Preserve current DB via SQLite backup before further deployment; test actual migration on old-schema fixtures.
- Historical Phase 2A eight expired-slot rejection delays: 2953, 2978, 5908, 3118, 5935, 2951, 2971, 3004 ms (order creation to rejection timestamps). Includes collection/cycle scheduling, not exchange latency; inspect service pacing before changing behavior. Never weaken expiry.
- Tasks 1–2: exact evidence verification, persisted retry/evidence, atomic settlement, ledger-derived accounting and risk/STOP fixes implemented with regression tests; final independent review pending, not complete.
- Task 3: 123 MVP tests pass Windows (78.293s) and WSL (5.680s); paper 50 and strategy 21 pass Windows. Broad discovery 1167 tests failed with two errors: missing retained CP5 DB fixture and Windows detached-supervisor cleanup race. Full-window 65 and guard 41 tests all show OK in broad log; targeted longer rerun pending.
- Task 3 Ruling: preserve accepted CP5 source/tests and do not invent missing historical fixture or hide failures with skip — full regression gate remains BLOCKED; cost is delayed live validation.
- Task 4: observer and host-heartbeat scripts drafted/tested, not deployed. Independent runtime limit/timer installation, lifecycle testing, and final review still required.
- Tasks 5–6: not started. WSL service remains inactive/dead/disabled, MainPID=0. No production payout or soak configured.
- Final review fix pass: five remaining findings reproduced RED then GREEN (setup/uptime, timeout cleanup, midnight settlement, partial-sale mark precision, unknown migration counts). Runtime bound numeric check added; paper execution event carries explicit signal-to-terminal wall-clock metric.
- Latest verification: 130 MVP tests pass Windows 47.677s and WSL 4.517s; full-window 65 pass 135.626s, guard 42 pass 61.492s. Final WSL full discovery hit 300s timeout; no complete green broad run. Seven accepted hashes and all 29 unrelated fingerprints still match. `git diff --check` no whitespace errors (autocrlf warnings only).
- Ruling: no further review churn or broad-scope CP5 edits; retain BLOCKED status until historical fixture/regression and observer deployment gates can be met. Cost: 60-minute integration and 24-hour soak not launched.
- Evidence and detailed limitations: docs/phase-2b-evidence.md, docs/phase-2b-latency.md, docs/phase-2b-validation.md.
