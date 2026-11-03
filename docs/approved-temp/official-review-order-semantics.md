# Official Polymarket CLOB order-semantics review (approved-temp)

Read-only offline docs review for PAPER execution truth. Sources: current `docs.polymarket.com` pages, `help.polymarket.com`, Wayback captures of pre-CLOB-V2 docs pages, official changelog. No market-data calls or services/DB writes. This review document is repository output. Reviewed: 2026-11-04.

## 1. Order types, lifetime, cancellation

- Current: `https://docs.polymarket.com/concepts/order-lifecycle.md`
- All orders are limit orders; "market orders" are marketable limit orders. Order types GTC / GTD / FOK / FAK. Post-only: order that would match immediately is rejected, never takes.
- GTD precision (`trading/place-orders.md`, Limit Orders note): expiration is Unix seconds; **expires one minute before stated expiration** (security threshold); expiration must be **at least 3 minutes in the future**; minimum effective lifetime ≈ 2 minutes. Example formula `now + 60 + N`. Historical match: archived `create-order` page (capture 2025-11-12): "There is a security threshold of one minute. If the order needs to expire in 90 seconds the correct expiration value is: now + 1 minute + 30 seconds."
- Market orders: `orderType` = FAK (default in SDK) or FOK, top-level request field, not part of signed EIP-712 data; `expiration` must be `"0"`; never rest — unfilled remainder canceled.
- FOK: full fill or nothing; error strings: `order couldn't be fully filled. FOK orders are fully filled or killed.`; FAK with zero match: `no orders found to match with FAK order. FAK orders are partially filled or killed if no match is found.` (`resources/error-codes.md`, Place Orders).
- Cancellation (`trading/manage-orders.md`): cancel available anytime before match, also in cancel-only mode; **in a pending delay window a marketable order cannot be canceled**; partial fills cannot be cancelled — only unfilled remainder; batch cancel up to 3,000 IDs (changelog Jun 15 2026: DELETE /orders batch max now 1,000); per-market cancel via asset_id or condition id.
- Balance reservation formula (order-lifecycle): `maxOrderSize = balance - Σ(openOrderSize - filledAmount)`.

## 2. Minimum order size and tick size

- `market-data/market-details.md` (Trading Constraints): tick sizes `0.1, 0.01, 0.005, 0.0025 (World Cup markets only), 0.001, 0.0001`; "Orders smaller than the minimum order size are rejected." Gamma fields `orderPriceMinTickSize`, `orderMinSize` — Gamma docs describe `orderMinSize` as "Minimum order size in USDC"; SDK `MarketTrading.minimumOrderSize` example `"5"`.
- `trading/place-orders.md` order-book example: `"min_order_size": "5"` on `GET /book`, described as "the minimum number of shares the CLOB accepts for an order"; both examples treat 5 as **5 shares** (10-share order "above the minimum of 5"; market-order example "exceeds the five-share minimum").
- RECOMMENDED FOR SIMULATOR: treat min order size as 5 shares (matches CLOB `/book` field, which is what a simulator reads); note Gamma doc wording says USDC — documented discrepancy between Gamma-field wording and CLOB-book semantics.
- Rejection strings (`resources/error-codes.md`, 400): `order {id} is invalid. Price ({price}) breaks minimum tick size rule: {tick}` and `order {id} is invalid. Size ({size}) lower than the minimum: {min}`.

## 3. Taker/matching delays (official tier history)

- Current `concepts/order-lifecycle.md` (Match or Rest): two delay classes:
  - **Taker delay**: "used on selected crypto and finance up/down markets. The order is held for 250 ms, then validation runs again and the order is matched or placed on the book." Check per market: `GET https://clob.polymarket.com/clob-markets/{condition_id}` → `itode: true`. API waits for the hold and returns the final result.
  - **Sports/game delay**: "enabled on configured sports markets around live game conditions. The order waits for the market's configured delay window before matching." Per-market value: Gamma `secondsDelay` (integer seconds; 0 = none) — `market-data/market-details.md#market-timing`.
  - During either delay: order pending, **cannot be canceled**; if market/balance/allowance/risk checks fail at delay expiry the order is rejected.
- Official delay-tier changes (changelog/predictions):
  - Sep 4 2026, 14:00 UTC: crypto taker delay `150ms`, up from `50ms`.
  - Aug 17 2026, 11:00 UTC: crypto taker delay `50ms`, down from `250ms`.
  - NOTE: current order-lifecycle page still states `250 ms` — stale vs changelog; changelog is the authoritative tier history (250 ms → 50 ms → 150 ms, effective dates above).
- No published official tier *list* of sports `secondsDelay` values exists; docs direct integrations to read `secondsDelay` per market. Simulated delay tiers: crypto 150 ms; sports = per-market `secondsDelay` value; everything else 0.
- Insert statuses (`trading/place-orders.md`, also archived create-order page): `live`, `matched`, `delayed` ("order is marketable but subject to a matching delay"), `unmatched` ("marketable but failed to delay. Placement still succeeded."). Archived error row: `ORDER_DELAYED` / `order match delayed due to market conditions`, `DELAYING_ORDER_ERROR`, also in current error-codes page (400).

## 4. Fee regime, rates, grace period

- Current: `trading/fees.md` and Help Center article "Trading Fees" (modified 2026-07-10, `help.polymarket.com/en/articles/13364478-trading-fees`).
- Formula: `fee = C × feeRate × p × (1 - p)`, C = shares, p = price (fee in USDC, symmetric around 0.50; at 0.50 fee = rate/2 × notional, e.g. crypto 0.07 → $1.75 per 100 shares). Takers only; **makers never pay fees.** Applied at match time by the protocol — no fee fields in the order (CLOB V2 removed `feeRateBps`, changelog Apr 17 2026).
- Per-category rates (current, post-Jul 10 2026): Crypto 0.07 (rebate 20%), Sports 0.05 (15%), Finance 0.04 (25%), Politics 0.04 (25%), Economics/Culture/Weather/Other 0.05 (25%), Mentions/Tech 0.04 (25%), Geopolitics 0 (fee-free), Combos 0.05 (own rate, 0% maker rebate — `trading/fees.md` table).
- **Fee-free (grace-period-free) regimes vs original schedules** — official history (changelog/predictions):
  - Jan 5 2026: taker fees first enabled, 15-minute crypto markets, "peak at 1.56% at 50% probability" (= rate 0.0312→ effectively the original crypto schedule); maker rebates funded by taker fees.
  - Feb 12 2026: 5-minute crypto markets with fees, "same curve as 15-minute".
  - Feb 11 2026: NCAAB + Serie A sports fees announced for Feb 18 2026; rebates calculated **per market**.
  - Mar 1 2026: fees extend to **all** crypto markets (1H, 4H, daily, weekly), "Only **new markets created after March 6** are affected."
  - Mar 30 2026: **"Fee Structure V2"** — the current category table (Crypto 0.07, Sports 0.03 initially, etc.); Geopolitics stays fee-free.
  - Jul 10 2026: Sports fee 0.03 → **0.05**, maker rebate 25% → **15%** (midnight UTC; archived May 23 2026 capture of fees.md confirms prior Sports 0.03/25%).
- **Fee grace period**: no official grace period exists for trading fees themselves. The word "grace" appears in the fee program only in the Taker Rebate Program (`programs/taker-rebates.md` FAQ): "Your tier moves down after a **short grace period** if your 30-day Weighted Volume drops below your current tier's threshold" — no duration published. Fee-applicability grandfathering is expressed instead as creation-date cutoffs in the changelog (e.g., crypto expansion applies only to markets created after Mar 6 2026). Simulator implication: fee regime must come from each market's `feesEnabled` + `feeSchedule` descriptor (as already modeled in `docs/paper-market-fees.md`), never from category constants alone.
- Fee precision (fees.md): rounded to 5 decimals; smallest chargeable fee 0.00001 USDC; below that rounds to zero.
- Fee metadata read path (`market-data/market-details.md`): Gamma `feesEnabled`, `feeSchedule { rate, exponent, takerOnly, rebateRate }` (CLOB V2/SDK exposes fee details at match time; archived May 2026 fees.md also showed `getClobMarketInfo(conditionID)` → `info.fd = { r, e, to }`).
- Changelog Mar 31 2026: "Fees should now be calculated using the `feeSchedule` object within a market."

## 5. Matching, price improvement, match-efficiency rules

- `concepts/order-lifecycle.md` (Maker vs Taker): "Price improvement always benefits the taker. If you place a buy order at `$0.55` and it matches against a resting sell at `$0.52`, you pay `$0.52`." Maker fee rate is 0, so maker P&L unaffected by fee regime.
- Market-order mechanics (`trading/place-orders.md`): BUY amount is pre-fee USD notional; fees charged on top; `maxSpend`/`max_price`/`min_price` caps; SDK rounds BUY shares **down** so `makerAmount / takerAmount` sits at-or-above maxPrice by less than the finest tick, guaranteeing fill at the resting ask's price; example: $10 BUY max price 0.52 → 19.2307 shares, effective price ≈ 0.5200019.
- Amount/precision table (same page): per tick size, price/size/amount decimals; encode amounts as 6-decimal integers. Fee-adjusted spends that can't hold the bound raise `UserInputError`.
- "Match efficiency" model implication: taker receives best resting price, fills walk the book across multiple makers; breakdown of which maker matched what is only discoverable after the fact via `GET /data/trades` (`makerOrders[]` with `matchedAmount` per maker) — no upfront knowledge of counterparty composition exists in any current or archived page inspected.

## 6. Fill and partial-fill reporting

- Placement response (`/order`, `/orders`): `success`, `errorMsg`, `orderID`/`orderId`, `status`, `makingAmount`, `takingAmount`, `transactionsHashes`, `tradeIDs`/`tradeIds`. `delayed` response has `"0"`/empty amounts and empty `tradeIDs` — "Treat it as a pending order rather than a fill" (place-orders.md).
- Jul 24 2026 (changelog 04:00 UTC rollout): async commit pipeline; `POST /order(/orders)` no longer returns `transactionHashes` on FAK/FOK matches — only `tradeIDs`; custom integrations poll trades by tradeID until hash or `FAILED`; SDKs resolve hashes via `waitForOrderFillSettlement()` (waits ≤30 s; returns immediately when `tradeIds` empty; does not wait for `delayed` matching).
- Trades: `trading/manage-orders.md` ClobTrade: `traderSide` TAKER|MAKER, `size`, `price`, `status` (`TRADE_STATUS_MATCHED` etc.), `bucket_index`, `transaction_hash`, `maker_orders[]` (`matched_amount`, `fee_rate_bps`), `matchedAt`/`updatedAt`.
- Trade statuses (order-lifecycle + archived trades-overview, identical): `MATCHED` (no) → `MINED` (no) → `CONFIRMED` (terminal yes) / `RETRYING` (no) / `FAILED` (terminal yes). Bucket splitting: "execution of a 'trade' must be broken into multiple transactions ... separate trade entities ... reconcile ... same market_order_id, match_time and incrementing bucket_index" (archived `/developers/CLOB/trades/trades-overview`, capture 2025-06-15).
- WebSocket: `trading/realtime-order-updates.md` — auth user channel carries order+trade events; fill updates are the authoritative async feed for delayed/matched orders.

## 7. Timestamps and status codes

- Signed order `timestamp` field: **Unix milliseconds**, used for order uniqueness; `salt` JSON-serializable number (keep < `Number.MAX_SAFE_INTEGER`). GTD `expiration`: Unix seconds. CLOB auth `POLY_TIMESTAMP`: Unix seconds; HMAC-SHA256 L2 signature over `timestamp + "POST" + path + body` — all in place-orders/manage-orders pages.
- REST reads: order `created_at` epoch seconds (manage-orders API example); trades `match_time`/`last_update` epoch seconds (archived Get Trades shape also used seconds). Wire caveat for simulator: two timestamp granularities (ms on signed orders, s on server responses).
- Status codes (`resources/error-codes.md`): 400 invalid/insufficient/crosses-book/FOK-unfilled/market-not-ready; 401 auth; 404 not found; **425 Too Early** matching-engine restarting (`Retry-After`); 429 rate limit; 500 `order timed out` (rejected before book, safe to resubmit), `the trade contains rounding issues`, price-discrepancy-vs-worst-maker error; 503 exchange paused / cancel-only / post-only (`code: post_only_mode`, `retry_after_seconds`). Internal override: message containing `not found`→404, `unauthorized`→401, `context canceled`→400.
- Matching-engine restarts (`trading/matching-engine.md`): after restart, **post-only mode for two minutes** (cancels allowed; new orders must be post-only maker orders); HTTP 425 + `Retry-After` during restart; 503 `post_only mode` body during the 2-minute window.
- Trading rate-limit tiers (`api-reference/trading-rate-limits.md`): ??? Not delay tiers — volume tiers Standard/Copper/Bronze/Silver/Gold/Platinum/Diamond/Elite per-signer token buckets; separately available under programs. For PAPER: irrelevant to match semantics, record only as backpressure constraint.

## 8. "Execution cannot be known upfront" — checked, not found verbatim

- Requirement was to check whether any historical official version states that upfront knowledge of how an order matches is impossible. **No such sentence was found** in the inspected current or archived pages: current `concepts/order-lifecycle.md`, `trading/place-orders.md`, `trading/manage-orders.md`; archived captures 2025-06-15 / 2025-11-12 / 2025-12-04 of `/developers/CLOB/introduction`, `/Clob-FAQ`, `/developers/CLOB/orders/orders`, `/developers/CLOB/orders/create-order`, `/developers/CLOB/trades/trades-overview`; archived 2026-05-23 `trading/fees.md`; py-clob-client README (archived repo).
- Closest official statements that carry the same intent for a simulator:
  - Archived `create-order` (2025-11-12): marketable orders "are executed on input at the best price" — no counterparty or fill-depth guarantee stated.
  - Current place-orders.md: delayed status returns no fills; "Treat it as a pending order rather than a fill"; market-order estimate "is a snapshot ... can change before you submit"; "If the book moves before submission, the order may fill only partially or not at all."
  - Maker-composition breakdown only appears post-hoc in `GET /data/trades` `maker_orders[]`.
- Conclusion for coordinator: match-at-ahead-of-time guarantees are nowhere promised in official docs; simulator must assume best-price-at-match-time only, with delay markets additionally paying the recipe: accept → `delayed` → re-validate → match/rest/reject.

## 9. Direct simulator anchors (PAPER truth)

| Behavior | Value | Source |
| - | - | - |
| Min order size | 5 shares (`/book` `min_order_size`) | place-orders.md, market-details.md |
| Tick sizes | 0.1 / 0.01 / 0.005 / 0.0025 / 0.001 / 0.0001 | market-details.md |
| Fee | `C × rate × p × (1−p)`, taker only, 5-dp, min 0.00001 | fees.md |
| Crypto taker delay | 150 ms (was 50 ms, 250 ms) | changelog Sep 4 / Aug 17 2026 |
| Sports delay | `secondsDelay` seconds per market; uncancellable while pending | order-lifecycle.md, market-details.md |
| Prices | taker gets resting price (improvement to taker) | order-lifecycle.md |
| FOK / FAK | all-or-nothing / partial-accept-rest-killed | place-orders.md, error-codes.md |
| Statuses | live, matched, delayed, unmatched; trades MATCHED/MINED/CONFIRMED/RETRYING/FAILED | order-lifecycle.md |
| Engine restart | HTTP 425, then 2-minute post-only mode | matching-engine.md, error-codes.md |
