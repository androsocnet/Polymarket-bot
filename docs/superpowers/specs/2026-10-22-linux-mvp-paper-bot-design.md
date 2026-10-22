# Linux MVP — BTC Up/Down 15-minute paper bot design

Date: 2026-10-22. Branch `linux-mvp`, based on accepted CP5/CP6 sources.

## Intent

A lean, reliable, 24/7 PAPER-ONLY service on an Ubuntu VPS that trades the current Polymarket
BTC Up/Down 15-minute market against public order books, records every decision and hypothetical
fill, and reports performance. Live trading is structurally absent, not merely disabled.

This document is the design; `docs/superpowers/plans/2026-10-22-linux-mvp-paper-bot.md` is the
implementation plan; `docs/superpowers/linux-mvp-architecture-and-gap-analysis.md` is the delivered
architecture + gap analysis with test evidence.

## Goals

1. Discover the unique current BTC 15-minute market each cycle via the accepted public discovery path.
2. Sample both outcome books on a 2-second cadence and derive decisions causally, with no look-ahead.
3. Simulate market-IOC paper fills, fees, cash, positions, and mark-to-market equity in SQLite.
4. Survive restarts, rollovers, transient HTTP failures, and operator restarts without duplicate orders
   or invented fills/settlement.
5. Enforce hard risk limits and an emergency stop file checked before every order.
6. Run under systemd with Linux resource controls and recover automatically from crashes.
7. Keep every accepted CP5/CP6 module byte-identical and make no claim that CP6 strict contracts passed.

## Non-goals

- No live orders, signing, wallets, API credentials, or authenticated HTTP.
- No settlement or resolution inference; positions at window end are marked `UNKNOWN_SETTLEMENT`.
- No profitability claim. Assumed fees/latency/slippage remain assumptions.
- No CP6 long-window guard, Windows Job, or 1 GiB/256 MiB/120 s contract claims.
- No market-making, queue modelling, multi-strategy framework, web UI, or backtest engine rewrite.
- No new runtime dependency.

## Constraints

- Python 3.12, stdlib + existing `httpx` only (repository `pyproject.toml`).
- Public endpoints only, via `market_data.http.PublicGet` allowlist and 10 MiB body cap.
- SQLite single writer, WAL, `synchronous=FULL`, `busy_timeout`.
- Decisions and order ids are deterministic and idempotent, so replaying a slot cannot duplicate a fill.
- One service instance per data directory (OS file lock).
- Deployment artifacts are Linux systemd files; they cannot be executed on this Windows host.

## Architecture

```
systemd unit (PAPER_ONLY env, MemoryMax=512M, CPUQuota=50%, Restart=on-failure)
        │ SIGTERM
        ▼
linux_mvp.service  ── single-instance lock, signal handling, error backoff, heartbeat
        │
        ▼
linux_mvp.engine.PaperEngine.step()  (one cycle)
        │  1 emergency-stop + risk gate
        │  2 collector.collect() ── accepted collect_snapshot()
        │       ServerClock + discover_current + 2× /book  (public allowlist)
        │  3 window state machine  (start / continue / rollover finalize)
        │  4 persist both observations (idempotent per slot+outcome)
        │  5 execute the pending hypothetical order against THIS book (depth walk)
        │  6 decide once per completed slot (momentum rule) and queue next-slot order
        │  7 equity mark, heartbeat, event log
        ▼
linux_mvp.store.MvpStore  (SQLite WAL, schema mvp-v1)
        │
        ├── linux_mvp.report  (JSON + text performance report)
        └── deploy/ (systemd unit, env example, install script)
```

### Reused accepted components (unchanged)

| Component | Reuse |
|---|---|
| `market_data.http.PublicGet` | public GET/POST allowlist, retries, 10 MiB cap, cookie/auth guard |
| `market_data.gamma.GammaClient` | event-by-slug, list events, market-by-id |
| `market_data.clob.ClobClient` / `ServerClock` | `/time`, `/book`, server-time anchoring |
| `market_data.discovery.discover_current` | unique current BTC 15m market, fast path + list fallback |
| `market_data.snapshot.collect_snapshot` | one coherent two-book snapshot, rollover detection |
| `market_data.parsing.parse_market` / `parse_book` | strict identity and numeric validation |
| strategy rule (threshold 0.02, lookback 3, hold 3, $10) | mirrored causally in the live engine |

`paper.py` / `strategy.py` remain the authoritative offline replay and are not modified. The live
engine mirrors the decision rule for causal, one-pass use; parity is asserted by a test over the
same synthetic snapshot sequence, and any divergence is reported rather than hidden.

### State model (schema `mvp-v1`)

- `runs(run_id, bot_id, started_us, mode, config_json, schema, software)` — `mode` CHECK = `PAPER_ONLY`.
- `windows(run_id, market_id, condition_id, event_id, start_us, end_us, status)` — statuses
  `ACTIVE|CLOSED|ABORTED|UNKNOWN_SETTLEMENT`.
- `observations(run_id, market_id, slot_index, outcome, token_id, bid, ask, mid, source_ts, received_us)`
  unique `(run_id, market_id, slot_index, outcome)`; first write wins.
- `decisions(run_id, market_id, slot_id, slot_index, action, reason, signal, momentum, mid_up, cash, exposure, inputs_json)`
  unique `(run_id, market_id, slot_id)`; first write wins, so a replayed cycle cannot duplicate decisions.
- `orders(order_id PK, run_id, market_id, token_id, outcome, side, quantity, decision_slot_id, status, reason, filled, notional, fee, created_us, updated_us)`
  statuses `PENDING|FILLED|PARTIAL|REJECTED`; deterministic id; insert-or-ignore + single execution.
- `positions(run_id, market_id, outcome, quantity, cost_basis)` updated in the same transaction as a fill.
- `equity(run_id, at_us, market_id, cash, exposure, realized, unrealized, missing_marks)`.
- `events(run_id, at_us, kind, detail)` — stop, data error, limit breach, rollover, recovery.
- `heartbeat(run_id PK, at_us, market_id, status, cycles, last_error)`.

### Safety invariants

1. **No live path exists**: package contains no authenticated client, no key handling, no order endpoint.
   `config` refuses any mode other than `PAPER_ONLY`.
2. **Emergency stop before action**: if `<state-dir>/STOP` exists, the engine queues no order, executes
   no pending order, records `emergency_stop`, and exits cleanly; restart with the file still present
   stays stopped.
3. **Risk gate**: per-order cap (`max_order`), exposure cap (`max_exposure`), trade-count cap per window,
   daily realized-loss cap, and source-age cap. Any breach rejects the decision with an explicit reason.
4. **No duplicates**: deterministic `order_id` and `(run_id, market_id, slot_id)` uniqueness; the pending
   order is executed exactly once (`PENDING`→terminal), including across restarts.
5. **No invented settlement**: at window end positions are marked with the last fresh bid if available,
   or `missing_marks`; `settlement` is never synthesized. Status `UNKNOWN_SETTLEMENT`.
6. **Fail closed on bad data**: stale/one-sided books, identity mismatch, discovery ambiguity, or a parse
   error produce no decision and no order; consecutive errors back off and then stop the service.
7. **Recovery**: on start, adopt the latest `ACTIVE` window if it is still inside its window; otherwise
   mark it `ABORTED`; pending orders older than one slot are expired, never filled retroactively.

## py-sdk evaluation

Official SDK: `Polymarket/py-sdk`, PyPI package `polymarket-client`, import namespace `polymarket`,
v0.x with documented possible breaking minors, sync `PublicClient` plus authenticated/trading clients.
Decision for this MVP: **do not adopt**. Reasons: (a) we only need public reads already implemented and
tested by the accepted `market_data` stack, with strict numeric/identity validation CP6 depends on;
(b) v0.x surface is still moving; (c) the MVP must remain installable/verifiable offline and add no
dependency; (d) authenticated/trading clients are out of scope. Re-evaluate at live-readiness review,
where its authenticated trading API could replace bespoke signing.

## Deployment resource model

- systemd: `MemoryMax=512M`, `CPUQuota=50%`, `TasksMax=64`, `LimitNOFILE=256`,
  `Restart=on-failure`, `RestartSec=5`, `NoNewPrivileges=true`, `PrivateTmp=true`,
  `ProtectSystem=strict`, `ProtectHome=true`, `ReadWritePaths=` data/state/log dirs, `KillSignal=SIGTERM`.
- `MemoryAccounting=true`; unit is a service, not a root process. A dedicated `polymarket` user owns
  `/opt/polymarket-btc` (code) and `/var/lib/polymarket-btc` (data/state).
- These are Linux kernel controls; on this Windows development host only unit-file contents are tested,
  never kernel enforcement. `systemd-analyze verify` is part of the deployment procedure on Ubuntu.

## Owner approvals still needed (not self-granted)

- Any move to live trading (requires separate review of keys, signing, order endpoints, and the CP6
  strict-resource contract — the latter is not claimed by this MVP).
- Adopting the official SDK as a dependency.
- Changing strategy economics, fees, or risk limits from the conservative defaults.
