# CP5.7 independent offline review — 2026-10-17

## Verdict

**Spec FAIL; quality FAIL. H0 / M3.** Fresh static review of actual working production diff, entire new seven-test module, relevant existing tests, checkpoint and SDD ledger. No code changes, Python execution, network, live activity, subagents, commits or historical DB connections/writes. Only this requested report created. Full802/focused309/target7 PASS are supplied preceding-run evidence, not independently rerun. Exact original numbered 14-area/10+10-case specification is not present in supplied message or checkpoint; matrix below explicitly maps requested subjects rather than inventing that missing wording.

## Findings

### M1 — added canonical branch fails required typecheck

`src/polymarket_bot/recorder/store.py:35–37`: exact-type membership does not narrow iterable type for Pyright; comprehension receives an inferred possible int. Checkpoint `docs/superpowers/cp5.7-arm-serialization-audit-remediation-checkpoint-2026-10-17.md:93–101` records mapped baseline265/current266, matched265/added1/removed0, `reportGeneralTypeIssues`, store.py:37:51–55, `"int" is not iterable`. This is attributable new code, not baseline noise. Repro: run checkpoint line13 scoped Pyright command (not executed during review). Minimal fix: explicit list/tuple narrowing compatible with exact-type policy, or locally typed validated iterable; do not weaken closed policy. Recheck mapped identities, not counts.

### M2 — healthy fatal now finishes run without resolving outstanding work

`src/polymarket_bot/recorder/service.py:1168–1175` stops workers and flushes audit but calls `finish_run` without `recover_interrupted` or result drain. Contrast semantic-error recovery at `service.py:1183` and normal shutdown at `service.py:1220–1231`. `store.py:1195–1206` only changes recording_runs; it does not resolve reservations. `store.py:1275–1296` owns IN_PROGRESS-slot interruption; `store.py:1299–1313` owns submitted research opportunity completion.

Concrete static repro: start real disposable run, reserve slot (or persist version2 submitted APPROXIMATE_OPPORTUNITY), then raise healthy `RecorderStoreError('fatal_invariant', ...)` during acquisition/store validation. New branch persists FAILED/fatal_invariant while slot remains IN_PROGRESS (or opportunity has no terminal completion). Completed transport audit can be present while run accounting stays incomplete. New fatal fixtures start empty runs only (`tests/test_cp57_arm_serialization_audit.py:143–162,211–214`), so cannot catch this. Minimal fix: after successful worker stop and audit persistence, invoke existing interruption recovery scoped to run with `finish_runs=False` and explicit fatal reason before finish; preserve failed-writer zero-write path. If worker results are retained instead, use existing owner-aware drain and recovery semantics, not new synthetic successes. Add disposable pending-slot and pending-opportunity assertions; no historical repair.

### M3 — acceptance matrix leaves key publication and cleanup failures untested

`tests/test_cp57_arm_serialization_audit.py:101–128` calls store directly with ARM/ACTIVATION labels. This meaningfully proves complete serialized detail size and rejection/no insert, but does not exercise activation transaction rollback or prevent in-memory successor publication. Retained-body test stops at successful ARM (`:78–81`). Existing service envelope test (`tests/test_recorder_service.py:433–464`) covers ARM −1/equal/+1 and rejected preparation only; dedicated Decimal activation failure remains absent. Lifecycle matrix (`new test :137–186`) uses SimpleNamespace reference stop, never real approximate stop timeout, and checks only exit_code for flush/insert failures (`:178–181`), not exact storage_failure/fatal reason, terminal NULL reason, writer/client cleanup or writer reacquisition. Double `super()._final_persistence()` checks idempotent flush, not repeated cleanup failure behavior. All seven tests may pass despite activation publication regression or wrong storage-failure reason.

Minimal fix: extend existing fixtures, not new framework: actual `_activate_prepared` with Decimal envelope equality/+1 and insert failure; assert prior revision/lifecycle/revision rows unchanged and no `_armed_opening` publication on failure. Add exact result reason, terminal fields, close/reacquire and real worker stop-failure assertions to fatal matrix. Missing tests are acceptance gap, not claim that currently correct publication order is broken.

## Fourteen requested review areas

| Area | Actual evidence / assessment |
|---|---|
| 1. Real CP5.6C root | Checkpoint :43–53 and retained test :31–80 recover strict Decimal event1197070/market5764590 from original request27–29. Actual discovery/revalidation/store ARM path, HTTP mocked. Current success test no longer independently demonstrates pre-fix exception; historical root evidence explicitly separated. |
| 2. Canonical Decimal | store.py:28–43 calls existing numeric.py:80–91 decimal_text; no float conversion. Exact bounded fixed-point strings, signed zero preserved. |
| 3. Deterministic bytes | sort_keys/compact separators/allow_nan=False retained. Single payload checked and inserted at store.py:938–949. Exact expected bytes tested :83–93; no repeated-order/context determinism test. |
| 4. No default=str / closed objects | New helper has no fallback conversion. Unknown objects rejected before insert. Existing digest default=str intentionally unchanged; checkpoint :56 distinguishes it. |
| 5. Integer/key bounds | Exact bool/int distinctions, signed64 range, exact string keys; rejects subclasses too. Upper/lower invalid cases meaningful; valid minimum not directly asserted. |
| 6. Float policy | All floats rejected, including finite; no hidden Decimal-to-float. NaN/Infinity Decimal tested; float nonfinite variants not explicitly tested. |
| 7. Recursion/errors | Cycles cause RecursionError caught and wrapped; unsupported nested types and decimal policy failures wrapped as fatal_invariant. Deep acyclic depth not separately tested. No uncaught-error defect established for supported exact built-ins. |
| 8. Whole 1MiB envelope | Authoritative actual UTF-8 payload cap remains store.py:938–941, before transaction. Equality and +1 with Decimal tested both labels; existing store test supplies −1/equality/+1. |
| 9. ARM atomic publication | service.py:901–902 clears prepared state before store; :938–939 publish only after insert. Retained actual ARM succeeds. Existing non-Decimal service boundary rejection asserts no prepared state. |
| 10. Activation atomic publication | service.py:963–976 groups revision/plans/expiry/event in transaction; :978–979 publishes after commit. Static order sound; Decimal integration failure assertions missing, M3. |
| 11. Healthy fatal vs failed writer | Guard :1168–1169 distinguishes preinsert validation from poisoned writer; failed-writer fixtures prohibit final persistence. Healthy fatal audit now runs, but outstanding work omitted, M2. |
| 12. Audit ownership/exact once | cp5_run.py:64–77 deduplicates after successful insert; new :130–186 waits for last synthetic worker event and invokes flush twice. Does not replay historical missing sends; no such claim accepted. |
| 13. Cleanup/flush/storage reason | OSError flush propagates to outer run handler service.py:1075–1078 as storage_failure; no finish follows failed flush. Insert failure poisons writer. Real stop timeout and exact error/cleanup assertions missing, M3. |
| 14. Compatibility/scope/custody | Global store._json changes every caller, not ARM alone: config/activation metadata, catalog, anchor diagnostics, fees/delays, selectors, quality and reference diagnostics (store.py:415–459,530–535,599,644,743,1011,1082,1257,1407). Strict primitives retain old bytes; Decimal newly becomes string; formerly accepted float/nonstring key/unbounded int now rejected in both schema revisions. No concrete current built-in producer regression found; broader API behavior change must be explicit. No schema/gate/clock/route/supervisor production diff. Read-only SHA256 matches retained DB. |

## Serialization: ten meaningful coverage checks

1. Retained strict parsed Decimal reaches actual ARM: yes (:31–81), artifact-dependent, not portable.
2. Nested Decimal and tuple canonical text/signed zero: yes (:83–93).
3. Exact deterministic expected bytes: yes; different insertion order/repeated encoding absent.
4. Finite float rejection: yes (:95–99,120–126); nonfinite floats absent.
5. Nonstring keys and integer overflow: yes; lower overflow included :121; valid minimum absent.
6. Unknown nested object rejection, no insert/healthy writer: yes (:118–128).
7. Nonfinite/out-of-policy Decimal: yes (:120–126); context variation not tested.
8. Recursive container: cycle yes (:118–128); deep acyclic structure absent.
9. Actual complete ARM envelope: store Decimal equality/+1 yes; service ARM bound has old non-Decimal −1/equality/+1 coverage.
10. Actual complete activation envelope and atomic publication: store Decimal equality/+1 yes; dedicated service Decimal rollback/publication absent (M3).

## Audit: ten meaningful coverage checks

1. Healthy actual preinsert validation fatal persists three completed records: yes (:206–221).
2. Already failed writer performs no final audit: yes (:188–204), existing safety test also prohibits finish/recovery.
3. Actual insertion failure fails closed: yes trigger (:159–161); no durable audit assertions meaningful.
4. Last worker completion precedes flush: synthetic reference thread yes (:145–154); real worker timeout absent.
5. Repeated final flush exactly once: yes double call and IDs [1,2] (:164–185).
6. Audit flush OSError avoids terminal success: yes; RUNNING is deliberate failure evidence, not successful terminalization.
7. Metadata error final audit: yes matrix mode metadata.
8. Parser error final audit: raised MarketDataError mode parser, not actual parser invocation; lifecycle coverage only.
9. Repeated cleanup/resource and writer reacquisition: not asserted by new test; existing safety tests partly cover close behavior, not new fatal branch timeout.
10. Exact terminal reason and pending ownership: healthy reason equality yes; storage/flush exact reason absent; pending slot/research terminalization absent (M2/M3).

## Evidence and limits

Fresh read-only `Get-FileHash -Algorithm SHA256` of `data/cp5_6c_replacement_20261017_0351adeb-6594-492e-9237-adf7d833dba0.sqlite3`: **A488583880D10507A24E82251D0870433C1D39160B77429820A2D339A54CA409**, matching checkpoint. No SQLite connection opened. Fresh `git diff --check` produced no diagnostics. Native diff reviewed: service8 insertions/1 deletion; store15 insertions/3 deletions. New test fully read, not counted solely from report assertions. Ledger initial RED-only entry is historical; checkpoint continuation supersedes it and discloses new Pyright defect. No fresh test/typecheck PASS asserted; no full-suite rerun. No launch authority restored; CP5.6C historical audit gap/DB state unchanged.
