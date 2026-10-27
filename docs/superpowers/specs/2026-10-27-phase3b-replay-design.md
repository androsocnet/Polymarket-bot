# Phase 3B — Offline Historical Replay & Backtesting Design Specification

## Overview

Phase 3B provides a deterministic, immutable offline historical replay and backtesting engine for the Polymarket BTC 15-minute Up/Down paper bot. It operates on real CP5 recorder SQLite databases without network access, live runtimes, or wallet/signing capabilities.

## Architecture

```
                    ┌────────────────────────┐
                    │  CP5 Recorder SQLite   │
                    │ (mode=ro, query_only)  │
                    └───────────┬────────────┘
                                │
                                ▼
                    ┌────────────────────────┐
                    │     ReplayAdapter      │
                    │   (validation & data)  │
                    └───────────┬────────────┘
                                │
               MarketSnapshots, opportunities,
               settlement evidence
                                │
                                ▼
                    ┌────────────────────────┐
                    │      ReplayEngine      │
                    │ (chronological replay) │
                    └───────────┬────────────┘
                                │
                                ▼
                    ┌────────────────────────┐
                    │     BacktestReport     │
                    │  (metrics & hash seal) │
                    └────────────────────────┘
```

### Components

1. **`ReplayAdapter` (`src/polymarket_bot/replay/adapter.py`)**:
   - Opens database read-only (`mode=ro`, `PRAGMA query_only=ON`).
   - Verifies integrity, foreign keys, and clock anchors.
   - Pairs UP and DOWN book observations into `MarketSnapshot` structures.
   - Preserves complete order book depth for realistic liquidity modeling.
   - Reconstructs execution opportunities with exact causal timestamps (`start_ns`, `available_ns`).
   - Loads directional settlement evidence from `resolution_observations` and verifies it using `verify_evidence`.

2. **`ReplayEngine` (`src/polymarket_bot/replay/engine.py`)**:
   - Replays order books chronologically using opportunity availability timestamps.
   - Evaluates momentum strategy decisions using only data revealed up to each slot (no future-data leakage).
   - Simulates fills against opposing book depth with realistic fees, VWAP slippage, and partial fill modeling.
   - Validates risk constraints (exposure limits, cash limits, max positions) and records diagnostics without stopping replay.
   - Applies verified settlement results to open positions at window close.

3. **`BacktestReport` (`src/polymarket_bot/replay/report.py`)**:
   - Records decisions, ledger, portfolio states, realized P&L, fees paid, and unrealized marks.
   - Groups realized P&L by UTC day.
   - Calculates sampled peak-to-trough maximum drawdown.
   - Implements `split_windows` for 2-way (train/test) or 3-way (train/val/test) chronological splitting.
   - Purges overlapping market windows across split boundaries and eliminates settlement-delay leakage.
   - Generates deterministic verification proofs: `config_hash` and `source_fingerprint`.

4. **`variants` (`src/polymarket_bot/replay/variants.py`)**:
   - Defines a prespecified parameter grid based on Phase 3A empirical hypotheses:
     - Entry price caps: `max_entry_price` ∈ {0.5, 0.6, 0.7, 0.75, 0.8}
     - Min time remaining: `min_remaining_seconds` ∈ {30, 60, 120, 300, 600}
     - Holding period variants: short {1, 2} and long {5, 10, 20}
     - Reversal confirmation
     - Cost sensitivity: `fee_bps` ∈ {5, 10, 20, 50}
     - Liquidity sensitivity: `depth_scale` ∈ {0.5, 1.0, 2.0}
   - Evaluates variants strictly on the training partition (`train_only=True`), preserving the untouched holdout test partition.

5. **CLI (`src/polymarket_bot/replay/cli.py`)**:
   - `python -m polymarket_bot.replay.cli --db <path> --run-id <id> --market-id <id> --output-dir <path>`
   - Writes `report.json`, `decisions.jsonl`, `ledger.jsonl`.
   - Never overwrites existing output directories.
   - Produces bit-identical deterministic outputs given identical inputs.

## Read-Only Export Spec (for Hermes Operator)

To perform backtesting on historical data captured on the soak VPS, the operator should extract an immutable copy of the recorder database with the following requirements:

### Required Tables and Fields

1. `recording_runs`: Run metadata (`run_id`, `started_wall_us`, `ended_wall_us`, `config_json`, `state`).
2. `clock_anchors`: Hardware and server clock synchronizations (`anchor_id`, `request_start_mono_ns`, `request_end_mono_ns`, `anchor_mono_ns`, `anchor_server_us`, `uncertainty_us`, `status`).
3. `markets`: Market definitions (`market_id`, `event_id`, `condition_id`, `up_token_id`, `down_token_id`, `window_start_us`, `window_end_us`, `lifecycle`).
4. `market_revisions`: Gamma and CLOB metadata snapshots with compressed payloads (`gamma_raw_z`, `clob_config_raw_z`, `raw_sizes_json`).
5. `sample_slots`: Causal slot schedules and status (`slot_id`, `slot_index`, `start_mono_ns`, `end_mono_ns`, `scheduled_us`, `status`).
6. `request_attempts`: HTTP audit log and raw response payloads (`request_id`, `slot_id`, `start_mono_ns`, `end_mono_ns`, `response_raw_z`, `raw_size_bytes`, `codec_version`).
7. `book_observations`: Parsed order book states and validation flags (`slot_id`, `outcome`, `best_bid_text`, `best_ask_text`, `status`).
8. `resolution_observations`: Settlement evidence payloads (`market_id`, `classification`, `raw_resolution_z`, `payouts_raw_json`).

### Export Procedure

```bash
# On the VPS (as the operator):
sqlite3 /path/to/recorder.sqlite ".backup /path/to/export/recorder_backup_$(date +%Y%m%d_%H%M%S).sqlite"
sha256sum /path/to/export/recorder_backup_*.sqlite > /path/to/export/checksum.sha256
```

Do not export `.env` files, wallet private keys, or VPS credentials. The SQLite file is strictly public order book and resolution market data.
