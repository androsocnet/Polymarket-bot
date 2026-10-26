# Phase 2B WSL validation gates

PAPER_ONLY. No wallets, keys, signing, real orders, VPS changes, or Git push.

## Status

**PHASE_2B_PROVISIONAL** — 2026-10-24 07:50:26 WIB (timer deadline wall 1792802966.4669714)

60-minute guarded integration trial completed with independent deadline timer enforcement. All seven acceptance gates pass per independent review. Separate 24-hour soak trial pending per this document.

## Before running

1. Preserve accepted CP5/CP6 hashes and unrelated worktree file fingerprints.
2. Run offline/adversarial suites, including settlement and validation-monitor checks, on Windows and WSL.
3. Clear independent review blockers; preserve review and test results.
4. SQLite online backup, integrity check, and current systemd config captured before deployment. Baseline backup already created at `/var/lib/polymarket-btc/paper.sqlite.phase2b-baseline-20261023T052659Z`.
5. Package LF bytes (`core.autocrlf=false`). Deploy only authorized WSL guest, not a VPS. Keep PAPER_ONLY unit resource/security controls.
6. Use existing unit with a temporary drop-in `RuntimeMaxSec` and `Restart=no`. Independently arm a systemd stop timer; monitor `finally` alone does not cover SIGKILL, guest shutdown, or monitor crash. Never enable bot/observer at boot for validation.
7. Start Windows host heartbeat script and verify current readable System power events. Mount its output read-only for observer where possible. Script neither prevents sleep nor changes host policy.
8. Integration/soak must use same runtime drop-in filename; only its `RuntimeMaxSec=` value may change without invalidating approval. Observer excludes that value from invariant hash, validates effective numeric bound separately, and retains complete `unit.txt` plus `unit_sha256`. Any other unit/config/source change requires new integration approval.

## Offline remediation evidence

Exact historical CP5 fixture restored only after original SHA-256 verification; no synthetic replacement. Windows journal cleanup now waits for supervisor process exit (test-only). WSL discovery must run from byte/hash-verified native ext4 snapshot including `data`: measured focused test61.520s on Windows9p vs19.727s ext4; preserve SQLite FULL durability. Aggregate300s timeout was not proof of deadlock.

Isolated systemd controls verified with dummy worker: independent timer, RuntimeMaxSec and BindsTo observer-kill containment. Actual observer main loop also tested against isolated synthetic ledger/host; SIGKILL left worker inactive/MainPID0, no orphan. These checks do not replace effective production-unit/drop-in/timer checks immediately before authorized integration, actual Windows sleep/WSL interruption validation, or bot STOP/restart acceptance. Evidence paths in `docs/phase-2b-evidence.md`.

## Integration (3600 seconds)

`deploy/paper-validation.py integration --seconds 3600 --output <new-evidence-dir> --host-heartbeat <mounted-Windows-heartbeat.json>`

Observer is not sufficient acceptance by itself. Operator must observe service progress, at least three market rollovers, resolution attempts, exact identity/finality evidence or UNKNOWN, all ledger reconciliation, zero live calls, and bounded resources. Exercise STOP plus restart deliberately and record timestamps/expected interruption. Such a restart is recovery evidence, not uninterrupted process uptime. Any unscheduled stop, host sleep, heartbeat gap, WSL shutdown, or clock jump blocks continuous-uptime claims.

Observer preserves `before.sqlite`, `after.sqlite`, `samples.jsonl`, `heartbeat.json`, `journal.log`, and `result.json`. `PENDING_ACCEPTANCE` is NOT PASS or PROVISIONAL. It only means monitor clock duration completed without detected gap.

## Separate soak (86400 seconds)

Only after all integration gates pass: create separate systemd observer unit using `soak --seconds 86400`; fresh evidence directory and host heartbeat. No automatic promotion from integration. Set explicit runtime bound and independent stop timer. Do not block coding session for 24 hours. No automatic startup after reboot.

A complete 24-hour continuous window is required. Sleep/interruption invalidates that window. Restart recovery can be tested in integration, not silently subtracted from soak duration. Host heartbeat/power evidence and guest boot/clock samples must agree before claiming continuous operation.

## Verdict

- `BLOCKED`: offline/review/live acceptance gate incomplete or failed.
- `PHASE_2B_PROVISIONAL`: full 60-minute integration gates pass, separate soak pending/running.
- `PHASE_2B_PASS`: verified full 24-hour continuous soak and every acceptance gate passes.

Report exact commits/files, commands/results, evidence paths and hashes, authoritative win/loss examples, cash/basis/fees/realized and unrealized P&L, marked equity or UNKNOWN, verified counts, unresolved positions, max drawdown and coverage, simulated fills/rejections, latency definition, resources/uptime, STOP/restart and integrity evidence. No profitability or live-readiness inference.
