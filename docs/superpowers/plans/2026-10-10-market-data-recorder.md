# BTC 15-minute Market-data Recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a recoverable local recorder of current Polymarket BTC 15-minute books and resolution, with explicit incomplete-reference labelling and a dormant exact-Chainlink reference domain.

**Architecture:** Reuse strict Gamma discovery and the snapshot parser after hardening external numeric handling. A focused public-data transport, window scheduler, book collector and resolution reconciler submit short transactions to one SQLite writer. Reference evidence, retrieval provenance and boundary associations are designed and offline-tested now; authenticated Chainlink collection remains disabled behind separate activation gates.

**Tech Stack:** Python 3.12+, uv, existing httpx, standard-library SQLite/Decimal/zlib/dataclasses/unittest, Windows PowerShell, Git and project-scoped RTK. No new dependencies or ORM.

**Spec:** [Approved amended specification](../specs/2026-10-10-market-data-recorder-design.md), amendment commit `246a949`; the specification wins over this plan. Record any execution-time conflict ruling before proceeding.

**Status:** Plan written for review only. No implementation task below has been performed. The prior Native choice applied to milestone 1; confirm the execution method for this recorder plan before executing it. Native is recommended for these coupled interfaces and gated runtime scope.

## Global Constraints

- Collect from now onward; no historical market import or assumed safe Chainlink backfill.
- SQLite `user_version=1`, foreign keys, rollback-journal mode, synchronous FULL, exactly one writer/connection owner and an OS-backed exclusive canonical-path lock.
- Full returned depth as compressed original response evidence, indexed summaries and exact Decimal TEXT; no giant normalized level table, automatic deletion, VACUUM, rotation or cadence adaptation.
- Gamma fast path plus independent filtered-list fallback; strict current 900-second window, immutable condition/token mapping and saved original outcome indices. No identifier-version branching.
- Public `POST /books` is the only permitted POST, internally constructed for two validated tokens. All other Polymarket calls remain unauthenticated fixed GET routes listed in spec section 4.
- Independent token/condition validation and source timestamps; batch REST is never guaranteed atomic. GET fallback only for batch 404/405 or missing requested tokens, within the same deadline.
- Slots `0..449`: `window_start + slot_index * 2 seconds`, last slot `+898`. Monotonic scheduling, explicit delay/missed records, next future slot and no burst catch-up.
- Capture deadline is the earlier of scheduled time +2 seconds and market end; at most one bounded retry, a 10 MiB response ceiling, and no usable late quote.
- CLOB time anchor: midpoint, uncertainty one second plus half RTT, refresh about every 60 seconds, unusable after 120 seconds; discontinuity threshold combined uncertainties plus one second. No system clock changes.
- Numeric limits: 256 input characters, 128 coefficient digits, absolute explicit/adjusted exponent 128; local exact arithmetic budget at most 512 digits. Reject nonfinite/unsupported values, never silently round/clamp/replace.
- Expected Decimal/conversion/arithmetic failures become explicit invalid evidence. An isolated malformed payload does not terminate recording; database integrity and explicit fatal invariants still stop the process.
- `BOOK_ONLY / REFERENCE_INCOMPLETE` is mandatory without verified references; book completeness and reference completeness remain independent.
- BOOK_ONLY must not load Chainlink username/API identifier or HMAC secret, construct signed requests, or contact authenticated report endpoints, including during import/startup.
- Pin `BTC/USD-Streams-TWAP-60s-mainnet-production`, feed `0x0002ee6757e8822c00d273bc340fc24c9cafe123a4ff2ea1dbdb31944bc7d95f`; no spot-feed substitution.
- Six activation gates: exact-feed entitlement, actual price/subscription decision, credentials and explicit use permission, successful authenticated retrieval, verified decoding/scaling, verified boundary selection.
- Chainlink signing must later follow its actual verified authentication timestamp contract; CLOB timing is not implicitly its signing clock. No live signer or secret loader in this milestone.
- Preserve live versus historical provenance, source validity/observation times and actual retrieval/learning time. Later repair never overwrites original availability or missing coverage.
- Resolution is independent: raw status/payout/time and time learned, winner only from unambiguous final payout mapping, never prices.
- No wallet/private keys, trading credentials, Polymarket authentication, orders/cancellation, strategy, signals, sizing, simulated trades, real money, WebSocket or BTC spot feed.
- No accounts, subscription, credential use, live recorder pilot or 24-hour run during this implementation unless separately authorized. Implementation-plan approval alone does not activate these gates.
- No push, merge, deployment, publication, global Git configuration, Superpowers or RTK changes. Feature implementation occurs in an isolated worktree/branch.
- Every behavioral group uses TDD: write test, observe meaningful RED, minimal implementation, observe GREEN, refactor only while green. Use systematic-debugging for real defects.

## Review Focus

Each additional input class has an explicit owning test below:

1. Ambient Decimal context changed by another caller: parsing and arithmetic remain exact and do not alter that caller's context (Task 1).
2. Duplicate/reordered batch tokens or one malicious numeric element: retain the valid other outcome, reject ambiguous identity, preserve one original response (Task 3).
3. A crash after slot reservation or during finalization: recovery preserves committed evidence and never reacquires the reserved slot (Tasks 5 and 10).
4. An existing credential environment or eager factory in BOOK_ONLY: neither credential lookup nor adapter/signing construction occurs (Tasks 8 and 12).
5. Discovery outages spanning markets never observed: coverage percentages disclose unassigned gaps instead of claiming all missing windows were sampled (Task 11).

## Working conventions and file map

Paths are relative to the executing checkout. Read this plan and the complete spec first. The original scaffold is already committed; do not repeat its baseline or mix unrelated files into feature commits.

Use session-only author/safe-directory options, never persistent Git configuration:

```powershell
$taskGitArgs = @('-c', "safe.directory=$((Get-Location).Path)", '-c', 'user.name=Andro Saptajulianto', '-c', 'user.email=Andro.socnet@gmail.com')
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
```

Git commands below use `rtk proxy git @taskGitArgs` where command-only options are required. Use ordinary supported RTK commands otherwise, following RTK.md. The advisory suggesting global RTK initialization is not authorization to run it. Request escalation if protected Git writes fail.

Focused test prefix is `uv run --offline --locked --cache-dir .uv-cache python -m unittest`; full suite:

```powershell
uv run --offline --locked --cache-dir .uv-cache python -m unittest discover -s tests -t . -v
```

Use the existing unittest/socket guard, httpx.MockTransport and injected clocks. A new-module import failure is an acceptable initial RED; fixture/syntax mistakes are not. For test groups sharing a task, complete one group's RED/GREEN before writing the next group's implementation. Do not install a test framework.
Every named
test group below uses the five separately tracked actions in this cycle:

- [ ] Write only the group's specified behavioral tests and fixtures.
- [ ] Run its focused command and record the actual meaningful RED output.
- [ ] Implement only that group's specified behavior/signature.
- [ ] Run the same command and record GREEN before optional refactoring.
- [ ] Run affected regressions; commit the task only when every group is green.

Task paragraphs identify each group's assertions and implementation decisions;
they do not authorize combining tests and implementation before observing RED.
Assertion tables specify required test method names and result expressions;
wrap expressions in unittest assertions after the described local fixture
arrange/act. They are not new application APIs. Use synthetic IDs/times only.

Commit steps name the intended message; stage only that task's listed application/test/document files, run `diff --cached --check` and inspect `diff --cached --name-only` first. Commit with the session-only identity above. Do not use `git add .`.

| File | Responsibility |
| --- | --- |
| `market_data/numeric.py` under `src/polymarket_bot/` | Bounded numeric/JSON conversion, isolated exact arithmetic and safe formatting |
| Existing `market_data/{clob,parsing,http}.py` | Harden inherited parsing; narrowly extend public transport/evidence without changing snapshot defaults |
| `recorder/models.py`, `recorder/__init__.py` | Recorder records/statuses, fixed runtime policies, no credential fields |
| `recorder/books.py` | Batch mapping, independent results and narrowly bounded GET fallback |
| `recorder/schema.sql`, `recorder/store.py` | Fourteen tables, compressed evidence, one writer, domain transactions and recovery |
| `recorder/clock.py`, `recorder/schedule.py` | CLOB calibration and pure 450-slot scheduling decisions |
| `recorder/metadata.py` | Discovery bridge, original outcome indices, raw revisions and lifecycle validation |
| `recorder/reference.py` | Credential-free reference evidence, gates, associations and future due-work selection |
| `recorder/resolution.py` | Public finality parsing, mapping and persisted due-work selection |
| `recorder/service.py` | Recorder coordinator, bounded capture/housekeeping, rollover and shutdown |
| `recorder/metrics.py` | Storage checkpoints, coverage, latency and reconciliation measurements |
| `recorder/cli.py`, `src/record.py` | Dedicated recorder/help/report CLI; preserve `src/main.py` snapshot behavior |
| `tests/recorder_support.py` | Shared deterministic recorder fixtures/fakes, introduced with their first consumer |
| New `tests/test_{numeric,recorder_http,batch_books,recorder_schema,recorder_store,recorder_schedule,recorder_metadata,recorder_reference,recorder_resolution,recorder_service,recorder_metrics,recorder_cli,recorder_boundary}.py` | Offline behavioral coverage |
| Existing parsing/HTTP/CLOB/boundary tests and `README.md` | Regression tests, scoped route audit and actual usage/limitations |

`pyproject.toml`, `uv.lock`, AGENTS.md, RTK.md and scoped hooks need no feature edits. Package-relative schema loading uses `Path(__file__).with_name('schema.sql')`; keep the existing application layout, without a build backend.

## Task 0: Record the plan and establish isolated execution

**Files:** This plan only if not yet committed; execution ledger under `.superpowers/sdd/2026-10-10-market-data-recorder/` only when execution begins.

**Interfaces:** Consumes approved spec `246a949` and the user's reviewed plan/execution choice. Produces a clean feature checkout and recorded baseline HEAD, test count and execution method.

- [ ] Inspect branch, worktrees, status and staged changes; confirm the amended spec and reviewed plan are recorded independently of implementation. Commit this plan alone if it is not already tracked.
- [ ] Invoke `superpowers:using-git-worktrees` before application edits. Prefer a suitable attached worktree; otherwise create an isolated `codex/market-data-recorder` branch from clean main using the skill. Check `.worktrees/` is ignored before any local fallback creation. Do not implement on main.
- [ ] Establish the uv environment from the existing lock/interpreter only if the new worktree needs it; no dependency additions or Python download. Use local cache/offline synchronization first; report an unavailable cached dependency rather than changing versions.
- [ ] Run the full offline suite and `uv run --offline --locked --cache-dir .uv-cache python src/main.py --help`. Record actual outputs; stop for baseline failures and investigate without feature work.
- [ ] Initialize the chosen execution workflow ledger with all task checkboxes and scope gates. Native uses executing-plans; no per-task permission stops, but real conflicts/blockers follow that skill. No live APIs or credential reads in this task.

## Task 1: Harden inherited external numeric parsing first

**Files:** Create `src/polymarket_bot/market_data/numeric.py`, `tests/test_numeric.py`; modify existing `clob.py`, `parsing.py`, `http.py`, `tests/test_clob.py`, `tests/test_parsing.py`, `tests/test_http.py` only for this hardening.

**Interfaces:** Consumes existing `MarketDataError`, `JsonRead`, `parse_book` and `parse_market`. Produces `parse_decimal(value: object, *, field: str) -> Decimal`, `parse_integer(value: object, *, field: str, minimum: int, maximum: int) -> int`, `load_numeric_json(raw: bytes | str) -> object`, `decimal_text(value: Decimal) -> str`, and `exact_decimal_context() -> ContextManager[decimal.Context]` in numeric.py. They raise a structured `MarketDataError('invalid_numeric', ...)`; callers map it to their domain error/warning.

- [ ] Write the regression first in test_clob.py, using existing fixtures:

```python
def test_extreme_exponent_is_domain_error(self):
    for raw in ('1e1000000', '1e-1000000', '1e999999999999999999999'):
        with self.subTest(raw=raw):
            self.error(bids=[{'price': '0.60', 'size': raw}])
```

- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_clob.ClobTests.test_extreme_exponent_is_domain_error -v`; observe overflow during top-size aggregation or the missing rejection of an extreme negative exponent. Constructor-only failures already caught by the old parser do not count as the regression proof; do not manufacture RED by breaking fixtures.
- [ ] Implement bounded lexical conversion and route book parsing/arithmetic through the isolated context. Catch expected DecimalException/conversion errors, preserve invalid_book behavior, never echo an unbounded input into diagnostics.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_numeric tests.test_clob tests.test_parsing tests.test_http -v` and confirm GREEN.
- [ ] Add the next behavioral group in test_numeric.py: reject 257-character inputs, 129 coefficient digits, exponent +/-129, huge exponent text, NaN/sNaN/Infinity, bool/float and oversized integer/time values; accept limits and preserve exact `0.00000000000000000000000000001`. Assert bounded output, no global-context modification, and structured errors instead of Decimal exceptions.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_numeric -v` for RED.
- [ ] Implement the helper limits, bounded JSON integer/float hooks and constant rejection, context-independent arithmetic/formatting; trap rounded/inexact/overflow/underflow rather than publish altered results.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_numeric -v` and confirm GREEN for this group.
- [ ] Add tests with ambient precision=2, restrictive Emax/Emin and traps, then restore that context in test cleanup. Assert midpoint `0.605`, exact top-size aggregation across widely separated accepted exponents, and the unchanged external context. Add malformed optional Gamma prices (warning, no quotes) and numeric JSON/slug/identifier/header bounds (structured errors, no traceback).
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_numeric tests.test_clob tests.test_parsing tests.test_http -v` and record meaningful RED for the new context/metadata/header group.
- [ ] Implement caller error mappings: required metadata failures to invalid_metadata, optional prices warning-only; no Decimal failure can escape snapshot rendering.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_numeric tests.test_clob tests.test_parsing tests.test_http -v` and confirm GREEN.
- [ ] Stage only this task's files, check the staged diff and commit `fix: bound external numeric parsing and exact book arithmetic`. This is a separate inherited-hardening commit, before recorder feature commits.

| Test method | Required assertions after local arrange/act |
| --- | --- |
| `test_numeric_input_budgets` | `error.code == 'invalid_numeric' for +/-129 exponents, huge exponent text, 129 coefficient digits and 257 characters` |
| `test_nonfinite_and_wrong_types` | `NaN/sNaN/Infinity/bool/float all raise structured errors, with no Decimal quote` |
| `test_ambient_context_does_not_change_results` | `book.midpoint == Decimal('0.605'); context_after == context_before` |
| `test_numeric_json_and_metadata_containment` | `Huge JSON tokens fail explicitly; malformed optional prices are None with warnings; no traceback` |

## Task 2: Add focused public recorder reads and attempt evidence

**Files:** Modify `market_data/http.py`; create `tests/test_recorder_http.py`; extend `tests/test_http.py` and the existing boundary audit only where new routes require it.

**Interfaces:** Extend `JsonRead` with optional defaulted `raw_body: bytes = b''` while keeping its first three positional arguments compatible. Add immutable `HttpAttempt` in http.py: operation/method/path/selectors, attempt number, started/ended_mono_ns and started/ended_wall_us, status/result/error, raw bytes/completeness. `PublicGet` gains optional `max_attempts: int = 3`, `phase_timeout: float = 10.0`, `monotonic_ns: Callable[[], int] | None = None`, `on_attempt: Callable[[HttpAttempt], None] | None = None`. Recorder supplies the integer clock for attempt evidence; legacy deadline/JsonRead float interfaces remain compatible. Add only `get_books(token_ids: tuple[str, str], *, deadline: float) -> JsonRead`; extend fixed GET allowlist for CLOB config and Data resolutions. No caller-supplied POST method/body/URL.

- [ ] Write tests asserting exact POST host/path/body `[{'token_id': UP}, {'token_id': DOWN}]`, no auth headers/redirects/environment proxy, duplicate or malformed token rejection before any request. Assert arbitrary POST and order routes are impossible.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_http -v` for RED.
- [ ] Implement a fixed batch method and narrowly shared private bounded-reading logic; preserve the one-shot GET defaults and new JsonRead defaults.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_http -v` and confirm GREEN for this group.
- [ ] Add tests for 10 MiB limits (announced and streamed), unexpected encoding, invalid JSON/numeric tokens, slow trickle/late parsing, retryable statuses, Retry-After, and recorder max_attempts=2. Assert each actual failed/successful attempt produces exactly one bounded HttpAttempt, with raw_complete=0 on truncation and without headers/secrets.
- [ ] Observe RED, then implement attempt delivery and timeout/deadline checks. Callback only buffers records; no SQLite work in transport/worker threads. Clamp every phase/retry to remaining time, including integer header parsing hardening from Task 1; run focused tests and existing HTTP regressions for GREEN.
- [ ] Extend read-only audit to distinguish unchanged snapshot GET-only behavior from the recorder's single named POST; do not weaken it to accept any POST or any host. Full suite GREEN.
- [ ] Stage only this task's files, check the staged diff and commit `feat: add bounded public batch books and request evidence`.

| Test method | Required assertions after local arrange/act |
| --- | --- |
| `test_named_batch_request_only` | `(request.method, request.url.path) == ('POST', '/books'); body contains exactly two token_id objects` |
| `test_attempt_limits_and_retry_after` | `Recorder attempts <=2; Retry-After beyond budget adds no request` |
| `test_raw_body_and_trickle_deadline` | `Success raw bytes equal original; late/capped read cannot succeed; truncated evidence has raw_complete=False` |
| `test_snapshot_defaults_unchanged` | `Existing JsonRead positional fixtures work; snapshot remains GET-only, maximum three attempts` |

## Task 3: Collect independently validated paired books

**Files:** Create `recorder/__init__.py`, `recorder/models.py`, `recorder/books.py`, `tests/test_batch_books.py`, initial `tests/recorder_support.py`.

**Interfaces:** `OutcomeResult(outcome: str, token_id: str, condition_id: str, status: str, book: OutcomeBook | None, attempt_index: int | None, error_code: str | None, quality: dict, book_metadata: dict)` and `PairedBooks(up: OutcomeResult, down: OutcomeResult, attempts: tuple[HttpAttempt, ...])` in models.py. book_metadata holds source hash and bounded optional tick/minimum-size fields, never replacement quotes. `collect_books(public: PublicGet, market: MarketDescriptor, *, deadline: float, receipt_server_us: Callable[[float], int], uncertainty_us: int) -> PairedBooks` in books.py. Default outcomes UP/DOWN; request indices refer to this pair's attempts, not database IDs. Use a task-local attempt buffer wired through on_attempt; restore the previous observer in finally and preserve housekeeping observation. Collection is sequential in this milestone; future Chainlink concurrency uses a separate adapter.

- [ ] Write tests for reordered response mapping and numeric isolation:

```python
def test_one_extreme_numeric_book_preserves_other_outcome(self):
    # Fixture supplies a reordered batch, valid Down and oversized Up size.
    pair = self.collect_batch(up_size='1e999999999999999999', reverse=True)
    self.assertEqual((pair.up.status, pair.down.status), ('INVALID', 'VALID'))
    self.assertIsNone(pair.up.book)
    self.assertEqual(pair.down.book.best_ask, Decimal('0.61'))
    self.assertEqual(len(pair.attempts), 1)
```

- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_batch_books -v` for RED.
- [ ] Implement per-token matching and invoke the hardened parse_book independently. Preserve raw batch body once in its attempt, source timestamps independently.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_batch_books -v` and confirm GREEN for this group.
- [ ] Add groups for huge negative exponent/extreme digits/nonfinite/arithmetic boundaries in batch and individual fallback, wrong condition, duplicated expected token, unexpected extra token, absent token, empty/crossed books, different source timestamps and future source time beyond 5 seconds plus uncertainty. Assert statuses and nullable summaries explicitly.
- [ ] Observe RED for each group; implement fallback only for batch 404/405 or absent token in a parseable successful batch. No fallback for 429, malformed envelope or identity mismatch. No request starts after deadline; late response has LATE status and no usable book summary. Ensure successful empty sides remain VALID and never derive quotes from complements/Gamma.
- [ ] Run batch and inherited CLOB tests for GREEN; full suite GREEN.
- [ ] Stage only this task's files, check the staged diff and commit `feat: collect independent outcome books from public batch reads`.

| Test method | Required assertions after local arrange/act |
| --- | --- |
| `test_reordered_and_duplicate_tokens` | `Reorder maps by token; duplicate Up fails Up; valid Down remains VALID` |
| `test_extreme_numerics_each_request_shape` | `Positive/negative exponent, digit/nonfinite/context fixtures invalidate their book in both batch and single fallback` |
| `test_fallback_is_bounded_and_justified` | `404/405 or absent token permits affected GETs before deadline; 429/mismatch/malformed envelope permits none` |
| `test_nonatomic_and_independent_source_times` | `Both source times retained; pair.up.quality['batch_not_guaranteed_atomic'] is True for batch` |

## Task 4: Establish the exact schema, evidence codec and writer ownership

**Files:** Create `recorder/schema.sql`, `recorder/store.py`, `tests/test_recorder_schema.py`; extend recorder_support.py with temporary DB fixtures.

**Interfaces:** `RecorderStore.open(path: Path) -> RecorderStore`, `close() -> None`, `encode_evidence(raw: bytes) -> tuple[bytes, int, int]`, `decode_evidence(blob: bytes, raw_size: int, codec_version: int) -> bytes`. One store owns one connection/OS lock; no generic SQL client or ORM exposed.

- [ ] Write tests that introspect the fourteen spec tables, PK/FK/CHECK/unique/partial indexes, version=1, foreign_keys=1, journal_mode=delete and synchronous=FULL. Assert unknown existing version/corruption fails without overwrite, and a second store for the same canonical path is refused.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_schema -v` for RED.
- [ ] Implement schema initialization only for a genuinely empty new file, built-in SQLite ownership and Windows OS-backed locking. Verify existing version=1 schema/integrity instead of trusting user_version alone. A failed initialization releases ownership without replacing an existing DB.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_schema -v` and confirm GREEN before the next test group.
- [ ] Add codec tests: compression round-trip exact original bytes, cap 10 MiB decompressed, invalid codec/length/bomb rejection, no uncontrolled exponent formatting.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_schema -v` and record meaningful RED for this group.
- [ ] Implement zlib/size/version checks, then GREEN.
- [ ] Add process-level offline Windows lock tests: live owner rejected and abrupt owner exit releases lock. Use the same installed interpreter, not another writer process in production. Final schema tests GREEN.
- [ ] Stage only this task's files, check the staged diff and commit `feat: establish single-writer recorder schema and raw evidence storage`.

| Test method | Required assertions after local arrange/act |
| --- | --- |
| `test_exact_schema_constraints` | `Fourteen domain tables; user_version=1; foreign_keys=1; journal_mode='delete'; synchronous=2 (FULL)` |
| `test_unknown_schema_preserves_file` | `Version 2 refuses startup; original contents unchanged` |
| `test_writer_lock_survives_transaction_gaps` | `Second writer refuses between commits; new owner succeeds after OS owner exit` |
| `test_codec_is_bounded_and_byte_exact` | `decode_evidence(*encoded) == original; invalid length/codec/expansion raises structured evidence error` |

## Task 5: Implement atomic slot transactions and durable recovery

**Files:** Modify recorder/models.py and store.py; create `tests/test_recorder_store.py`; extend recorder_support.py with complete/partial/failed bundles.

**Interfaces:** `RunConfig(mode: str = 'BOOK_ONLY', use_fast_path: bool = True)` includes fixed numeric/timing policies without credential values; `SlotTarget(index: int, scheduled_us: int)` and `SlotCommit(market_id: str, target: SlotTarget, run_id: str, anchor_id: int, metadata_revision_id: int, books: PairedBooks, reference: ReferenceAssociationDraft, started_mono_ns: int, ended_mono_ns: int)` in models.py. Introduce `ReferenceAssociationDraft(market_id: str, role: str, target_us: int, status: str, origin: str, available_live: bool, learned_wall_us: int, learned_server_us: int | None, slot_id: int | None = None, fetch_id: int | None = None, report_id: int | None = None, selection_policy_revision: str | None = None, next_due_us: int | None = None, attempt_count: int = 0)` here, initially DISABLED/NOT_FETCHED. Its feed is the pinned model constant CHAINLINK_FEED_ID. Store produces `start_run(config: RunConfig, *, wall_us: int, app_revision: str) -> str`, `reserve_slot(market_id: str, target: SlotTarget, *, run_id: str, anchor_id: int, metadata_revision_id: int, started_mono_ns: int, started_server_us: int) -> int`, `finalize_slot(slot_id: int, commit: SlotCommit, *, committed_wall_us: int) -> None`, `mark_missed(market_id: str, target: SlotTarget, *, run_id: str | None, reason: str, wall_us: int) -> None`, `recover_interrupted(*, wall_us: int) -> None`, `finish_run(run_id: str, *, state: str, wall_us: int, reason: str) -> None`.

- [ ] Write tests for atomic terminal slot+two result rows+attempts+reference association, COMPLETE/PARTIAL/FAILED counts, and single stored response per shared batch. Verify exact Decimal text, independent timestamps and original bytes, no level truncation or missing-side fabrication.
- [ ] Observe RED with `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_store -v`.
- [ ] Implement short reserve/finalize transactions and FK/index mapping; reservation contains no fabricated observations and holds no transaction across network work. A reserve call consumes only identity/timing fields, not final books. Until Task 7 adds acquisition/revisions, tests seed known market/revision rows through the store's own connection in a fixture-only transaction; never use a competing writer connection.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_store -v` and confirm GREEN before the next test group.
- [ ] Add tests for wrong formula, wrong market/revision/token mapping, duplicate writes with equal versus conflicting content, injected failure between outcome inserts and storage error. Assert rollback yields no half-pair, terminal slots cannot be replaced/reacquired and write failure is fatal.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_store -v` and record meaningful RED for this group.
- [ ] Implement writer invariants and idempotence,.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_store -v` and confirm GREEN.
- [ ] Write the restart-after-reservation test: original IN_PROGRESS becomes INTERRUPTED with two missing-side rows, original mode/times remain, and terminal results survive. Recovery is one writer-owned transaction.
- [ ] Run schema/store tests and full suite for GREEN.
- [ ] Stage only this task's files, check the staged diff and commit `feat: persist atomic paired slots and recover interrupted work`.

| Test method | Required assertions after local arrange/act |
| --- | --- |
| `test_pair_and_evidence_one_transaction` | `One slot, two outcome rows, one shared batch body and one original reference association commit together` |
| `test_mid_transaction_failure_rolls_back` | `Reservation stays IN_PROGRESS; no half-pair or attempt rows survive failed finalization` |
| `test_recovery_never_reacquires_reserved_slot` | `Reservation becomes INTERRUPTED with two explicit result rows; completed slot unchanged` |
| `test_conflicting_duplicate_is_fatal_invariant` | `Equal duplicate idempotent; changed identity/content refuses overwrite` |

## Task 6: Implement calibrated monotonic scheduling

**Files:** Create `recorder/clock.py`, `recorder/schedule.py`, `tests/test_recorder_schedule.py`; extend store.py for anchor inserts and models.py for scheduling records.

**Interfaces:** `RecorderClock(*, monotonic_ns: Callable[[], int], wall_us: Callable[[], int])`, `calibrate(read: JsonRead) -> ClockAnchor`, `now() -> datetime`, `server_us_at(mono_ns: int) -> int`, `deadline_ns(target_us: int) -> int`, `usable() -> bool`. now() supplies the UTC duck-type interface consumed by discover_current. ClockAnchor contains raw server seconds, midpoint_ns, server_us, uncertainty_us, observed request times and status. `slot_grid(window_start_us: int) -> tuple[SlotTarget, ...]`; `select_future_slot(market: MarketDescriptor, *, now_us: int, terminal_indices: set[int]) -> SlotSelection(missed: tuple[int, ...], target: SlotTarget | None)`; `classify_wake(target: SlotTarget, *, now_us: int, window_end_us: int) -> str` returns CAPTURE or MISSED. Store: `save_anchor(run_id: str, anchor: ClockAnchor) -> int`.

- [ ] Write exact grid assertions:

```python
def test_grid_is_exactly_450_window_aligned_slots(self):
    grid = slot_grid(1_000_000_000)
    self.assertEqual(len(grid), 450)
    self.assertEqual([s.index for s in grid], list(range(450)))
    self.assertEqual(grid[0].scheduled_us, 1_000_000_000)
    self.assertEqual(grid[-1].scheduled_us, 1_898_000_000)
```

- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_schedule -v` for RED.
- [ ] Implement pure slot selection and arming behavior without real sleep.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_schedule -v` and confirm GREEN for this group.
- [ ] Add groups for mid-window start, late armed wake <2 seconds, >=2-second wake, processing past multiple targets, a slightly passed unarmed target, expiry and already terminal slots. Assert missed/delay reasons, next future slot and no catch-up requests.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_schedule -v` and record meaningful RED for this group.
- [ ] Implement timing classifications and bounded deadline mapping. Add clock tests for midpoint, uncertainty `1_000_000 + RTT_us/2`, refresh due at 60 seconds, anchor unusable after 120 seconds, discontinuity outside combined bounds+one second, and distinct restart monotonic epochs. Arithmetic stays integer-based at persisted boundaries.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_schedule -v` and record meaningful RED for this group.
- [ ] Implement calibration and anchor persistence, then focused/full GREEN.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_schedule -v` and confirm GREEN before the next test group.
- [ ] Stage only this task's files, check the staged diff and commit `feat: schedule market-window slots with calibrated monotonic timing`.

| Test method | Required assertions after local arrange/act |
| --- | --- |
| `test_start_midwindow_selects_future_slot` | `At start+3 seconds, missed=(0,1); next index=2 at +4 seconds` |
| `test_armed_late_wake_versus_passed_unarmed_slot` | `Armed +1.9-second wake CAPTURE; +2 wake MISSED; passed unarmed target never captured` |
| `test_anchor_bounds_and_epoch` | `RTT 2 seconds => uncertainty_us=2_000_000; age 121 seconds unusable; old monotonic epoch never reused` |

## Task 7: Preserve discovery, metadata revisions and market identity

**Files:** Create `recorder/metadata.py`, `tests/test_recorder_metadata.py`; extend store.py/models.py; modify GammaClient only for a narrowly scoped observation bridge if needed.

**Interfaces:** `MarketRevision(market: MarketDescriptor, up_index: int, down_index: int, description: str, resolution_source_url: str, gamma_raw: bytes, clob_config_raw: bytes | None, observed_us: int, config: dict)`; `acquire_market(gamma: GammaClient, public: PublicGet, *, clock: RecorderClock, deadline: float, use_fast_path: bool) -> MarketRevision`, `refresh_revision(previous: MarketRevision, gamma: GammaClient, public: PublicGet, *, clock: RecorderClock, deadline: float) -> MarketRevision`. Store: `save_market_revision(run_id: str, revision: MarketRevision) -> int`, `expire_market(market_id: str, *, observed_us: int) -> None`, `record_event(run_id: str, *, market_id: str | None, kind: str, started_us: int, ended_us: int | None, detail: dict) -> int`.

- [ ] Write tests proving unchanged strict discovery and forced fallback work through the recorder clock, outcome indices survive reversed Gamma arrays, raw rules/source/config are preserved, Gamma prices remain metadata and optional identifiers never replace clobTokenIds.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_metadata -v` for RED. Bridge existing discover_current rather than duplicate its algorithm; record the actual selected raw response via buffered HttpAttempt evidence. CLOB config verifies returned identity/token mapping where fields are supplied; do not make up missing documented fields. GREEN.
- [ ] Add groups for metadata refresh every ~60 seconds, unchanged-content freshness, failed config/refresh with age warning, newly closed/not-ready market, contradictory identities/windows and ambiguous enumeration.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_metadata -v` and record meaningful RED for this group.
- [ ] Implement immutable identity and operational capture pause while retaining prior raw revisions.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_metadata -v` and confirm GREEN for this group.
- [ ] Persist CURRENT/EXPIRED/INVALIDATED and resolution due at expiry; do not delay rollover because expired accepting flags remain true. Run inherited discovery tests/full suite.
- [ ] Stage only this task's files, check the staged diff and commit `feat: retain recorder market identities and metadata revisions`.

| Test method | Required assertions after local arrange/act |
| --- | --- |
| `test_forced_fallback_independent_of_slug` | `use_fast_path=False makes no slug read and selects the strictly validated current market` |
| `test_original_outcome_indices_and_rules` | `Reversed outcomes save up_index=1/down_index=0; raw rules/source bytes unchanged` |
| `test_refresh_conflict_does_not_rewrite_identity` | `Changed condition/window rejects capture; original identity remains stored` |
| `test_expired_open_flags_do_not_block_rollover` | `EXPIRED and resolution due at end even with acceptingOrders=true` |

## Task 8: Implement credential-free reference evidence and activation gates

**Files:** Create `recorder/reference.py`, `tests/test_recorder_reference.py`; extend models.py/store.py. No signer, secret loader, authenticated transport or dependency.

**Interfaces:** Consumes ReferenceAssociationDraft from Task 5. `ActivationEvidence(entitlement: bool, price_decision: bool, credential_use_authorized: bool, authenticated_retrieval: bool, decoder_verified: bool, boundary_verified: bool)` is nonsecret evidence. `require_reference_activation(mode: str, evidence: ActivationEvidence | None) -> None`, `disabled_reference(market_id: str, target_us: int, *, learned_wall_us: int, learned_server_us: int | None) -> ReferenceAssociationDraft`, `normalize_reference_fields(raw_signed_report: bytes, fields: dict, *, price_scale: int | None, decoder_revision: str | None, received_wall_us: int, received_server_us: int | None) -> ReferenceReportDraft`, `eligible_as_of(association: ReferenceAssociationDraft, report: ReferenceReportDraft | None, *, decision_us: int, role: str) -> bool`, `reference_due_work(rows: Sequence[ReferenceAssociationDraft], *, now_us: int, limit: int) -> tuple[ReferenceAssociationDraft, ...]`. Store: `save_reference_evidence(fetch: ReferenceFetchDraft, reports: tuple[ReferenceReportDraft, ...], associations: tuple[ReferenceAssociationDraft, ...]) -> None`. ReferenceFetchDraft carries an HttpAttempt (assigned request_id by writer), pinned feed/purpose/origin/target/status/error and raw response. ReferenceReportDraft carries feed/schema/raw bytes/hash, integer observation/validity/expiry timestamps, Decimal-or-null price, decoder/decode/verification state and actual receipt times. Their field names map directly to spec tables 7.9/7.10; writer supplies first_fetch_id and row IDs. Offline fields use feed_id, report_schema_version, valid_from_seconds, observations_seconds, expires_at_seconds and price_integer; these are decoder-output fixtures, not claims about a live REST envelope. Receipt arguments are independent of source fields.

- [ ] Write fail-fast spy tests: BOOK_ONLY imports/composition never consult a credential getter/file/environment, instantiate an authenticated adapter, call a signer or send a Chainlink request. REFERENCE_REQUIRED with any absent gate raises activation_required before a run/credential lookup.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_reference -v` for RED.
- [ ] Implement data-only gate/disabled association functions. No code path loads actual credentials; all present gates in a unit test are synthetic evidence, not runtime permission.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_reference -v` and confirm GREEN before the next test group.
- [ ] Write tests for pinned feed identity/schema, missing scale returns UNVERIFIED/null price, extreme decoded numerics INVALID, duplicate signed bytes retain first receipt/fetch, and raw report persistence through all four reference tables. Test fixtures provide actual receipt timestamps separately from decoded source fields; normalization must not substitute one for the other.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_reference -v` and record meaningful RED for this group.
- [ ] Implement pure normalization of explicitly supplied offline decoded fields and persistence. Do not claim it is an authenticated ABI decoder or signature verifier. Production binary decoding/scaling is a future activation deliverable; arbitrary synthetic fixtures cannot verify the exact live feed's decoder.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_reference -v` and confirm GREEN before the next test group.
- [ ] Add as-of tests: LIVE_LATEST usable only after receipt and age policy; historical current repair never increases original live coverage; opening lookup is usable prospectively only after learning/verification; closing/resolution is not an earlier input. eligible_as_of is eligibility for current/opening research inputs; CLOSING always returns false there, while its evidence remains available as a later label. Append later repairs without upgrading sample_slots.reference_status.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_reference -v` and record meaningful RED for this group.
- [ ] Implement association eligibility/status projection and due-work selection.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_reference -v` and confirm GREEN before the next test group.
- [ ] Implement exact opening/closing targets, future retries at target+2/+5/+15/+30 seconds then backoff capped at six hours. Persist unavailable/conflict/missing, not guessed boundary values. Short-gap page requests remain dormant, bounded at 100 reports/call and two calls/operation when later activated.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_reference -v` and confirm GREEN for this group.
- [ ] Document the six-gate future activation checklist, including Chainlink timestamp units/freshness/skew/source verification independent of CLOB time. Full offline suite GREEN.
- [ ] Stage only this task's files, check the staged diff and commit `feat: persist dormant exact-feed reference provenance and gates`.

| Test method | Required assertions after local arrange/act |
| --- | --- |
| `test_book_only_never_reads_or_constructs_credentials` | `Credential getter/signer/authenticated factory/request spies all have call_count=0` |
| `test_each_missing_activation_gate_refuses` | `Each of six false gates blocks reference-required mode before credentials/network` |
| `test_reference_numeric_failure_is_evidence` | `Extreme decoded field => decode_status='INVALID', decoded_price=None and bounded evidence` |
| `test_repaired_opening_is_prospective_only` | `Learned at T+10 => ineligible at T+9, eligible at T+10 after verification; origin stays HISTORICAL_LOOKUP` |
| `test_historical_current_never_becomes_live` | `available_live=False; original slot status unchanged; disabled reference has no report` |

## Task 9: Reconcile resolution independently through the same writer

**Files:** Create `recorder/resolution.py`, `tests/test_recorder_resolution.py`; extend store.py and models.py.

**Interfaces:** `ResolutionObservation(market_id: str, condition_id: str, raw_status: str | None, payouts: tuple[Decimal, ...] | None, reported_us: int | None, learned_wall_us: int, learned_server_us: int | None, classification: str, mapped_outcome: str | None, raw_response: bytes, provenance: dict)`; `parse_resolution(payload: object, market: MarketRevision, *, raw_response: bytes, learned_wall_us: int, learned_server_us: int | None) -> ResolutionObservation`; `reconcile_due(public: PublicGet, store: RecorderStore, *, now_us: int, deadline: float) -> int`. Store: `due_resolutions(now_us: int, *, limit: int = 20) -> tuple[MarketRevision, ...]`, `save_resolutions(observations: tuple[ResolutionObservation, ...], *, requests: tuple[HttpAttempt, ...]) -> None`. Save one shared batch attempt once and attach all matching/absent market observations in one short transaction. Preserve raw payout vector/time before normalization through raw response and spec summary columns.

- [ ] Write tests for absent/pending/resolved responses, non-unit payouts `[1000000, 0]`, reversed saved outcome indices, split payouts, missing reported time and learned-time separation. Never infer winner from quote/expiry/reference.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_resolution -v` for RED.
- [ ] Implement strict identity/finality and bounded numeric/time parsing, pure outcome mapping.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_resolution -v` and confirm GREEN for this group.
- [ ] Add tests for malformed/huge payouts, conflicting finals, multi-condition responses reordered/missing/duplicated, at most 20 due conditions, no live-reference dependence, persisted retry schedule 30/60/120 seconds capped at six hours and restart with no retry burst.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_resolution -v` and record meaningful RED for this group.
- [ ] Implement fixed public GET and writer transactions for raw observations/state/next due. Unknown/malformed final vectors remain invalid or nondirectional according to the spec; any absent/error result is recorded, not silently skipped. Final states stop routine polling; conflicts preserve both records. GREEN.
- [ ] Full suite GREEN.
- [ ] Stage only this task's files, check the staged diff and commit `feat: reconcile final payouts with independent durable resolution state`.

| Test method | Required assertions after local arrange/act |
| --- | --- |
| `test_nonunit_payout_maps_saved_indices` | `[1000000,0] maps DOWN with saved Up index=1; no unit-scale assumption` |
| `test_split_unknown_and_malformed_never_force_winner` | `Both positive => RESOLVED_OTHER/None; invalid/unknown vector => no winner and explicit diagnostic` |
| `test_reported_time_not_replaced_by_learned_time` | `Missing reported timestamp remains None despite known learning time` |
| `test_resolution_independent_of_reference` | `DISABLED reference does not block public reconciliation` |

## Task 10: Coordinate capture, rollover, failures and restart

**Files:** Create `recorder/service.py`, `tests/test_recorder_service.py`; extend models.py/store.py and recorder_support.py with deterministic end-to-end fakes.

**Interfaces:** `RunResult(run_id: str, state: str, exit_code: int, slots_finalized: int, reason: str)`; `Recorder(store: RecorderStore, public: PublicGet, *, monotonic_ns: Callable[[], int], wall_us: Callable[[], int], sleep: Callable[[float], None])`, `run(config: RunConfig, *, should_stop: Callable[[], bool]) -> RunResult`. Book-only reference is the pure disabled provider from Task 8. Separate logical due jobs share one connection owner and never run independent writer processes.

- [ ] Write fake-clock integration tests for reserve -> batch -> finalize -> future slot; rollover expires/queues old market and dynamically validates the new one. Assert one writer, expected methods/paths, no old-market request after expiry and no cross-condition pairing.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_service -v` for RED.
- [ ] Implement coordinator with cached market, refresh/calibration deadlines and an in-memory attempt buffer per operation. Convert completed pair indices to persisted request IDs only inside the writer transaction.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_service -v` and confirm GREEN for this group.
- [ ] Add groups for malformed numeric then valid next slot, partial outcome, delayed response crossing expiry, discovery gap/ambiguity, stale/discontinuous CLOB anchor, not-ready metadata and overruns.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_service -v` and record meaningful RED for this group.
- [ ] Implement the specified behavior: record explicit quality/gaps, skip passed targets and preserve raw late evidence. Discovery operation budget <=10 seconds followed by two-second pause.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_service -v` and confirm GREEN before the next test group.
- [ ] Add interruption/restart integration: stop between reservation and finalization, fail final transaction, restart under a new monotonic epoch, preserve terminal pairs, mark known-market downtime and disclose unknown-market gaps.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_service -v` and record meaningful RED for this group.
- [ ] Implement recovery and shutdown. Disk/corruption/invariant failures exit nonzero; Ctrl+C stops new requests and returns 130 after bounded persistence/close.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_service -v` and confirm GREEN before the next test group.
- [ ] Add bounded-housekeeping tests: resolution/metadata/checkpoints cannot launch work without remaining time before the next target; clamp any synchronous blocking phase to its allowance and skip elapsed slots rather than burst. Network work never holds SQLite transactions open; callback/worker code cannot write the connection.
- [ ] Assert malformed external numerics are contained without a blanket catch-all; permanent reference gate failure cannot silently change immutable run mode. In this milestone REFERENCE_REQUIRED runtime remains unavailable because no authorized live collector exists.
- [ ] Full offline suite GREEN.
- [ ] Stage only this task's files, check the staged diff and commit `feat: coordinate durable fixed-cadence recorder lifecycle`.

| Test method | Required assertions after local arrange/act |
| --- | --- |
| `test_bad_numeric_payload_does_not_end_run` | `First affected sample INVALID/FAILED; next scheduled valid pair COMPLETE; no catch-up requests` |
| `test_old_and_new_market_never_share_a_pair` | `Every pair matches its slot's immutable condition; no old read starts after expiry` |
| `test_restart_preserves_original_quality` | `Terminal evidence/learned times unchanged; downtime and interrupted slots explicit` |
| `test_write_failure_stops_capture` | `Nonzero result; no further HTTP requests or durable-success claim` |

## Task 11: Measure storage, sampling quality and coverage honestly

**Files:** Create `recorder/metrics.py`, `tests/test_recorder_metrics.py`; extend store.py for checkpoints and service.py to schedule them.

**Interfaces:** `MeasurementReport` has elapsed_seconds, mode, physical_growth_bytes, mb_per_day, gb_per_30_days, bytes_per_terminal_slot, bytes_per_complete_slot, average_depth, status_percentages, latency_percentiles, rollover_gaps, reference_coverage, resolution_summary and denominator/gap counts. `measure(store: RecorderStore, *, start_us: int, end_us: int) -> MeasurementReport`; `take_checkpoint(store: RecorderStore, *, run_id: str, wall_us: int) -> None`. Add `RecorderStore.read_only(path: Path) -> RecorderStore` for an existing DB, using SQLite mode=ro, no initialization/ownership/write permission. All store mutation methods refuse on that reader; it does not become a second writer.

- [ ] Write fixed-fixture tests: elapsed=86,400 seconds and growth=1,000,000,000 bytes yields MB/day=1000 and projected GB/30 days=30; two valid books with 4 and 8 levels yield mean depth=6; shared batch bytes count once, not twice.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_metrics -v` for RED.
- [ ] Implement physical-file/page/freelist/journal readings plus schema counters and exact labelled formulae. Save baseline before capture, hourly and clean shutdown, measuring outside an open transaction.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_metrics -v` and confirm GREEN for this group.
- [ ] Add complete/partial/missed/failed/interrupted denominator tests, pre-first-start exclusion vs restart downtime inclusion, zero elapsed/denominator returns unavailable, IN_PROGRESS listed separately and unknown markets disclosed as unassigned gap duration. Add latency p50/p95/p99/max, define nearest-rank percentiles and test empty/singleton samples.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_metrics -v` and record meaningful RED for this group.
- [ ] Implement read-only metric queries and explicit fractions. Reference-disabled =0%, backfill cannot raise live coverage, opening/closing counts distinct from slot coverage; resolution pending/conflict/directional/other counts and learning-delay remain separate. No automatic cadence mutation.
- [ ] Test restart/startup-baseline handling and checkpoint overhead: use measured actual file size as the physical authority and report missing checkpoints rather than extrapolate fabricated data. Full suite GREEN.
- [ ] Stage only this task's files, check the staged diff and commit `feat: measure recorder storage and coverage with explicit denominators`.

| Test method | Required assertions after local arrange/act |
| --- | --- |
| `test_growth_formula_and_shared_batch_bytes` | `1 GB/86,400 seconds => 1000 MB/day, 30 GB/30 days; batch counted once` |
| `test_unknown_windows_have_disclosed_gap_duration` | `Unknown markets outside eligible count but unassigned gap duration >0; no false whole-period 100%` |
| `test_percentiles_and_disabled_reference` | `[1,2,3,4] => p50=2, p95=4, p99=4; BOOK_ONLY reference coverage=0` |
| `test_read_only_report_never_creates_or_writes_db` | `Missing path refuses; existing DB unchanged; no writer/ownership lock` |

## Task 12: Expose a safe recorder CLI and milestone documentation

**Files:** Create `recorder/cli.py`, `src/record.py`, `tests/test_recorder_cli.py`, `tests/test_recorder_boundary.py`; update README.md and existing read-only-boundary tests as needed. Preserve src/main.py and the snapshot CLI interface.

**Interfaces:** `recorder.cli.main(argv: Sequence[str] | None = None) -> int`; subcommands `record --mode book-only --db PATH [--no-fast-path]` and `report --db PATH --start UTC --end UTC`. A requested `--mode reference-required` refuses with activation_required; there are no credential/signing/activation CLI options. Thin src/record.py imports main. Report opens existing SQLite read-only, creates no schema/lock/write connection and never contacts APIs.

- [ ] Write help/report/CLI tests: help makes no HTTP/SQLite/credential access, supplied book-only run warns BOOK_ONLY / REFERENCE_INCOMPLETE, missing explicit mode refuses, unavailable reference-required refuses before credentials/run creation, report has labelled units/denominators. Test actual argparse interfaces and exit codes.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_cli -v` for RED.
- [ ] Implement thin composition with trust_env=False/follow_redirects=False and the coordinator.
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_cli -v` and confirm GREEN for this group. Add spies for environment credential reads, files, adapter/signer construction and authenticated requests; none are allowed in BOOK_ONLY or help/report.
- [ ] Write boundary tests exercising every permitted route and rejecting unknown hosts, redirects, arbitrary POST, trading routes, Polymarket auth headers and any live Chainlink auth path. Update the existing AST audit for the additional fixed public host/data routes, without accepting generic method/URL arguments. Assert malicious numerics still cannot bypass the route/deadline boundary.
- [ ]
- [ ] Run `uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_recorder_cli -v` and record meaningful RED for this group.
- [ ] tighten composition/allowlist until GREEN. AST/source review supplements behavioral request capture; no AST test alone is treated as proof of safety.
- [ ] Update README to current merged baseline and recorder behavior, commands, DB/raw-depth layout, fixed cadence, non-atomic warning, failure/restart rules and original-vs-backfilled information distinction. Remove outdated milestone-1 branch/scaffold claims. Label any example data synthetic; do not claim a pilot ran or strategy completeness. Include six future gates and independent signing-clock checks; no secret names/values are loaded by usage examples.
- [ ] Full offline suite and both CLI help commands GREEN.
- [ ] Stage only this task's files, check the staged diff and commit `feat: expose explicitly book-only recorder and measurement CLI`.

| Test method | Required assertions after local arrange/act |
| --- | --- |
| `test_help_is_entirely_offline_and_credential_free` | `Exit=0; API/store/credential/signer factories call_count=0` |
| `test_record_requires_explicit_incomplete_mode` | `Missing mode refuses; book-only output contains BOOK_ONLY and REFERENCE_INCOMPLETE` |
| `test_reference_required_cannot_activate_from_cli` | `Nonzero activation_required; no credentials/adapter/run row` |
| `test_only_named_public_post_is_reachable` | `Captured POST paths exactly {'/books'}; no authenticated Polymarket/Chainlink request` |

## Task 13: Verify and review the whole change before completion

**Files:** Existing/new tests only for genuinely uncovered behavior; README corrections if verified behavior differs. Workflow evidence in the chosen execution ledger. No new product scope.

**Interfaces:** Consumes the entire branch and binding amended spec. Produces actual offline counts, review findings, read-only/dormancy audit results, Git status and declared gated limitations.

- [ ] Invoke `superpowers:verification-before-completion`. Run the complete offline suite freshly, with its socket guard, and both help commands:

```powershell
uv run --offline --locked --cache-dir .uv-cache python -m unittest discover -s tests -t . -v
uv run --offline --locked --cache-dir .uv-cache python src/main.py --help
uv run --offline --locked --cache-dir .uv-cache python src/record.py --help
```

- [ ] Review the entire feature diff against all spec sections, not just passing tests. Inspect endpoint/method construction, every numeric conversion/arithmetic/formatting site, exception boundaries, raw-byte retention, SQLite transactions/locking and all possible credential-loading/signing paths. Confirm pyproject/uv.lock and integration/global settings remain unchanged.
- [ ] Run `rtk proxy git @taskGitArgs diff --check`, `diff main...HEAD --stat`, `status --short` and inspect staged/untracked/ignored files. No runtime DB, secrets or generated pilot data enters Git. Record actual commits and final branch/worktree status; do not merge/push.
- [ ] Perform the independent whole-change review required by the chosen execution method: Native executes every task locally then one fresh reviewer inspects the complete branch; subagent-driven uses its per-task gates plus whole-change review. The review checks the amended spec, no-look-ahead rules and dormant credentials, and lists severity/file/evidence for findings. Do not dispatch reviewers while merely authoring this plan.
- [ ] Fix real review defects through systematic-debugging/TDD, preserve scope, rerun affected and full tests, and record new commits. Stop for an actual design conflict; spec wins and the ruling is recorded. No completion claim while required findings remain unresolved.
- [ ] Report offline test count/result, both help results, numeric hardening evidence, endpoint/dormancy audit, Git commits/status and independent review findings. Describe the software as recorder engineering with REFERENCE_INCOMPLETE data capability, not a verified strategy-complete dataset. Authenticated retrieval, real decoding/boundary proof and 24-hour measurements remain unperformed gates.

## Separately authorized runtime validation, not execution tasks

This plan does not authorize even a short live recorder pilot. After offline implementation/review, ask for that specific authorization with concrete commands/duration and output location. A later bounded BOOK_ONLY engineering check can verify batch identity, forced Gamma fallback, rollover, raw depth and measured growth; its output is always BOOK_ONLY / REFERENCE_INCOMPLETE. If a check is not authorized, report it as not run rather than blocked software verification or passed evidence.

A long-term run waits for all six Chainlink gates and a separately reviewed activation implementation: exact-feed access/cost, authorized credential use, actual signed retrieval, verified binary decoding/scaling and boundary behavior. During those tests verify Chainlink's real timestamp units/freshness/skew/source; never wire CLOB time into signing by assumption. No purchases/accounts/secret generation are implicit.

The future first 24-hour measurement reports every metric in spec section 11, including actual DB growth and reference coverage. It neither changes cadence nor retroactively upgrades a book-only pilot.

## Self-review and handoff

Spec coverage mapping: numeric hardening Task 1 and batch/reference/resolution regressions; API boundaries Tasks 2/3/12; all fourteen tables Tasks 4/5/7/8/9/11; clocks/slots Task 6; metadata/rollover Tasks 7/10; reference provenance/gates Task 8; resolution Task 9; recovery/ownership Tasks 4/5/10; measurements Task 11; CLI/docs Task 12; fresh verification/independent review Task 13. No active Chainlink collector is implied by the dormant reference domain.

Self-review checks: every task has a testable deliverable, named files/signatures and RED/GREEN steps; five Review Focus conditions are assigned tests; snapshot defaults remain compatible; gates precede any future credentials; no live/24-hour validation is disguised as a normal test. Exact schema columns/invariants come from spec section 7 rather than duplicated inconsistent DDL in this plan. Code examples are test assertions, not implementation bodies.

Recommendation: Native execution using executing-plans. These components share timing, evidence and writer interfaces, so implementing them in one session with a fresh whole-branch reviewer keeps integration coherent. Choose subagent-driven if per-task independent review is preferred despite its additional context cost. Review and approve this plan and select the execution method before any implementation.
