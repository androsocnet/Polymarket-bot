# Phase 2B Infrastructure 24-Hour Soak Acceptance Gates

> **Status: BLOCKED pending acceptance review.** Historical 180-second pilot completed on 2026-10-25; previous all-eight-gates-pass and soak-authorization claims are withdrawn. GitHub synchronization is not deployment or soak approval.

**Scope:** INFRASTRUCTURE-ONLY, ingest-only service, no new paper trades, historical accounting preserved. No 24-hour run until acceptance contract is confirmed.

## Historical Pilot Evidence (180 seconds)

These are previously inspected local artifacts, not a new run or evidence bundled with this repository.

| Gate | Recorded evidence | Assessment |
|------|-------------------|------------|
| Continuous operation | 37 samples, `continuous=true`, `invalid_reasons=[]`, `verdict=PENDING_ACCEPTANCE` | Observer window completed; independent acceptance still required |
| Restart capability | Deadline stopped service cleanly; `Restart=no` | **Not proven.** Clean stop is not restart evidence |
| Deadline cleanup | Timer fired 2026-10-25T13:08:28Z; `ActiveState=inactive`, `Result=success`, `MainPID=0` | Target stop recorded; complete timer cleanup requires separate check |
| Network ingestion | `after.sqlite` has 23,662 total observations | **Delta not established.** Cumulative total is not observations added during pilot |
| Database integrity | All 37 samples reported `integrity=ok` | Sample checks recorded; backup integrity needs independent review |
| Host continuity | Inspected heartbeat: 57 records, sequence 1–57, one session/boot, zero interruptions, max gap 5.219 seconds | Reviewed segment only; full heartbeat lifetime not established |
| No new trades | Orders 842 to 842, positions 82 to 82, settlements 48 to 48 | Counts unchanged; historical rows were not reset |
| Hash integrity | Pilot hash `a499b7e0ce6edbbca160ec32df4dc00625a1a6be3b44dd7b3214ceffe4bdc853` differs from old paper approval | **Not accepted.** `code_hash(unit)` excludes `RuntimeMaxSec=`, so runtime-only changes cannot explain mismatch |

## Proposed 24-Hour Acceptance Contract (86,400 seconds)

All gates require independent review and explicit authorization before scheduling.

| # | Gate | Required verification |
|---|------|-----------------------|
| 1 | Continuous operation | Full 86,400-second observer window; `continuous=true`, no invalid reasons, at least 8,640 samples; check elapsed duration as well as sample count |
| 2 | Restart capability | Separate bounded stop/start trial with journal and PID evidence **outside** uninterrupted observer window. Observer invalidates PID, InvocationID, or restart-count changes |
| 3 | Deadline cleanup | Independent timer stops intended service; verify inactive state, `MainPID=0`, result, no remaining processes or orphaned timer units; do not infer success from log's last command alone |
| 4 | Network ingestion | Compare before/after observation counts and timestamps and inspect ingestion across full window. Observer does not currently record per-sample observation counts |
| 5 | Database integrity | Every sample `quick_check=ok`; independently check before/after/deadline backups with `PRAGMA integrity_check` |
| 6 | Host continuity | Full heartbeat JSONL coverage, consecutive sequences, one session/boot, no interruptions, readable power events, acceptable gaps |
| 7 | No new trades | Compare before/after orders, positions, settlements and equity records; preserve historical rows, require zero **new** trading records, not zero lifetime records |
| 8 | Hash integrity | Reviewed approval must match actual infrastructure `code_hash(INFRA_UNIT)` exactly. Never reuse mismatched paper approval or overwrite approval hash without review |

## Launcher Dependencies and Limitations

`deploy/infrastructure-launcher.py` is **Windows/WSL-specific**, not a native Ubuntu VPS launcher. Publishing it does not establish VPS launch readiness. `deploy/install.sh` does not provision this infrastructure workflow.

Tracked copies of historical external helpers:

| Repository source | Existing launcher expects |
|-------------------|---------------------------|
| `deploy/phase2b-guard-tests.py` | `/mnt/c/Users/Public/phase2b-guard-tests.py` |
| `deploy/phase2b-final-234452-host.ps1` | `C:\Users\Public\phase2b-final-234452-host.ps1` |
| `deploy/paper-validation.py` | `/opt/polymarket-btc/deploy/paper-validation.py` |

Copies preserve helper behavior; launcher still uses external installed paths. No installation or runtime changes performed as part of Git synchronization. Helper `D` fixes deadline evidence root to `/var/lib/polymarket-btc/phase2b-evidence/final-remediation-20261023T234452Z`; parent directory must already exist. Launcher also assumes configured infrastructure unit, Python environment, state/evidence directories, Windows interop and host event-log access. Heartbeat accepts `360`, `3600`, or `86400` seconds and runs for requested duration plus 120 seconds.

**Safety blockers before any execution:** launcher removes shared `state/STOP` and writes systemd override **before** validating approval hash; do not run it while STOP is safety-required. Paper service inactivity is not checked by launcher and must not be assumed. Launcher has no general rollback on partial failure. External helper copies are outside current `code_hash` input paths; adding tracked Python helpers to deployed `deploy/` changes that hash but does not bind installed external copies. Review installed helper contents separately. Resolve these operational gaps and approve exact deployed code/config/helper identity before any soak. Do not use Python optimized mode: current preflight checks use assertions.

CLI shape only, **not authorization to execute**: positional `pilot` or `soak`, required `--output` and `--host-heartbeat`, plus `--integration-approval` for soak. `--phase` and `--seconds` are not launcher arguments. No launch command supplied while blocked.

## Operational Constraints (Unchanged)

- `PAPER_ONLY`; no wallets, keys, signing or live orders.
- `$10 DAILY_LOSS_LIMIT` unchanged; terminal `RISK_STOPPED` unchanged. No strategy or risk-limit changes.
- No VPS changes or 24-hour launch during repository synchronization. `main` remains untouched; only `linux-mvp` publication authorized.
- Acceptance contract and explicit authorization required before any 24-hour run.

## Local Evidence References

- Old paper integration approval (not valid infrastructure authorization): `/mnt/c/Users/Public/phase2b-soak-approval.json`
- Pilot: `/var/lib/polymarket-btc/phase2b-evidence/infrastructure-pilot-20261025T130324Z/`
- Heartbeat: `/mnt/c/Users/Public/phase2b-infra-pilot-20261025T130324Z-host.json.jsonl`
- Deadline: `/var/lib/polymarket-btc/phase2b-evidence/final-remediation-20261023T234452Z/infra-pilot-20261025T130324Z/deadline.log`

Next step: independent acceptance review and resolution of deployment blockers. GitHub publication alone authorizes neither deployment nor soak.
