# PAPER execution evidence

Forward PAPER execution uses the book supplied to `MvpStore.execute_pending`, not the independent full-depth recorder or the offline latency estimator. Historical fills without retained execution depth remain **UNVERIFIED**; top-of-book prices cannot reconstruct quantities.

`execution_evidence` links `order_id` to JSON with execution-side levels, consumed quantities, fee input, outcome and account changes. Join `orders` for market, token, requested quantity and decision timestamp; join `events` using `freshness_event_id` for timing evidence. `CAPTURED` means levels were recorded, not independently verified or guaranteed available. `UNVERIFIED` means that claim is unavailable. Legacy fills with no row are UNVERIFIED, not proof of zero depth. Evidence and terminal order updates share a transaction; a pending order is not a terminal execution attempt.

The forward engine walks displayed asks for BUY and displayed bids for SELL. This is a hypothetical depth walk, not proof of exchange execution, queue position, or liquidity remaining when an order would reach the exchange. The forward engine does not debit previously simulated consumption from later snapshots. Repeated execution against the same displayed liquidity can overstate executable quantity; an audit must account for this limitation and must not treat independent book evidence as proof of replenishment.

A pre-existing window-expiry branch can leave an order PENDING until a later rollover, settlement, or expiry path rejects it; if collection stops first, no terminal execution attempt exists. This change does not alter window or execution rules.

Separate offline replay limitation: `estimated.py` reuse protection keys by session, market, condition, token, and side rather than selected snapshot. Repeated same-side replay orders for that identity can raise `selected snapshot reused` even when a later snapshot exists. This is not the forward PAPER execution path; no replay or strategy behavior is changed here.
