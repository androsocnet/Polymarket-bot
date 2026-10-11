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
