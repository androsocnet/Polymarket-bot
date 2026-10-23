# Phase 2B public resolution contract

Read on 2026-10-23. No authenticated requests or on-chain transactions.

Sources:
- https://docs.polymarket.com/api-reference/markets/get-resolution-state.md
- https://data-api.polymarket.com/v2/openapi.json
- https://docs.polymarket.com/concepts/resolution.md
- https://docs.polymarket.com/migrate/data-api-v1-to-v2.md

Documented semantics:
- GET /v2/resolutions is public, no authentication. Exactly one selector family; condition selects Gamma condition IDs, at most 20. Nonpaginated `data` envelope; miss is `data: []`.
- Condition-grain rows prefer native V2 lifecycle, then UMA, then terminal CTF state.
- `payouts`: per-outcome payout in micro-USDC per share `[outcome0, outcome1]`, present on resolved condition-keyed rows. 1000000 therefore means $1/share, not arbitrary positive payout.
- `status`: lifecycle includes unresolved proposed/challenged/disputed states and `resolved`.
- `resolved_block`, `resolved_at`, `transaction_hash`, `log_index`, `resolution_source` carry provenance where available. `resolution_source` is reported (oracle) or derived (neg-risk sibling).
- `extended_review` true means managed proposal remains under review. `was_disputed` is historical, not necessarily current; MVP conservatively rejects it even if resolved.
- Official resolution concept states BTC up/down uses Chainlink TWAP; winning tokens redeem at $1, losing at $0. Never calculate winner from local BTC spot data.

Verification policy:
- Require exact Gamma market ID, condition ID, BTC 15m slug/time identity, unique UP/DOWN outcome-token mapping matching persisted observations and orders.
- Use source outcome order, never default UP index.
- Require resolved condition row, binary unit payout, no review/dispute/update ambiguity, valid transaction/block/time provenance; require closed resolved Gamma agreeing on final binary outcome.
- Missing/contradictory/split evidence remains UNKNOWN_SETTLEMENT. Keep original response text with retry records and successful settlement atomically.
- This is official-public-API-attested PAPER accounting, NOT an independent Polygon receipt/finality proof and NOT a real redemption. Reorg or compromised upstream correctness is not independently verified.

Historical live response inspected in earlier session:
- condition 0x785a3f10888922ac1c58978c32a01e36c9032effb0756dcdaf21a600c09dee87
- status resolved, payouts [1000000,0], transaction 0xeda480b1ff1d40e2865203c143b43d0cf66b69912dd8d4592c797ebe589c7470
- resolved block 96006739, resolved_at 2026-10-23T00:31:20Z, resolution_source reported, log_index 1282.
- Historical shell output is not a substitute for durable raw fetch evidence during validation.
