# Phase 3B — Offline Historical Replay/Backtesting Scaffold

## Objective

Build a minimal, immutable offline replay system that:
1. Reads CP5 recorder evidence (schema v1) and reconstructs MarketSnapshot sequences
2. Replays the exact baseline strategy (PaperEngine logic) causally with executable liquidity, partial fills, fees, slippage
3. Uses real historical settlement evidence when available; labels simulated trades as hypothetical
4. Produces a backtest report with P&L, drawdown, train/validation/test splits, leakage prevention, determinism proofs
5. Evaluates prespecified parameter variants (no ML, no production changes)

## Constraints (from AGENTS.md)

- Offline, immutable; reject stale/missing/invalid/one-sided books
- Model executable liquidity, partial fills, realistic fees
- Use real historical settlement evidence when available
- Label simulated trades as hypothetical, not historical executions
- Evaluate baseline with: entry/exit, bid/ask spread, executable prices, fees, slippage, partial fills, exposure, settlement P&L, net profit, max drawdown, risk limits
- Out-of-sample: chronological train/validation/test splits, purge overlapping 15-minute markets, settlement-delay leakage prevention, fixed baseline comparison, reproducible seeds/config, untouched final holdout
- Strategy experiments: max entry price, min time remaining, holding-period exits, reversal exits, cost/liquidity sensitivity
- TDD (RED→GREEN→REFACTOR); tests for: no future-data leakage, chronological replay, fees/P&L reconciliation, partial fills, missing/stale books, settlement timing, risk constraints, deterministic reproduction, data completeness failures
- Inspect available local evidence first; if missing, use clearly labeled synthetic fixtures
- Do not claim profitable backtest results
- Identify exact historical files needed from Hermes
- Provide read-only export spec
- Commit Phase 3B changes only, push origin/feat/phase3b-replay, verify local and remote SHA match
- DO NOT: merge, force-push, modify live/paper runtime, change hard risk limits, connect wallets/signing keys, place real/paper market orders, access VPS or disrupt soak, build autonomous AI, add unnecessary libraries

## Architecture

### 1. ReplayAdapter (`src/polymarket_bot/replay/adapter.py`)

Reads CP5 recorder database (read-only, `mode=ro`, `query_only=ON`) and produces validated `MarketSnapshot` sequences.

**Input**: Recorder DB path, run_id, market_id
**Output**: Tuple of (validated_snapshots, excluded_slots, opportunities, metadata)

**Validation rules** (reusing `paper.py:load_recording` logic):
- Only `VALID` clock anchors
- Only `COMPLETE` slots with `VALID` observations
- Identity checks: token_id, condition_id match market revision
- CLOB config: condition_id, token_ids match
- Book parsing via `parse_book` (full depth preserved)
- Source timestamp verification against observation record
- Receipt clock verification against anchor
- Market window bounds check
- Stale detection: `max_age_us` against source and receipt
- One-sided book rejection (both bid and ask required)

**Rejected categories** (tracked in `excluded`):
- `NO_OBSERVATIONS` — slot has no book observations
- `MISSING_OBSERVATION` — COMPLETE slot missing one outcome
- `INVALID_EXECUTION_BOOK` — observation failed validation
- `MISSING_EXECUTION_BOOK` — no observation for outcome
- `UNSAFE_MISSING_SLOT_TIMING` — missing slot with unsafe causal clock

**Opportunities** (for execution):
- Each outcome at each slot with `start_ns` (decision availability) and `available_ns` (execution availability)
- `reason` = None if valid, else rejection reason
- Include `available_ns` from slot `end_mono_ns` for strategy timing

### 2. ReplayEngine (`src/polymarket_bot/replay/engine.py`)

Deterministic chronological replay over validated snapshots. Subclasses/adapts `PaperEngine` logic but runs offline.

**Key differences from PaperEngine**:
- No collector injection; iterates pre-validated snapshots chronologically
- No wall-clock time; uses snapshot `available_ns` as decision time
- No live risk gates (daily loss limit, consecutive errors) — replay reports what would happen
- Settlement: uses `verify_evidence` from recorder evidence when window expires
- Positions marked to market at each step using fresh best_bid

**State machine per slot**:
1. Observe: record both outcomes' books (bid, ask, mid, source_at, received_us)
2. Execute pending: if pending order exists and this slot > decision_slot, execute against fresh book
3. Check risk: exposure, cash, position limits (report only, don't stop)
4. Decide: if fresh paired books and no pending, run momentum strategy
5. Queue order: if BUY/SELL decision, queue for next slot execution
6. Mark: update unrealized P&L using fresh best_bid

**Configuration** (baseline, matching `Settings` defaults):
```python
initial_cash = Decimal('100')
max_order = Decimal('10')
max_exposure = Decimal('10')
fee_bps = Decimal('10')  # ASSUMED_NOT_VERIFIED
threshold = Decimal('0.02')
lookback = 3
holding_period = 3  # slots
delay_us = 0
max_wait_us = 5_000_000
max_age_us = 5_000_000
max_trades_per_window = 6
max_daily_loss = Decimal('10')
sample_seconds = 2  # slot spacing
```

**Output per step**: Decision record, execution result, position state, equity sample

### 3. BacktestReport (`src/polymarket_bot/replay/report.py`)

Builds final report from ReplayEngine output.

**Metrics**:
- `counts`: requested, filled, partial, rejected
- `cash`, `fees_paid`, `positions` (quantity, cost_basis, unrealized_pnl, mark_evidence)
- `realized_pnl`, `unrealized_pnl`, `total_pnl`
- `settlement`: per-window RESOLVED/UNKNOWN_SETTLEMENT with payout, realized_pnl
- `max_drawdown`: sampled peak-to-trough on `cash + exposure + unrealized` (only complete marks)
- `daily_pnl`: UTC day buckets from SELL updated_us / settlement settled_us
- `exit_reason_breakdown`: HOLDING_PERIOD, REVERSAL, SETTLEMENT, PARTIAL_BOOK, etc.
- `diagnostics`: decision reasons, order statuses, event kinds, execution delays, missing quotes

**Chronological splits** (no leakage):
- Input: `train_end_us`, `val_end_us`, `test_end_us` (UTC epoch microseconds)
- Purging: exclude any market window overlapping split boundaries
- Settlement delay: windows with `resolved_us > split_boundary` excluded from earlier split
- Fixed baseline: same strategy config across all splits
- Holdout: test split never examined during parameter selection

**Determinism proof**:
- Report includes `config_hash` (SHA256 of sorted config JSON)
- Report includes `source_fingerprint` (SHA256 of canonical recorder rows used)
- Same inputs → byte-identical report JSON

### 4. Strategy Variants (`src/polymarket_bot/replay/variants.py`)

Prespecified parameter grid (from Phase 3A hypotheses):

| Variant | Parameters | Hypothesis |
|---------|------------|------------|
| baseline | (default) | Reference |
| entry_price_cap | max_entry_price ∈ {0.5, 0.6, 0.7, 0.75, 0.8} | Late/high-price entries lack room |
| min_remaining | min_remaining_seconds ∈ {30, 60, 120, 300, 600} | Late entries lack time to cover costs |
| hold_short | holding_period ∈ {1, 2} | Forced exits pay spread too often |
| hold_long | holding_period ∈ {5, 10, 20} | Holding longer may capture resolution |
| reversal_confirm | require_reversal_confirmation=True | Reversal may be noise |
| fee_sensitivity | fee_bps ∈ {5, 10, 20, 50} | Cost sensitivity |
| liquidity_sensitivity | depth_scale ∈ {0.5, 1.0, 2.0} | Depth sensitivity (multiply book sizes) |

**Execution**: Each variant runs full replay on train split only. Test split frozen until final evaluation.

### 5. Synthetic Fixtures (`tests/replay_fixtures.py`)

For TDD when no historical DB exists:
- Minimal CP5 schema with known-good books (paired, fresh, valid)
- Stale book scenarios (source age > max_age_us)
- One-sided book scenarios (missing bid or ask)
- Missing slot scenarios (gap in slot sequence)
- Invalid observation scenarios (status != VALID)
- Settlement evidence fixtures (RESOLVED_UP, RESOLVED_DOWN, UNKNOWN_SETTLEMENT)
- Clock anchor scenarios (VALID, DISCONTINUITY, causal ordering)

### 6. Tests (`tests/test_replay_*.py`)

| Test | Property |
|------|----------|
| `test_no_future_data_leakage` | Decisions at slot N never use data from slot > N |
| `test_chronological_replay` | Snapshots processed in available_ns order |
| `test_fees_pnl_reconciliation` | cash + basis = initial + realized (within tolerance) |
| `test_partial_fills` | PARTIAL fills consume depth, remainder abandoned |
| `test_missing_stale_books_rejected` | STALE_PAIR, MISSING_EXECUTION_BOOK, ONE_SIDED_BOOK |
| `test_settlement_timing` | Settlement only at window end with verified evidence |
| `test_risk_constraints_reported` | Exposure, cash, position limits checked but don't stop replay |
| `test_deterministic_reproduction` | Same inputs → identical report (byte-for-byte) |
| `test_data_completeness_failures` | Missing tables, malformed values fail fast |
| `test_split_purging` | Overlapping windows excluded from splits |
| `test_settlement_delay_leakage` | Resolved after split boundary → excluded from earlier split |

## Implementation Order (TDD)

1. **RED**: Write failing tests for ReplayAdapter (adapter tests)
2. **GREEN**: Implement ReplayAdapter
3. **RED**: Write failing tests for ReplayEngine (engine tests)
4. **GREEN**: Implement ReplayEngine
5. **RED**: Write failing tests for BacktestReport (report tests)
6. **GREEN**: Implement BacktestReport
7. **RED**: Write failing tests for variants (variant tests)
8. **GREEN**: Implement variants
9. **REFACTOR**: Consolidate, deduplicate, add CLI entry point
10. **Integration**: Full replay pipeline test with synthetic fixture

## Files to Create

```
src/polymarket_bot/replay/
├── __init__.py
├── adapter.py          # ReplayAdapter: CP5 evidence → MarketSnapshot
├── engine.py           # ReplayEngine: chronological replay loop
├── report.py           # BacktestReport: metrics, splits, determinism
├── variants.py         # Strategy variant definitions
└── cli.py              # python -m polymarket_bot.replay ...

tests/
├── replay_fixtures.py  # Synthetic CP5 DB builder
├── test_replay_adapter.py
├── test_replay_engine.py
├── test_replay_report.py
├── test_replay_variants.py
└── test_replay_integration.py

docs/superpowers/specs/
└── 2026-10-27-phase3b-replay-design.md  # This plan as design spec
```

## Read-Only Export Spec (for Hermes Data Request)

Operator must provide an offline consistent SQLite backup containing:

**Required tables** (from recorder schema v1):
- `recording_runs` (run_id, started_wall_us, ended_wall_us, mode, config_json, state)
- `clock_anchors` (all columns)
- `markets` (all columns)
- `market_revisions` (all columns including gamma_raw_z, clob_config_raw_z)
- `sample_slots` (all columns)
- `request_attempts` (all columns including response_raw_z)
- `book_observations` (all columns)
- `resolution_observations` (all columns including raw_resolution_z)
- `reference_fetches`, `reference_reports`, `reference_associations` (for reference price context)

**Required metadata**:
- Bot ID used in paper trading
- Exact engine commit hash and config history (MVP_BOT_ID, MVP_* env vars)
- Data time range (first/last window_start_us)
- Known outages/gaps
- Source database SHA256 checksum

**Format**: Single `.sqlite3` file, WAL checkpointed (or `-wal` file included), copied via `sqlite3 backup` or filesystem copy of closed DB. No credentials, wallet material, or environment files.

## Verification Checklist

- [ ] All tests pass with `PYTHONPATH=src uv run --offline --locked python -m unittest tests.test_replay_* -v`
- [ ] Baseline replay reproduces PaperEngine behavior on same inputs
- [ ] Synthetic fixtures cover all rejection/execution paths
- [ ] Report includes all required metrics and determinism proofs
- [ ] Variants run on train split only; test split untouched
- [ ] No new dependencies added (pyproject.toml unchanged)
- [ ] No live runtime code modified
- [ ] Commit only Phase 3B files to feat/phase3b-replay
- [ ] Push origin/feat/phase3b-replay, verify SHA match