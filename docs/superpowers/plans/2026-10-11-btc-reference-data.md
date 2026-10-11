# BTC Reference-Price Data Side (Milestone 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the independent Chainlink BTC/USD Data Streams reference-price collector, including public Discovery verification, authenticated HMAC signing, Schema v2 report decoding, WebSocket live streaming, REST boundary reconciliation, and strict activation gating.

**Architecture:** A dedicated, credential-isolated Chainlink subsystem feeds point-in-time reference prices into the recorder. Live reports arrive via WebSocket into an in-memory buffer sampled at 2-second slot intervals. Housekeeping uses REST for Discovery verification and opening/closing boundary reconciliation. All data is persisted to SQLite version 1 schema tables with point-in-time eligibility tracking.

**Tech Stack:** Python 3.12+, uv, httpx, standard-library hashlib, hmac, decimal, sqlite3, unittest.

**Spec:** [Approved Milestone 2 Design](../specs/2026-10-11-btc-reference-data-design.md) and [Recorder Design](../specs/2026-10-10-market-data-recorder-design.md).

## Global Constraints

- Pinned reference target: `BTC/USD-Streams-TWAP-60s-mainnet-production`, feed ID `0x0002ee6757e8822c00d273bc340fc24c9cafe123a4ff2ea1dbdb31944bc7d95f`.
- Mandatory Discovery verification against public Chainlink Discovery endpoint before activation.
- `BOOK_ONLY` mode remains completely isolated: zero credential loads, zero Chainlink network connections.
- `REFERENCE_REQUIRED` mode fails closed if any of the six activation gates is missing.
- Database schema remains SQLite `user_version = 1`. No DDL modifications.
- Numeric safety: Inputs bounded (max 256 chars, 128 digits, exponent $\le 128$). Isolated exact Decimal arithmetic up to 512 digits.
- Point-in-time eligibility: `available_live = 1` only for timely `LIVE_LATEST` reports. Repaired/boundary reports remain `HISTORICAL_LOOKUP`.
- No live trading, orders, Polymarket private endpoints, or profitability claims.
- Every test group runs offline under unittest socket guards.

## Review Focus

1. **Discovery mismatch:** Discovery endpoint reports a feed ID, schema version, or deprecated status differing from pinned constants; activation must fail closed with an explicit error.
2. **WebSocket disconnect during active window:** Connection drops; collector must fall back to bounded REST within the 2-second budget, maintain retry backoff, and record connection gaps without dropping book capture.
3. **Stale report delivery:** WebSocket buffer contains a report with `observations_seconds` $> 5$ seconds older than decision server time; slot must mark reference `UNAVAILABLE` (`STALE_REFERENCE`).
4. **Boundary arrival delay:** Opening boundary report at $T_{\text{open}}$ is not yet produced; boundary task must record `PENDING` and schedule retries via `boundary_retry_due` without blocking slot capture.
5. **Credential isolation guard:** Live credentials loaded for Chainlink must never attach to Polymarket public requests under any code path.

---

### Task 1: Chainlink Public Discovery Client & Dynamic Feed Verification (Gate 4)

**Files:**
- Create: `src/polymarket_bot/recorder/chainlink_discovery.py`
- Test: `tests/test_chainlink_discovery.py`

**Interfaces:**
- Produces: `verify_feed_identity(discovery_payload: dict, expected_feed_id: str) -> VerifiedFeedMetadata`
- Produces: `fetch_feed_discovery(client: httpx.Client, endpoint_url: str, deadline: float) -> dict`

- [ ] **Step 1: Write failing tests for Discovery feed verification**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement Discovery parsing and validation in `chainlink_discovery.py`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

### Task 2: Chainlink HMAC Authentication & Request Signer (Gate 3)

**Files:**
- Create: `src/polymarket_bot/recorder/chainlink_auth.py`
- Test: `tests/test_chainlink_auth.py`

**Interfaces:**
- Produces: `build_chainlink_auth_headers(client_id: str, client_secret: str, method: str, path: str, body: bytes | None, timestamp_ms: int) -> dict[str, str]`
- Produces: `validate_credentials(client_id: str | None, client_secret: str | None) -> tuple[str, str]`

- [ ] **Step 1: Write failing tests for HMAC signature generation**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement HMAC signing in `chainlink_auth.py`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

### Task 3: Chainlink Report Schema v2 Decoder & Numeric Hardening (Gate 5)

**Files:**
- Modify: `src/polymarket_bot/recorder/reference.py`
- Test: `tests/test_recorder_reference.py`

**Interfaces:**
- Produces: `decode_schema_v2_report(raw_report_bytes: bytes, price_scale: int, decoder_revision: str, receipt_wall_us: int, receipt_server_us: int | None) -> ReferenceReportDraft`

- [ ] **Step 1: Write failing tests for Schema v2 payload decoding**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `decode_schema_v2_report` in `reference.py`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

### Task 4: Chainlink REST Transport Adapter for Boundary Queries & Discovery

**Files:**
- Create: `src/polymarket_bot/recorder/chainlink_rest.py`
- Test: `tests/test_chainlink_rest.py`

**Interfaces:**
- Produces: `ChainlinkRestClient(base_url: str, client_id: str, client_secret: str, transport: httpx.BaseTransport | None)`
  - `get_latest_report(feed_id: str, deadline: float) -> tuple[bytes, dict]`
  - `get_report_at_timestamp(feed_id: str, timestamp_s: int, deadline: float) -> tuple[bytes, dict]`
  - `get_reports_page(feed_id: str, start_s: int, end_s: int, limit: int, deadline: float) -> list[tuple[bytes, dict]]`

- [ ] **Step 1: Write failing tests for REST report fetching and header isolation**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `ChainlinkRestClient` in `chainlink_rest.py`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

### Task 5: Chainlink Live WebSocket Consumer & Buffer (Primary Transport)

**Files:**
- Create: `src/polymarket_bot/recorder/chainlink_ws.py`
- Test: `tests/test_chainlink_ws.py`

**Interfaces:**
- Produces: `ChainlinkLiveConsumer(ws_url: str, feed_id: str, client_id: str, client_secret: str)`
  - `start() -> None`
  - `stop() -> None`
  - `get_freshest_report(max_age_s: float, now_server_s: float) -> tuple[bytes, dict] | None`
  - `is_connected() -> bool`

- [ ] **Step 1: Write failing tests for WebSocket frame consumption and buffering**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `ChainlinkLiveConsumer` in `chainlink_ws.py`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

### Task 6: Reference Housekeeping & Boundary Reconciler (Gate 6)

**Files:**
- Modify: `src/polymarket_bot/recorder/reference.py`
- Modify: `src/polymarket_bot/recorder/service.py`
- Test: `tests/test_recorder_service.py`

**Interfaces:**
- Produces: `reconcile_due_reference_boundaries(store: RecorderStore, rest_client: ChainlinkRestClient, now_server_us: int, deadline: float) -> int`

- [ ] **Step 1: Write failing tests for boundary reconciliation integration**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement boundary reconciliation in `reference.py` and hook into `RecorderService._housekeeping`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

### Task 7: Full Service Integration & `REFERENCE_REQUIRED` Execution Mode

**Files:**
- Modify: `src/polymarket_bot/recorder/service.py`
- Modify: `src/polymarket_bot/recorder/cli.py`
- Test: `tests/test_recorder_service.py`
- Test: `tests/test_recorder_cli.py`

**Interfaces:**
- Consumes: `ChainlinkLiveConsumer`, `ChainlinkRestClient`, `verify_feed_identity`
- Modifies: `RecorderService._record` to capture live reference per slot when mode is `REFERENCE_REQUIRED`.

- [ ] **Step 1: Write failing tests for reference-required sampling and gate enforcement**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement reference capture wiring in `RecorderService`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

### Task 8: Measurement, Coverage Reporting, and Boundary Security Audit

**Files:**
- Modify: `src/polymarket_bot/recorder/metrics.py`
- Test: `tests/test_recorder_metrics.py`
- Test: `tests/test_recorder_boundary.py`

**Interfaces:**
- Produces: Updated `RecorderReport` reflecting `live_reference_coverage_pct`, `opening_boundary_covered`, `closing_boundary_covered`, and reference byte accounting.

- [ ] **Step 1: Write failing tests for reference metric reporting and boundary audit**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement metric updates and boundary assertion tests**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

## Major Checkpoints & Review Gates

- **CP0:** Implementation Setup (branch/worktree isolation, clean baseline verified)
- **CP1:** Pure Reference Core / Gate A (Tasks 1–3: Discovery verification, HMAC auth, Schema v2 decoder)
- **CP2:** Transport Layer / Gate B (Tasks 4–5: REST adapter, WebSocket consumer & buffer)
- **CP3:** Full Recorder Integration / Gate C (Tasks 6–8: boundary reconciler, service integration, metrics & security audit)
- **CP4:** Safe Runtime Validation (refusal check without credentials, public Discovery verification only)
- **CP5:** Authenticated 15-Minute Pilot (separately authorized live pilot)

## CP3 continuation ledger

Base: b5fef873a2efa454c162fec526254099130d31f8. Scope: offline partial implementation only; independent review pending.

Ruling: preserve legacy VERIFIED eligibility and never generate VERIFIED — user continuation explicitly requires UNVERIFIED structural evidence to remain unavailable; cost: no verified live coverage from this collector.
Ruling: boundary selection is injected with a policy revision; absent policy records AMBIGUOUS, no selection records PENDING — no verified Polymarket policy supplied; cost: unresolved boundaries remain due.
Ruling: live service and CLI remain refused — missing signed-binary decoder and verified boundary gates; this does not excuse missing offline integration, which remains outstanding.

Task 6 partial: dormant reconciler persists injected retrieval evidence, exact historical targets, UNAVAILABLE/DON_UNVERIFIED and retry schedule. RED observed missing reconciler assertion; focused GREEN observed persisted raw/report status and retry. The injected reader exposes fetch_boundary; real ChainlinkRestClient adapter is not yet supplied. Synchronous reconciler is not hooked into housekeeping; it does not guarantee nonblocking capture. Missing transport errors/malformed/delayed/restart integration coverage remains work, not a verified feature.

Task 7 partial: single-flight ReferenceWorker has nonblocking take, bounded join, retained active handle on shutdown failure, and no built-in SQLite access. RED observed missing worker assertion; GREEN full suite observed. Worker remains unwired to Recorder. Offline gated service, slot capture/fallback, boundary scheduling and connection-gap persistence remain outstanding. BOOK_ONLY unchanged and runtime REFERENCE_REQUIRED refuses.

Task 8 partial: metrics separate decoded research-collected, DON-unverified and cryptographically-verified report counts from legacy verified live coverage. Counts are cumulative as of report end, not interval collection counts. RED metric run emitted missing-key ERROR (not assertion failure); full GREEN observed. Existing security tests retained; no new integrated security audit yet.

Verification: exact user full offline unittest command ran 410 tests in 212.029 seconds, OK; baseline previously 407 tests. No existing tests deleted or weakened. No configured lint/typecheck command in pyproject.toml or README. No network, credential loading, installs, live runtime, CP4, push or merge.

Author review: partial only. Independent reviewer unavailable; parent dispatch required. Review new reconciler trust boundary, unbounded queue read, reader interface mismatch, policy validation, evidence learning times, worker cleanup and missing service integration first. CP3 BLOCKED/PARTIAL; no checkpoint PASS or strategy-complete claim.

### Gate C independent review remediation

Independent Gate C FAIL received against 9f5bd20. Production Gate5 signed-binary decoding and Gate6 verified selection remain unresolved: CP3 partial/BLOCKED, no activation or PASS claim.

Root causes verified: historical work monopolized single-flight dispatch before current buffer sampling; expected provider OS/connection/timeouts escaped fetch normalization; finally join error escaped RunResult; housekeeping enumerated all markets; Discovery used host-only guard, eager unbounded read, inherited redirects and exception text.

Fixes: current cheap buffer sampling independent of historical worker, busy fallback explicit REFERENCE_BUSY; provider ConnectionError/TimeoutError/OSError persist sanitized reference_transport_failed; shutdown MarketDataError returns FAILED while worker handle retained; boundary plan preparation LIMIT 2 SQL with deadline checks; Discovery exact fixed HTTPS URL, independent Request, explicit redirects false, bounded raw streaming, absolute remaining phase budget and reject-late checks, sanitized errors. Cumulative research and synthetic activation scope exposed in diagnostics and README; current httpx/websockets dependencies and dormant limits documented.

RED observed Discovery wrong-route requests and late success; service contention missing SLOT_CURRENT, provider nonzero result, shutdown exception escaping; boundary preparation forbidden full scan. GREEN focused tests then exact full offline suite 429 tests in 260.029 seconds OK. Added stale/late integrated reopen coverage. Existing audit updated narrowly to REQUIRE stream=True/follow_redirects=False for Discovery, no exemption or weakened security guard. No existing test deleted. No configured lint/typecheck command. No network, actual credentials, CP4, DDL, trading, push or merge.

Ruling: LIMIT bounds returned/prepared work, not SQLite query execution preemption; phase budgets likewise not hard total cancellation — schema fixed and synchronous operations cannot guarantee that; cost: large DB query or dependency blocking can overshoot, caller rechecks deadlines. Cheap buffer provider contract is nonblocking; arbitrary injected blocking callbacks violate that contract.

Review handoff: parent rechecks 9f5bd20..HEAD and complete CP3 b5fef87..HEAD. No independent re-review dispatched here. High live gates remain BLOCKED; medium fixes delivered for review only.

### Further autonomous continuation

Continuation base: 4566458. Prior outstanding items above describe that commit, not the current diff.

Implemented: REST on_attempt evidence preserves actual bounded raw bytes and sanitized failures; ReferenceRestEvidence adapts fixed latest/timestamp interfaces without reconstructed transport bodies. OfflineReferenceSession accepts explicit synthetic Discovery/gates, structural fields and injected providers, never loads credentials or constructs clients. Recorder creates worker only in explicitly injected REFERENCE_REQUIRED offline path, rechecks gates before requests, samples buffer then fallback concurrently with books, persists unavailable original coverage atomically with books, drains evidence solely on SQLite owner, queues opening/closing through bounded latest-ID SQL projection, retries historical work, persists bounded connection-gap diagnostics using existing event enum, and joins workers on all exit paths. Missing/malformed reference does not discard valid books. BOOK_ONLY ignores any supplied session and starts no threads.

Ruling: schema v1 run activation enum VERIFIED represents synthetic gate checks only in injected offline path; activation_evidence_json explicitly says SYNTHETIC_OFFLINE_ONLY/live_activation=false — schema has no synthetic enum and no DDL allowed; cost: consumers must read evidence scope, not treat enum alone as live authorization. Report verification is never generated VERIFIED. CLI/runtime without injection still refuses.
Ruling: offline provider envelopes are synthetic evidence, not actual HTTP/WS attempts; REST evidence adapter separately retains actual transport bodies — no false real-network provenance claim; cost: injected offline records are unsuitable as production evidence.
Ruling: all session-produced references remain UNAVAILABLE even when structurally decoded, and learned association times are owner drain times; no point-in-time completeness claim — unsigned fields and DON unverified; cost: verified live metrics remain zero.
Ruling: fractional historical targets are rejected rather than rounded — timestamp API has whole seconds and no approved fractional policy; cost: fractional boundaries require future verified selection policy.
Ruling: slow injected operations cannot be forcibly cancelled by Python; one non-daemon single-flight worker retains handle and raises shutdown failure on bounded join; workers never own SQLite — no orphan writes or fake preemptive deadline promise; cost: blocking provider may keep process alive until it returns.

TDD: fresh meaningful RED observed actual attempt count 0, missing offline session/REST adapter, worker not stopped on storage failure, research counts erased without clock, mutated gate accepted, CP2 tuple buffer discarded, gaps not persisted, feed scale not persisted. Full suite first 418 run exposed missing-policy ambiguity overwritten by late flag; fixed explicit ambiguity preservation. Some supplementary edge tests passed on first run and are coverage, not claimed RED.

Fresh full suite before final feed-persistence amendment: 422 tests in 209.563 seconds OK. Final exact full offline suite: 422 tests in 212.248 seconds, OK. Baseline 407 tests retained, no weakened assertions. No configured lint/typecheck command found. No live network, real credentials, CP4, trading, installs, push or merge.

Final review: self-review (no subagent tool). Independent review still required, no CP3 PASS. Parent review range b5fef87..HEAD, including previous partial commit. Focus: synthetic activation scope enum, single-flight capture priority, source/receipt/drain timing, REST callback evidence and postparse errors, shutdown failure behavior, same-bytes metadata conflict refusal, bounded queue/projection and restart, unsigned fields never authenticating signed bytes. Live completeness BLOCKED by Gate5 signed-binary decoder and Gate6 verified Polymarket selection; offline engineering delivered for review, not live activation.
