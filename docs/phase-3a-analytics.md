# Phase 3A — Offline strategy learning

## Status and safety

No authoritative trading DB exists in this worktree (search: `*.db`, `*.sqlite`, `*.sqlite3`, `*.db3`; `data/` only `.gitkeep`). All new tests use explicitly SYNTHETIC temporary SQLite fixtures. Prior reported 480 fills, approximately -17.42 P&L, 0.641 SELL-fill profit factor and exit-reason losses remain **unverified prior reports**, not findings produced here. No historical strategy improvement is established.

Analytics opens SQLite with `mode=ro`, sets `query_only=ON`, starts one consistent read transaction, never imports the engine/store or migrates schemas. Use an operator-provided consistent offline copy, not an active soak DB. Nothing here connects to VPS, deployment, Hermes, wallet, signer or exchange. No strategy/risk behavior changes.

## Run

PowerShell:

```powershell
$env:PYTHONPATH = 'src'
uv run --offline --locked python -m polymarket_bot.analytics 'C:\offline\paper.sqlite' --bot-id default --source-label OPERATOR_PROVIDED_OFFLINE_COPY
uv run --offline --locked python -m unittest tests.test_analytics -v
```

POSIX:

```sh
PYTHONPATH=src uv run --offline --locked python -m polymarket_bot.analytics /offline/paper.sqlite --bot-id default --source-label OPERATOR_PROVIDED_OFFLINE_COPY > /offline/analytics.json
```

JSON goes to stdout. Exit 0: report reconciles; exit 1: report emitted with reconciliation errors; exit 2: unsupported DB, malformed values, invalid arguments or evaluation refused. Source label is user-supplied provenance, not authenticity verification. Do not commit reports containing operational data.

Optional fixed chronological cohort comparison:

```powershell
uv run --offline --locked python -m polymarket_bot.analytics 'C:\offline\paper.sqlite' --bot-id default --split-us 1792713600000000 --max-entry-price .75 --min-remaining-seconds 60
```

Cutoff is UTC epoch microseconds. Example values are NOT recommendations. Freeze cutoff and candidate filters before viewing test outcomes. Compare `evaluation.train` and `evaluation.test` baseline versus candidate. This is **DESCRIPTIVE_COHORT_NOT_BACKTEST**: existing executed trades retained or removed using available decision-time ask and remaining time. Recorded fills, partial execution, fees and exits remain unchanged. No automated search. Multi-entry episodes or unavailable decision inputs excluded from candidate with counts. A skipped trade could change capital, risk and later decisions; cohort P&L is not a counterfactual strategy return. Unknown/open trades excluded explicitly, not treated as losses/wins. An unresolved-only sample cannot validate a strategy.

## Existing system inventory

| Data | Available | Limits |
| --- | --- | --- |
| PaperEngine | `linux_mvp/engine.py`: UP-midpoint momentum, configurable lookback/threshold; one position; holding-age or reversal SELL | Holding age counts recorded decisions after entry decision slot, not elapsed seconds. HOLDING_PERIOD takes precedence over reversal. No explicit entry-price or remaining-time gate. |
| Execution | `orders`: terminal FILLED/PARTIAL aggregate, requested/filled quantity, notional, fee, realized, decision ID, created/updated microseconds | Not a separate fills table. Price-time visible-depth walk at next slot; terminal partial remainder abandoned. No queue model. `reason` overwritten with PARTIAL_BOOK; original SELL intent recovered from linked decision where available. |
| Fees | BUY notional + fee added to weighted-average cost basis; SELL realized = proceeds - SELL fee - proportional basis | Configured flat bps, not verified exchange schedule. Settlement realized = payout - remaining basis, includes remaining BUY fee. |
| Runs/accounts | IDs, initial cash, start, config JSON, schema, PAPER_ONLY | Analytics scope is entire bot account across runs, not isolated run. Multiple bots require explicit selection. |
| Market windows | Identity, start/end in UTC microseconds, status/note | Missing windows prevent time grouping/evaluation. UNKNOWN_SETTLEMENT cannot imply payout. |
| Observations | Slot/outcome/token, bid/ask/mid, source and receive times | MVP omits book sizes/full depth, validated-at clock and per-request latency. Decision-time quote usable only when source and receive timestamps are no later than decision. Clock ordering alone cannot prove freshness. |
| Decisions | Slot/action/reason/signal/momentum, mid_up, cash/exposure, inputs JSON, at_us | Momentum is not an expected-return probability. No calibrated expected edge. |
| Positions | Current residual quantity and fee-inclusive basis | No position history; reconstruct from full account orders/settlements. |
| Settlements | RESOLVED winner, aggregate payout/realized, settled_us, evidence, position_count (legacy may lack count) | Per-outcome settlement P&L reconstructed from residual holdings. Never infer resolution from BTC spot or window end. No settlement row required for already sold positions. |
| Equity | Cash, basis exposure, realized/unrealized, missing_marks, time | Bid-mark samples, not continuous curve; liquidation fees not included. Any incomplete mark suppresses full sampled maximum drawdown. No marks means unavailable. |
| Risk/events | Events and heartbeat, including daily_loss_limit and execution diagnostics | Event kinds counted, no claim that risk gates were triggered correctly without authoritative chronology. Execution delays are decision-to-terminal wall time, not network latency. |
| Recorder | Separate `recorder/schema.sql`: sample slots, request attempts, book observations, compressed response evidence, resolution observations, reference associations | Not an MVP ledger; rejected by this tool. Full depth may be recoverable from raw evidence for future separate replay adapter. Availability and identity/timestamps must be audited first. |

Required core tables: runs, accounts, orders, positions. Optional absent tables listed in `missing_tables`; absent quotes/timing grouped as MISSING or excluded, never substituted with zeros. Malformed financial values fail rather than silently convert to zero. Missing positions/entry history cannot establish basis. Settlement schemas missing legacy optional metadata remain usable; required financial fields may not be silently omitted.

## Financial definitions

- Fill count = terminal FILLED/PARTIAL **order aggregate**; never completed trade count.
- Trade = flat-to-flat market/outcome position episode. Partial SELL realizes an event but does not close trade. Multiple BUY additions use same weighted-average basis as store.
- `realized_net_pnl` = stored SELL realized + RESOLVED settlement realized. Independently reconstructed amounts compared per exit and settlement.
- `closed`/`groups` statistics use only closed episodes. Win/loss/breakeven based on net episode P&L; expectancy is net P&L / count. Profit factor sums positive net episode P&L / absolute negative net episode P&L; null when no losses, with explicit note. Empty cohort has null expectancy/win rate.
- `fees` reports all executed BUY/SELL fees including open holdings; informational, never subtracted from realized again. Per-trade `fees` are the only fees that offset that trade's `gross_pnl` to give `net_pnl`; the aggregate does not net to closed P&L when open positions remain.
- Check residual positions, payout, per-exit realized, total episode realized and `cash + remaining basis = initial cash + realized`. Decimal precision 512 matches store; absolute tolerance 1e-480 accommodates extreme proportional basis roundoff, not economic discrepancies.
- `realized_by_utc_day` uses SELL updated_us / settlement settled_us, never decision/create time or settlement evidence resolution date. Full history retained for basis.
- Groups: UP/DOWN; entry VWAP bands `<.25`, `[.25,.5)`, `[.5,.75)`, `>=.75`; final exit reason; elapsed holding seconds `<10`, `[10,30)`, `[30,60)`, `>=60`; fill-time market seconds remaining `<60`, `[60,300)`, `[300,600)`, `>=600`.
- `realized_by_exit_reason` includes actual partial realization events, even if episode remains open. Final-exit-reason groups attribute entire episode to its final exit; these are intentionally different views.
- Sampled maximum drawdown uses chronological `cash + exposure + unrealized` with initial cash as initial peak. All samples must have zero missing marks and finite unrealized. No claim about intra-cycle extrema.
- Snapshot fingerprint hashes sorted logical account rows, including configs, marks and events; repeated unchanged snapshots and parameters yield identical reports. No current timestamp, filesystem paths or secrets echoed.

## Biggest-loss investigation and three hypotheses

No real losses ranked yet. `biggest_losses`, realized-by-exit-reason, grouped cohorts, execution delays, partial/rejected order counts, stale/one-sided decision counts and event kinds prepare investigation. Correlation in these summaries is not prediction or causation.

1. **Forced exits may pay spread and fees too often.** Inspect HOLDING_PERIOD versus REVERSAL partial-realization and closed-episode views separately. Holding period is decision count; test longer/shorter holds and reversal confirmation in causal replay with same risk limits, net of both sides' fees. Do not assume holding to resolution is better; include terminal losses and unknown settlement.
2. **Late/high-price entries may lack room to cover costs.** Prespecify minimum remaining-time and maximum decision-ask gates, inspect train then untouched holdout. Existing cohort tool supports descriptive first pass only. High prices alone do not imply negative edge; require calibrated outcome probability before claiming expected profit.
3. **Small momentum may not cover liquidity/timing costs.** Stratify spread, decision-to-fill delay, partial/expired fills, stale/one-sided snapshots and direction. Hypothesize minimum cost-adjusted signal or spread/freshness gate. Compare with unchanged baseline under conservative depth, latency and fee sensitivity. Do not equate raw midpoint momentum with expected edge.

## Real data needed

Operator should provide an offline consistent SQLite backup of full paper account, including all runs/accounts, windows, observations, decisions, orders, current positions, settlements plus evidence/legacy metadata, equity, events and resolution_attempts. Include bot ID, exact engine commit/config history, data time range, known outages and source checksum. No credentials, wallet material or environment files needed. An ordinary copy of a live WAL database may omit committed data; backup creation belongs to authorized operator, not this tool/session. Do not access or disrupt soak.

For changed-parameter backtests also need full depth/raw recorder evidence, token/condition identity, coherent source/receive/validated times, explicit gap/staleness flags and defensible historical fee schedule. Top-of-book alone cannot justify unlimited-size fills. Unknown outcomes require verified resolution evidence or explicit open censoring.

## Proposed Phase 3B

1. Audit supplied offline ledger; require reconciliation and provenance, reproduce claimed totals and rank actual losses. Reconcile trade-count differences from prior SELL-fill reports.
2. Audit separate recorder evidence for depth and timestamp coverage; build immutable replay adapter exposing only data received by simulation time. Reject missing/stale books; never synthesize favorable fills or settlement.
3. Reproduce unchanged engine baseline causally, including next-slot expiry, depth-walk partials, fee-inclusive basis, account/risk state and UTC loss boundary. Compare orders/positions/realized values against recorded ledger before evaluating alternatives.
4. Freeze chronological training/validation/test market blocks, purge overlapping windows and delayed settlements, choose small prespecified parameter set from training only. Keep final holdout untouched; no repeated optimization against it.
5. Test three hypotheses independently against baseline with adverse fee/depth/latency assumptions, sample counts, unresolved exposure, drawdown coverage and uncertainty by market/day. Reproducible evidence/commit/config/split manifests. No deployment or automatic parameter updates.

## Verification record

TDD: analytics import missing (RED), 11 ledger tests GREEN after correcting hand-calculated fixture (-0.278, not -0.678); evaluation API missing and CLI arguments rejected (RED), 17 combined tests GREEN. All test data synthetic.

Independent review verdict: **ACCEPT — no Critical or Important findings.** Three Minor items addressed:
1. Removed dead `from contextlib import closing` import from `tests/test_analytics.py` (unused).
2. Documented `split_us` boundary asymmetry (non-strict market end, strict closed-us) in `analytics.py`.
3. Tightened `fees` semantics wording: per-trade `fees` only offset that trade's `gross_pnl` to `net_pnl`; aggregate is informational.

Note: reviewer flagged `closing` in `analytics.py` as unused — it is used at `analyze()` line 74 for the sqlite connection context manager. Import retained.

Baseline: 159 existing Linux MVP tests, 157 passed; `test_heartbeat_script_is_valid_bash` and `test_installer_shell_syntax_ok` fail because Bash receives Windows absolute paths. No deployment code changed.

**Final verification:** Phase 3A analytics 17/17 GREEN; focused store+report+engine+settlement 66/66 GREEN; broader `test_linux_mvp*.py` 157/159 (only 2 pre-existing Windows/Bash infra failures). No new regressions.
