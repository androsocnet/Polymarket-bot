# Read-only BTC 15-minute Market Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dynamically identify the current BTC 15-minute Up/Down market, print one validated public REST market/book snapshot, and exit.

**Architecture:** Gamma owns discovery and metadata; CLOB owns server time and authoritative outcome books. A computed slug is only a fast path, backed by independent filtered-list discovery and shared strict validation. Small synchronous clients, pure parsers, a snapshot coordinator, and a CLI keep responsibilities separate without a generic HTTP framework.

**Tech Stack:** Python 3.12+, uv, httpx, standard-library dataclasses/Decimal/datetime/unittest, Git, PowerShell on Windows, and existing project-scoped RTK.

**Spec:** [Approved design](../specs/2026-10-10-read-only-market-data-design.md), committed as `41e1e28cbfefae71d8c315bd68703a4b60264f01`; user approved the written artifact on 2026-10-10.

**Plan status:** Awaiting user review and execution-method selection. No task below has been executed. The baseline commit is a future execution prerequisite, not permission to start changing files during plan authoring.

## Global Constraints

- Python 3.12 or newer; uv manages the environment and dependencies. Keep `[tool.uv] package = false`.
- One REST snapshot per invocation; no automatic output files or database.
- Public GET only: Gamma `/events/slug/{slug}`, `/events`, `/markets/{id}`; CLOB `/time`, `/book`.
- Gamma hosts metadata and outcome `clobTokenIds`; CLOB `token_id` consumes those exact strings. `positionIds` and `version` remain metadata; no protocol selector or replacement-ID guess.
- Slug pattern `btc-updown-15m-<timestamp>` is a fast path only; fallback must succeed without it.
- `eventStartTime <= server_now < endDate`; duration exactly 900 seconds. Do not substitute `startDate` or slug timestamps for window metadata.
- Prices/sizes use finite Decimal values; prices 0 through 1; sizes nonnegative; zero-size levels do not set best prices.
- Best bid/ask, midpoint, and spread come exclusively from books. Gamma `outcomePrices` is metadata; absent quotes remain absent.
- Independent source/retrieval timestamps and the exact warning: `The Up and Down books were fetched separately; this REST snapshot is not atomic.`
- One rollover rediscovery attempt; discard both old books. No continuous loop.
- Fallback: fixed end bounds `[captured_now, captured_now + 30 minutes]`, `limit=100`, up to five offset pages, `order=endDate`, `ascending=true`; do not query `recurrence=15m`.
- HTTP: 10-second connect/read/write/pool timeouts; three attempts total; retry delays 0.5 then 1 second; valid `Retry-After` within the run budget.
- Entire run shares one 120-second monotonic deadline; response bodies limited to 10 MiB. Late responses never become successful snapshots.
- UTC internally; optional Jakarta display uses UTC+07:00. No system clock changes.
- Only direct runtime dependency added is httpx; standard-library unittest is sufficient. No SDK, third-party test framework, generic middleware, transport plugins, new async runtime, or speculative abstractions.
- No wallet, private key, authentication, order creation/cancellation, strategy, signals, paper-trading engine, WebSocket, persistence, or BTC spot-price feed.
- No changes to Superpowers, RTK integration, global Git settings, or unrelated files.
- Implementation uses Superpowers TDD: write a behavioral test, observe RED, implement the smallest change, observe GREEN, then refactor while green. Initial import failure is valid only for a genuinely new module; a test syntax/setup error is not a useful RED.

## Review Focus

These additional failure modes are pinned to the named tests below:

1. JSON booleans masquerading as numbers/IDs and malformed optional prices: strict required parsing, warning-only optional metadata (Tasks 1 and 3).
2. Unexpected compressed bodies defeating a size cap: request identity encoding and reject other encodings before buffering (Task 2).
3. Trickle streams/late responses crossing the deadline: check streaming elapsed time and reject late success (Task 2).
4. Repeated full pages with duplicate candidates or changing refresh data: duplicates do not count as pagination completion; validate refreshed identity (Task 4).
5. A book disappearing exactly during rollover: verify time before deciding whether a book-404 is an ordinary failure or the one permitted rediscovery (Task 5).

## Working conventions and file map

Project root: `C:\Users\Andro Saptajulianto\Downloads\Polymarket-bot`. Paths below are relative to the executing checkout. Read both this plan and the spec before execution.

Define these session-only PowerShell arguments for Git; use the executing checkout's path if it differs from the original root:

```powershell
$gitArgs = @('-c', "safe.directory=$((Get-Location).Path)", '-c', 'user.name=Andro Saptajulianto', '-c', 'user.email=Andro.socnet@gmail.com')
```

Use `rtk git` for supported commands where it works. The known Windows ownership issue requires command-only Git `-c` arguments; `rtk proxy git @gitArgs ...` is the established fallback. Do not set global `safe.directory` or author settings. Request tool escalation if Git's protected directory prevents a write; do not bypass permissions.

After Task 0 and in each execution checkout, run tests with:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
uv run --locked --cache-dir .uv-cache python -m unittest discover -s tests -t . -v
```

`PYTHONPATH` is session-only; no packaging/build backend is introduced. Ordinary tests use MockTransport or fake clients and never contact Polymarket. Focused RED/GREEN commands use `python -m unittest tests.test_MODULE -v` under the same uv prefix. For every test group below: write the named tests/assertions, run the focused command and confirm behavioral RED, implement the specified behavior, rerun for GREEN, then move to the next group. Each action is separately checkable; do not write the whole component before observing RED. Assertion tables use values captured by each test's local arrange/act steps, not extra application interfaces.

| File | Ownership |
| --- | --- |
| `src/polymarket_bot/__init__.py`, `market_data/__init__.py` | Minimal package markers |
| `src/polymarket_bot/market_data/models.py` | Domain dataclasses and one explicit error type |
| `src/polymarket_bot/market_data/parsing.py` | Pure Gamma candidate normalization/validation |
| `src/polymarket_bot/market_data/http.py` | One small public GET helper, bounded reading/retries |
| `src/polymarket_bot/market_data/clob.py` | Server-clock adapter and book parsing/pricing |
| `src/polymarket_bot/market_data/gamma.py` | Three Gamma GET methods, no discovery rules |
| `src/polymarket_bot/market_data/discovery.py` | Fast path, fallback, uniqueness, refresh |
| `src/polymarket_bot/market_data/snapshot.py` | One snapshot and one rollover retry |
| `src/polymarket_bot/cli.py`, `src/main.py` | Console rendering/composition and thin entry point |
| `tests/__init__.py`, `tests/support.py` | Test package and deterministic fixtures/fakes |
| `tests/test_parsing.py`, `test_http.py`, `test_clob.py`, `test_discovery.py`, `test_snapshot.py`, `test_cli.py`, `test_read_only_boundary.py` | Behavioral offline tests |
| `pyproject.toml`, `uv.lock`, `README.md` | Single dependency and milestone usage instructions |

No application files are added or changed while this plan is awaiting approval.

## Task 0: Record the existing scaffold baseline before application changes

**Files:** Existing `.gitignore`, `.env.example`, `.python-version`, `pyproject.toml`, `uv.lock`, `README.md`, `src/main.py`, `tests/.gitkeep`, `data/.gitkeep`, `logs/.gitkeep`, `AGENTS.md`, `RTK.md`, `.codex/hooks.json`; approved plan separately.

**Interfaces:** Consumes the inspected scaffold and existing design commit. Produces a recorded baseline commit SHA and an unchanged, tracked scaffold with clean Git status. This task installs no packages and edits no application/configuration content.

- [ ] Inspect status/index, list ignored/untracked files, and read the 13 listed scaffold files. Check `.env.example` and scoped hook files for secrets/machine-specific content before staging. Preserve unrelated user changes; stop for clarification if new files/staged changes are unexplained.

```powershell
rtk proxy git @gitArgs status --short --ignored
rtk proxy git @gitArgs diff --cached --name-only
rtk proxy git @gitArgs ls-files --others --exclude-standard
rtk proxy git @gitArgs check-ignore .venv .uv-cache .env data/example.csv logs/example.log
```

- [ ] Commit only the now-approved plan in a separate documentation commit if it is still untracked/changed. Stage its exact path, inspect the staged diff, run `diff --cached --check`, and commit `docs: record market data implementation plan`. Do not update the approved spec merely to change its historical status text.
- [ ] Stage exactly the original scaffold/configuration manifest, never `git add .` or force-add ignored files:

```powershell
rtk proxy git @gitArgs add -- .gitignore .env.example .python-version pyproject.toml uv.lock README.md src/main.py tests/.gitkeep data/.gitkeep logs/.gitkeep AGENTS.md RTK.md .codex/hooks.json
rtk proxy git @gitArgs diff --cached --name-only
rtk proxy git @gitArgs diff --cached --check
rtk proxy git @gitArgs diff --cached
```

- [ ] Confirm the staged manifest contains only those 13 files and no market-data code/dependency edits; commit `chore: record original Python project scaffold`. Record `git rev-parse HEAD` as `$baselineSha` for final review.
- [ ] Verify `git status --porcelain` is empty, `git ls-files` includes the expected scaffold/documents, and `git ls-files .venv .uv-cache .env data logs` contains only the intended `.gitkeep` files from data/logs. Run the original offline startup check with `uv run --offline --locked --cache-dir .uv-cache python src/main.py`; expect the existing two-line scaffolding message.
- [ ] Before executing feature tasks, invoke Superpowers using-git-worktrees at execution time and follow the user's selected execution method. If isolation is needed, create it from the recorded baseline, carry the committed spec/plan, and perform remaining work there. Do not create a worktree or change branches during plan authoring. Use the installed Python path if uv cannot resolve Python; do not install another interpreter implicitly.

## Task 1: Establish strict metadata models and parsers through TDD

**Files:** Create package markers, `models.py`, `parsing.py`, `tests/__init__.py`, `tests/support.py`, `tests/test_parsing.py`.

**Interfaces:**

- `MarketDataError(code: str, message: str, *, http_status: int | None = None, market: MarketDescriptor | None = None)` is the only shared exception. Codes are the spec's error categories; no error-class hierarchy.
- Frozen `TokenOutcome(label: str, token_id: str, metadata_price: Decimal | None = None)`.
- Frozen `MarketDescriptor` fields: `event_id/event_slug/event_title/market_id/slug/question/condition_id: str`, `window_start/window_end: datetime`, `active/closed: bool`, `accepting_orders/enable_order_book: bool | None`, `up/down: TokenOutcome`, `metadata: dict[str, object]`, `warnings: tuple[str, ...]`. Property `pricing_ready: bool` requires both operational flags true.
- Frozen `BookLevel(price: Decimal, size: Decimal)`; `OutcomeBook` fields: `token_id/condition_id/source_timestamp_raw: str`, `source_at: datetime`, `bids/asks: tuple[BookLevel, ...]`, `best_bid/best_ask/midpoint/spread/bid_size/ask_size: Decimal | None`.
- Frozen `BookObservation(outcome: TokenOutcome, book: OutcomeBook, request_started_at: datetime, request_finished_at: datetime)`.
- Frozen `MarketSnapshot` fields: `market: MarketDescriptor`, `discovery_source: str`, `up/down: BookObservation`, `collection_started_at/collection_finished_at/validated_at: datetime`, `time_round_trip_seconds: float`, `rolled_over: bool`.
- `parse_market(event: dict[str, object], market: dict[str, object], *, now: datetime) -> MarketDescriptor | None`: returns None for unrelated/noncurrent markets; raises explicit errors for recognizable current BTC targets with invalid required metadata. `is_btc_family(event: dict[str, object], market: dict[str, object]) -> bool` owns family recognition.
- `tests/support.py`: `NOW = datetime(2026, 10, 10, 0, 50, tzinfo=timezone.utc)`, `TOKEN_UP="9000000000000000000001"`, `TOKEN_DOWN="9000000000000000000002"`, `CONDITION="0x" + "ab" * 32`, `MARKET_ID="1001"`; `make_event(**overrides: object) -> dict[str, object]`, `make_market(**overrides: object) -> dict[str, object]`, `make_book(**overrides: object) -> dict[str, object]` build synthetic public payloads with fixed test IDs only. `FakeClock` exposes `monotonic() -> float`, `sleep(seconds: float) -> None`, `advance(seconds: float) -> None`, `utcnow() -> datetime` without real sleeps. Fixture defaults have `eventStartTime=NOW-5 minutes`, `endDate=NOW+10 minutes`, the synthetic condition, and the two token strings. The start is aligned to a 900-second UTC boundary.

- [ ] Write `test_reversed_outcomes_preserve_exact_token_mapping` before parser code:

```python
market = make_market(outcomes='["Down", "Up"]', clobTokenIds='["9000000000000000000001", "9000000000000000000002"]')
result = parse_market(make_event(), market, now=NOW)
self.assertEqual(result.up.token_id, "9000000000000000000002")
self.assertEqual(result.down.token_id, "9000000000000000000001")
```

- [ ] Run `uv run --locked --cache-dir .uv-cache python -m unittest tests.test_parsing -v`; observe missing parser/behavior RED. Add only the domain records/parser necessary for GREEN.
- [ ] In small RED/GREEN increments add named cases: `test_window_start_inclusive_end_exclusive` (opening valid, end None); `test_creation_date_is_not_window_start`; `test_missing_window_is_invalid_metadata`; `test_rejects_wrong_asset_duration_or_status`; `test_series_rename_can_use_btc_question`; `test_conflicting_asset_metadata_is_rejected`; `test_missing_token_ids_does_not_use_position_ids` (unsupported_identifier_shape); `test_extra_version_metadata_does_not_select_ids`; `test_booleans_and_duplicate_ids_are_rejected`; `test_malformed_optional_prices_warn_without_changing_tokens`; `test_unknown_readiness_is_not_pricing_ready`. Use subTests for malformed JSON, cardinality, empty IDs, label duplicates, archived states, and price metadata shapes.
- [ ] Implement recognition from the observed series identity OR word-boundary Bitcoin/BTC plus an explicit Up-or-Down question; contradictory known asset data rejects. Parse required JSON strings and aware times strictly, validate exact duration/condition/ID shape, and retain optional version/position metadata without branching. Known timestamp-slug conflicts reject; unknown slug formats do not prevent list discovery.
- [ ] Run the full offline suite; expect passing tests and no network. Inspect diff/check whitespace.
- [ ] Commit only Task 1 files as `feat: validate read-only BTC market metadata`.

Additional assertion contract (each row belongs to the named RED/GREEN group above):

| Test(s) | Core assertions |
| --- | --- |
| window boundaries / creation date | `self.assertIsNotNone(at_start); self.assertIsNone(at_end); self.assertEqual(result.window_start, NOW - timedelta(minutes=5))` |
| missing window / booleans / malformed required arrays | `self.assertEqual(caught.exception.code, "invalid_metadata")` |
| wrong asset, duration, status / conflicting asset | `self.assertIsNone(result)` for non-target/ineligible rows; explicit malformed recognizable current rows instead raise `invalid_metadata` |
| renamed series / extra version | `self.assertEqual(result.up.token_id, TOKEN_UP); self.assertEqual(result.down.token_id, TOKEN_DOWN)` |
| missing or unusable token IDs / duplicate IDs | `self.assertEqual(caught.exception.code, "unsupported_identifier_shape")` |
| optional prices malformed | `self.assertTrue(result.warnings); self.assertIsNone(result.up.metadata_price); self.assertEqual(result.up.token_id, TOKEN_UP)` |
| unknown readiness | `self.assertFalse(result.pricing_ready)` |

## Task 2: Add one bounded httpx public GET helper through TDD

**Files:** Create `http.py`, `tests/test_http.py`; modify `pyproject.toml`, `uv.lock` by `uv add httpx --cache-dir .uv-cache` after Task 0 and approval. Inspect the resolved direct dependency; retain uv's generated lock, add no SDK/dev packages.

**Interfaces:**

- Local frozen `JsonRead(payload: object, started_mono: float, finished_mono: float)` in `http.py`. These readings bracket the final successful HTTP attempt only, excluding earlier failures/backoff, so clock anchoring is not skewed by retry time.
- `PublicGet(client: httpx.Client, *, monotonic: Callable[[], float], sleep: Callable[[float], None], utcnow: Callable[[], datetime])`.
- `PublicGet.get_json(source: Literal["gamma", "clob"], path: str, *, params: dict[str, str | int] | None = None, deadline: float) -> JsonRead`; all endpoint clients use this method and a shared absolute deadline. No method argument, credentials argument, arbitrary host, or public generic request method.

- [ ] Use MockTransport/SyncByteStream with FakeClock to write `test_only_public_get_paths_are_sent`: allowed requests capture `method == "GET"`, fixed host/path, no authorization header; unauthorized path/source is rejected before MockTransport receives anything. Run focused tests and observe RED before wrapper code.
- [ ] Implement the smallest helper: two fixed origin constants and explicit endpoint-path matching for the spec's five routes; one `client.stream("GET", ...)` call; redirect following disabled, TLS verification enabled, no auth/default sensitive headers. Encode IDs/slugs in Gamma/CLOB adapters and validate final paths. Instantiate the production client with `trust_env=False`; no ambient credentials or proxy authentication.
- [ ] Add `test_identity_stream_is_size_limited`, `test_compressed_response_is_rejected_before_reading`, `test_trickle_stream_hits_shared_deadline`, `test_late_success_is_rejected`, `test_stream_closed_after_failure` before each behavior. Test exactly 10 MiB accepted and 10 MiB + 1 rejected using valid whitespace-padded JSON, absent/lying Content-Length, and JSON parse failures. Oversize/encoding failures are nonretryable `transport_error`; invalid JSON is nonretryable `invalid_metadata` with the HTTP status retained. Gamma fast-path invalid JSON can therefore trigger the specified payload fallback rather than masquerading as a network failure. CLOB remaps that payload error as specified in Task 3.
- [ ] Read using raw streamed chunks, request `Accept-Encoding: identity`, and reject unexpected Content-Encoding before buffering. Reject oversized declared lengths early; count actual bytes before extending a bytearray and parse JSON only after EOF. Check deadline before requests, after headers, before/after each raw chunk, after JSON parsing, and before returning. Do not use unbounded `.content`/`.json()` reads or add a decompression framework. Close every response with a context manager.
- [ ] Add `test_retry_budget_is_three_total_attempts`, `test_backoff_is_point_five_then_one`, `test_retry_after_seconds_and_http_date`, `test_nonretryable_4xx_is_preserved`, `test_deadline_prevents_retry_or_sleep`. Assert 429/5xx/connect-timeout sequences, terminal 429 classification, negative/malformed Retry-After fallback, and immediate KeyboardInterrupt propagation. Valid past dates mean zero delay; scheduling uses injected utcnow only for Retry-After, never discovery authority.
- [ ] Implement 10-second phase timeouts clamped to remaining deadline; three attempts total for the named failures, delays 0.5/1 seconds, valid Retry-After overriding delay only when it fits. Do not clamp a long valid Retry-After into a shorter premature retry; terminate with its explicit diagnostic if it cannot fit. A blocking phase may notice expiry late as acknowledged by the spec; reject its result and start no more work. No worker threads, custom httpx transport, resilience library, policy objects, or asynchronous scheduler.
- [ ] Run focused/full offline tests and staged diff check.
- [ ] Commit Task 2 files as `feat: add bounded public GET transport`.

| Test(s) | Core assertions |
| --- | --- |
| allowed GET paths / forbidden route | `self.assertEqual(request.method, "GET"); self.assertNotIn("authorization", request.headers)`; forbidden route: `self.assertEqual(len(requests), 0)` |
| identity size cap | exact cap: `self.assertEqual(read.payload, {})`; cap + 1: `self.assertEqual(caught.exception.code, "transport_error")` |
| compressed body / stream closed | `self.assertEqual(stream.chunks_read, 0); self.assertTrue(stream.closed)` |
| trickle deadline / late success | `self.assertEqual(caught.exception.code, "transport_error"); self.assertEqual(len(requests), 1)` |
| retry limit / backoff | `self.assertEqual(len(requests), 3); self.assertEqual(clock.sleeps, [0.5, 1.0])` |
| valid Retry-After seconds/date | `self.assertEqual(clock.sleeps, [2.0])` for both fixtures; malformed values use `[0.5]` |
| terminal 429 / other 4xx | `self.assertEqual(caught.exception.code, "rate_limited")` for terminal 429; other 4xx: `self.assertEqual(caught.exception.http_status, 403); self.assertEqual(len(requests), 1)` |
| deadline prevents retry/sleep / interrupt | `self.assertEqual(clock.sleeps, []); self.assertEqual(len(requests), 1)`; interrupt: `self.assertRaises(KeyboardInterrupt, call)` |

## Task 3: Normalize CLOB time/books and derive book pricing through TDD

**Files:** Create `clob.py`, `tests/test_clob.py`; extend `tests/support.py` only for deterministic book/time responses.

**Interfaces:**

- `ClobClient(get: PublicGet)`, `get_time(*, deadline: float) -> JsonRead`, `get_book(token_id: str, *, deadline: float) -> JsonRead` use only `/time` and `/book`.
- `ServerClock(sample: JsonRead, *, monotonic: Callable[[], float])`; `now() -> datetime`, `utc_at(moment: float) -> datetime`, `round_trip_seconds: float`. Server seconds anchor at request midpoint; local wall-clock discovery fallback is absent.
- `parse_book(read: JsonRead, *, token_id: str, condition_id: str) -> OutcomeBook` validates both identities and calculates prices/sizes in one pass over normalized levels. Book milliseconds are converted explicitly, not through float.

- [ ] Write `test_unsorted_book_uses_decimal_extrema` and observe RED:

```python
read = JsonRead(make_book(bids=[{"price": "0.58", "size": "3"}, {"price": "0.60", "size": "2"}], asks=[{"price": "0.64", "size": "5"}, {"price": "0.61", "size": "4"}]), 2.0, 2.1)
book = parse_book(read, token_id=TOKEN_UP, condition_id=CONDITION)
self.assertEqual((book.best_bid, book.best_ask), (Decimal("0.60"), Decimal("0.61")))
self.assertEqual((book.midpoint, book.spread), (Decimal("0.605"), Decimal("0.01")))
```

- [ ] Implement the client methods, pure book parser, and minimal server clock from the interfaces. Reuse the synthetic fixture constants from Task 1; they never enter app defaults.
- [ ] Add RED/GREEN cases `test_token_and_condition_mismatch_fail`, `test_empty_sides_never_use_gamma_or_half`, `test_zero_sizes_are_ignored_duplicate_top_sizes_sum`, `test_locked_book_valid_crossed_book_invalid`, `test_malformed_levels_fail_entire_book`, `test_price_size_booleans_fail`, `test_seconds_server_time_milliseconds_book_time`, `test_server_clock_anchors_request_midpoint`, `test_old_unchanged_book_timestamp_is_retained`. Assert nonfinite/negative values, missing arrays, invalid IDs/timestamps, and retained independent source times.
- [ ] Reject incompatible timestamp shapes: `/time` must be a positive nonboolean integer in Unix seconds convertible to aware UTC; book timestamp must be a 13-digit decimal-string Unix millisecond value convertible to aware UTC. This book-width check is an explicit adapter assumption for the observed current service, verified in the live smoke check, not a freshness threshold. Do not reinterpret a seconds-shaped book field or guess units silently. A valid old timestamp alone is not a freshness failure.
- [ ] For books, preserve transport/rate-limit errors, translate HTTP 404 to `book_unavailable` with `http_status=404`, and invalid JSON/root/levels to `invalid_book`. `/time` failures become `time_unavailable` with the original status/reason in the message. Identity mismatches are `book_identity_mismatch`.
- [ ] Run focused/full offline tests and diff check.
- [ ] Commit `feat: read and price validated CLOB books`.

| Test(s) | Core assertions |
| --- | --- |
| identity mismatch | `self.assertEqual(caught.exception.code, "book_identity_mismatch")` |
| empty sides | `self.assertIsNone(book.midpoint); self.assertIsNone(book.spread)`; fully empty: `self.assertIsNone(book.best_bid); self.assertIsNone(book.best_ask)` |
| duplicate top sizes / zeros | `self.assertEqual(book.bid_size, Decimal("5")); self.assertEqual(book.best_bid, Decimal("0.60"))` with zero-size higher levels |
| locked / crossed / malformed | locked: `self.assertEqual(book.spread, Decimal("0"))`; crossed/malformed/booleans: `self.assertEqual(caught.exception.code, "invalid_book")` |
| explicit units / midpoint clock | `self.assertEqual(book.source_at, NOW); self.assertEqual(clock.now(), NOW + timedelta(seconds=1))` for server sample at monotonic 0..2 and current monotonic 2 |
| unchanged source timestamp | `self.assertEqual(book.source_at, NOW - timedelta(days=1))` |
| book 404 / malformed time | `self.assertEqual(caught.exception.code, "book_unavailable"); self.assertEqual(caught.exception.http_status, 404)`; time: `self.assertEqual(caught.exception.code, "time_unavailable")` |

## Task 4: Implement authoritative discovery with a slug fast path through TDD

**Files:** Create `gamma.py`, `discovery.py`, `tests/test_discovery.py`; extend support.py with FakeGamma whose methods match GammaClient.

**Interfaces:**

- `GammaClient(get: PublicGet)`: `event_by_slug(slug: str, *, deadline: float) -> dict[str, object]`, `list_events(*, end_min: datetime, end_max: datetime, offset: int, deadline: float) -> list[dict[str, object]]`, `market_by_id(market_id: str, *, deadline: float) -> dict[str, object]`.
- `discover_current(gamma: GammaClient, *, clock: ServerClock, deadline: float, use_fast_path: bool = True) -> tuple[MarketDescriptor, str]`; source is exactly `fast_path` or `filtered_list`.
- Consumes `parse_market`, `is_btc_family`, MarketDataError, and the shared deadline. Gamma adapter validates endpoint root types, not domain rules.

- [ ] Write `test_list_discovery_survives_slug_rename_and_disabled_fast_path`; fixture question/series/time identifies current BTC despite a wholly different slug. Assert selected IDs and source `filtered_list`; FakeGamma must raise if the fast path is called when disabled. Run focused tests; observe RED.
- [ ] Implement Gamma's three GET methods and discovery. Fast path computes the 900-second start and examines all event markets. Missing/invalid/wrong-window/ambiguous fast-path data falls back; HTTP transport failures preserve their diagnostics. A candidate's naming alone cannot pass local validation.
- [ ] Add RED/GREEN cases `test_fast_path_validates_all_children`, `test_fast_404_falls_back`, `test_future_open_market_not_selected`, `test_fixed_bounds_across_pages`, `test_filters_do_not_send_recurrence`, `test_ambiguous_distinct_market_ids_fail`, `test_zero_candidates_fail`, `test_current_target_missing_ids_is_explicit_error`, `test_repeated_full_pages_remain_incomplete`, `test_refreshed_identity_change_is_rejected`.
- [ ] Implement complete bounded enumeration: 100 raw events/page, offsets 0/100/200/300/400, fixed 30-minute bounds; short raw page ends scanning. Deduplicate by event/market IDs without using deduplicated length as completion proof. Five full pages => discovery_incomplete even if one plausible candidate was seen. Conflicting duplicates cannot be silently merged into a trustworthy candidate. Exactly one eligible market is refreshed and reparsed with its original event context; refresh market ID/condition/token/window changes invalidate that attempt rather than mixing identities.
- [ ] Preserve current-target unsupported identifier/required-field diagnostics instead of reporting them as a successful alternative. Unrelated/expired/future rows can be skipped with diagnostic reasons. Operational unavailability does not select a different future market. Confirm no fixed research IDs or sole dependency on slug convention.
- [ ] Run focused/full offline tests and diff check.
- [ ] Commit `feat: discover current BTC market with Gamma fallback`.

| Test(s) | Core assertions |
| --- | --- |
| renamed slug / disabled fast path | `self.assertEqual(source, "filtered_list"); self.assertEqual(result.market_id, MARKET_ID); self.assertEqual(gamma.slug_calls, [])` |
| fast path all children / 404 fallback | `self.assertEqual(result.up.token_id, TOKEN_UP)`; sources respectively `self.assertEqual(source, "fast_path")` / `self.assertEqual(source, "filtered_list")` |
| fixed bounds / no recurrence | `self.assertEqual({call.end_min for call in calls}, {NOW}); self.assertEqual({call.end_max for call in calls}, {NOW + timedelta(minutes=30)}); self.assertNotIn("recurrence", captured_query)` |
| ambiguity / zero or future-only | `self.assertEqual(caught.exception.code, "ambiguous_market")` / `self.assertEqual(caught.exception.code, "no_current_market")` |
| full duplicate pages | `self.assertEqual(caught.exception.code, "discovery_incomplete"); self.assertEqual(offsets, [0, 100, 200, 300, 400])` |
| missing target IDs / changed refresh | `self.assertEqual(caught.exception.code, "unsupported_identifier_shape")` / `self.assertEqual(caught.exception.code, "invalid_metadata")` |

## Task 5: Coordinate one snapshot and bounded rollover through TDD

**Files:** Create `snapshot.py`, `tests/test_snapshot.py`; extend support.py with FakeClob matching the client methods.

**Interfaces:**

- `collect_snapshot(gamma: GammaClient, clob: ClobClient, *, monotonic: Callable[[], float], use_fast_path: bool = True) -> MarketSnapshot` creates one `deadline = monotonic() + 120.0` shared by all work, including retry/rediscovery.
- Consumes discover_current, ServerClock, parse_book and model records. Does not construct HTTP clients or format output.
- MarketDataError's optional `market` carries identified metadata for an explicitly incomplete readiness result. No cached book state persists across calls.

- [ ] Write `test_rollover_discards_both_books_and_rediscover_once`; FakeClob's final time crosses the first window. Assert the returned market/books are all from attempt two, `rolled_over is True`, and every fake call received the same absolute deadline. Run RED, then implement only the coordinator loop limited to two attempts.
- [ ] Obtain initial time, discover, reject missing/false readiness as `book_unavailable` with identified metadata, fetch Up then Down, parse/verify each, and re-read time. Build separate BookObservation request timestamps through that attempt's server clock. Validate the fresh final time against the selected window and check the shared deadline again immediately before returning the snapshot; store both source timestamps, collection interval, final sample/RTT. Use `clock.now()` from the final sample to include elapsed parsing time in the final-window validation.
- [ ] Add RED/GREEN cases `test_second_rollover_exhausts`, `test_independent_book_timestamps_are_preserved`, `test_no_book_reuse_across_runs_or_conditions`, `test_book_404_during_rollover_rediscover_once`, `test_book_404_inside_window_fails`, `test_partial_book_failure_is_not_success`, `test_invalid_time_never_uses_local_wall_clock`, `test_readiness_failure_keeps_current_metadata`.
- [ ] On a book-404 only, take a fresh time sample: if its window expired, take the same one allowed rediscovery; otherwise preserve book_unavailable. Invalid book identity/shape remains a hard failure, not a rollover retry. Time/transport failures remain failures. If attempt two expires (including its book-404), fail rollover_exhausted. Never print intermediate attempts.
- [ ] Run focused/full offline tests and diff check.
- [ ] Commit `feat: assemble one market snapshot with rollover protection`.

| Test(s) | Core assertions |
| --- | --- |
| rollover discards both books | `self.assertTrue(result.rolled_over); self.assertEqual(result.up.book.token_id, NEW_UP); self.assertEqual(result.down.book.token_id, NEW_DOWN); self.assertEqual(set(deadlines), {120.0})` for monotonic start 0 |
| second rollover / book 404 at rollover | `self.assertEqual(caught.exception.code, "rollover_exhausted")`; first rollover 404: `self.assertTrue(result.rolled_over)` |
| independent source times | `self.assertNotEqual(result.up.book.source_at, result.down.book.source_at)` |
| no reuse / partial failure | `self.assertEqual(book_calls, [TOKEN_UP, TOKEN_DOWN, TOKEN_UP, TOKEN_DOWN])` across two independent successful runs; partial failure: `self.assertRaises(MarketDataError, call)` |
| in-window book 404 | `self.assertEqual(caught.exception.code, "book_unavailable"); self.assertEqual(discovery_attempts, 1)` |
| invalid time / unknown readiness | `self.assertEqual(caught.exception.code, "time_unavailable")`; readiness: `self.assertEqual(caught.exception.code, "book_unavailable"); self.assertEqual(caught.exception.market.market_id, MARKET_ID); self.assertEqual(book_calls, [])` |

## Task 6: Add CLI presentation and enforce the read-only boundary through TDD

**Files:** Create `cli.py`, `tests/test_cli.py`, `tests/test_read_only_boundary.py`; modify `src/main.py:1` onward (currently only the startup check) and README.md. No other scaffold/RTK configuration changes.

**Interfaces:**

- `render_snapshot(snapshot: MarketSnapshot, *, jakarta: bool = False, debug: bool = False) -> str` is pure.
- `run_snapshot(gamma: GammaClient, clob: ClobClient, *, monotonic: Callable[[], float], use_fast_path: bool, jakarta: bool, debug: bool, stdout: TextIO, stderr: TextIO) -> int` calls collect_snapshot and handles presentation/errors.
- `main(argv: Sequence[str] | None = None) -> int` parses only `--no-fast-path`, `--jakarta`, `--debug`, and composes one context-managed httpx.Client/PublicGet/GammaClient/ClobClient with real clocks. `src/main.py` exits with that return code.

- [ ] Write `test_non_atomic_warning_and_reference_price_labels` on a synthetic snapshot; assert the spec's exact warning, distinct book times, explicit bid/ask, and label `midpoint reference price`. Write `test_one_run_exits_and_never_persists`; run focused RED before CLI code.
- [ ] Implement rendering/composition with Decimal strings, explicit unavailable fields, book-size/depth summaries, UTC and optional UTC+07:00 times, window/IDs/status/source/collection interval. `--debug` shows Gamma prices/version/position fields only with metadata labels. No automatic writes, configuration files, wallet setup, or background work.
- [ ] Add RED/GREEN cases `test_empty_books_exit_zero_with_unavailable_quotes`, `test_failure_exit_one_not_success`, `test_unready_market_prints_incomplete_metadata`, `test_interrupt_exits_130_without_retry`, `test_jakarta_conversion`, `test_debug_prices_do_not_replace_books`, `test_no_fast_path_flag_selects_list_flow`. Normal failures print code plus actionable message to stderr; no successful snapshot on stdout. Identified unready metadata is explicitly incomplete, not a success.
- [ ] Write `test_request_surface_is_public_get_only` using a full MockTransport snapshot run and forced-list run. Assert every request host/path is one of the allowed endpoints, method GET, no authorization/API-key/signature headers, exact Gamma token_id requests, no external reference-price feed, and no filesystem writes. Fail if a wrong-ID book is accepted. Keep network guards for ordinary tests.
- [ ] Add a narrow AST/source audit test `test_application_has_no_authenticated_or_order_request_surface`: inspect src, confirm http.py is the sole raw httpx request site with literal GET, fixed allowed route construction, no order/auth route literals or SDK imports, and no dynamic HTTP method injection. Runtime capture complements this audit; text search alone cannot prove reachability.
- [ ] Update README only for one-snapshot commands, dependency/test instructions, meanings of bid/ask/reference midpoint, exit/availability/rollover behavior and the non-atomic limitation. State no credentials are needed and no persistence/trading exists. Keep uv package=false and .env.example untouched.
- [ ] Run focused/full offline tests and diff check.
- [ ] Commit `feat: expose read-only market snapshot CLI`.

| Test(s) | Core assertions |
| --- | --- |
| warning / reference labels | `self.assertIn("The Up and Down books were fetched separately; this REST snapshot is not atomic.", output); self.assertIn("midpoint reference price", output)` |
| exit/no persistence / empty books | `self.assertEqual(exit_code, 0); self.assertEqual(files_after, files_before)`; empty books: `self.assertIn("unavailable", output)` |
| failure / incomplete readiness | `self.assertEqual(exit_code, 1)`; ordinary failure: `self.assertEqual(stdout.getvalue(), "")`; readiness: `self.assertIn("incomplete", output)` |
| interruption / Jakarta | `self.assertEqual(exit_code, 130)`; display: `self.assertIn("07:50", output); self.assertIn("+07:00", output)` for NOW |
| debug prices / forced-list | `self.assertEqual(rendered_bid, "0.60")` despite Gamma metadata 0.99; `self.assertEqual(gamma.slug_calls, [])` |
| runtime request capture / source boundary | `self.assertTrue(all(r.method == "GET" for r in requests)); self.assertEqual(requested_tokens, [TOKEN_UP, TOKEN_DOWN]); self.assertEqual(forbidden_request_sites, [])` |

## Task 7: Final verification and Git review (must finish the milestone)

**Files:** Existing/new application/tests/README/lock; no new reporting database, smoke script, or credential file. Any discovered defect returns to its owning task's RED/GREEN cycle before repeating affected checks.

**Interfaces:** Consumes the executable CLI and passing offline suite. Produces verification evidence, reviewed implementation commits separate from the original-scaffold baseline, and a candid final report. No deployment, push, real/paper execution engine, or additional feature work.

- [ ] Invoke Superpowers verification-before-completion. Run all offline tests:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
uv run --offline --locked --cache-dir .uv-cache python -m unittest discover -s tests -t . -v
uv run --offline --locked --cache-dir .uv-cache python src/main.py --help
```

Expected: all tests pass without network; CLI help contains only the snapshot options. Confirm no tests silently contact the live API and no forbidden capability was added to make a test pass.

- [ ] Run the live public read-only smoke check manually through the real CLI, without fixed IDs/prices:

```powershell
uv run --locked --cache-dir .uv-cache python src/main.py --jakarta
uv run --locked --cache-dir .uv-cache python src/main.py --no-fast-path --jakarta
```

Expected: a current validated BTC 900-second window; Gamma-to-CLOB condition/token matches; each book's timestamps and quotes; the non-atomic warning. Run two separate checks, not a polling loop. Independent runs may legitimately select different windows/prices. A service outage, schema issue, cap exhaustion or unavailable book is documented as a failed/incomplete live integration check, never called a pass or bypassed with old IDs. Do not loosen safety/window rules to manufacture success.

- [ ] Review the public request boundary and ignored/tracked content after the smoke check:

```powershell
uv run --offline --locked --cache-dir .uv-cache python -m unittest tests.test_read_only_boundary -v
rg -n 'https?://|\.stream\(|\.request\(|\.post\(|\.put\(|\.patch\(|\.delete\(|Authorization|api[_-]?key|private[_-]?key|/orders?|/cancel|/auth' src
rtk proxy git @gitArgs diff --check
rtk proxy git @gitArgs diff --stat $baselineSha
rtk proxy git @gitArgs diff $baselineSha -- src pyproject.toml uv.lock README.md
rtk proxy git @gitArgs diff $baselineSha -- AGENTS.md RTK.md .codex .env.example
rtk proxy git @gitArgs status --short --ignored
```

Expected: boundary tests pass; source audit shows only approved public origins/routes and the literal GET stream call. Manually inspect every request construction: there is no authenticated endpoint, order endpoint, signing capability, SDK, or non-GET request in application code. The RTK/config/.env-example diff is empty. Source/dependency changes are attributable to the feature commits after baseline; .venv/.uv-cache/generated bytecode stay ignored, .env remains untracked, and data/logs contain no runtime outputs.

- [ ] Inspect final tracked files, staged diff and status; any remaining intended changes must have their own reviewed commit. Never fold feature edits into the scaffold baseline or amend it. Require empty `git status --porcelain` for the executing checkout; preserve/explain unrelated user changes rather than deleting them. Confirm no global Git configuration was written.
- [ ] Perform the whole-change review required by the selected Superpowers execution workflow, then report offline results, actual live-check outcome, IDs/times only as observed evidence, Git baseline/feature commit separation, and the source-boundary audit result. If blocked externally, state that precise limit. Stop at the read-only milestone.

## Self-review and execution handoff

Plan self-review checklist (completed when this artifact is presented):

- Spec sections 1-4 map to global constraints/Tasks 1-4; component boundaries to the file/interface map; discovery/time to Tasks 3-5; book pricing to Task 3; output/errors to Tasks 2/5/6; verification to Tasks 6/7.
- User's Git-baseline amendment is Task 0, before dependencies or application edits, with explicit tracked-file manifests and separate documentation/scaffold/feature commits.
- Transport remains one fixed-endpoint wrapper; phase timeouts are distinguished from total elapsed time, streams are bounded, and no generic future API machinery is introduced.
- All five Review Focus items have named owning tests. Fixture/client interfaces are defined before consumers; source/retrieval timestamps, error codes and field names agree across tasks.
- Each feature task has a RED/GREEN cycle and focused/full checks before its commit. Task 7 finishes with offline tests, actual live GET reads, Git diff/status review, and authenticated/order-surface verification.
- No plan step authorizes implementation before user review and execution-method selection. No baseline commit, dependency installation, new package, test code, or application change occurred during plan authoring.

Recommend native execution using superpowers:executing-plans: these tasks have sequential interface dependencies and a small shared HTTP boundary, so one implementer avoids repeated handoffs. The user may choose subagent-driven execution for independent task-by-task reviews instead. Either approach still requires TDD, the separate baseline, and all final checks. Do not dispatch agents or start either execution skill until the user has reviewed this plan and selected the method.
