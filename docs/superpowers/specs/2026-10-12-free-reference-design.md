# FREE REFERENCE Development Mode Design

Date: 2026-10-12
Status: Revised and approved by user (design), awaiting implementation-plan approval

## 1. Purpose and Boundaries

Add an APPROXIMATE_REFERENCE mode to the Polymarket BTC 15-minute price recorder
for development, recorder testing, data collection, strategy research, backtesting,
and later paper-trading research using **free public BTC market data from exchanges**.

This mode must NEVER claim to reproduce the exact Chainlink BTC/USD TWAP 60s
settlement reference.

The existing Chainlink implementation is preserved exactly as-is. DON
verification remains UNVERIFIED. No credentials, authentication, or private
endpoints are used.

## 2. Semantic Boundary

### EXACT reference (unchanged)
- Chainlink Data Streams / future authenticated exact settlement source
- Mode: `REFERENCE_REQUIRED` (existing)
- Feed: `BTC/USD-Streams-TWAP-60s-mainnet-production`
- Verification status: `UNVERIFIED` (DON signatures not verified)
- Tables: `reference_feeds`, `reference_reports`, `reference_fetches`,
  `reference_associations` (schema v1)

### APPROXIMATE reference (new)
- Free public exchange-derived BTC reference
- Mode: `APPROXIMATE_REFERENCE` (new)
- NO claim of settlement equivalence
- NO Chainlink identity, NO DON verification, NO settlement truth
- NO claim of producing a TWAP
- Sources: Coinbase public BTC/USD, Kraken public BTC/USD
- Tables: `approximate_feeds`, `approximate_observations`, `approximate_references`
  (schema v2, fully separate from Chainlink tables)

### BOOK_ONLY (unchanged)
- Book observations only, no reference collection
- Mode: `BOOK_ONLY` (existing)
- Tables: existing v1 tables only

**Rule**: Approximate data must NEVER enter Chainlink reference tables.
Approximate data must NEVER be labeled: exact, settlement reference,
Chainlink, DON verified, or resolution truth.

## 3. Providers and Public APIs

### 3.1 Coinbase (PRIMARY)

**REST endpoint**: `GET https://api.exchange.coinbase.com/products/BTC-USD/ticker`

Response schema:
```json
{
  "trade_id": 123456789,
  "price": "6268.48",      // last trade price string
  "size": "0.00698254",    // last trade size string
  "time": "2020-03-20T00:22:57.833Z",  // ISO 8601 UTC
  "bid": "6265.15",
  "ask": "6267.71",
  "volume": "53602.03940154"
}
```

- No API key required
- No authentication headers
- Public endpoint
- `time` field provides a parseable exchange timestamp (`source_timestamp_us`)
- `price` field provides the last trade price (used as the sampled observation)
- `bid`/`ask` provide book depth snapshot (preserved as optional provenance)

**WebSocket**: `wss://ws.exchange.coinbase.com` (ticker channel) — Phase B only

### 3.2 Kraken (SECONDARY)

**REST endpoint**: `GET https://api.kraken.com/0/public/Ticker?pair=XBTUSD&assetVersion=1`

Response schema:
```json
{
  "error": [],
  "result": {
    "BTC/USD": {
      "a": ["30300.10000", "1", "1.000"],  // ask [price, lot volume, lot volume]
      "b": ["30300.00000", "1", "1.000"],  // bid [price, lot volume, lot volume]
      "c": ["30303.20000", "0.00067643"],  // last trade [price, lot volume]
      "v": ["4083.67001100", "4412.73601799"],  // volume [today, 24h]
      "p": ["30706.77771", "30689.13205"],  // VWAP [today, 24h]
      "t": [34619, 38907],  // trade count [today, 24h]
      "l": ["29868.30000", "31631.00000"],  // low [today, 24h]
      "h": ["31631.00000", "31631.00000"],  // high [today, 24h]
      "o": "30502.80000"  // opening price
    }
  }
}
```

- No API key required
- No authentication
- Public endpoint
- `assetVersion=1` returns display names `BTC/USD` instead of `XXBTZUSD`
- Quote currency is USD

**Kraken timestamp semantics**:
- The Kraken public Ticker endpoint does NOT return a trustworthy per-trade
  exchange timestamp.
- Therefore `source_timestamp_us = None` for Kraken ticker observations.
- Freshness and ordering decisions use:
  - `receipt_wall_us` (local wall receipt time)
  - `receipt_server_us` (calibrated server receipt time, if clock is usable)
  - Monotonic receipt timing
- This limitation is recorded explicitly in provenance and diagnostics.
- **Do not fabricate or infer an exchange timestamp.**

**WebSocket**: `wss://ws.kraken.com` (trade channel) — NOT used in Phase A.
The `/public/Trades` REST endpoint provides trade-level timestamps but is out
of scope for the initial V1.

### 3.3 Design Decision: REST-only for Phase A

Per the staged approach:
- **Phase A**: REST compatibility probes + parser implementations
- **Phase B**: WebSocket probes (optional, for better TWAP quality in a future milestone)

REST polling at 2-second cadence (matching the existing recorder slot cadence)
is sufficient for development/research. WebSocket can be added later.

## 4. Provider Abstraction

New module: `src/polymarket_bot/recorder/free_reference.py`

### 4.1 Core Protocol

```python
@runtime_checkable
class ApproximateReferenceProvider(Protocol):
    """A public exchange provider returning BTC/<quote> observations."""

    @property
    def provider_name(self) -> str:
        """e.g. 'coinbase', 'kraken'"""

    @property
    def pair(self) -> str:
        """e.g. 'BTC/USD'"""

    @property
    def quote_currency(self) -> str:
        """e.g. 'USD'"""

    def fetch_ticker(self, *, deadline: float) -> ApproximateObservation:
        """Fetch a single ticker observation from the public endpoint."""
```

### 4.2 Evidence Dataclasses

```python
@dataclass(frozen=True)
class ApproximateObservation:
    provider: str                       # 'coinbase', 'kraken'
    pair: str                           # 'BTC/USD'
    quote_currency: str                 # 'USD'
    price: Decimal                      # raw price, positive
    bid_price: Decimal | None           # raw bid, positive or None
    ask_price: Decimal | None           # raw ask, positive or None
    source_timestamp_us: int | None     # exchange timestamp if trustworthy; None otherwise
    receipt_wall_us: int                # local wall receipt time
    receipt_server_us: int | None       # calibrated server receipt time
    raw_payload_hash: str               # sha256 of raw response bytes
    raw_payload_size: int               # bytes
    transport_type: str                 # 'rest'
    parser_revision: str                # e.g. 'coinbase-ticker-v1'
    http_status: int | None             # HTTP status code
    result: str                         # 'success' or error code
    error_code: str | None              # error code if failed
```

```python
@dataclass(frozen=True)
class ApproximateReferenceReport:
    """Computed approximate reference from public exchange observations.
    
    V1 semantics: APPROXIMATE_SAMPLE_MEAN_60S — arithmetic mean of sampled
    prices over the previous 60-second window. NOT a TWAP, NOT equivalent to
    Chainlink's BTC/USD TWAP 60s.
    """
    mode: str = "APPROXIMATE"            # hard-coded semantic label
    reference_kind: str                  # 'APPROXIMATE_SAMPLE_MEAN_60S'
    value: Decimal | None               # mean price, None if computation failed
    window_start_us: int                # start of 60-second window
    window_end_us: int                  # end of 60-second window
    observation_count: int              # number of contributing observations
    source_provider: str                # which provider's data was used
    was_fallback: bool                  # True if Coinbase was unavailable and Kraken used
    verification_status: str = "UNVERIFIED"  # NEVER VERIFIED
    receipt_wall_us: int                # local wall receipt time
    receipt_server_us: int | None       # calibrated server receipt time
    diagnostics: tuple[str, ...] = ()  # error codes if computation failed
```

## 5. APPROXIMATE_SAMPLE_MEAN_60S Calculation

**NOT a TWAP** — this is an arithmetic mean of sampled REST ticker snapshots:

- **Window**: 60-second sliding window. `window_end_us` is the receipt time;
  `window_start_us = window_end_us - 60_000_000`
- **Input**: price observations from the selected provider (Coinbase primary,
  Kraken fallback) within the window
- **Method**: simple arithmetic mean of accepted sampled prices
- **No look-ahead**: observations with `receipt_wall_us` or
  `receipt_server_us` > `window_end_us` are excluded
- **No future observations**: rejected explicitly
- **Stale exclusion**: observations older than the 60-second window are excluded
- **No interpolation**: gaps result in fewer observations, not interpolated values
- **Missing data**: if zero observations in window, `value = None` with
  diagnostics `MISSING_OBSERVATIONS`
- **Deterministic replay**: same observations always produce same result
- **Never claimed equivalent to** Chainlink BTC/USD TWAP 60s

### 5.1 Multi-source V1 Policy: Independent Recording + Explicit Fallback

**NO median or consensus aggregation in V1.**

Record Coinbase and Kraken observations independently with full provenance.

**V1 source selection policy**:

- **Coinbase BTC/USD**: PRIMARY approximate source
- **Kraken BTC/USD**: SECONDARY corroboration / explicit fallback

**Selection rules**:
1. If Coinbase observation is fresh (within 60s window) and valid → use Coinbase
2. If Coinbase is missing or stale and Kraken is fresh and valid → use Kraken
   with `was_fallback=True` and diagnostic `FALLBACK_COINBASE_UNAVAILABLE`
3. If both are missing/stale → `value = None` with diagnostic `MISSING_OBSERVATIONS`
4. If both are valid → both observations are preserved independently in the
   `approximate_observations` table; the reported value still comes from the
   PRIMARY (Coinbase). The Kraken observation is stored as corroboration.

**Fallback must never be invisible**:
- `source_provider` field in the report always names the provider used
- `was_fallback` is True when Kraken was used due to Coinbase unavailability
- `diagnostics` records the fallback reason
- Metrics expose per-provider availability and fallback counts

## 6. Schema V2 Safety

### 6.1 Schema Versioning Rules

- **Existing v1 databases** (BOOK_ONLY, REFERENCE_REQUIRED): remain untouched,
  no automatic migration
- **APPROXIMATE_REFERENCE mode**: requires a NEW v2 database
  - v2 contains all v1 tables + new `approximate_*` tables
  - If approximate mode is pointed at an existing v1 database, FAIL clearly and
    safely rather than silently upgrading
- No destructive migration. Any future v1 → v2 in-place migration is a separate
  explicitly approved task

### 6.2 New Tables (v2)

```sql
CREATE TABLE approximate_feeds (
    provider TEXT NOT NULL,          -- 'coinbase', 'kraken'
    pair TEXT NOT NULL,              -- 'BTC/USD'
    quote_currency TEXT NOT NULL,    -- 'USD'
    PRIMARY KEY (provider, pair)
);

CREATE TABLE approximate_observations (
    observation_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES recording_runs(run_id),
    provider TEXT NOT NULL,
    pair TEXT NOT NULL,
    quote_currency TEXT NOT NULL,
    price_text TEXT NOT NULL,
    bid_price_text TEXT,
    ask_price_text TEXT,
    source_timestamp_us INTEGER,
    receipt_wall_us INTEGER NOT NULL,
    receipt_server_us INTEGER,
    raw_payload_hash TEXT NOT NULL,
    raw_payload_size INTEGER NOT NULL,
    transport_type TEXT NOT NULL,
    parser_revision TEXT NOT NULL,
    http_status INTEGER,
    result TEXT NOT NULL,
    error_code TEXT,
    UNIQUE (run_id, provider, receipt_wall_us)
);

CREATE TABLE approximate_references (
    reference_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES recording_runs(run_id),
    mode TEXT NOT NULL CHECK (mode = 'APPROXIMATE'),
    reference_kind TEXT NOT NULL,   -- 'APPROXIMATE_SAMPLE_MEAN_60S'
    value_text TEXT,                -- mean price, NULL if computation failed
    window_start_us INTEGER NOT NULL,
    window_end_us INTEGER NOT NULL,
    observation_count INTEGER NOT NULL,
    source_provider TEXT NOT NULL,  -- 'coinbase' or 'kraken'
    was_fallback INTEGER NOT NULL,  -- 0 or 1
    verification_status TEXT NOT NULL,
    receipt_wall_us INTEGER NOT NULL,
    receipt_server_us INTEGER,
    diagnostics_json TEXT           -- JSON array of diagnostic strings
);
```

### 6.3 Store Behavior

- `RecorderStore.open()` for a new path: if approximate data is expected,
  create v2 schema (v1 tables + approximate tables)
- `RecorderStore.open()` for existing path: read `user_version`. If v1 and
  approximate mode requested → raise `RecorderStoreError('schema_version_mismatch', ...)`
  with a clear message that a new v2 database is required
- v1 databases in BOOK_ONLY or REFERENCE_REQUIRED modes: unchanged behavior

## 7. Recorder Integration

### 7.1 RunConfig

```python
@dataclass(frozen=True)
class RunConfig:
    mode: str = "BOOK_ONLY"
    # "BOOK_ONLY", "REFERENCE_REQUIRED", "APPROXIMATE_REFERENCE"
```

### 7.2 Recorder

```python
class Recorder:
    def __init__(self, store, public, *, monotonic_ns, wall_us, sleep,
                 offline_reference=None, approximate_reference=None):
        self.approximate_reference = approximate_reference
        ...
```

- New `ApproximateReferenceWorker` (thread-based, similar to `ReferenceWorker`)
- `approximate_reference` is an `ApproximateReferenceSession` or `None`
- In `_execute_started_run`:
  - `BOOK_ONLY`: no reference worker (existing)
  - `REFERENCE_REQUIRED`: Chainlink worker only (existing)
  - `APPROXIMATE_REFERENCE`: approximate worker only (new)
  - Mixing modes: refuse (e.g., `REFERENCE_REQUIRED` + `approximate_reference`
    not provided → error)
- In `_record`, at each slot:
  - Submit approximate reference fetch alongside books (if approximate worker active)
  - Drain approximate results after book collection
  - Store approximate observations + computed reference report
- `BOOK_ONLY` mode: no approximate worker started (unchanged)
- `REFERENCE_REQUIRED` mode: no approximate worker started (unchanged)
- No approximate source should accidentally start in BOOK_ONLY or exact mode

### 7.3 Metrics

Update `metrics.py`:

- `reference_coverage` dict must distinguish:
  - `live_slots` (existing — Chainlink only)
  - `don_unverified_reports` (existing — Chainlink, UNVERIFIED)
  - `approximate_reference_coverage` (new — separate, clearly labelled)
- `approximate_observations` count per provider
- `approximate_references` count
- `approximate_fallback_count` (Kraken fallback count)
- NEVER aggregate approximate coverage into exact Chainlink coverage

### 7.4 CLI

```bash
uv run python src/record.py record --mode approximate-reference --db data/free_reference.sqlite3
```

- `--mode` choices: `book-only`, `reference-required` (refused at runtime),
  `approximate-reference`
- `approximate-reference`: creates/opens v2 database, starts approximate worker
- Default remains safe: `--mode` is required; no implicit activation

## 8. Test Strategy (TDD, all offline)

### Phase A tests:

1. **Coinbase provider tests** (`test_free_coinbase.py`):
   - Valid payload → correct Decimal price, bid, ask, source_timestamp_us
   - Malformed JSON → error
   - Wrong pair (e.g. ETH-USD) → rejected
   - Non-positive price → rejected
   - Missing fields → error
   - Transport failure (timeout, network error)
   - Oversized response
   - No-look-ahead: future timestamp rejected
   - Raw payload hash computed

2. **Kraken provider tests** (`test_free_kraken.py`):
   - Valid payload (assetVersion=1) → correct price, pair=BTC/USD, source_timestamp_us=None
   - Default assetVersion (XXBTZUSD) → handled or rejected per explicit opt-in
   - Malformed payload → error
   - Wrong pair → rejected
   - Non-positive price → rejected
   - Kraken API error array → rejected
   - No fabricated source_timestamp_us → always None
   - Transport failure cases

3. **Reference engine tests** (`test_free_reference.py`):
   - Both sources valid → Coinbase used, value = mean of Coinbase observations
   - Coinbase missing/stale, Kraken valid → Kraken used, was_fallback=True
   - Both missing → value=None, diagnostics=MISSING_OBSERVATIONS
   - Stale observations excluded from window
   - No look-ahead: future observations rejected
   - Deterministic replay: same inputs → same output
   - Empty window → failure
   - Diagnostics populated correctly
   - Provider provenance preserved

4. **Mode isolation tests** (`test_free_mode_isolation.py`):
   - BOOK_ONLY: no approximate worker, no free-source HTTP requests
   - REFERENCE_REQUIRED: no approximate worker
   - APPROXIMATE_REFERENCE: no Chainlink worker, no credential paths

5. **Schema isolation tests** (`test_free_schema.py`):
   - v1 database + APPROXIMATE_REFERENCE → clear failure
   - New v2 database → approximate tables created
   - v2 database with BOOK_ONLY → works, approximate tables unused
   - Existing CP3 tests still pass (v1 schema unchanged)

6. **Metrics separation tests** (`test_free_metrics.py`):
   - approximate coverage != exact coverage
   - Per-provider observation counts
   - Fallback counts
   - No aggregation across exact/approximate

### Test patterns (matching existing):
- `unittest.TestCase` with `unittest.mock.patch`
- `httpx.MockTransport` for HTTP mocking
- `TemporaryDirectory` for databases under `data/`
- Synthetic clocks (`NOW_US` constants)
- No real network in unit tests

## 9. Risks

1. **API changes**: Coinbase/Kraken public APIs may change format/rate limits
   - Mitigation: versioned parsers (`parser_revision`), fail-closed on unexpected schema

2. **Rate limits**: Free APIs have rate limits; 2s polling may be acceptable
   - Mitigation: bounded requests, single request per slot per provider,
     exponential backoff on 429
     - Mitigation: bounded retries, bounded payloads, bounded timeouts,
       no infinite reconnect loops, explicit backoff policy

3. **Semantic leakage**: Risk that approximate data could be misinterpreted as exact
   - Mitigation: separate tables, `mode = 'APPROXIMATE'` in reference table,
     `verification_status = 'UNVERIFIED'`, clear provenance, never enters
     Chainlink tables

4. **Clock calibration**: Approximate mode has no CLOB clock anchor requirement
   - Mitigation: local UTC wall time + monotonic, server time if clock is usable

5. **Schema complexity**: New tables add v2 complexity
   - Mitigation: keep tables minimal, focused on observability
   - v2 is separate from v1, no in-place migration

6. **Kraken timestamp gap**: Kraken Ticker has no source trade timestamp
   - Mitigation: explicitly record `source_timestamp_us = None`, use receipt
     times for all freshness decisions, document the limitation

## 10. Exact Files/Interfaces Affected

### New files:
- `src/polymarket_bot/recorder/free_reference.py` — provider protocol,
  observation dataclasses, CoinbaseRestProvider, KrakenRestProvider,
  ApproximateReferenceSession, APPROXIMATE_SAMPLE_MEAN_60S engine
- `tests/test_free_coinbase.py` — Coinbase provider tests
- `tests/test_free_kraken.py` — Kraken provider tests
- `tests/test_free_reference.py` — ApproximateReferenceSession + engine tests
- `tests/test_free_mode_isolation.py` — Mode and schema isolation tests
- `tests/test_free_metrics.py` — Metrics separation tests

### Modified files:
- `src/polymarket_bot/recorder/models.py` — Add `APPROXIMATE_REFERENCE` to RunConfig
- `src/polymarket_bot/recorder/service.py` — ApproximateReferenceWorker, mode handling
- `src/polymarket_bot/recorder/store.py` — v2 schema support, approximate table CRUD
- `src/polymarket_bot/recorder/schema.sql` — v2 schema with approximate tables
- `src/polymarket_bot/recorder/metrics.py` — Approximate reference coverage metrics
- `src/polymarket_bot/recorder/cli.py` — `--mode approximate-reference`

### Unchanged files (do NOT touch):
- `src/polymarket_bot/recorder/chainlink_rest.py`
- `src/polymarket_bot/recorder/chainlink_ws.py`
- `src/polymarket_bot/recorder/chainlink_discovery.py`
- `src/polymarket_bot/recorder/chainlink_auth.py`
- `src/polymarket_bot/recorder/clock.py`
- `src/polymarket_bot/recorder/books.py`
- `src/polymarket_bot/recorder/resolution.py`
- `src/polymarket_bot/recorder/schedule.py`
- `src/polymarket_bot/recorder/metadata.py`
- `src/polymarket_bot/recorder/__init__.py`
- All existing tests
- `src/polymarket_bot/market_data/` (http.py, gamma.py, clob.py, etc.)

## 11. Network Safety

Authorized for Phase A:
- `https://api.exchange.coinbase.com/products/BTC-USD/ticker` (public REST)
- `https://api.kraken.com/0/public/Ticker` (public REST)

NOT authorized:
- No authenticated endpoints
- No API keys or credentials
- No HTML scraping
- No paid services
- No Polymarket private endpoints
- No trading, orders, or signing
- No Chainlink credentials

Network policy:
- `trust_env=False` (consistent with existing project policy)
- Bounded timeouts (max 5s per request)
- Bounded payloads (max 1 MiB)
- Bounded retries (max 1 retry per provider per slot)
- No infinite reconnect loops
- Explicit backoff policy on 429

## 12. Self-Review of Design

| Criterion | Status |
|-----------|--------|
| TWAP vs arithmetic sample mean | Fixed: `APPROXIMATE_SAMPLE_MEAN_60S`, explicitly NOT a TWAP |
| Independent recording vs median aggregation | V1: record independently, explicit fallback only, no consensus |
| v1 compatibility vs automatic v2 migration | v2 is new-database only; v1 databases untouched; clear failure if approximate mode pointed at v1 |
| Kraken source timestamp semantics | `source_timestamp_us = None` explicitly; receipt times used for freshness |
| Semantic leakage | Impossible: separate tables, `mode = 'APPROXIMATE'`, `verification_status = 'UNVERIFIED'`, never in Chainlink tables |
| Look-ahead | Rejected: future observations excluded, deterministic windowed mean |
| Hidden fallback | Made explicit: `was_fallback` field, `diagnostics` records reason, metrics track fallback count |
| Unnecessary complexity | Minimal: 3 new tables, 3 new dataclasses, protocol-based providers |
| Chainlink unchanged | Yes: all chainlink_*.py files untouched, all CP3 tests preserved |
