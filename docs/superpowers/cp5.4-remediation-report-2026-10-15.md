# CP5.4 failed Task-9 remediation — 2026-10-15

## Status and scope

BLOCKED before production changes. Tasks 1–7 remain completed; Task 9 remains failed. No smoke, opening, rollover, pilot, network, subagents or commits authorized or executed. Existing failed database was not opened. No schema, analyzer, gate, clock policy or Chainlink table changes.

## Durable invariant fixed before production changes

Every version-2 durable observation and report must have exactly one matching durable submitted opportunity and compatible fetch event. Identity is the original run, market, slot ID/index, scheduled target and submission opportunity ID; identity cannot be reconstructed or inferred at report time. Every scheduled opportunity must have an explicit terminal disposition: completed, skipped, busy, unsubmitted or explicit interrupted/failed publication. Submitted is pending, not terminal. Duplicate, foreign, missing or mismatched slot/target/submission identity must fail closed. Publication must not expose orphan durable observations/reports or a complete fetch without its completion event. Existing SQLite transaction/event mechanism and existing single writer are the only permitted publication mechanism. Replay must remain closed on incomplete or conflicting authority. Revision-1 behavior remains unchanged.

## Independently inspected root causes

- `src/polymarket_bot/recorder/service.py:326-330`: opportunity insertion prunes non-submitted/non-completed rows at owner-now minus 60 seconds.
- `service.py:450-461`: drain prunes target membership at receipt minus 60 seconds; request receipts can extend submitted history, but skipped rows have no such extension.
- `service.py:501-506`: live validation requires `(start - 2 seconds, receipt]` and exact 2-second monotonic target intervals. These retention predicates cannot preserve all required padded skipped membership. Forensic five-row finding is consistent with inspected producer code; failed database was not mutated or reopened.
- `service.py:1016-1024`: target is projected before housekeeping, then projected again after housekeeping using active anchor. `clock.py:98-101` uses anchor midpoint plus target UTC offset; an ordinary VALID calibration can change this offset without exceeding discontinuity threshold.
- `schedule.py:13-17,20-46`: scheduler identities remain fixed UTC market slots; capture classification uses current projected server time and slot boundary.
- `store.py:270-283`: each existing save/event call starts and commits its own transaction. `service.py:406,412,437-442` publishes observations, report, fetch and completion independently. Current code permits orphan rows on interruption. Fix requires atomic existing-store publication, not merely later complete-fetch rejection.

## Frozen-policy blocker

Authoritative recorder design `docs/superpowers/specs/2026-10-10-market-data-recorder-design.md:202-215,219-239` requires refresh about every 60 seconds, active-anchor conversion of future UTC slots, current slot-boundary capture classification and active-anchor network deadlines. CP5 exact monotonic cadence is independently frozen by current instruction and `service.py:501-506` / analyzer.

Let an anchor project UTC target T to monotonic M. Consecutive UTC targets differ by 2 seconds. A VALID recalibration changing projection offset by delta gives next monotonic target M + 2 seconds + delta, not M + 2 seconds. Existing clock policy explicitly permits nonzero delta below discontinuity threshold. Freezing only an already armed target prevents that target's mutation, but next selected target still uses new anchor and introduces the same cadence transition one slot later. Freezing the entire run/market grid instead would stop mapping future targets through active anchor and can conflict with current UTC slot opening/deadline checks. Deferring calibration or ignoring its new projection also changes frozen clock policy. Treating ordinary valid calibration as a discontinuity changes frozen discontinuity policy. None is authorized here.

Decision: use user's explicit stop-with-documented-blocker option. Do not silently choose a new clock/grid contract. Required owner ruling: authorize a stable scheduling projection distinct from fresh acquisition clocks, including its interaction with UTC slot opening/end deadlines, or supply another policy-preserving scheduler contract. No relaxation of exact 2 seconds, 30 cells/15 pairs, exact 15 opportunities/28 seconds, cutoff, warmup or fallback proposed.

## TDD and fault coverage

`using-superpowers`, `systematic-debugging`, and `test-driven-development` loaded before implementation. Forensic report read first; full 375-line recovery ledger read. No implementation begun, so no RED/GREEN cycle or remediation success claimed. Existing baseline tests are not new fault-injection evidence.

Requested new regression/fault matrix remains NOT IMPLEMENTED: opportunity persistence failure; worker completion before drain; observation/report interruption; event interruption; duplicate drain; pending shutdown; skipped/busy; target change; rollover; duplicate/foreign/mismatched slot/target. Atomic ownership invariant is specified above, not yet enforced. Retention fix alone would leave cadence failure and atomic-publication defect unresolved; no partial production fix applied.

## Fresh offline verification

All commands ran from current worktree with `$env:PYTHONPATH='src'` and `.venv\Scripts\python.exe`; suites ran independently in parallel with 1,800,000 ms tool bounds.

| Command after interpreter | Observed result |
|---|---|
| `-m unittest discover -s tests -t . -q` | 690 tests, 639.680 seconds, OK |
| `-m unittest tests.test_cp5_safety tests.test_free_schema tests.test_free_reference tests.test_free_persistence tests.test_free_mode_isolation tests.test_free_metrics tests.test_free_kraken tests.test_free_engine tests.test_free_coinbase tests.test_free_cli tests.test_cp3_regression tests.test_cp3_lifecycle tests.test_cp3_review -q` | 237 tests, 113.538 seconds, OK |
| `-m unittest tests.test_recorder_service tests.test_recorder_schedule tests.test_recorder_boundary tests.test_recorder_metadata tests.test_discovery tests.test_parsing tests.test_batch_books tests.test_read_only_boundary -q` | 184 tests, 579.051 seconds, OK |

Full and focused output contains expected negative CLI argument errors and lifecycle fixtures, not suite failures. These are baseline regression results only, not proof defects fixed. Separate new targeted RED/GREEN was not run because production remediation stopped at policy blocker.

Lint N/A. No changed Python files; scoped changed-file Pyright comparison is not applicable. Historical 6e1f582 diagnostic comparison is not represented as fresh verification. No globally clean typecheck claim.

Changed artifacts: this requested report and appended ignored `.superpowers/sdd/2026-10-14-cp5-3-research-quality-plan/progress.md` only. Preexisting forensic report, review artifacts and plan preserved. Final native Git subprocess verification: `git diff --check` exit 0, empty stdout/stderr; `git diff --name-only` exit 0, empty stdout/stderr (no tracked changes). `git rev-parse HEAD` exit 0: `e9f254a5eb5251c5d99d4c9b6ee2304ee1ec2824`. Status displayed 18 untracked artifacts versus initial 17, including this report. Absolute Git shell output remained compressed, so subprocess captured exact exit codes/output for these checks. Note tracked diff check does not inspect untracked report or ignored ledger.

## Resumed remediation after independent review

Historical sections above preserved; blocker conclusion superseded by full independent review `cp5.4-root-cause-review-2026-10-15.md`, read verbatim. Frozen uncertainty/freshness/discontinuity policy does not freeze historical scheduler implementation. Prior interpretation delayed independent repairs unnecessarily. Ledger records correction, cost and safety constraints. No permission request, network, subagent, commit or failed-DB access occurred. Tasks 1–7 remain DONE; Task 9 FAIL; live phases remain stopped pending independent clean review.

### Production changes

- `service.py`: opportunity retention uses last published receipt/origin, not advancing owner-now, with preceding two-second pad. Outstanding worker delays cannot move retention horizon forward. Drain retains padded opportunities and aligned observation/context/outcome/request tuples; 128-opportunity/256-observation validation bounds retained, overflow explicitly raises. Producer now compares required durable opportunity membership in both directions before publication and rejects duplicate embedded IDs.
- `service.py`: original submitted event binds pending market/slot/index/target/scheduled identity before publication. Existing fetch/completion for submission rejects duplicate drain even if memory mapping lost. Observation/report/fetch/completion writes execute together under existing store transaction; memory mapping/history advance after commit only.
- `store.py`: transaction composition joins existing transaction instead of nested BEGIN; outer writer transaction owns rollback/commit. Recovery emits one explicit interrupted completion for each durable v2 submitted opportunity without terminal completion; no reference/completion time invented. No schema or Chainlink table changes.
- Approximate-mode scheduler now captures one nominal UTC-to-monotonic projection and preserves exact nominal cadence across valid recalibration and market identities. Active acquisition anchors unchanged. Wait reaches both nominal target and active UTC opening; existing classify_wake, clock usability, slot-end, market-end and run deadlines remain. Legacy book-only/reference-required projection behavior preserved. This explicitly changes historical active-anchor nominal scheduling, not uncertainty policy.

### Observed TDD

Before first production edit, three new regression methods ran: six expected assertion failures in 0.620s. Padded skipped opportunity vanished; omitted and duplicated authority accepted; reference/fetch/completion interruption left durable orphan rows. First-observation failure already rolled back/no rows and was a control. After repair, persistence suite 32 PASS / 4.858s.

Original pending-identity mutation regression initially observed target accepted plus fixture contamination error. Mutation fixture isolated using outer rollback; subsequent run independently observed three assertion failures for changed target, foreign market and boolean slot identity, plus downstream mapping contamination error. Guards then added; final targeted suite verifies all cases and duplicate drain with cleared mapping.

Scheduler initial fixture queried nonexistent SQLite columns twice; those errors are not RED evidence. Corrected test inspects actual `/books` transport timestamps. Before scheduling production edit, positive phase shift observed actual send `9000001372500007` before armed nominal `9000002005000007`; one expected assertion failure, 5.785s. Negative phase control passed. Stable nominal fix then passed. Test later moved to approximate-mode disposable DB and actual recorder with legacy no-op reference adapter so legacy scheduler stays unchanged; no real network.

Pending recovery regression observed zero terminal events versus expected one (one expected assertion failure / 0.280s), then recovery fix. Opportunity-write failure and completed-worker-before-drain busy behavior already passed controls, no RED claim.

### Fault coverage and honest limits

| Case | Evidence |
|---|---|
| opportunity persistence failure | injected event OSError before worker; thread absent |
| worker completed before drain | real stopped worker retains handle; new submission busy, no second accepted operation |
| observation/report interruption | real SQLite before/after rows equal on injected save failure |
| fetch/completion event interruption | before/after all three tables equal; memory mapping unchanged |
| duplicate drain | cleared mapping still rejects second acquisition owner; reference count unchanged |
| pending recovery | explicit interrupted terminal, repeated recovery creates no duplicate |
| skipped/busy/unsubmitted | existing durable disposition tests and new padded skipped retention/control |
| target/foreign market/bool slot | original submitted authority mismatch rejects |
| duplicate/omitted authority | producer validator rejects |
| shutdown | stopped worker publication and subsequent shutdown retain exactly one completed terminal |
| rollover/expired/discontinuous clocks | existing scheduler/service suites remain passing; no new dedicated approximate-mode rollover or large-shift availability test |
| revision 1 / exact mode | full replay/isolation/safety suites pass; analyzer and schema unchanged |

Coverage is not yet the complete requested adversarial matrix: pending live-worker shutdown interruption, crash/reopen recovery, foreign submitted opportunity substitution, strict typed original authority mutation, approximate-mode rollover and expiry/discontinuity combinations, and both active UTC interval/deadline actual-send bounds need stronger dedicated tests. Current phase-shift test verifies actual sends not before nominal target for both signs, but does not independently assert every active-clock UTC deadline. Do not represent baseline tests as new fault proof. General transaction composition needs independent review for nested-exception handling outside this publication path. Producer validation scans durable run events; bounded in-memory history does not imply bounded query cost.

### Fresh verification after correction

Common interpreter/PYTHONPATH and exact module lists are those above. No Python changes after final runs:

- Targeted: persistence module plus nominal-phase and discontinuity tests, **38 PASS / 12.551s**.
- Full discovery: **698 PASS / 506.338s**.
- FREE_REFERENCE/CP5/CP3 combined: **244 PASS / 81.908s**.
- Scheduler/service/identity: **185 PASS / 424.098s**.
- Earlier intermediate full run: 697 tests, one failure `test_discontinuity_requires_pause_fresh_clock_and_market_validation`; scheduler run 185 tests same failure. Stable grid mistakenly affected book-only mode; production scoped to approximate mode, original test unchanged, final runs above pass. Earlier focused 243 PASS / 93.434s. Initial Pyright found four new patch-kwargs fixture diagnostics, fixed using explicit side_effect.
- Fresh scoped Pyright against untouched `6e1f582` archive: all four changed Python files, identical interpreter/options, current **132** errors, baseline **134**, zero warnings/informations. Unchanged-line mapped exact identities: **added []**, two removed service optional-anchor member diagnostics. Not globally typecheck-clean. Raw JSON/full comparison retained at `C:\Users\ANDROS~1\AppData\Local\Temp\opencode\cp54-w5un9nmj`; runnable comparison `cp54_compare.py` in approved temp. Baseline read-only git archive, no workspace checkout/history changes.
- Lint N/A. Git diff check executed after tests; final explicit exit verification follows.

Changed Python: `src/polymarket_bot/recorder/service.py`, `src/polymarket_bot/recorder/store.py`, `tests/test_free_persistence.py`, `tests/test_recorder_service.py`. Report and ignored ledger appended. No new subsystem/schema/comments, analyzer/gate/threshold/uncertainty edits, commits or failed database edits. Status: offline partial remediation implemented and baseline tests passing; required fault-matrix completion and independent review remain outstanding. No live authorization or smoke success claimed.

Final native Git subprocess: `diff --check` exit 0, stdout/stderr empty; `diff --name-only` confirms exactly four Python files above; HEAD remains `e9f254a5eb5251c5d99d4c9b6ee2304ee1ec2824`. Report/ledger append follows test verification only; Python unchanged.

## Fix round 1 — independent implementation review

Full `cp5.4-implementation-review-2026-10-15.md` read, including all required reproducible regressions. Historical evidence above preserved. Exact findings:

> H1 — stop/interrupt recovery terminalizes live acquisition before shutdown drains it
>
> H2 — original-submission guard does not bind result acquisition owner or strictly validate original authority
>
> H3 — nested store transaction error can be swallowed and partial writes committed
>
> H4 — mandatory complete fault/scheduler safety matrix remains unmet
>
> M1 — producer authority validation adds run-length scans to every publication

Full finding text remains verbatim in unchanged independent review lines28–78; this section quotes finding titles, not a replacement transcription of all findings.

### Observed RED before distinct production changes

- H3: two new test methods, four expected assertion failures / 0.153s: caught nested IntegrityError, RuntimeError and KeyboardInterrupt committed instead of failing outer transaction; nested publication accepted. Fix: nested exceptions mark outer transaction rollback-only; outer rethrows original exception, converts SQLite failure and latches writer. Research publication explicitly rejects caller-owned transaction before writes. GREEN two tests / 0.155s. Propagated/caught ordinary/BaseException cases checked. Existing mutation tests now reject nested publication rather than depending on it.
- H2 original metadata: first fixture version mutation unexpectedly published, then contaminated later assertions. Isolated `_publish_research` mutations under rollback; accepted RED two expected failures (version1 and embedded foreign run) / 0.219s. Added strict original version/run and integer identity validation. Stronger same-run replacement fixture includes replacement in scheduled authority so reverse-membership rejection cannot mask owner bug: one expected assertion failure / 0.183s. Added current observation outcome/expected-request owner and provider attempt run/opportunity/slot/target/scheduled binding. GREEN / 0.194s. Attempt target omission in old identity-free fixture exposed separate producer omission; worker now captures resolved nominal target even when caller omitted optional target argument. Valid rolling-history controls pass existing replay tests.
- H1: first reproduction had expected duplicate-publication ValueError plus closed-store fixture errors. Corrected fixture converted unexpected ValueError into assertion and reopened between cases: four expected assertion failures / 0.284s across stop/interrupt and live/completed-undrained worker. Fix coordinates slot recovery separately (`recover_research=False` before join), drains worker, then research recovery; never publishes after interrupted terminal. Real Events/Timer and worker used, real session-produced result. GREEN four cases / 0.945s. First GREEN attempt exposed missing fixture finalized field, corrected test only. Initial full/focused run later failed existing `test_stop_and_interrupt_recovery_errors_survive_failed_join`; slot recovery restored before join with research recovery deferred, preserving original error priority and retained writer ownership.
- M1: 600 same-run 4KiB audit events caused 1208 Python JSON decodes, expected <40: one expected failure / 5.257s. SQL now selects opportunity target interval and exact duplicate submission owner before returning JSON, including old duplicate terminal controls through existing duplicate-drain regression. GREEN persistence suite41 / 11.975s. Important limit: SQLite still scans JSON predicates without new index/schema; bounded returned/decoded rows is proven, bounded SQLite scan work is not. No quadratic-work elimination claim.
- H4 stopped-worker submit: expected unsubmitted but durable submitted, one expected assertion failure / 0.097s. Check stopped state before durable submission; completed handle remains busy. Real scheduler transport matrix initially used invalid synthetic anchors (inconsistent server seconds/midpoint), rejected by existing store invariants. Corrected fixture shifts all monotonic request/midpoint fields together without changing RTT uncertainty, advances until anchor usable. These fixture failures are not production RED evidence. Actual provider/book tests pass both ±632500us and ±2500000us phases.

### Runnable fault checks added this round

Existing test files only, no comments/schema/subsystem/gate/uncertainty edits:

- `test_nested_transaction_failures_never_commit_caught_inner_writes`: caught/propagated SQLite, ordinary exception and BaseException, rollback/latch.
- `test_nested_publication_rejected_before_rows_or_memory_change`: nested caller transaction rejected, valid standalone control.
- `test_current_acquisition_owner_and_original_types_are_bound`: version/run/bool/target/scheduled authority, same-run replacement owner, attempt owner fields; all three durable tables unchanged for original mutations.
- `test_stop_and_interrupt_drain_live_and_undrained_acquisitions`: four deterministic lifecycle cases, explicit exactly-one completion, run exit, closed store/no worker handle; provider acquisition itself is completed first, then result held behind real worker barrier. Not a provider blocked inside actual book transport guard.
- `test_live_worker_timeout_retains_store_and_thread_ownership`: zero-time join retains live thread/store and rejects redispatch; release/join cleanup.
- `test_after_insert_faults_reopen_without_orphans`: OSError/SQLite failure after each of five successful inserts (two observations, reference, fetch, completion), close/reopen equality, idempotent interrupted recovery without fake reference/time.
- `test_commit_failure_rolls_back_rows_and_keeps_memory`: injected SQLite commit failure through connection proxy; writer latched, reopened rows unchanged.
- `test_stopped_submit_never_leaves_unexplained_pending_owner`: stopped worker classified unsubmitted.
- `test_deadline_clock_and_overflow_returns_are_explicit`: missing/discontinuous clock, deadline equality, explicit retention overflow.
- `test_real_research_nominal_grid_transport_guards_and_rollover`: real session/providers/worker, MockTransport retries, actual Coinbase/Kraken and book starts >= nominal and active opening, < active slot end/run deadline; persisted unique exact2s targets across two market identities, one session origin, skipped opportunities retained. Completed-undrained provider worker joined before book; pending rollover separately checked.
- `test_pending_rollover_preserves_original_owner_and_fresh_run_grid`: Event-held result across next-market busy opportunity, original owner unchanged, origin stable, adjacent nominal projection continuous. Does not independently instantiate second fresh run.
- `test_padded_dispositions_remain_aligned_through_delayed_drain`: forty real cycles then old skipped/busy/unsubmitted pad retention under delayed owner clock, aligned IDs/contexts/outcomes/expected requests.
- `test_authority_queries_do_not_decode_pilot_history`: deterministic decode count, no wall-time benchmark.

Mandatory matrix now has more runnable checks, but exact H4 acceptance is not proven in full: active-provider interruption at actual book before-attempt, expiry at every housekeeping/reserve/drain boundary, metadata invalidation, explicit opening/end equality for all transport types, fresh-run projection reuse, and delayed-window exact replay with inserted dispositions still lack dedicated checks. SQL performance also retains underlying run-length predicate scans. H4 and strict M1 closure must not be marked complete based on passing suites. No narrow external blocker established; implementation remains unaccepted, not policy-blocked.

### Fresh final verification

No Python edits after final runs:

- Persistence module: 48 PASS / 14.313s (includes latest two rollover/retention checks).
- Persistence plus isolation before latest two checks: 56 PASS / 24.447s.
- Full unittest discovery: **711 PASS / 390.722s**.
- Documented FREE_REFERENCE/CP5/CP3 combined command: **256 PASS / 76.424s**.
- Documented scheduler/service command after final production edits: **186 PASS / 505.912s**; latest two additions only in persistence, covered by full run.
- Prior intermediate full709 / 567.619s and focused254 / 99.682s each failed existing recovery-error-priority test; fixed and rerun. Subsequent full709 /568.510s, focused254 /91.257s passed before final two tests.
- Fresh four-file Pyright vs untouched6e1f582: current132/baseline134 errors, warnings/informations0, exact unchanged-line-mapped added identities[], two removed service optional-anchor diagnostics. Initial run introduced11 test typing diagnostics, all corrected. Raw JSON/comparison retained approved temp `C:\Users\ANDROS~1\AppData\Local\Temp\opencode\cp54-h374on23`; same `cp54_compare.py` command. Not globally clean.
- Lint N/A. Final diff check recorded separately below.

Tasks1–7 DONE; Task9 historical FAIL. No failed-DB access, network, subagents, commits, reset/restore/clean or live phase. Same four Python files changed plus report/ignored ledger. Fresh reviewer required; no claim all four High/one Medium closed.

## Round 1 continuation — bounded authority and safety checks

Review and prior report reread. No network/subagents/commits/failed-DB access. Same four Python files changed; policies/schema/analyzer/revision1 unchanged.

M1 production queries now use recorder_events INTEGER PRIMARY KEY ranges: read at most4097 IDs, fail closed above4096, then JSON-filter only that bounded range. Membership lower event boundary advances only after committed retention to oldest retained opportunity. Duplicate check begins at original submission ID, not receipt horizon, so old duplicate publication never silently escapes: too-old replay fails explicit bound. No index/schema/new subsystem. This bounds SQLite VM work independently of total historical run size, at cost of explicit unavailable publication when outstanding relevant range exceeds4096 events. Query bounds are evidence limits, not truncated membership acceptance. Retention floor relies on existing single-writer chronological scheduler; no external database mutation support claimed.

Observed RED: old VM-step test counted8959 steps against5000. Initial bounded implementation uses14996 steps for600 events (extra bounded ID pass); 5000 was not a legitimate requirement on current-window600 events. Revised tests distinguish current bounded window (<20000 VM steps), overflow4100 events (<30000 with explicit failure/no rows), and600 historical4KiB payloads before current authority (<2500 steps). All passed. Do not describe revised threshold as same-threshold performance improvement. Stronger old-history test directly proves old payloads excluded by rowid boundary.

Submission safety regression observed four expected assertion failures /0.132s: before nominal, before active UTC opening, at active slot end and at run deadline were accepted by direct submit. Added owner pre-submission guards using existing active clock and min(run, caller, active slot end) deadline. GREEN /0.135s. Existing busy fixture used future target and now correctly classified unsubmitted; corrected busy control to eligible target rather than weakening guard.

Worker submit(False) after durable submitted event regression observed zero terminal versus expected1 /0.089s. Producer now emits explicit interrupted/WORKER_REJECTED completion without reference or invented completion time. Stopped-state precheck remains unsubmitted. Final suites cover GREEN.

Additional runnable checks:

- `test_actual_book_guard_stops_with_provider_transport_pending`: real session/provider transport blocks on Event; actual PublicGet book before-attempt guard raises stop/KeyboardInterrupt; Timer releases provider before join timeout. Eight shift/signal cases, exactly one completed atomic terminal, store closed/thread cleared/client explicitly closed. Targeted PASS1 /81.570s; later final suites include it. Existing completed-undrained and timeout matrices retained.
- `test_research_housekeeping_faults_have_no_unowned_fetches`: metadata unavailable, revision identity removal, expiry in housekeeping/wait/reserve/drain, discontinuous anchor. Known submissions equal terminal/fetch owners; no fabricated fetch. Reserve/drain expiry correctly returns FAILED/time_unavailable (test initially expected graceful STOPPED; expectation corrected, no production relaxation). Unknown schedules remain unknown.
- `test_submission_requires_nominal_and_active_utc_opening_and_run_budget`: negative boundary checks above.
- `test_opening_equality_accepts_but_end_and_run_equality_never_send`: real submit accepts nominal/UTC equality; expired run boundaries leave no worker/new pending owner.
- `test_fresh_recorder_projection_and_expired_drain_keep_captured_owner`: separate recorder projects fresh99s rather than previous20s, no inherited pending identity; current clock expiry does not invalidate already captured result during durable drain.
- `test_duplicate_actual_drain_preserves_every_row_and_memory`: actual repeated empty drain leaves all three tables/memory exact; accepted two-cycle control replays exactly.
- `test_authority_range_is_bounded_and_old_duplicates_fail_closed`: range overflow before observation writes and memory advancement. Existing duplicate-after-cleared-mapping and padded omitted authority controls retained.
- `test_old_history_does_not_increase_authority_vm_work`:600 prior4KiB events excluded from current publication query VM budget.

Final verification after all Python edits (same documented interpreter/PYTHONPATH/module lists): full720 PASS947.841s; FREE_REFERENCE/CP5/CP3 combined263 PASS236.911s; scheduler/service188 PASS823.180s. Scoped Pyright four changed Python files current132/baseline134, zero warnings/informations, added exact mapped identities[], two removed optional-anchor diagnostics. Raw JSON/comparison approved temp cp54-9rhn0ezu. Earlier full718/focused261/scheduler188 passed before latest controls; one new optional worker diagnostic fixed with assertion, final comparison clean relative to baseline. Lint N/A.

Acceptance honesty: H1/H2/H3 and bounded-query M1 have concrete fixes/regressions. H4 has broad runnable coverage but strict reviewer requested delayed-window exact replay with inserted skipped/busy/unsubmitted dispositions, completed-undrained rollover under changed market metadata, all provider retry deadline-equality cases and every metadata invalidation/no-target branch are not individually established by these checks. Therefore DONE/all-findings-closed is not claimed. No external blocker proved. Tasks1–7 DONE, Task9 historical FAIL; independent review/live escalation not granted.

## Fresh implementer H4 closure — 2026-10-15

Prior remaining-gap statement superseded by concrete checks below. No new production behavior needed: new tests exercise existing contracts and pass after fixture corrections. No production RED or new production fix claimed. Initial errors were fixture errors: rollover inserted a next-market opportunity at the old receipt target, then queried absent optional identity keys; fixed next-grid target and supplied original market/index metadata. No-target patch initially leaked into later subcases; fixed per-run cleanup. Unsaved synthetic changed market initially continued into capture; now stops after identity-invalidating housekeeping and verifies no old-market submission. Initial Pyright added three fixture diagnostics; explicit thread assertion and initialized patch handle remove all three.

### Remaining checks now runnable

- `test_delayed_window_dispositions_replay_exactly`: forty nominal cycles with skipped/busy/unsubmitted at indices9/10/11; each completed worker drains with owner clock advanced200 seconds. Final durable fetch retains all three dispositions at padded window boundary; analyzer `replay_rows` is exact across all accepted reports. Post-drain observation IDs/context lengths, durable mapping, outcomes/expected requests and retained dispositions agree. This exercises insertion pruning and receipt-driven drain pruning, not merely set retention before publication.
- `test_completed_undrained_rollover_binds_changed_market`: real worker completes but remains untaken; metadata revision removed, changed-market next-grid submission is busy; original pending market/index/target/scheduled identity unchanged. Drain publishes original owner, preserves session origin, exact replay passes, adjacent projection remains2 seconds. Optional slot_id remains absent rather than invented. Existing real scheduler rollover check supplies real reserved slot IDs and two persisted market grids; pending-rollover check supplies live-worker counterpart; fresh-recorder control proves no projection reuse.
- `test_provider_retry_deadline_equality_never_sends`: Coinbase and Kraken, each crossed with429/ReadTimeout/ConnectError and initial equality, response equality, exact100ms remaining backoff, retry-send equality and retry-receipt equality. Counts are0/1/1/1/2 respectively; every actual send strictly precedes deadline; all rejected observations have no price. Retry response equality is explicit DEADLINE_EXPIRED. Exact100ms remaining correctly does not retry; no new error-code requirement invented.
- Strengthened `test_research_housekeeping_faults_have_no_unowned_fetches`: metadata-not-ready must emit known unsubmitted/METADATA_NOT_READY; changed market after housekeeping must not submit old market; forced legitimate no-target selection must make zero provider/book sends and zero submitted owners. Existing missing revision, expiry, discontinuity, wait/reserve/drain cases remain. Existing `test_restart_of_invalidated_current_market_keeps_pause_until_rollover` supplies real metadata identity conflict, durable INVALIDATED lifecycle and later-market recovery; not misrepresented as new approximate fault injection.

### Complete minimum reviewer matrix mapping

| Reviewer requirement | Concrete runnable evidence |
|---|---|
| H4.1 phases, large shifts, real transport/retries, nominal cadence, active bounds | `test_real_research_nominal_grid_transport_guards_and_rollover` four shifts±632500/±2500000us; actual provider/book sends, unique exact2s persisted targets, two markets; opening/end/run controls in `test_submission_requires_nominal_and_active_utc_opening_and_run_budget`, `test_opening_equality_accepts_but_end_and_run_equality_never_send`; provider retry equalities above |
| H4.2 expiry/recovery/metadata/missed/no-target/submit rejection/overflow | strengthened housekeeping matrix above; existing discontinuity recovery service tests; `test_restart_of_invalidated_current_market_keeps_pause_until_rollover`, `test_metadata_overrun_skips_passed_slots_and_clamps_http`; stopped/rejected worker tests; `test_deadline_clock_and_overflow_returns_are_explicit`, bounded-authority overflow tests |
| H4.3 pending and completed-undrained rollover, fresh run | new completed-undrained test; `test_pending_rollover_preserves_original_owner_and_fresh_run_grid`; real scheduler two-market transport check; `test_fresh_recorder_projection_and_expired_drain_keep_captured_owner` |
| H4.4 crash/publication boundaries/SQLite/commit/idempotent recovery | `test_after_insert_faults_reopen_without_orphans`, `test_commit_failure_rolls_back_rows_and_keeps_memory`, `test_pending_opportunity_recovery_is_explicit_and_idempotent` |
| H4.5 lifecycle/authority/nesting/repeated drain/delayed exact replay | actual-book stop/interrupt pending transport matrix; live/undrained lifecycle and timeout tests; current-owner/original-type and duplicate authority tests; nested transaction/publication tests; `test_duplicate_actual_drain_preserves_every_row_and_memory`; new delayed exact replay above |

No speculative cross-product beyond reviewer minimum added. H1/H2/H3/M1 prior fixes/evidence remain; H4 remaining coverage work DONE, subject to required fresh independent review, not live acceptance. Tasks1–7 DONE; Task9 historical FAIL unchanged.

### Fresh final evidence

All Python edits preceded these final runs. Interpreter `.venv\\Scripts\\python.exe`, `PYTHONPATH=src`; exact full/focused module commands unchanged from prior report sections.

- Targeted persistence:58 PASS /67.300s; expanded housekeeping matrix also passed in prior targeted59-test run except then-unfixed rollover fixture KeyError, subsequently fixed and covered by persistence/full runs.
- Final full discovery: **723 PASS /985.289s**, explicit subprocess exit0. Expected negative CLI errors/lifecycle fixtures only.
- Final documented FREE_REFERENCE/CP5/CP3 combined: **266 PASS /238.834s**.
- Final documented scheduler/service/identity: **188 PASS /863.114s**.
- Final four-changed-file Pyright against untouched `6e1f582`, same existing harness/options/interpreter: current132/baseline134 errors, warnings/informations0; exact unchanged-line mapped **added[]**, two removed optional-anchor diagnostics. Not globally clean. Raw JSON/comparison: `C:\Users\ANDROS~1\AppData\Local\Temp\opencode\cp54-dnb5ovnq`; harness `cp54_compare.py` unchanged.
- Lint N/A per existing ledger. `git diff --check` ran after final Python edits with no output; explicit exit check follows documentation append.

This takeover changes only two existing test files plus requested report/ignored ledger append. Prior production diff preserved; no gate/uncertainty/replay/schema edits, network, subagents, commits, destructive Git, failedDB access or live smoke. Fresh independent review required before any smoke.

Final native subprocess evidence after report/ledger append: `git diff --check` exit0, stdout/stderr empty; `git diff --stat` confirms same four tracked Python files, no additional production files. HEAD unchanged `e9f254a5eb5251c5d99d4c9b6ee2304ee1ec2824`. Untracked report/ignored ledger are outside tracked diff check.
