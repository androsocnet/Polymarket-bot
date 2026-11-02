# Forward PAPER market fees

Applies to `polymarket_bot.linux_mvp`, starting with fee model
`ctf-clob-v2-taker-level-floor5-v1`. No live orders or deployment authorization.

## Supported model

Gamma `version: "v1"` identifies CTF tokens, **not legacy CLOB V1**.
Modern CLOB V2 CTF markets after the April 28, 2026 cutover use collateral
fees on both sides. Protocol-V2/Gamma `v2` positions and earlier fee regimes
are unsupported here.

Each current market descriptor must explicitly supply `feesEnabled` and,
when enabled, `feeSchedule.rate`, integer `exponent: 1`, `takerOnly: true`.
Rate must be finite and in `[0,1]`. This deliberately narrow ceiling excludes
other exponents and maker/builder executions. An explicit `feesEnabled: false`
without a contradictory schedule supports zero platform fees; missing fields
never mean zero. Category defaults, `makerBaseFee`, `takerBaseFee`, and old
`feeRateBps` are not substitutes for `feeSchedule`.

For each visible execution level:

```
raw_fee = gross_shares * rate * price * (1 - price)
```

Examples at rate `.07`, 100 shares:

| Price | Notional | Fee | BUY cash spend | SELL proceeds |
|---|---:|---:|---:|---:|
| .20 | 20 | 1.12 | 21.12 | 18.88 |
| .50 | 50 | 1.75 | 51.75 | 48.25 |
| .80 | 80 | 1.12 | 81.12 | 78.88 |

BUY credits full shares and capitalizes fee into basis. SELL deducts fee from
proceeds and allocates existing fee-inclusive basis proportionally. Partial
fills charge only executed quantity; remainder stays abandoned. Fees summed
across actual prices, never computed from VWAP. Fifty shares at `.20` plus
fifty at `.80` cost `1.12` fees, not VWAP-based `1.75`.

**Rounding limitation:** official docs specify five decimals and sub-`.00001`
fees becoming zero, but inspected sources do not establish direction, ties,
or match/batch aggregation. PAPER policy is **floor to five decimals per visible
level, then sum**. Visible levels are not proven exchange match boundaries.
Results therefore say `MARKET_SCHEDULE_ESTIMATE`, never exchange-exact or
verified profitability. Fees denominated in modeled **pUSD collateral**;
older docs still call units USDC. No maker/taker rebates or builder fees assumed.

Conservative sizing preserves the old $1 price ceiling and `.01` share step,
reserving `1 + rate/4` collateral per share. Risk limits unchanged; fees reduce
quantity rather than enlarge risk budget. `MVP_FEE_BPS` environment overrides
now fail configuration explicitly. Retained `Settings.fee_bps` exists only for
legacy config/API compatibility and has no forward sizing/execution authority.
Offline `paper.py`/`strategy.py` remain legacy flat-notional replay tools already
labelled `ASSUMED_NOT_VERIFIED`; they are not this market-fee model.

## Evidence and fail-closed behavior

Decision inputs persist selected Gamma fields, identities, observation and
snapshot times, source, model, collateral, role and rounding policy. New
`order_fees` side table stores immutable queued basis, execution evidence and
actual price/quantity/notional/fee per level atomically with accounting.
Opening an existing database adds this table without rewriting monetary rows
or run schema headers. Read-only reports introspect its presence; old databases
need no migration to report.

Missing/unsupported metadata blocks new orders, preserves computed signal and
records `FEE_UNVERIFIED`. Missing/changed execution schedule or legacy pending
order without queued evidence is rejected, never silently filled free or at
10 bps. This includes exits: unknown exit costs leave inventory explicit.
Next-slot expiry, STOP and persistent `FAILED_STOP`/`RISK_STOPPED` stay enforced.
Repeated observations may change `updatedAt` without changing fee terms;
changed fee terms reject queued execution. Same authoritative Gamma descriptor
is the source; this is not independent CLOB/chain attestation or proof of
historical schedule stability.

Reports separate recorded fee totals, provenance coverage and bookkeeping
reconciliation. Any filled legacy row leaves account status
`LEGACY_ESTIMATE_UNVERIFIED`; historical amounts never recomputed from current
metadata. `performance.profitability_verified` is always false: exchange fee
rounding, execution realism and operating costs are not verified.

## November 2 preserved session

Original evidence opened only using immutable, read-only SQLite. No rewrite.
Archived resolution-time Gamma metadata:

| Market | Version | Rate | Exponent | Taker only | Metadata updated UTC |
|---|---|---|---|---|---|
| 6512238 | v1 | .07 | 1 | true | 2026-11-02 09:02:29.075801 |
| 6512467 | v1 | .07 | 1 | true | 2026-11-02 09:16:49.753065 |
| 6513072 | unknown | unknown | unknown | unknown | unavailable |

Both archived markets have `feesEnabled: true`, `feeType: crypto_fees_v2`.
These captures occurred after their trading windows, not proof of unchanged
fees during fills. Full execution depth also absent. **Corrected historical
net P&L cannot be verified.** No current fee fetched as historical substitute.

Recorded legacy results remain: gross `-4.8451`, assumed 10-bps fees
`.1174325`, net `-4.9625325`, final cash `95.0374675`, no inventory.
Earlier fixed-fill sensitivity `-8.31664` uses uniform `.07`, VWAP and
per-order nearest rounding assumptions. It is **not corrected historical
performance**, not this level-floor policy, and not a strategy rerun.
Operating costs unknown/excluded; no profitability established.

Original SQLite SHA256:
`68007827455fbcb2ed508f989830b93a8898ef58fcd7d4b6a56efcc9c9c94867`.

## Verification scope

Targeted forward suite: 183 tests, 182 passed, one skipped; includes persistent
terminal-stop regressions and 12 fee tests. Parsing/discovery/snapshot: 50 passed.
Independent review approved after production Decimal metadata serialization and
endpoint-price regressions were fixed. Cash/basis/realized identity checked in
transactions and synthetic multi-price partial BUY/SELL tests.

Repository-wide discovery is **not green**: full attempts timed out at 120 and
600 seconds. Fail-fast reproduces existing
`test_cp57_arm_serialization_audit.ArmSerializationAuditTests.test_retained_numeric_event_reaches_actual_arm_before_publication`
error because untracked historical fixture
`data/cp5_6c_replacement_20261017_0351adeb-6594-492e-9237-adf7d833dba0.sqlite3`
is missing. Test unchanged from safety base; fixture absent from base Git tree.
No fake fixture added, no broad-suite PASS claimed. No deployment or merge.

## Official sources inspected November 2, 2026

Mutable docs are current observations, not historical snapshots:
- https://docs.polymarket.com/trading/fees.md
- https://docs.polymarket.com/market-data/market-details.md
- https://docs.polymarket.com/migrate/polymarket-v2/api-integrations.md
- https://docs.polymarket.com/v2-migration.md
- https://docs.polymarket.com/changelog/predictions.md

Pinned official source (not deployed-bytecode attestation):
- SDK generalized curve: https://github.com/Polymarket/ts-sdk/blob/a8ce0451eac85992c8367231d80ab81e1b653fa2/packages/client/src/actions/orders/market.ts
- Modern CTF collateral fees: https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Trading.sol and sibling `Fees.sol`.
- Protocol V2: https://github.com/Polymarket/polymarket-v2-external/blob/741f8bbe88c3f29a1c55d1248a3dd411f622f111/src/exchange/Exchange.sol
- Legacy, deliberately not used: https://github.com/Polymarket/ctf-exchange/blob/ed5c7708b7be3aa98bf5f0c6602b57cc498e2ef4/src/exchange/libraries/CalculatorHelper.sol

Legacy signed-bps fees use a different min-price curve, BUY share deduction
and integer floors. They must not be inferred from Gamma's `version: v1`.
