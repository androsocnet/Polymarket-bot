# CP5.10 — Stable Readiness Review (independent, read-only)

Date: 2026-10-18
Branch: `feat/cp5-data-quality`
Worktree: `C:\Users\Andro Saptajulianto\Downloads\Polymarket-bot\.worktrees\cp5-data-quality`
Reviewer: fresh, independent subagent. Author of none of the reviewed code. No source/test file was modified; nothing was committed.

## Verdict

**Spec PASS; quality PASS; High 0 / Medium 0.**

Disclosed non-blocking caveat: the full offline suite is not deterministic on this machine — run #1 was `816 tests / FAILED (failures=1)` on a timing-sensitive `test_cp5_safety` stream/cookie test, run #2 was `816 tests / OK`; that test then passed 6/6 in isolation. It is unrelated to the reviewed delta (see §10 and observation #6). All findings relate to the code under review; no production defect was found.

`scripts/cp5_run.py` **CHANGED** in the `1759f88`..`671f877` delta. (commit `c4d68e8`, 446 -> 462 lines, git blob `c5805f8c` -> `48cc32f5`). The CP5.8-era gate-equivalence argument, which rested on `scripts/cp5_run.py` being **unchanged**, is therefore **superseded** (see §2/§2a). The change is confined to the opening-wait evidence canonicalization and to routing `CP5Recorder._flush_timing` through the strict `_json` serializer; scheduler arithmetic, B−60 alignment, and admission bounds are byte-equivalent (§5).

---

## 0. Constraints observed

- READ-ONLY on production code and tests. No edits, no fixes, no commits.
- No network requests of my own. No invocation of `scripts/cp5_supervise_durable.py` (run/launch/inspect). No live `scripts/cp5_run.py` mode (no `--child`, `--aligned-child`, `--mode rollover/pilot`).
- No DB/journal/lock/stream artifacts created, deleted, or written. `data/` and `logs/` touched only via read-only hashing/listing.
- Only file created: this report.

---

## 1. Repository freeze

Commands and raw output:

```
$ git rev-parse HEAD
671f8775584a8f7da03e8ee8e1bfa10760bcb7b0

$ git branch --show-current
feat/cp5-data-quality

$ git status --porcelain --untracked-files=no
<EMPTY>

$ git diff --check
<EMPTY>            ; exit 0
$ git diff --cached --check
<EMPTY>            ; exit 0

$ git log --oneline -5
671f877 docs: record CP5.9 opening wait remediation checkpoint
50ecfe6 test: cover recorder JSON producer compatibility
c4d68e8 fix: canonicalize aligned opening wait evidence
a085312 docs: record CP5.7 serialization and audit checkpoint
b4f4610 fix: serialize ARM evidence and finalize healthy fatal audit
```

- HEAD == expected `671f8775584a8f7da03e8ee8e1bfa10760bcb7b0`. **PASS**
- Branch == `feat/cp5-data-quality`. **PASS**
- Tracked working tree clean (empty porcelain). **PASS**
- `git diff --check` and `git diff --cached --check` both exit 0 / empty. **PASS**

---

## 2. Exact live-code delta, `1759f88` -> `671f877`

```
$ git diff --stat 1759f88 671f877
 ...tion-audit-remediation-checkpoint-2026-10-17.md | 194 ++++++++++
 .../cp5.7-independent-review-2026-10-17.md         |  72 ++++
 docs/superpowers/cp5.7-rereview-2026-10-17.md      |  71 ++++
 .../cp5.9-independent-review-2026-10-18.md         | 238 +++++++++++++
 ...ening-wait-remediation-checkpoint-2026-10-18.md | 183 ++++++++++
 scripts/cp5_run.py                                 |  34 +-
 src/polymarket_bot/recorder/service.py             |  11 +-
 src/polymarket_bot/recorder/store.py               |  18 +-
 tests/test_cp57_arm_serialization_audit.py         | 389 +++++++++++++++++++++
 tests/test_cp59_opening_wait_evidence.py           | 180 ++++++++++
 10 files changed, 1377 insertions(+), 13 deletions(-)

$ git diff --name-status 1759f88 671f877
A docs/...cp5.7-arm-...-checkpoint-2026-10-17.md
A docs/...cp5.7-independent-review-2026-10-17.md
A docs/...cp5.7-rereview-2026-10-17.md
A docs/...cp5.9-independent-review-2026-10-18.md
A docs/...cp5.9-opening-wait-remediation-checkpoint-2026-10-18.md
M scripts/cp5_run.py
M src/polymarket_bot/recorder/service.py
M src/polymarket_bot/recorder/store.py
A tests/test_cp57_arm_serialization_audit.py
A tests/test_cp59_opening_wait_evidence.py
```

**Every changed file under `src/` and `scripts/`:**
- `scripts/cp5_run.py` (M)
- `src/polymarket_bot/recorder/service.py` (M)
- `src/polymarket_bot/recorder/store.py` (M)

No other file under `src/` or `scripts/` changed. The remaining files are docs and tests.

Per-commit attribution (`git log --oneline 1759f88..671f877`):

```
671f877 docs: record CP5.9 opening wait remediation checkpoint
50ecfe6 test: cover recorder JSON producer compatibility
c4d68e8 fix: canonicalize aligned opening wait evidence
a085312 docs: record CP5.7 serialization and audit checkpoint
b4f4610 fix: serialize ARM evidence and finalize healthy fatal audit
```

- `b4f4610` (CP5.7) changed `service.py` + `store.py` + tests only. It did **not** touch `scripts/cp5_run.py`.
- `c4d68e8` (CP5.9) changed `scripts/cp5_run.py` **only** (25 insertions / 9 deletions).
- `50ecfe6` added `tests/test_cp59_opening_wait_evidence.py`.

### 2a. Is the CP5.8-era gate-equivalence argument stale? — YES

`git rev-parse` of the `cp5_run.py` blob across the chain:

```
1759f88:scripts/cp5_run.py  blob=c5805f8c270869659bddf4744e7afd179cb35648  (446 lines)
b4f4610:scripts/cp5_run.py  blob=c5805f8c270869659bddf4744e7afd179cb35648  (446 lines)
50ecfe6:scripts/cp5_run.py  blob=48cc32f5c773e66417154c8740988380dd24e8ba  (462 lines)
671f877:scripts/cp5_run.py  blob=48cc32f5c773e66417154c8740988380dd24e8ba  (462 lines)
```

The CP5.8 stable-readiness review read `scripts/cp5_run.py` at **446 lines** and its gate-equivalence claims ("gates unchanged", "reviewed code is current HEAD source") presupposed that file was byte-identical to the previously reviewed revision. After `c4d68e8` the file is **462 lines** (new blob). **The gate-equivalence argument that assumed `scripts/cp5_run.py` unchanged is superseded.** Not refuted — it is a genuine content change, though the change is confined to the opening-wait evidence shape and the `_flush_timing` serializer route (see §3, §5). Scheduling/gate *semantics* remain equivalent (§5), but the byte-identity premise no longer holds.

Exactly what changed in `scripts/cp5_run.py` (raw diff `git show c4d68e8 -- scripts/cp5_run.py`):

1. New import: `from polymarket_bot.recorder.store import _json`.
2. `CP5Recorder._flush_timing`: two `json.dumps(...)` calls in the `recorder_events` inserts replaced with `_json(...)` (the DISCOVERY_GAP per-event detail and the final `DIAGNOSTIC_PERSISTENCE`/`SCHEDULER_TIMING` summary). This closes the one producer that bypassed the strict sink.
3. New pure helper `opening_wait_evidence(intended_boundary, *, wait_started, wait_ended, wait_end_ns)` returning canonical integer units.
4. New pure helper `aligned_wait_delay(intended_boundary, now)` (same expression + guard).
5. New pure helper `require_admission(intended_boundary, now)` (same expression).
6. `child()`: inline `if time.time() >= intended_boundary-22: raise ...` replaced by `require_admission(intended_boundary, time.time())`.
7. `__main__` `--aligned-child`: inline delay computation replaced by `delay = aligned_wait_delay(args.intended_boundary, wait_started)`; inline evidence dict `dict(intended_boundary=..., wait_started=..., wait_ended=..., wait_end_ns=...)` replaced by `opening_wait_evidence(args.intended_boundary, wait_started=wait_started, wait_ended=time.time(), wait_end_ns=time.monotonic_ns())`.

---

## 3. CP5.9 producer fix — inspected in the CURRENT working tree (`scripts/cp5_run.py`)

`scripts/cp5_run.py:117-120` (current source, read in full):

```python
def opening_wait_evidence(intended_boundary, *, wait_started, wait_ended, wait_end_ns):
    return dict(intended_boundary=intended_boundary,
                wait_started_wall_us=round(wait_started * 1_000_000),
                wait_ended_wall_us=round(wait_ended * 1_000_000), wait_end_ns=wait_end_ns)
```

`scripts/cp5_run.py:431-438` (current source, read in full):

```python
        if args.aligned_child:
            wait_started = time.time()
            delay = aligned_wait_delay(args.intended_boundary, wait_started)
            time.sleep(delay)
            wait_evidence = opening_wait_evidence(args.intended_boundary, wait_started=wait_started,
                wait_ended=time.time(), wait_end_ns=time.monotonic_ns())
            print(json.dumps(wait_evidence), flush=True)
```

Verification:
- Returns exactly `intended_boundary` — `intended_boundary=intended_boundary`, an **int passthrough**, no arithmetic, no unit reinterpretation. The caller passes `args.intended_boundary` (argparse `type=int`) and `child()` asserts `isinstance(intended_boundary, int)` / `type(intended_boundary) is not int` rejection. **PASS**
- `wait_started_wall_us = round(<float> * 1_000_000)` — `round()` on a float returns `int`. **int. PASS**
- `wait_ended_wall_us = round(<float> * 1_000_000)` — **int. PASS**
- `wait_end_ns = wait_end_ns` fed from `time.monotonic_ns()` — **int. PASS**
- The OLD float-second keys `wait_started` / `wait_ended` are **NOT emitted** anywhere in the current `opening_wait_evidence` output. **PASS**
- `intended_boundary` is not silently reinterpreted in units (epoch seconds in = epoch seconds out). **PASS**

The test module independently asserts all of this (§8).

---

## 4. Strict Recorder JSON contract still fail-closed

`src/polymarket_bot/recorder/store.py:28-44` (current source):

```python
def _json(value) -> str:
    def canonical(item):
        if item is None or type(item) in (bool, str):
            return item
        if type(item) is int and -(2**63) <= item <= 2**63 - 1:
            return item
        if type(item) is Decimal:
            return decimal_text(item)
        if type(item) is list or type(item) is tuple:
            return [canonical(child) for child in item]
        if type(item) is dict and all(type(key) is str for key in item):
            return {key: canonical(child) for key, child in item.items()}
        raise TypeError("Unsupported recorder JSON value.")
    try:
        return json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError, MarketDataError) as exc:
        raise RecorderStoreError("fatal_invariant", "Recorder JSON evidence is not supported.") from exc
```

`record_event` (`store.py:926-949`) computes `payload = _json(detail)` once (`:938`) and inserts the same `payload`. A finite `float` (or `NaN`/`Infinity`, which are `float`), `set`, `bytes`, `object()`, a non-`str` dict key, `bool`-as-key, and an out-of-range int all fall through `canonical` to `raise TypeError`, which is caught and re-raised as `RecorderStoreError('fatal_invariant', ...)`.

No `default=str`, no float→number relaxation, no `parse_constant` change was introduced by this delta: the `store.py` diff (`1759f88..671f877`) touches only `_json` (adding the `Decimal`/range/`canonical` acceptance) and the `record_event` insert (`detail_json=payload`). Confirmed by `git diff`.

Independent probe (stdin only; nothing written; `PYTHONPATH=src`):

```
$ @'...'@ | .venv\Scripts\python.exe -
float        REJECTED code='fatal_invariant' cause=TypeError
nan          REJECTED code='fatal_invariant' cause=TypeError
inf          REJECTED code='fatal_invariant' cause=TypeError
-inf         REJECTED code='fatal_invariant' cause=TypeError
set          REJECTED code='fatal_invariant' cause=TypeError
bytes        REJECTED code='fatal_invariant' cause=TypeError
object       REJECTED code='fatal_invariant' cause=TypeError
nonstr_key   REJECTED code='fatal_invariant' cause=TypeError
bool_key     REJECTED code='fatal_invariant' cause=TypeError
decimal-ok: {"k":"0.0000001","n":[null,true,5]}
int64-max-ok: {"k":9223372036854775807}
tuple-ok: {"k":[1,2,"a"]}
```

**PASS** — fail-closed on every rejected type; canonical types (Decimal→text, int64, tuple→list, bool, None) accepted.

Note (non-blocking, see residual list): `store.py:485` uses `json.dumps(..., default=str)` but only to compute a `sha256` digest of selected gamma metadata; it does **not** feed `_json` or any `detail_json`. `store.py:491` `json.loads(..., parse_float=str)` lacks a `parse_constant` guard (disclosed residual, §7/Residuals).

---

## 5. Scheduler behavioral equivalence

Pure helpers (`scripts/cp5_run.py:262-271`):

```python
def aligned_wait_delay(intended_boundary, now):
    delay = intended_boundary-60-now
    if not 0 <= delay <= 900:
        raise ValueError('CP5 BLOCKED: alignment window missed')
    return delay

def require_admission(intended_boundary, now):
    if now >= intended_boundary-22:
        raise ValueError('CP5 BLOCKED: intended preparation admission missed')
```

Byte-for-byte comparison against the pre-fix inline expressions (from `git show c4d68e8^:scripts/cp5_run.py`):

| Behaviour | Pre-fix (inline) | Post-fix (helper) | Equivalent |
|---|---|---|---|
| Delay | `args.intended_boundary-60-wait_started` | `aligned_wait_delay(b, wait_started)` = `b-60-now` | YES |
| Delay guard | `if not 0 <= delay <= 900: raise ValueError('CP5 BLOCKED: alignment window missed')` | identical expression + message | YES |
| Admission | `if time.time() >= intended_boundary-22: raise ValueError('CP5 BLOCKED: intended preparation admission missed')` | `require_admission(b, time.time())` → identical expression + message | YES |
| `wait_started` capture | `wait_started = time.time()` before delay | `wait_started = time.time()` before `aligned_wait_delay(...)` | YES |
| `time.sleep(delay)` | present, `delay` from above | unchanged | YES |
| Arg evaluation order | `dict(intended_boundary=…, wait_started=…, wait_ended=time.time(), wait_end_ns=time.monotonic_ns())` | `opening_wait_evidence(b, wait_started=…, wait_ended=time.time(), wait_end_ns=time.monotonic_ns())` — positional then keywords left→right | YES |
| B−60 target | `intended_boundary-60` | `intended_boundary-60` | YES |
| `opening_plan` | `start = boundary-60` (untouched, absent from diff) | same | YES |

- `wait_started = time.time()` is captured **before** the delay computation; `time.sleep(delay)` is unchanged. **PASS**
- Argument evaluation order preserved: `wait_ended=time.time()` is still evaluated before `wait_end_ns=time.monotonic_ns()`, exactly as in the old `dict(...)`. **PASS**
- B−60 alignment and admission bounds (`0 <= delay <= 900`; `now >= B−22`) unchanged. **PASS**
- `opening_plan` `start = boundary-60` untouched (`git diff` shows no hunk in `opening_plan`). **PASS**

The `test_cp59` module reproduces these formulas (`test_aligned_wait_delay_matches_b_minus_60_schedule`, `test_alignment_window_rejection_unchanged`, `test_late_admission_rejection_unchanged`) and they pass (§10).

---

## 6. No weakening

`git diff 1759f88 671f877 -- scripts/cp5_supervise_durable.py scripts/cp5_analyze.py` → **empty** (exit 0).

- Quality thresholds: not in any changed file; no threshold/gate constant touched. **unchanged**
- Fallbacks / metadata authority / audit semantics / clock policy: the only `service.py` change (CP5.7 `b4f4610`) adds a *finalization* path on a healthy-writer `fatal_invariant` (stop workers → `_final_persistence()` → `recover_interrupted` → `finish_run(FAILED, reason=exc.code)`) and still returns `(exit_code=1, reason='fatal_invariant')`. It does not relax the fail-closed outcome. Clock policy (`clock.py`) unchanged; `metadata.py` unchanged; `models.py` unchanged.
- Network routes/allowlist (`AuditTransport` in `cp5_run.py`, hosts/routes/header/method/selector checks): **not touched** by `c4d68e8` (no hunk in that region). **unchanged**
- Durable supervisor `scripts/cp5_supervise_durable.py`: **hash-identical to the CP5.8 frozen hash**.

```
$ (Get-FileHash -Algorithm SHA256 scripts/cp5_supervise_durable.py).Hash
2BCA463314A3D737AA0CF44DC7FA5C1E248B49FE4D70AF663244F5AA371F954D
```
Matches the frozen CP5.8 hash `2BCA4633...954D`. **PASS**

---

## 7. Cross-producer audit vs the strict sink

Static enumeration of every producer reaching `store._json` / `record_event` / a raw `recorder_events` insert (`grep` across `src/` and `scripts/`):

`src/polymarket_bot/recorder/store.py` (`_json` call sites): `:415,416,425,426,459,530,533,535,539,599,644,743,1011,1082,1257,1407`; raw `recorder_events` insert at `:537` (via `_json`) and `:947` (via `payload=_json(detail)`); recovery re-insert at `:1309` → `record_event`.

`src/polymarket_bot/recorder/service.py`: ~40 `record_event(...)` calls (STARTUP/SHUTDOWN/RESTART/DISCOVERY_GAP/CLOCK_PAUSE/IDENTITY_CONFLICT/reference learning+commit/APPROXIMATE_*/PREPARED_ARM/PREPARED_ACTIVATION/OPENING_WAIT). Spot-checked the value-bearing ones:
- `_flush_timing` detail: `target_ns`/`mono_ns`/`started`/`end_mono_ns`/`wall_us()` ints; `phase`/`market_id` str; `slot_index` int. No float.
- `_flush_audit` / `capture_identity`: `attempt/started_wall_us/started_mono_ns/status/request_bytes/response_bytes/ended_wall_us/ended_mono_ns` int, `host/path/method/error/provider/leg/run_id` str, `redirect/complete/stream_closed/set_cookie_present` bool, `*_header_presence` `list[str]`, plus identity `market_id` str / `slot_id/slot_index/target_ns/scheduled_us` int (`service.py:1409-1411`, `models.py:79 scheduled_us: int`). No float.
- `result.quality` (`store.py:1082`): from `books.py` — `{...: True}`, `min(count,10_000)` ints, `receipt_us - source_us` int. No float.
- Market-revision event detail `{"warnings": numeric_warnings, ...}`: `numeric_warnings` is `tuple[str]` from `_normalize_clob_numbers`. No float.

`scripts/cp5_run.py` (the CP5 live path):
- `opening_wait_evidence` — int (fixed, §3).
- `CP5Recorder._flush_timing` — **now routes through canonical `_json`** (was the one bypass, via `json.dumps`). `:53` and `:56`.
- `CP5Recorder._flush_audit` — `record_event(..., detail=dict(event, type='ACTUAL_SEND_ATTEMPT', version=2))`, all canonical (above).

**Result: no current reachable producer emits a rejected type (finite float / NaN / Inf / set / bytes / bad Decimal context / non-`str` dict keys) at a `_json` sink.** The only producer that could previously emit a raw float into recorder JSON (`_flush_timing`) now goes through `_json`; the `--aligned-child` OPENING_WAIT producer now emits integers. Because `_json` is fail-closed, any future float leakage would surface as a `fatal_invariant` (visible, product-internal) rather than silent corruption.

Disclosed residual: `store.py:491` `json.loads(revision.clob_config_raw.decode("utf-8"), parse_float=str)` has **no `parse_constant` guard**. A vendor `NaN`/`Infinity` token reaching keys `oas/itode/ibce/fd` (which `_normalize_clob_numbers` does not coerce) would produce a Python `float` and then **fail closed** at `_json` (`fatal_invariant`), not silently persist. `NaN`/`Infinity` are **not well-formed JSON**, so this is **not reachable with well-formed JSON**; it is inconsistent with the `load_numeric_json` policy and is a latent, non-blocking hardening item. `store.py:485` `default=str` feeds only a `sha256` digest, not `_json`.

---

## 8. Review of `tests/test_cp59_opening_wait_evidence.py`

- **Drives the REAL flow, not a bare `_json` unit test.** `run_opening_wait` constructs a real `CP5Recorder` over `fixture.store` (a real `RecorderStore` opened by `RecorderFixture`, real SQLite) and calls `recorder.run(RunConfig(), should_stop=lambda: True)`, which exercises `Recorder._record` → `record_event(..., detail=dict(type='OPENING_WAIT', ...))` (`service.py:1260-1263`) → `_json` (`store.py:938`). `RecorderFixture` uses `httpx.MockTransport`; **no sockets**. **PASS**
- **Asserts canonical int types:** `for field in ('intended_boundary','wait_started_wall_us','wait_ended_wall_us','wait_end_ns'): assertIs(type(...), int)` (`:57-58`), plus exact values (`:53-56`). **PASS**
- **Asserts absence of old keys:** `assertNotIn('wait_started', detail)` / `assertNotIn('wait_ended', detail)` (`:59-60`). **PASS**
- **Raw-float producer still fails closed:** `test_raw_float_opening_wait_evidence_still_fails_closed` injects the CP5.8-shaped float evidence and asserts `(result.exit_code, result.reason) == (1, 'fatal_invariant')` and zero OPENING_WAIT rows persisted (`:62-67`). This is the CP5.8 failure reproduced through the real producer path. **PASS**
- Additionally asserts: deterministic/pure producer; B−60 schedule; alignment-window and late-admission rejection; `_flush_timing` canonical + fails closed on float with the buffer retained and zero rows written; healthy writer survives a rejected pre-insert validation (`_write_failed` stays `False`, a subsequent valid event persists); audit `ACTUAL_SEND_ATTEMPT` canonical through the store.
- **Deterministic and network-free:** fixed constants (`BOUNDARY = 1792228500`, literal floats), fake clock, `MockTransport`. **PASS**

---

## 9. Fresh hash capture (working tree)

```
$ Get-FileHash -Algorithm SHA256 <path>
9FE1106631B5D57FC405DC76E0F337105029239E42F75FDCDB8E9D1F6F8073B8  scripts/cp5_run.py
2BCA463314A3D737AA0CF44DC7FA5C1E248B49FE4D70AF663244F5AA371F954D  scripts/cp5_supervise_durable.py
35902EE05C64D2E17BF65E6205B5FFB66339D41081677D104197F44B4D6992CA  src/polymarket_bot/recorder/store.py
72CEA586D84398F259175E4DDC8574CC7C3FDCFB67CBA5462EA6A0057C81D024  src/polymarket_bot/recorder/service.py
D4597A6B3D769B7F9CC6C40E20B044668B22FC0B142525EE58ED09A374484373  tests/test_cp59_opening_wait_evidence.py
90BEBB30854F373ABF601C8F98AD86DEDDFE9052895A241FA7BD67E9183CCCD2  scripts/cp5_analyze.py
FBE83C02C9BC5B941DC753717067EE7C7CD43ABB7C3708F2D094383A8861BF93  tests/test_cp57_arm_serialization_audit.py
```

- `scripts/cp5_supervise_durable.py` == frozen CP5.8 hash `2BCA4633...954D`. **PASS**
- `store.py` `35902ee0…` and `service.py` `72cea586…` match the CP5.9 checkpoint's stated unchanged-at-start hashes. **PASS**
- `cp5_run.py` working-tree hash `9FE11066…` is a new value (was `2FC4AA38…`-equivalent content / blob `c5805f8c` before `c4d68e8`); the worktree file uses CRLF while the git blob does not, so the working-tree SHA256 differs from the blob SHA256 — the identity change is confirmed by the blob hash/line-count change in §2a.

---

## 10. Tests, freshly run by me

Environment: `$env:PYTHONPATH='src;scripts'`; interpreter `.venv\Scripts\python.exe`; run from the worktree root.

```
$ python -m unittest -q tests.test_cp59_opening_wait_evidence
Ran 10 tests in 1.142s
OK
exit=0   (wall 1.8s)

$ python -m unittest -q tests.test_cp57_arm_serialization_audit
Ran 11 tests in 3.186s
OK
exit=0   (wall 3.8s)

$ python -m unittest discover -s tests -t . -q        # full-suite run #1
=== FILTERED SUMMARY ===
FAIL: test_public_get_stream_and_cookie_isolation (tests.test_cp5_safety.CP5SafetyTests.test_public_get_stream_and_cookie_isolation)
Ran 816 tests in 682.976s
FAILED (failures=1)
=== FULL-SUITE-EXIT=1 ELAPSED=683.6s ===

$ python -m unittest discover -s tests -t . -q        # full-suite run #2 (clean)
=== SUMMARY ===
Ran 816 tests in 664.731s
OK
=== TRACEBACKS (if any) ===
=== FULL-SUITE-EXIT=0 ELAPSED=665.3s ===

$ python -m unittest -q tests.test_cp5_safety                    # module, isolated
Ran 66 tests in 21.670s
OK
$ python -m unittest -q tests.test_cp5_safety.CP5SafetyTests.test_public_get_stream_and_cookie_isolation   # x5
run 1: Ran 1 test in 0.154s | OK
run 2: Ran 1 test in 0.173s | OK
run 3: Ran 1 test in 0.156s | OK
run 4: Ran 1 test in 0.163s | OK
run 5: Ran 1 test in 0.161s | OK
```

- `tests.test_cp59_opening_wait_evidence`: **Ran 10 tests, OK** (0 failures/errors), 1.142s. **PASS**
- `tests.test_cp57_arm_serialization_audit`: **Ran 11 tests, OK** (0 failures/errors), 3.186s. **PASS**
- Full discovery suite run #1: **Ran 816 tests in 682.976s, FAILED (failures=1)** — exit 1. The single failure was `tests.test_cp5_safety.CP5SafetyTests.test_public_get_stream_and_cookie_isolation`.
- Full discovery suite run #2: **Ran 816 tests in 664.731s, OK** — exit 0.
- The failing test then passed **6/6** subsequent runs (1x in-module via the 66-test `test_cp5_safety` module = OK; 5x in isolation = OK). It exercises sub-second deadlines (`deadline=time.monotonic()+0.01`), `time.sleep(0.03)`, a `ThreadPoolExecutor`, and stream/cookie isolation — i.e. it is timing-/load-sensitive. Verdict: **load/timing flake**, unrelated to the reviewed delta (its subject is HTTP stream/cookie isolation; the changed files are the OPENING_WAIT producer, `_flush_timing`, `_json`, and the fatality finalization). See non-blocking observation #6.

---

## 11. Findings

- **High: 0**
- **Medium: 0**

No High/Medium finding was identified. The reviewed change is a producer-side canonicalization that closes the exact CP5.8 failure (float OPENING_WAIT evidence), leaves the strict sink strict, and preserves scheduler/gate semantics.

---

## Verification scope

**Ran:**
- Read-only native Git: `rev-parse`, `branch --show-current`, `status --porcelain -uno`, `diff --check`, `diff --cached --check`, `log --oneline`, `diff --stat/--name-status`, `diff`, `show`, `rev-parse <commit>:<path>`, `cat-file blob`.
- Read-only `Get-FileHash -Algorithm SHA256` on 7 files (item 9).
- Read-only source inspection of `scripts/cp5_run.py` (full), `src/polymarket_bot/recorder/store.py` (relevant regions), `src/polymarket_bot/recorder/service.py` (relevant regions), `tests/test_cp59_opening_wait_evidence.py` (full), `tests/recorder_support.py`, `tests/test_cp57_arm_serialization_audit.py` (partial), `src/polymarket_bot/recorder/models.py`, `metadata.py`, `books.py`.
- Offline unit tests: `tests.test_cp59_opening_wait_evidence` (10, OK), `tests.test_cp57_arm_serialization_audit` (11, OK), full `unittest discover -s tests -t . -q` (result above).
- An in-memory `_json` probe via stdin (`PYTHONPATH=src`), no file created.

**Did NOT run / did not do:**
- No `scripts/cp5_supervise_durable.py` (run/launch/inspect).
- No live `scripts/cp5_run.py` (`--child`, `--aligned-child`, `--mode rollover/pilot`) and no boundary/lock/journal/DB/stream created or touched.
- No network (no curl/Invoke-WebRequest/httpx to the internet).
- No Pyright/typecheck, no lint (no configured project lint command; Pyright comparison is the CP5.9 harness's, not re-run here).
- No analysis of `data/` or `logs/` DB contents beyond read-only hashing; no CP5.8/CP5.6C DB semantic inspection.
- No edits, fixes, refactors, or commits.

---

## Disclosed non-blocking observations

1. **`store.py:491` residual** — `json.loads(..., parse_float=str)` lacks a `parse_constant` guard. `NaN`/`Infinity` in vendor CLOB config keys (`oas/itode/ibce/fd`, not coerced by `_normalize_clob_numbers`) would reach `_json` and fail closed (`fatal_invariant`), not silently persist. Not reachable with well-formed JSON. Latent hardening item; outside this delta's scope.
2. **`store.py:485` `default=str`** — used only for a `sha256` digest of selected gamma metadata; never fed to `_json`/`detail_json`. Not a contract relaxation.
3. **`round()` vs `wall_us()` truncation** — `round(wait_started * 1e6)` can differ from `time.time_ns()//1000` by ≤1 µs; OPENING_WAIT evidence is diagnostic-only. Deterministic and negligible.
4. **Cross-producer audit is static/manual**, not a runtime enumerator; corroborated by the full-suite pass and explicit `scripts/` coverage (the CP5.7 audit's gap).
5. **Line-ending artifact** — working-tree `cp5_run.py` SHA256 (`9FE11066…`) differs from the LF git-blob SHA256 (`BD0A9978…`) purely due to CRLF normalization; the content identity change is established by the blob-hash/line-count delta.
6. **Full-suite flake (disclosed).** Full-suite run #1 produced `Ran 816 tests in 682.976s / FAILED (failures=1)` on `tests.test_cp5_safety.CP5SafetyTests.test_public_get_stream_and_cookie_isolation`; run #2 produced `Ran 816 tests in 664.731s / OK`. The test passed 6/6 in isolation/in-module afterward. It is timing-/load-sensitive (sub-second deadlines, `time.sleep`, thread pool, stream/cookie isolation) and is unrelated to the changed files. This is a **non-blocking flake**, not a defect in the reviewed delta — but it means "full suite green" is not deterministic on this machine, and it is the one place my freshly observed result diverges from the CP5.9 checkpoint's "816 tests OK" claim.
7. **Full-suite runtime ~11 min**; results reported in §10.
