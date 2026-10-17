# CP5.10 — EXACT-CONFIG READINESS REVIEW (live rollover, frozen contract)

Fresh, independent, adversarial reviewer. READ-ONLY. No source/test/doc/artifact modified.
No launch, no `inspect`, no `run`/`launch` of `scripts/cp5_supervise_durable.py`. No SQLite
connection opened. No network. No `data/`/`logs/` write. The only file written is this report.

## Verdict

**Spec: PASS — quality: PASS — High 0 / Medium 0**, for the EXACT frozen configuration in
`docs/superpowers/cp5.10-rollover-validation-checkpoint-2026-10-18.md`.

The frozen argv, boundary, label stem, hashes, absence set, source behavior, strict-JSON
contract, public allowlist, and PASS/fail contract all reconcile with CURRENT source. No
gate/clock/quality/fallback/metadata-authority/network/audit weakening was found. Four
non-blocking observations are listed separately.

## Timestamps and deadline

| Item | Host UTC | Unix |
|---|---|---|
| Review start | `2026-10-17T13:40:37.3786290+00:00` | `1792244437` |
| Report write | `2026-10-17T13:42:34.2612353+00:00` | `1792244554` |
| Hard deadline `B−180` | `2026-10-17T13:57:00Z` | `1792245420` |

**Margin at write: `1792245420 − 1792244554 = 866 s` ≈ 14.4 min — DEADLINE MET.**

## Working state

- Worktree: `C:\Users\Andro Saptajulianto\Downloads\Polymarket-bot\.worktrees\cp5-data-quality`
- Branch `feat/cp5-data-quality`; HEAD `671f8775584a8f7da03e8ee8e1bfa10760bcb7b0` ✔ matches frozen.

---

## 1. Freeze identity

```
$ git rev-parse HEAD
671f8775584a8f7da03e8ee8e1bfa10760bcb7b0
$ git branch --show-current
feat/cp5-data-quality
$ git status --porcelain --untracked-files=no
(empty)
$ git diff --check
exit=0
$ git diff --cached --check
exit=0
```

PASS — HEAD/branch match; tracked+index clean; both whitespace checks exit 0 and empty.

## 2. Frozen SHA256 hashes (re-hashed now)

```
9FE1106631B5D57FC405DC76E0F337105029239E42F75FDCDB8E9D1F6F8073B8  scripts\cp5_run.py
2BCA463314A3D737AA0CF44DC7FA5C1E248B49FE4D70AF663244F5AA371F954D  scripts\cp5_supervise_durable.py
35902EE05C64D2E17BF65E6205B5FFB66339D41081677D104197F44B4D6992CA  src\polymarket_bot\recorder\store.py
72CEA586D84398F259175E4DDC8574CC7C3FDCFB67CBA5462EA6A0057C81D024  src\polymarket_bot\recorder\service.py
D4597A6B3D769B7F9CC6C40E20B044668B22FC0B142525EE58ED09A374484373  tests\test_cp59_opening_wait_evidence.py
90BEBB30854F373ABF601C8F98AD86DEDDFE9052895A241FA7BD67E9183CCCD2  scripts\cp5_analyze.py
```

PASS — all six byte-for-byte equal to contract §E (§E lists the same five plus `cp5_analyze.py`).
`cp5_supervise_durable.py` equals the CP5.8 value (unchanged); `cp5_run.py` is the new CP5.9
value (`9FE11066…`), not the CP5.8 `DB2ECE16…`.

## 3. Boundary arithmetic (independent)

```
B%900=0
900*1991384=1792245600
B%900==0 => True
B-180  1792245420  2026-10-17T13:57:00Z
B-120  1792245480  2026-10-17T13:58:00Z
B-60   1792245540  2026-10-17T13:59:00Z
B-30   1792245570  2026-10-17T13:59:30Z
B-22   1792245578  2026-10-17T13:59:38Z
B-12   1792245588  2026-10-17T13:59:48Z
B-1ms  1792245599  2026-10-17T13:59:59.999Z
B      1792245600  2026-10-17T14:00:00Z
B+2    1792245602  2026-10-17T14:00:02Z
B+4    1792245604  2026-10-17T14:00:04Z
B+6    1792245606  2026-10-17T14:00:06Z
B+8    1792245608  2026-10-17T14:00:08Z
B+10   1792245610  2026-10-17T14:00:10Z
CP5.8 boundary 1792228500 equals B? False
```

PASS — `1792245600 % 900 == 0`; every derived point matches contract §C exactly;
B ≠ CP5.8 boundary `1792228500`.

## 4. 11-path absence (stem `cp5_10_rollover_20261017`)

```
False data\cp5_10_rollover_20261017.sqlite3
False data\cp5_10_rollover_20261017.sqlite3-wal
False data\cp5_10_rollover_20261017.sqlite3-shm
False data\cp5_10_rollover_20261017.sqlite3-journal
False data\cp5_10_rollover_20261017.sqlite3.writer.lock
False data\cp5_10_rollover_20261017.sqlite3.partial
False data\cp5_10_rollover_20261017.sqlite3.supervision.lock
False logs\cp5_10_rollover_20261017.jsonl
False logs\cp5_10_rollover_20261017.jsonl.stdout
False logs\cp5_10_rollover_20261017.jsonl.stderr
False logs\cp5_10_rollover_20261017.jsonl.interrupt
--- parents ---
data exists: True
logs exists: True
--- cp5_10* under data --- (none)
--- cp5_10* under logs --- (none)
```

PASS — all eleven absent; parents exist as directories; no `cp5_10*` entry under either parent.
Absence is not reservation/global-process proof; the eleven must be rechecked immediately
before launch (contract §D).

## 5. Frozen argv vs real supported CLI (source-confirmed)

Read CURRENT `scripts/cp5_supervise_durable.py` and `scripts/cp5_run.py`.

- Subcommand `launch` exists: `cp5_supervise_durable.py:286` `for action in ('run','launch')`. ✔
- Supervisor flags exactly `--journal/--db/--boundary(int)/--watchdog(float)` + REMAINDER
  `command`: `:288–292`. `boundary` `type=int`, `watchdog` `type=float`. ✔
- `--` stripped: `:77` `command = args.command[1:] if args.command[:1] == ['--'] else args.command`. ✔
- Non-empty command + positive finite watchdog: `:78`. `1015` is finite and positive. ✔
- Child flags `--aligned-child --db --seconds --mode --intended-boundary`:
  `cp5_run.py:421–427`. Modes `('rollover','pilot')`. ✔
- Both `--db` values in the frozen argv are the identical `…\data\cp5_10_rollover_20261017.sqlite3`. ✔
- Both boundary values are the identical `1792245600`. ✔
- `--watchdog 1015` finite/positive (checked at `:78`). ✔
- **No `--execution-uuid` flag exists.** `inspect` takes one positional journal (`:293–294`);
  the UUID is generated internally at `:84` `execution = str(uuid.uuid4())` and written only to
  the `.supervision.lock` (`:87`) and every journal record (`:100`). Grep confirms no CLI surface.
- `launch` re-spawns detached `run` with `*sys.argv[2:]` (`:301`), so the child `run` receives the
  same flags. ✔

PASS — the frozen argv is exactly what the current CLIs accept.

## 6. Critical-path outcome rules (source-confirmed)

`run(args)` (`cp5_supervise_durable.py:74–305`):

- Collision refusal: `:80–83` — refuses if any of journal, `<journal>.stdout`, `<journal>.stderr`,
  DB, `-wal`, `-shm`, `-journal`, `.writer.lock`, `.partial` already exists (`FileExistsError`). ✔
- Exclusive `.supervision.lock` reservation: `:85–89` `guard.open('x')` (exclusive create) +
  write + flush + `fsync`; re-check absence after reservation `:90–91`. ✔
- **`.interrupt` is NOT in the supervisor collision set.** `paths + db_paths` (`:80–81`) omit it.
  It is a *runtime* self-interrupt trigger only: `:151` and `:216` raise/transition when
  `<journal>.interrupt` appears. This is the disclosed limit (contract §D) and is why the
  review checks the eleven paths explicitly. ✔

`child(path, seconds, mode=..., intended_boundary=...)` (`cp5_run.py:319–337`):

- New DB required: `:325` `path.exists()` → `ValueError('CP5 requires new bounded database')`. ✔
- `seconds == 70` for rollover: `:328`. ✔
- `intended_boundary % 900 == 0` and integer: `:329`. ✔
- Rejects `now >= B−22`: `:335` `require_admission` → `:269–271` `if now >= intended_boundary-22: raise`. ✔
- Rollover deadline capped at `B+10`: `:336–337`
  `min(fixed_deadline_ns, acquisition_start_ns + round((intended_boundary + (10 if rollover else 1140) - time.time())*1e9))`. ✔

PASS.

## 7. Source delta vs pre-CP5.8 baseline (`1759f88 → 671f877`)

```
$ git diff --stat 1759f88 671f877
...test-cp57-arm-serialization-audit-remediation-checkpoint-2026-10-17.md | 194 ++
...docs/superpowers/cp5.7-independent-review-2026-10-17.md              |  72 ++
...docs/superpowers/cp5.7-rereview-2026-10-17.md                        |  71 ++
...docs/superpowers/cp5.9-independent-review-2026-10-18.md              | 238 ++
...docs/superpowers/cp5.9-opening-wait-remediation-checkpoint-2026-10-18.md | 183 ++
scripts/cp5_run.py                                                      |  34 +-
src/polymarket_bot/recorder/service.py                                  |  11 +-
src/polymarket_bot/recorder/store.py                                    |  18 +-
tests/test_cp57_arm_serialization_audit.py                              | 389 ++
tests/test_cp59_opening_wait_evidence.py                                | 180 ++
10 files changed, 1377 insertions(+), 13 deletions(-)
```

**`scripts/cp5_run.py` DID change** (CP5.9, +25/−9 net across the shown hunks). The CP5.8-era
"gate-equivalence because `cp5_run.py` unchanged" claim is therefore **SUPERSEDED** and must be
re-argued here (done below).

`git diff 1759f88 671f877 -- scripts/cp5_run.py` shows exactly three changes:

1. `+from polymarket_bot.recorder.store import _json`; two `_flush_timing` inserts change
   `detail_json=json.dumps(detail)` → `detail_json=_json(detail)` (`:53`, `:56`).
2. New `opening_wait_evidence(...)` producer (`:117–120`) emitting **ints only**:
   `intended_boundary` passthrough, `wait_started_wall_us=round(wait_started*1e6)`,
   `wait_ended_wall_us=round(wait_ended*1e6)`, `wait_end_ns`. The old float keys
   `wait_started`/`wait_ended` are **renamed, not reinterpreted**.
3. Pure lifts reproducing the previously inline scheduler arithmetic:
   `aligned_wait_delay(B, now)` = `B−60−now` with guard `0 <= delay <= 900` (`:262–266`),
   `require_admission(B, now)` = reject `now >= B−22` (`:269–271`); the `--aligned-child` block
   (`:431–437`) and `child()` (`:335`) now call these pure helpers with identical arithmetic.

Changed files under `src/` and `scripts/` in the delta, and gate impact:

| File | Change | Touches a rollover gate / clock / quality / fallback / metadata / network / audit? |
|---|---|---|
| `scripts/cp5_run.py` | CP5.9 producer canonicalization + pure arithmetic lifts | **No gate change.** `phase_verdict` (`:288–302`) unchanged; scheduler arithmetic identical; producer is diagnostic-only (see below). |
| `src/polymarket_bot/recorder/store.py` | CP5.7 `_json`/`canonical` fail-closed canonicalizer; `record_event` uses precomputed `payload` (`:938`→`:949`) | **Strengthens** the JSON contract. No gate logic. Byte-identical output for previously supported values; `record_event` still stores `_json(detail)`. |
| `src/polymarket_bot/recorder/service.py` | CP5.7 failure-path recovery block in `run()` (`:1165–1173`) | **Failure path only.** On `RecorderStoreError('fatal_invariant')` with an un-failed writer it stops workers, does final persistence, `recover_interrupted`, `finish_run(state='FAILED')`, then still `return RunResult(..., "FAILED", 1, ...)`. Cannot turn a FAIL into a PASS. |

**Rollover PASS-gate equivalence re-argued for the CURRENT delta.** `opening_wait_evidence` is
recorded only as a `DISCOVERY_GAP` event with `type='OPENING_WAIT'`, `diagnostic_only=True`
(`service.py:1260–1263`). The rollover PASS gate is `intended_phase()` (`cp5_analyze.py:528–566`),
which reads only `PREPARED_ARM`, `PREPARED_ACTIVATION`, `ACTUAL_SEND_ATTEMPT`, `sample_slots`, and
the summary integrity/`replay.exact`/`user_version`/`isolation`. It never reads `OPENING_WAIT`,
`SCHEDULER_TIMING`, or `DISCOVERY_GAP`. `replay_rows`/`_replay_rows` (`:11–130`) processes **only**
`detail['type'] == 'APPROXIMATE_FETCH'` events and skips all others, so the `_json` re-encoding of
`SCHEDULER_TIMING` details cannot change `replay.exact`. `slot_diagnosis` (`:606–667`) consumes
`SCHEDULER_TIMING` only for diagnostic timelines, not the verdict. Therefore: no changed file
touches the rollover PASS gate, clock policy, quality threshold, fallback selection, metadata
authority, network route, or audit semantics in a gate-affecting way. The producer change is a
strict compatibility fix: pre-CP5.9, float `wait_started`/`wait_ended` keys would be rejected by
the CP5.7 strict `_json` (`fatal_invariant`) when the `OPENING_WAIT` event is written; the integer
producer removes that startup-failure mode without altering any gate.

PASS.

## 8. Strict Recorder JSON contract

`store.py:28–44`:

```python
def _json(value) -> str:
    def canonical(item):
        if item is None or type(item) in (bool, str): return item
        if type(item) is int and -(2**63) <= item <= 2**63 - 1: return item
        if type(item) is Decimal: return decimal_text(item)
        if type(item) is list or type(item) is tuple: return [canonical(c) for c in item]
        if type(item) is dict and all(type(key) is str for key in item): return {k: canonical(v) ...}
        raise TypeError("Unsupported recorder JSON value.")
    try:
        return json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError, MarketDataError) as exc:
        raise RecorderStoreError("fatal_invariant", "Recorder JSON evidence is not supported.") from exc
```

`record_event` (`:926–949`) computes `payload = _json(detail)` at `:938` and stores it at `:949`.
No `default=str`; no float relaxation.

Throwaway in-memory probe (no file writes; `PYTHONPATH=src;scripts`):

```
finite_float  => RecorderStoreError code= fatal_invariant msg= Recorder JSON evidence is not supported.
nan           => RecorderStoreError code= fatal_invariant msg= ...
inf           => RecorderStoreError code= fatal_invariant msg= ...
plain_object  => RecorderStoreError code= fatal_invariant msg= ...
nonstr_key    => RecorderStoreError code= fatal_invariant msg= ...
good_int_list => OK {"a":1,"b":[1,2],"c":null,"d":true,"e":"s","f":null}
tuple         => OK [1,2]
big_int       => RecorderStoreError code= fatal_invariant msg= ...   (2**70 out of int64 range)
```

PASS — finite float / NaN / Inf / unknown object / non-str key / out-of-range int all raise
`RecorderStoreError('fatal_invariant', …)`. `tuple`→list and int64 ints are accepted, which is
not a float relaxation.

## 9. Public allowlist / safety (unchanged, source-confirmed)

`AuditTransport.handle_request` (`cp5_run.py:160–241`):

- `https` only, port `None/443`, no userinfo/fragment: `:175`. ✔
- Host/route patterns: Coinbase `api.exchange.coinbase.com` → `/products/BTC-USD/ticker`;
  Kraken `api.kraken.com` → `/0/public/Ticker`; Gamma → `/events|/events/slug/[A-Za-z0-9_-]+|/markets/[0-9]+`;
  CLOB → `/time|/book|/books|/clob-markets/0x[0-9a-fA-F]{64}`: `:162–167,176–177`. ✔
- Credential-like header rejection (`authorization`,`cookie`,`api-key`,`apikey`,`signature`,
  `passphrase`,`secret`,`token`), with `_`→`-` normalization: `:169–171`. ✔
- Duplicate query-selector rejection: `:179–181`. ✔
- GET body forbidden: `:183–184`. ✔
- Kraken `pair=XBTUSD&assetVersion=1`: `:188–189`. ✔
- POST `/books` exactly two distinct decimal token dicts (`{'token_id'}` only, distinct):
  `:195–204`. ✔
- Redirect rejection (`300 <= status < 400` → close + raise): `:221,226–228`. ✔
- `set-cookie` stripping: `:224–225`. ✔

Child client construction (`cp5_run.py:340–342`): `httpx.HTTPTransport(retries=0, trust_env=False)`;
`httpx.Client(transport=…, trust_env=False, auth=None, follow_redirects=False)`. ✔

`max_response_bytes`: `RunConfig` default `10_485_760` (`models.py:55`); the live read path enforces
the same cap `10 * 1024 * 1024 = 10_485_760` hard-coded in `PublicGet._receive_body`
(`http.py:251,256,260`). ✔

No credential/private/wallet/order/signing path exists: grep over `src/` found no
private-key/wallet/order/signing code; the only credential-like tokens are the *rejection*
lists in `cp5_run.py:170` and `http.py:151–154`. ✔

PASS.

## 10. Pass/fail contract

`phase_verdict` (`cp5_run.py:288–302`, unchanged in the delta):

```python
if supervisor.get('watchdog_killed') or supervisor.get('returncode') != 0:
    return dict(verdict='FAIL', reason='SUPERVISOR_FAILURE')
if supervisor.get('stderr'):
    return dict(verdict='FAIL', reason='UNEXPECTED_STDERR')
if evidence.get('result', {}).get('reason') == 'opening_blocked':
    return dict(verdict='BLOCKED', reason='PREPARATION_UNAVAILABLE')
valid = (evidence.get('result', {}).get('state') == 'STOPPED'
    and evidence.get('result', {}).get('exit_code') == 0
    and evidence.get('integrity') in ([('ok',)], [['ok']]) and not evidence.get('foreign_keys')
    and evidence.get('user_version') == 2 and evidence.get('writer_reacquired')
    and evidence.get('client_closed') and evidence.get('transport_closed')
    and not evidence.get('worker_active') and not evidence.get('remaining_threads')
    and evidence.get('intended_phase', {}).get('verdict') == 'PASS')
return dict(verdict='PASS' if valid else 'FAIL', reason='INTENDED_PHASE_EVIDENCE')
```

PASS requires: supervisor exit 0 + no watchdog kill + empty stderr; DB integrity `ok`, empty FK,
`user_version 2`, writer reacquired, client closed, transport closed, no active worker, no
remaining threads, result state `STOPPED`, `exit_code 0`, and `intended_phase` verdict `PASS`.
`opening_blocked` → BLOCKED/PREPARATION_UNAVAILABLE.

**Process `COMPLETED` alone is NOT product PASS.** The supervisor records
`process_only=state == 'COMPLETED'` (`cp5_supervise_durable.py:274`); that only means the child
process exited 0. Product PASS is decided solely by `phase_verdict`, which additionally requires
the full `intended_phase` evidence. The frozen PASS contract is unchanged in the delta.

PASS.

## 11. Historical custody (read-only hashes, no SQLite opened)

```
A488583880D10507A24E82251D0870433C1D39160B77429820A2D339A54CA409  data\cp5_6c_replacement_20261017_0351adeb-6594-492e-9237-adf7d833dba0.sqlite3
38B4A3C4A9ABB4A042AF294B0E46705683D7B551E6CE0D755BEE716C1C047337  data\cp5_8_rollover_20261018.sqlite3
```

PASS — both match the expected custody hashes byte-for-byte. `Get-FileHash` only; no connection.

---

## Runtime disposition

- **Execution NOT LAUNCHED.** No `PREPARED`, no supervisor, no child, no network.
- Live gates **NOT RUN / UNAVAILABLE** — not zero, not PASS: metadata resolution, ARM/activation,
  opening slots 0–4, raw/durable audit bijection, exact replay, DB/lifecycle, and the live
  `intended_phase` verdict are all unexercised. They cannot be evaluated read-only.
- The single-use live authorization is UNCONSUMED at this review.
- No new `data/`/`logs/` artifact created by this review.

## Non-blocking observations

1. `.interrupt` is outside the supervisor's own collision set (`cp5_supervise_durable.py:80–81`).
   Disclosed (contract §D) and covered by the explicit eleven-path absence check; a stale
   `.interrupt` would self-interrupt at `:151/:216`. Currently absent.
2. `_flush_timing` now persists `DISCOVERY_GAP`/`SCHEDULER_TIMING` `detail_json` via `_json`
   (compact, sorted) instead of `json.dumps` (spaced, unsorted) — a byte-level encoding change of
   diagnostic evidence only. Verified not to affect the PASS gate: `replay` consumes only
   `APPROXIMATE_FETCH`; `intended_phase` ignores these types; all timing detail values are
   int/str/bool/None so no `fatal_invariant` is triggered.
3. `RunConfig.max_response_bytes` (`models.py:55`) is declared but not read at the HTTP call site;
   `PublicGet._receive_body` enforces an equal hard-coded `10 * 1024 * 1024` cap. Same numeric
   value; no effective weakening.
4. The review is read-only, so absence of the eleven paths is not reservation or global-process
   proof; they must be rechecked immediately before any launch (contract §D).

## Handoff

- **B = `1792245600` = `2026-10-17T14:00:00Z`** (window `B..B+900`).
- **Exact-config review deadline = `B−180` = `1792245420` = `2026-10-17T13:57:00Z`** —
  **MET** with 866 s margin (written 13:42:34Z).
- **The frozen configuration is READY** — Spec PASS; quality PASS; High 0 / Medium 0.
- Publication of this report does not run, reserve, or otherwise consume the live authorization.
