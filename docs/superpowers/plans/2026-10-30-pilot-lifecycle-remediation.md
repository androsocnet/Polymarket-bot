# Phase 2B verified pilot lifecycle remediation

Base: c472e1487c121923b5a4d8b10ffcdff60fa7ac4b. Branch: fix/phase2b-soak-remediation. No VPS access, pilot/soak, DB reset, historical evidence edits, trading/wallet operations or Phase 3B work authorized.

## Evidence and root causes

User supplied forensic summary for pilot-20261030T013400Z: observation elapsed 1800.0000859 seconds; successful stop cleared post-stop InvocationID; observer failed finalization; deadline reused failed proof and stopped heartbeat early; audit BLOCKED. Separate Hermes forensic report/export is not present in this checkout; supplied summary is not a substitute for raw historical evidence. Synthetic regression fixtures are newly constructed, not recovered pilot artifacts.

Inspected controller, heartbeat, existing timing/remediation/infrastructure tests and existing native infrastructure design/remediation plans/runbook. Published controller required post-stop InvocationID in both stop_main and validate_timing. Deadline cleanup included heartbeat despite launch-bound T+2280 expiry. Heartbeat script already has monotonic self-expiry and bounded systemd runtime.

## Checklist

- [x] Baseline: 102 relevant tests passed with PYTHONPATH=src under WSL Python 3.12.13.
- [x] Focused RED tests precede production changes. Recorded 25 tests: 16 failures, 2 errors (including missing timing identity key); successful full observation reproduced finalization failure.
- [x] Initial GREEN: 25 focused tests passed; updated legacy fixtures for explicit pre-stop identity evidence.
- [x] Initial relevant regressions: 127 tests passed; additional config/deploy/service/validation/ingest: 58 tests, OK (1 skipped).
- [x] Resolve fresh independent review findings with RED/GREEN tests; obtain explicit ACCEPT. Reviewer ses_e749013abffeR2X9FsVYH7Kk5U first verdict REJECT (six Important). New counterexamples reproduced 21 failing subcases; corrected evidence/fixtures and 132 relevant tests pass. Second review REJECT identified saved-proof reuse, nested deadline-proof agreement, frozen-bundle validation and activation/finite-clock binding gaps. New RED probes reproduced all four; corrected paths and 135 relevant tests pass. Third review ACCEPT by ses_e749013abffeR2X9FsVYH7Kk5U; no remaining Critical/Important findings. Reviewer independently ran 135 relevant tests plus adversarial probes. Acceptance is code review only, not runtime/deployment acceptance.
- [x] Final targeted/regression tests, shell syntax, offline systemd checks and git diff checks. Final combined run: 193 tests in 6.083 seconds, OK (1 skipped); Bash syntax and git diff --check clean. Offline systemd verification reports existing ignored CPUAccounting option warning. Full unittest discovery timed out after 600 seconds without summary; log includes WSL Git worktree path incompatibility and skip. No full-suite pass claimed; unrelated suite not repaired.
- [ ] Commit scoped files, push remediation branch, verify full local/remote SHA equality; stop.

Runtime acceptance remains unproven. PENDING_REVIEW never grants acceptance. Preserve all eight gates, 1800-second observation, existing startup/deadline/runtime limits, fingerprint/approval safeguards and MODE=PAPER_ONLY. Deployment and fresh pilot require separate owner authorization; use docs/phase-2b-final-soak-remediation.md, not scratch scripts. After deployment, preserve pre/post shutdown identity evidence and full heartbeat JSONL through T+2280, wait for T+2640 audit exit, capture inspect-cleanup stdout as new post-audit.json only after successful independent review. Never repair historical evidence.
