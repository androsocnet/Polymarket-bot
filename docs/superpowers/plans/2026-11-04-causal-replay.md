# Causal PAPER Replay Implementation Plan

> **For agentic workers:** Use executing-plans or subagent-driven-development; user explicitly requests autonomous implementation without approval pauses.

**Goal:** one bounded full-depth public recording and deterministic offline fixed-strategy replay; never certified fills.
**Architecture:** separate gzip JSONL event recorder wraps existing LiveCollector/PublicGet hooks. Offline replay reuses PaperEngine._decide and MvpStore safety/accounting in disposable SQLite only; separate estimated execution feeds accounted level fills, without editing strict engine. Preserve strict freshness gating for strategy decisions, estimates never upstream-freshness certification.
**Tech:** Python stdlib, installed httpx, existing collector/fees/store/estimator; WSL.
**Spec:** current user AUTONOMOUS MILESTONE Complete Causal PAPER Replay.

## Constraints
PAPER_ONLY. No credentials/live orders/production/VPS/merge/deploy. One capture only: target 2100s active, hard total 2700s, <=850 cycles, <=4500 actual HTTP attempts, every rolling 60s <=180, compressed <=262144000 bytes, serial no catchup. Original safeguards unchanged. Independent review before capture; offline tests before capture. Preserve existing user files.

## Review focus
- Attempt callbacks must not duplicate guard counts; count at actual send boundary incl retries.
- gzip complete frames append-only and bounded before write, failed attempts/cycles preserved; partial-tail detection.
- Unknown execution gaps must stop scenario or carry unknown, never fabricate zero account change as fact.
- Repeated observed snapshots are not proven replenishment; prevent reuse of depth at same source identity/hash, carry prior own consumption conservatively.
- Settlement receipt availability, stale marks, loss stops, hard risk admission retained; no future settlement in prior decisions.

## Tasks
- [ ] 1 Capture: `src/polymarket_bot/depth_capture.py`, `tests/test_depth_capture.py`. RED/GREEN gzip fidelity including bytes/Decimal/time, incomplete/failed cycles, rate/attempt/size/time bounds; original collector reused. CLI output new isolated /home/androunix temp root. Record raw attempts and successful serialized snapshots, freshness evaluation, local decision timestamp, resolution responses and terminal summary. No authenticated calls.
- [ ] 2 Replay: `src/polymarket_bot/causal_replay.py`, `tests/test_causal_replay.py`. Deserialize recorded dataclasses safely; reuse fixed decisions/settings/safety and store accounting. Replay only temporal-prefix events; estimated execution from subsequent receipt with fee/identity provenance, explicit latency scenarios, conservative own-depth debit. Recompute state-dependent decisions, buys/sells/partial/no-fill/cancel/rollover/verified settlement. Unknown fills halt evaluation explicitly. Deterministic report incl coverage, trades/round trips/unresolved/gross/fees/extra cost/spread/slippage/net/observed drawdown.
- [ ] 3 Independent review of capture controls, baseline targeted tests, ONE authorized bounded capture in background. No retry launch. Use remaining time for replay tests/integration and review.
- [ ] 4 Replay frozen capture twice across declared scenarios, account reconcile/integrity, no-lookahead prefix tests. Targeted/full relevant suites. Audit caps and archive hashes. Independent final review, fix defects, commit targeted files, final nine-section report.

Scenarios: optimistic local100ms+exchange150ms, conservative local250ms+exchange250ms, pessimistic local750ms+exchange250ms plus 50% displayed depth and 25bps extra cost; original limit .99 BUY/.01 SELL (existing marketable intent), explicit bounded observation deadline. No profit inference from one capture. Any limits reached stop once, preserve evidence, no automatic repeat.
