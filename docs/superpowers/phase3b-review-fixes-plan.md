# Phase 3B Review Fixes — Execution Plan

Independent review session: ses_e824996a4ffeFqw13U8WbNyvdH
Verdict: NOT ready to merge (3 Critical, 6 Important, 8 Minor)
Constraint: no changes to production trading behavior, VPS, risk limits, Hermes soak.
All fixes confined to `src/polymarket_bot/replay/*`, `tests/replay/*`.

## Root-cause verification (Phase 1, systematic-debugging)
- [x] C1: verify adapter _load_settlement_evidence contract mismatches vs linux_mvp/settlement.verify_evidence
- [x] C2: verify variant params crash run_replay with TypeError; CLI not catching TypeError
- [x] C3: verify source_us scaling (ms vs us) and fabricated receipt_us/ns; report crash on any fill
- [x] I1: verify staleness gate dead (fabricated receipt == now)
- [x] I2: verify settlement excluded from report totals
- [x] I3: verify train_only is cosmetic
- [x] I4: verify load_all_windows bugs (available_ns attr, no run filter, silent swallow)
- [x] I5: verify dead config keys hashed into config fingerprint

## Fixes (TDD: RED test first, then GREEN)
- [x] C1 fix: settlement evidence mapping (gamma_raw from market_revisions, resolution_raw from raw_resolution_z, project window keys start_us/end_us, at_us = learned_server_us)
- [x] C2 fix: implement max_entry_price / min_remaining_seconds / require_reversal_confirmation / depth_scale in replay strategy; unknown variant params fail cleanly
- [x] C3+I1 fix: real source_us (ms→us) and real receipt_us/receipt_ns from paper snapshots; no fabricated staleness
- [x] I2 fix: fold settlement into report totals; re-verify caller-supplied settlement_result in run_replay
- [x] I3 fix: evaluate_variants enforces train-only against ChronologicalSplit
- [x] I4 fix: load_all_windows (run filter + dedupe, available_ns source, surface skipped diagnostics)
- [x] I5 fix: implement max_trades_per_window + max_daily_loss guards (or drop from config hash)
- [x] I6 fix: parity test replay vs live strategy path (test-only, no production change)
- [x] Minor: real uncertainty_us if trivially available; TypeError in CLI catch list; real db_sha256 threading

## Verification
- [x] tests/replay full pass (red→green evidence per fix) — 62 tests OK
- [x] Full suite; pre-existing 9 failures (infra ×2 / recorder-live-clob ×6 / cp57 ×1) must not grow — confirmed 8 failures + 1 error, exactly the pre-existing set
- [x] Re-review by same reviewer session with fixes applied — verdict: READY TO MERGE (session ses_e824996a4ffeFqw13U8WbNyvdH). All C1–C3/I1–I6 confirmed FIXED in code; 6 new Minor/cosmetic findings (N1–N6) and 2 carried non-blockers (M4, M8) documented as post-merge follow-ups, none merge-blocking.
- [ ] Commit + push only after tests + re-review pass; verify SHA match

## Post-merge follow-ups (reviewer recommendations, non-blocking)
- Wire `max_wait_us` into the replay loop or drop it from config/hash (carried M8)
- Map fills ↔ decision reason for `exit_reason_breakdown` (N2)
- Type-check settlement evidence elements before `.get` in `_verify_settlement` (N1)
- Surface skip reasons from `_load_settlement_evidence` decode failures (N3)
- Wrap settlement + report build in the shared `ARITHMETIC` localcontext (N5)
- Add adversarial lookahead-trap test (carried M7); expand `load_all_windows` failure-shape matrix (M6)
- Consider `immutable=1` on recorded-DB opens to avoid WAL sidecars (N6)
