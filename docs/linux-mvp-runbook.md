# Linux MVP Phase 1 runbook — PAPER_ONLY ingest pipeline

Scope: an unattended, restart-safe **public-data ingest** service for BTC 15-minute markets on
Ubuntu. It discovers the current market, stores both top-of-book observations, and records window
rollover. It places **no orders**, holds no keys, makes no decisions, and never infers settlement.
The paper engine remains in the tree but is dormant until Phase 2.

This runbook does not claim that Linux resource enforcement has passed. The unit file is statically
tested on Windows; the host checks in §10 are a separate acceptance gate.

## 1. What Phase 1 does and does not do

Each cycle (default every 2 seconds):

1. Checks the `STOP` file before any network request.
2. Reuses the accepted public stack: server time, discovery, and one coherent two-book snapshot.
3. Starts, continues, or rolls over the current window.
4. Writes one idempotent observation per `(bot, market, slot, outcome)`.
5. Updates `heartbeat` and appends `events`; no `decisions`, `orders`, `positions`, or `equity`.

Explicitly **not** included: strategy decisions, paper fills, Telegram, dashboards, the official
`polymarket-client` SDK, live signing, wallets, or order endpoints.

## 2. Host prerequisites

- Ubuntu 24.04 LTS (Python 3.12) with systemd 249+; Ubuntu 22.04 needs a Python 3.12 interpreter.
- Outbound HTTPS to `gamma-api.polymarket.com` and `clob.polymarket.com` only.
- `rsync`, `python3-venv`, `sqlite3`, and root once for installation.
- No credentials, wallet, or API key is needed or accepted. `MODE` must be exactly `PAPER_ONLY`.

## 3. Install (exact commands)

Run from the checked-out project root on the VPS:

```bash
sudo apt-get update
sudo apt-get install -y rsync python3-venv sqlite3

sudo mkdir -p /opt/polymarket-btc
sudo rsync -a --delete \
  --exclude .git --exclude .venv --exclude data --exclude logs --exclude __pycache__ \
  ./ /opt/polymarket-btc/
sudo bash /opt/polymarket-btc/deploy/install.sh
```

The installer creates the `polymarket` system user, the venv, `/var/lib/polymarket-btc/{,state}`,
`/etc/polymarket-btc/paper.env` (mode `0600`), and the systemd unit. It verifies the unit with
`systemd-analyze verify` and does **not** start the service unless `ENABLE=1` is passed.

Review the environment before starting:

```bash
sudoedit /etc/polymarket-btc/paper.env
```

Useful Phase 1 settings:

| Variable | Default | Meaning |
|---|---|---|
| `MVP_POLL_SECONDS` | `2` | minimum seconds between ingest cycles (2–60) |
| `MVP_BACKOFF_BASE_SECONDS` | `2` | first delay after a data error (bounded, >0) |
| `MVP_BACKOFF_MAX_SECONDS` | `30` | hard cap on the adaptive backoff (1–300) |
| `MVP_MAX_CONSECUTIVE_ERRORS` | `5` | stop cleanly after this many consecutive failures |
| `MVP_SAMPLE_SECONDS` | `2` | slot width used for the 450-slot window; fixed at 2 |

Raising `MVP_POLL_SECONDS` reduces public requests but also reduces slot coverage. Raising the
backoff quickly reduces load during throttling. Success resets the backoff to the poll interval.

## 4. Start and observe

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-btc-paper
systemctl status polymarket-btc-paper --no-pager
journalctl -u polymarket-btc-paper -f
```

The service logs a start line, one heartbeat line per 30 cycles (about once a minute), and a stop
line. It writes `windows`, `observations`, `events`, `heartbeat`, `runs`, and `accounts` only.

## 5. Verify the database (read-only)

```bash
sudo -u polymarket sqlite3 /var/lib/polymarket-btc/paper.sqlite <<'SQL'
.mode column
.headers on
SELECT market_id, status,
       datetime(start_us/1000000, 'unixepoch') AS start_utc,
       datetime(end_us/1000000, 'unixepoch') AS end_utc
FROM windows ORDER BY start_us DESC LIMIT 5;

SELECT market_id, COUNT(DISTINCT slot_index) AS slots, COUNT(*) AS books,
       MIN(slot_index) AS first_slot, MAX(slot_index) AS last_slot
FROM observations GROUP BY market_id ORDER BY market_id DESC LIMIT 5;

SELECT (SELECT COUNT(*) FROM decisions) AS decisions,
       (SELECT COUNT(*) FROM orders)    AS orders,
       (SELECT COUNT(*) FROM equity)    AS equity_rows;

SELECT datetime(at_us/1000000, 'unixepoch') AS heartbeat_utc,
       status, cycles, last_error
FROM heartbeat;
SQL
```

Expected during normal operation: two book rows per slot, up to 450 distinct slots per 15-minute
window, `ACTIVE` for the current window, `UNKNOWN_SETTLEMENT` for the previous one at rollover, and
**zero** decisions/orders/equity rows. A stale heartbeat means the service is not running; a growing
`events` table with `data_error` means public data or the network is failing.

## 6. Emergency stop and resume

```bash
sudo -u polymarket touch /var/lib/polymarket-btc/state/STOP
# within one poll interval (or one backoff slice) the service exits cleanly
systemctl status polymarket-btc-paper --no-pager
sudo -u polymarket sqlite3 /var/lib/polymarket-btc/paper.sqlite \
  "SELECT status, cycles, last_error FROM heartbeat;"
```

The expected heartbeat status is `STOPPED`. `Restart=on-failure` does not restart a clean STOP.
To resume:

```bash
sudo rm /var/lib/polymarket-btc/state/STOP
sudo systemctl start polymarket-btc-paper
```

`systemctl stop` is also safe: `SIGTERM` is handled in 0.25-second sleep slices, and collection
checks the stop predicate before each bounded HTTP phase (each request has a 10-second timeout).
A `STOP` file left in place intentionally keeps the service stopped across restarts.

`FAILED_STOP` exits with code `75`, and the unit sets `RestartPreventExitStatus=75`, so systemd
does **not** restart a data-exhaustion failure. The start-rate limit remains a second guard against
crash loops.

## 7. Rollover, restart, and polling behavior

- **Rollover:** when discovery returns a different market, the old window is closed as
  `UNKNOWN_SETTLEMENT` with note `rollover to <market>`; the new window is inserted `ACTIVE`.
  Observations are never copied or inferred across markets.
- **Restart mid-window:** the active window is reused. The observation primary key
  `(bot_id, market_id, slot_index, outcome)` makes a repeated snapshot idempotent; it is reported as
  `DUPLICATE_SLOT` and does not create a second row.
- **Window expiry without a successor:** the old window is `ABORTED`; the next valid market starts a
  fresh window. No settlement is inferred.
- **Discovery conflict:** if discovery briefly returns a closed/abandoned window, the active window
  is left untouched and the heartbeat sets `last_error=WINDOW_CONFLICT`; one event is recorded.
  Observation resumes when discovery returns the active successor. It never reopens or settles a
  closed window.
- **Throttling:** the accepted HTTP layer honors `Retry-After` for 429/5xx within its bounded
  attempts. At the cycle level, consecutive failures back off exponentially from
  `MVP_BACKOFF_BASE_SECONDS` to `MVP_BACKOFF_MAX_SECONDS`; a success resets the backoff. After
  `MVP_MAX_CONSECUTIVE_ERRORS` failures the service writes `FAILED_STOP` and exits `75`; systemd
  does not restart it (`RestartPreventExitStatus=75`), so this is a clean fail-closed stop rather
  than a crash loop.
- **Request volume:** one snapshot per poll (time + discovery + two books, about five public
  requests). No extra probes, metrics exporters, or health-check requests are made. There is no
  separate daily request budget; raise `MVP_POLL_SECONDS` on a constrained host.

## 8. Report (Phase 2 surface)

The read-only report command still exists and never fills missing marks or infers settlement:

```bash
sudo -u polymarket env PYTHONPATH=/opt/polymarket-btc/src \
  /opt/polymarket-btc/.venv/bin/python -m polymarket_bot.linux_mvp report \
  --db /var/lib/polymarket-btc/paper.sqlite
```

In Phase 1 it will show windows and the explicit `UNKNOWN_NOT_INFERRED` settlement state, with no
decisions or orders. Performance reporting becomes meaningful only after Phase 2 enables the paper
engine.

## 9. Resource controls (static claims)

The unit sets `MemoryMax=512M`, `CPUQuota=50%`, `TasksMax=64`, `LimitNOFILE=256`,
`Restart=on-failure` with `RestartPreventExitStatus=75` and a bounded start rate,
`StateDirectory=polymarket-btc`, and hardening
(`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, empty capabilities).

These were **not executed** on the Windows development host; only their presence is tested there.
They are not the CP6 hard 1 GiB/256 MiB/120 s contracts and make no such claim.

## 10. Ubuntu host acceptance gate (separate from local tests)

Run all of these on the VPS; do not mark Phase 1 accepted until they pass:

1. `systemd-analyze verify /etc/systemd/system/polymarket-btc-paper.service` → no errors.
2. Start the service and let it run 10 minutes.
3. Run the §5 queries: heartbeat fresh, two books per slot, at least one window, zero
   decisions/orders/equity.
4. `sudo systemctl restart polymarket-btc-paper` mid-window. Confirm the active window is reused,
   observation counts are monotonic, and the primary key prevents duplicates.
5. Create `STOP`; confirm a clean `STOPPED` heartbeat and no network activity, then remove it and
   start again.
6. `systemctl show polymarket-btc-paper -p MemoryMax -p CPUQuotaPerSecUSec -p TasksMax` matches the
   unit; inspect `systemd-cgtop -m` while running.
7. Leave it running for 30–60 minutes; check `journalctl` for unbounded restarts or repeated
   `data_error`, and confirm the database grows predictably.
8. Record the observation window, host, unit hash, and query output as the Phase 1 host evidence.

Only after this gate may the verdict change from "offline-tested" to "operated on Linux". Resource
enforcement on Windows/static tests is explicitly not evidence of kernel enforcement.

## 11. Backup, upgrade, rollback

- Copy with `sqlite3 paper.sqlite ".backup '/var/backups/paper.sqlite'"` (WAL files are part of the
  live store), or stop the service first.
- Upgrade: copy the new tree, re-run the installer, then `systemctl restart`. Schema is `mvp-v1`;
  there is no automatic migration, so keep the old database for rollback.
- Rollback: `systemctl disable --now polymarket-btc-paper`, restore the previous tree/unit, and run
  `systemctl daemon-reload`. Data remains under `/var/lib/polymarket-btc`.

## 12. Known gaps

- No settlement or realized-outcome claim: the ingest pipeline stops at top-of-book evidence.
- No automated retention/disk guard; monitor database size.
- No Telegram, dashboard, metrics exporter, or alerting (Phase 2).
- No full-depth/raw payload archive; CP5 remains the full-evidence recorder.
- Public rate limits are managed by bounded retries plus cycle backoff, not by a measured request
  budget.
- The official `polymarket-client` SDK is intentionally absent (dependency conflict and unnecessary
  surface).
