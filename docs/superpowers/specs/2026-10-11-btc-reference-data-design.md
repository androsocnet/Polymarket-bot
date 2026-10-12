# BTC Reference-Price Data Side Design (Milestone 2)

Date: 2026-10-11 (Asia/Jakarta)

Status: Approved by the user with required amendments (Discovery verification, WebSocket primary transport, explicit activation gates, and clarified backtest/authenticity scope). Implementation plan approved for autonomous major-checkpoint execution.

## Authoritative CP3 amendment — 2026-10-12

User-approved ruling overrides older boundary/availability wording: opening and closing target exact integer second T, accepting decoded signed `validFrom <= T <= observations` inclusive. No rounding. Gap-absorbing observations after T are valid historical evidence but never backdated live. Missing reports remain pending with existing retry schedule; multiple distinct containing report hashes are ambiguous and fail closed. Signed fullReport ABI content is authoritative; envelope metadata cannot override it. Structural DECODED plus UNVERIFIED evidence may be available live with correct identity, freshness and timely calibrated receipt/learning; DON verification is separate and never fabricated. Live coverage measures point-in-time availability, with verification counts separate.

Managed production lifecycle must check permission evidence before credential factories, validate Discovery and persisted pins before loading credentials, manage real CP2 REST/WS through injectable interfaces, stop workers and close resources on failures/shutdown. BOOK_ONLY creates no credential resources. Schema v1 unchanged. No runtime network, actual credentials, CP4, private Polymarket, trading, push or merge during implementation. Offline synthetic vectors do not prove DON signatures or grant runtime permission.

### CP3 remediation evidence contract

Managed coverage and replay require exact `REFERENCE_DURABLE` identity: market, role, slot, target, learning wall time, report hash and live flag. Learning timestamps are raw owner observations, not SQLite completion timestamps. The evidence durability transaction is the linked `REFERENCE_COMMIT` barrier committed after association persistence. Only a calibrated receipt sampled after that barrier returns establishes eligibility, strictly before the live two-second deadline and no later than the as-of decision. `REFERENCE_DURABLE` is the subsequently persisted attestation of that already completed barrier, not a claim that its own recording transaction completed at its embedded timestamp. Replay and metrics require resolved persisted barrier and receipt events, both bound to the fetch run, exact identity, increasing event IDs and nondecreasing raw wall times. This resolves older literal proof-commit wording without permitting precommit, backdated or clamped eligibility timestamps. Untagged legacy associations do not count as newly validated managed coverage; pure legacy compatibility remains distinct.

WebSocket fetch identity describes immutable transport arrival, not a later slot target or market. Each slot retains its own association. Conflicting authoritative decoded evidence remains fatal. Failed cleanup retains resource handles and blocks restart until cleanup succeeds. Partial persisted pins fail before credentials. Managed stream health is checked before clock or market work, including periods without slot sampling.

`reconcile_due_reference_boundaries` is quarantined to legacy offline collection: databases containing managed production runs are refused before fetching. Its `offline-reconcile-only-v1` associations are ineligible for replay and coverage, and it never manufactures durability events. DON verification remains `UNVERIFIED`; schema remains `user_version=1`.

## 1. Purpose and Boundaries

Build the independent BTC reference-price side required for a strategy-complete dataset for the Polymarket BTC 15-minute Up/Down market.

The existing milestone 1 recorder captures Polymarket public market discovery, metadata revisions, paired outcome order books, timing evidence, and eventual resolution in SQLite `user_version=1`. However, every run is currently explicitly `BOOK_ONLY` / `REFERENCE_INCOMPLETE`.

Milestone 2 designs and implements the reference-price acquisition pipeline using Chainlink Data Streams BTC/USD 60-second TWAP, fulfilling the data requirements for point-in-time signal research and market-state backtesting while maintaining strict credential isolation, offline safety, and fail-closed activation gating.

### Completeness Boundaries & Backtest Claims
- **Point-in-Time Signal Research & Market-State Backtesting:** The resulting dataset is strategy-complete for evaluating point-in-time market states, price discrepancies, and theoretical signal dynamics.
- **Explicit Trading Limitation:** This dataset is **NOT** yet sufficient for execution-realistic PnL claims. Realistic trading evaluation later requires explicit modeling of:
  1. Polymarket fee schedule (maker/taker tiers).
  2. Bid/ask crossing costs.
  3. Market price slippage.
  4. Execution fill probabilities.
  5. Passive queue position and available level depth consumption.
  6. Decision-to-order network latency.
  7. Order acknowledgement and matching-engine latency.
  8. Partial fill handling.

## 2. Reference Source Identity & Discovery Verification

Polymarket BTC 15-minute markets settle against Chainlink BTC/USD 60-second TWAP. Spot exchange prices (e.g. Coinbase, Binance) do not govern contract settlement and are excluded.

### Expected Reference Stream
- **Feed Name:** `BTC/USD-Streams-TWAP-60s-mainnet-production`
- **Feed ID:** `0x0002ee6757e8822c00d273bc340fc24c9cafe123a4ff2ea1dbdb31944bc7d95f`
- **Schema Version:** 2
- **Lookback:** 60 seconds

### Mandatory Dynamic Discovery Verification
Feed identity, schema version, decimals, and active status cannot be assumed permanently valid solely from prior documentation.
At runtime activation, `REFERENCE_REQUIRED` mode must query Chainlink's public Discovery endpoint (`GET https://api.dataengine.chain.link/api/v1/feeds`).

The recorder must verify and persist:
1. `feed_id`: Stream hex ID matching expected feed ID.
2. `name`: Full stream name from Discovery.
3. `report_schema_version`: Target schema version (2).
4. `price_scale`: Decimals verified from feed metadata (e.g. 8).
5. `status`: Must be active; deprecated or inactive feeds refuse activation.
6. `verified_us`: Calibrated server timestamp of successful Discovery verification.

**Fail-Closed Rule:** If Discovery metadata disagrees with expected feed ID, schema version, or active status, `REFERENCE_REQUIRED` mode must refuse activation immediately with an explicit error (`identity_conflict` or `feed_deprecated`). The recorder must never silently substitute another BTC/USD stream.

## 3. Commercial Terms & Subscription

- Production Chainlink Data Streams access requires active client credentials (User ID / Client ID and HMAC secret) under an active commercial subscription.
- Exact commercial pricing and terms must be verified at activation time; no fixed pricing assumptions are made.
- No paid subscription or live credentials are authorized during implementation or offline testing.

## 4. Transport Architecture

### Primary Live Transport: WebSocket Consumer & Buffer
Following Chainlink best practices for sub-second report delivery, live reports are delivered via WebSocket rather than tight REST polling:
- **Endpoint:** `wss://ws.dataengine.chain.link`
- **Operation:** Background consumer receives signed report messages for the pinned feed.
- **In-Memory Buffer:** Decoded reports update an atomic, thread-safe freshest report pointer.
- **Sampling Integration:** At each 2-second slot deadline, the recorder samples the freshest report from the buffer concurrently with Polymarket paired books.
- **Stale Detection:** Reports with `observations_seconds` older than 5 seconds relative to slot decision server time are rejected as stale (`UNAVAILABLE` with reason `STALE_REFERENCE`).
- **Deduplication:** Reports are deduplicated by `report_hash`. An unchanged report across slots links the existing stored report without duplicating storage.
- **Reconnect & Backoff:** WebSocket disconnections trigger automatic reconnection with exponential backoff (1s, 2s, 4s, capped at 30s). Connection gaps are recorded in `recorder_events`.

### Secondary / Housekeeping Transport: REST Adapter
REST calls are used only for:
1. Public Discovery verification at startup.
2. Opening boundary lookups (`OPENING`) targeting `window_start_us`.
3. Closing boundary lookups (`CLOSING`) targeting `window_end_us`.
4. Bounded short-gap repairs.
5. Bounded fallback if the WebSocket buffer is empty during a slot.

### Complete Isolation for BOOK_ONLY
`BOOK_ONLY` mode instantiates neither WebSocket nor REST Chainlink clients, makes zero outbound requests to Chainlink, and performs zero credential lookups.

## 5. Timestamp Taxonomy & Look-Ahead Bias Prevention

To maintain point-in-time validity across all research replays, four distinct timestamps are recorded:
1. `source_timestamp`: Consensus oracle observation time (`observations_seconds`, `valid_from_seconds`).
2. `receipt_timestamp`: Arrival time at the recorder (`first_received_wall_us`, calibrated `first_received_server_us`).
3. `server/chain_timestamp`: Polymarket CLOB server time from active `clock_anchors`, governing market window sampling.
4. `learned_timestamp`: raw owner learning observation (`learned_wall_us`, `learned_server_us`), not transaction completion. Managed eligibility additionally requires linked post-barrier durability receipt as specified above.

### Point-in-Time Eligibility Rules (`eligible_as_of`)
- **Current Reference:** To be used at decision time $T_{\text{decision}}$, a report must satisfy:
  - `origin == 'LIVE_LATEST'`
  - `available_live == 1`
  - `first_received_server_us <= T_decision`
  - `learned_server_us <= T_decision`
  - `valid_from_seconds * 1_000_000 <= T_decision <= expires_at_seconds * 1_000_000`
  - `T_decision - observations_seconds * 1_000_000 <= 5_000_000` (max 5s age).
- **Opening Boundary Reference:** Target is `window_start_us`. Fetched via boundary reconciliation (`OPENING`). If learned at $T_{\text{learned}} > T_{\text{open}}$, derived features like $(P_t - P_{\text{open}})$ remain unavailable (`None`) for all $t < T_{\text{learned}}$.
- **Closing Boundary Reference:** Target is `window_end_us`. Fetched post-expiry; never eligible as a live pre-expiry input.
- **Historical Repairs:** Reports obtained via historical queries have `available_live = 0` and cannot retroactively convert a missing slot into live coverage.

## 6. Report Authenticity & DON Signatures

- **API Access Authenticity:** HMAC headers authenticate communication with Chainlink endpoints.
- **Raw Evidence Retention:** Every report retains its complete raw signed bytes compressed in `reference_reports.raw_report_z` (`codec_version=1`) alongside its SHA-256 hash.
- **DON Threshold Signatures:** Milestone 2 does not execute offchain or onchain cryptographic verification of DON threshold ECDSA signatures. Reports are persisted with `verification_status = 'UNVERIFIED'`.
- Cryptographic signature validation is designated as a separate downstream gate prior to capital deployment and is not required for point-in-time market-data research when exact byte provenance is stored.

## 7. Activation Gates

`REFERENCE_REQUIRED` mode requires all six gates to be verified and present. The recorder fails closed if any gate is missing:

| Gate | Requirement | Verification Method | Credential Required | Failure Behavior |
| --- | --- | --- | --- | --- |
| **Gate 1: Feed Entitlement** | Confirmed entitlement to exact mainnet TWAP feed | Separately authorized (user documentation) | No | Refuse startup (`activation_required`) |
| **Gate 2: Subscription Decision** | Authorized acceptance of commercial subscription terms | Separately authorized (explicit user confirmation) | No | Refuse startup (`activation_required`) |
| **Gate 3: Credentials Available** | Client ID and HMAC secret available via isolated loader | Credential-required (runtime loader check) | Yes | Refuse startup (`activation_required`) |
| **Gate 4: Discovery Verification** | Active status, schema v2, matching feed ID from Discovery endpoint | Public-network-verifiable (or synthetic offline fixture) | No | Refuse startup (`identity_conflict` / `feed_deprecated`) |
| **Gate 5: Decoder & Scaling** | Schema v2 parsing, finite positive price, exact Decimal scaling | Public-network-verifiable / Offline-verifiable | Yes (live) / No (offline) | Refuse startup (`decoder_verification_failed`) |
| **Gate 6: Boundary Semantics** | Verified opening/closing boundary selection policy matching Polymarket | Offline-verifiable / Separately authorized | No | Refuse startup (`boundary_verification_failed`) |

## 8. Outages, Stale Prices, Clock Uncertainty, and Schema Invariants

- **Feed Outages:** Failed attempts recorded in `request_attempts` and `reference_fetches`. Slot association commits `status = UNAVAILABLE`, `report_id = NULL`, `available_live = 0`.
- **Stale Prices:** Reports older than 5 seconds committed as `status = UNAVAILABLE`, reason `STALE_REFERENCE`.
- **Clock Uncertainty:** CLOB anchor uncertainty $>1$s or anchor expiration ($>120$s) pauses capture.
- **Numeric Safety:** All numeric inputs bounded (max 256 chars, 128 digits, exponent $\le 128$). Isolated exact Decimal arithmetic up to 512 digits. Malformed payloads mark `decode_status = INVALID` without unhandled exceptions.
- **SQLite Schema Version:** Retains SQLite `user_version = 1`. No migrations or table alterations.
- **Restart Recovery:** Recovers pending boundary queues (`OPENING`/`CLOSING`), preserves interrupted slots, applies exponential retry backoff, and resumes only at future slots without burst catch-up.

## 9. Explicit Non-Goals
- No real or simulated trading.
- No order submission or cancellation.
- No Polymarket private/authenticated endpoints.
- No strategy optimization or signal generation.
- No backtest profitability claims.
