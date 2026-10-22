# Linux PAPER MVP Implementation Plan

> Inline execution (executing-plans): implement each task in this session, RED→GREEN, no subagents, no commits.

**Goal:** A runnable, restart-safe, PAPER-ONLY BTC 15-minute bot on Ubuntu, reusing accepted market-data and paper components.

**Architecture:** `linux_mvp` package: config → store → engine (collector-injected) → report → service loop; deploy/ systemd artifacts.

**Tech Stack:** Python 3.12, stdlib, existing httpx, SQLite, systemd.

**Spec:** `docs/superpowers/specs/2026-10-22-linux-mvp-paper-bot-design.md`

## Global constraints
- PAPER_ONLY only; no key/order/auth code paths; refusals are errors, not flags.
- No changes to accepted CP5/CP6 files; no CP6 strict-contract claims.
- Deterministic ids, idempotent writes, one writer, fail closed.
- Exactly one runtime dependency already present (`httpx`). No new dependency.
- Windows host: only offline tests; Linux kernel/systemd behavior is explicitly unverified.

## Tasks

### Task 1: Config and refusal surface
Files: create `src/polymarket_bot/linux_mvp/__init__.py`, `config.py`; test `tests/test_linux_mvp_config.py`.
Produces: `Settings` frozen dataclass (`from_env`, `validate`), `ConfigError`, constants for conservative defaults; `mode` must equal `PAPER_ONLY`.
Tests: defaults; env parsing; rejects live/unknown mode, non-positive budgets, bad thresholds, negative risk values; rejects state/data dir overlap? (no) ; `PAPER_ONLY` constant exported.

### Task 2: Store and idempotency
Files: create `store.py`; test `tests/test_linux_mvp_store.py`.
Produces: `MvpStore.open(path)` (WAL, FULL, busy_timeout, schema mvp-v1), context `transaction()`, methods:
`start_run`, `upsert_window`, `record_observation`, `record_decision`, `queue_order`, `pending_orders`, `execute_order`, `positions`, `record_equity`, `record_event`, `heartbeat`, `close`, `active_window`, `window_market`, `latest_observation`.
Tests: WAL/FULL pragmas; observation/decision dedupe (first wins); deterministic order id dedupe; single execution transition (PENDING→FILLED, second attempt no-op); positions transactionality; schema constant `mvp-v1`.

### Task 3: Engine decisions, execution, risk
Files: create `engine.py`; test `tests/test_linux_mvp_engine.py` with `tests/test_linux_mvp_support.py` (synthetic `MarketSnapshot` builder reusing `tests.support`).
Produces: `PaperEngine(store, collector, settings, run_id, bot_id, now_us=...)`; `step() -> StepResult`; injectable clock `now_us` and `stop_path`. Reuses accepted models `MarketSnapshot`.
Tests: warmup (3 pairs) then BUY; next-slot depth-walk fill with fee; exposure/order/trade caps; duplicate slot ignored; stale/one-sided book yields no decision; emergency stop blocks decision+execution and records event; data-error backoff counter; deterministic order id; daily loss gate stops new entries; no settlement invention.

### Task 4: Rollover and restart recovery
Files: extend `engine.py`; tests `tests/test_linux_mvp_rollover.py`.
Tests: market A→B transition closes A `UNKNOWN_SETTLEMENT` (never settled), starts B active; restart mid-window reuses active window, does not re-emit decisions/orders, resumes position and executes pending order once; restart after window end marks `ABORTED` and starts fresh; stale pending order expired not filled retroactively.

### Task 5: Report
Files: create `report.py`; test `tests/test_linux_mvp_report.py`.
Produces: `build_report(db_path, run_id=None) -> dict`, `render(report) -> str`; counts windows/decisions/orders by status, fills, fees, realized/unrealized/missing marks, rejection reasons, per-window rows, assumptions and limitations block.
Tests: synthetic DB yields exact counts, exit disposition flags, deterministic JSON.

### Task 6: Service loop, lock, CLI
Files: create `service.py`, `__main__.py`; test `tests/test_linux_mvp_service.py`.
Produces: `single_instance(state_dir)` file lock (fcntl/msvcrt), `Service.run(cycles=None)` with SIGTERM-safe stop flag, backoff on `MarketDataError`, heartbeat, STOP polling; CLI `run` and `report` subcommands.
Tests: second instance refused; clean stop on flag; error backoff does not crash; START/STOP file semantics; report CLI writes JSON; CLI refuses non-PAPER_ONLY environment.

### Task 7: Deployment artifacts + static contract tests
Files: create `deploy/polymarket-btc-paper.service`, `deploy/polymarket-btc.env.example`, `deploy/install.sh`, `docs/linux-mvp-runbook.md`; test `tests/test_linux_mvp_deploy.py`.
Tests: unit has `PAPER_ONLY=1`, `Restart=on-failure`, `MemoryMax=512M`, `CPUQuota=50%`, `TasksMax=64`, `NoNewPrivileges=true`, `ProtectSystem=strict`, `ReadWritePaths`, non-root `User=`; env example has no secrets; install script is idempotent-shaped (`set -euo pipefail`, `systemctl daemon-reload`, `enable --now` only behind flag); no `PRIVATE_KEY`/`order` endpoint strings in package.
Parses unit with configparser? (systemd syntax is INI-like but keys repeated sections; use simple text assertions + `systemd-analyze verify` documented as Linux-only).

### Task 8: End-to-end soak, regressions, docs
- Fake-clock end-to-end: 2 windows × 450 slots via synthetic collector; verify decisions, fills, rollover, report.
- Run targeted regressions (paper/strategy/market_data/recorder-selected) plus new tests.
- Run full suite once after final changes; custody: accepted files unchanged hashes; `git diff --check`.
- Write `docs/superpowers/linux-mvp-architecture-and-gap-analysis.md` (architecture, gap analysis, test evidence, deployment procedure) and update `README.md` link.
- Self-review against `requesting-code-review` checklist; disclose author-only review.
