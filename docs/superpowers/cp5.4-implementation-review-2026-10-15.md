# CP5.4 independent implementation review — 2026-10-15

## Verdict

**SPEC compliance: FAIL (partial remediation). Quality: FAIL; not ready for clean acceptance or live escalation.** Four High findings and one Medium finding remain below. Full minimum fault matrix is mandatory, not waived. Tasks 1–7 remain DONE; Task 9 remains FAIL. No smoke, opening, rollover or pilot authorized by this review.

Review inspected actual working-tree native Git diff and full current changed-file context, remediation report, forensic report, root-cause review and all 395 ledger lines. Git command output was compressed by environment even with absolute native executable and subprocess capture; complete added test bodies were independently read directly, rather than treating compressed diff as complete evidence. No production/test edits, subagents, network, commits or failed-DB access. Only this requested review document written. Failed DB not opened, hashed, repaired or reinterpreted.

Accepted verification evidence, not rerun: remediation report lines 90–104 records full 698 PASS, focused 244 PASS, scheduler 185 PASS, targeted 38 PASS; scoped current132/baseline134 Pyright exact mapped added identities [] with two removed diagnostics. This is not globally clean typecheck. Lint N/A per ledger line9. Review ran one isolated SQLite `:memory:` transaction reproduction described below; no full suite rerun.

## Ten-area assessment

| Area | Assessment |
|---|---|
| Exact five reconstruction | Consistent with forensic and root-cause evidence; historical, not freshly queried here. Fetch600/opportunity598/reference26/observations51,52 omits skipped277; 694/615/28/55,56 omits278; 699/697/29/57,58 omits279; 708/704/30/59,60 omits280; 713/711/31/61,62 omits281. Forensics lines27–69; root review65–75. Not five lost fetches: all ten provider attempts HTTP200; 57 fetches/completions/references and114 observations. Remaining52 membership matches are not52 certified replays. |
| Root cause | Correct: two pruning sites discarded padded skipped authority; active-anchor phase change independently created1,367,500,000ns transition instead of2,000,000,000ns. Separate atomicity defect was not historical five-row cause. Forensics15–17,61–73,95–101. |
| Durable invariant | Correctly declared in remediation9 and ledger381, only partially enforced; H2/H3 below. |
| Atomicity | Standalone publication now encloses observations/reference/fetch/completion in one transaction (service397–400,449–485), memory retained afterward. Nested transaction semantics remain unsafe (H3). |
| Replay | Analyzer unchanged; reverse membership and completion barrier remain fail closed (scripts/cp5_analyze.py:195–226,293–310). Producer reverse check now matches required interval (service365–384). Producer can still commit authority replay rejects (H2). |
| Scheduler semantics | Approximate-only immutable projection preserves nominal spacing (service304–310); wait uses max(nominal, active opening), active usability and classify_wake remain (1070–1084); slot/market/run network deadlines remain (1100,1121–1124). No demonstrated blanket early-send bug from nominal projection alone. Required actual-send safety matrix unproved (H4). |
| Lifecycle/failure | Completed handle remains busy until take; duplicate drain rejected by durable event scan even if mapping cleared (service417–421; tests386–410,426–440). Recovery-before-final-drain conflicts with new terminal barrier (H1). |
| Revision1 | Legacy tuple drain/event shape untouched (service281–288), nominal grid scoped away from legacy modes. Shared store transaction/recovery changes still need negative lifecycle and composition controls; passing baseline evidence not exhaustive proof. |
| Unchanged gates | Diff contains only service/store/two test files. No analyzer/schema/clock/free_reference/gate edits. Exact2s,30cells/15pairs, exact15-opportunity/28s, cutoff, warmup, fallback and acquisition uncertainty policy not relaxed. Stable projection explicitly changes historical scheduling implementation, as permitted by root review15–27. |
| No fabricated provenance | Report distinguishes missing membership from absent fetches and admits incomplete coverage (remediation88,102; ledger395). Recovery does not invent reference IDs or completion timestamps. No historical success inferred. No new failed-DB reconstruction claimed here. |

## High findings

### H1 — stop/interrupt recovery terminalizes live acquisition before shutdown drains it

**References:** src/polymarket_bot/recorder/service.py:881–897,938–947; src/polymarket_bot/recorder/store.py:1276–1290; service.py:417–421; tests/test_free_persistence.py:442–456.

Both `_StopRequested` and `KeyboardInterrupt` call `recover_interrupted(...finish_runs=False)` before `_shutdown`. New recovery writes interrupted completion for every submitted opportunity lacking a completion, even if its worker is alive or already completed but undrained. `_shutdown` then joins and drains that result; `_publish_research` sees recovery's terminal event and raises `ValueError('Duplicate research publication.')`. Those stop handlers catch storage/OSError, not ValueError. Normal stop/interrupt can escape intended RunResult, omit SHUTDOWN/finish_run and discard completed acquisition publication. Existing shutdown test starts from an already drained fixture, so does not cover this ordering.

**Required regression:** disposable v2 store, real session/worker and deterministic Events: stop at actual book before-attempt guard while provider worker is pending; repeat with worker completed but undrained and KeyboardInterrupt. Release worker before join timeout. Assert documented choice of completed atomic publication or interrupted terminal exactly once, no second terminal, no uncaught ValueError, correct run state/exit result, closed store/client and no retained thread. Separate timeout control must retain live-worker/store ownership without pretending shutdown completed. Coordinate recovery and draining rather than blindly allowing publication after interrupted completion.

### H2 — original-submission guard does not bind result acquisition owner or strictly validate original authority

**References:** src/polymarket_bot/recorder/service.py:406–421,433–443,468–485; service.py:534–565; scripts/cp5_analyze.py:195–225,325–334; tests/test_free_persistence.py:386–410.

Guard compares durable original with mutable pending identity, but never requires pending opportunity ID to equal owner of current result's expected requests/outcomes/attempts. Replacing `_research_pending[0]` with another same-run submitted event carrying identical identity can pass guard; unchanged result still owns old opportunity, while fetch/completion get replacement ID. Attempt checks validate timing only, not opportunity/run/slot binding. This publishes conflicting authority that replay rejects. Foreign run event is checked, but same-run foreign submission substitution is not. Also strict integer checks apply only to pending identity: original `slot_id=True` compares equal to pending1; original version/detail run ID are not checked here. Analyzer rejects those original authority mutations, so atomic commit alone does not enforce producer invariant.

**Required regression:** intercept first publication before any writes. Create second durable same-run submitted opportunity with identical target/identity and substitute only pending ID; assert rejection with exact unchanged observation/reference/event rows and memory. Independently mutate original JSON slot_id/slot_index from1 toTrue, version2 to1 and embedded run_id to foreign; keep pending/result unchanged. Add changed slot index/ID/scheduled_us and provider-attempt owner mutations plus valid controls. Require current two observations, expected requests, outcomes, attempts and completion to share original immutable submission owner; compatible rolling history must remain legal. Run replay on accepted controls.

### H3 — nested store transaction error can be swallowed and partial writes committed

**References:** src/polymarket_bot/recorder/store.py:270–286; src/polymarket_bot/recorder/service.py:397–400; tests/test_free_persistence.py:396–401.

New in_transaction branch yields outside rollback/latch handling. If caller catches nested sqlite3.DatabaseError, outer context sees success and commits writes preceding failure; `_write_failed` stays False. This weakens shared store failed-writer contract. Same branch also means `_persist_research` inside an existing outer transaction advances mapping/history at line400 before actual outer commit; subsequent outer rollback leaves memory ahead of durable authority. Current mutation fixture deliberately raises after nested call, so does not prove caught-error safety or successful nested publication rollback.

**Fresh reproduction observed:** construct RecorderStore via `__new__`, attach SQLite `:memory:` connection, writable flags and dummy lock; CREATE TABLE probe(id INTEGER PRIMARY KEY). Enter outer `_transaction`; inside nested `_transaction` insert1 twice; catch sqlite3.DatabaseError inside outer scope. After exit, `SELECT * FROM probe` returned `[(1,)]`; `_write_failed` printed False. No failed DB used.

**Required regression:** persist this exact caught-inner-integrity-error case using disposable real store and assert no partial publication/commit, correct storage_failure conversion and stopped-writer latch according to existing contract. Cover BaseException/ordinary exception semantics explicitly, propagated and caught cases. Add successful nested research publication followed by outer rollback: durable tables and memory must both remain prepublication, or reject nested publication before writes if unsupported. Preserve single writer; use explicit ownership/rollback-only semantics or appropriate savepoint contract, not silent join with early memory advancement.

### H4 — mandatory complete fault/scheduler safety matrix remains unmet

**References:** docs/superpowers/cp5.4-remediation-report-2026-10-15.md:71–88,102; .superpowers/sdd/2026-10-14-cp5-3-research-quality-plan/progress.md:395; tests/test_recorder_service.py:67–98; tests/test_free_persistence.py:333–456; service.py:516–566,990–1104.

Report honestly acknowledges omissions, but user requires full minimum matrix, not optional follow-up. Phase-shift test uses legacy no-op adapter, only observes three `/books` sends and asserts lower bounds relative to first planned target; it does not assert persisted v2 exact targets or Coinbase/Kraken transport sends against active openings/ends/run deadlines. No dedicated approximate rollover, expiry/discontinuity/large-shift combinations, crash/reopen or genuinely pending shutdown test closes required coverage. Existing baseline tests cannot substitute these new contract tests.

**Required reproducible regressions (all offline, disposable DB/MockTransport, deterministic clocks/barriers):**

1. Both ±632,500us valid phase shifts and larger permitted shifts: persist real v2 nominal targets exactly2,000,000,000ns apart; capture active anchor and actual transport entry for all provider/book attempts including retries. Assert send >= nominal target and active slot/market opening; send < active slot end, market end and run deadline. Boundary equality controls: opening allowed; end/deadline forbidden. Incompatible intervals must produce honest non-fetch disposition, not shifted/relabelled nominal targets or fabricated coverage.
2. Expiry during wait/housekeeping/reserve/drain, discontinuity pause and fresh recovery, metadata unavailable/change, market identity invalidation, missed/overrun and no-target branches. Enumerate existing returns/continues at service517–525,535–538,566,995–1034,1052–1064,1070–1092: distinguish legitimate unknown schedules from known opportunities; no unexplained submitted owner or invented fetch. Test stopped worker submit(False) after durable submission as well as busy completed handle. Preserve bounds and fail closed on overflow.
3. Approximate-mode market rollover while acquisition pending and while completed-undrained: original market/slot/index/target ownership unchanged; next market UTC identities correct; run-long nominal projection continuous across adjacent grids, no reset of origin/warmup, duplicate scheduling, catch-up fetch or early send. Separate fresh-run control must not reuse prior run authority/projection.
4. Crash/close/reopen after durable opportunity and each publication boundary: fail after first observation, second observation, report, fetch, completion and commit. Assert all-or-none rows and one acquisition owner; explicit idempotent interrupted recovery, no fabricated completion time/reference. Include SQLite DatabaseError and commit failure, not only pre-method OSError. Existing first-observation injection is control before any insert, not interruption after first successful insert.
5. H1 pending shutdown/interrupt matrix, H2 duplicate/foreign/mismatched authority matrix and H3 nested error cases. Repeated actual drain must preserve all table rows and memory, not just reference count. Delayed worker receipt window must retain padded skipped/busy/unsubmitted membership through drain, exact reverse membership/replay and aligned observation/context/outcome/expected-request retention at both pruning sites.

Run focused affected modules plus documented regression commands after fixes; full698 and scoped baseline comparison accepted for this reviewed snapshot only. No live data needed to meet this offline matrix.

## Medium finding

### M1 — producer authority validation adds run-length scans to every publication

**References:** src/polymarket_bot/recorder/service.py:367–371,417–421; src/polymarket_bot/recorder/store.py:295–302; remediation report88.

Each result now materializes and JSON-decodes all same-run events twice, including earlier large fetch-evidence JSON, once inside write transaction. Total publication work grows quadratically with run history; bounded128 opportunities/256 observations do not bound this query cost. Single writer/drain delay can cause processing overruns and lose required availability during1200-second run, especially with book/audit events. This is quality/performance risk, not measured proof of a particular pilot timeout.

**Required regression:** seed disposable same-run historical events/fetch evidence at intended pilot scale; measure examined/decoded rows or instrument validator selection, not flaky absolute timing. Confirm publication membership/duplicate checks inspect bounded relevant authority rather than every prior large payload; preserve exact reverse membership and cross-history duplicate rejection. Use existing SQLite/event mechanism and no new subsystem/schema unless separately authorized. Include old relevant boundary opportunity and old duplicate completion controls so optimization cannot silently narrow safety checks.

## Positive changes and closure boundary

Receipt/origin-driven padded pruning now avoids owner-now eviction; both pruning sites use exclusive lower bound consistent with analyzer (service334–342,498–510). Required reverse set equality exists before writes. Standalone fault injection checks all three tables and mapping (tests356–384); duplicate publication scan survives cleared mapping. Recovery records interrupted rather than inventing success. Stable nominal schedule and fresh acquisition clocks remain distinct; active guards are still present, not proven complete by current test.

Old root-cause findings are therefore partially addressed, not simply repeated as unchanged defects. Original atomicity finding is repaired for standalone uncaught write failures; nested/lifecycle paths remain open. No analyzer relaxation or failed-dataset repair requested. Report's partial-status honesty is accepted; mandatory acceptance criteria are not satisfied by acknowledging missing tests.

**Acceptance requires H1–H4 and M1 closure with reproducible regressions and fresh independent review. Task9 historical FAIL remains unchanged even after offline remediation passes.**
