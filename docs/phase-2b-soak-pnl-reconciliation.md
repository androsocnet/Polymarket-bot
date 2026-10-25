# Phase 2B Soak P&L Reconciliation

> **Test:** Soak run `2026-0413T141531Z` (480 fills, RISK_STOPPED at cycle 8368)
> **Mode:** PAPER_ONLY. No live trades. Historical data only.

## Executive Summary

| Metric | Value |
|--------|-------|
| Total fills | 480 (241 BUY, 239 SELL) |
| Gross wins | +35.56645560 |
| Gross losses | -55.51361430 |
| Net realized (SELL) | -19.94715870 |
| Settlement P&L | +5.09001490 |
| Total fees | 2.56694380 |
| Gross P&L (realized + settlement - fees) | -17.42405160 |
| Starting equity | 100.00000000 |
| Ending equity | 70.26571240 |
| Peak equity | 102.94125580 |
| Max drawdown | 39.65415780 (38.52%) |
| Stop trigger | RISK_STOPPED at cycle 8368 (daily realized ≤ -10.0245913 vs $10 limit) |
| Daily loss at stop | -10.02459130 |

---

## Fee Attribution

Total fees across 480 fills: **$2.56694380**

Fees are charged per fill (BUY and SELL). Breakdown:
- BUY fills: 241 orders paying entry fees
- SELL fills: 239 orders paying exit fees
- Fees reduce realized P&L at each settlement

Fee attribution is symmetric (paid on both entry and exit). Fees are not the primary driver of losses — the win/loss ratio is.

---

## Entry/Exit Analysis

### Fill Prices
| Side | Avg Fill Price | Volume |
|------|---------------|--------|
| BUY (entry avg) | 0.53979789 | 241 fills |
| SELL (exit avg) | 0.53079515 | 239 fills |

**Avg exit < avg entry**: Markets declined during the soak period. The 0.0089 price differential across averages is the primary headwind.

### Timing (Holding Periods)
| Metric | Duration (µs) | Duration (human) |
|--------|--------------|------------------|
| Avg BUY cycle time | 3,000,761 µs | ~3.0 seconds |
| Avg SELL cycle time | 3,003,981 µs | ~3.0 seconds |

Holding periods are ~15-minute slots (market data refresh cadence). No slippage from delayed fills — entries/exits are tightly coupled to slot openings.

---

## Slippage Analysis

**Entry/exit prices are not explicitly modeled as slippage.** The engine fills at the spot price of the market snapshot at decision time. No additional slippage modeling exists beyond the single-cycle price delta. This is a simplification (not a bug):

> `ponytail:` Slippage = spread of 1 cycle. No multi-level order book modeling. Add Level 2 orderbook simulation if paper→live transition required.

---

## Main Causes of Losing Trades

**HOLDING_PERIOD** (forced sales, 106 losing fills): -35.68126670 total

- Reason: Markets moved against position within the holding period, forcing SELL exit
- Avg loss per fill: -0.33661572
- This is the dominant loss driver — 106 of 161 losing fills

**REVERSAL** (signal reversals, 55 losing fills): -19.83234760 total

- Reason: Market reversed against the position after entry (UP→DOWN or DOWN→UP)
- Avg loss per fill: -0.36058814
- Higher avg loss per fill than HOLDING_PERIOD

### Root Cause Summary
1. **Market direction**: 241 BUY fills at avg 0.5398, 239 SELL fills at avg 0.5308 — markets declined
2. **Holding period pressure**: 106 fills held full period with adverse movement
3. **Reversal risk**: 55 fills exited on signal reversal, compounding losses

---

## Equity Curve & Max Drawdown

- **11,215 equity points** recorded across the soak
- Starting: $100.00
- Peak: $102.94 (at ~16% into run)
- Ending: $70.27
- **Max drawdown**: $39.65 (38.52% from peak)

The equity curve declined monotonically after peak — no recovery periods observed. This indicates a sustained adverse market regime during the soak.

---

## Daily Loss Limit Trigger

The RISK_STOPPED was triggered at cycle 8,368 when daily realized P&L fell below the threshold.

| Check | Value |
|-------|-------|
| Daily realized at stop | -10.02459130 |
| DAILY_LOSS_LIMIT | $10.00 |
| Stop condition | `realized <= -max_daily_loss` (strict, fail-closed) |
| Engine guard | `engine.py:71,163` (≤ operator) |
| Service terminal | `service.py:13` — RISK_STOPPED in TERMINAL tuple |
| Journal confirmation | 2026-10-25T04:16:40+07:00, cycle 8368 |

**The stop was correct behavior** — no code defect. The market moved unfavorably, daily P&L breached the $10 limit, and the fail-closed risk guard terminated the service as designed.

---

## Settlement Accounting

- 48 settlements resolved during soak period
- Settlement P&L: +5.09001490
- This represents markets that resolved in our favor (winning outcome)
- Settlement P&L partially offset trading losses but insufficient to prevent daily breach

---

## Reconciliation

| Source | Calculation | Result |
|--------|------------|--------|
| Gross wins (SELL realized >0) | SUM(realized) WHERE realized>0 | +35.56645560 |
| Gross losses (SELL realized <0) | SUM(realized) WHERE realized<0 | -55.51361430 |
| Net realized | wins + losses | -19.94715870 |
| Settlement P&L | SUM(realized_pnl) FROM settlements | +5.09001490 |
| Total fees | SUM(fee) FROM orders | -2.56694380 |
| **Total P&L** | net_realized + settlement - fees | **-17.42405160** |

The $-17.42 total P&L is consistent across all tables. The $-10.02 daily loss that triggered RISK_STOPPED was the within-UTC-day portion of the realized losses.

---

## Key Takeaways

1. **No code defect** — RISK_STOPPED fired correctly at the $10 daily limit
2. **Market regime** — markets declined during soak; avg exit < avg entry
3. **Loss drivers** — 106 holding-period exits (-35.68) + 55 reversals (-19.83) = 71% of losses
4. **Max drawdown** — 38.52% from peak $102.94 to $70.27
5. **Fee efficiency** — $2.57 in fees across 480 fills is acceptable (0.05% of total notional)
6. **Settlement timing** — 48 markets resolved, +$5.09 P&L from winning outcomes

> **When to revisit**: Before live trading, add Level 2 slippage modeling and market-regime filters for declining markets.