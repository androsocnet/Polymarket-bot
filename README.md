# Polymarket BTC 15-minute Market Data

A Python project for a future paper-trading bot. This milestone reads public
market data only: it finds the current BTC 15-minute Up/Down market, prints one
snapshot, and exits.

There is no wallet, authentication, credential handling, order submission,
strategy, signal, simulated trading engine, WebSocket, persistence, or BTC
spot-price feed. No money is used. The application does not load `.env`.

## Setup

Use Python 3.12 or newer and uv. In PowerShell, from this checkout:

```powershell
uv sync --locked --cache-dir .uv-cache --no-python-downloads
uv run --locked --cache-dir .uv-cache python src/main.py --jakarta
```

uv creates a separate `.venv` environment. The only direct dependency is httpx;
its supporting dependencies are pinned in `uv.lock`. If Python is not on PATH,
pass an installed interpreter to `uv sync --python "C:\path\to\python.exe"`.
The original setup used Python bundled with Codex. No additional interpreter
is needed.

Feature implementation is on the local `codex/read-only-market-data` branch
in an isolated worktree. Run these commands there; main retains the scaffold.

## Commands

```powershell
# One snapshot, UTC internally and in the output
uv run --locked --cache-dir .uv-cache python src/main.py

# Also show Jakarta time
uv run --locked --cache-dir .uv-cache python src/main.py --jakarta

# Exercise discovery independently of the observed timestamp slug convention
uv run --locked --cache-dir .uv-cache python src/main.py --no-fast-path --jakarta

# Show optional Gamma metadata with explicit non-quote labels
uv run --locked --cache-dir .uv-cache python src/main.py --debug

# Show help without contacting any API
uv run --offline --locked --cache-dir .uv-cache python src/main.py --help
```

No market ID is hard-coded. The program uses CLOB server time, validates Gamma's
BTC identity and exact 900-second window, and maps Up/Down by the outcome and
clobTokenIds array indices. A timestamp slug is only a fast path. The independent
fallback scans a bounded Gamma event list and refreshes the unique selection.
It never chooses a future market just because that market is open.

Only five public GET routes are permitted: Gamma event-by-slug, event list,
market-by-ID; CLOB server time and token order book. Requests do not follow
redirects or use ambient proxy/credential settings.

## Reading the prices

For each outcome:

- **Best bid** is the highest positive-size resting bid.
- **Best ask** is the lowest positive-size resting ask.
- **Midpoint reference price** is the average of those prices, when both exist.
- **Spread** is best ask minus best bid.
- Sizes and depth summarize the returned book; they do not guarantee a fill.

These are prices of the outcome tokens, not a BTC/USD spot-price feed. All
price/size calculations use Decimal. Gamma outcomePrices is optional metadata,
never a replacement for a missing or failed book quote.

Empty sides are labeled `unavailable`; they are not replaced with 0, 0.5, the
other outcome's complement, or metadata prices. A crossed or malformed book,
or a token/condition mismatch, fails explicitly.

Up and Down are separate REST reads. The output always warns:

> The Up and Down books were fetched separately; this REST snapshot is not atomic.

Each book has its own source and retrieval timestamps. Source timestamp age
alone may reflect an unchanged book. Book milliseconds and server seconds are
interpreted separately; the 13-digit millisecond shape is an observed adapter
assumption. Server time has whole-second precision and network-latency error;
displayed times are estimates, not synchronized exchange guarantees. The
collection duration uses monotonic elapsed time even when time samples differ.

## Failures and limits

Exit 0 means a valid current market and both book reads succeeded (possibly
with empty sides). Exit 1 means discovery, parsing, identity, transport, or
operational-readiness failure. An identified market without explicitly true
acceptingOrders/enableOrderBook may be shown as an **incomplete** result with no
quotes. Ctrl+C exits 130 without retrying.

A rollover discards both books and permits one rediscovery. A second rollover
fails. GET requests have 10-second phase timeouts, at most three attempts for
connection/timeouts/429/5xx, 0.5/1-second backoff or valid Retry-After, a shared
120-second run deadline, and a 10 MiB response cap. A synchronous blocking
network phase can notice deadline expiry late; late results are rejected.
Five full fallback pages fail as incomplete enumeration rather than guessing.

## Offline tests

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
uv run --offline --locked --cache-dir .uv-cache python -m unittest discover -s tests -t . -v
```

Tests use Python's built-in unittest, injected clocks, synthetic public API
responses, and a socket guard. They do not sleep or contact Polymarket.
Live smoke checks are the normal and forced-fallback commands above, invoked
manually and separately. Live availability/schema diagnostics are reported
honestly, never substituted with old IDs or fabricated prices.

## Layout and version control

`src/polymarket_bot/market_data/` contains metadata parsing, the small public
GET helper, Gamma/CLOB clients, discovery, and snapshot coordination.
`src/polymarket_bot/cli.py` formats the result; `src/main.py` is the entry point.
`tests/` contains offline behavioral checks. `data/` and `logs/` retain only
their placeholders; this application creates no output files.

Keep `pyproject.toml` and `uv.lock` in Git. Environments, caches, generated
bytecode, local environment files, and generated data/logs remain ignored.
The original scaffold, design/plan, and feature changes have separate commits.
RTK integration remains project-scoped.

The approved [design](docs/superpowers/specs/2026-10-10-read-only-market-data-design.md)
and [implementation plan](docs/superpowers/plans/2026-10-10-read-only-market-data.md)
are retained as historical approval artifacts.

Official references: [Gamma event list](https://docs.polymarket.com/api-reference/events/list-events),
[CLOB book](https://docs.polymarket.com/api-reference/market-data/get-order-book),
[CLOB server time](https://docs.polymarket.com/api-reference/data/get-server-time).
