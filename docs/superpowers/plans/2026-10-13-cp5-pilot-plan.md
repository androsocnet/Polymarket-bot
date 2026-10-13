# CP5 FREE_REFERENCE DATA QUALITY PILOT — Execution Plan

> **For agentic workers:** Uses superpowers:subagent-driven-development or native execution.

**Goal:** Prove the current final FREE_REFERENCE implementation can collect a usable, internally consistent research dataset across at least one full BTC 15-minute market window. RESEARCH-ONLY.

**Architecture:**
- Phase 1 (2-min smoke): CLI `--mode approximate-reference --duration-seconds 120` against a disposable v2 DB.
- Phase 2 (20-min pilot): Custom CP5 driver script that calls `Recorder.run` directly with a 20-minute deadline, since CLI limits duration to 120s.
- Phase 3 (analysis): Read-only SQL analysis of the persisted v2 DB, computing all required metrics.
- Phase 4 (replay): Re-derive APPROXIMATE_SAMPLE_MEAN_60S from persisted observations without network.
- Phase 5 (DB validation): SQLite integrity_check, foreign_key_check, user_version, resource cleanup.

**Tech Stack:** Python 3.12, existing httpx/httpx.MockTransport, SQLite, unittest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-12-free-reference-design.md`

## Global Constraints

- Disposable v2 databases only under `data/`
- Never touch existing research datasets
- No credentials, API keys, secrets, private endpoints, wallet, signing, orders, trading
- Only approved public hosts: `api.exchange.coinbase.com`, `api.kraken.com`, `gamma-api.polymarket.com`, `clob.polymarket.com`
- `trust_env=False`, `auth=None`, `follow_redirects=False`
- Bounded timeouts, no infinite retries, no automatic extension
- Hard external watchdog (shell timeout) for each pilot run

## Tasks

### Phase 0: Baseline Verification (DONE)
- [x] Branch is `feat/cp5-data-quality`
- [x] Existing untracked WIP preserved; `git diff --check` had no output for tracked changes only. Working tree was not clean.
- [x] All 591 tests green
- [x] Design, plan, implementation, tests read
- [x] git diff --check clean

### Phase 1: 2-Minute Smoke Test
- [ ] Verify no credentials/private/trading code paths
- [ ] Run CLI with `--duration-seconds 120` against new v2 DB
- [ ] Collect evidence: elapsed time, request counts, observations, gaps, cleanup
- [ ] PASS/FAIL determination

### Phase 2: 20-Minute Pilot
- [ ] Create CP5 driver script for 20-minute deadline (CLI limited to 120s)
- [ ] Wait up to 15 min for next 15-min market window boundary (max)
- [ ] Run 20-minute bounded pilot with hard watchdog
- [ ] Capture complete 15-minute Polymarket BTC market window + buffer

### Phase 3: Data Quality Analysis
- [ ] Provider availability statistics (Coinbase/Kraken)
- [ ] Sampling gap distributions (min/median/mean/p95/max/count > 4s/> 6s/> 10s)
- [ ] Fallback statistics (events, reasons, durations, percentage)
- [ ] Cross-source price disagreement (USD and bps, min/median/mean/p95/max)
- [ ] APPROXIMATE_SAMPLE_MEAN_60S coverage and provenance
- [ ] Polymarket market/book/window coverage

### Phase 4: Replay/Determinism
- [ ] Re-derive approximate reference from persisted observations (offline)
- [ ] Verify same outputs, no look-ahead, no interpolation

### Phase 5: Database/Lifecycle Validation
- [ ] SQLite integrity_check = ok
- [ ] foreign_key_check = clean
- [ ] schema user_version = 2
- [ ] approximate data only in approximate_* tables
- [ ] no approximate rows in Chainlink exact tables
- [ ] Reopen read-only succeeds
- [ ] Zero active threads after shutdown
- [ ] Zero retained transport/client handles
- [ ] No DB lock remains

### Phase 6: Dataset Usability Decision
- [ ] Evaluate all 9 criteria
- [ ] Classify: PASS / FAIL / BLOCKED

### Final Report
- [ ] Produce CP5 checkpoint report with all sections A–T
