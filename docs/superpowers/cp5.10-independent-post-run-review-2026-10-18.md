# CP5.10 — INDEPENDENT POST-RUN EVIDENCE REVIEW (live rollover, adversarial)

Date: 2026-10-18. Reviewer: fresh, independent subagent. I authored none of the code, the fix,
the frozen contract, or the operator's conclusions. Adversarial posture: I actively tried to
falsify every operator claim from the raw artifacts.

Worktree: `C:\Users\Andro Saptajulianto\Downloads\Polymarket-bot\.worktrees\cp5-data-quality`
Run under review: stem `cp5_10_rollover_20261017`; `B = 1792245600 = 2026-10-17T14:00:00Z`;
frozen invocation launched once (supervisor `PREPARED` at `1792245465.969Z`).

READ-ONLY. No source/test/artifact modified. No commit. No launch. No `cp5_supervise_durable.py`
run/launch/inspect. No `cp5_run.py --child/--aligned-child/--mode`. No network. No DB/journal/lock
created, deleted, or written. Only file written: this report.

---

## VERDICT

**Spec PASS; quality PASS; High 0 / Medium 0** — for the executed run's evidence.

- Every operator claim (items 1–9) was independently reproduced from the raw artifacts. No claim was
  falsified. Two figures in claim 4 are imprecise but immaterial (see Non-blocking #2).
- **CP5.9 live regression gate: PASSED.** Exactly one canonical-integer `OPENING_WAIT` payload is
  persisted durably; no old float keys; the pre-fix `fatal_invariant` failure mode did not occur.
- **Rollover opening PASS gates: NOT reached / NOT exercised.** No successor market was ever obtained,
  so `PREPARED_ARM`, `PREPARED_ACTIVATION`, and successor opening slots 0–4 do not exist. The product
  outcome is a legitimate `FAILED` / `opening_blocked` (per the frozen contract this maps to
  `BLOCKED` / `PREPARATION_UNAVAILABLE`). CP5.10 did **not** validate the rollover hand-off — it
  validated only the CP5.9 opening-wait producer fix. This is stated plainly and is not a defect in
  the reviewed delta.
- I **agree** with the operator's root cause (Gamma `/events` payload growth × a fixed 10 s
  preparation budget), with a small numeric correction.

---

## 1. Supervisor gate (operator claim 1) — VERIFIED

Journal `logs\cp5_10_rollover_20261017.jsonl` (344,060 bytes):

```
$ python -c "<parse journal>"
lines: 89
top-level event counts: PREPARED=1, STARTED=1, STDOUT=86, COMPLETED=1
lines with execution_uuid == c53b5da9-e3a6-4db7-9780-60a7243bf3c6: 89
other execution_uuid values: {}
```

- Exactly **one** `PREPARED` (line 1), **one** `STARTED` (line 2), **one** terminal `COMPLETED`
  (line 89). No `SIGNAL`, `WATCHDOG_EXPIRED`, `WATCHDOG_KILLED`, `KILL_WAIT_EXPIRED`, `LATE_SIGNAL`,
  or `FINALIZATION_ERROR` records — **no watchdog/kill/interrupt semantics occurred**.
- Exactly **one execution UUID** `c53b5da9-e3a6-4db7-9780-60a7243bf3c6`, present on **every** line.
  `data\…sqlite3.supervision.lock` contains exactly:

```json
{"execution_uuid": "c53b5da9-e3a6-4db7-9780-60a7243bf3c6",
 "journal": "C:\\Users\\Andro Saptajulianto\\Downloads\\Polymarket-bot\\.worktrees\\cp5-data-quality\\logs\\cp5_10_rollover_20261017.jsonl"}
```

- Child PID present: `STARTED` → `child_pid: 28440`.
- Ordering valid: `PREPARED` (utc 1792245465.969) → `STARTED` (1792245465.982) → `STDOUT` (…,
  ending utc 1792245600.512) → `COMPLETED` (utc 1792245600.527). Monotonic non-decreasing.
- Retained streams: `.stdout` = 133,103 bytes; `.stderr` = 0 bytes.
- Terminal record (line 89):

```json
{"event":"COMPLETED","execution_uuid":"c53b5da9-…","returncode":0,"error":null,
 "child_stopped":true,
 "evidence_hashes":{"…jsonl.stdout":"dcb1ca851c4c6e493600def928fc6a27054367c87ae2495ecdbe8405eb4415f6",
                    "…jsonl.stderr":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
 "classification":"UNAVAILABLE_OVERSIZE","semantic_reason":null,"result_line":null,
 "classification_record_limit":65536,"process_only":true}
```

The two `evidence_hashes` equal the independently re-hashed `.stdout` / `.stderr` (§ Freeze &
custody below). **No duplicated execution identity.**

**The 65536-byte record limit exists in source** (`scripts/cp5_supervise_durable.py`):

```
156:        result_line = None
163:        def classify(chunk):
167:                    if len(pending) + len(piece) > 65536:
170:                        classification = 'UNAVAILABLE_OVERSIZE'
172:                        result_line = None
190:                            result_line = line_number
273:           classification=classification, semantic_reason=semantic_reason, result_line=result_line,
274:           classification_record_limit=65536, process_only=state == 'COMPLETED')
```

Quoted limit (line 167): `if len(pending) + len(piece) > 65536:` — the supervisor abandons
classification of any single newline-terminated record exceeding 65,536 bytes.

**Product-result recoverability + fidelity — CONFIRMED.** The child's final evidence line is
**66,061 bytes** (`> 65536`), which is exactly why the supervisor set `UNAVAILABLE_OVERSIZE` /
`result_line: null` / `process_only: true`. The line is nevertheless **fully retained** in `.stdout`:

```
$ python (concat journal STDOUT 'data' frames)  -> sha256 dcb1ca85…4415F6, len 133103
$ python (concat journal STDOUT 'base64' frames)-> sha256 dcb1ca85…4415F6, len 133103
$ sha256(.stdout)                               -> dcb1ca85…4415F6, len 133103
data==stdout: True   base64==stdout: True
```

Both the plain-text frame and the base64 frame of the journal reconstruct the `.stdout` **byte for
byte**, and it hash-matches. Recovery of the product result from `.stdout` is therefore **faithful**.
Because `classification != 'LAST_TOP_LEVEL_RESULT'`, `state` stayed `COMPLETED` (line 246 requires a
classifiable `opening_blocked` reason to relabel to `BLOCKED`). The supervised terminal state is
therefore *process-only* and is **not** the product verdict (see Non-blocking #1).

---

## 2. CP5.9 live regression gate (operator claim 2) — VERIFIED — PASSED

`RecorderStore.record_event` enforces a strict `kind` allowlist
(`src/polymarket_bot/recorder/store.py:935-936`):

```python
_require(kind in ("STARTUP", "SHUTDOWN", "RESTART", "DISCOVERY_GAP",
                "CLOCK_PAUSE", "IDENTITY_CONFLICT", "STORAGE_FAILURE")
         and type(detail) is dict, "Invalid recorder event.")
```

So `OPENING_WAIT` cannot be a `kind`; it is carried in `detail["type"]` on a `kind='DISCOVERY_GAP'`
row. The row (read-only SQL):

```
$ sqlite3  (file:…?mode=ro)
SELECT kind, market_id, started_us, detail_json FROM recorder_events
  WHERE detail_json LIKE '%OPENING_WAIT%';
```

```
kind        = DISCOVERY_GAP
market_id   = NULL
started_us  = 1792245540169835
detail      = {"diagnostic_only": true, "intended_boundary": 1792245600,
               "run_id": "e7f59343-11f4-42d9-8e7f-a98d7dab9ac1", "type": "OPENING_WAIT",
               "wait_end_ns": 179894390000000,
               "wait_ended_wall_us": 1792245540021481,
               "wait_started_wall_us": 1792245466332680}
```

- **Exactly ONE** `OPENING_WAIT` row exists (count = 1). ✔
- Canonical **ints** (JSON-parsed Python types):

```
intended_boundary     = 1792245600          type=int
wait_started_wall_us  = 1792245466332680    type=int
wait_ended_wall_us    = 1792245540021481    type=int
wait_end_ns           = 179894390000000     type=int
```

- OLD float keys `wait_started` / `wait_ended`: **ABSENT** (`[]`). ✔
- `wait_ended_wall_us` = 1792245540021481 µs = `1792245540.021481 s` → consistent with the frozen
  **B−60 = 1792245540 s** contract (≈21 ms of sleep overshoot). ✔
- No `fatal_invariant` / `RecorderStoreError` anywhere: full-DB token scan across **all** tables and
  columns returned `fatal_invariant: 0`, `RecorderStoreError: 0`, `audit_persistence_failed: 0`,
  `unsupported: 0`. ✔
- `recording_runs.exit_reason = 'opening_blocked'` (not a JSON/unsupported-evidence failure). ✔
  The only occurrence of the token `opening_blocked` in the DB is that one `exit_reason` cell.
- The matching line 0 of `.stdout` prints the same four values byte-identically to the persisted row.

**Conclusion: the CP5.9 live regression gate PASSED** (the CP5.8 float-evidence `fatal_invariant`
did not recur; exactly one canonical-int `OPENING_WAIT` was persisted durably).

---

## 3. Product outcome / rollover failure (operator claim 3) — VERIFIED

From the last line of `.stdout` (66,061 bytes, hash-matched):

```
result = {"run_id": "e7f59343-11f4-42d9-8e7f-a98d7dab9ac1", "state": "FAILED",
          "exit_code": 2, "slots_finalized": 19, "reason": "opening_blocked"}
intended_phase = {"verdict": "FAIL", "reason": "INTENDED_MARKET_UNAVAILABLE"}
```

From the DB:

```
$ SELECT run_id,state,exit_reason,mode,started_wall_us,ended_wall_us,last_committed_wall_us
  FROM recording_runs;
run_id              = e7f59343-11f4-42d9-8e7f-a98d7dab9ac1
state               = FAILED
exit_reason         = opening_blocked
mode                = APPROXIMATE_REFERENCE
started_wall_us     = 1792245540141024   (13:59:00.141Z)
ended_wall_us       = 1792245599895949   (13:59:59.896Z)
last_committed_wall_us = 1792245597658832
```

`state=FAILED, exit_code=2, reason=opening_blocked` and `intended_phase = FAIL /
INTENDED_MARKET_UNAVAILABLE` are both reproduced. Note the OS-process exit code seen by the
supervisor is `0` (the frozen argv runs `cp5_run.py --aligned-child`, which prints evidence and
returns normally); the `exit_code=2` is the recorder's own `RunResult` field, not the process exit
status (see Non-blocking #1).

---

## 4. Root cause (operator claim 4) — VERIFIED; I AGREE (minor numeric corrections)

### 4a. Source constants (independently read)

- `src/polymarket_bot/recorder/service.py:873` — `phase_end = phase_start+10_000_000_000` (fixed 10 s
  budget handed to `acquire_next_market` at `:883-884`, `deadline=phase_end/1e9`). ✔
- `src/polymarket_bot/recorder/service.py:836` — `lead = 30_000_000 if stage == 'NEW' …`
  (`_preparation_wake_ns` 30 s lead). ✔
- `src/polymarket_bot/market_data/discovery.py:84` — `for offset in range(0, 500, 100):` (offsets
  0,100,200,300,400 = up to **5 pages**). ✔
- `src/polymarket_bot/market_data/gamma.py:20` — `"limit":100, "offset":offset, …`. ✔

### 4b. `request_attempts` for `/events` (read-only SQL)

```
SELECT attempt_number,method,path,selector_json,started_wall_us,ended_wall_us,
       http_status,result,error_code,response_bytes
FROM request_attempts WHERE path='/events' ORDER BY started_wall_us;
```

| offset | http_status | result | error_code | response_bytes | dur |
|---:|---:|---|---|---:|---:|
| 0   | 200 | success | null | 5,968,744 (5.97 MB) | 3.29 s |
| 100 | 200 | success | null | 7,865,841 (7.87 MB) | 1.78 s |
| 200 | 200 | success | null | 9,602,021 (9.60 MB) | 2.26 s |
| 300 | 200 | success | null | 10,079,943 (10.08 MB) | 2.41 s |
| 400 | **None** | **failure** | **transport_error** | **0** | ~0.08 s |

- Each page **5.9–10.1 MB** — matches the operator. ✔
- 4 pages spanned 9.71 s by raw-send wall times (`1792245569597639 → 1792245579303741`), 9.86 s by
  DB `request_attempts` times. Operator's "~9.7 s" matches the raw sends. ✔
- The 5th page (offset 400) was issued at `started_wall_us = 1792245579507510` and aborted at
  `1792245579586976` (duration 79.5 ms) with `ReadTimeout`.

### 4c. Raw audit send for the failure

```
attempt 38: GET gamma-api.polymarket.com /events
started_wall_us=1792245579507510 ended_wall_us=1792245579586976
status=null  error="ReadTimeout"  complete=false  stream_closed=false
request_bytes=0  response_bytes=0  redirect=false  set_cookie_present=false
```

`status: null`, `error: "ReadTimeout"`, `complete: false` — matches the operator exactly. ✔

### 4d. `PREPARATION_PHASE` rows

```
{"type":"PREPARATION_PHASE","phase":"NEW","reason":"START","failure_reason":null,
 "started_ns":179923937000000,"deadline_ns":179933937000000,"ended_ns":179923937000000,…}
{"type":"PREPARATION_PHASE","phase":"NEW","reason":"transport_error","failure_reason":"transport_error",
 "started_ns":179923937000000,"deadline_ns":179933937000000,"ended_ns":179934312000000,…}
```

- One `reason=START` (failure_reason null); one `reason=transport_error` with
  `failure_reason=transport_error`. ✔
- Both spans `deadline_ns − started_ns = 10,000,000,000 ns = 10.000 s exactly`. ✔
- The `transport_error` row ends `ended_ns − deadline_ns = 375 ms` **after** its deadline.
- **Decisive detail:** the failing send's `ended_mono_ns = 179933937000000` equals the phase
  `deadline_ns = 179933937000000` **exactly** — the read was aborted precisely at the fixed 10 s
  deadline. The ReadTimeout is deadline-driven, not a spontaneous network stall. This corroborates
  the root cause.

### 4e. Payload-growth comparison and successor absence

```
$ sqlite3  data\cp5_6c_replacement_…-adf7d833dba0.sqlite3  (ro)
/events rows: response_bytes 1,570,964 and 1,570,904   (~1.57 MB/page)
markets:      [('btc-updown-15m-1792192500', …)]        (predecessor only)
```

Today's pages (5.97–10.08 MB) vs CP5.6C (~1.57 MB): **3.8×–6.4× growth**. Today's payload growth is
the **credible trigger**. ✔

`markets` (this run) holds **only the predecessor**:

```
market_id=5775413  slug=btc-updown-15m-1792244700
window_start_us=1792244700000000 (2026-10-17T13:45:00Z)
window_end_us  =1792245600000000 (2026-10-17T14:00:00Z)
```

No successor. ✔ The CP5.3 readiness run likewise recorded **only its predecessor**
`btc-updown-15m-1792103400` and ended `state=FAILED / exit_reason=opening_blocked` with **0** `/events`
rows — i.e. it too **never recorded a successor**. ✔

### 4f. Agreement

I **agree** with the operator's root cause: the successor was never obtained because
`_prepare_next` grants `acquire_next_market` a **fixed 10 s** budget while `discover_next` must
enumerate up to **5** Gamma `/events` pages; on this date each page was 5.9–10.1 MB, four pages
consumed ~9.7 s, and the 5th page (offset 400) was issued with only ~61 ms of budget left and was
aborted with `ReadTimeout` at the deadline → `transport_error` → no `_prepared` → `opening_blocked`.

Two numeric corrections (immaterial): the 5th page had **~61 ms** of budget left (not ~78 ms), and
4 pages consumed **9.7 s (raw) / 9.86 s (DB)**. The operator's "~78 ms" appears to be the failing
request's own ~79.5 ms duration; either way it is < 100 ms. Conclusion unchanged.

---

## 5. Gates NOT reached (operator claim 5) — VERIFIED (with a labeling note)

```
$ SELECT COUNT(*) FROM recorder_events
  WHERE detail_json LIKE '%PREPARED_ARM%' OR detail_json LIKE '%PREPARED_ACTIVATION%';
0
$ SELECT status, COUNT(*) FROM sample_slots GROUP BY status;
COMPLETE 19 ; MISSED 431          (total 450)
$ SELECT DISTINCT market_id FROM sample_slots;
5775413                            (predecessor only)
$ sample_slots WHERE slot_index < 5;
slot 0..4 : status=MISSED, scheduled_us 1792244700000000..1792244708000000
```

- `PREPARED_ARM` = 0, `PREPARED_ACTIVATION` = 0. ✔
- No **successor** slots 0–4 exist: the only `sample_slots` are the predecessor's own 450-slot
  ledger (`market_id 5775413`, slot_index 0–449; **19 COMPLETE / 431 MISSED**). ✔
- **The rollover opening gates were NOT EXERCISED.** `intended_phase` short-circuits at the first
  gate — no market whose window is `B..B+900`:

```python
# scripts/cp5_analyze.py:528-532
def intended_phase(summary, markets, slots, events, boundary_us, mode):
    selected = [row for row in markets if row['window_start_us'] == boundary_us
        and row['window_end_us'] == boundary_us+900_000_000]
    if len(selected) != 1:
        return dict(verdict='FAIL', reason='INTENDED_MARKET_UNAVAILABLE')
```

- Frozen pass/fail contract (`scripts/cp5_run.py:288-302`), quoted verbatim:

```python
def phase_verdict(supervisor, evidence):
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

So `opening_blocked` → `BLOCKED` / `PREPARATION_UNAVAILABLE`. ✔

Labeling note: the operator refers to the opening gates as "Phases 10 and 12". No such numbering
exists anywhere in this worktree's `docs/**` (grep for `Phase 10|Phase 12` → no matches). The
**substance** of the claim is fully verified (no arm, no activation, no successor opening slots), but
the phase numbering is not independently mappable from repository artifacts. See Non-blocking #3.

---

## 6. Audit bijection (operator claim 6) — VERIFIED, EXACT, NON-VACUOUS

Raw sends are the 66 `actual_send` lines in `.stdout` (and, identically, `evidence.sends`).
Durable rows are the 66 `detail['type'] == 'ACTUAL_SEND_ATTEMPT'` recorder events.

```
raw actual_send lines            : 66
durable ACTUAL_SEND_ATTEMPT rows : 66
evidence.sends == raw lines      : True
raw keys  ⊃ durable-only keys    : ['type']        (durable adds exactly one 'type' field)
raw-only keys                    : []
missing   (raw w/o durable)      : 0
duplicate raw attempts           : 0
foreign   (durable w/o raw)      : 0
field mismatches after dropping 'type' (attempt, method, host, path, started/ended wall+mono,
                                        status, error, owner/run_id, identity) : 0
```

- **66 ↔ 66**, missing 0, duplicate 0, foreign 0, and after dropping the single added `type` field,
  **all** matched fields are equal on **every** row. ✔
- `audit_persistence_failed` is **absent** (full-DB token scan: 0). ✔
- **Not vacuous:** 66 real outbound sends occurred (19 `POST /books`, 19 `GET coinbase
  /products/BTC-USD/ticker`, 19 `GET kraken /0/public/Ticker`, 5 `GET gamma /events`, 2 `GET clob
  /time`, 1 `GET gamma /events/slug/…`, 1 `GET clob /clob-markets/0x…`). Acquisition partially
  occurred (19 of 450 slots committed), so a non-zero bijection is meaningful.

---

## 7. DB / lifecycle (operator claim 7) — VERIFIED, but product lifecycle PASS NOT MET

```
$ sqlite3 …?mode=ro
PRAGMA integrity_check       -> ok
PRAGMA foreign_key_check     -> []            (empty)
PRAGMA user_version          -> 2
lifecycle line (.stdout)     -> {"worker_active":false,"client_closed":true,
                                 "transport_closed":true,"store_closed":true,"remaining_threads":[]}
evidence                     -> writer_reacquired=true, worker_active=false,
                                remaining_threads=[], client_closed=true, transport_closed=true
```

- integrity `ok`, FK empty, `user_version 2`, resources closed, `writer_reacquired true`,
  `worker_active false`, `remaining_threads []`, store closed. ✔
- **Product lifecycle PASS conditions are NOT met:** the run is `state=FAILED`, `exit_code=2`
  (`intended_phase=FAIL`), i.e. **not** `STOPPED` / `exit_code 0`. Under `phase_verdict` this is
  `BLOCKED`, not `PASS`. ✔
- **No watchdog kill** occurred (`watchdog=1015`; the run lasted ~135 s; no
  `WATCHDOG_EXPIRED`/`WATCHDOG_KILLED` record). ✔

---

## 8. Public safety (operator claim 8) — VERIFIED

Hosts used (66 sends): `clob.polymarket.com` ×22, `api.exchange.coinbase.com` ×19,
`api.kraken.com` ×19, `gamma-api.polymarket.com` ×6 — **all within the allowlist**
(`scripts/cp5_run.py:162-167`, read in full). Paths used are exactly `/products/BTC-USD/ticker`,
`/0/public/Ticker`, `/events`, `/events/slug/…`, `/clob-markets/0x…`, `/time`, `/books`.

- `redirect` = `false` for **all 66** sends. ✔
- GETs carry **no body**: the set of GET sends with `request_bytes > 0` is **empty**; `POST` appears
  **only** for `/books` (×19). ✔
- Request header presence union: `accept, accept-encoding, connection, host, user-agent,
  content-length, content-type` — **no** `authorization`/`cookie`/`api-key`/`signature`/`passphrase`/
  `secret`/`token`. ✔ (The transport rejects such headers at `cp5_run.py:169-171`.)
- **No outbound cookie** (request side). `set_cookie_present=true` on 65 responses is the *response*
  `set-cookie`, which the transport strips (`cp5_run.py:224-225`); it is never re-sent.
- No wallet / signing / order / auth / Chainlink-auth path: token scan of `.stdout` for
  `wallet, private_key, privatekey, signing, signature=, authorization, api_key, apikey, passphrase,
  secret=, order, chainlink, eth_sign, personal_sign` → **all 0**. ✔
- No credential-like request/response headers. ✔

---

## 9. Freeze identity held (operator claim 9) — VERIFIED

```
$ git rev-parse HEAD
671f8775584a8f7da03e8ee8e1bfa10760bcb7b0        (== frozen)
$ git status --porcelain --untracked-files=no
<EMPTY>                                         (tracked/index clean)
```

Frozen source SHA256 (re-hashed now, working tree):

| Path | Now | Frozen | Match |
|---|---|---|---|
| `scripts/cp5_run.py` | `9FE1106631B5D57FC405DC76E0F337105029239E42F75FDCDB8E9D1F6F8073B8` | `9FE11066…8073B8` | ✔ |
| `scripts/cp5_supervise_durable.py` | `2BCA463314A3D737AA0CF44DC7FA5C1E248B49FE4D70AF663244F5AA371F954D` | `2BCA4633…1F954D` | ✔ |
| `src/polymarket_bot/recorder/store.py` | `35902EE05C64D2E17BF65E6205B5FFB66339D41081677D104197F44B4D6992CA` | `35902EE0…4D6992CA` | ✔ |
| `src/polymarket_bot/recorder/service.py` | `72CEA586D84398F259175E4DDC8574CC7C3FDCFB67CBA5462EA6A0057C81D024` | `72CEA586…7C81D024` | ✔ |
| `tests/test_cp59_opening_wait_evidence.py` | `D4597A6B3D769B7F9CC6C40E20B044668B22FC0B142525EE58ED09A374484373` | `D4597A6B…484373` | ✔ |

**The executed code equals the reviewed code.** ✔

---

## Freeze & custody (all six operator custody hashes re-verified STABLE now)

| Artifact | SHA256 now | Operator custody | Match |
|---|---|---|---|
| `data\cp5_10_rollover_20261017.sqlite3` | `D7DE9F016A1729B0ABD308D0518691E9E7C36705C6F4EDE8418AA760734A2177` | `d7de9f01…734a2177` | ✔ |
| `logs\…jsonl` | `6463D8C9CE664F45DE39ACDA90DF1007340762C5B06F0CB4CECD2AD261404C7B` | `6463d8c9…61404c7b` | ✔ |
| `logs\…jsonl.stdout` | `DCB1CA851C4C6E493600DEF928FC6A27054367C87AE2495ECDBE8405EB4415F6` | `dcb1ca85…eb4415f6` | ✔ |
| `logs\…jsonl.stderr` | `E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855` (=empty) | `e3b0c442…7852b855` | ✔ |
| `data\…sqlite3.supervision.lock` | `310CD78DB81C345CA8331A7668F8E1A5C7A81E0C3803DF7E3926927C87CEC56F` | `310cd78d…7c87cec56f` | ✔ |
| `data\…sqlite3.writer.lock` | `6E340B9CFFB37A989CA544E6BB780A2C78901D3FB33738768511A30617AFA01D` (1 byte `0x00`) | `6e340b9c…17afa01d` | ✔ |

Path state: DB, `.writer.lock`, `.supervision.lock`, journal, `.stdout`, `.stderr` exist;
`-wal`, `-shm`, `-journal`, `.partial`, `.interrupt` are **absent**. No `cp5_10*` file besides these
six.

---

## Root cause — agreement

**I agree** with the operator's root cause, with the two immaterial numeric corrections in §4f. The
fixed 10 s preparation budget (`service.py:873`) is the binding constraint interacting with
5.9–10.1 MB Gamma `/events` pages (3.8–6.4× the CP5.6C baseline of ~1.57 MB/page); the 5th page was
aborted by the deadline and the successor was never obtained.

---

## Findings

- **High: 0**
- **Medium: 0**

No High or Medium finding was identified against the executed run's evidence. All nine operator
claims were independently reproduced; the evidence is complete, hash-matched, and internally
consistent; the audit bijection is exact; the CP5.9 gate passed. Non-blocking observations follow and
are **not** softened into findings that would misrepresent a correct run.

---

## Verification scope

**Ran (read-only):**
- Native Git: `rev-parse HEAD`, `status --porcelain --untracked-files=no`, `status --porcelain`.
- `Get-FileHash -Algorithm SHA256` on 6 artifacts + 5 frozen source files.
- Journal parse (JSON per line; event counts, UUID sweep, terminal record inspection).
- `.stdout`/`.stderr` parse; reconstruction of `.stdout` from journal `data` and `base64` frames;
  SHA256 comparison; send extraction; bijection against durable `ACTUAL_SEND_ATTEMPT`.
- Read-only SQLite (`file:…?mode=ro`): `PRAGMA integrity_check`, `PRAGMA foreign_key_check`,
  `PRAGMA user_version`, `PRAGMA journal_mode`; `recording_runs`, `recorder_events` (kind × detail-type
  tallies + full-table token scan), `markets`, `sample_slots`, `request_attempts`,
  `reference_associations`, `storage_checkpoints`, `clock_anchors`.
- Read-only SQLite on `cp5_6c_replacement_…` and `cp5_3_rollover_20261016_readiness` (comparison).
- Source inspection: `scripts/cp5_supervise_durable.py` (classification + CLI + run loop),
  `scripts/cp5_run.py` (CP5Recorder, AuditTransport, `phase_verdict`, `child`, aligned-child,
  `__main__`), `src/polymarket_bot/recorder/store.py` (`record_event` allowlist, `_json`),
  `src/polymarket_bot/recorder/service.py` (`_prepare_next`, `_preparation_wake_ns`, `_phase_trace`),
  `src/polymarket_bot/market_data/discovery.py` (`discover_next`), `gamma.py` (`list_events`),
  `scripts/cp5_analyze.py` (`intended_phase`).

**Did NOT run / did not do:**
- No `cp5_supervise_durable.py` `run`/`launch`/`inspect`.
- No `cp5_run.py` acquiring mode (`--child`, `--aligned-child`, no `--mode`).
- No network requests of any kind; no second live launch.
- No DB/journal/lock/stream/`data/`/`logs/` write, create, or delete; no SQLite opened in write mode.
- No edits, fixes, refactors, or commits.
- Did not re-run the unit tests or the full suite (out of scope for post-run artifact review).
- Did not inspect the CP5.6C/CP5.3 DBs beyond the specific comparison queries in §4e.

---

## Non-blocking observations

1. **The supervised terminal record cannot carry the product verdict, and the frozen contract did
   not pre-disclose the 64 KiB classification limit.** The child's final evidence line is 66,061 B
   `> 65536`, so the supervisor recorded `UNAVAILABLE_OVERSIZE`, `semantic_reason=null`,
   `result_line=null`, `process_only=true`, and left `state=COMPLETED` (it could not perform the
   `opening_blocked → BLOCKED` relabel at line 246). Reading the journal alone would show
   "COMPLETED", which is **not** the product verdict. Recovery is out-of-band but faithful
   (§1: byte-for-byte, hash-matched). Compounding this, the frozen argv invokes `cp5_run.py
   --aligned-child` directly, so the `phase_verdict` PASS/BLOCKED function was **never executed** —
   the `BLOCKED/PREPARATION_UNAVAILABLE` mapping is a source-level contract, not a run artifact. Not
   a defect in the reviewed delta (the supervisor hash is unchanged from CP5.8), and the contract's
   own note that "process COMPLETED ≠ product PASS" partially guards against a mis-read. Worth
   documenting the 64 KiB limit in the contract for future runs.
2. **Two immaterial numeric imprecisions in operator claim 4.** The 5th `/events` page had **~61 ms**
   of budget remaining (the operator stated ~78 ms; 78–79 ms is more accurately the failing request's
   own duration). "4 pages ~9.7 s" matches the raw sends (9.71 s); the DB `request_attempts` span is
   9.86 s. Neither changes the conclusion.
3. **"Phases 10 and 12" is not verifiable from this worktree.** No phase numbering of that form exists
   in `docs/**` (grep empty). The substance (opening gates unexercised) is verified; the label is
   opaque here.
4. **Launch occurred ~14 s earlier than the contract's "planned durable launch B−120".** `PREPARED`
   at `1792245465.969Z` = B−134.03; contract §C plans B−120 = `1792245480`. No gate impact — the
   aligned child self-synchronizes to B−60 and admission is bounded by B−22 — but the timing deviates
   from the frozen plan.
5. **Response header presence contains opaque `*-pad` names** (e.g. `1eavvah0i1-pad`,
   `ab7aue7gudsahu-pad`, `btz8rbvg-pad`, …). These are response-side only, non-credential, and never
   sent outward; recorded via `response_header_presence`. Harmless, but unexplained by the allowlist.
6. **`reference_reports` and `reference_fetches` are 0** while 19 `APPROXIMATE_OPPORTUNITY_COMPLETION`
   events exist. Consistent with `APPROXIMATE_REFERENCE` mode (approximate samples, no committed
   reference report); flagged only for completeness.
7. **`storage_checkpoints` holds a single early checkpoint** (`db_file_bytes=196608`,
   `committed_slot_count=0`) taken at run start. Not examined further; noted as an observed
   characteristic, not a finding.

---

## Handoff

- **The ONE live authorization was consumed exactly once.** One `PREPARED`, one `STARTED`, one
  execution UUID `c53b5da9-e3a6-4db7-9780-60a7243bf3c6` (matching `.supervision.lock`), one terminal
  `COMPLETED`, one child PID, one `writer.lock`, and no duplicate execution identity anywhere. No
  artifact indicates a **second launch**; no `cp5_10*` file exists beyond the six expected ones; the
  tracked tree is clean (`git status --porcelain --untracked-files=no` empty).
- **No network activity outside the allowlist.** All 66 outbound sends target only
  `api.exchange.coinbase.com`, `api.kraken.com`, `gamma-api.polymarket.com`, `clob.polymarket.com`,
  on allowlisted paths, with no credential-like headers, no outbound cookie, no GET body, POST only
  `/books`, `redirect=false` on all, and no wallet/signing/order/auth path.
- **CP5.9 live regression gate: PASSED.**
- **Rollover opening PASS gates: NOT REACHED** (no successor market → no arm/activation/opening).
  Product outcome `FAILED` / `opening_blocked` → contract-maps to `BLOCKED` /
  `PREPARATION_UNAVAILABLE`. CP5.10 validated the CP5.9 fix, **not** the rollover hand-off.
