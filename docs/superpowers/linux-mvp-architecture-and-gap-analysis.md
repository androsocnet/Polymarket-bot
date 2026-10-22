# Linux-first PAPER MVP: architecture, gap analysis, and deployment

Date: 2026-10-22; Phase 1 update 2026-10-23. Branch: `linux-mvp` (isolated worktree, based on the
accepted CP5/CP6 sources).
Status: **Phase 1 ingest-only pipeline implemented and offline-tested on Windows; the paper
engine/report remain present but dormant; Linux/systemd execution has not been performed; no
live-trading authorization exists or is implied.**

## 1. Verdict

**Phase 1 is implemented and offline-accepted.** The deployable unit runs `ingest`, not the paper
engine. Each cycle reuses the accepted public discovery/time/two-book stack, persists a window and
idempotent top-of-book observations in SQLite, handles rollover and restart, checks `STOP` before
any request, and applies a configurable poll interval plus bounded adaptive backoff for throttling.
It writes no decisions, orders, positions, or equity. The paper engine, execution, risk gates, and
read-only report remain in the tree for Phase 2 but are not started by the unit.

The earlier end-to-end PAPER MVP remains available as dormant code: public discovery and two-book
snapshots are reused unchanged, a causal momentum decision loop runs at the 2-second cadence,
hypothetical next-slot market-IOC fills are recorded in SQLite, risk limits and an operator `STOP`
file are enforced, restarts and window rollovers are handled without duplicate orders or invented
settlement, and a read-only report exists. None of that is claimed as exercised by the Phase 1
systemd unit.

What this does **not** establish:

- It was not run on Linux. The unit file and its `MemoryMax`/`CPUQuota`/`TasksMax` controls were
  only statically tested on Windows; kernel enforcement is unverified here.
- No live market endpoint was contacted during this work. Every test used synthetic snapshots and
  a socket guard; no profitability, fill-quality, or availability claim follows.
- It does not satisfy or modify any CP5/CP6 strict-resource contract. CP6.3 remains a separate,
  stopped, blocked line of work; this branch neither reopens nor relaxes it.
- There is no wallet, API key, signing, authenticated request, order endpoint, or activation flag
  anywhere in the package. PAPER_ONLY is a schema/config invariant, not a runtime preference.

## 2. What exists, and the evidence for it

| Requirement | Implementation | Evidence |
|---|---|---|
| Phase 1 ingest | `ingest` subcommand + `IngestEngine`: windows, both top-of-book observations, heartbeat/events only | 2×450-slot ingest soak; restart/rollover/STOP/dedupe tests |
| Discover the current BTC 15-minute market | reuse `market_data.discovery.discover_current` through `collect_snapshot` | accepted discovery tests + MVP cycle tests |
| Monitor both order books | one coherent `MarketSnapshot` per configured poll (2 s default), both outcomes stored | ingest soak: 1,800 observations; engine stale/one-sided tests (Phase 2) |
| Evaluate strategy (Phase 2, dormant) | causal mirror of the accepted rule: UP-midpoint momentum, lookback 3, threshold 0.02, hold 3, $10 order/$10 exposure, single position | warmup→BUY→fill test; holding-period SELL test |
| Paper execution (Phase 2, dormant) | next-slot market IOC walking visible depth; 10 bps assumed fee; partial fill terminal; no settlement | store exactly-once/partial/no-liquidity tests; fill accounting test |
| Risk limits | per-order, exposure, trades/window, daily realized loss, source-age, consecutive-error fail-closed | risk, loss-gate, stale, error-backoff tests |
| Emergency stop | `<state-dir>/STOP` checked before collection, execution, and decisions; clean exit | STOP test: zero collector calls, pending order untouched, one event |
| Order deduplication | deterministic `order_id`, unique decision `(bot, market, slot)`, `PENDING`→terminal exactly once | duplicate-slot, duplicate-order, restart tests |
| Failure recovery | active window/positions/orders reused on restart; pending order fills only on the next fresh slot, else expires | mid-window restart and expiry tests |
| Rollover | old window `UNKNOWN_SETTLEMENT`, pending orders `WINDOW_CLOSED`, new window starts | rollover test + soak |
| Performance reporting | read-only SQLite report (JSON/text) with explicit assumptions and missing-mark flags | report tests; deterministic output |
| 24/7 supervision | systemd unit runs `ingest`; `Restart=on-failure`, bounded start-limit, signal-safe loop, single-instance lock | service/lock tests; unit static tests; **host gate pending** |
| Linux resource controls | `MemoryMax=512M`, `CPUQuota=50%`, `TasksMax=64`, `LimitNOFILE=256`, hardening | unit static tests; **host execution unverified** |
| Live trading disabled | no live code path; config refuses `MODE != PAPER_ONLY`; secrets absent | CLI refusal test; forbidden-token scan; env example test |

## 3. Architecture

```
systemd (PAPER_ONLY, cgroup limits, Restart=on-failure)
   │ SIGTERM / STOP file
   ▼
linux_mvp.__main__  ingest | run | report
   │ single_instance(state_dir)  (OS byte-range lock)
   ▼
linux_mvp.service.Service.run
   │ one engine step per cycle, configurable poll, bounded adaptive backoff, signals
   ├── Phase 1 (unit): linux_mvp.ingest.IngestEngine
   │    1. STOP gate                  (before any network request)
   │    2. collector.collect()        accepted PublicGet → Gamma/CLOB → MarketSnapshot
   │    3. window state machine       start | continue | rollover/abandon
   │    4. idempotent observations    top-of-book, unique per (bot, market, slot, outcome)
   │    5. heartbeat/events           no decisions, orders, positions, or equity
   └── Phase 2 (dormant): linux_mvp.engine.PaperEngine
        pending execution → one decision per slot → mark-to-market
   ▼
linux_mvp.store.MvpStore  (SQLite WAL, synchronous=FULL, schema mvp-v1)
   ├── windows, observations, events, heartbeat, runs, accounts  (Phase 1)
   ├── decisions, orders, positions, equity                      (Phase 2, dormant)
   └── linux_mvp.report.build_report (read-only connection, Phase 2 surface)
```

### Cycle ordering (why it is causal)

`collect → persist → execute pending → decide → mark` (dormant Phase 2 engine). A decision made
from slot *N* can only be executed against a snapshot at slot ≥ *N+1*; the engine refuses to fill a
pending order on its own decision slot (`slot_index <= decision_slot`), expires it if the next slot
is missed, and rejects it at rollover. There is no look-ahead and no retroactive fill. A duplicate
slot can never create a second decision or order.

Phase 1 uses the narrower causal prefix `STOP → collect → window → observe → heartbeat`. It shares
the same collector, window state machine, and unique observation key, but deliberately omits every
decision/execution/mark step. A duplicate snapshot is idempotent and returns `DUPLICATE_SLOT`.

### State model (schema `mvp-v1`)

`runs` (append-only start records, `mode` CHECK = `PAPER_ONLY`), `accounts` (one paper account per
bot), `windows` (`ACTIVE|CLOSED|ABORTED|UNKNOWN_SETTLEMENT`), `observations` (unique per slot and
outcome, first write wins), `decisions` (unique per slot, first write wins), `orders` (unique
deterministic id, `PENDING|FILLED|PARTIAL|REJECTED`), `positions` (fee-inclusive cost basis),
`equity`, `events`, `heartbeat`.

### Safety invariants

1. **No live path**: package scan test forbids key/signing/wallet/order-endpoint tokens; no
   authenticated client exists; `MODE` must be exactly `PAPER_ONLY`.
2. **Action gates first**: STOP and the daily loss gate precede collection, execution, and decisions.
3. **Exactly-once fills**: the pending→terminal transition is a single SQLite transaction; a short
   book yields a terminal `PARTIAL`, never a retry against later liquidity.
4. **No invented settlement**: rollover closes `UNKNOWN_SETTLEMENT`; open positions are marked only
   at a fresh bid, otherwise `missing_marks` increments and `net_value` becomes `null`.
5. **Fail closed on data**: stale, one-sided, out-of-window, or ambiguous snapshots make no order;
   consecutive errors stop the service after a bounded number of attempts.
6. **Restart safety**: the paper account is stable across runs; the active window, positions,
   decisions, and pending order are recovered; an expired window is `ABORTED`.

## 4. Reuse and preservation of CP5/CP6

Reused byte-identical to the accepted CP5 worktree sources: `market_data` public read stack
(including the accepted numeric `/events/{id}` allowlist extension), `paper.py` arithmetic,
`strategy.py` offline replay, `full_window.py` assessor, recorder modules, and their tests. The
isolated branch carries the accepted (previously uncommitted) sources from the CP5 worktree; no
accepted file was edited by Phase 1.

Verified hashes (SHA-256, unchanged):

| File | SHA-256 |
|---|---|
| `src/guarded_full_window.py` | `b21dab03660415cb175d36fd5be66b6bd753c1756786777e9b7b88b97f3d5872` |
| `src/polymarket_bot/recorder/live_guard.py` | `8e2d0c94287c0bce7b7818289752abdee7cb51ca5cceff69a436f9a0a2aba3a1` |
| `src/polymarket_bot/recorder/windows_job.py` | `d5d1a19a2f3688e7ce85c68cafee14d983706f0138dfa3b8d93521aa2fc39561` |
| `src/polymarket_bot/full_window.py` | `2daca0ca23aec609e6bd74ced97c37178d4ffb250655a08d39796c8ee2814407` |
| `tests/test_full_window.py` | `6544b05fee5606e114ebc32bd88c981bac103c3c942734d95e6e67e39d4b62f8` |
| `src/polymarket_bot/paper.py` | `1bc3c229281c93518c4fb04bd16a80b46c9ce82e7735028bd446fcd69e6fbe71` |
| `src/polymarket_bot/strategy.py` | `5c37ca4690aaab257e6c2cae0d1cdde84a560f093970ceca72fad3301e274e01` |
| `src/polymarket_bot/market_data/http.py` | `cecebd668a4556863694d45d65cdd04fea903aea1f3d4e7a16efd0f3f600ec22` |

The live engine mirrors the strategy rule for one-pass use. It is intentionally not claimed to be
decision-for-decision identical to offline replay: replay sees the complete recording, the live
engine sees only what has arrived. Parity is asserted for the rule's inputs and actions
(warmup, momentum sign, one-position handling, quantity formula); any future divergence study must
compare the same causal prefix.

## 5. Official py-sdk evaluation

The official SDK is `Polymarket/py-sdk` (GitHub), published as **`polymarket-client`**, imported as
`polymarket`, currently **v0.x** with documented possible breaking changes in minor versions, in both
sync and async form. It offers a `PublicClient` for market data plus authenticated and trading
clients.

**Decision: do not adopt for this MVP.** Reasons:

1. The MVP needs only public reads, and the accepted `market_data` stack already provides stricter
   identity/numeric validation and the 10 MiB/allowlist guards that CP5/CP6 evidence depends on.
2. Introducing a 0.x dependency with a moving surface to re-implement reads adds risk without a
   tested benefit, and cannot be verified in this offline exercise.
3. Authenticated/trading clients are out of scope; they would create exactly the live surface this
   MVP forbids.
4. Dependency surface stays unchanged (`httpx`, `websockets` as already pinned).

A concrete compatibility check was run on 2026-10-23. `polymarket-client==0.14.0` requires Python
`>=3.11` (compatible) but declares `websockets>=13,<16`; this repository pins `websockets>=17.2`.
`uv` resolution fails with "your requirements are unsatisfiable". Resolving the SDK standalone would
downgrade `websockets` to 15.0.1 and add about 20 packages (`eth-account`, `eth-abi`, `pydantic`,
`pycryptodome`, `msgpack`, …). Adopting it is therefore a dependency decision, not a drop-in for
Phase 1.

Re-evaluate at a live-readiness review, where the SDK's authenticated trading client could replace
bespoke signing — under separate owner approval and a dedicated review, not by implication from
this document.

## 6. Gap analysis

| Area | Status | Detail / residual risk |
|---|---|---|
| Discovery and book capture | COVERED (reuse) | accepted guards; one full discovery + two-book snapshot per configured poll (2 s default, 2–60 s, ≈5 HTTP requests); 429/5xx retries honor `Retry-After`, then cycle backoff is bounded and resets on success |
| Phase 1 ingest storage | COVERED (offline) | top-of-book for both outcomes, unique per slot/outcome; no decisions/orders/positions/equity; 2×450-slot synthetic run |
| Operator-visible liveness | PARTIAL | heartbeat row + journald; no alerting, Telegram, or dashboard until Phase 2 |
| Decision rule | COVERED (causal mirror) | same inputs/actions; profitability and parameter quality remain unvalidated |
| Fill realism | PARTIAL | price-time depth walk with constant fees; no queue position, maker/taker distinction, or measured latency/slippage |
| Settlement / realized outcome at expiry | NOT COVERED (by design) | no resolution inference; positions end `UNKNOWN_SETTLEMENT`; PnL is mark-to-market only |
| Accounting | COVERED | cash, fee-inclusive basis, realized on sells, marked net value, drawdown of marked points |
| Dedupe / idempotency | COVERED | deterministic ids and uniqueness constraints, tested across restart |
| Restart/recovery | COVERED (offline) | mid-window, expired-window, pending-expiry tests; long-run crash injection not performed |
| Rollover | COVERED | tested with disjoint synthetic markets; real Gamma timing/edge cases rely on accepted discovery |
| Emergency stop | COVERED | file gate + signal handling; STOP persists across restart by design |
| Risk limits | COVERED (basic) | order/exposure/trades/day-loss/staleness/error budget; no volatility or correlation limits, no per-hour throttle |
| Reporting | PHASE 2 (dormant) | read-only report exists and is tested; Phase 1 writes no decision/order rows, so it reports windows and `UNKNOWN_NOT_INFERRED` only |
| Retention / backup | PARTIAL (manual) | runbook documents stop-and-copy or `.backup`; no automatic rotation or disk guard |
| Alerting / metrics | NOT COVERED | `journalctl`, heartbeat table, and manual report only |
| Clock discipline | PARTIAL | accepted server-clock anchoring; host NTP health is not checked |
| Linux resource enforcement | UNVERIFIED ON HOST | unit statically tested; needs `systemd-analyze verify` and `systemctl show` evidence on Ubuntu |
| Supply chain / deploy | PARTIAL | installer installs `httpx` from PyPI at deploy time; no hash pinning yet |
| Security | COVERED (baseline) | unprivileged user, strict filesystem, no secrets, no live capability; host patching is operational |
| Multi-bot / multi-market | NOT COVERED | one bot id per database, one current market at a time |
| CP6 strict contracts | NOT CLAIMED | unchanged status; no resource or long-window guarantee is transferred |

## 7. Failure behavior

| Condition | Behavior |
|---|---|
| one-sided/stale book | decision recorded with `ONE_SIDED_BOOK`/`STALE_PAIR`, no order |
| pending order, next slot missing | order rejected `EXPIRED_EXECUTION_WINDOW`, no retroactive fill |
| rollover with pending order | rejected `WINDOW_CLOSED`; old window `UNKNOWN_SETTLEMENT` |
| public data error | event logged; bounded adaptive backoff (base … cap); after the configured consecutive errors, `FAILED_STOP`, exit `75`, and no systemd restart (`RestartPreventExitStatus=75`) |
| rate limiting / request volume | one discovery + two-book snapshot per configured poll (≈5 requests); 429/5xx are retried by the accepted stack, then cycle backoff grows from the base to the cap and resets on success; no separate daily request budget |
| daily realized loss reached | no collection or orders; `RISK_STOPPED` event; resumes only when the loss window no longer breaches |
| operator STOP | immediate clean exit, no further action; restart requires removing the file |
| crash / power loss | systemd restarts; SQLite WAL recovery; active window and account resume; no duplicate ids |
| disk full / SQLite error | unhandled error → non-zero exit → systemd restart loop bounded by start-limit; operator action required (no disk guard) |
| second instance | refused by OS lock, exit code 3 |

## 8. Test evidence

All new tests are offline: synthetic snapshots, injected clocks, temporary SQLite, denied sockets,
and a source-stability inventory. Commands use the repository venv Python and the evidence runner
`C:\Users\Andro Saptajulianto\AppData\Local\Temp\opencode\linux-mvp-20261022\run.py`.

| Suite | Result | Log SHA-256 |
|---|---|---|
| Phase 1 MVP suites (config, store, engine, report, service, deploy, soak, ingest) | 76/76 OK (`42.875 s`) | `b835b618ab7524aa96182ca3b328b526383375bd0e1890ab815a00b94bd08579` |
| exact 239-test baseline replay (original accepted test IDs, unchanged) | 239/239 OK (`236.906 s`) | `c625a35478669fb9a34af42af34498e850cf40ba995e22cc6b375a9f65832cde` |
| focused regression (accepted paper, strategy, full_window, recorder-selected, discovery, snapshot) + all Phase 1 MVP suites | 258/258 OK (`257.421 s`) | `c04386ceeb3dd95753f48b71d1a638bfb529134df71ba71518b9d67e27faedb4` |
| full discovery (prior tree, disclosure only) | 1116 tests, 0 failures, **2 errors**, `source_stable=true` | `f6651f6045cba83f11151d2098d2294dc8c955ba152dd8860da7c3a2d375a2eb` |

The two full-discovery errors are not Phase 1 failures: one expects the historical
`data/cp5_6c_replacement_…sqlite3` file which was not copied into this worktree, and one is a
Windows `TemporaryDirectory` cleanup lock (`execution.jsonl`). The exact 239-test baseline replay
above is green and is the regression gate requested for Phase 1.

Initial Phase 1 RED failures were real: the `ingest` module did not exist, the new config fields were
absent, and the unit still ran the paper engine. All were fixed before the green runs above.

Initial RED failures were real defects found by tests: Python's `Decimal` accepts `1_0` unless the
input grammar is constrained (fixed with a plain-decimal regex); the first position write was an
`UPDATE` without an insert (fixed with an upsert); the report used the last *markable* point as the
current net value instead of refusing an unmarked current position (fixed); the deploy baseline
asserted the wrong pre-existing dependency list (corrected to the branch's actual mix).

Phase 1 ingest evidence: two complete 450-slot windows (900 cycles, 1,800 observations, 0
decisions/orders). The dormant Phase 2 paper soak separately executes 900 cycles with 900 decisions
and a report. Neither is a duration/reliability soak; they are full-size offline workloads.

## 9. Deployment procedure

Full runbook: [`docs/linux-mvp-runbook.md`](../linux-mvp-runbook.md). Summary:

```bash
sudo apt-get install -y rsync python3-venv sqlite3
sudo mkdir -p /opt/polymarket-btc
sudo rsync -a --delete --exclude .git --exclude .venv --exclude data --exclude logs \
  --exclude __pycache__ ./ /opt/polymarket-btc/
sudo bash /opt/polymarket-btc/deploy/install.sh      # ENABLE=1 to also enable/start
# edit /etc/polymarket-btc/paper.env (MODE is hard-refused unless PAPER_ONLY)
sudo systemctl enable --now polymarket-btc-paper    # runs the `ingest` subcommand
journalctl -u polymarket-btc-paper -f
```

Emergency stop: `sudo -u polymarket touch /var/lib/polymarket-btc/state/STOP`; resume only after
removing it and starting explicitly. Phase 1 verification queries and the full host gate are in
[`docs/linux-mvp-runbook.md`](../linux-mvp-runbook.md) §5 and §10.

On the host, verify: unit syntax (`systemd-analyze verify`), actual cgroup values
(`systemctl show … -p MemoryMax -p CPUQuotaPerSecUSec -p TasksMax`), a clean STOP, a mid-window
restart with no duplicate observation keys, and two books per observed slot with zero
decisions/orders. These steps are **not** performed on the Windows development host, so Phase 1 is
not yet accepted for Linux operation.

## 10. Residual risks and owner decisions

Owner approval is required for, and is not granted by this document:

1. Installing and running the service on a VPS in PAPER_ONLY mode, including a stated observation
   period and an owner of the host.
2. Any strategy, fee, or risk-limit change from the conservative defaults.
3. Data retention/backup policy and alerting (currently manual/journal only).
4. Adopting `polymarket-client` and any live-trading work. Live requires a separate design covering
   keys, signing, order lifecycle, reconciliation, and the still-unmet CP6 strict-resource and
   long-window contracts.
5. Any claim about CP6.3; this branch leaves its blockers and evidence untouched.

Until host verification and that separate review, the accurate verdict is: **Phase 1 ingest
pipeline complete and offline-tested; the paper engine is dormant; not yet operated on Linux; live
trading remains disabled by construction; Linux resource enforcement is unverified.**
