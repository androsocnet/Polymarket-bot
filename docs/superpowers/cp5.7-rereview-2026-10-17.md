# CP5.7 scoped independent rereview — 2026-10-17

## Verdict

**Spec PASS; quality PASS within requested CP5.7 scope. H0 / M0. M1 ADDRESSED; M2 ADDRESSED; M3 ADDRESSED. READY for explicit commit/offline finalization, not a commit instruction and not live authorization.**

Fresh read-only review of actual two-file production diff, complete current eleven-test module, relevant transaction/worker/activation/audit code, latest checkpoint and existing SDD ledger. No implementation participation in this review; no subagents. Only this requested report written. No Python execution, test/typecheck rerun, network, live/acquisition activity, SQLite connection, historical repair or commit. Existing full/focused/target results below are producer evidence, not fresh reviewer runs. Assessment does not manufacture missing original numbered specification wording or reopen unrelated historical scope.

## Finding dispositions

### M1 — ADDRESSED (previous Medium, typecheck)

`src/polymarket_bot/recorder/store.py:35–37` now uses exact-list OR exact-tuple narrowing. Closed-type policy remains intact; no ignore, cast or subclass widening. Read external `cp57_compare.py` and complete comparison artifact: baseline source verification uses read-only git show against 6e1f582 with CRLF/LF normalization; diagnostic identities include path, both span endpoints/columns, severity, rule, complete message and multiplicity, mapped through unchanged SequenceMatcher blocks with autojunk=False. Not count subtraction.

`cp57-comparison.json:123–162` records current/baseline exit1, counts265/265, added[], removed[], equal=true, new_file_diagnostics[]. Thus mapped265 retained and zero attributable added diagnostics, not globally clean Pyright. Scope27 current/23 baseline files; four absent-baseline files have no diagnostics. Current extraPaths configuration differs from untouched baseline, as checkpoint explicitly discloses; this is a scoped baseline-debt comparison, not identical-configuration clean certification. Fresh store/service/test hashes match comparison source hashes and final checkpoint hashes exactly. Original added int-not-iterable diagnostic no longer exists in retained comparison.

### M2 — ADDRESSED (previous Medium, fatal pending ownership)

`src/polymarket_bot/recorder/service.py:1166–1179` distinguishes healthy fatal_invariant from poisoned writer. Order is reference stop, approximate stop, final audit/timing persistence, run-scoped recover_interrupted(finish_runs=False, reason=exc.code), then FAILED finish. Quiescence failure prevents audit/recovery/finish; flush failure likewise prevents subsequent recovery/finish. No result is invented as successful research. Existing `store.py:1270–1317` transaction resolves IN_PROGRESS slots with interrupted book/reference evidence and submitted version2 opportunities with interrupted completions, preserving available ownership fields. Recovery is before terminal run update, while run remains writable.

`tests/test_cp57_arm_serialization_audit.py:343–372` seeds real SQLite slot reservation and submitted opportunity, asserts INTERRUPTED/fatal_invariant, one interrupted completion with original target42, and completed audit[1]. `:374–389` exercises actual preinsert validation fatal and audit[1,2,3]. Static run-scoping and copied ownership keys supplement assertions; test does not independently assert every copied key or terminal timestamp. This is no longer the empty-run-only acceptance gap.

Failed-writer control `:325–341` forbids final persistence; actual insert-failure matrix `:235–258` asserts storage_failure and RUNNING/null terminal reason. Guard skips all new final writes when writer already failed; store._check_writer independently refuses poisoned-writer operations. Close is resource release, not additional durable evidence. Healthy audit insertion failure poisons writer and cannot fall through to finish.

### M3 — ADDRESSED (previous Medium, acceptance coverage)

Actual `_activate_prepared` integration `tests/test_cp57_arm_serialization_audit.py:130–188` covers Decimal complete-envelope equality, +1 serialization/size rejection and real SQLite trigger insertion failure. Equality persists exactly1048576 UTF-8 bytes and canonical -0.00, then publishes successor. Failures retain prior in-memory revision, no `_armed_opening` publication, clear `_prepared`, and compare complete markets/market_revisions/reference_associations/recorder_events rows with pretransaction snapshots, including lifecycle/plans represented there. `service.py:960–979` clears consumed preparation, groups revision/plans/expiry/event under outer transaction and publishes revision/revision_id/opening only after successful commit. `store.py:283–305` rolls back both nested validation and database failure; database error poisons writer. No activation production change required; acceptance-gap tests do not claim newly discovered product RED.

Lifecycle matrix `:206–277` asserts exact storage_failure/exit1, RUNNING/null reason for flush and insert faults, worker completion before audit[1,2], repeated final persistence without duplicate audit, closed writer and successful reacquisition. Injected trigger is removed through explicitly closed disposable connection only after failed writer closes; this is test cleanup, not recovery of historical evidence. Client repeated-close assertion is fixture cleanup, not proof Recorder owns HTTP-client closure.

Real approximate-worker timeout `:279–323` uses actual submit/stop with shortened join timeout: exact approximate_reference_shutdown_failed/exit2, no flush/events, RUNNING/null, writer/session ownership retained while worker alive. Explicit release/join, repeated stop and store close permit reacquisition. It does not promise successful cleanup while worker remains active. Worker stop is idempotent after join; repeated final audit marks attempt persisted only after successful insertion (`scripts/cp5_run.py:64–77`). No unsafe new repeated-finalization write path found. Reference-worker timeout and process-level repeated client lifecycle are not exhaustively modeled; no concrete new regression established and no blanket cleanup guarantee accepted.

## New-breakage checks: canonical Decimal and strict bound only

`store.py:28–44` recursively accepts exact null/bool/str, signed64 exact int, bounded exact Decimal via unchanged decimal_text, exact list/tuple and exact-string-key dict. Unknown objects, floats including finite values, nonstring keys, integer overflow, nonfinite/out-of-policy Decimal and recursion failures fail closed as RecorderStoreError(fatal_invariant). No generic str fallback or Decimal-to-float conversion. Tuple-to-array, sorted compact JSON and allow_nan=False remain. Primitive canonical bytes remain compatible; Decimal is durable text, not original numeric token preservation.

`market_data/numeric.py:80–91` retains finite512-digit/1024 exponent-adjusted/2048 output policy, context-independent fixed-point formatting and signed zero. Strict ingress numeric parser and10MiB transport policy unchanged. Determinism/context and closed-edge tests `:190–204` supplement nested Decimal test `:83–93` and rejection/envelope tests `:101–128`. Global _json closure affects all existing callers, not ARM alone: previously permissive float/nonstring-key/unbounded-int inputs are now deliberately rejected. No concrete current producer breakage attributable to this scoped diff found; this is not universal external API backward compatibility.

`store.record_event` uses one serialized payload for authoritative whole-envelope1MiB UTF-8 check and insertion. No cap increase, preliminary-metadata substitution, repeated unchecked encoding, schema/gate/clock/parser/dependency/supervisor change in production diff. Actual activation rollback covers rejection after earlier transaction mutations. Existing ARM boundary tests remain relevant; new retained-body ARM test is local-artifact dependent, not portable fixture distribution. Parser lifecycle case injects error code rather than exercising parser integration. These disclosed limits do not reproduce M1–M3.

## Verification evidence and honest limits

Latest checkpoint fix-round1 sections K–P and ledger lines3–9 agree:

| Evidence | Retained result | Attribution / limit |
|---|---|---|
| Final full discovery | 806, OK,708.790s, exit0 | Producer run after final Python edit; not rerun here. |
| Final CP5.7 target | 11, OK,2.175s, exit0 | Same final sources; not rerun here. |
| Focused17 modules | 436, OK,681.044s, exit0 | Before last test-only optional-worker assert. Final full includes focused scope afterward; no claim of focused rerun after assert. |
| Mapped Pyright1.1.414 | 265/265, added0, removed0, equal=true; exit1 each | Retained JSON/harness inspected and hashes captured; baseline debt remains. |
| Whitespace | Fresh git diff --check, no diagnostics | Documentation-only review; lint N/A per ledger ruling. |

Checkpoint reports hashing all Python sources before final full and unchanged after full+target (IDENTITY=True, EXITS[0,0]). Fresh three relevant source hashes match checkpoint and comparison artifact. Full raw unittest stdout/stderr and all-source pre/post transcript are not separately available as a standalone evidence artifact in inspected paths; durations/counts/source-identity assertion remain checkpoint/ledger producer evidence, not independently replayed execution proof. No invented output-capture hash supplied for absent transcript.

Earlier failures remain material chronology: full806/805.314s had new fixture SQLite connection leak plus existing Windows job-close cleanup WinError32; target also failed leak. Fixture now explicitly closes SQLite connection. Later full806/849.421s failed existing job-close missing WATCHDOG_KILLED. Final full passed. `tests/test_cp5_supervise_durable.py:313–352` is independent Windows job-object/breakaway toy-process test; neither it nor supervisor changed in CP5.7 diff. Failures are unrelated to reviewed serializer/fatal-recovery paths by static dependency/scope, but intermittency is not fixed, root cause is not proven and no reliability guarantee follows from final green run. Earlier focused436/777.673s and target11/2.074s are chronological results, not substituted for final evidence.

## Fresh SHA256 capture

Hex case normalized below. Evidence JSON paths are under `C:\Users\ANDROS~1\AppData\Local\Temp\opencode\`.

| Path | SHA256 |
|---|---|
| src/polymarket_bot/recorder/store.py | 35902ee05c64d2e17bf65e6205b5ffb66339d41081677d104197f44b4d6992ca |
| src/polymarket_bot/recorder/service.py | 72cea586d84398f259175e4ddc8574cc7c3fdcfb67cba5462ea6a0057c81d024 |
| tests/test_cp57_arm_serialization_audit.py | fbe83c02c9bc5b941dc753717067ee7c7cd43abb7c3708f2d094383a8861bf93 |
| cp57-comparison.json | 910d5ca63ee2225aaa9c0f48eedafb4612da08c335b50f91d5c92f44cbc49a1c |
| cp57-current-pyright.json | 9697c8346a796718a936aadcd8af2f3494e3e35fa9193c87db758977c3cc3c97 |
| cp57-baseline-pyright.json | d4de0ad1169fff859d86736facc15fa7f582caa6f1fd90544bde044e4227b534 |
| data/cp5_6c_replacement_20261017_0351adeb-6594-492e-9237-adf7d833dba0.sqlite3 | a488583880d10507a24e82251d0870433c1d39160b77429820a2d339a54ca409 |

Historical DB hash matches prior custody; only read-only byte hashing performed, no SQLite connection. Historical RUNNING/null state and raw69/durable66 audit gap remain unrepaired; no reconstructed sends or new historical accounting claim. Original UNKNOWN/PARTIAL history and consumed CP5.6C launch authority unchanged. Scoped H0/M0 clears rereview prerequisite for separately authorized commit/offline finalization only. No live authorization, acquisition qualification, rollover/pilot approval or historical PASS.
