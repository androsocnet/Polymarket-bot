# CP5.4 independent root-cause review — 2026-10-15

## Verdict and scope

**Spec compliance: remediation blocker rationale NOT ACCEPTED as a blanket policy prohibition. Quality: FAIL; live escalation remains blocked. Proceed with independent offline retention and atomic-publication remediation.** Tasks 1–7 stay DONE; Task 9 stays failed. This review neither reopens completed tasks nor authorizes another smoke, opening, rollover or pilot.

Research only: no production/test edits, network, subagents, runner, commits or ledger edits. Only this requested report is written. Prior reports and relevant implementation/specification were read directly. Fresh independent SQLite assertions used `mode=ro&immutable=1`; no writable connection. DB SHA256 before and after: `cae48b3e05f446f02a2ac77b9f460d2b7385d1bee42f0451f6207925017a823f`. Integrity returned `ok`. No full replay certification or new suite PASS is claimed.

## Policy question: real incompatibility, overstated prohibition

The approved source-time policy is captured fresh VALID acquisition anchor plus its uncertainty, not a requirement that scheduler phase change after every calibration: `docs/superpowers/specs/2026-10-13-cp5-3-research-quality-design.md:55-72,123-125`. Calibration uncertainty, freshness and discontinuity rules are implemented at `src/polymarket_bot/recorder/clock.py:59-86`. These remain frozen. Source-time uncertainty qualification does not prove exact true-UTC ordering (`...research-quality-design.md:68`); neither current nor proposed scheduler may claim that stronger guarantee.

There is a genuine conflict **if all existing scheduling behavior is also held immutable**: future UTC targets use active-anchor projection (`clock.py:98-101`; recorder design `docs/superpowers/specs/2026-10-10-market-data-recorder-design.md:225`), while producer and replay require exactly 2,000,000,000 ns between monotonic opportunity targets (`service.py:501-506`; `scripts/cp5_analyze.py:299-310`). If projection offset changes by delta, consecutive targets are separated by 2 seconds plus delta. VALID calibration permits nonzero delta (`clock.py:64-73`). Keeping both contracts literally unchanged cannot eliminate this transition. Pinning only an armed target postpones it; changing analyzer tolerance or declaring ordinary calibration a discontinuity is not an acceptable workaround.

But the user's freeze does **not** freeze every historical scheduling implementation detail. The remediation report `docs/superpowers/cp5.4-remediation-report-2026-10-15.md:22-26` incorrectly promotes active-anchor scheduling projection into an immutable uncertainty-policy requirement. A producer scheduling correction can separate:

- immutable, real monotonic nominal opportunity grid (exact 2 seconds, persisted once with run/market/slot identity), used by unchanged scheduler replay and smoke gates;
- fresh active acquisition anchors, retained per observation with unchanged uncertainty/freshness/discontinuity checks;
- current UTC opening/end guards and network deadlines, evaluated using the active usable clock before sending, not bypassed by nominal-grid eligibility.

This changes historical scheduler projection behavior and must be documented as such; it is not a clock-uncertainty-policy relaxation. The old design's active-anchor target conversion sentence must not be described as simultaneously preserved verbatim. A bounded scheduling-contract correction is legitimate under the current user clarification, rather than requiring permission to unfreeze uncertainty.

A concrete feasible case exists in retained evidence. Previous target is `25813562000000`; stable next nominal target would be `25815562000000`. New anchor projects that UTC slot opening to `25814929500000` and its +2-second end to `25816929500000`. Stable nominal target lies inside that current UTC slot interval, 632,500,000 ns after opening and 1,367,500,000 ns before end. Thus this recalibration does not mathematically force an early send, deadline extension, uncertainty change or irregular nominal cadence. This is feasibility arithmetic, not an implemented fix or proof that historical observations would all qualify under a different producer.

For opposite-sign shifts, wait until both nominal target and active UTC opening are reached; never send early merely to preserve cadence. If that wait reaches slot end, market end, run deadline, expired clock or discontinuity pause, classify the opportunity honestly and do not fetch. Large permitted VALID shifts can make stable nominal and current UTC intervals incompatible. Therefore **no universal smoke PASS guarantee follows**. A scheduler can preserve safety and exact nominal cadence while failing availability/coverage honestly. No fake target, reset warmup, catch-up, hidden omission or backfill is allowed. Persisted `target_ns` must represent the actual nominal scheduling plan, not a retrospective relabeling of active-anchor wake deadlines. Keep slot-driven worker and authoritative UTC slot identity; no independent poller.

UTC no-early-send safety here means preserving current active-clock slot-opening checks and actual monotonic scheduled-target checks, including market boundary. Exact true-UTC no-early proof is not available from the existing integer-second uncertain clock. If a stronger bound-based UTC rule were intended, that would need a separately specified contract; it is not evidence for weakening current guards.

## High findings

### H1 — producer drops required padded scheduler authority

`src/polymarket_bot/recorder/service.py:326-330` prunes skipped/busy/unsubmitted history at owner-now minus 60 seconds. `service.py:450-460` separately prunes at receipt minus 60 seconds, with request receipts preserving some fetched rows but not skipped rows. Both live validation (`service.py:501-506`) and reverse replay membership (`scripts/cp5_analyze.py:299-306`) require `(max(origin, receipt-60s)-2s, receipt]`. Missing durable skipped authority is a producer defect; analyzer rejection is correct.

Minimum fix must preserve the left-bracket pad at both pruning sites, align observation/context/outcome/expected-request retention, and retain or reload authority required by an outstanding immutable worker snapshot until publication finishes. Merely replacing 60 with 62 seconds relative to advancing owner-now is not a proof for delayed in-flight results. Use the receipt/origin-derived required interval, include pending-horizon needs, and fail closed on configured bounds rather than silently evict required rows. Add producer reverse-membership validation before committing publication; current `service.py:353-361` only checks that supplied members exist, not that required durable members were omitted. Preserve the 128-opportunity/256-observation bounds or explicitly fail closed when exceeded (`service.py:340-345`). No run-long unbounded history is needed.

### H2 — active-anchor reprojection violates exact monotonic producer contract

`service.py:1016-1025` computes an armed target, runs housekeeping, then recomputes through possibly changed anchor; housekeeping calibrates at `service.py:638-640`. This creates the observed 1.3675-second transition, which `service.py:501-506` correctly cannot certify as exact 2 seconds. Replay checks stored bounds against the same cadence at `scripts/cp5_analyze.py:307-310`; absent bounds then cause `INCOMPLETE_PROVENANCE` at `free_reference.py:449-451,506-507`.

Correct producer scheduling contract, not uncertainty or analyzer gates. A stable nominal grid plus independent active UTC safety/deadline guards is a viable direction subject to offline tests for both shift signs, expired/discontinuous anchors, missed/busy/unsubmitted opportunities, rollover, actual sends and run shutdown. Keeping only one armed deadline immutable is insufficient.

### H3 — fetch publication lacks durable atomicity

`service.py:406` saves each observation, `service.py:412` saves reference, and `service.py:437-442` saves fetch and completion separately. Observation/reference methods each open their own transaction (`store.py:1317-1338,1368-1386`); `_transaction` begins and commits each call (`store.py:270-283`). Interruptions can leave observations without fetch/report ownership, a reference without authority, or a COMPLETE fetch without its completion barrier. The last case is rejected by replay (`scripts/cp5_analyze.py:224-226`), but rejection is not atomic storage.

Required invariant: each newly durable v2 observation/reference has exactly one compatible owning fetch and original submitted opportunity bound to original run/market/slot/index/target/submission identity; historical observations may appear in multiple rolling evidence windows, but have one acquisition owner. New observations, reference, fetch authority and completed terminal event commit together or none commit. Already-durable submitted opportunity may remain pending after rollback/crash, then receive an explicit interrupted/failed terminal record under writer-owned recovery; it must never be inferred completed. Skipped/busy/unsubmitted opportunities remain honest terminal non-fetch dispositions. Reject duplicates, foreign identities and mismatches before publication. Advance memory mapping/history only after commit; duplicate drain must not create a second owner.

Use existing SQLite single writer and store transaction mechanism, no schema migration or second writer. Simply wrapping current public save calls in an outer `_transaction` will attempt nested `BEGIN IMMEDIATE`, so minimal store composition is required, not an outer wrapper alone. Keep revision-1 behavior unchanged. This defect is independent of scheduler policy and is not the cause of the five historical omissions: fresh ownership assertions found no current orphan observations/references and all 57 completion barriers exist.

## Medium findings

### M1 — stop decision unnecessarily blocks independent repairs

`docs/superpowers/cp5.4-remediation-report-2026-10-15.md:24-26,32` stops all production remediation because it equates stable scheduling with a frozen clock-policy change. The algebraic active-projection conflict is valid, but that policy inference is not under the clarified freeze. Even if scheduler design remained unresolved, retention and atomic ownership fixes do not depend on changing uncertainty, exact cadence, coverage or live authorization. Their offline implementation and fault tests should proceed independently. Baseline passing suites in that report do not close H1–H3.

### M2 — interruption regression does not assert atomic storage

`tests/test_free_persistence.py:316-331` injects reference-save failure after resetting memory mapping, then checks only recorder-event count and memory mapping. It never checks observation/reference table counts, so committed orphan observations can escape this assertion. Extend this existing regression with before/after rows and owner relations, failure after each publication step, completion-event failure, duplicate drain and reopen/recovery. Test opportunity-persistence failure before worker submission, worker completion before drain, pending shutdown, and original identity mutation. Current test proves neither all-or-none publication nor absence of orphans.

## Fresh five-fetch reconstruction

All numbers below were independently computed from the immutable failed DB, not copied as a replay PASS. Durable membership used same-run opportunity events satisfying `(max(origin, receipt-60s)-2s, receipt]`; compared both directions against embedded membership.

| Fetch event | Current submitted opportunity | Reference | Current observations | Missing older skipped opportunity | Replay exclusive lower ns | Missing target ns |
|---|---|---|---|---|---|---|
| 600 | 598 | 26 | 51,52 | 277 | 25751328000000 | 25751562000000 |
| 694 | 615 | 28 | 55,56 | 278 | 25753437000000 | 25753562000000 |
| 699 | 697 | 29 | 57,58 | 279 | 25755078000000 | 25755562000000 |
| 708 | 704 | 30 | 59,60 | 280 | 25757218000000 | 25757562000000 |
| 713 | 711 | 31 | 61,62 | 281 | 25759062000000 | 25759562000000 |

Exactly these five differences; no extra embedded members. Remaining 52 memberships equal their durable sets, not 52 certified exact replays. DB contains 57 v2 fetches, 336 opportunities, 57 completion events, 114 uniquely current-owned observations and 57 uniquely owned references. Every fetch has one matching completion barrier and original submitted opportunity. All ten provider attempts for these five fetches record HTTP 200. Missing IDs 277–281 have zero request outcomes across all fetch evidence: these are skipped scheduler memberships, not five lost provider fetches. Their durable reasons are BEFORE_START for 277–278 and PROCESSING_OVERRUN for 279–281.

Fresh query confirms null validated bounds for reference 26 and references 28–57. Transition opportunity 605 target `25813562000000` to opportunity 615 target `25814929500000` differs by `1367500000` ns; no APPROXIMATE_DISCONTINUITY event exists. Final persisted selected assessment is FULL, VALID, 30 occupied cells, 15 exposed pairs, zero empty pairs and missing requests, but INSUFFICIENT with sole reason INCOMPLETE_PROVENANCE. This confirms the two independent producer failures; it does not recompute every price/assessment or certify full replay.

## Concrete safe path

1. Preserve failed DB and reports unchanged. Keep Task 9 FAIL and all live phases stopped; tasks 1–7 remain completed historical work.
2. Implement only offline minimum retention and producer reverse-authority checks, plus all-or-none v2 publication and explicit interrupted pending-opportunity recovery. Add focused regressions/fault injection in existing test files. These repairs proceed even if scheduling remains unresolved.
3. Correct producer nominal scheduling contract without changing uncertainty, freshness, discontinuity classification, exact replay cadence, 30 cells/15 pairs, exact 15-opportunity/28-second smoke window, warmup origin, fallback or strict cutoff. Preserve active-clock no-early-send and slot-end/run deadlines. Verify actual send timings, not only nominal metadata. Do not assert stable-grid implementation already exists or guarantees smoke success.
4. Run requested full/focused/scheduler suites and changed-file scoped Pyright with exact baseline diagnostic comparison where needed; lint remains N/A per recorded ruling. Obtain fresh independent rereview closing High/Medium findings. Only a separately authorized new disposable live run could provide new quality evidence; never repair the old DB or reinterpret its failure as PASS.

## Verification limits

Fresh review executed one read-only SQLite assertion program and hash comparison. No production/test files changed, so no implementation lint/typecheck or test suite was run; earlier report suite results remain attributed historical evidence only. This report establishes root causes, policy distinction and a safe remediation direction, not remediation completion, atomicity enforcement or live quality success.
