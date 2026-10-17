# CP5.9 independent review — 2026-10-18

Fresh, independent, **offline** review of the CP5.9 producer fix for the CP5.8 `OPENING_WAIT`
finite-float defect. This review performed **no** network, **no** launch/rollover/pilot, **no**
supervisor/acquisition invocation, **no** code change/fix, **no** subagent, **no** commit, **no**
history rewrite, and **no** modification of historical CP5.8 artifacts. Only read-only commands and
passive reads/tests were used.

Working dir: `C:\Users\Andro Saptajulianto\Downloads\Polymarket-bot\.worktrees\cp5-data-quality`.
HEAD `a085312f286cab82c272de9dcf932bf0ccb6ed60`, branch `feat/cp5-data-quality`.

## A. Verdict

**Spec PASS / quality PASS. H 0 / M 0.**

All 13 required areas independently verified. The CP5.9 change is exactly the producer fix the CP5.8
review demanded: `store._json` stays strict, the aligned-child OPENING_WAIT producer now emits only
canonical types, `_flush_timing` routes through canonical `_json`, scheduling formulas are unchanged,
and no gate/clock/network/supervisor path moved. New test suite (10 tests) passes; CP5.7 audit suite
(11 tests) passes; the full repository suite (816 tests) passes. All five historical artifact hashes
match exactly.

Non-blocking observations (not H/M) are listed in section N.

## B. Exact diff under review

`git status` (tracked): only `scripts/cp5_run.py` modified (25 insertions / 9 deletions).
`git diff HEAD --name-only`: `scripts/cp5_run.py`. One new untracked file:
`tests/test_cp59_opening_wait_evidence.py`. No other tracked/untracked source change.

Change set:
- New `opening_wait_evidence(intended_boundary, *, wait_started, wait_ended, wait_end_ns)` →
  `dict(intended_boundary=<int>, wait_started_wall_us=round(wait_started*1e6),
  wait_ended_wall_us=round(wait_ended*1e6), wait_end_ns=<int>)`.
- `_flush_timing` now serializes each event detail with canonical `store._json(detail)` instead of
  raw `json.dumps(detail)` (both the buffered timing events and the DIAGNOSTIC_PERSISTENCE event).
- New pure `aligned_wait_delay(intended_boundary, now)` and `require_admission(intended_boundary, now)`.
- `child()` admission check and `__main__` aligned-child wait scheduling call those pure helpers.

Current working-tree hashes: `scripts/cp5_run.py`
`9FE1106631B5D57FC405DC76E0F337105029239E42F75FDCDB8E9D1F6F8073B8`;
`tests/test_cp59_opening_wait_evidence.py`
`D4597A6B3D769B7F9CC6C40E20B044668B22FC0B142525EE58ED09A374484373`;
`src/polymarket_bot/recorder/store.py`
`35902EE05C64D2E17BF65E6205B5FFB66339D41081677D104197F44B4D6992CA`;
`src/polymarket_bot/recorder/service.py`
`72CEA586D84398F259175E4DDC8574CC7C3FDCFB67CBA5462EA6A0057C81D024`.

## C. (1) Exact CP5.8 finite-float reproduction — PASS

Root cause independently re-derived from pre-fix code (`git show HEAD:scripts/cp5_run.py`):
pre-fix producer `wait_evidence = dict(intended_boundary=..., wait_started=wait_started,
wait_ended=time.time(), wait_end_ns=time.monotonic_ns())` emitted **finite float** `wait_started`/
`wait_ended`. `child()` assigned it to `recorder.opening_wait_evidence`; `service.py` `_record`
(HEAD lines 1260–1263) spreads `**wait_evidence` into
`detail=dict(type='OPENING_WAIT', diagnostic_only=True, run_id=..., **wait_evidence)` →
`RecorderStore.record_event` (store.py:938 `payload = _json(detail)`) → `_json` `canonical`
(store.py:28–44) rejects `float` with `TypeError` → `RecorderStoreError("fatal_invariant", ...)`.

The new test drives the **real** flow, not merely `_json(float)`:
- `run_opening_wait()` constructs a real `CP5Recorder` over the real `RecorderFixture.store`
  (`RecorderStore.open`), sets `recorder.opening_wait_evidence`, and calls `recorder.run(...)`.
- `run()` → `_execute_run` → `_record`, which reaches service.py:1260–1263 and
  `RecorderStore.record_event` → store.py:938 → `_json`.
- `test_aligned_child_producer_evidence_persists_canonically` asserts the persisted
  `recorder_events.detail_json` contains the canonical keys and rejects the old key names.
- `test_raw_float_opening_wait_evidence_still_fails_closed` feeds the exact pre-fix producer shape
  (`wait_started`/`wait_ended` floats) through the same real flow and asserts
  `(exit_code, reason) == (1, 'fatal_invariant')` and zero OPENING_WAIT rows.

Pre-fix RED reasoning (permitted; ranked evidence):
1. HEAD lacks `opening_wait_evidence`, `aligned_wait_delay`, `require_admission` (verified absent via
   `git show HEAD:scripts/cp5_run.py`) → every test in the new module errors at import/attribute use.
2. HEAD's aligned-child producer emitted finite floats (lines 419–420) → the real flow yields
   `fatal_invariant`; a canonical-persistence assertion cannot pass.
3. HEAD's `_flush_timing` used raw `json.dumps` (lines 52/55), which accepts `1.5` →
   `test_scheduler_timing_flush_fails_closed_on_float` (expects `RecorderStoreError` and a preserved
   buffer) could not pass pre-fix.

Command (PASS): `$env:PYTHONPATH='src;scripts'; & '.venv\Scripts\python.exe' -m unittest -q tests.test_cp59_opening_wait_evidence`
→ `Ran 10 tests ... OK`.

## D. (2) Producer/sink data-flow — PASS

`opening_wait_evidence` returns only `int` (`intended_boundary` passthrough int, `round(...)` → `int`,
`wait_end_ns` passthrough `time.monotonic_ns()` → `int`). The real-flow test additionally asserts
`type(detail[field]) is int` for `intended_boundary`, `wait_started_wall_us`, `wait_ended_wall_us`,
`wait_end_ns` and that `wait_started`/`wait_ended` are absent. No `float` reaches any `_json` sink on
the OPENING_WAIT path. In `scripts/cp5_run.py` the only DB `_json` sinks are `_flush_timing`
(canonical `_json`) and `_flush_audit` (via `record_event` → `_json`); all other `json.dumps` calls are
stdout only (lines 247, 387, 416, 437, 443, 456, 461).

## E. (3) Canonical persisted timestamp representation — PASS

Fields carry explicit units: `wait_started_wall_us`/`wait_ended_wall_us` (integer wall microseconds)
and `wait_end_ns` (monotonic nanoseconds), consistent with repository conventions (`wall_us`,
`_epoch_us`, `*_wall_us`, `*_ns`). No silent unit change under the same name: the old float-second
fields `wait_started`/`wait_ended` were **renamed** (not reinterpreted) to `*_wall_us`; the new test
asserts the old names are absent. `intended_boundary` remains epoch seconds, matching its established
meaning in the surrounding code (`intended_boundary_us = intended_boundary*1_000_000`) and the CLI
argument; the field is unchanged from the pre-fix event, so no silent unit change occurred. No
downstream consumer depends on the renamed fields (`scripts/cp5_analyze.py`, `cp5_reconstruct.py`,
`cp5_supervise_durable.py` contain no references to `OPENING_WAIT`/`wait_started`/`wait_ended`).

## F. (4) Scheduler behavior unchanged — PASS

`aligned_wait_delay(intended_boundary, now)` computes `delay = intended_boundary-60-now` and raises
`ValueError('CP5 BLOCKED: alignment window missed')` unless `0 <= delay <= 900` — byte-for-byte the
pre-fix formula/guard. `require_admission(intended_boundary, now)` raises
`ValueError('CP5 BLOCKED: intended preparation admission missed')` when `now >= intended_boundary-22`
— identical to the pre-fix inline check in `child()`. In `__main__` the scheduling is unchanged:
`wait_started = time.time()` captured first, then `delay = aligned_wait_delay(args.intended_boundary,
wait_started)`, then `time.sleep(delay)`. Argument evaluation order in `opening_wait_evidence(...,
wait_ended=time.time(), wait_end_ns=time.monotonic_ns())` matches the prior dict literal.
`test_aligned_wait_delay_matches_b_minus_60_schedule`, `test_alignment_window_rejection_unchanged`, and
`test_late_admission_rejection_unchanged` pass.

## G. (5) B-60 behavior unchanged — PASS

The only B-60-dependent expression is `intended_boundary-60` inside `aligned_wait_delay`, preserved
exactly; `opening_plan`'s `start = boundary-60` is untouched. No boundary/lead/threshold arithmetic
changed.

## H. (6) Strict store serializer unchanged — PASS

`git diff HEAD --name-only` shows only `scripts/cp5_run.py`. `store.py` and `service.py` are unchanged
compared to HEAD. The strict serializer is intact at store.py:28–44 (`_json`/`canonical`), and
`record_event` still enforces `payload = _json(detail)` at store.py:938.

## I. (7) Finite float remains rejected at store boundary — PASS

`_json` `canonical` accepts only `None`, `bool`/`str`, 64-bit `int`, `Decimal`, `list`/`tuple`, and
`dict` with all-`str` keys; any other type (including `float`) raises
`RecorderStoreError("fatal_invariant", ...)`. Independent probe:
`_json(1.0)`, `_json(float('nan'))`, `_json(float('inf'))`, `_json({'a':1.5})` all raise
`fatal_invariant`; `_json({'a':1,'b':'x','c':True,'d':None})` →
`{"a":1,"b":"x","c":true,"d":null}`.
`test_raw_float_opening_wait_evidence_still_fails_closed` passes.

## J. (8) Cross src/ + scripts/ producer audit — PASS (no missed producer)

Independent static scan of every `record_event(...)` call site and `_json(...)` sink in `src/` and
`scripts/`:
- `scripts/cp5_run.py`: `_flush_timing` (canonical `_json`, ints/strs/bools only), `_flush_audit` →
  `record_event` (AuditTransport event fields are int/str/bool/list-of-str; `capture_identity` adds
  `_book_identity`/`_research_pending` fields that are ints/strs and `run_id`/`version`).
- `src/.../service.py`: PREPARED_ARM/ACTIVATION (`*_ns`, `boundary_us`, Decimals already canonical),
  BOOK_SEND_ATTEMPT (`*_mono_ns`, ints), APPROXIMATE_SESSION/OPPORTUNITY/FETCH/COMPLETION
  (`*_ns`/`*_us` ints; `error_code`/`disposition`/`reason` strings; header/selector tuples strings),
  SCHEDULER/DIAGNOSTIC/PREPARATION_PHASE (`round(...)`/`*_ns` ints), CLOCK_PAUSE/IDENTITY_CONFLICT/
  DISCOVERY_GAP/RESTART/STARTUP (`reason`/`time_domain` strings, booleans, ints),
  INTENDED_ENDPOINT (`*_us`/`*_ns` ints), `_persist_research` (`json.dumps` round-trips into
  `record_event`; dataclass evidence fields are ints/strs/Decimals).
- `src/.../store.py`: `record_event`, `save_anchor` (`diagnostic` ints/strs), `save_market_revision`
  (`_normalize_clob_numbers` strings/None, bools), `save_resolutions` (provenance strings/None,
  `match_count` int; selectors strings), `save_reference_evidence` (selector tuples, ints,
  `_decimal` text), `save_control_attempts` (`HttpAttempt` ints/strs).
- `src/.../resolution.py` provenance contains only strings/None/int. No `float` is produced into any
  `_json` sink. `metrics.MeasurementReport` floats are stdout-only (never a `_json` sink);
  `market_data.models` floats are stdout-only.
- All 816 repository tests pass, giving broad coverage that no reachable producer emits a rejected
  type at a `_json` sink. No missed producer found.

## K. (9) External cp5_run producer explicitly covered — PASS

The new test file covers `scripts/cp5_run.py` producers: OPENING_WAIT (`opening_wait_evidence` through
the real service→store flow), `_flush_timing` (canonical persistence + fail-closed on float),
`_flush_audit`/AUDIT event fields (`test_audit_attempt_event_is_canonical_through_store` asserts the
`ACTUAL_SEND_ATTEMPT` payload is canonical), plus `aligned_wait_delay`/`require_admission` scheduling
guards.

## L. (10) CP5.7 Decimal path preserved — PASS

Command (PASS): `$env:PYTHONPATH='src;scripts'; & '.venv\Scripts\python.exe' -m unittest -q tests.test_cp57_arm_serialization_audit`
→ `Ran 11 tests ... OK`. This exercises Decimal canonicalization (`1E-7`→`0.0000001`), signed zero
(`-0.00`), sort-key determinism under reduced `decimal` context, signed-64 bounds, the 1 MiB
PREPARED_ARM/ACTIVATION envelope fail-closed, and rejection of `NaN`/`Infinity`/unknown objects/
non-`str` keys.

## M. (11) Audit finalization preserved — PASS

`_final_persistence`, `_flush_audit`, and `_audit_persisted_attempts` bookkeeping are untouched by the
diff. CP5.7 tests covering healthy-fatal audit finalization
(`test_healthy_preinsert_fatal_exit_preserves_completed_audit`, `test_fatal_lifecycle_matrix`,
`test_healthy_fatal_recovers_pending_ownership`) and the new
`test_healthy_writer_survives_rejected_opening_wait_evidence` all pass.

## N. (12) Historical artifact custody — PASS

Byte-only `Get-FileHash -Algorithm SHA256`, read-only, all matching exactly:

| Artifact | SHA256 (verified) |
|---|---|
| `data\cp5_8_rollover_20261018.sqlite3` | `38B4A3C4A9ABB4A042AF294B0E46705683D7B551E6CE0D755BEE716C1C047337` |
| `logs\cp5_8_rollover_20261018.jsonl` | `E78AD9AAEDCBC03D9C5354D2A6DFD015016F0BD3DD4E64162BE8FA77EB9FF96B` |
| `logs\cp5_8_rollover_20261018.jsonl.stdout` | `2A634E33033F13740620FCF8083972FE8AFC033956F70838A95279DCEF54E8DC` |
| `logs\cp5_8_rollover_20261018.jsonl.stderr` | `E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855` |
| `data\cp5_6c_replacement_20261017_0351adeb-6594-492e-9237-adf7d833dba0.sqlite3` | `A488583880D10507A24E82251D0870433C1D39160B77429820A2D339A54CA409` |

No CP5.8 artifact was opened for write; no file was modified or relocated.

## O. (13) No gate/clock/network changes — PASS

`git diff HEAD --name-only` is only `scripts/cp5_run.py`. `scripts/cp5_supervise_durable.py`
SHA256 `2BCA463314A3D737AA0CF44DC7FA5C1E248B49FE4D70AF663244F5AA371F954D` — identical to the frozen
CP5.8 exact-config hash (unchanged). `scripts/cp5_analyze.py` is tracked and unmodified by this diff.
Within `scripts/cp5_run.py` the diff touches only the OPENING_WAIT producer, `_flush_timing`
serializer, and the newly extracted pure scheduler helpers; no research-gate, clock-policy, threshold,
fallback, network-route, or durable-supervisor behavior changed.

## P. Serializer contract reproduction

- `tests.test_cp59_opening_wait_evidence`: `Ran 10 tests ... OK`.
- `tests.test_cp57_arm_serialization_audit`: `Ran 11 tests ... OK`.
- Full suite `python -m unittest discover -s tests -q`: `Ran 816 tests ... OK` (~734 s).
- Scoped pyright (`pyright scripts/cp5_run.py`): 1 error, `scripts/cp5_run.py:15:8 Import "httpx" could
  not be resolved (reportMissingImports)`. This is a pre-existing environment/interpreter-resolution
  issue (pyright cannot locate the venv's `httpx`), not a diagnostic on any CP5.9 symbol; no type
  error is reported for `opening_wait_evidence`, `aligned_wait_delay`, `require_admission`, or the
  `_json` usage. Not counted as H/M.

## Q. Observations (non-blocking, no action required)

1. `opening_wait_evidence` derives wall microseconds via `round(float_seconds * 1e6)`, whereas the
   repository's `wall_us()` uses `time.time_ns() // 1000` (truncation). The ≤1 µs rounding-direction
   difference is intrinsic to having captured the value as float seconds and does not affect
   correctness or the canonical type.
2. The cross-`src`/`scripts` producer audit is necessarily a manual/static inspection backed by the
   full-suite pass; there is no single test that enumerates every `_json` sink and asserts canonical
   types for all producers. No producer was found to violate the contract, so this is a residual
   methodology note, not a defect.

## R. Final

**Verdict: Spec PASS / quality PASS. H 0 / M 0.** No blockers. No fix, rerun, retarget, second DB,
threshold/tuning change, or pilot was performed. No historical artifact was modified.

Report path: `docs/superpowers/cp5.9-independent-review-2026-10-18.md`.
