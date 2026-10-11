# Polymarket BTC 15-minute market data

This project reads public BTC 15-minute Up/Down market data. `src/main.py` prints
one snapshot and exits. `src/record.py` is a separate, explicitly `BOOK_ONLY`
recorder that persists public market and book evidence in SQLite. The reference
collector is disabled; every recorder run is `REFERENCE_INCOMPLETE`. Neither
command trades or produces a strategy-complete dataset.

There is no wallet, Polymarket authentication, order/cancellation path, strategy,
signal, simulated trading, WebSocket, BTC spot-feed substitution, or live
Chainlink signer. No `.env` or credential file is loaded.

## Setup and commands

Use Python 3.12 or newer and uv from this checkout. The only direct dependency
is httpx; `uv.lock` pins its supporting packages.

```powershell
uv sync --locked --cache-dir .uv-cache --no-python-downloads

# One public snapshot; these retain the original snapshot interface.
uv run --locked --cache-dir .uv-cache python src/main.py --jakarta
uv run --locked --cache-dir .uv-cache python src/main.py --no-fast-path --debug

# Help is offline and never opens a database or constructs an HTTP client.
uv run --offline --locked --cache-dir .uv-cache python src/record.py --help
uv run --offline --locked --cache-dir .uv-cache python src/record.py record --help
uv run --offline --locked --cache-dir .uv-cache python src/record.py report --help
```

When a separate book-only run is authorized, its command is explicit:

```powershell
uv run --locked --cache-dir .uv-cache python src/record.py record --mode book-only --db data/recorder.sqlite3
```

`--no-fast-path` forces independent filtered-list discovery. Omitting `--mode`
refuses before opening the writer or HTTP client. `--mode reference-required`
refuses with `activation_required`; the CLI has no credentials, signing, or
activation option. `BOOK_ONLY / REFERENCE_INCOMPLETE` is printed on a run, and
book coverage and verified live reference coverage remain separate.

An existing database can be measured offline. Requested UTC bounds use local
wall time. Scheduled slots use a separate half-open calibrated server interval;
checkpoint and receipt evidence retain their labelled wall bounds. This example
is a command template, not a claim that data was recorded:

```powershell
uv run --offline --locked --cache-dir .uv-cache python src/record.py report --db data/recorder.sqlite3 --start 2026-10-10T00:00:00Z --end 2026-10-11T00:00:00Z
```

The report opens only an existing database in SQLite read-only mode. It neither
initializes a schema nor takes writer ownership or contacts an API. JSON output
labels decimal MB/day and GB/30 days, bytes per newly terminal or demonstrably
COMPLETE slot, request latency in nanoseconds, and status/reference
denominators. It includes mode, revisions, clock uncertainty, interruptions,
restart downtime, discovery gaps, book-depth breakdown, resolution counts and
raw/compressed evidence bytes. Rates or physical COMPLETE denominators that
cannot be established are `null`, with diagnostic bounds or reasons. A storage
projection describes the observed fixed workload and is not a guarantee.
Quality bounds use persisted valid clock anchors with compatible recorded
uncertainty intervals. Counts and percentages are labelled calibrated point
estimates, with eligible-slot and status-count bounds. Missing calibration or
incompatible clock evidence produces `null` quality values with a diagnostic
reason. This projection does not establish universal wall-clock accuracy or
change recorded timestamps.

## Public data and evidence

Discovery checks the current 900-second window against CLOB time, validates the
market and independent Up/Down token mapping, and uses Gamma's timestamp slug
only as a fast path. A bounded filtered-list fallback remains available. Public
GET routes are limited to Gamma events/markets, CLOB time/book/config, and Data
API resolutions. The sole POST is a public `POST /books` with a body constructed
from exactly two validated token IDs. Requests use no ambient proxy or
credential settings and never follow redirects.

Each outcome has independent book identity and source-time validation. The
recorder tries the batch read and uses single-book GET fallback only for a batch
404/405 or missing requested token, within the slot deadline. REST batch
responses are **not guaranteed atomic**. Equal source timestamps do not make
them atomic. An empty book can be valid with nullable quote summaries.
Malformed observations carry explicit failures. Missing quotes are never
replaced by a complement, metadata price or guessed quote. All exact price and
size arithmetic uses bounded Decimal inputs. Malformed numeric evidence is
contained per observation, without rounding or clamping source values.

SQLite schema version 1 retains the full returned response bytes compressed as
original evidence, alongside indexed book summaries and exact Decimal text.
`data/` is the suggested location for the database; generated data and logs are
ignored by Git. The recorder owns one SQLite writer and a canonical-path OS
lock. It does not rotate, vacuum, delete depth, or adapt cadence automatically.

The 450 scheduled slots per validated window are at `window_start + 2 × index`
seconds for indices 0–449. The last is at +898 seconds. Monotonic scheduling
selects the next future slot after a delay; it never bursts to catch up. A
capture deadline is the earlier of its scheduled time +2 seconds and market
end. Requests have bounded retries and a 10 MiB response ceiling. Clock anchors
are refreshed about every minute; expired or discontinuous anchors pause
sampling. Missing/partial/failed/interrupted slots and discovery gaps remain
visible in storage and reports.

After a restart, the writer validates the existing schema and integrity,
recovers interrupted runs/slots, creates a new clock anchor, verifies any reused
market identity, records known downtime, then resumes only at a future slot.
Unknown intervening markets are not invented. Resolution reconciliation stores
raw status, payout and time separately from book prices. Original live reference
availability remains distinct from any later historical repair: backfilled
information cannot become a value that was known during the original window.

## Reference activation and limits

The pinned future reference is
`BTC/USD-Streams-TWAP-60s-mainnet-production`, feed
`0x0002ee6757e8822c00d273bc340fc24c9cafe123a4ff2ea1dbdb31944bc7d95f`.
Reference-required collection remains unavailable until these six gates are
verified and separately authorized:

1. Entitlement to that exact mainnet TWAP feed.
2. Actual subscription price and a subscription decision.
3. Credential availability and explicit permission to use them.
4. Successful authenticated retrieval for that feed.
5. Verified report decoding, schema and numeric scaling.
6. Verified opening/closing boundary report selection applicable to Polymarket.

Future Chainlink signing must follow its verified authentication timestamp
contract, including units, freshness and source/skew. CLOB server time governs
market-window sampling; it is not assumed to be the Chainlink signing clock.
No pilot, subscription, credential use or 24-hour run is claimed here.

## Offline tests and design

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
uv run --offline --locked --cache-dir .uv-cache python -m unittest discover -s tests -t . -v
```

Tests use synthetic public API responses, injected clocks and temporary SQLite
databases under `data/`. A socket guard prevents live connections. See the
[recorder design](docs/superpowers/specs/2026-10-10-market-data-recorder-design.md)
for the evidence and activation rules. The earlier
[snapshot design](docs/superpowers/specs/2026-10-10-read-only-market-data-design.md)
remains relevant to `src/main.py`.
