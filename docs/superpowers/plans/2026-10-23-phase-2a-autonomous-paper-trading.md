# Phase 2A — Autonomous Paper Trading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the existing paper-trading engine with the verified Linux market-data pipeline, run extensive adversarial tests, perform bounded WSL2 PAPER_ONLY live validation, and produce an evidence-backed report.

**Architecture:** Reuses the existing `PaperEngine`, `MvpStore`, and momentum strategy. Switches the default systemd service command from `ingest` to `run` (or adds a separate paper unit). Adds settlement reconciliation safeguards, fee handling, and extensive adversarial unit tests before performing bounded WSL2 live trial.

**Tech Stack:** Python 3.12/3.14, SQLite (WAL, FULL sync), systemd, httpx, cgroups v2.

**Spec:** User prompt: Phase 2A Autonomous Paper Trading.

## Global Constraints

- `MODE=PAPER_ONLY` enforced at config, CLI, store schema, and systemd levels.
- No wallet access, signing, private keys, or authenticated API calls.
- No new SDK unless technically necessary (use existing `GammaClient` + `ClobClient` + `PublicGet`).
- No Git push. No remote VPS modifications.
- Preserve all CP5/CP6 accepted source hashes (7 files) and 25 pre-existing dirty worktree entries.
- Never fabricate fills, prices, or settlement.
- Missing or one-sided books must NEVER produce fills or artificial P&L.
- Settlement status `UNKNOWN_SETTLEMENT` preserved until authoritative verification; never infer from BTC spot price.
- Never claim a strategy is profitable without sufficient independent evidence.

## Review Focus

1. **One-sided book fill prevention:** Ensure an order for UP cannot fill if UP asks are empty/zero, even if DOWN has bids.
2. **Partial fill basis accounting:** When an order partially fills, cash deduction and position cost basis must exactly match `filled * price + fee`, and remainder is cancelled (not retried).
3. **Mid-window restart with pending order:** If the service restarts while an order is PENDING, verify it expires safely rather than filling against stale data.
4. **Settlement preservation across rollover:** When a market window closes, its status must be `UNKNOWN_SETTLEMENT`, not inferred from spot price or coin toss.
5. **Daily loss limit breach:** When realized daily loss reaches `-MVP_MAX_DAILY_LOSS`, the service must enter `RISK_STOPPED` and refuse all further orders without restart loops.

---

### Task 1: Audit & Integration Verification (Read-Only)

**Files:**
- Read: `src/polymarket_bot/linux_mvp/engine.py`
- Read: `src/polymarket_bot/linux_mvp/store.py`
- Read: `src/polymarket_bot/linux_mvp/config.py`
- Read: `src/polymarket_bot/linux_mvp/report.py`
- Read: `src/polymarket_bot/linux_mvp/__main__.py`
- Read: `deploy/polymarket-btc-paper.service`

**Interfaces:**
- Consumes: Existing Phase 1B codebase
- Produces: Verified integration gap checklist

- [ ] **Step 1: Verify accepted source hashes and dirty worktree count**

Run: check 7 accepted hashes match, status shows exactly 25 dirty entries.

- [ ] **Step 2: Verify `PaperEngine` wiring in `__main__.py` and `service.py`**

Confirm that `__main__.py` `run` subcommand already passes `engine_factory=None` which defaults to `PaperEngine`.

- [ ] **Step 3: Document integration gaps**

Document:
1. `deploy/polymarket-btc-paper.service` has `ExecStart=... ingest`. Needs to be `run` for Phase 2A paper trading.
2. `report.py` needs enhanced output: win rate on verified settled trades, unknown/unsettled position count, drawdown.
3. Need settlement reconciliation module or documentation explaining `UNKNOWN_SETTLEMENT` preservation.

---

### Task 2: Enhanced Reporting Metrics

**Files:**
- Modify: `src/polymarket_bot/linux_mvp/report.py`
- Test: `tests/test_linux_mvp_report.py`

**Interfaces:**
- Consumes: `MvpStore` SQLite database
- Produces: Enhanced report dict with `unsettled_positions`, `win_rate`, `drawdown`

- [ ] **Step 1: Write failing test for enhanced report fields**

```python
def test_report_includes_unsettled_and_win_rate():
    # Test that build_report returns 'unsettled_positions' count and 'win_rate'
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Implement enhanced metrics in `report.py`**

Add:
- `unsettled_positions`: count of open positions in windows with status `UNKNOWN_SETTLEMENT` or `ACTIVE`
- `settled_win_rate`: `None` (explicitly stating 0 settled trades; never invented)
- Ensure all limitations are clearly reported.

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

```bash
git add src/polymarket_bot/linux_mvp/report.py tests/test_linux_mvp_report.py
git commit -m "feat(report): add unsettled position count and win rate metrics"
```

---

### Task 3: Comprehensive Adversarial Unit Tests (TDD)

**Files:**
- Create: `tests/test_linux_mvp_adversarial.py`

**Interfaces:**
- Consumes: `PaperEngine`, `MvpStore`, `Settings`
- Produces: 10 focused adversarial test cases per Task 10 of prompt

- [ ] **Step 1: Write test: Missing bid/ask rejects execution**

Verify: Empty book on outcome produces `INSUFFICIENT_LIQUIDITY` rejection; zero position change.

- [ ] **Step 2: Write test: Insufficient liquidity produces partial fill or rejection**

Verify: Book size < order size results in `PARTIAL` status; remainder cancelled; cost basis matches filled portion only.

- [ ] **Step 3: Write test: Stale and out-of-order snapshots**

Verify: Snapshot older than `max_source_age_seconds` produces `STALE_PAIR` reason; no orders generated.

- [ ] **Step 4: Write test: Duplicate slot replay**

Verify: Replaying identical slot returns `DUPLICATE_SLOT`; no duplicate decisions or fills.

- [ ] **Step 5: Write test: Market rollover preserves positions as UNKNOWN_SETTLEMENT**

Verify: Rollover closes previous window as `UNKNOWN_SETTLEMENT`; open positions remain in `positions` table; never auto-settled.

- [ ] **Step 6: Write test: Restart with open paper positions**

Verify: Engine restart preserves existing positions and cost basis; marks equity correctly against new market.

- [ ] **Step 7: Write test: Unknown settlement never substituted with BTC spot price**

Verify: Window close sets status `UNKNOWN_SETTLEMENT`; no P&L is recognized without authoritative outcome.

- [ ] **Step 8: Write test: Trading fee calculation accuracy**

Verify: Fee is exactly `notional * fee_bps / 10000`; cash is deducted by `notional + fee` on BUY; cash credited by `notional - fee` on SELL.

- [ ] **Step 9: Write test: Daily loss limit breach enters RISK_STOPPED**

Verify: Cumulative realized loss >= `max_daily_loss` causes `RISK_STOPPED`; no further collection or execution.

- [ ] **Step 10: Write test: Emergency STOP file halts before collection**

Verify: `stop_path.exists()` halts engine immediately; returns `STOPPED`.

- [ ] **Step 11: Run all adversarial tests**

Run: `python -m unittest tests.test_linux_mvp_adversarial -v`
Expected: ALL PASS.

- [ ] **Step 12: Commit**

```bash
git add tests/test_linux_mvp_adversarial.py
git commit -m "test: add Phase 2A adversarial paper execution test suite"
```

---

### Task 4: Deploy Configuration & Service Unit Update

**Files:**
- Modify: `deploy/polymarket-btc-paper.service`
- Modify: `docs/linux-mvp-runbook.md`
- Test: `tests/test_linux_mvp_deploy.py`

**Interfaces:**
- Consumes: Service configuration
- Produces: Systemd unit configured for autonomous paper trading (`run` command)

- [ ] **Step 1: Update unit `ExecStart` from `ingest` to `run`**

- [ ] **Step 2: Update deploy tests to verify `run` is the default service command**

- [ ] **Step 3: Run deploy tests**

Run: `python -m unittest tests.test_linux_mvp_deploy -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add deploy/polymarket-btc-paper.service docs/linux-mvp-runbook.md tests/test_linux_mvp_deploy.py
git commit -m "feat(deploy): switch service command to run for Phase 2A paper trading"
```

---

### Task 5: Full Regression Testing (Offline)

- [ ] **Step 1: Run all Phase 1 and Phase 2 offline tests**

Run:
- `tests.test_linux_mvp_config`
- `tests.test_linux_mvp_service`
- `tests.test_linux_mvp_engine`
- `tests.test_linux_mvp_ingest`
- `tests.test_linux_mvp_store`
- `tests.test_linux_mvp_report`
- `tests.test_linux_mvp_deploy`
- `tests.test_linux_mvp_adversarial`

Expected: 100% PASS.

- [ ] **Step 2: Verify accepted CP5/CP6 source hashes are unchanged**

Verify 7 files:
- `src/guarded_full_window.py`
- `src/polymarket_bot/recorder/live_guard.py`
- `src/polymarket_bot/recorder/windows_job.py`
- `src/polymarket_bot/full_window.py`
- `tests/test_full_window.py`
- `src/polymarket_bot/paper.py`
- `src/polymarket_bot/strategy.py`

- [ ] **Step 3: Verify 25 pre-existing dirty worktree entries are untouched**

---

### Task 6: Bounded WSL2 Live Paper Trading Validation

**Files:**
- Target: Local WSL2 Ubuntu instance (already configured in Phase 1B)
- Deploy package: Built from committed tree only

- [ ] **Step 1: Build Phase 2A deployment package from committed HEAD**

- [ ] **Step 2: Deploy to WSL2 `/opt/polymarket-btc`**

- [ ] **Step 3: Update systemd unit and start service**

Command: `systemctl restart polymarket-btc-paper`
Command: `journalctl -u polymarket-btc-paper -f`

- [ ] **Step 4: Run bounded live trial (15–30 minutes)**

Monitor:
- Ingestion cycles
- Momentum calculations
- Decisions (WARMUP → HOLD / BUY)
- Any orders queued and executed
- Equity marks
- Resource usage (CPU, RAM via cgroups)

- [ ] **Step 5: Stop service and gather evidence**

- [ ] **Step 6: Run `polymarket_bot.linux_mvp report` and inspect DB**

Verify:
- Decisions recorded
- Orders recorded (if any signals fired)
- Equity recorded
- No live network trading calls
- 0 violations of safety constraints

---

### Task 7: Independent Review & Final Deliverable

- [ ] **Step 1: Dispatch independent review subagent**
- [ ] **Step 2: Produce comprehensive Phase 2A report**
