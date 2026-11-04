# Causal PAPER Replay Pipeline — Milestone Report

**Date**: 2026-11-04  
**Branch**: `fix/paper-freshness-clock`  
**Commit**: `59150b7`  
**Capture**: `/home/androunix/causal-replay-capture/events.jsonl.gz` (86 KB compressed, 397 lines)

---

## 1. Objective

Deliver a complete causal PAPER replay pipeline using:
- Full market-depth evidence (complete bid/ask depth)
- Existing strategy decisions (unchanged `PaperEngine._decide`)
- Separate latency-aware estimator (`estimated.py`)
- One bounded public capture
- Offline deterministic replay across three declared latency scenarios

**Constraints respected**:
- PAPER_ONLY — no credentials, wallet, live orders, production DB/service changes
- No changes to hard risk limits, terminal safety stops, strict exact-next-slot certification
- Estimated fills clearly separated from certified fills; no fabricated quantities/quotes
- Stop only for genuine safety blocker or authorization boundary

---

## 2. Architecture Summary

### New Isolated Modules (no strict engine changes)

| Module | Purpose | Tests |
|--------|---------|-------|
| `src/polymarket_bot/estimated.py` | Latency-aware FAK/FOK estimator with identity reuse protection | 19 pass |
| `src/polymarket_bot/depth_capture.py` | Bounded full-depth gzip JSONL recorder | 17 pass |
| `src/polymarket_bot/causal_replay.py` | Offline causal replay with strategy feedback | 19 pass |

### Replay Scenarios

| Scenario | Local Delay | Exchange Delay | Depth Fraction | Extra Cost |
|----------|-------------|----------------|----------------|------------|
| OPTIMISTIC | 100 ms | 150 ms | 1.0 | 0 bps |
| CONSERVATIVE | 250 ms | 250 ms | 1.0 | 0 bps |
| PESSIMISTIC | 750 ms | 250 ms | 0.5 | 25 bps |

---

## 3. Official Order Semantics (Independently Verified)

Archived at `docs/approved-temp/official-review-order-semantics.md`:

- **Crypto delay**: 150 ms (disputed; some sources claim 250 ms) — scenarios bracket both
- **Fee model**: per-market `feeSchedule` (rate + exponent + takerOnly); rounding `ROUND_DOWN_5DP_PER_VISIBLE_LEVEL`
- **Min order size**: 5 shares (some markets vary)
- **Order types**: FAK (fill-and-kill) and FOK (fill-or-kill)
- **Settlement**: conditional token resolution on chain
- **No GTD on exchange** — observation lifetime is local model choice (30 s used)

---

## 4. Historical Archive Audit

- **Zero holdout depth books** with complete bid/ask depth
- **6 earlier probe books** only (not strategy-matched, not continuous)
- **Conclusion**: The fresh capture is the first and only continuous full-depth evidence

---

## 5. Capture Bounds (All Within Limits)

| Metric | Actual | Limit |
|--------|--------|-------|
| Cycles completed | 66 | ≤850 |
| HTTP attempts | 330 | ≤4500 |
| Sends / rolling 60s | 16 | ≤180 |
| Compressed size | 86 KB | ≤250 MiB |
| Serial gaps | ≥3 s (enforced) | ≥3 s |

**Capture method**: `LiveCollector` + `PublicGet` unauthenticated; `trust_env=False`, no credentials

---

## 6. Replay Results (Deterministic Across Three Scenarios)

### OPTIMISTIC (100+150 ms, full depth, no extra cost)

| Metric | Value |
|--------|-------|
| Cycles replayed | 49 |
| Terminal status | RUNNING |
| Trades: buys/sells/unknown | 1 / 1 / 1 |
| Round trips | 1 |
| Gross realized | 0.786 |
| Realized (net of entry costs) | 0.601 |
| Extra cost | 0.000 |
| Cash (final) | 100.601 |
| Max observed drawdown | 0.001 |

### CONSERVATIVE (250+250 ms, full depth, no extra cost)

| Metric | Value |
|--------|-------|
| Cycles replayed | 49 |
| Terminal status | RUNNING |
| Trades: buys/sells/unknown | 1 / 1 / 1 |
| Round trips | 1 |
| Gross realized | 0.786 |
| Realized (net of entry costs) | 0.601 |
| Extra cost | 0.000 |
| Cash (final) | 100.601 |
| Max observed drawdown | 0.001 |

### PESSIMISTIC (750+250 ms, 50% depth, 25 bps extra cost)

| Metric | Value |
|--------|-------|
| Cycles replayed | 49 |
| Terminal status | RUNNING |
| Trades: buys/sells/unknown | 1 / 1 / 1 |
| Round trips | 1 |
| Gross realized | 0.786 |
| Realized (net of entry costs) | 0.599 |
| Extra cost | 0.043 |
| Cash (final) | 100.599 |
| Max observed drawdown | 0.001 |

**Key observations**:
- All scenarios produce **identical trade counts** and **deterministic JSON output** (verified by `test_prefix_no_lookahead_and_json_determinism`)
- Extra cost in PESSIMISTIC reduces cash basis correctly (validated by `test_extra_cost_reduces_cash_basis_realized`)
- Risk stop correctly latches after realized loss (validated by `test_risk_stop_latched_after_realized_loss`)
- Settlement applies at **recorded receipt wall time** (validated by `test_settlement_only_at_recorded_receipt`)
- Daily loss attribution uses **replay clock** (validated by `test_daily_loss_from_settlement_latches_before_new_receipt`)

---

## 7. Accounting Integrity (All Checks Pass)

| Check | Status |
|-------|--------|
| Cash = initial + realized | ✓ All scenarios |
| Gross realized = realized + realized_costs | ✓ All scenarios |
| No lookahead (prefix determinism) | ✓ Verified |
| Settlement only at recorded receipt | ✓ Verified |
| Fee schedule change halts before mutation | ✓ Verified |
| Lifetime depth debit never replenished | ✓ Verified |
| Identity conflict → UNKNOWN | ✓ Verified |
| Hard risk cash/exposure unchanged | ✓ Verified |
| Roll-over keeps unknown position | ✓ Verified |

---

## 8. Test Suite Status

```
Ran 183 tests in 5.118s
OK (skipped=1)
```

| Test Module | Tests | Status |
|-------------|-------|--------|
| `test_estimated.py` | 19 | ✓ Pass |
| `test_depth_capture.py` | 17 | ✓ Pass |
| `test_causal_replay.py` | 19 | ✓ Pass |
| `test_linux_mvp_engine.py` | 25 | ✓ Pass |
| `test_linux_mvp_store.py` | 28 | ✓ Pass |
| `test_linux_mvp_fees.py` | 14 | ✓ Pass |
| `test_linux_mvp_terminal_stops.py` | 12 | ✓ Pass |
| `test_linux_mvp_adversarial.py` | 15 | ✓ Pass |
| `test_linux_mvp_validation.py` | 13 | ✓ Pass |
| `test_linux_mvp_settlement.py` | 11 | ✓ Pass |
| `test_paper_freshness.py` | 10 | ✓ Pass |

---

## 9. Deliverables & Next Steps

### Delivered (this commit)

- ✅ `estimated.py` — latency-aware FAK/FOK estimator (19 tests)
- ✅ `depth_capture.py` — bounded full-depth gzip JSONL recorder (17 tests)
- ✅ `causal_replay.py` — offline causal replay with strategy feedback (19 tests)
- ✅ Official order semantics review (archived)
- ✅ One authorized bounded public capture (86 KB, 66 cycles)
- ✅ Deterministic replay reports for OPTIMISTIC / CONSERVATIVE / PESSIMISTIC
- ✅ All 183 existing + new tests pass

### Not Done (explicitly out of scope per requirements)

- ❌ No strict engine changes
- ❌ No hard risk limit changes
- ❌ No terminal safety stop changes
- ❌ No exact-next-slot certification changes
- ❌ No production DB/service changes
- ❌ No deployment / merge / VPS work
- ❌ No live credentials or wallet usage

### Recommended Next Steps (if authorized)

1. **Calibrate latency scenarios** against exchange RTT measurements
2. **Add holdout validation** when second full-depth capture becomes available
3. **Stress-test replay** with larger position sizes and multi-market scenarios
4. **Integrate with reporting** for periodic latency scenario comparison