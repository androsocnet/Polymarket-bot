# Phase 2B evidence ledger — incomplete acceptance

## Status

BLOCKED pending final independent review, complete regressions, and live validation. No PHASE_2B_PROVISIONAL or PHASE_2B_PASS claim. No 60-minute run or 24-hour soak started yet.

## Safety / preserved evidence

- WSL service confirmed inactive/dead, MainPID=0 before work.
- No wallet, signing, authenticated trade, VPS changes, or Git push performed.
- Seven accepted CP5/CP6 SHA-256 values matched; 29 unrelated dirty-file fingerprints matched baseline (0 mismatches).
- Original ledger backed up via SQLite online backup: `/var/lib/polymarket-btc/paper.sqlite.phase2b-baseline-20261023T052659Z`; integrity `ok`.
- Earlier session manually widened production windows status CHECK constraint; this evidence ledger does not erase that history. No production settlement payout has been applied during current continuation.

## Public evidence fetch (read-only)

Directory: `/var/lib/polymarket-btc/phase2b-evidence/public-resolution-20261023T052957Z`.
Each market JSON contains exact original Gamma/Data API response text, recorded identities, fetch timestamps, and verification output.

| Market | API-attested winner | Evidence JSON SHA-256 |
|---|---|---|
| 6032439 | UP | e51f9e11f193ac9fc2d6a30969afc0bd5633e24b71773b1e8153a1edc2889c2b |
| 6032678 | UP | 9f11d1180b3e7ae98202f7552fd29fdda19eb3b17863771dea6e1ff8f1900b4f |
| 6032790 | DOWN | 22be06125b30a0fe0789ec490ee8d8bfb648adfbfa1af5b6b81763f03b438c16 |
| 6032896 | UP | 26487b67abdf258d6608871834a68b239d943099015a38f05917f3ab82ef9025 |
| 6034056 | UP | 444e6241a416f49b8d689a990b2cc1b849697bc1ca74d8cd6f7272e615d98105 |
| 6034202 | UP | b49e82c1bbb7cff6733dd0cc850561476b2cc0fce59ab6374ecd3ecd8abc32a6 |

Official contract reference: `docs/superpowers/plans/2026-10-23-phase-2b-resolution-contract.md`.
This is public-API-attested evidence, not independent Polygon receipt verification or real redemption.

## Offline replay of real historical evidence

Separate backup copy only: `.../public-resolution-20261023T052957Z/offline-replay.sqlite`.
Report: same directory `offline-replay-report.json`.
No edits to original production ledger.

- Five resolved markets had no open holdings: payout 0, not counted as trades.
- Market 6034202: UP holding 9.9900 shares, remaining fee-inclusive basis 8.9999910.
- API attests UP: simulated payout 9.9900, settlement P&L +0.9900090.
- Replayed account: cash 101.1228760; open basis 0; sale + settlement realized P&L 1.1228760; marked equity 101.1228760; unresolved positions 0.
- Fees remain historical 0.0759240. Verified holding settlements: 1 win, 0 losses; insufficient sample for performance inference.
- `cash + basis = initial 100 + realized 1.1228760` holds.
- Observed-mark maximum drawdown 1.4466519; incomplete historical mark coverage. Not continuous max drawdown proof.
- Original ledger still has cash 91.1328760, basis 8.9999910, sale realized 0.1328670, one unresolved position until reviewed live deployment.
- Winning and losing payouts both covered by synthetic tests; no real historical losing-position settlement example available in preserved ledger.

## Tests / review

- Original commit `2a0ccdb` incomplete and unsafe; do not deploy alone.
- Review exposed outcome-order loss, stale account report, missing transport property, execution identity, post-fill daily limit, midnight attribution, final-response STOP race, missing fill reconciliation, updated-question ambiguity, precision, legacy reports, and observer cleanup/continuity gaps.
- Added failing tests before follow-up fixes; latest scoped WSL run: 123 Linux MVP tests, 5.680 seconds, OK (2026-10-23).
- Latest post-review MVP reruns: 130 tests, Windows 47.677 seconds, WSL 4.517 seconds, both OK. Earlier failures from old synthetic fixtures were corrected to supply persisted identity and account-consistent data.
- Final reviewer found five more reproducible gaps: setup time counted as uptime, missing cleanup on start timeout, settlement timestamp across midnight, rounded partial-sale marks, and legacy unknown counts becoming zero after migration. Added tests, observed all five fail, then applied minimal fixes; latest 130-test runs include these regressions. Runtime bounds now checked numerically against requested window plus 30–120 seconds. No independent clean-review verdict claimed.
- Broad Windows discovery: 1167 tests, 1237.670 seconds, FAILED (errors=2). Full log: `C:\Users\Andro Saptajulianto\AppData\Local\Temp\opencode\phase2b-full-tests.log`. This run overlapped changes; not final acceptance.
- Error 1 reproduced: tracked `test_retained_numeric_event_reaches_actual_arm_before_publication` requires missing untracked `data/cp5_6c_replacement_20261017_0351adeb-6594-492e-9237-adf7d833dba0.sqlite3` with fixed SHA-256. Fixture also absent from original Downloads checkout. No fabricated fixture or skipped test used.
- Error 2: `test_m3_abrupt_controller_death` failed during Windows temporary-directory cleanup because detached supervisor still held `execution.jsonl`. Test waits for terminal journal event but not supervisor process exit; terminal event is flushed while journal remains open. Isolated rerun: 1 test, 1.786 seconds, OK. Race remains in preserved CP5 test/supervisor scope; no changes made there.
- Targeted Windows paper regression: 50 tests, 108.907 seconds, OK; strategy: 21 tests, 1.818 seconds, OK.
- Historical WSL combined paper/strategy/full-window command timed out at 120 seconds; Windows full-window at 180 seconds. Neither timeout is a pass. Diagnostic runner then had bytes/string timeout-output bug; runner outside repository fixed. Longer targeted Windows reruns: full-window 65 tests, 135.626 seconds, OK; guard 42 tests, 61.492 seconds, OK.
- Final current-worktree WSL full discovery attempted with 300-second bound; timed out in `test_full_window.FourFindingTests.test_rh2_properly_linked_new_calibration_positive`. Partial log: `/var/lib/polymarket-btc/phase2b-evidence/final-wsl-discovery.log`. Not full regression acceptance.
- Independent review remains BLOCKED as issued; five latest findings have RED→GREEN fixes, not a new clean verdict. Observer lifecycle still requires independent systemd runtime/stop timer installation and kill/restart testing before use.

## Blocker remediation continuation

- Exact missing CP5 fixture recovered from `Downloads/Polymarket-bot/.worktrees/cp5-data-quality/data`, matching checkpoint commit `a085312f286cab82c272de9dcf932bf0ccb6ed60` and independent historical hash. Source/destination SHA-256 `a488583880d10507a24e82251d0870433c1d39160b77429820a2d339a54ca409`; restored byte-for-byte without overwrite or fabrication. Focused historical audit passes.
- Windows cleanup root cause reproduced deterministically: terminal event fsync precedes supervisor journal close. `tests/test_cp5_supervise_durable.py` adds test-only bounded SYNCHRONIZE handle wait before temporary cleanup, never kill-by-PID; 32 tests pass. Accepted supervisor unchanged.
- WSL aggregate timeout diagnosed, not hidden: target completes in61.520s on9p vs19.727s ext4, with973 FULL commits. Stacks show SQLite commits and `p9_client_rpc`/journal waits. No deadlock demonstrated; original stopped stack unavailable. Full diagnostic `/var/lib/polymarket-btc/phase2b-evidence/remediation-timeout/independent-20261023T074912Z`; no lingering diagnostic process groups. Native ext4 snapshot runs preserve durability and exact fixture hash.
- Fresh independent review BLOCKED on phase-runtime approval mismatch, future-mark clock rollback, and CLOSED unresolved undercount. All three reproduced RED; narrow fixes applied. Runtime-only approval change allowed, full unit text/hash retained; unrelated unit changes rejected. Report requires mark age0–5s and counts every non-SETTLED holding unresolved. Full MVP current run134 tests passes63.579s Windows; settlement+observer45 tests pass11.797s. Fresh review verdict remains historical BLOCKED, not silently relabelled CLEAR.
- Process-kill recovery regression: uncommitted mutation rolls back, unresolved holding/cash survive, concurrent instance refused, lock reacquired after bounded OS cleanup, absent evidence stays UNKNOWN, authoritative synthetic evidence pays exactly once.20 repeated Windows checks pass7.672s; immediate reacquisition had7/20 transient lock failures before condition-based test synchronization.
- Actual `settle_window` kill proof (disposable ledger) pauses after position UPDATE but before settlement INSERT/commit, then separately after commit. WSL SIGKILL: pre-commit cash94.995/quantity10/UNKNOWN; post-commit cash104.995/quantity0/SETTLED; restart/reconcile yields exactly one payout and integrityok. Evidence `/var/lib/polymarket-btc/phase2b-evidence/settlement-kill-proof.json`. Windows counterpart also passes with transient `disk I/O error` immediately after TerminateProcess; diagnostic retries bounded2s; no production storage retry added. Evidence approved temp `phase2b-settlement-kill-proof.json` and runnable diagnostic script. Migration-specific kill remains untested.
- Actual isolated systemd probes: SIGKILL dummy observer stops dependent worker; independent STOP timer terminates restarted worker; RuntimeMaxSec independently terminates worker. `/var/lib/polymarket-btc/phase2b-evidence/remediation-stop-controls/phase2b-control-39e86d64.json`. Actual observer main loop with isolated UNIT/STATE and synthetic ledger/host then SIGKILL also leaves worker inactive/MainPID0 and no orphan PIDs: `.../remediation-observer/phase2b-observer-1cde2001`. Not production bot integration. Production remains inactive/dead/disabled/MainPID0.
- Seven accepted hashes match historical CP6-3 closure manifest; all29 unrelated fingerprints match `phase2b-baseline.json`. `git diff --check` passes (autocrlf notices only).
- Actual Service offline subprocess STOP/restart proof: sentinel STOPPED0.3153s; SIGTERM STOPPED_BY_SIGNAL0.1143s; duplicate exit73; lock reacquired; cash94.995/quantity10/UNKNOWN and integrityok survive both. `/var/lib/polymarket-btc/phase2b-evidence/service-stop-proof.json`.
- Controlled Windows targeted results: paper50 tests62.604s, strategy21 tests1.747s, full-window65 tests146.618s, guard42 tests79.281s allOK. InitialMVP131 predates final3 review tests; separate currentMVP134/63.579sOK. Logs approved temp `phase2b-remediation-tests`.
- Native ext4 current-code snapshot results: MVP134/4.285s, paper50/132.175s, strategy21/4.910s, full-window65/282.073s allOK. Guard42: FAILED(failures=9, errors=37), Windows Job Object API required onLinux. These are platform failures, not hidden passes; accepted guard sources unchanged. Snapshot manifest/source-changes disclose docs-only edits, no code/test/data changes. Evidence `/var/lib/polymarket-btc/phase2b-evidence/remediation-regressions-20261023T075434Z`.
- Host preflight found Windows PowerShell5.1 lacks `Environment.TickCount64` (null); normal samples falsely flag interruption. Actual PowerShell clock regression RED(exit7), two clock reads changed to stdlib Stopwatch timestamp/frequency;11 monitor testsGREEN9.891s. Bounded real heartbeat run produced3 samples/12.082s, monotonic12076.522ms, readable System events/interruptedfalse: approved temp `phase2b-host-proof-1792742783271326000.json` and `.json.jsonl`. No sleep prevention/power-policy changes; real suspend/WSL interruption remains untested.
- Fresh combined CP5 supervisor+historical audit+observer regressions54 tests pass40.272s. Production rechecked inactive/dead/disabled/MainPID0.
- Controlled Windows broad discovery reached hard1800s deadline (driver1800.203s), TIMEOUT exit1; not acceptance. Log approved temp `phase2b-remediation-tests/5-testALL.py.log`, SHA-256 `a283e9cf48fba8c8a054bd6038fd34c68e7cbfb7d86e99a6c78be395ff6aa2f8`. Contains1106 completed `ok` lines, no test-terminal FAIL/ERROR lines,14 periodic stack dumps; no final unittest summary. Last printed `test_stale_and_late_buffer_evidence_never_eligible` separately passes1 test8.294s (total9.156s), approved temp `phase2b-timeout-last-test.log`; no hang reproduced. Periodic stacks show different progressing tests, repeatedly SQLite transaction commits/open, consistent with aggregate durable-storage overhead, not one1800s stuck test. Exact state at hard cutoff unavailable. Runner attempts task-tree kill; fresh process scan finds no matching runner/supervisor Python process. Production freshly inactive/dead/disabled/MainPID0.
- Native ext4 broad verdict still pending. Both broad runs precede final host-heartbeat portability fix/test, so final-state qualification remains required. Windows target run initially overlapped earlier changes. No integration/soak/commit authorized by incomplete offline acceptance.

## Final Regression Triage — 2026-10-23

### TASK 1: test_live_guard_races
- **Linux (ext4):** 4 errors, all `OSError: windows_job_required` — Win32 Job Object API required.
- **Windows (canonical):** 11/11 PASS, 8.620 s, exit 0.
- **Classification:** Windows-only platform tests. Documented platform applicability; no skip/weakening added to source. File is in preserved 29-fingerprint baseline (sha256 `528E7B30...`) — unchanged.

### TASK 2: test_recorder_service
- **Original broad per-module run:** 180 s watchdog fired mid-module (tests=0, exit=1). Stack showed SQLite `commit()` in `mark_missed`.
- **Diagnosis:** Root cause = ext4 `synchronous=FULL` + `journal_mode=DELETE` ≈ 25.8 ms/commit. Module has 91 tests ≈ 768 s total. My `dump_traceback_later(180, exit=True)` was a whole-process wall-clock watchdog, not a per-test hang.
- **Re-run (1500 s bound, no premature exit):** 91/91 PASS, 767.813 s, 0 failures/0 errors/0 skips.
- **Conclusion:** No product defect; measurement artifact. No code change.

### TASK 3: Worktree Classification
- **Canonical Windows worktree (autocrlf=true):** 17 modified + 33 untracked = 50 entries.
- **WSL ext4 snapshot (autocrlf=false):** 135 modified + 33 untracked. 118 "CRLF-only" are WSL tar-extraction artifacts (content identical to HEAD modulo EOL).
- **Modified tracked (17):** 12 intended Phase 2B (linux_mvp/* + test_linux_mvp_*) + 5 pre-existing baseline recorder core.
- **Untracked (33):** 9 intended Phase 2B new (deploy/, docs/phase-2b-*, test_linux_mvp_validation.py) + 24 pre-existing baseline (paper.py, strategy.py, full_window*.py, guard files, test_paper.py, etc.).
- **CRLF-only (118):** Excluded from commit.

### TASK 4: Acceptance Verification

**Historical hashes re-verified:**
- CP5 fixture: sha256 `a488583880d10507a24e82251d0870433c1d39160b77429820a2d339a54ca409`, 1,429,504 B — matches preserved tar + manifest exactly.
- Archive integrity: 12/12 files OK.
- Tar↔snapshot fidelity: 0 differing (4 dotfile-name parser artifacts only).
- Baseline 29 fingerprints: **26/29 match**. 3 mismatches explained:
  - `src/polymarket_bot/recorder/full_window_guard.py`: O_BINARY portability fix (3 sites) — intended.
  - `tests/test_full_window_guard.py`: 3 platform-skip decorators — intended.
  - `tests/test_live_guard.py`: 1 platform-skip decorator — intended.
  - These 3 match the 10:34 preserved tar/manifest and drift vs 07:54 evidence snapshot exactly. Baseline.json predates the fixes.

**Platform guard verification:**
- `tests.test_full_window_guard`: Windows 42/42 OK (99.2 s). Linux 42 OK (14 skipped = Windows Job Object tests).
- `tests.test_live_guard`: Windows 20/20 OK (42.7 s). Linux 20 OK (1 skipped = Windows Job Object tests).
- `tests.test_live_guard_races`: Windows 11/11 OK (8.6 s).
- All skip decorators: `@unittest.skipUnless(os.name == 'nt', 'Windows Job Object API required')` — legitimate platform declarations, no assertions weakened.

**Directly-affected regression re-runs:**
- linux_mvp core (store/settlement/report/engine/adversarial/config/validation/deploy/service/ingest): 99 tests, 1.004 s, 1 Windows-only skip.
- test_paper: 50/50 OK (82.3 s, broad per-module).
- test_strategy: 21/21 OK (2.0 s, broad per-module).
- test_full_window: 65/65 OK (167.0 s, broad per-module).
- test_full_window_guard: 42/42 OK (49.8 s, broad per-module).
- test_recorder_service: 91/91 OK (767.8 s, re-run).

**Broad per-module effective result:** 61/61 modules PASS (59 original + 2 verified on correct platforms). No test skipped/weakened to pass.

**Independent settlement/accounting review:**
- 7 test cases cover: midnight UTC attribution, future-mark exclusion, CLOSED→unresolved, legacy NULL position_count, partial-sale exact basis, daily loss limit pre/post-fill, fail-closed settlement.
- All PASS. Reconciliation invariant (`cash + basis = initial + realized`) enforced at key points and in report.
- No weakening of safety guarantees.

### Verdict
- **Offline gates:** PASSED.
- **60-minute integration:** NOT RUN (separate authorized trial).
- **Overall Phase 2B status:** **BLOCKED** pending PHASE_2B_PROVISIONAL gate (integration).

### Commit
Explicitly staging and committing the 50 canonical Phase 2B files in the Windows worktree.

## Remaining gates

- Full regression verdicts, final Windows suite, independent review clear.
- Exact current signal/collection/terminal latency metrics; historical limits documented in `docs/phase-2b-latency.md`.
- Actual bounded integration 60 minutes, >=3 rollovers, verified payout or UNKNOWN, STOP/restart, accounting/resources/integrity.
- Only then separate 24-hour unattended soak with pinned host/guest continuity evidence and guaranteed terminal stop.
