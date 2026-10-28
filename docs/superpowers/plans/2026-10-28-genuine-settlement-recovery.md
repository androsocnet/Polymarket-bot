# Genuine Settlement Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate authentic later final settlement for historical market 5483041 without backdating evidence or changing strategy decisions.

**Architecture:** Keep exact official response bytes as UTF-8 raw strings in a separate pinned provenance bundle under docs/superpowers/evidence/5483041/. Opt-in legacy loader validates bundle SHA256, metadata, endpoint provenance, retrieval timestamps, raw hashes, and equality to already-proven recorded resolution response. Unchanged settlement verifier and engine apply final evidence after strategy execution; strict loader and capture metadata remain untouched.

**Tech Stack:** Existing Python stdlib, unittest, SQLite immutable reads, unchanged verify_evidence and replay engine.

**Spec:** User's PHASE 3B — GENUINE SETTLEMENT RECOVERY request; existing docs/superpowers/specs/2026-10-27-phase3b-replay-design.md. User explicitly authorized autonomous execution and conditional scoped commit/push.

## Global Constraints

- Approved base 3c6448252b52180795e7d8334309ea35845152d7, branch feat/phase3b-replay, existing linked worktree.
- Preserve source SHA256 ade9b814b34deab7cfd21b1dbbb76de1731bed27d91c866611c0749a61fa8fde and all original DB bytes/config; no sidecars.
- Never alter settlement verifier, strict loader, paper.py, strategy.py, recorder, risk limits, running soak, live/paper trading, wallets or dependencies; no VPS/Hermes.
- Actual external retrieval time supplies at_us. Reported resolution time is distinct. Current response cannot prove historical availability.
- Publish only after relevant tests pass and fresh independent review accepts; no profitability claim.

## Review Focus

- Coordinated raw/hash edits: caller bundle SHA256 required; valid hash is identity/integrity, not cryptographic proof of remote origin.
- Wrong market/run/source/window/token or resolution mismatch: reject before payout.
- Nonfinal, disputed, contradictory, missing/extra resolution: unchanged verifier rejects; loader fails closed.
- Malformed JSON/provenance/timestamps and CLI without explicit legacy opt-in: error without output artifacts.
- Later knowledge must not alter recording, pairs, opportunities, decisions, ledger or strategy config; winning and losing open-inventory tests reconcile exact values.

### Task 1: Genuine acquisition and validation

**Files:** docs/superpowers/evidence/5483041/settlement.json; docs/superpowers/2026-10-28-genuine-settlement-evidence.md.
**Interfaces:** Bundle schema phase3b-external-settlement-v1; identity/run/source/window plus sources.gamma/data_api/clob raw UTF-8 strings, method, exact URL/query, final URL, HTTP status, request UTC start/end, headers, byte size, SHA256.

- [x] Read original immutable identities, recorded raw provenance, verifier requirements and prior decisions.
- [x] GET official Gamma /markets/5483041, Data API /v2/resolutions?condition=<exact>, CLOB /markets/<condition>; capture exact responses once, never reconstruct raw bodies.
- [x] Validate matching Gamma closed/resolved, payout [0,1000000], recorded DOWN, token mapping and Data API hash equal original recording; unchanged verifier returns RESOLVED at actual retrieval time.
- [x] Document conflicting source fields, timestamps, source consistency versus independent on-chain finality limitation; stop if final evidence invalid.

### Task 2: Minimal opt-in integration with TDD

**Files:** modify src/polymarket_bot/replay/legacy.py and cli.py; tests/replay/test_external_settlement.py.
**Interfaces:** load_legacy_replay_recording(..., expected_sha256, settlement_file=None, settlement_sha256=None); --settlement-file PATH --settlement-sha256 HEX require --legacy-scope. Evidence at_us is max actual response retrieval time. Diagnostics include bundle SHA256 and at_us.

- [x] Write positive loader and winning/losing open-inventory reconciliation test; compare decisions/ledger/config/pairs/opportunities to default, assert no source edits. Use disposable synthetic fixtures only.
- [x] Write adversarial matrix for bundle pin, raw hashes, official endpoint/method/status, identity/window/run/source, retrieval ordering/timezone, recorded resolution mismatch, malformed bundle and unchanged-verifier rejection. Add CLI fail-closed and deterministic success tests.
- [x] Run new tests RED (missing behavior), then minimum loader/helper and CLI changes; rerun GREEN. Default routes and missing-slot/clock validation stay unchanged.

### Task 3: Verification, fresh review, conditional publication

**Files:** evidence report and this plan; ignored execution logs.

- [x] Relevant unittest suite: all eight tests.replay modules plus new external settlement module, tests.test_paper, tests.test_strategy. Run full unittest discovery; explicitly disclose historical failures by name, no unrelated fixes.
- [x] Two network-disabled genuine CLI replays with same original config/default parameters and pinned bundle; outputs byte-identical, source hash/config/sidecars unchanged; accounting exact, decisions/ledger equal baseline.
- [x] Fresh independent review of entire diff plus untracked evidence/tests, exact plan/user constraints and actual logs; resolve significant findings with RED/GREEN tests.
- [ ] Only on tests/review acceptance: scoped commit, push origin/feat/phase3b-replay, compare HEAD with actual git ls-remote branch SHA. Final report eight requested sections, remaining backtest limits; stop.
