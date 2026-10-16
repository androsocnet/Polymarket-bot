# CP5.5 independent whole-change review — 2026-10-16

## Verdict and boundary

**H1 / M1. Not accepted. Offline only; no rollover, pilot or live authorization under any condition.**

Review covers the two requested issues: bounded `invalid_metadata` evidence and final audit ownership. No production/test edits, network, live execution, subagents, staging or commits. Only this requested document created. Historical evidence databases were not opened or modified. Disposable databases created only by existing targeted tests.

HEAD verified `9d26a7dd56a25daf0af3d02b6ad6f38b25c95fc0`. Actual HEAD diff lists 16 tracked Python files. Read latest checkpoint continuation A–S, historical research sections, opening supplement, Task8 report and ignored SDD progress ledger. HEAD is not the pre-CP5.5 baseline: existing Task8 changes must not be attributed to CP5.5. Frozen pre-CP5.5 context is ledger/checkpoint diff hash `8d81ad77b99a0495e0054f277bfb0fac7a1ee7b571e46c5681c3ca156f5be527`; current reported hash `8700873cad86ef7a5c613cffd53e0b48206c2f1d4a0c5cc41aa0fe2f71cb187b`. No retained machine-readable pre-CP5.5 patch was independently reconstructed here; attribution uses historical source maps and documents, not an invented Git commit.

## Findings

### H1 — Normal final audit ownership still precedes worker completion for reference mode

**Locations:** `scripts/cp5_run.py:84-89`; `src/polymarket_bot/recorder/service.py:1211-1224` and `1184-1206`.

CP5Recorder normal `_shutdown` flushes audit before base shutdown stops workers. Approximate mode has a second audit flush through overridden `_approximate_drain`; reference mode calls `_reference_drain` with no equivalent final audit flush. A completed audit appended during reference worker stop therefore remains unpersisted while shutdown finishes successfully. The new `_final_persistence` barrier is used only in the MarketDataError branch, not by normal shutdown. This violates requested all-normal/all-exception ownership requirement; not a claim that historical approximate opening followed this reference-mode path.

**Independent offline reproduction:** instantiate CP5Recorder without running acquisition; seed audit attempt1; use writable Store Mock collecting `record_event` details; set `approximate_worker=None`, reference worker `stop()` appending completed attempt2, `_reference_drain` no-op, `_flush_timing` no-op; patch `take_checkpoint`; call real `_shutdown('STOPPED','STOP_REQUESTED')`. Observed pending IDs `[1,2]`, persisted audit IDs `[1]`; no exception. This exercises actual shutdown dispatch/order, not real reference transport. Existing real-store exceptional test covers a different branch and cannot establish normal-mode completeness.

**Acceptance:** one owner barrier after both workers have stopped and all completed sends are observable, before terminal finish/close, on every successful normal termination including reference/no-worker configurations. Add disposable real-store normal-stop/interrupt tests with completion during join, exact ID-set equality and uniqueness after read-only reopen. Preserve failed-writer no-write rule and explicit persistence failures. Verify normal shutdown preflush failure does not invoke recovery/finish/checkpoint persistence after writer failure: current `finally: super()._shutdown(...)` still invokes those methods, although real store guards can reject them. Do not substitute a blanket finally flush.

### M1 — 1 MiB check budgets fragment wrapper, not persisted arm/activation JSON

**Locations:** `src/polymarket_bot/recorder/metadata.py:152-155`; `src/polymarket_bot/recorder/service.py:929-937,967-974`; `tests/test_recorder_metadata.py:133-157`.

Acquisition accepts `json.dumps({'event': event, 'list_evidence': descriptors})` at exactly1,048,576 bytes. Arm/activation add type, identity, boundary, timestamps and other envelope fields afterward. `RecorderStore.record_event` (`store.py:914-933`) has no total event-byte ceiling. Thus accepted evidence can produce persisted event JSON above frozen same1MiB existing-event contract. New boundary test measures only the inner wrapper, so its equality PASS does not prove durable bound.

**Independent size reproduction:** construct event/list_evidence wrapper padded to exactly1,048,576 bytes with default `json.dumps`; add arm envelope with type PREPARED_ARM, boundary900000000, predecessor1001, identity2002/1002, preparation/validation/arm/target/deadline/cutoff fields. Observed **1,048,843 bytes**. Actual full identity adds more. This is serializer arithmetic backed by production persistence path, not a claim this synthetic envelope was inserted into historical DB.

**Acceptance:** enforce bound on complete serialized durable PREPARED_ARM and PREPARED_ACTIVATION payloads, using actual store serializer semantics and bounded selected fragment plus all descriptors; fail closed with sanitized evidence-bound reason before publishing usable arm. Test real arm and activation JSON at limit−1/exact/+1 and query byte lengths from disposable SQLite. Do not raise ceiling or truncate selected JSON.

## Mandatory 12-area matrix — actual coverage, not suite-count inference

| # | Area | Inspected evidence / actual coverage | Verdict or gap |
|---|---|---|---|
| 1 | Exact A–F reproduction vs historical cause | Fresh `tests.cp55_opening_repro`: A1,689,540/B639 prepare1002, exact original attempts forwarded; C malformed and D exponent invalid_metadata; E no_next_market; F ambiguous_market. Hashes match checkpoint. | PASS synthetic proof. Missing historical body prevents unique historical attribution; not proof body is inherently unrecoverable elsewhere. |
| 2 | Selected fragment/hash/size/completeness | Observer SHA256/bytes/complete/selectors/replay label, original callback forwarding; fresh large-list test verifies hash/size/raw bytes. | PASS basic retention. Hash alone cannot replay competitors. |
| 3 | Durable1MiB evidence ceiling | Fresh selected-wrapper limit−1/exact/+1 test passes. Arm/activation persist larger envelope. | **M1**; no full durable boundary test. |
| 4 | Complete strict JSON /10MiB | Existing complete decoder and identity encoding/body cap inspected; fresh malformed/exponent/schema/oversize cases pass. | PASS these controls; exact10MiB transport equality not independently rerun. No prefix acceptance. |
| 5 | Complete candidate enumeration | `discover_next:84-118`: five100-row pages, incomplete fails, conflicts/ambiguity fail before detail. Existing discovery test covers500-row incomplete/conflicts/two/zero. | Source/test inspection; fresh F confirms ambiguity. No fresh multipage descriptor aggregate test. |
| 6 | Returned detail/config authority | Fresh repro requests returned1002 and returned condition config; metadata test checks raw detail/config/original outcome indices and changed/missing/swapped config rejection. | PASS targeted identity/config test. No guessed slug. |
| 7 | Future identity/token boundary | Future parser exact contiguous900-second window with actual now; predecessor market/condition/token disjointness and detail signature checks inspected. | Existing parsing/discovery controls inspected; not all permutations rerun. |
| 8 | Stale ownership/activation | Existing predecessor-change, late/owner/invalidated/clock/storage activation matrix and pending old-owner integration inspected; activation publication after transaction. | Coverage exists; no blanket fresh execution claim. |
| 9 | Durable sanitized reasons | Fresh actual-parser reason matrix plus real scheduler/SQLite preparation test passes seven categories. Revalidation source uses same failure_reason field. | PASS preparation; durable revalidation seven-category matrix absent. Messages not serialized by new reason field. |
| 10 | Final audit exact once / explicit failure | Fresh real-store semantic exits opening_blocked/invalid_metadata/transport_error: pending real thread completes second audit during stop; reopen IDs[1,2]; repeat flush no write. Fresh G persists1. | PASS exceptional covered cases; **H1 normal reference shutdown gap**. Arbitrary programming exceptions not covered by this matrix. |
| 11 | Writer failure / flush failure | Fresh failed-writer metadata/storage tests verify no audit flush/recovery and nonzero result; fresh audit OSError verifies close/no finish. | PASS covered exceptional branches. No blanket proof for normal preflush failure; inherited finally shutdown still calls persistence methods. |
| 12 | Close failures/idempotence/scope | Existing child client/store/transport-close tests, stream-close evidence, timing-flush failure and worker-retention tests inspected. New exceptional repeat audit flush covered. Actual HEAD diff includes earlier Task8 send admission and endpoint code. | Existing coverage, not fresh full matrix. No new schema/dependencies, quality gate, clock algorithm, Chainlink or private-auth changes found in scoped CP5.5 correction. Existing Task8 changes not relabeled CP5.5. |

## Verification evidence and limits

- Fresh socket-guarded A–G repro plus **9 targeted unittest tests PASS,0.433s**. Guard patches `socket.socket.connect` with `AssertionError('Offline tests: network forbidden')`. Existing MockTransport used; no live runner entrypoint invoked.
- Fresh independent normal-reference shutdown ordering probe reproduces H1; complete-envelope size probe reproduces M1. Initial exploratory normal probe tried to read `attempt` from SHUTDOWN event and raised KeyError; corrected filter and reference-mode probe yielded results above. No production change resulted.
- Checkpoint/ledger report final full **760 PASS640.375s**, focused **491 PASS577.985s**, after final production edit. These are retained implementer evidence, **not independently rerun or certified from counts**. Expensive suites deliberately not repeated.
- Read retained `C:\Users\ANDROS~1\AppData\Local\Temp\opencode\cp54-c6b48klv\comparison.json`: `added=[]`, `removed=[]`, `equal=true`. Checkpoint reports current17 files177 diagnostics versus baseline16 files177, zero warnings/informations, exact mapped identities against untouched6e1f582. Not globally clean; comparison JSON corroborates result but reviewer did not rerun entire mapper or Pyright. Documentation-only review creates no new Python type delta. Lint N/A per ledger.
- Historical DB/raw/analysis hashes retained as checkpoint evidence only; no fresh historical DB inspection needed. No repair, writer reacquisition or live refetch attempted.

## Disposition

Offline corrections resolve reproducible large-valid-list rejection and covered writable MarketDataError audit loss, but H1/M1 prevent H0/M0 acceptance. Keep historical opening BLOCKED/NOT VALIDATED. No rollover or pilot may run under this review; user restriction is unconditional.
