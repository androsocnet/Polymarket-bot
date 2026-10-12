# FREE REFERENCE Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an APPROXIMATE_REFERENCE mode to the Polymarket BTC 15-minute recorder that collects free public BTC/USD prices from Coinbase (primary) and Kraken (fallback) into a separate v2 schema, computes APPROXIMATE_SAMPLE_MEAN_60S, and never touches the existing Chainlink exact reference path.

**Architecture:** Provider abstraction (`ApproximateReferenceProvider` protocol) with two REST implementations (Coinbase, Kraken), each returning a fully-provenanced `ApproximateObservation`. Observations feed a deterministic `APPROXIMATE_SAMPLE_MEAN_60S` engine that produces an `ApproximateReferenceReport`. All data lives in new `approximate_*` tables (schema user_version=2), isolated from Chainlink tables. A new `ApproximateReferenceWorker` thread manages lifecycle inside `Recorder`.

**Tech Stack:** Python 3.12+, httpx, unittest, SQLite. No new dependencies (uses existing httpx, Decimal, hashlib).

**Spec:** `docs/superpowers/specs/2026-10-12-free-reference-design.md`

## Global Constraints

- Python >=3.12 (from `pyproject.toml`)
- Dependencies: httpx>=0.28.1, websockets>=17.2 (existing only; no new deps)
- `trust_env=False` on all HTTP clients
- Local database under `data/` only
- No API keys, no authentication, no private endpoints, no trading
- v1 databases must remain untouched; v2 only for approximate mode
- Existing CP3 tests (`tests/test_cp3_*.py`, `tests/test_chainlink_*.py`, `tests/test_schema_v2_decoder.py`) must stay green
- Test command: `PYTHONPATH=src python -m unittest discover -s tests -t . -v`

## Review Focus

1. **Semantic leakage**: Does any approximate code write to or read from `reference_reports`, `reference_feeds`, `reference_associations`, or `reference_fetches`? Tests must assert approximate data never appears in Chainlink tables.
2. **Look-ahead / future observations**: Are observations with receipt time > window end rejected? Tests must cover future-timestamp rejection.
3. **Kraken source_timestamp_us = None**: Is the timestamp explicitly None and never fabricated? Tests must assert this invariant.
4. **Mode isolation**: Does BOOK_ONLY with no `approximate_reference` argument make zero free-source HTTP requests? Tests must assert zero requests.
5. **v1 database protection**: Is pointing APPROXIMATE_REFERENCE at a v1 database a hard failure with no writes? Tests must assert no mutation attempts succeed.

---

## Task 1: v2 Schema Support

**Files:**
- Modify: `src/polymarket_bot/recorder/schema.sql` — add approximate tables, keep v1 tables, bump user_version to 2
- Modify: `src/polymarket_bot/recorder/store.py` — handle v2 creation, v1→v2 rejection
- Test: `tests/test_free_schema.py`

**Interfaces:**
- `RecorderStore.open(path)` — creates v2 if new; raises `RecorderStoreError('schema_version_mismatch', ...)` if existing is v1 and approximate requested
- `RecorderStore.read_only(path)` — opens existing, reads user_version
- `RecorderStore.save_approximate_observation(observation, run_id)` — inserts into approximate_observations
- `RecorderStore.save_approximate_reference(report)` — inserts into approximate_references

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_free_schema.py
class FreeSchemaTests(unittest.TestCase):
    def test_v1_database_rejects_approximate_mode(self):
        # Create v1 database (BOOK_ONLY), then try to open for approximate
        # Expect RecorderStoreError with schema_version_mismatch
        pass

    def test_new_v2_database_creates_approximate_tables(self):
        # Open new database in approximate mode, verify tables exist
        store = RecorderStore.open_approximate(path)
        assert store._rows("SELECT name FROM sqlite_master WHERE name='approximate_observations'")
        assert store._rows("SELECT name FROM sqlite_master WHERE name='approximate_references'")
        assert store._rows("SELECT name FROM sqlite_master WHERE name='approximate_feeds'")

    def test_v2_database_supports_book_only(self):
        # v2 must also support BOOK_ONLY mode (it includes all v1 tables)
        pass

    def test_existing_v1_book_only_unaffected(self):
        # Open v1 database, verify it still works for BOOK_ONLY
        # user_version must remain 1
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python -m unittest tests.test_free_schema -v`
Expected: FAIL — `RecorderStore.open_approximate` not defined

- [ ] **Step 3: Add approximate tables to schema.sql and v2 support to store.py**

Schema additions:
- Bump `user_version` to 2 (in `RecorderStore.open` when creating new)
- All v1 tables unchanged (exact same text)
- New tables: `approximate_feeds`, `approximate_observations`, `approximate_references`
- `RecorderStore._verify_existing`: if user_version==1 and approximate requested → raise
- `RecorderStore.open_approximate(path)`: alias that creates v2 schema

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python -m unittest tests.test_free_schema -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_free_schema.py src/polymarket_bot/recorder/schema.sql src/polymarket_bot/recorder/store.py
git commit -m "feat: add v2 schema with approximate reference tables"
```

---

## Task 2: Approximate Observation Model and Provider Protocol

**Files:**
- Create: `src/polymarket_bot/recorder/free_reference.py` (protocol + dataclasses only)
- Test: `tests/test_free_reference.py` (model tests)

**Interfaces:**
- `ApproximateReferenceProvider` (Protocol): `provider_name`, `pair`, `quote_currency` properties, `fetch_ticker(*, deadline: float) -> ApproximateObservation`
- `ApproximateObservation` (frozen dataclass): provider, pair, quote_currency, price (Decimal), bid_price (Decimal|None), ask_price (Decimal|None), source_timestamp_us (int|None), receipt_wall_us (int), receipt_server_us (int|None), raw_payload_hash (str), raw_payload_size (int), transport_type (str), parser_revision (str), http_status (int|None), result (str), error_code (str|None)
- `ApproximateReferenceReport` (frozen dataclass): mode (str='APPROXIMATE'), reference_kind (str='APPROXIMATE_SAMPLE_MEAN_60S'), value (Decimal|None), window_start_us (int), window_end_us (int), observation_count (int), source_provider (str), was_fallback (bool), verification_status (str='UNVERIFIED'), receipt_wall_us (int), receipt_server_us (int|None), diagnostics (tuple[str,...])

- [ ] **Step 1: Write the failing tests**

```python
def test_observation_is_frozen_dataclass():
    obs = ApproximateObservation(provider='coinbase', pair='BTC/USD', quote_currency='USD',
        price=Decimal('50000'), bid_price=Decimal('49999'), ask_price=Decimal('50001'),
        source_timestamp_us=123456789, receipt_wall_us=999, receipt_server_us=999,
        raw_payload_hash='abc', raw_payload_size=100, transport_type='rest',
        parser_revision='v1', http_status=200, result='success', error_code=None)
    with pytest.raises(FrozenInstanceError):
        obs.price = Decimal('1')

def test_report_mode_hardcoded_approximate():
    report = ApproximateReferenceReport(reference_kind='APPROXIMATE_SAMPLE_MEAN_60S',
        value=Decimal('50000'), window_start_us=0, window_end_us=60000000,
        observation_count=30, source_provider='coinbase', was_fallback=False,
        receipt_wall_us=60000000, receipt_server_us=60000000)
    assert report.mode == 'APPROXIMATE'
    assert report.verification_status == 'UNVERIFIED'

def test_protocol_is_runtime_checkable():
    from typing import runtime_checkable
    assert runtime_checkable(ApproximateReferenceProvider)
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL — classes not defined

- [ ] **Step 3: Implement protocol and dataclasses**

- [ ] **Step 4: Run tests — expect PASS**

`PYTHONPATH=src python -m unittest tests.test_free_reference.FreeModelTests -v`

- [ ] **Step 5: Commit**

---

## Task 3: Coinbase Provider

**Files:**
- Modify: `src/polymarket_bot/recorder/free_reference.py` (add `CoinbaseRestProvider`)
- Test: `tests/test_free_coinbase.py`

**Interfaces:**
- `CoinbaseRestProvider(client: httpx.Client, *, monotonic_ns, wall_us)` implements `ApproximateReferenceProvider`
- Properties: `provider_name='coinbase'`, `pair='BTC/USD'`, `quote_currency='USD'`
- `fetch_ticker(*, deadline: float) -> ApproximateObservation`: GET `/products/BTC-USD/ticker`, 5s timeout, 1 MiB max, trust_env=False
- Parses: `price` (string → Decimal), `bid`, `ask`, `time` (ISO 8601 → epoch_us)
- `parser_revision = 'coinbase-ticker-v1'`
- Fail-closed: wrong product, non-positive price, malformed time, HTTP errors, timeout

- [ ] **Step 1: Write failing tests**

```python
class CoinbaseProviderTests(unittest.TestCase):
    def test_valid_ticker(self): # price, bid, ask, source_timestamp_us from 'time'
    def test_wrong_pair_rejected(self): # ETH-USD response → rejected
    def test_nonpositive_price_rejected(self)
    def test_malformed_json_rejected(self)
    def test_transport_timeout_error_code(self)
    def test_oversized_response_rejected(self)
    def test_no_lookahead_future_timestamp(self): # receipt time < source time → future_source_time error
    def test_missing_price_field_rejected(self)
    def test_raw_payload_hash_computed(self)
    def test_trust_env_false(self): # no proxy/creds
```

- [ ] **Step 2: Run tests — expect FAIL**

- [ ] **Step 3: Implement CoinbaseRestProvider**

- [ ] **Step 4: Run tests — expect PASS**

`PYTHONPATH=src python -m unittest tests.test_free_coinbase -v`

- [ ] **Step 5: Commit**

---

## Task 4: Kraken Provider

**Files:**
- Modify: `src/polymarket_bot/recorder/free_reference.py` (add `KrakenRestProvider`)
- Test: `tests/test_free_kraken.py`

**Interfaces:**
- `KrakenRestProvider(client: httpx.Client, *, monotonic_ns, wall_us)` implements `ApproximateReferenceProvider`
- Properties: `provider_name='kraken'`, `pair='BTC/USD'`, `quote_currency='USD'`
- `fetch_ticker(*, deadline: float) -> ApproximateObservation`: GET `/0/public/Ticker?pair=XBTUSD&assetVersion=1`
- Parses: last trade price from `result.XBTC...c[0]` or `result.BTC/USD.c[0]`
- `source_timestamp_us = None` — **explicitly, always**
- `parser_revision = 'kraken-ticker-v1'`
- `pair` field extracted from result key (must be `BTC/USD` when assetVersion=1)
- Fail-closed: wrong pair, Kraken error array non-empty, non-positive price, malformed

- [ ] **Step 1: Write failing tests**

```python
class KrakenProviderTests(unittest.TestCase):
    def test_valid_ticker(self): # price from 'c' field, source_timestamp_us=None
    def test_default_assetversion_rejected(self): # XXBTZUSD keys must fail unless assetVersion=1
    def test_kraken_error_array_rejected(self)
    def test_wrong_pair_rejected(self)
    def test_nonpositive_price_rejected(self)
    def test_transport_timeout(self)
    def test_source_timestamp_always_none(self): # explicit invariant
    def test_pair_from_result_key(self): # BTC/USD with assetVersion=1
```

- [ ] **Step 2: Run tests — expect FAIL**

- [ ] **Step 3: Implement KrakenRestProvider**

- [ ] **Step 4: Run tests — expect PASS**

`PYTHONPATH=src python -m unittest tests.test_free_kraken -v`

- [ ] **Step 5: Commit**

---

## Task 5: ApproximateReferenceSession and APPROXIMATE_SAMPLE_MEAN_60S Engine

**Files:**
- Modify: `src/polymarket_bot/recorder/free_reference.py` (add session + engine)
- Test: `tests/test_free_reference.py` (engine tests)

**Interfaces:**
- `ApproximateReferenceSession(client, *, providers, monotonic_ns, wall_us, sleep)`
  - `providers`: dict of `provider_name → ApproximateReferenceProvider`
  - `start()`, `stop()` lifecycle (no WebSocket, REST-only Phase A)
  - `check_health()` → no-op for REST
  - `fetch(*, run_id, anchor_id, project) -> tuple[ApproximateReferenceReport, tuple[ApproximateObservation, ...]]`
    - Fetches Coinbase (primary)
    - If Coinbase fails/stale → fetch Kraken (fallback)
    - Computes `APPROXIMATE_SAMPLE_MEAN_60S` from observation window
    - Returns report + observation tuple

- `compute_approximate_sample_mean_60s(observations, *, receipt_server_us, receipt_wall_us) -> ApproximateReferenceReport`
  - Window: `receipt_time - 60_000_000` to `receipt_time`
  - If `source_timestamp_us` present and > receipt time → reject (future observation)
  - Exclude observations older than 60s window
  - Arithmetic mean of `price` fields
  - `value=None` + diagnostics if no observations
  - `was_fallback=True` if Kraken was used (set by session)
  - `source_provider` = name of provider used
  - `verification_status = 'UNVERIFIED'`

- [ ] **Step 1: Write failing tests**

```python
class SampleMeanEngineTests(unittest.TestCase):
    def test_both_sources_valid_coinbase_selected(self)
    def test_coinbase_stale_kraken_fallback(self): # was_fallback=True, diagnostics='FALLBACK_COINBASE_STALE'
    def test_both_missing_value_none(self): # diagnostics='MISSING_OBSERVATIONS'
    def test_future_observation_rejected(self): # source_timestamp > receipt → excluded
    def test_stale_observation_excluded(self)
    def test_deterministic_same_inputs_same_output(self)
    def test_arithmetic_mean_correct(self)
    def test_kraken_source_timestamp_none_handled(self)

class ApproximateReferenceSessionTests(unittest.TestCase):
    def test_fetch_returns_report_and_observations(self)
    def test_coinbase_primary(self)
    def test_kraken_fallback_when_coinbase_fails(self)
    def test_both_fail(self)
    def test_lifecycle_start_stop(self)
    def test_check_health_noop(self)
```

- [ ] **Step 2: Run tests — expect FAIL**

- [ ] **Step 3: Implement session and engine**

- [ ] **Step 4: Run tests — expect PASS**

`PYTHONPATH=src python -m unittest tests.test_free_reference.FreeEngineTests tests.test_free_reference.SessionTests -v`

- [ ] **Step 5: Commit**

---

## Task 6: Persistence Layer for Approximate Evidence

**Files:**
- Modify: `src/polymarket_bot/recorder/store.py` (add approximate save methods)
- Test: `tests/test_free_schema.py` (persistence tests)

**Interfaces:**
- `RecorderStore.save_approximate_observation(run_id, observation)` → observation_id
- `RecorderStore.save_approximate_reference(run_id, report)` → reference_id
- `RecorderStore.start_run()` → handles APPROXIMATE_REFERENCE mode (mode check, v2 requirement)
- `RecorderStore` must reject save if v1 database

- [ ] **Step 1: Write failing tests**

```python
def test_save_approximate_observation(self):
    obs = ...  # construct valid observation
    obs_id = store.save_approximate_observation(run_id, obs)
    assert obs_id > 0
    row = store._one("SELECT * FROM approximate_observations WHERE observation_id=?", (obs_id,))
    assert row['provider'] == 'coinbase'
    assert row['price_text'] == '50000'

def test_save_approximate_reference(self):
    report = ...
    ref_id = store.save_approximate_reference(run_id, report)
    row = store._one("SELECT * FROM approximate_references WHERE reference_id=?", (ref_id,))
    assert row['mode'] == 'APPROXIMATE'
    assert row['verification_status'] == 'UNVERIFIED'

def test_v1_database_rejects_approximate_save(self):
    # Open v1 database, try save_approximate → error
```

- [ ] **Step 2: Run tests — expect FAIL**

- [ ] **Step 3: Implement persistence methods**

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

---

## Task 7: ApproximateReferenceWorker and Recorder Integration

**Files:**
- Modify: `src/polymarket_bot/recorder/models.py` (add APPROXIMATE_REFERENCE to RunConfig)
- Modify: `src/polymarket_bot/recorder/service.py` (add worker, mode handling)
- Test: `tests/test_free_mode_isolation.py`

**Interfaces:**
- `RunConfig.mode` gains value `"APPROXIMATE_REFERENCE"`
- `ApproximateReferenceWorker` (mirrors `ReferenceWorker` pattern)
- `Recorder.__init__` gains `approximate_reference=None` param
- `Recorder._execute_started_run` handles APPROXIMATE_REFERENCE mode
- `Recorder._record` submits approximate fetch in each slot, drains after books

- [ ] **Step 1: Write failing tests**

```python
class ModeIsolationTests(unittest.TestCase):
    def test_book_only_no_approximate_worker(self): # BOOK_ONLY + no approximate_reference → zero free-source requests
    def test_reference_required_no_approximate_worker(self): # REFERENCE_REQUIRED → zero free-source requests
    def test_approximate_mode_starts_worker(self): # APPROXIMATE_REFERENCE → worker active
    def test_approximate_mode_rejects_if_no_session(self): # APPROXIMATE_REFERENCE without approximate_reference → error
    def test_mixed_modes_rejected(self): # REFERENCE_REQUIRED + approximate_reference → error
    def test_approximate_mode_requires_v2(self):
```

- [ ] **Step 2: Run tests — expect FAIL**

- [ ] **Step 3: Implement worker + recorder changes**

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

---

## Task 8: Metrics Separation

**Files:**
- Modify: `src/polymarket_bot/recorder/metrics.py` (add approximate coverage)
- Test: `tests/test_free_metrics.py`

**Interfaces:**
- `MeasurementReport` gains fields: `approximate_coverage`, `approximate_observations_by_provider`, `approximate_fallback_count`
- `_references()` in metrics must NOT include approximate data
- New `_approximate_coverage()` function reads `approximate_*` tables only

- [ ] **Step 1: Write failing tests**

```python
class FreeMetricsTests(unittest.TestCase):
    def test_approximate_coverage_separate_from_exact(self):
        # Insert approximate data, verify it doesn't affect Chainlink coverage count
    def test_approximate_observations_by_provider(self):
    def test_approximate_fallback_count(self):
    def test_v1_database_approximate_metrics_none(self):
```

- [ ] **Step 2: Run tests — expect FAIL**

- [ ] **Step 3: Implement metrics changes**

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

---

## Task 9: CLI/Config

**Files:**
- Modify: `src/polymarket_bot/recorder/cli.py` (add approximate-reference mode)
- Test: `tests/test_free_cli.py`

**Interfaces:**
- `--mode` gains choice: `approximate-reference`
- `approximate-reference` mode: opens v2 database, creates `ApproximateReferenceSession` with Coinbase + Kraken providers, runs recorder

- [ ] **Step 1: Write failing tests**

```python
class FreeCliTests(unittest.TestCase):
    def test_approximate_mode_choice(self): # parser accepts approximate-reference
    def test_approximate_mode_starts_session(self):
    def test_book_only_cli_unchanged(self):
    def test_approximate_mode_requires_v2(self):
```

- [ ] **Step 2: Run tests — expect FAIL**

- [ ] **Step 3: Implement CLI changes**

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

---

## Task 10: Full Offline Verification

**Files:**
- No new files
- Run: all tests

- [ ] **Verify**: existing CP3 tests pass
- [ ] **Verify**: all free reference tests pass
- [ ] **Verify**: no approximate writes in Chainlink tables
- [ ] **Verify**: Pyright on new files

Run: `PYTHONPATH=src python -m unittest discover -s tests -t . -v`
Expected: ALL PASS
