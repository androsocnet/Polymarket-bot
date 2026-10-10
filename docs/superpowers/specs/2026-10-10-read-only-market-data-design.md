# Read-only BTC 15-minute market-data subsystem

Date: 2026-10-10 (Asia/Jakarta)

Status: Written specification awaiting user review. The conversational
architecture was approved with amendments; this document and any subsequent
implementation plan require separate approval before implementation.

## 1. Objective and success criteria

Build a Python subsystem that discovers the Polymarket BTC 15-minute Up/Down
market whose observation window contains the current time, retrieves its
public metadata and both outcome order books, prints one REST snapshot, and
exits. The snapshot is information for a future paper-trading project; it is
not a trade, recommendation, signal, or simulated execution.

Success means the displayed market passes strict family, time, status, and
identifier validation; both books belong to its condition and their respective
outcomes; prices come from those books; and timing limitations are explicit.
Failures produce clear diagnostics rather than a guessed market or price.

The current repository has Python 3.12 and uv, an empty dependency list,
`src/main.py` containing only a startup check, and placeholder test/data/log
folders. Existing RTK and Superpowers configuration is outside this subsystem.

## 2. Scope and exclusions

In scope:

- Dynamic Gamma discovery with a timestamp-slug fast path and a mandatory,
  independently usable filtered-list fallback.
- Normalized event/market metadata and explicit Up/Down-to-token mapping.
- Public CLOB server time and separate REST order books for both outcomes.
- Decimal-based best bid, best ask, midpoint, spread, and visible availability.
- One snapshot per invocation, bounded request handling, and one rollover retry.
- Offline automated tests with injected HTTP responses and clocks, plus a
  separately invoked public live smoke check.

Excluded:

- Wallets, private keys, authentication, API credentials, order creation,
  cancellation, funding, or any money movement.
- Trading strategies, signals, position sizing, paper-trading engines, fills,
  balances, fees applied to simulated trades, or profit calculations.
- WebSockets, continuous polling, background services, BTC/USD reference-price
  feeds, persistence, dashboards, additional assets, and other market durations.
- SDK migration, speculative protocol abstractions, unrelated refactoring,
  changes to Superpowers/RTK, or global Codex configuration.

Only public GET requests are exposed by the application transport. This
milestone has no authenticated client and no write/order API methods.

## 3. Evidence and assumptions

### Official API contracts

The design uses these official references, checked on 2026-10-10:

| Reference | Design responsibility |
| --- | --- |
| [Discover markets](https://docs.polymarket.com/market-data/discover-markets) | Public Gamma event/market discovery and slug lookup |
| [List events](https://docs.polymarket.com/api-reference/events/list-events) | Status filters, end-time bounds, limit/offset pagination |
| [Market details](https://docs.polymarket.com/market-data/market-details) | IDs, outcome labels, JSON-encoded arrays, status metadata |
| [Get server time](https://docs.polymarket.com/api-reference/data/get-server-time) | CLOB `/time` |
| [Get order book](https://docs.polymarket.com/api-reference/market-data/get-order-book) | Token request parameter and book identity/level fields |
| [Prices and order books](https://docs.polymarket.com/market-data/prices-order-books) | Public pricing concepts and order-book reads |
| [Real-time data](https://docs.polymarket.com/market-data/realtime-data) | Future public market WebSocket boundary, deferred here |

### Verified live observations from the preceding architecture research

Unauthenticated requests on 2026-10-10 found a BTC 15-minute event through both
computed-slug lookup and a time-filtered event list. Its market returned
`outcomes` containing `Up` and `Down`, two `clobTokenIds`, `conditionId`,
`eventStartTime`, and `endDate`. Each returned token successfully fetched a
CLOB book whose `asset_id` and `market` matched the token and condition.
The observed market carried `version: v1`; `positionIds` was null.

Observed family names were `btc-updown-15m-<timestamp>` for event slugs and
`btc-up-or-down-15m` for the series slug. These are configurable recognition
conventions, not guaranteed naming contracts or fixed market IDs.

The market's `startDate` preceded the actual window by about a day. Its
`eventStartTime` matched the slug's window-start timestamp and its window
ended 900 seconds later. Therefore `startDate` and creation timestamps do not
define the observation window.

The event-list endpoint rejected the query `recurrence=15m`, even though the
returned series reported a 15-minute recurrence. Do not send that filter.
Filter by documented status/end-time parameters and validate duration locally.

Live books contained decimal price/size strings and a millisecond Unix
timestamp string. `/time` returned Unix seconds. Keep these units separate.
The API reference describes the book timestamp without a strong unit guarantee;
the millisecond interpretation is a verified adapter assumption, checked again
by the future live smoke check and rejected if incompatible.

These observations establish feasibility, not permanent naming, schema, or
availability guarantees. Neither the current research IDs nor its prices
are configuration defaults or production test expectations.

### Outcome identifier policy: approved amendment

For the verified target, parse Gamma's `clobTokenIds` and send those exact
strings as CLOB `token_id`. Verify returned book identity before using prices.
Store IDs as strings; never route them through float conversion.

Keep `version`, `positionIds`, and other identifier fields as optional metadata.
Do not add a protocol-v1/v2 selector or silently substitute position IDs in
this milestone. The official details page documents other position systems;
this specification does not claim that CLOB token IDs cover every Polymarket
market. The target's verified token behavior supports the smaller design.

If the target stops returning usable `clobTokenIds`, report an unsupported
identifier shape. A book 404 is `book_unavailable`; failed book identity
verification is `book_identity_mismatch`. Any replacement-field support
requires official evidence applicable to
the target, verification against CLOB, and a reviewed spec amendment. Never
infer an identifier's purpose solely from its presence or numeric appearance.

## 4. API ownership and allowed requests

| Responsibility | Source |
| --- | --- |
| Event discovery, titles, rules, window fields, status | Gamma |
| Event ID, Gamma market ID, condition ID | Gamma; each has a separate field |
| Up/Down labels and outcome token IDs | Gamma `outcomes` and `clobTokenIds` |
| Metadata prices | Gamma `outcomePrices`, optional and explicitly nonauthoritative |
| Time authority | CLOB `GET /time` |
| Current resting bids/asks and book depth | CLOB `GET /book?token_id=...` |
| Derived best bid/ask, midpoint, spread | Local calculations on the validated book |

Default public hosts:

- Gamma: `https://gamma-api.polymarket.com`
- CLOB: `https://clob.polymarket.com`

The permitted endpoint set is `/events/slug/{slug}`, `/events`, and
`/markets/{id}` on Gamma, and `/time` and `/book` on CLOB. The market-by-ID
read refreshes a fallback selection before book acquisition. Event-derived
paths and query values are encoded, not interpreted as arbitrary URLs.
HTTP redirects are not followed to unapproved destinations.

The Data API, blockchains, reference-price services, `/price` side mappings,
and authenticated endpoints are unnecessary for this milestone.

## 5. Component boundaries and data flow

Use a small synchronous Python application. A prospective
`src/polymarket_bot/market_data/` package holds this subsystem, while the CLI
entry point delegates to it. This is a proposed boundary, not scaffolding
created by this specification.

| Component | Contract and dependencies |
| --- | --- |
| Public HTTP transport | Performs allowed GET requests, parses JSON, applies timeouts and retry budgets; accepts an injected HTTP client |
| Gamma client | Returns candidate event/market responses using only the transport; owns endpoint/query construction |
| CLOB client | Returns server time and token books using the transport; owns response normalization |
| Models and validation | Pure parsers and immutable domain records; validates fields, times, labels, IDs, levels, and derived values |
| Discovery service | Uses Gamma and an injected server-time clock; fast path, list fallback, candidate uniqueness, current-window checks |
| Snapshot service | Uses discovery, CLOB, and the clock; obtains books, checks identities and rollover, assembles a snapshot |
| CLI presentation | Prints normalized results/diagnostics and chooses exit status; owns no discovery or pricing rules |

Data flow:

```text
CLI -> server time -> discovery -> validated MarketDescriptor
    -> Up book -> Down book -> final time/window check
    -> MarketSnapshot -> console output -> exit
```

Models carry no HTTP client, wallet, signing method, or trading action.
Inject transport and monotonic time so tests do not sleep or contact the network.

The proposed runtime dependency is `httpx`, added through uv only after the
required spec/plan approvals. Standard-library dataclasses, Decimal, datetime,
JSON handling, and unittest cover the other requirements. No SDK, build-system
change, or third-party test framework is required at this stage.

## 6. Time and dynamic discovery

### Time authority

At run start, fetch CLOB time. Anchor the returned Unix second to the midpoint
of the request's monotonic start/end readings; advance that estimate by elapsed
monotonic time for discovery checks. Retain round-trip duration: this estimate
has network-latency and server-second precision limitations. Fetch CLOB time
again after the books before publication, and validate using that fresh sample.
Do not set the Windows clock. A failed or malformed initial/final time read
is a failure, not an implicit switch to an unchecked local wall clock.

Use aware UTC timestamps internally. Jakarta display is optional and uses the
known UTC+07:00 offset without adding a timezone database dependency.

### Fast path

Compute `floor(server_unix_seconds / 900) * 900` and use the observed slug
convention to request a candidate event. Examine its markets rather than
assuming the first child is the target. A fast-path result must pass the same
local market validation as a list result. Its naming alone proves nothing.

A missing slug, incompatible payload, wrong family, wrong window, or ambiguous
children leads to the list fallback. Persistent authorization/transport failures
retain their explicit diagnostic; they must not be relabeled as no market.

### Authoritative fallback

Query Gamma `/events` with `active=true`, `closed=false`, `end_date_min` equal
to the captured server time, and `end_date_max` equal to that time plus 30
minutes. Use `order=endDate`, `ascending=true`, `limit=100`, and offset
pagination. Keep query bounds fixed across pages of a discovery attempt.

The fallback must work when the fast path is disabled, missing, or its slug
pattern changes. It must not require a timestamp-slug match for eligibility.
Validate BTC identity and Up/Down semantics from series/title/question data,
and actual duration/time fields from the market. Series title and recurrence
are corroborating metadata; no unsupported recurrence query is sent.

Read up to five pages, deduplicating by event ID and then market ID. Stop on a
short page. If the five-page limit is reached without proving enumeration
complete, return `discovery_incomplete`, not `no_current_market` or an
arbitrarily chosen candidate. Pagination is not a transactional snapshot;
documented bounded enumeration and subsequent validation mitigate change but
cannot guarantee a globally atomic view of the event catalog.

After complete enumeration, exactly one valid current market is required.
Refresh that market through Gamma `/markets/{id}` and validate it again. Zero
valid candidates means no current market; multiple distinct valid candidates
means ambiguous discovery. Do not select by highest volume or earliest row.

### Shared local validation

Require a Bitcoin/BTC Up/Down market with exactly two recognized outcome labels,
one `Up` and one `Down` after conservative case/whitespace normalization.
Family recognition requires either the observed BTC 15-minute series identity
or a series title/market question naming Bitcoin or the standalone token BTC
and an explicit Up-or-Down question. Reject contradictory asset/family metadata.
Duration and window checks still apply whichever recognition route succeeded.
Pair labels and token IDs by their shared array index. Do not assume Up is
first, infer Down from Up, or map labels from price order.

The market must have valid `eventStartTime` and `endDate`, exactly 900 seconds
apart, with `eventStartTime <= server_now < endDate`. `startDate` is retained
only as metadata. Missing window fields cause validation failure; slug parsing
must not substitute for authoritative window fields. When a recognized
timestamp slug exists, disagreement with the window is a rejection.

Require market `active=true`, `closed=false`, and reject explicitly archived
events/markets. Event-level closed/inactive states also reject the candidate.
These flags do not substitute for the current-window test.

Require nonempty event/market IDs, a valid condition ID, and two distinct
nonempty decimal-string token IDs from a two-entry `clobTokenIds` array.
Parse documented JSON-encoded arrays strictly; preserve their order until the
label-to-token mapping is built. Required malformed arrays are not tolerated.
Missing/malformed optional `outcomePrices` becomes a metadata warning rather
than preventing valid book pricing.

Report `acceptingOrders` and `enableOrderBook` as operational status, not as
authorization to place orders. If either is not explicitly true, the market
can be shown as identified with pricing unavailable; do not label its quotes
as currently tradable. Discovery must not replace it with a future market
merely because the future market's book is open.

## 7. Order books and pricing

Fetch one book per mapped token. Require book `asset_id` to match the requested
token string and book `market` to match the selected Gamma condition ID.
A mismatch is a hard failure; never attach that book to another outcome.

Parse all level prices and sizes as finite Decimal values. Prices must be in
the range 0 through 1; sizes must be nonnegative. Ignore zero-size levels for
top-of-book calculations. Malformed, negative, nonfinite, or out-of-range
values invalidate the book rather than being quietly discarded. Require
`bids` and `asks` to be arrays; a missing array is not an empty side.

For each validated book:

- Best bid is the maximum price among positive-size bids.
- Best ask is the minimum price among positive-size asks.
- Midpoint is `(best_bid + best_ask) / Decimal(2)` when both sides exist.
- Spread is `best_ask - best_bid` when both sides exist.

If top-level size is displayed, sum positive-size levels at the same best price
using Decimal; do not select a single arbitrary duplicate level.

Do not assume array ordering. Preserve the levels and their sizes in the
snapshot model; the console view may summarize depth rather than dump it.
If best bid exceeds best ask, classify the book as inconsistent, suppress
usable quote calculations for it, and return a diagnostic failure. A locked
book with equal prices is valid with zero spread.

If one side is empty, show the available side; midpoint and spread are absent.
If both sides are empty, show no quotes. Absence is never represented by zero,
0.5, the last trade, Gamma prices, or the other outcome's complement.

Gamma `outcomePrices` can accompany an outcome only as metadata/debug data.
They never populate these quote fields or recover a failed book read.
Midpoint is a reference value, not an executable quote. Bid/ask describe
resting liquidity at observation time; no fill, fee, slippage, or execution
guarantee is implied.

## 8. Snapshot timestamps, freshness, and rollover

Each outcome keeps its own book source timestamp, HTTP request start/end
observation times, and retrieval status. Normalize known timestamp units in
the CLOB adapter and retain the raw field for diagnostics. Do not collapse
two source timestamps into one market timestamp.

Record total collection interval and a final server-time validation. Print:
`The Up and Down books were fetched separately; this REST snapshot is not atomic.`
This warning appears even if source timestamps happen to match.

A source timestamp may describe an unchanged book; age alone does not prove
that a fresh successful REST read is stale. Show source and retrieval times
without promising exact price freshness. Reject unparseable or incompatible
time fields, and never reuse cached books from an earlier run.

If final server time falls outside the selected market window, discard the
entire attempt's books and metadata for snapshot purposes. Obtain fresh time,
rediscover, and fetch both books again once. No book is reused across conditions.
If the second attempt also rolls over, return `rollover_exhausted` with no
successful snapshot. The retry is bounded and is not a continuous polling loop.

## 9. Output and error behavior

A successful console view contains:

- Event/market title and slug, event ID, Gamma market ID, condition ID.
- UTC window start/end and remaining time at final observation; optional
  Jakarta equivalents.
- Market status, discovery source, and explicit Up/Down token mapping.
- For each outcome: best bid, best ask, midpoint reference, spread, sizes/depth
  summary, independent source/retrieval timestamps, and quote availability.
- Collection interval and the non-atomic warning.

Default output excludes bulky raw metadata. Optional diagnostic presentation
may expose Gamma metadata prices, version, and other identifiers with labels
that distinguish them from the selected tokens and authoritative books.
No output files or database are created automatically.

Use explicit error categories: `no_current_market`, `ambiguous_market`,
`discovery_incomplete`, `invalid_metadata`, `unsupported_identifier_shape`,
`book_unavailable`, `book_identity_mismatch`, `invalid_book`, `time_unavailable`,
`transport_error`, `rate_limited`, and `rollover_exhausted`.

Exit 0 means a valid current market was identified and both required book
requests succeeded, including empty-sided books clearly labeled as such.
Exit 1 covers discovery/transport/parsing failure, invalid identities, and
operationally unavailable pricing. In the latter case, identified metadata
may be printed as an explicitly incomplete result. A failed request for either
outcome is not presented as a successful two-outcome snapshot. Ctrl+C exits
promptly without retrying or taking any external action.

Transport defaults are 10-second connect/read/write/pool timeouts and three attempts total
for connection failures, timeouts, HTTP 429, and HTTP 5xx. Retry delays are
0.5 then 1 second; valid `Retry-After` takes precedence within the run budget.
Other HTTP 4xx responses and malformed required payloads are not retried
blindly. A Gamma fast-path 404 specifically triggers fallback; a book 404 means
unavailable book. A 120-second monotonic deadline bounds the entire run,
including fallback pagination, delays, book reads, and a rollover retry.
Transport timeouts/delays are clamped to remaining time. A synchronous HTTP
timeout applies to network phases, not automatically to a whole response.
Check the monotonic deadline during streamed response reads as well as between
requests/retries; bound response bodies to 10 MiB and reject larger payloads.
An in-progress network operation can take up to its clamped phase timeout to
notice the deadline; late results are rejected, never published as success.
Exhausted budgets return an explicit failure rather than starting more work.

## 10. Validation strategy and acceptance criteria

Automated tests use captured/synthetic public response fixtures, an injected
clock, and injected transport. Ordinary test runs make no network requests.
They exercise behavior rather than merely asserting internal call structure.

Required coverage:

1. Fast-path success and list-only success; slug rename, missing slug, wrong
   slug result, and recognition from independent series/title metadata.
2. Pagination/deduplication, five-page incompleteness, zero candidates,
   ambiguous candidates, and refreshed metadata invalidating a selection.
3. BTC versus other assets; 5-minute, hourly, daily, future, expired, closed,
   inactive, and archived markets; missing/unknown operational readiness.
4. Exact window opening/closing boundaries, 900-second duration, misleading
   `startDate`, missing `eventStartTime`, and conflicting slug timestamps.
5. Reversed Up/Down ordering, duplicate/unknown labels, malformed arrays,
   mismatched lengths, missing/duplicate token IDs, and large ID strings.
6. Extra `positionIds`/version metadata does not replace verified tokens;
   absent usable `clobTokenIds` produces an explicit unsupported result.
7. Unsorted books, exact Decimal arithmetic, positive/zero/negative sizes,
   nonfinite or invalid prices, missing arrays, empty sides, locked/crossed
   books, and book token/condition mismatches.
8. Source milliseconds versus server seconds, independent outcome times,
   non-atomic warning, rollover discard/recollection, and retry exhaustion.
9. GET-only endpoint restrictions, timeouts, retry classifications,
   `Retry-After`, total deadline, interruption, and exit status behavior.

The later manually invoked live smoke check must independently discover a
current target, verify the actual Gamma token IDs against both CLOB books,
check window/status and timestamp shapes, and print the result. It must not
assert fixed IDs, prices, liquidity, or a guarantee that a market exists.
Service/schema failures are reported distinctly from offline test failures.

Acceptance requires all offline tests to pass and the public smoke check to
produce either a validated snapshot or an actionable environmental/availability
diagnostic. A live diagnostic is not represented as a successful integration
check. No credentials or order endpoints may be involved in any test.

## 11. Review record and approval gates

Self-review completed on 2026-10-10:

- All three approved amendments are explicit: verified Gamma `clobTokenIds`
  with CLOB identity checks; slug fast path backed by independent filtered
  discovery; authoritative pricing calculated from books.
- Identifier support is limited to evidence for this target; no automatic
  protocol selector or replacement-field guess is specified.
- Naming and book timestamp units are labeled observed assumptions, not
  official permanent contracts. Missing window metadata fails explicitly.
- Open status, window currency, book availability, and executable-price
  limitations are kept distinct. Incomplete discovery never becomes success.
- Retry/page/deadline limits and one rollover rediscovery prevent unbounded
  work. Test behavior and errors cover the component boundaries.
- Scope contains no strategy, credentials, WebSocket implementation,
  persistence, paper-trading engine, or live order path.

This is a design specification, not an implementation plan. The next action
after user approval of this written artifact is the Superpowers writing-plans
workflow. Implementation still requires review of that plan and selection of
its execution method. No implementation or dependency installation is
authorized by creation of this document.
