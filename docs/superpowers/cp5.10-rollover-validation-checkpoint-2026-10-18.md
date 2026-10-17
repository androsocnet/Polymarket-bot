# CP5.10 — ROLLOVER VALIDATION CHECKPOINT (FROZEN CONTRACT, pre-launch)

Date (host-local filename convention): 2026-10-18. Host UTC at freeze: 2026-10-17T13:40Z.
Branch: `feat/cp5-data-quality`. HEAD: `671f8775584a8f7da03e8ee8e1bfa10760bcb7b0`.

This document is the **frozen contract** for ONE live rollover validation authorized by the CP5.10
instruction. Sections A–F are frozen before launch. Sections G–T are appended only from post-run
evidence. **No pilot. One live validation only.**

---

## A. Repository state (frozen)

- Branch `feat/cp5-data-quality`; HEAD `671f8775584a8f7da03e8ee8e1bfa10760bcb7b0` (`docs: record CP5.9 opening wait remediation checkpoint`).
- Recent commits: `671f877` → `50ecfe6` (`test: cover recorder JSON producer compatibility`) → `c4d68e8` (`fix: canonicalize aligned opening wait evidence`) → `a085312`.
- `git status --porcelain --untracked-files=no` → empty (tracked/index clean). `git diff --check` and `git diff --cached --check` → exit 0, empty.
- Untracked remainder only (docs/ledger/artifacts), pre-existing.
- Prior CP5.8 live authority: **CONSUMED** (single-use, `b4de3df7-f919-4382-9f76-d8af1a41fd8f`) and its CP5 FAIL result is unchanged. Historical CP5.8 DB/journal/streams remain on disk as retained evidence and are NOT reused.

## B. Stable readiness (fresh, independent) — frozen

Report: `docs/superpowers/cp5.10-stable-readiness-review-2026-10-18.md`.
Verdict: **Spec PASS; quality PASS; High 0 / Medium 0.**

Fresh test evidence: `tests.test_cp59_opening_wait_evidence` 10 OK; `tests.test_cp57_arm_serialization_audit`
11 OK; full discovery `816 tests` OK (one load-sensitive `test_cp5_safety` flake on a first full-suite attempt,
deterministic in isolation and on clean re-run — disclosed non-blocking).

Live-code delta `1759f88 → 671f877` touches `scripts/cp5_run.py` (CP5.9, +25/−9), `src/polymarket_bot/recorder/service.py`,
`src/polymarket_bot/recorder/store.py` (CP5.7 `b4f4610`), `tests/test_cp59_opening_wait_evidence.py`, `tests/test_cp57_arm_serialization_audit.py`, and CP5.7/CP5.9 docs.
**The CP5.8 gate-equivalence argument is SUPERSEDED**: `scripts/cp5_run.py` is no longer unchanged, so the
live boundary/opening producer is inside the delta and must be re-argued by the exact-config review.

Confirmed in current source: `opening_wait_evidence()` emits only ints (`intended_boundary` int passthrough,
`wait_started_wall_us`/`wait_ended_wall_us` = `round(seconds*1e6)` int, `wait_end_ns` int); the old float-second
keys are renamed, not reinterpreted; `_flush_timing` routes through canonical `store._json`; `store._json`/`canonical`
remains fail-closed against float/NaN/Inf/unknown/non-str-key; `aligned_wait_delay` (B−60, guard `0<=delay<=900`) and
`require_admission` (`now >= B−22` rejected) reproduce the pre-fix arithmetic; no quality-threshold, fallback,
metadata-authority, network-route, audit, clock, or durable-supervisor change.

## C. Selected boundary and derived times (frozen)

- Host UTC at selection: `2026-10-17T13:39:27.950Z` (Unix `1792244367`).
- Selection rule: first 900-aligned boundary with `B − now >= 1200 s`. `B % 900 == 0` (verified: `900 * 1991384 = 1792245600`).
- **B = `1792245600` = `2026-10-17T14:00:00Z`**. Intended window `B..B+900 = 14:00:00Z..14:15:00Z`.
- Not a historical boundary (CP5.8 used `1792228500`; CP5.6C/CP5.6B differ).

| Point | Epoch s | UTC 2026-10-17 |
|---|---:|---|
| Exact-config review deadline B−180 | 1792245420 | 13:57:00Z |
| Planned durable launch B−120 | 1792245480 | 13:58:00Z |
| Acquisition start B−60 | 1792245540 | 13:59:00Z |
| Preparation B−30 | 1792245570 | 13:59:30Z |
| Late-admission bound B−22 | 1792245578 | 13:59:38Z |
| Revalidation B−12 | 1792245588 | 13:59:48Z |
| Strict arm cutoff B−1ms | 1792245599.999 | 13:59:59.999Z |
| B / slot 0 | 1792245600 | 14:00:00Z |
| Slots 1–4 (B+2i) | 1792245602 / 604 / 606 / 608 | 14:00:02 / :04 / :06 / :08Z |
| Acquisition cap B+10 | 1792245610 | 14:00:10Z |

Timing disclosure: the selected boundary leaves ≈17.2 min from freeze (13:39:52Z) to the B−180 deadline.
If the exact-config review does not complete before B−180 with nothing launched (no PREPARED, no launch, no
network, no new artifacts), the authorization is UNUSED and a later ≥20-minute boundary is selected with
**only** exact freeze + exact-config review repeated (stable readiness is not repeated).

## D. New path label and critical-path absence (frozen)

New CP5.10-specific stem: **`cp5_10_rollover_20261017`**. Not a reuse of any CP5.8 path. Never hand-create,
hand-delete, or overwrite artifacts to satisfy this check.

| # | Path | Absent at freeze |
|---:|---|---|
| 1 | `data\cp5_10_rollover_20261017.sqlite3` | True |
| 2 | `data\cp5_10_rollover_20261017.sqlite3-wal` | True |
| 3 | `data\cp5_10_rollover_20261017.sqlite3-shm` | True |
| 4 | `data\cp5_10_rollover_20261017.sqlite3-journal` | True |
| 5 | `data\cp5_10_rollover_20261017.sqlite3.writer.lock` | True |
| 6 | `data\cp5_10_rollover_20261017.sqlite3.partial` | True |
| 7 | `data\cp5_10_rollover_20261017.sqlite3.supervision.lock` | True |
| 8 | `logs\cp5_10_rollover_20261017.jsonl` | True |
| 9 | `logs\cp5_10_rollover_20261017.jsonl.stdout` | True |
| 10 | `logs\cp5_10_rollover_20261017.jsonl.stderr` | True |
| 11 | `logs\cp5_10_rollover_20261017.jsonl.interrupt` | True |

`Test-Path -LiteralPath` on all eleven; parents `data` and `logs` exist as directories; no `cp5_10*` entry
under either parent. `.interrupt` is not in the supervisor's own collision set (pre-existing disclosed limit)
and is therefore checked explicitly. Absence is not reservation or global-process proof; all eleven are
rechecked immediately before any launch.

## E. Frozen source hashes (fresh SHA256 at freeze)

| Path | SHA256 |
|---|---|
| `scripts/cp5_run.py` | `9FE1106631B5D57FC405DC76E0F337105029239E42F75FDCDB8E9D1F6F8073B8` |
| `scripts/cp5_supervise_durable.py` | `2BCA463314A3D737AA0CF44DC7FA5C1E248B49FE4D70AF663244F5AA371F954D` |
| `src/polymarket_bot/recorder/store.py` | `35902EE05C64D2E17BF65E6205B5FFB66339D41081677D104197F44B4D6992CA` |
| `src/polymarket_bot/recorder/service.py` | `72CEA586D84398F259175E4DDC8574CC7C3FDCFB67CBA5462EA6A0057C81D024` |
| `tests/test_cp59_opening_wait_evidence.py` | `D4597A6B3D769B7F9CC6C40E20B044668B22FC0B142525EE58ED09A374484373` |
| `scripts/cp5_analyze.py` | `90BEBB30854F373ABF601C8F98AD86DEDDFE9052895A241FA7BD67E9183CCCD2` |

`scripts/cp5_supervise_durable.py` equals the CP5.8 frozen hash exactly (unchanged). `scripts/cp5_run.py` is a
**new** hash (`9FE11066…`), superseding the CP5.8 value `DB2ECE16…`; no historical hash is reused.

## F. Frozen exact invocation (verified against current source)

CWD: `C:\Users\Andro Saptajulianto\Downloads\Polymarket-bot\.worktrees\cp5-data-quality`
`PYTHONPATH=src;scripts`; interpreter `.venv\Scripts\python.exe` (exists).

```json
[".venv\\Scripts\\python.exe","scripts\\cp5_supervise_durable.py","launch","--journal","C:\\Users\\Andro Saptajulianto\\Downloads\\Polymarket-bot\\.worktrees\\cp5-data-quality\\logs\\cp5_10_rollover_20261017.jsonl","--db","C:\\Users\\Andro Saptajulianto\\Downloads\\Polymarket-bot\\.worktrees\\cp5-data-quality\\data\\cp5_10_rollover_20261017.sqlite3","--boundary","1792245600","--watchdog","1015","--",".venv\\Scripts\\python.exe","scripts\\cp5_run.py","--aligned-child","--db","C:\\Users\\Andro Saptajulianto\\Downloads\\Polymarket-bot\\.worktrees\\cp5-data-quality\\data\\cp5_10_rollover_20261017.sqlite3","--seconds","70","--mode","rollover","--intended-boundary","1792245600"]
```

Source-confirmed:
- `cp5_supervise_durable.py:286–292` — `run`/`launch` accept exactly `--journal`, `--db`, `--boundary` (`type=int`),
  `--watchdog` (`type=float`) and `command` via `argparse.REMAINDER`; `:293–294` `inspect` takes one positional journal.
  **No `--execution-uuid` flag exists.**
- `cp5_supervise_durable.py:77` — leading `--` stripped; `:78–79` require a non-empty command and positive finite watchdog.
- `cp5_supervise_durable.py:82` — refuses if journal, `.stdout`, `.stderr`, DB, `-wal`, `-shm`, `-journal`,
  `.writer.lock`, or `.partial` already exist; `:84–89` generates one `uuid.uuid4()` and exclusively creates/fsyncs
  the retained `.supervision.lock` reservation; `:90–91` re-checks absence after reservation.
- `cp5_run.py:421–427` — flags `--db` (Path), `--seconds` (int), `--mode` (`rollover`/`pilot`), `--intended-boundary`
  (int), `--child`, `--aligned-child`; `:431–437` aligned-child: `wait_started = time.time()`,
  `delay = aligned_wait_delay(intended_boundary, wait_started)`, `time.sleep(delay)`, then `opening_wait_evidence(...)`.
- `cp5_run.py:325–337` — `child()` requires a new DB, mode `rollover`/`pilot`, `seconds == 70` (rollover), integer
  `intended_boundary % 900 == 0`, rejects `time.time() >= intended_boundary-22`, caps the rollover deadline at `B+10`.

Both `--db` values are identical; both boundary values are identical (`1792245600`); `--watchdog 1015`.
The execution UUID is generated internally by the supervisor and is UNAVAILABLE until launch.

## F2. Runtime disposition at freeze

Execution **NOT LAUNCHED**. The single-use live authorization is UNCONSUMED at freeze time. Metadata, ARM/activation,
opening slots 0–4, raw/durable audit bijection, exact replay, DB/lifecycle and the live outcome are NOT RUN.

---
---

# POST-RUN EVIDENCE (G–T)

Appended only from durable journal/DB/stdout/stderr evidence after the single live launch. Nothing in this section
was known or written before launch.

## G. Exact-config review (fresh, independent) — PASS

Report: `docs/superpowers/cp5.10-exact-config-review-2026-10-18.md`.
Verdict: **Spec PASS; quality PASS; High 0 / Medium 0** for the exact frozen configuration.

Review start host UTC `2026-10-17T13:40:37Z`; report written `13:42:34Z`; final check `13:43:21Z`.
Deadline `B−180 = 1792245420 = 13:57:00Z` — **MET** with ≈866 s of margin. All five fresh hashes matched the contract
byte-for-byte; the eleven critical paths were independently confirmed absent; `cp5_run.py` was confirmed changed vs
`1759f88` and the CP5.8 gate-equivalence argument was declared superseded and re-argued for the current delta.

## H. Single durable launch, execution identity, supervision

- Pre-launch recheck at `13:57:36.712Z`: all 11 paths absent; `cp5_run.py`/`cp5_supervise_durable.py` hashes unchanged;
  HEAD unchanged; tracked/index clean.
- Intent recorded at host UTC `2026-10-17T13:57:45.458Z`; **`launch` invoked exactly once**, exit code 0 at `13:57:45.644Z`
  (≈B−134 s). **THE SINGLE-USE LIVE AUTHORIZATION IS CONSUMED. No second launch occurred under any condition.**
- Execution UUID `c53b5da9-e3a6-4db7-9780-60a7243bf3c6` (generated internally; also in `data\cp5_10_rollover_20261017.sqlite3.supervision.lock`).
- Journal: exactly one `PREPARED` (`13:57:45.969Z`), exactly one `STARTED` (child PID `28440`), 85 `STDOUT` data frames,
  exactly one terminal `COMPLETED` (`returncode 0`, `child_stopped true`, `error null`). Valid ordering; no duplicate
  execution identity; no `WATCHDOG`/`KILL`/`INTERRUPT` semantics; stdout and stderr retained.
- **Supervisor gate: PASS.** Child application `run_id e7f59343-11f4-42d9-8e7f-a98d7dab9ac1`.
- Disclosed: the supervisor's own terminal classification is `UNAVAILABLE_OVERSIZE` / `result_line: null` /
  `process_only: true` / `classification_record_limit: 65536` (`cp5_supervise_durable.py:167,:274`) because the child's
  final evidence line is 66 061 bytes and exceeds the 64 KiB record limit. The product evidence is fully recoverable
  from the hash-matched retained `.stdout` (verified independently, byte-for-byte, from both journal `data` and `base64`
  frames). Process `COMPLETED` alone is **not** product PASS; the product result is read from the retained stdout.

## I. CP5.9 live regression gate — **PASS** (the objective of CP5.10)

`RecorderStore.record_event` (`store.py:933-943`) enforces a strict kind allowlist, so `OPENING_WAIT` is carried in
`detail["type"]` on a `kind='DISCOVERY_GAP'` row (not as its own `kind`). Exactly **one** such row persisted:

```
kind=DISCOVERY_GAP   detail.type=OPENING_WAIT   started_us=1792245540021472
{"diagnostic_only":true,"intended_boundary":1792245600,"run_id":"e7f59343-11f4-42d9-8e7f-a98d7dab9ac1",
 "type":"OPENING_WAIT","wait_end_ns":179894390000000,"wait_ended_wall_us":1792245540021481,
 "wait_started_wall_us":1792245466332680}
```

- `intended_boundary` = int `1792245600` ✔
- `wait_started_wall_us` = int `1792245466332680` (13:57:46.332680Z, child wait start) ✔
- `wait_ended_wall_us` = int `1792245540021481` (**13:59:00.021Z = B−60**) ✔ consistent with the frozen B−60 contract
- `wait_end_ns` = int `179894390000000` (monotonic ns) ✔
- Old float keys `wait_started` / `wait_ended`: **ABSENT** ✔
- No `fatal_invariant`, no `RecorderStoreError`, no `audit_persistence_failed`, no unsupported-JSON value anywhere ✔

**The CP5.8 blocker (finite-float OPENING_WAIT against a fail-closed strict sink) is repaired and PROVEN LIVE.**

## J. Metadata enumeration / preparation — **FAILED (successor unavailable)**

- `markets` holds **only the predecessor**: `btc-updown-15m-1792244700`, entity `5775413`, `condition_id`
  `0x6e14cf49efd25872ec1fc437ecaf7b2fea51f2aa6d09f4e7d4c6aba7639084f7`, window `13:45:00Z–14:00:00Z`,
  `lifecycle CURRENT`. The intended successor for `B..B+900` was never obtained.
- Attempts: `GET /time` (200), `GET /events/slug/btc-updown-15m-1792244700` (200, 7003 B),
  `GET /clob-markets/0x6e14…` (200), then Gamma enumeration `GET /events`
  (`active=true, closed=false, ascending=true, order=endDate, limit=100`, 30-min endDate window) at offsets 0/100/200/300
  returning **5 968 744 / 7 865 841 / 9 602 021 / 10 079 943 bytes** (200, complete), then offset=400:
  `status null`, `result` timeout, `error_code transport_error`, `response_bytes 0`, `raw_complete 0`.
- Raw audit attempt 38: `path /events`, `error "ReadTimeout"`, `complete false`, `status null`,
  `started_wall_us 1792245579507510` (13:59:39.507Z), `ended_wall_us 1792245579586976`, `ended_mono_ns` equal to the
  phase `deadline_ns` → the timeout is **deadline-driven**, not a transport fault.
- `PREPARATION_PHASE` rows: one `reason=START` (stage NEW, `started_ns 179923937000000` → wake at B−30),
  one `reason=transport_error`, `failure_reason=transport_error`, `deadline_ns 179933937000000`. Both span **exactly
  10.000 s** (`service.py:873`, `phase_end = phase_start + 10_000_000_000`).
- Root cause (independent reviewer agreed): `_prepare_next` grants `acquire_next_market` a **fixed 10 s budget**, while
  `discover_next` (`discovery.py:84`) must page up to **5** Gamma `/events` calls (`gamma.py:20`, `limit=100`). Four
  pages consumed 9.70 s (raw) / 9.86 s (DB) of the 10 s; the fifth page (offset 400) began with only **~61 ms** of budget
  remaining and was clamped by httpx to the remaining deadline → `ReadTimeout` → `MarketDataError('transport_error')`
  → preparation aborted, `_prepared` stayed `None` → at `B−1 ms` the recorder raised `opening_blocked`.
- Trigger credibility: on 2026-10-16 (CP5.6C) each `/events` page was ~1.57 MB (~0.6 s); today each page was
  5.9–10.1 MB (~2.2–2.4 s) — a ~4–6× upstream payload growth against a fixed budget. The read-time is budget-bound, so
  this is a **preparation-availability** outcome, not a transient network hiccup, and **not** a CP5.9 regression
  (the CP5.9 diff touches only the OPENING_WAIT producer, `_flush_timing`, and the two pure scheduler helpers).
- Precedent: prior rollover DBs also never recorded a successor (`data\cp5_3_rollover_20261016_readiness.sqlite3` and
  `cp5_6c_replacement_20261017_0351adeb-…`: `markets=1`, predecessor only). The 120 s/1200 s runs that did show two
  markets obtained the successor via the in-run boundary path, not via pre-boundary `discover_next`.
- Intended endpoint: `intended_end_us 1792245610000000` = B+10. No `PREPARED_ARM`, no `PREPARED_ACTIVATION`.

## K. ARM / revalidation / activation — **NOT REACHED**

`PREPARED_ARM` count = 0; `PREPARED_ACTIVATION` count = 0. No canonical ARM serialization, no revalidation, no
single-use activation was exercised. Not a failure of those paths — they were never entered.

## L. Opening slots 0–4 — **NOT REACHED**

No successor ledger exists. The only `sample_slots` are the predecessor's own 450-slot ledger for run
`e7f59343-…`: 19 `COMPLETE` (13:59:00–13:59:58, the observed pre-boundary window), 431 `MISSED` (before_start).
Slots 0–4 of a `B`-anchored successor were never created, so `B/B+2/B+4/B+6/B+8` nominal targets, pre-boundary-send,
catch-up, backfill, predecessor-inheritance and stable-grid conditions were not evaluable.

## M. Audit bijection — **PASS**

Raw actual sends in the child evidence = **66**; durable `ACTUAL_SEND_ATTEMPT` events = **66**. All contract fields
match exactly (attempt id, method, host, path, `started_wall_us`, `started_mono_ns`, `ended_wall_us`, `ended_mono_ns`,
status, `run_id`): **missing 0, duplicate 0, foreign 0**. The only structural delta is that the durable rows add a
`type` field; after dropping it, the 66-entry payload multisets are identical. `audit_persistence_failed` is absent.
**Non-vacuous** — 19 slot commits and 66 real sends occurred before the failure. By host/path: 19 Coinbase ticker,
19 Kraken ticker, 19 CLOB `/books`, 2 CLOB `/time`, 1 CLOB `/clob-markets`, 5 Gamma `/events`,
1 Gamma `/events/slug/…`.

## N. Revision-2 replay — **NOT REACHED**

The revision-2 exact replay and research-assessment semantics require a completed intended phase; there was none.
NOT RUN (not 0/PASS).

## O. DB / lifecycle

`PRAGMA integrity_check` = **ok**; `PRAGMA foreign_key_check` = empty; `user_version` = **2**; `market_revisions` = 1;
`storage_checkpoints` = 1; `resolution_observations` = 0; zero Chainlink contamination.
Child-reported closure: `client_closed true`, `transport_closed true`, `worker_active false`, `remaining_threads []`,
`writer_reacquired true`. **No watchdog kill.**
Product lifecycle PASS conditions are **NOT met**: `recording_runs.state = FAILED`, `exit_reason = opening_blocked`,
child `exit_code = 2` (not `STOPPED`/`0`). `intended_phase verdict = FAIL, reason = INTENDED_MARKET_UNAVAILABLE`.
Per the frozen pass/fail contract (`cp5_run.py:288-302` `phase_verdict`), reason `opening_blocked` maps to
**BLOCKED / PREPARATION_UNAVAILABLE**.

## P. Public safety — clean

Only the four allowlisted hosts appear: `api.exchange.coinbase.com` `/products/BTC-USD/ticker`,
`api.kraken.com` `/0/public/Ticker`, `gamma-api.polymarket.com` `/events` + `/events/slug/…`,
`clob.polymarket.com` `/time` + `/books` + `/clob-markets/0x…`. No non-allowlisted host/path. No credential-like
request or response header names (authorization/cookie/api-key/signature/passphrase/secret/token) — 0. No outbound
cookie header. `redirect=false` on all 66 sends. GET bodies empty; POST body only `/books` (186 B). No cookie value is
stored (only boolean `set_cookie_present`). No wallet/signing/order/auth/Chainlink-authenticated path. Disclosed
transports retained: `trust_env=False`, redirect rejection, selector validation, request deadlines, 10 MiB response cap.

## Q. Independent post-run review (fresh)

Report: `docs/superpowers/cp5.10-independent-post-run-review-2026-10-18.md`.
Verdict: **Spec PASS; quality PASS; High 0 / Medium 0**. Independently confirmed: CP5.9 live regression gate **PASSED**;
rollover PASS gates **NOT reachable**; audit bijection 66↔66 non-vacuous; hashes stable and matching the reviewed
freeze; the one authorization was consumed exactly once with no second launch and no allowlist-external activity.
The reviewer **agreed** with the root cause, with two immaterial corrections (page 5 had ~61 ms of budget left, not
~78 ms; four pages = 9.70 s raw / 9.86 s DB). Seven non-blocking observations were recorded; the leading one is that
the supervised terminal record is `UNAVAILABLE_OVERSIZE`/`process_only=true` and cannot itself carry the product
verdict (recoverable out-of-band, and not pre-stated in the frozen contract).

## R. Artifact custody hashes (post-run, stable)

| Artifact | SHA256 |
|---|---|
| `data\cp5_10_rollover_20261017.sqlite3` | `d7de9f016a1729b0abd308d0518691e9e7c36705c6f4ede8418aa760734a2177` |
| `logs\cp5_10_rollover_20261017.jsonl` | `6463d8c9ce664f45de39acda90df1007340762c5b06f0cb4cecd2ad261404c7b` |
| `logs\cp5_10_rollover_20261017.jsonl.stdout` | `dcb1ca851c4c6e493600def928fc6a27054367c87ae2495ecdbe8405eb4415f6` |
| `logs\cp5_10_rollover_20261017.jsonl.stderr` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` (empty) |
| `data\cp5_10_rollover_20261017.sqlite3.supervision.lock` | `310cd78db81c345ca8331a7668f8e1a5c7a81e0c3803df7e3926927c87cec56f` |
| `data\cp5_10_rollover_20261017.sqlite3.writer.lock` | `6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d` |

Frozen source hashes (re-verified post-run, unchanged from section E): `cp5_run.py 9FE11066…8073B8`,
`cp5_supervise_durable.py 2BCA4633…1F954D`, `store.py 35902EE0…4D6992CA`, `service.py 72CEA586…7C81D024`,
`tests\test_cp59_opening_wait_evidence.py D4597A6B…484373`. HEAD `671f877…`; tracked/index clean.

## S. Remaining risks / residuals

1. **Preparation budget vs upstream volume (leading risk).** The successor preparation has a fixed 10 s budget
   (`service.py:873`) for up to 5 Gamma `/events` pages; today's ~6–10 MB pages consumed it. Unless/until this is
   addressed in a separately authorized offline change, a new rollover may again end `opening_blocked` when Gamma
   pages are large. Not remediated here (out of scope; no tuning performed).
2. **Supervisor terminal classification.** `UNAVAILABLE_OVERSIZE`/`process_only=true` means the supervised record alone
   cannot carry the product verdict for large evidence envelopes; the verdict must be read from retained stdout.
   Pre-existing disclosed 64 KiB limit; not pre-stated in the frozen contract.
3. Carried-forward non-blocking residuals (unchanged): `round()` vs `wall_us()` ≤1 µs; `store.py` `json.loads(parse_float=str)`
   without a `parse_constant` guard (still fails closed); producer audit is static/manual.
4. The rollover PASS gates remain **unexercised**, so their correctness is still unproven — one more live validation
   would be required after the preparation-availability issue is resolved.

## T. Final verdict

**CP5 BLOCKED — validation cannot safely continue**

Rationale, scoped strictly to evidence: the single authorized live rollover executed exactly once and completed
safely, and it **achieved the objective of CP5.10** — the CP5.9 repaired path is proven live (canonical integer
`OPENING_WAIT` persisted, no float JSON, no `fatal_invariant`, no unsupported value). However the rollover itself did
**not** validate: the intended successor market was never obtained, so metadata identity, ARM, revalidation,
activation and opening slots 0–4 were never exercised (the reason code is `opening_blocked`, which the frozen
pass/fail contract itself maps to `BLOCKED / PREPARATION_UNAVAILABLE`). No product defect and no data-quality failure
was demonstrated; no unsafe write, no partial activation, and no allowlist-external activity occurred. Therefore the
result is **not** `CP5.10 ROLLOVER PASS` (the opening gates were not reached) and **not** `CP5 FAIL` (no data/runtime
insufficiency was measured — there is no opening-phase data to judge); it is a determinate, fully-evidenced
preparation-availability obstruction: **CP5 BLOCKED**.

**STOP. No pilot.** Task10 (1200 s pilot) remains separately unauthorized. No second launch, no retry, no merge, no push.
