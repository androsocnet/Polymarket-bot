# Phase 2B Soak P&L Reconciliation — Not Accepted

> Historical PAPER_ONLY evidence: `/var/lib/polymarket-btc/phase2b-evidence/soak-20261024T141531Z/`. No new run. Prior reconciled-P&L, drawdown, market-cause and slippage conclusions are withdrawn pending a correctly scoped analysis.

## Previously Recorded Raw Query Results

These values came from prior queries, not a fresh accounting reconciliation. They may include historical records outside the soak window. They must not be presented as soak deltas or used as acceptance evidence.

| Query output | Recorded value |
|--------------|----------------|
| Fills | 480 (241 BUY, 239 SELL) |
| SELL realized total | -19.94715870 |
| Order fees | 2.56694380 |
| Settlement realized total | 5.08001490 |

Previous document reported settlement total `5.09001490` and total P&L `-17.42405160`; those did not reconcile with recorded queries. No replacement total asserted here.

## Why Previous Analysis Was Invalid

- Decimal amounts are stored as SQLite TEXT. Comparisons such as `realized > 0` without decimal-aware conversion do not reliably classify numeric wins/losses. Use Python `Decimal` for exact classification and aggregation.
- Lifetime table totals were mixed with run/window deltas. Bound records using before/after snapshots, timestamps, bot/run IDs, and settlement attribution before comparing totals.
- Realized accounting already incorporates applicable fees. Subtracting all order fees again double-counts them; reconcile against store/report accounting rather than inventing a new formula.
- Net marked equity uses `cash + exposure + unrealized`, not `cash + realized + unrealized`. Missing marks and scope must be handled before computing drawdown. Previous equity/peak/drawdown figures are withdrawn.
- Unpaired average BUY/SELL prices cannot prove market direction or loss causes. Cycle durations cannot establish position holding periods. Slippage conclusions require inspection of actual fill/depth logic and paired executions; prior assertions of no depth modeling or no delayed-fill slippage are withdrawn.

## Risk Stop (Separate From Reconciliation)

Prior evidence recorded `RISK_STOPPED` at cycle 8368 with UTC-day realized P&L `-10.02459130`, crossing unchanged `$10 DAILY_LOSS_LIMIT`. Terminal stop is intentional fail-closed behavior, not permission to weaken the guard or resume trading. This does not validate the rejected P&L analysis.

No strategy or risk-limit changes made for GitHub synchronization. Exact scoped accounting reconciliation remains pending; infrastructure-only acceptance is separate and requires zero **new** trading records while preserving historical accounting.
