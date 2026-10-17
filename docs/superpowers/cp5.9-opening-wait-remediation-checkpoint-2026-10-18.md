# CP5.9 — OPENING_WAIT CANONICAL TIMING EVIDENCE REMEDIATION CHECKPOINT

Date: 2026-10-18
Branch: `feat/cp5-data-quality`
Scope: OFFLINE remediation only. No rollover, no network, no new boundary, no pilot.

Final verdict: **CP5.9 READY TO REQUEST ONE NEW ROLLOVER VALIDATION**

## A. Starting HEAD / status

- Starting HEAD `a085312f286cab82c272de9dcf932bf0ccb6ed60` (`docs: record CP5.7 serialization and audit checkpoint`).
- Branch `feat/cp5-data-quality`. Before edits: index clean; working tree only untracked artifacts (36+), pre-existing.
- Sources used for the proven root cause were unchanged at start (store.py `35902ee0…`, service.py `72cea586…`), consistent with the CP5.8 evidence.

## B. CP5.8 evidence custody (immutable)

Verified by SHA256 before and after all work; all match exactly:

| Artifact | SHA256 |
|---|---|
| `data/cp5_8_rollover_20261018.sqlite3` | `38B4A3C4A9ABB4A042AF294B0E46705683D7B551E6CE0D755BEE716C1C047337` |
| `logs/cp5_8_rollover_20261018.jsonl` | `E78AD9AAEDCBC03D9C5354D2A6DFD015016F0BD3DD4E64162BE8FA77EB9FF96B` |
| `logs/cp5_8_rollover_20261018.jsonl.stdout` | `2A634E33033F13740620FCF8083972FE8AFC033956F70838A95279DCEF54E8DC` |
| `logs/cp5_8_rollover_20261018.jsonl.stderr` | `E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855` |
| `data/cp5_6c_replacement_20261017_0351adeb-6594-492e-9237-adf7d833dba0.sqlite3` | `A488583880D10507A24E82251D0870433C1D39160B77429820A2D339A54CA409` |

No repair, no rewrite, no `mode=ro` writes.

## C. Exact RED

Regression `tests/test_cp59_opening_wait_evidence.py` drives the REAL flow: aligned-child producer → `recorder.opening_wait_evidence` → `Recorder._record` OPENING_WAIT emission (`service.py:1260-1263`) → `RecorderStore.record_event` → `_json` (`store.py:938/40`). Disposable SQLite, synthetic clocks, no sockets.

Extraction-first (behavior-preserving) then observed RED on the un-canonicalized producer:

```
FAIL test_aligned_child_producer_evidence_persists_canonically
  AssertionError: (1, 'fatal_invariant') != (0, 'STOP_REQUESTED')
ERROR test_producer_evidence_is_deterministic_and_pure  KeyError: 'wait_started_wall_us'
Ran 3 tests ... FAILED (failures=1, errors=1)
```

`test_raw_float_opening_wait_evidence_still_fails_closed` passed pre-fix (strict sink already rejects float). This is the exact CP5.8 failure reproduced through the real producer path, not a bare `_json(float)` unit test.

## D. Producer / sink trace

- Producer: `scripts/cp5_run.py:431-437` `opening_wait_evidence(...)` (previously inline floats `wait_started`/`wait_ended` = `time.time()`).
- Carrier: `cp5_run.py:343` `recorder.opening_wait_evidence = wait_evidence`.
- Emit: `service.py:1260-1263` `record_event(..., detail=dict(type='OPENING_WAIT', diagnostic_only=True, run_id=..., **wait_evidence))`.
- Sink: `store.py:938` `payload = _json(detail)` → `store.py:40` rejects float → `RecorderStoreError('fatal_invariant', 'Recorder JSON evidence is not supported.')`.
- Consumers of `wait_started`/`wait_ended`/`wait_end_ns`/`opening_wait_evidence`/`OPENING_WAIT`: only the producer and the service passthrough; **no analyzer/test/DB consumer** depends on those names or units. Renaming is therefore safe.

## E. Time representation decision

Repository conventions: wall time persisted as **integer microseconds** (`wall_us()` = `time.time_ns()//1000`; `_epoch_us`; audit `*_wall_us`), monotonic as **integer nanoseconds** (`*_ns`, e.g. `wait_end_ns`).

- Keep scheduling in float wall seconds (`time.time()`, `time.sleep(delay)`), unchanged.
- Persist a separate canonical object with explicit integer units: `intended_boundary` (epoch s int), `wait_started_wall_us` (int µs), `wait_ended_wall_us` (int µs), `wait_end_ns` (monotonic int ns).
- The float `wait_started`/`wait_ended` units are **renamed** to `*_wall_us`, never reinterpreted under the same name.
- `store._json` remains strict; no float accepted, no `default=str`, no float→JSON number.

## F. Implementation

`scripts/cp5_run.py` (only production file changed, +25/-9):

```
def opening_wait_evidence(intended_boundary, *, wait_started, wait_ended, wait_end_ns):
    return dict(intended_boundary=intended_boundary,
                wait_started_wall_us=round(wait_started * 1_000_000),
                wait_ended_wall_us=round(wait_ended * 1_000_000), wait_end_ns=wait_end_ns)
```

- `__main__` aligned-child now calls `opening_wait_evidence(...)` (scheduling float retained).
- Phase-8 contract enforcement: `CP5Recorder._flush_timing` now serializes via canonical `_json` instead of raw `json.dumps` (the one producer that bypassed the strict sink), so `SCHEDULER_TIMING` can no longer silently persist a float.
- No change to `store.py`, `service.py`, `cp5_analyze.py`, `cp5_supervise_durable.py`, `models.py`, or any gate.

## G. Scheduler-equivalence proof

The aligned-child scheduling formulae are lifted into pure helpers with identical expressions (byte-identical arithmetic; only named):

```
def aligned_wait_delay(intended_boundary, now):
    delay = intended_boundary-60-now
    if not 0 <= delay <= 900:
        raise ValueError('CP5 BLOCKED: alignment window missed')
    return delay

def require_admission(intended_boundary, now):
    if now >= intended_boundary-22:
        raise ValueError('CP5 BLOCKED: intended preparation admission missed')
```

- `wait_started = time.time()`, `delay = aligned_wait_delay(...)`, `time.sleep(delay)` unchanged from prior behavior.
- B−60 target, alignment-window rejection (`0<=delay<=900`), and late-admission rejection (`now>=B−22`) are covered by tests `test_aligned_wait_delay_matches_b_minus_60_schedule`, `test_alignment_window_rejection_unchanged`, `test_late_admission_rejection_unchanged`.
- No approximation alters scheduler behavior; the canonical evidence is a separate object.

## H. OPENING_WAIT GREEN

`PYTHONPATH=src;scripts; .venv\Scripts\python.exe -m unittest -q tests.test_cp59_opening_wait_evidence` → **10 tests OK**.

Covers: producer persists canonically; start/end correspond to synthetic clocks; `wait_end_ns` retained; `intended_boundary` retained exactly; delay/B−60/alignment/admission unchanged; deterministic pure producer; raw float still fails closed; healthy writer survives a rejected pre-insert validation; scheduler-timing flush canonical + fails closed on float; audit `ACTUAL_SEND_ATTEMPT` canonical.

## I. Producer compatibility audit (src/ + scripts/)

Traced every producer reaching `store._json` / `record_event` / raw recorder-event inserts. Result: **no current reachable producer emits a finite float (or other rejected type) at a `_json` sink.**

- All HTTP JSON is parsed via `load_numeric_json` (int/Decimal/str/bool/None/list/dict; constants rejected), so discovery/CLOB/resolution payloads never yield float.
- CP5 live path: `opening_wait_evidence` (now int), `_flush_audit` (`ACTUAL_SEND_ATTEMPT` int/str/bool), `capture_identity` (int/str), audit event fields (int/str/bool/None/list[str]) — all canonical. `_flush_timing` was the only bypass and is now routed through `_json`.
- Service producers (reference learning/commit, BOOK_SEND_ATTEMPT, APPROXIMATE_SESSION/OPPORTUNITY(_COMPLETION), PREPARATION_PHASE, PREPARED_ARM/ACTIVATION, CLOCK_PAUSE, IDENTITY_CONFLICT, STARTUP/SHUTDOWN/RESTART/DISCOVERY_GAP) — all int/str/bool/None/Decimal/list/dict(str keys).
- Methodology corrected: the earlier CP5.7 review inspected only the 17 in-recorder `_json` call sites and missed the external `cp5_run.py` producer; this audit covers `scripts/` explicitly.
- Residual (documented, non-blocking): `store.py:491` decodes raw CLOB config with `json.loads(..., parse_float=str)` but no `parse_constant` guard; a vendor `NaN`/`Infinity` in `oas/itode/ibce/fd` would reach `_json` and still **fail closed** (fatal_invariant) rather than silently persist. Inconsistent with `load_numeric_json` policy; not reachable with well-formed JSON; left as residual risk T.

## J. Serializer contract preservation

`store._json`/`canonical` (`store.py:28-44`) unchanged. Reconfirmed by `tests.test_cp57_arm_serialization_audit` (**11 tests OK**): Decimal canonical text, signed zero preserved, deterministic ordering across context/precision, finite float rejected, NaN/Inf rejected, unknown object rejected, non-string key rejected, signed64 boundaries unchanged, oversized complete envelope fails closed.

The fix is **not** "floats now serialize"; it is "the production OPENING_WAIT evidence producer no longer sends floats into strict recorder JSON."

## K. CP5.7 regression suite

`tests.test_cp57_arm_serialization_audit` → **11 tests OK**, including ARM/ACTIVATION Decimal serialization, envelope boundaries, audit finalization, healthy-fatal audit preservation, failed-writer no-writes, real-worker timeout retention.

## L. Lifecycle / fault tests (offline)

- A. canonical OPENING_WAIT succeeds — `test_aligned_child_producer_evidence_persists_canonically`.
- B. deliberately unsupported OPENING_WAIT value still fails closed — `test_raw_float_opening_wait_evidence_still_fails_closed`.
- C. writer healthy after pre-insert evidence validation failure — `test_healthy_writer_survives_rejected_opening_wait_evidence` (`_write_failed` stays False; a subsequent valid event persists).
- D. healthy fatal final audit behavior still CP5.7-correct — `test_cp57` `test_fatal_lifecycle_matrix` / `test_healthy_preinsert_fatal_exit_preserves_completed_audit`.
- E. poisoned writer receives no unsafe writes — `test_cp57` `test_failed_writer_fatal_exit_does_not_flush`.
- F. cleanup closed/no-thread/no-owner leak — `test_cp57` fatal lifecycle + `test_cp5_safety`.
No network.

## M. Historical compatibility

- CP5.8 DB/streams and CP5.6C DB unchanged (see B).
- No schema migration: `store.py` unchanged (no DDL).
- No clock-policy, research-quality threshold, fallback, or network-route change.
- No durable-supervisor change: `scripts/cp5_supervise_durable.py` unchanged (hash `2BCA4633…954D`).
- No CP5.3 research policy change; `cp5_analyze.py` unchanged.

## N. Targeted tests

- CP5.9 module: **10 PASS**.
- CP5.7 module: **11 PASS**.

## O. Full / focused tests

- Focused 21-module set (CP5.9, CP5.7, CP5 safety, durable supervisor, recorder store/service/boundary/metadata/schedule, CP3 lifecycle/regression/review, FREE suites): **513 tests, 647.334s, OK, exit 0**.
- Full discovery `PYTHONPATH=src;scripts; .venv\Scripts\python.exe -m unittest discover -s tests -t . -q`: **816 tests, 661.776s, OK, exit 0** (806 prior + 10 new).
- `$env:PYTHONPATH='src;scripts'`.

## P. Pyright mapped comparison vs `6e1f582`

Fresh scoped run via the existing external harness (`cp57_compare.py`), explicit `.venv` interpreter, read-only baseline export verified against `6e1f582` (newline-normalized only):

- current 28 files / **265 errors** / 0 warnings; baseline 23 files / **265 errors** / 0 warnings; exit 1 each (pre-existing debt).
- Mapped diagnostic identities (path + full span + severity + rule + message + multiplicity): **added `[]`, removed `[]`, equal true**, 265 matched.
- New/absent-baseline files (`scripts/cp5_supervise_durable.py`, `tests/cp55_opening_repro.py`, `tests/test_cp57…`, **`tests/test_cp59_opening_wait_evidence.py`**, `tests/test_cp5_supervise_durable.py`): **0 diagnostics**.
- `scripts/cp5_run.py` diagnostics unchanged under mapping. **Zero attributable added diagnostics.**

## Q. Whitespace

`git diff --check` → exit 0 (only a CRLF normalization warning). Lint N/A — no configured project lint command.

## R. Independent review

`docs/superpowers/cp5.9-independent-review-2026-10-18.md` — fresh independent reviewer: **Spec PASS / quality PASS, H 0 / M 0**. Reproduced CP5.9 (10 OK) and CP5.7 (11 OK) and the full suite (816 OK); confirmed producer/sink flow, canonical units, scheduler formulas unchanged, `store.py`/`service.py` unchanged, float still rejected at the store boundary, CP5.8/CP5.6C custody hashes, and no gate/clock/network/supervisor change. Non-blocking observations only (`round()` vs `wall_us()` ≤1 µs; audit is static/manual, corroborated by the full suite).

## S. Commits / status

Local commits on `feat/cp5-data-quality` after H0/M0 (no merge/push/reset/stash/clean/history rewrite; runtime DB/log files not committed):

1. `fix: canonicalize aligned opening wait evidence` — `scripts/cp5_run.py`.
2. `test: cover recorder JSON producer compatibility` — `tests/test_cp59_opening_wait_evidence.py`.
3. `docs: record CP5.9 opening wait remediation checkpoint` — this checkpoint + independent review.

## T. Risks

1. `round(wait_started * 1e6)` differs from `wall_us()` truncation by ≤1 µs; diagnostic-only OPENING_WAIT evidence, negligible and deterministic. Deliberate: ties persisted evidence to the scheduling value.
2. `store.py:491` `json.loads(..., parse_float=str)` lacks a `parse_constant` guard; vendor `NaN`/`Infinity` in CLOB config keys would fail closed at `_json` rather than being rejected at parse. Latent; not reachable with well-formed JSON; not in CP5.8/CP5.9 scope. Recommend a future hardening.
3. Cross-producer compatibility audit is static/manual (not a runtime enumerator); backed by the full-suite pass and the corrected `scripts/` coverage.
4. CP5.8 live FAIL and consumed launch authority are unchanged; this remediation restores readiness only. A new live rollover requires a fresh independent exact-config review.

Verdict: **CP5.9 READY TO REQUEST ONE NEW ROLLOVER VALIDATION**.
