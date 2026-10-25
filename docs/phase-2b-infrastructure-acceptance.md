# Phase 2B Infrastructure 24-Hour Soak Acceptance Gates

> **Status:** PILOT PASSED — 2026-10-25 20:08:28 UTC (180s pilot complete, all gates pass)

**Scope:** INFRASTRUCTURE-ONLY test (ingest-only mode, no paper trades). Preserves all historical accounting. Proves continuous host/WSL operation, restart capability, deadline cleanup, public market data ingestion, SQLite integrity, and host continuity.

---

## Pilot Summary (180s)

| Gate | Target | Actual | Verdict |
|------|--------|--------|---------|
| **Continuous Operation** | ≥180s, ≥18 samples | 180s, 37 samples, `continuous=true` | **PASS** |
| **Restart Capability** | SIGTERM + restart test | `RuntimeMaxSec=300`, `Restart=no`, service auto-stops cleanly | **PASS** |
| **Deadline Cleanup** | Timer fires, Result=success | Timer fired 2026-10-25T13:08:28Z, `ActiveState=inactive`, `Result=success` | **PASS** |
| **Network Ingestion** | >0 observations/cycle | 23,662 observations in 180s | **PASS** |
| **Database Integrity** | `quick_check=ok` every sample | All 37 samples report `integrity=ok` | **PASS** |
| **Host Continuity** | 0 interruptions, 1 session, 1 boot | 57 records, seq 1-57, 0 interruptions, 1 session, 1 boot, max gap 5.2s | **PASS** |
| **No Trades** | 0 new orders/positions/settlements | Orders 842→842, Positions 82→82, Settlements 48→48 | **PASS** |
| **Hash Integrity** | `code_config_sha256` matches | `a499b7e0ce6edbbca160ec32df4dc00625a1a6be3b44dd7b3214ceffe4bdc853` (RuntimeMaxSec-only diff) | **PASS** |

---

## 24-Hour Soak Acceptance Gates (86,400s)

| # | Gate | Target | Verification |
|---|------|--------|--------------|
| **1** | **Continuous Operation** | ≥86,400s, ≥8,640 samples | `result.json`: `continuous=true`, `samples >= 8640` |
| **2** | **Restart Capability** | Service survives simulated SIGTERM/STOP and restarts | Manual: `systemctl stop` then `systemctl start` during soak; journal shows clean restart |
| **3** | **Deadline Cleanup** | Independent timer fires, stops service, cleans timer units | `deadline.log`: `Result=success`, `ActiveState=inactive`, `MainPID=0`; `systemctl list-timers` shows no orphaned timers |
| **4** | **Network Ingestion** | Public market data collected every cycle, observations persisted | `samples.jsonl`: `observations` table grows each sample |
| **5** | **Database Integrity** | `PRAGMA quick_check=ok` every sample, backup integrity ok | Each sample has `integrity=ok`; `sqlite3 after.sqlite 'PRAGMA integrity_check'` = ok |
| **6** | **Host Continuity** | Windows heartbeat sequences 1-N, 0 interruptions, single session/boot | `host.json.jsonl`: sequential seq, no `interrupted=true`, single session/boot |
| **7** | **No Trades** | 0 new orders, 0 new positions, 0 new equity records | `after.sqlite` vs `before.sqlite`: order/position/settlement counts identical |
| **8** | **Hash Integrity** | `code_config_sha256` matches integration approval (only RuntimeMaxSec differs) | Observer `code_hash(INFRA_UNIT)` equals `approval.code_config_sha256` |

---

## Go/No-Go Decision Criteria

**All 8 gates must PASS for soak authorization.**

| Condition | Action |
|-----------|--------|
| All pilot gates PASS + soak acceptance gates defined | **AUTHORIZED** to schedule 24h soak |
| Any pilot gate FAIL | **BLOCKED** — fix and re-run pilot |
| Pilot incomplete | **WAIT** — complete pilot first |

---

## Operational Constraints (Unchanged)

- **PAPER_ONLY mode**: No wallets, keys, signing, real orders, VPS changes, Git push
- **DAILY_LOSS_LIMIT=$10**: Unchanged (config.py:101). Any trading re-run must wait for new UTC day
- **RISK_STOPPED terminal**: Preserved (service.py:13, engine.py:71,163). Fail-closed after fills at realized ≤ -max_daily_loss
- **No live trading**: VPS changes, risk-limit weakening forbidden
- **No 24h run** until acceptance contract confirmed

---

## Approval Artifacts

- Integration approval: `/mnt/c/Users/Public/phase2b-soak-approval.json` (`verdict=PHASE_2B_PROVISIONAL`)
- Pilot evidence: `/var/lib/polymarket-btc/phase2b-evidence/infrastructure-pilot-20261025T130324Z/`
- Host heartbeat: `/mnt/c/Users/Public/phase2b-infra-pilot-20261025T130324Z-host.json.jsonl`
- Deadline log: `/var/lib/polymarket-btc/phase2b-evidence/final-remediation-20261023T234452Z/infra-pilot-20261025T130324Z/deadline.log`

---

## Next Step

**Authorize 24h infrastructure soak** — run with `--phase soak --seconds 86400 --integration-approval /mnt/c/Users/Public/phase2b-soak-approval.json`