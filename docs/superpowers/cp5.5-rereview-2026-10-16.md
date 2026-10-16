# CP5.5 independent scoped rereview — 2026-10-16

## Verdict and scope

**H1 ADDRESSED. M1 ADDRESSED. Spec PASS; quality PASS. Remaining scoped High 0 / Medium 0. New correction-induced High 0 / Medium 0. Readiness: OFFLINE ONLY. No live, rollover or pilot authorization.**

Fresh review of `cp5.5-independent-review-2026-10-16.md` against actual working-tree diff, current source/tests, checkpoint fix-round-1 section and ignored SDD progress ledger. This is a two-finding rereview, not renewed whole-project acceptance. No production/test code edits, network, live acquisition, subagents, staging or commits. Only this requested report written. Historical databases were not opened or repaired. Existing tests used disposable SQLite.

Existing Task8 changes in HEAD-relative diff are not newly attributed to CP5.5. Checkpoint retains HEAD `9d26a7dd56a25daf0af3d02b6ad6f38b25c95fc0` and historical pre-CP5.5 diff hash; no retained machine-readable pre-CP5.5 patch was reconstructed during this rereview. Attribution is source-and-document scoped, not an invented baseline commit.

## H1 — ADDRESSED: final audit after worker completion

- `scripts/cp5_run.py:75-82`: CP5 final owner hook flushes audit then timing. Old preflush/finally shutdown override is gone; approximate drain's earlier idempotent flush remains, but is not sole final barrier.
- `src/polymarket_bot/recorder/service.py:1211-1225`: common normal shutdown stops/drains reference and approximate workers before `_final_persistence`, then recovery, SHUTDOWN event, finish and checkpoint. Reference, approximate and no-worker configurations share the barrier; no mode branch bypasses it on successful normal termination.
- `service.py:1171-1182`: writable semantic MarketDataError termination stops both workers before final persistence and terminal finish. Already-failed writer returns explicit persistence failure without audit/recovery/finish writes. Storage/invariant failure branch remains no-write (`:1166-1170`). Stop/join failures cannot be reported as successful final persistence: worker stop raises if thread remains alive (`:61-68`, `:110-117`). Existing nested cleanup remains; no blanket finally flush added.
- `tests/test_cp5_safety.py:8-64`: normal STOPPED and INTERRUPTED tests complete attempt2 on a real thread during stop/join, reopen disposable DB read-only and require exact ordered audit IDs `[1,2]`, excluding loss and duplicates. Flush failure test forbids recovery, finish and checkpoint after failure. Existing exceptional real-store, writer-failure and explicit audit-failure tests remain.

Fresh independent execution: all seven selected audit/store tests PASS in 0.526s, including both normal reference termination subcases, three semantic-error subcases, writer failures and audit-flush failure. Both workers absent is safe through unconditional hook; approximate mode retains common post-stop barrier in addition to earlier drain flush. This does not claim persistence after arbitrary programming exceptions, failed writers or unjoinable workers; those are not successful termination paths and must remain explicit failures. No new correction-induced lifecycle defect found.

## M1 — ADDRESSED: complete durable serialized budget

`src/polymarket_bot/recorder/store.py:914-937` validates writer, serializes complete detail with existing `_json`, measures UTF-8 bytes and rejects PREPARED_ARM/PREPARED_ACTIVATION above unchanged 1,048,576 before insert. Actual insertion uses same serializer semantics. Bound includes envelope, selected event and aggregate page descriptors, not independent per-fragment allowances. No ceiling increase, truncation or general event-cap expansion. Sanitized `invalid_metadata` classifies as `EVIDENCE_BOUND_EXCEEDED`.

`metadata.py:127-161` still bounds selected-event/all-descriptor wrapper; that earlier conservative check is not presented as complete durable proof. Full raw attempts still forward to existing callback. Complete list enumeration/strict parsing and returned-ID detail/config authority remain unchanged by this fix.

Arm event persistence precedes usable `_prepared` publication (`service.py:929-941`); oversize rejection leaves no usable arm. Activation event is inside outer transaction before active revision/id publication (`service.py:966-985`); failure rolls back candidate save/expiration and cannot publish successor. No partial selected JSON accepted.

`tests/test_recorder_store.py:25-48` checks actual durable serializer limit−1/exact/+1 for **both** event types, queries SQLite BLOB byte lengths, rejects oversize with no row and no failed-writer flag. `tests/test_recorder_service.py:396-467` additionally exercises actual scheduler arm envelope at all three boundaries, including sanitized failure and `_prepared is None`. Fresh independent store-boundary test PASS; fresh scheduler arm-boundary, atomic/single-use activation and late/stale/invalidated/storage-failure tests all PASS (3 tests, 0.655s).

### Remaining activation coverage assessed against contract

Dedicated scheduler-generated activation limit−1/exact/+1 matrix is absent; checkpoint explicitly discloses this. Store test does exercise both durable event types at all exact byte boundaries, while actual activation integration covers successful publication/single-use and failure admission/storage behavior. Production activation necessarily passes through same store guard inside transaction. Frozen supplement requires bounded existing event JSON and commit-before-publication (`plans/2026-10-15-cp5-3-task8-opening-supplement.md:43-45,57`), not a separate scheduler boundary matrix for every payload shape. Current layered evidence establishes requested property. Missing duplicate end-to-end matrix is a nonblocking coverage limit, not a new Medium or invented contract expansion. No claim that scheduler activation exact-boundary cases were independently executed.

## Verification evidence and limits

| Evidence | Result | Attribution |
|---|---|---|
| Final full suite | 764 PASS, 647.172s | Latest checkpoint fix round1 and SDD ledger; retained implementer evidence, not freshly rerun here |
| Expanded focused suite | 517 PASS, 597.547s | Same; expanded scope includes recorder store |
| Targeted fix suite | 69 PASS, zero failures/errors | Same; do not conflate with fresh 10-test rereview |
| Fresh independent selected checks | 7 PASS / 0.526s; 3 PASS / 0.655s | Existing tests executed with `socket.socket.connect` patched to raise `AssertionError('Offline tests: network forbidden')`; no live runner |
| Baseline-relative Pyright | current19 files / baseline18; 265 diagnostics each, zero added/removed exact mapped identities | Latest checkpoint/ledger against untouched `6e1f582`; not globally clean |
| Comparison artifact | `added=[]`, `removed=[]`, `equal=true` | Independently read `C:\Users\ANDROS~1\AppData\Local\Temp\opencode\cp54-deeufrcd\comparison.json`; current raw JSON identifies Pyright1.1.414; mapper/typechecker not rerun |
| Whitespace | fresh `git diff --check` exit0 | Existing CRLF conversion notices only |
| Lint | N/A | Existing explicit ledger ruling; no new Python edits |

Counts alone do not establish correctness. Findings resolved from source ordering, common durable guard, tests' actual assertions and fresh selected execution. Full/focused/type comparison remain accurately labeled retained evidence. Earlier 760/491/177 evidence is historical and superseded for current fix round, not silently reused as final evidence.

## Historical root uncertainty and preserved evidence

Checkpoint historical research D and continuation P/R remain truthful: original HTTP200 `/events` body of 1,689,540 bytes and original exception message were not recovered from reviewed artifacts. Equal synthetic byte count is not equal historical body, hash or exact replay. Valid-large-list callback rejection is a proven reachable defect, strongly consistent with missing forwarded attempt, but not uniquely proven body-specific historical cause. Strict malformed/numeric/schema failures remain possible causes without original bytes; no successor publication or identity is inferred.

Historical raw80/persisted79 and missing attempt80 remain unchanged evidence, not retrospectively repaired by fixes or synthetic tests. Hash descriptors identify bytes and selected evidence; alone they cannot replay unretained competitors. Existing raw control-attempt storage is separate authority. No claim that missing historical body is inherently unrecoverable elsewhere, and no refetch could reconstruct historical bytes.

## Disposition

Scoped H1/M1 corrections accepted at H0/M0 for offline spec/quality review. No new High/Medium breakage found in corrections. Historical opening remains **BLOCKED / NOT VALIDATED**; live availability, successful activation, rollover and pilot remain unproven. Readiness is **OFFLINE ONLY**, with live prohibition unconditional.
