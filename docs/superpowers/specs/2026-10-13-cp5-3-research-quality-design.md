# CP5.3 research-quality contract — approved design

Date: 2026-10-13. Clarified: 2026-10-14. Status: APPROVED with mandatory clock-semantics and coverage-separation clarifications incorporated. Implementation-plan approval remains required; no production implementation or live validation authorized by this checkpoint.

## Scope and classification

Start from clean branch `feat/cp5-data-quality`, HEAD `6e1f582`; preserve `51a7645` and `6e1f582`. This is a cross-cutting semantic-contract project, not a restart or schema redesign. Existing storage supports bounded implementation, but changes to acceptance, replay, fallback eligibility and smoke validation require a written design gate. Opening preparation is a second conditional feature; inspect its authoritative discovery implementation after quality PASS and stop for further approval if substantial architecture change is necessary.

Intent: distinguish arithmetic value correctness from research sampling sufficiency. Never imply settlement accuracy, Chainlink equivalence, TWAP, strategy edge or trading readiness. No interpolation, fabricated samples, backfill, private routes, credentials, orders, merge or push.

## Existing evidence and semantics

The approved FREE_REFERENCE design section 5 explicitly permits fewer samples across gaps and defines zero accepted samples as missing. Current engine therefore permits one sample numerically; CP5.3 does not invent this rule. One sample does not qualify as research-sufficient after warmup.

Preserve existing `APPROXIMATE_SAMPLE_MEAN_60S`, `APPROXIMATE`, `UNVERIFIED`, `LOCAL_WALL`, Decimal precision 512 and ROUND_HALF_EVEN. Preserve original report diagnostics and their order for historical replay. Legacy strict gap means head >2,000,000 us, consecutive accepted wall receipts >4,000,000 us, or tail >4,000,000 us. Equality is allowed. Keep this computation independently accessible, including startup head gaps; never rewrite historical stored reports.

Current immutable observation/report models use string classifications, not enums. Add a separate derived immutable research assessment rather than adding database columns or changing historical report fields. Assessment carries contract revision, selected provider, contributing IDs, value status, coverage quality, warmup status, strict gap, timing/gap diagnostics and reasons. Persist policy provenance in existing versioned fetch event JSON for new runs. Historical version-1 events keep legacy computation and selection. No schema migration.

## Value validity

`VALID` means a nonempty set of genuine successful selected-provider BTC/USD USD REST observations with positive finite prices, correct provider/pair/parser provenance, no parser error, and receipts inside the inclusive 60-second local-wall window. Apply monotonic stale/future checks when clocks are present; require complete monotonic/anchor provenance for new research-qualified runs. Never mix provider prices in one mean. Reject malformed, incompatible currency/pair, future receipt, stale-only and empty evidence. Compute precisely the existing deterministic sampled arithmetic mean. Source-time admissibility follows the versioned policy below, not a comparison between unlike clock domains. For revision-2 research qualification, source timestamp must be inside the approved uncertainty envelope; this establishes only `SOURCE_TIME_WITHIN_CLOCK_UNCERTAINTY`, not exact UTC ordering or proof of source time before true receipt. Actual observation receipt and report completion must precede the applicable research decision cutoff using persisted same-session monotonic timing. No post-cutoff report may be treated as earlier-available evidence. Numerical validity alone never proves causal research availability.

`INVALID` means these conditions cannot establish a numeric value. A numerical value can be VALID while coverage is INSUFFICIENT. Missing or inconsistent provenance fails research qualification even if a historical number can still be displayed.

Replay must reconstruct fetch membership, captured anchors, observation exclusions, selected provider, exact Decimal value, original diagnostics and derived classification. No nearest-anchor inference. Incomplete events fail closed.

## Report coverage quality — approved operational fault budget

Keep three independently computed concepts, outputs and gates distinct; never share one generic coverage variable or threshold:

A. **REPORT COVERAGE QUALITY**: receipt-anchored 2-second cells / 4-second pairs yielding `COMPLETE`, `DEGRADED` or `INSUFFICIENT` as defined below.

B. **SMOKE CONTINUITY EVIDENCE**: 15 consecutive persisted scheduled 2-second opportunities, exactly one completed qualifying report per opportunity, and first-to-last completion span >=28 monotonic seconds. Scheduled opportunity identity, not receipt-cell occupancy, establishes continuity.

C. **PILOT CAUSAL RESEARCH AVAILABILITY**: a qualifying reference completed strictly before the earliest actual book request send across both legs and all attempts for the paired slot, with completion age <=2,000,000,000 ns at that cutoff. Missing actual attempt/completion timing fails this gate; report quality or slot association cannot substitute for it.

No approved design currently defines a research error tolerance or statistical confidence target. Therefore no accuracy bound can honestly be inferred from 2-second sampling alone. Approved operational fault budget: require evidence in every consecutive pair of nominal polling opportunities, rather than exact receipt deadlines. This permits one missed observation per pair, never an entire missing pair. This is an approved operational research-preparation policy, NOT a statistical accuracy requirement.

For post-warmup reports, partition the 60-second monotonic interval ending at report receipt into 30 half-open 2-second cells, with the report endpoint included in the last cell. Project accepted contributing receipt ages into these cells; wall inclusion remains required. Count occupied cells, not raw sample count, so duplicate/bursty observations cannot improve coverage. Partition cells into 15 adjacent 4-second pairs anchored at the report end.

- `COMPLETE`: all 30 nominal cells have accepted evidence, no failed/excluded in-window requests, no fallback, no missing-request evidence, and valid provenance.
- `DEGRADED`: every one of the 15 cell pairs has accepted evidence, but COMPLETE conditions fail. Thus at least 15 independently occupied cells are required; a single sample cannot qualify. Head/tail and interior gap measurements remain explicit. This is an operational coverage claim only, not a bound on price-estimation error.
- `INSUFFICIENT`: any pair is empty, numeric value INVALID, incomplete provenance, or clock discontinuity prevents reliable window assignment.

Report counts, occupied cells, empty cells/pairs, maximum wall and monotonic interior gaps, head/tail offsets, failed requests, exclusions, missing scheduled fetches, fallback, provider availability, same-fetch disagreement and receipt jitter. Missing-request inference uses persisted scheduler/fetch evidence; never equate an empty cell with a proved HTTP failure. No disagreement threshold is invented; disagreement is diagnostic, not consensus aggregation.

This partition policy is phase-sensitive by design; tests must cover cell boundaries and jitter. It is approved before any new live data. Changing this fault budget requires renewed approval before implementing a different threshold. Do not tune it against CP5.2 or fresh data to obtain PASS.

## Warmup

Separate structural unavailable prehistory from observed runtime omissions. Warmup begins at the persisted monotonic start of the approximate sampling session, not the first accepted sample and not merely recorder run creation. Persist this origin in new event policy metadata. Full warmup requires report monotonic age >=60,000,000,000 ns from that origin, with same-run clocks and valid provenance. Run-wall age remains a separate historical descriptive metric.

Before full warmup, mark `WARMING_UP`; classify coverage only over the observable elapsed interval using cells clipped to sampling start and label it provisional. Do not count unavailable pre-start cells as failed requests. Warmup reports never satisfy the post-warmup smoke minimum. Accepted observations and legacy gap diagnostics are still computed without fabricated prehistory.

## Coinbase clock investigation and approved policy

Root cause: CP5 runner constructs provider without projection; parser compares exchange source timestamp against local wall, then session appends captured-anchor server projection after parsing. Engine repeats the local-wall source check. Thus parser/session/engine disagree on clock provenance.

Retained fresh case: observation 75, reference 38: source 1791873042866047, wall 1791873042839308, projected server 1791873042265500, captured anchor uncertainty 1093500 us. Source-wall +26739 us, source-server +600547 us. Timestamp retained; prices NULL. This proves inconsistent comparisons, not the cause of clock skew.

Compare policies:

- A local wall only: reproducible but ignores approved calibration and observed uncertainty; reject.
- B server point only: treats integer-second midpoint estimate as exact; reject.
- C uncertainty-aware server interval: use captured valid fresh anchor projection and its persisted uncertainty; approved.
- D combined wall/server bound: no persisted independent bound on local-wall accuracy; reject rather than invent one.

New clock-policy revision accepts source timestamp <= projected receipt server time + captured uncertainty; rejects greater timestamps as FUTURE_SOURCE_TIME. Equality accepted. No additional tolerance, timestamp rewriting or Kraken source timestamp. Passing this upper-bound check establishes only `SOURCE_TIME_WITHIN_CLOCK_UNCERTAINTY`; it never proves source timestamp occurred before true receipt time, including timestamps at or below the projected server point. Strict no-look-ahead / causal availability relies on persisted monotonic observation receipt and report completion before the research cutoff, not uncertainty-qualified source ordering. Do not call this exact UTC ordering or exact no-future certification. Kraken still has no fabricated source timestamp; its causal availability depends on actual acquisition/completion evidence.

Capture anchor before request; pass its immutable policy context through parser/session; persist anchor ID, policy revision and uncertainty used in fetch event. Anchor must be VALID and usable at acquisition; missing/expired/discontinuous context fails closed, never silently falls back to wall. Replay checks uncertainty against stored anchor and projects original receipt monotonic time. Engine applies the same policy; local-wall source comparison remains for legacy revision only. Rejected parsed source timestamp stays retained and numeric fields stay NULL. Historical legacy rejection is not reaccepted or silently recomputed with new policy.

RED tests include the retained exact case, upper-bound equality, one microsecond beyond, missing/expired/discontinuous anchors, future receipt, wall jumps and exact new/legacy replay.

## Fallback

Keep independent histories. New research selection uses primary only if value VALID and coverage is not INSUFFICIENT after warmup. Latest primary request failure alone never causes fallback while its window remains sufficient. If primary is insufficient and Kraken is VALID with sufficient coverage, explicitly select Kraken; record primary insufficiency reason and fallback provenance. Both insufficient yields no research-usable selected reference; retain any numerically valid primary result for diagnostic display with INSUFFICIENT status rather than conceal it.

During warmup retain existing availability-based selection, with explicit provisional quality. Historical revision-1 fallback remains unchanged. `was_fallback` for new reports means actual Kraken selection, not unsuccessful fallback attempt. Store failed fallback attempts as diagnostics, not provider switching. No provider consensus.

## Predeclared 120-second quality gate

Only after design approval, implementation RED/GREEN, full/focused tests, scoped Pyright, whitespace verification and fresh independent review with no High/Medium findings:

1. One new disposable v2 DB, exactly 120 seconds, existing bounded supervisor, public allowlist only; no reuse or overwrite.
2. Supervisor returncode 0, watchdog false, child STOPPED/exit 0, empty unexpected stderr; all clients/transport/store closed, worker inactive, no retained threads, writer reacquired.
3. SQLite integrity OK, foreign keys clean, user_version 2, zero exact Chainlink contamination, complete exact version-aware replay.
4. Smoke continuity evidence requires 15 consecutive persisted scheduled 2-second opportunities, exactly one completed post-warmup research-sufficient VALID report per opportunity, and first-to-last completion span >=28 monotonic seconds. This requires an observed half-window of repeated rolling decisions, not an arbitrary percentage. Every emitted post-warmup assessment must be VALID and COMPLETE or DEGRADED; any INSUFFICIENT fails.
5. Full source/policy provenance, no future receipts or accepted source outside justified upper bound; uncertainty-qualified source ordering explicitly reported.
6. At least one COMPLETE paired book slot; honest classifications for every attempted/scheduled slot. Full-market book coverage is not claimed by this smoke.

Capture all user-requested send/retry/provider/error/value/quality/warmup/legacy-gap/fallback/book/replay/DB/lifecycle metrics. Legacy gap-free report count is diagnostic, not PASS prerequisite. FAIL stops live escalation; no rerun for threshold tuning and no pilot.

## Conditional opening preparation

Only after quality smoke PASS, inspect authoritative public market discovery and scheduler. Prepare explicitly returned next-market metadata before boundary using existing public route/identity validation. No guessed slug, condition or token IDs. Verify unique market, exact contiguous start/end, UP/DOWN identity and token mapping. Preserve current metadata until boundary; atomically activate validated prepared metadata at boundary. Preparation unavailable or ambiguous leaves existing fail-closed discovery behavior. No book request before scheduled target, catch-up, backfill or stale prepared reuse.

TDD covers future identity, wrong start, ambiguity, token mismatch, rollover activation, slot 0 wake, no pre-boundary book, stale preparation and unchanged unavailable-preparation behavior. If existing API cannot authoritatively return future metadata or safe preparation requires major redesign, STOP for approval. Opening contract must be finalized in a reviewed checkpoint before bounded live rollover validation.

Opening test: one boundary and first several slots only; predeclare exact bounded timing after inspecting current scheduler. PASS requires correct slot 0 attempt, observed request start >=target, matching verified identity, no backfill and stable scheduler. Failure stops escalation.

## Conditional pilot and decision

Only quality PASS and opening PASS allow one new disposable v2 DB, aligned 1200-second pilot, no extension. Preparation must begin before intended full-market boundary, not startup after boundary. Analyze one complete 900-second market: paired valid slots/450, opening/closing, missing/classified slots, longest gap, reference validity/quality/warmup/strict-gap distributions, provider availability/disagreement/receipt uncertainty, exact replay and lifecycle/DB isolation.

Research usability requires reliable complete market identity and explicit 450-slot accounting, opening and closing observations, deterministic replay, honest gaps and exclusions, and meaningful post-warmup sufficient references. Do not automatically equate 450/450 books with required usability; final per-slot quality evidence and exclusion capacity must be independently reviewed. No invented numeric book-availability percentage. Any unsafe identity/provenance/replay/lifecycle defect fails closed. Do not claim strategy edge or trading readiness.

## Verification and review

Preserve baseline 625 tests as historical evidence; run fresh complete unittest suite plus focused CP5/FREE_REFERENCE/CP3 suites. Add requested regression categories, using existing unittest framework. Do not weaken legacy tests; retain legacy replay path and explicitly test new revision. Scoped Pyright for changed files; compare service diagnostic identities against untouched starting commit using read-only git content/native temporary approved location, without altering working tree. No unrelated debt fixes. Run git diff --check. No configured lint command exists; ask for project lint command before completion if one is required.

Independent review covers all 13 requested semantic/safety areas. High/Medium: reproduce, RED, fix, verify, rereview. Logical local commits only after verification and review; inspect status/diff/log, stage only intended files. No push/merge/history rewriting. Final checkpoint includes A–Z requested evidence and exactly one requested verdict; unrun phases explicitly labeled.

## Independent builder-check resolutions

Fresh builder review identified seven material ambiguities. Resolutions below are approved, subject to the explicit clock-semantics and three-concept separation above.

1. References are asynchronous research observations, not inputs asserted available at a book target. A report may contribute to a later decision only when its persisted monotonic receipt and completion are strictly before that decision cutoff. Never assign a post-target report as information available at an earlier target. This task does not create strategy decisions or alter legacy reference associations; analyzer must expose actual clocks and must not claim causal availability from association alone.
2. Pilot research-usability gate is frozen before pilot: all system/replay/provenance gates above; one identity-consistent 450-slot market accounting; valid paired opening and closing; at least one uninterrupted 60-second interval of paired valid books and sufficient post-warmup references available by each book's actual request start. At 2-second cadence this means 31 consecutive paired slots covering 60 seconds. Other gaps remain explicit and exclude affected research decision points. This is an approved minimum reusable rolling-window segment, not a statistical accuracy or availability percentage. Failure of any criterion yields FAIL, unavailable safe validation yields BLOCKED. Independent final review may reject usability for evidenced safety/identity defects, not introduce a new tuned percentage.
3. Keep current slot-driven reference worker; independent polling during unavailable discovery is out of scope. Missing opportunities remain visible and can cause insufficiency. For smoke consecutive-report requirement, use persisted scheduled slot IDs/targets associated with fetch submission, not receipt-time binning. Require 15 consecutive 2-second scheduled opportunities with exactly one completed qualifying report each, and actual monotonic first-to-last completion span >=28 seconds. Do not fill skipped/busy opportunities. Persist association in new fetch event metadata.
4. Sampling origin is monotonic session.start activation, captured once and persisted; dispatch delay is runtime observable absence, not prehistory. Warmup provisional cells/pairs remain receipt-anchored and clipped to origin; evaluate fully exposed pairs, report the oldest partial pair separately. There is no incomplete newest pair. No full pair yet means provisional insufficient evidence, not request failure. Full warmup still requires 60 seconds of elapsed sampling-session lifetime.
5. Revision-1 exact replay remains successful independently of new qualification. Emit historical numeric validity and descriptive cell coverage where evidence supports them, but qualification/warmup/missing-request state is UNKNOWN without policy-origin evidence. Do not silently treat UNKNOWN as sufficient or relabel historical stored results. New smoke and pilot require revision-2 complete provenance.
6. Coverage completeness and request-failure degradation are candidate-provider scoped. A Kraken error does not downgrade otherwise COMPLETE primary coverage. Missing global fetch membership or anchor provenance disqualifies the complete fetch for qualification; an ordinary explicit Coinbase rejection does not disqualify sufficient Kraken. Corroboration availability remains separate diagnostics. Explicit fallback is always at least DEGRADED even if Kraken cells are full.
7. Anchor validity/freshness checked both before send and at receipt, including retries; receipt age must satisfy existing 120-second anchor usability ceiling. Discontinuous/expired/missing anchor disqualifies observation for research evidence. Later valid calibration permits newly acquired evidence, but a window containing an unresolved clock discontinuity is INSUFFICIENT until affected 60-second history expires. Wall/monotonic window-membership disagreement is explicit; new qualification fails closed for affected windows rather than inventing a wall-jump magnitude tolerance. Sampling origin does not reset and missing intervals do not disappear.

Builder follow-up precision: warmup pairs remain receipt-anchored like full windows; exclude only the oldest pair intersecting sampling origin, with its partial exposure separately reported. There is no incomplete newest pair. For membership disagreement, inspect otherwise successful, identity-valid, numerically valid candidate-provider observations before temporal exclusion; error rows do not create a wall-discontinuity inference. Explicit anchor discontinuity is global: retain its observed calibration-end monotonic timestamp in event provenance and disqualify until report time is at least 60 seconds after that timestamp. Ordinary primary errors do not block fallback. Pilot paired-slot cutoff is earliest actual send start across both legs and all attempts; qualifying reference must have completed before that cutoff and be no older than one nominal 2-second polling interval at cutoff. Older reports cannot satisfy causal segment gate. Request-attempt timing must be persisted and reconstructible; missing timing fails the gate rather than being inferred from slot association.

## Approval decisions

Approved: paired-opportunity report coverage fault budget, uncertainty-qualified source-time admissibility (only `SOURCE_TIME_WITHIN_CLOCK_UNCERTAINTY`, never exact UTC ordering proof), coverage-aware fallback eligibility, persisted sampling-start provenance, separate 15-report/28-second smoke continuity evidence, and separate pilot causal availability with the 2-second age ceiling and 31-slot/60-second reusable segment. All other rulings remain approved. Next deliverable is the self-reviewed implementation plan. Stop for implementation-plan approval; do not implement production code or run live validation now.
