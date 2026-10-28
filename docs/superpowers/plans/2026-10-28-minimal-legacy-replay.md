# Minimal Legacy Replay Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Demonstrate honest, deterministic partial replay of market `5483041`, source run `0cba7ec8-c91f-4b78-a9b3-ee7d54e55b40`, with 434/450 recorded paired captures.

**Architecture:** Add an explicit replay-only legacy loader and CLI opt-in. Derive scope from original recorded discovery, direct Gamma revisions, CLOB configurations, and capture links; never manufacture persisted selection configuration. Reuse existing paper data types, parsers, missing-slot timing helper, execution engine, and settlement verifier; keep normal strict loader and runtime modules unchanged.

**Tech Stack:** Existing Python, sqlite3, hashlib, unittest; no new dependencies.

**Spec:** User task “PHASE 3B — MINIMAL LEGACY REPLAY RECOVERY”; compatibility assessment in preceding conversation; approved `docs/superpowers/specs/2026-10-27-phase3b-replay-design.md`.

## Global Constraints

- Base branch `feat/phase3b-replay`, SHA `bf5e2ab558d0c852687384ae4d86f59a2a0b3c85`.
- Real source: `C:\Users\Andro Saptajulianto\Downloads\Polymarket-bot\data\runtime-validation-8b511e7.sqlite3`.
- Source SHA256: `ade9b814b34deab7cfd21b1dbbb76de1731bed27d91c866611c0749a61fa8fde`.
- No database mutation, source copying, VPS/SSH, Hermes changes, trading, runtime changes, strategy changes, or settlement-verifier changes.
- No event-envelope revision normalization or additional real markets in this change.
- Use immutable read-only database connections; reject active WAL/journal evidence, preserve source hashes and original run configuration.
- Test-generated fixtures are unit tests only, never historical replay evidence.
- Commit/push only after regressions pass and fresh independent implementation review has no unresolved significant findings.

## Review Focus

- A matching markets row alone is insufficient: same-run recorded discovery must prove unique event membership and agree with Gamma/CLOB token identities.
- Metadata learned after a capture, crossed run/anchor/revision links, and broken request ordering must fail closed.
- Missing captures must remain execution opportunities with rejection reasons; no skipping holes to obtain fills.
- Opt-in cannot silently alter strict loader behavior, use another run, normalize configuration, or accept source hash drift.
- Unknown settlement must remain unknown in reports; no fabricated terminal liquidation or payout.

## Files

- Create `src/polymarket_bot/replay/legacy.py`: explicit immutable legacy evidence loader and exact coverage/provenance summary.
- Modify `src/polymarket_bot/replay/cli.py`: `--legacy-scope` opt-in and expected source SHA256, route legacy loader and attach scope diagnostics only for opted-in output.
- Create `tests/replay/test_legacy.py`: focused positive/adversarial tests, optional explicitly configured real-source check; no committed DB fixture.
- Create `docs/superpowers/2026-10-28-legacy-replay-evidence.md`: bounded real-run command, configuration, coverage, result hashes, test/review evidence.
- Do not edit `paper.py`, `strategy.py`, recorder/runtime modules, production strategies, or settlement verifier.

## Task 1: Proven scope and validated legacy recording

**Interfaces:** `load_legacy_replay_recording(db, run_id, market_id, *, expected_sha256)` returns existing adapter six-value result plus coverage/provenance dictionary. Normal `load_replay_recording` remains untouched. Expected SHA256 is caller-supplied provenance pin, not a substitute for evidence validation.

- [x] Add RED tests for opt-in success, absent/ambiguous discovery membership, Gamma/CLOB mismatch, crossed run/revision/anchor links, metadata learned after capture, altered raw evidence, source hash mismatch, and unchanged strict-loader rejection.
- [x] Run `python -B -m unittest tests.replay.test_legacy -v`; verify failures identify missing behavior.
- [x] Implement minimum replay-only loader using `Recording`, `Snapshot`, `_missing_slot_start`, existing parsers/decoders, and unchanged per-book/audit/clock/depth requirements. Validate discovery provenance and direct revision content hashes; retain rejected evidence and missing opportunities.
- [x] Preserve slot-end availability, single-run horizon, source SHA256, original configuration, and exact slot status/index coverage. Load settlement using existing verifier and immutable connection handling without global monkeypatches.
- [x] Run focused tests GREEN. Confirm successful load cannot convert absent selection metadata into claimed original selection intent.

## Task 2: Deterministic opt-in replay and evidence

- [x] Add tests for CLI opt-in/default separation, missing-slot execution rejection, historical clock causality, unknown-settlement handling, deterministic canonical output, source bytes/config preservation, and no SQLite sidecars. See handoff for original RED versus acceptance mutation evidence.
- [x] Run CLI RED→GREEN and add opt-in coverage/provenance diagnostics; use unchanged replay engine and settlement verification. Acceptance behaviors already pass existing implementation; six in-memory mutations prove assertion failures without changing production files.
- [x] Run focused tests GREEN and replay relevant existing adapter/engine/CLI/paper/strategy regressions. Relevant 171/171 passed; full suite completed with 8 failures + 1 error identical to historical baseline.
- [x] Run real source twice with identical default strategy configuration, fixed expected hash, network blocked, no variants/depth scaling. Verify 434 paired captures, 16 MISSED slots, 868 VALID books, unchanged hash/config, unknown settlement, identical output hashes, and honest remaining positions/valuation.
- [x] Record bounded replay summary and reproducible command in evidence document; do not commit bulk ledger/book exports or databases.
- [x] Obtain fresh independent implementation review, resolve significant findings with RED/GREEN tests, then rerun verification. Fresh reviewer session `ses_e7f3a2e90ffeiK3rlsBy5ND5bm` returned ACCEPT (0 Critical, 0 Important, 3 Minor).
- [ ] Commit scoped changes, push `origin/feat/phase3b-replay`, and verify local/remote SHA equality. If review unavailable or significant findings remain, do not commit/push as complete.

## Progress

- [x] Approved base and clean existing isolated worktree confirmed.
- [x] Source hash, terminal first run, 434 COMPLETE/16 MISSED slots, and five direct-market revisions freshly inspected.
- [x] Previous review findings and approved replay design inspected.
- [x] Baseline replay regression: 62 passed in 107.914s (initial 120-second invocation timed out; longer run completed).
- [x] Independent design check received; replay-only isolation approved, with corrections below.
- [x] Plan approval before implementation; native inline execution and conditional commit/push approved.

## Independent design review disposition

- Keep isolated replay-only validation and both paper-loop / strategy-availability semantics; add strict-versus-legacy fixture parity test to detect drift. Normal runtime modules remain untouched.
- Discovery existence is already resolved by read-only assessment: same-run GAMMA_EVENTS request 2 proves event 1163394 membership for market 5483041, corroborated by request 109. Require this stronger proof; reject missing/ambiguous evidence. Do not add reviewer-proposed DIRECT_MARKET_ONLY fallback or extra discovery shapes not needed by target.
- Reviewer expectation of fewer than 868 validated books is unsupported: independent assessment validated all 868 target books; 46 are one-sided and remain valid raw books. Strategy usability is separate from validation. Expected target totals remain 434 pairs, 16 MISSED slots, 868 validated books, 411 pairs with both books two-sided.
- Reuse existing adapter descriptor/pairing helpers where safe; settlement helpers currently open their own non-immutable connections, so legacy path needs local immutable evidence reads plus unchanged verifier, not process-wide monkeypatching.
- No-resolution evidence currently produces None on normal adapter/engine route, not automatically UNKNOWN_SETTLEMENT. Preserve normal behavior; actual target has a directional record and must remain UNKNOWN_SETTLEMENT / Gamma not final.
- Document pin as exact file-byte identity, not logical SQLite equivalence. Reject hash mismatch, nonterminal or crossed runs, malformed discovery, and unsafe sidecars.
- Design review is not implementation review and does not authorize commit/push.
- [x] Replay baseline: 62 tests passed in 107.914 seconds.
- [x] Strict-versus-legacy superset fixture parity test added to RED/GREEN cycle.
- [x] Fresh independent implementation review after code and real replay exist (ACCEPTED: session `ses_e7f3a2e90ffeiK3rlsBy5ND5bm`).

## Execution handoff — ACCEPTED FOR COMMIT/PUSH

Evidence: `docs/superpowers/2026-10-28-legacy-replay-evidence.md`. Legacy suite now 38 tests. Six acceptance behaviors passed 6/6 in 14.711s after six in-memory mutations each produced assertion failures without errors. No acceptance production changes required. Latest relevant regressions: 171 passed in 248.381s, zero failures/errors (`acceptance-regressions.txt`). Genuine replay repeated in `acceptance-real-one`/`acceptance-real-two`: expected counts, UNKNOWN_SETTLEMENT, identical output hashes, unchanged source bytes/hash/config and no sidecars. Full suite: 1,336 tests, 8 failures + 1 error matching baseline (`acceptance-full-suite.txt`). Fresh independent reviewer session `ses_e7f3a2e90ffeiK3rlsBy5ND5bm` returned ACCEPT. Ready for final rerun of relevant regressions, commit, push, and remote SHA equality verification.
