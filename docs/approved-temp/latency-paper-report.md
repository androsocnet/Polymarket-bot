# Latency-aware PAPER execution — milestone closure

## 1. Status: BLOCKED

Separate offline execution component built and tested. Full milestone blocked by missing historical execution depth and missing causal fixed-strategy/account replay. No claim of strategy returns or certified execution. No new network experiment.

## 2. Changed / unchanged

New `src/polymarket_bot/estimated.py`: isolated FAK/FOK estimator using same-session monotonic receipt timing, explicit local/exchange delays, first eligible subsequent book, original limit, visible level quantities, partial remainder cancellation, cash/held checks, market minimum/tick and per-market fee evidence. Missing evidence distinct from observed no-fill. Unknown upstream freshness remains UNKNOWN. Each session/market/condition/token permits only one selected-book execution per estimator, even with later receipts; no replenishment proof means no multi-order replay. Decimal inputs bounded to 50 digits and exponent ±50. Fee provenance requires supported model/raw schedule and temporal applicability; local wall receipt used only for metadata checks, never execution causality. Scenario extra costs remain separate from observed prices.

New `tests/test_estimated.py`: synthetic acceptance checks. New `tests/check_estimated_archive.py`: runnable read-only captured-book smoke/sensitivity check. Official semantics reviewed separately in `official-review-order-semantics.md`.

Strict engine, exact-next-slot certification, hard risk limits, terminal stops, production services/databases and existing source files unchanged. No wallet, credentials, live orders, deployment, merge or VPS work. Existing untracked user files preserved.

Component is not strategy admission, account manager, persistence or exchange matching simulator. Callers must enforce unchanged strategy/risk/account feedback. Not wired into existing engine; no accidental replacement of certification.

## 3. Model-estimated trades, net P&L, drawdown

| Dataset | Estimated strategy trades | Net P&L | Drawdown |
|---|---:|---:|---:|
| Four-hour holdout, proposed latency model | Unknown / BLOCKED | Unknown / BLOCKED | Unknown / BLOCKED |
| Recorded strict holdout | 0 certified fills; 965 expiries | 0 | 0, flat account |

Strict zeros measure nonexecution, not strategy edge. Do not reinterpret 965 rejected BUY requests as executable strategy trades: fills change holdings, exit decisions, cash, limits and subsequent entries.

Independent archive audit: 4,779 holdout cycles, 1,644 decisions, 3,288 observations, 17 markets; **zero persisted complete holdout depth books**. All decision observations match identity and monotonic timing through freshness events, but bid/ask quantities and depth were omitted. Older 684-cycle archive also lacks depth and per-book monotonic timing; its recorded 24 fills/flat 10bps fees are not evidence of realistic execution or modern fee correctness.

Only six complete archived token books exist, from three earlier independent smoke-time probes. None matches strategy ingestion receipts/source timestamps. They cannot substitute for missing holdout books.

## 4. Costs/delay sensitivity

Official docs conflict: crypto taker delay 150ms in changelog versus 250ms lifecycle text. Scenarios are assumptions, not measured transmission/matching calibration. Fee metadata rate 0.07, exponent 1, taker-only present. Reused fee helper floors five decimals per visible level; exchange match boundaries/rounding remain unverified. No fee grace assumed.

### Actual archived-book checks — not strategy replay

Command under WSL with `PYTHONPATH=src`:

```sh
/opt/polymarket-btc/.venv/bin/python -B tests/check_estimated_archive.py /home/androunix/polymarket-repeat-holdout-9l3zcdq4
```

Each probe treated as isolated session. Hypothetical DOWN BUY 5 shares, limit 0.60, cash 100, submitted at existing Gamma receipt; no reconstructed strategy signal. Explicit two-second model deadline, not exchange GTD. No cross-probe continuity assumed.

| Scenario | Local + exchange delay | Depth | Extra cost | Maximum spread | Probe 0 | Probes 1/2 |
|---|---:|---:|---:|---:|---|---|
| Base | 100 + 150ms | 100% | 0 | 0.04 | Estimated 5 @ 0.54; notional 2.70, fee 0.08694 | NO_FILL, original limit |
| Delay stress | 250 + 250ms | 100% | 0 | 0.04 | UNEVALUATED | UNEVALUATED |
| Pessimistic | 750 + 250ms | 50% | 25bps | 0.04 | UNEVALUATED | UNEVALUATED |
| Tight spread | 100 + 150ms | 100% | 0 | 0.005 | NO_FILL, spread constraint | NO_FILL |

Longer delays lack subsequent observed books before deadline. These are unknown fills, **not proven zero fills**. One isolated estimated buy is component evidence only; no sale/account replay or P&L.

### Synthetic fixture sensitivity — software checks only

Five-share hypothetical BUY, limit 0.55; fixed fixture receipts 300/600/1100ms after submission. Future quote changes prescribed by test, not market evidence.

| Scenario | Filled | Observed-level VWAP | Fee | Extra cost | Cash change |
|---|---:|---:|---:|---:|---:|
| 250ms ready | 5 | 0.508 | 0.08744 | 0 | −2.62744 |
| 500ms ready | 5 | 0.518 | 0.08734 | 0 | −2.67734 |
| 1000ms ready, 50% depth, 25bps | 1.5; cancel 3.5 | 0.54 | 0.02608 | 0.002025 | −0.838105 |

Delayed fixture VWAP rises 0.01/share, then 0.032/share relative to base. This demonstrates delay/slippage/depth/cost mechanics, not expected performance or market calibration. No invented historical spread, price, quantity or interpolation.

## 5. Usefulness

Execution mechanics now testable on full observed books. Historical fixed-strategy profitability evaluation **not yet useful**. Current data can reproduce accepted decision inputs and strict zero-fill accounting, not alternative latency fills or equity paths.

## 6. Genuine blockers

1. Missing per-cycle executable prices/sizes/depth, especially cycles rejected by strict timing. Quotes alone cannot establish quantity, partial fills or account equity.
2. No causal strategy replay with simulated cash/positions, exits, trade counts, settlement receipt ordering and unchanged hard-risk/terminal-stop feedback. Filling historical request list would be invalid.
3. Sparse independent probes cannot calibrate local-to-exchange delay, upstream freshness, queue competition or match-level fee rounding. Conservative assumptions must remain explicit even after fuller capture.

No certification redesign required for isolated estimates. No external capture launched under this milestone.

## 7. Git / tests

Base: `a05507b4449b37224935ed911389ced70d33cd69`, branch `fix/paper-freshness-clock`, existing linked worktree. Commit and final verification recorded at closure.

Initial RED: importing new estimator failed `ModuleNotFoundError: No module named 'polymarket_bot.estimated'`. Initial safety-suite child process lacked PYTHONPATH; corrected test environment, not product code. Final targeted run: 147 tests, 146 passed, one platform-specific Windows heartbeat test skipped under WSL, zero failures/errors; includes 19 estimator tests. Captured archive check: 12 scenario/probe evaluations, one isolated estimated fill, five observed no-fills, six UNEVALUATED. Independent review identified duplicate-depth, observation-deadline cancellation, Decimal precision and malformed-fee defects; regression tests reproduced failures before fixes. Independent re-review: all four findings resolved within isolated-order scope; no remaining Critical/Important defects. Reviewer independently passed 19 tests and reproduced 12 captured-book results. Full strategy acceptance remains BLOCKED.

Preservation: four existing evidence manifests validated; holdout DB SHA256 `3c4d6d0e6e87ad05e9657068540de85aaada7b4dc07fcbf409a53fb961e5fdb9`; older DB `68007827455fbcb2ed508f989830b93a8898ef58fcd7d4b6a56efcc9c9c94867`.

## 8. Single next milestone

**Depth-complete causal PAPER replay:** add separate evidence-preserving capture/replay path for full per-cycle books plus receipt/metadata/session identity, then replay fixed strategy with accounting/risk/settlement feedback across predeclared scenarios. Preserve strict engine; any new external capture requires explicit bounded scope. Do not rerun four-hour test without closing data-persistence gap first.
