# CP5.1 checkpoint — FAIL / incomplete

## A. Verdict
CP5 full-window quality remains FAIL. Safe scheduler and diagnostic fixes verified; new instrumented smoke ran and failed existing reference-quality smoke gate. Conditional aligned pilot was not run. No claim of CP5.1 completion.

## B. Authority
No subagents used by this implementer continuation or final-review continuation. Controller agents occurred earlier, as supplied by user; this is not a global no-subagents claim. No commits, credentials, authentication, schema changes, merge, push, trading, destructive commands, gate relaxation, fabricated evidence, or unarmed backfill. Final-review continuation made no network requests or live runs. New public-only 120-second smoke exercised explicit authorization. Existing evidence opened using SQLite URI mode=ro.

## C. Baseline
Branch feat/cp5-data-quality. Final-review starting HEAD and final HEAD both 4adaccea23f8757e65b701cec65e7076b7603e9d (checkpoint 4adacce), verified with `git rev-parse HEAD`; branch verified with `git branch --show-current`. Inherited uncommitted changes preserved. Historical DB: data/cp5_new1200b_20261013.sqlite3. New DB: data/cp5_remaining_smoke_20261013.sqlite3. Files changed this continuation: scripts/cp5_run.py, scripts/cp5_analyze.py, src/polymarket_bot/recorder/service.py, tests/test_cp5_safety.py, tests/test_recorder_service.py, this report.

## D. Opening
Next-market preparation remains UNMET. CP5-only future-metadata preparation was not implemented; no future timestamp substitution. Current discovery remains current-only and opening slot remains insufficient. Missing preparation is an unresolved feature, not a fundamental architecture blocker. Smoke did cross rollover and captured next market slot 1, not slot 0.

## E. Scheduler RED
Added test_armed_target_survives_housekeeping_at_target using real RecorderFixture/adapters/store. Housekeeping advances exactly to armed target. Command: `$env:PYTHONPATH='src'; & '.venv\Scripts\python.exe' -m unittest tests.test_recorder_service.RecorderServiceTests.test_armed_target_survives_housekeeping_at_target`. RED: captured [152], expected [151], 1 failure in 1.672 seconds. Plain `python` was unavailable (Windows Store alias); existing virtualenv used thereafter.

## F. Scheduler GREEN
Removed only post-housekeeping `monotonic_ns() >= target_ns` abandonment. Revision identity and clock checks retained; `_wait` and `classify_wake` still enforce armed interval and window end. Same regression GREEN: 1 test, 1.691 seconds. No unarmed slot selection semantics changed. Existing resolution-delay regression expected old drop behavior; updated to captured slots [1,2], retaining one resolution request and retry assertions. Full-suite initial run exposed that mismatch explicitly.

## G. Timing persistence
CP5 timing buffer now writes pending events plus persistence marker inside one existing RecorderStore._transaction using existing _run/_insert methods; no schema/API change. Failed flush restores pending buffer and rethrows. No recursively buffered persistence tail. RED actual assertion: 2 BEGIN IMMEDIATE statements versus expected 1. Initial RED cleanup also raised Windows file-lock error because store remained open; test now closes store before assertions. Final suite verifies one transaction and empty tail.

## H. Timing limits
DIAGNOSTIC_PERSISTENCE explicitly records `excludes_commit=true`: measured interval is precommit insertion work, not full transaction latency. Actual flush can consume housekeeping budget; no claim of zero overhead. Crash can lose buffered markers. Shutdown retains inherited two flush calls; second is empty now. Smoke persisted 432 timing events across 7 batches, maximum measured precommit interval 16,000,000 ns. No full commit-overhead attribution claim.

## I. Per-run partition
Multiple participating runs now emit separate `runs[run_id]` 450-row grids rather than overwriting same slot indices. Single-run output retains existing rows/categories shape. Requests, anchors, assigned timing events and unassigned controls retain run/market filters. Multi-run regression also checks outcome categories; initial test failed because slot_diagnosis accepted only five arguments. Final suite GREEN.

## J. Partition limits
Unassigned control/clock context belongs to participating run, not asserted market cause. Multi-run output shape differs from single-run shape and must be handled by downstream consumers. No aggregate cross-run 450-row success claim.

## K. Outcome-aware classifier
Book-observation error codes now join request error codes. Persisted book status LATE produces ATTEMPTED_LATE; invalid/malformed book error produces ATTEMPTED_MALFORMED even with HTTP-success request. Added regression verifies malformed run a and late run b independently. Existing COMPLETE/PARTIAL precedence retained.

## L. Classifier limits
Categories are diagnostic outcome summaries, not causal proof. Substring classification remains bounded existing heuristic. Cause remains UNKNOWN unless separately established. No inferred historical timing events or invented dispatch timestamps.

## M. Historical window
Market 5593086, run e2b1c84e-b214-400e-b1ee-14e5f38c0104; start 1791860400000000 us. Exact 450 rows: 427 COMPLETE, 23 UNSENT. Run wall start 1791860400399205 us; wall end 1791861600555803 us. Startup 399205 us after opening. Fresh read-only analyzer confirmed counts and indices.

## N. Historical misses
All 23 have zero linked request attempts. Slot 0 startup BEFORE_START; slot 243 CLOCK_PAUSE with ANCHOR_EXPIRED_DURING_WAIT event at 1791860886036634 us. Remaining 21 UNKNOWN: 16, 65, 66, 88, 90, 92, 107, 189, 213, 236, 256, 318, 357, 359, 367, 373, 378, 393, 410, 414, 418. PROCESSING_OVERRUN labels do not establish root cause. Confirmed deterministic scheduler bug is not proof it caused these historical misses.

## O. Historical gaps
22 contiguous uncollected gaps: 21 single-slot gaps and one two-slot gap at 65–66; total 23 slots, maximum 2. Opening absent, closing present. Prior receipt leads preserved from inherited report (seconds before target): 16 1.711; 65 1.805; 66 3.805; 88 1.789; 90 1.727; 92 1.742; 107 1.7505; 189 1.7425; 213 1.7425; 236 1.7885; 243 1.8045; 256 1.8045; 318 1.7425; 357 1.7575; 359 1.7895; 367 1.7885; 373 1.7885; 378 1.7885; 393 1.7885; 410 1.773; 414 1.758; 418 1.805. Historical housekeeping/wake/dispatch/finalization timing unavailable.

## P. Live smoke command and evidence
Command: `Test-Path -LiteralPath 'data'; if ($?) { $env:PYTHONPATH='src'; & '.venv\Scripts\python.exe' scripts/cp5_run.py --db data/cp5_remaining_smoke_20261013.sqlite3 --seconds 120 }`, tool timeout 210000 ms; child watchdog 165 seconds. Raw command output retained by harness at C:\Users\Andro Saptajulianto\.local\share\opencode\tool-output\tool_133af8382001gbqT0ZktuXUwMU. Run 11b398c5-29bf-459f-9800-0e716de0939a STOPPED; start 1791868382766330 us, end 1791868502840238 us, span 120.073908 seconds. Final review extracted existing artifact without rerun: supervisor returncode=0, watchdog_killed=false, elapsed_seconds=120.5, stderr empty. Lifecycle worker_active=false, client_closed=true, transport_closed=true, store_closed=true, remaining_threads=[]. Final evidence independently repeats client/transport closed, worker inactive, no remaining threads, writer_reacquired=true. Actual-send lines=180, final sends list=180. These are captured prior smoke lifecycle observations, not a post-fix smoke certification.

Exact extraction command:
```powershell
& '.venv\Scripts\python.exe' -c "import pathlib,json; p=pathlib.Path(r'C:\Users\Andro Saptajulianto\.local\share\opencode\tool-output\tool_133af8382001gbqT0ZktuXUwMU'); lines=p.read_text(encoding='utf-8').splitlines(); records=[]; [records.append(json.loads(s)) for s in lines if s.startswith('{')]; r=next(x for x in records if 'watchdog_killed' in x); child=[json.loads(s) for s in r['stdout'].splitlines() if s.startswith('{')]; e=next(x for x in reversed(child) if 'result' in x); print(json.dumps({'supervisor':{k:r[k] for k in ('returncode','watchdog_killed','elapsed_seconds','stderr')},'lifecycle':[x['lifecycle'] for x in child if 'lifecycle' in x],'actual_send_count':sum('actual_send' in x for x in child),'evidence':{k:e[k] for k in ('client_closed','transport_closed','worker_active','remaining_threads','writer_reacquired')},'send_count':len(e['sends'])}))"
```

## Q. Smoke integrity and replay
Read-only analyze_db: integrity ['ok'], foreign keys [], user_version 2. Chainlink reference_reports=0, reference_fetches=0, linked_associations=0. Exact approximate replay: 57 fetches, 114 observations. 57 COMPLETE book slots; market 5597197 56 paired slots with closing 449 present, opening absent; market 5597287 one paired slot at index 1. Full-window verdict FAIL as expected for partial smoke. Reference pipeline unchanged.

## R. Smoke reference quality
Coinbase 53/57 accepted (92.982456%); errors FUTURE_SOURCE_TIME=3, HTTP_ERROR=1. Kraken 57/57 accepted. Coinbase accepted monotonic gaps median 2.0075 s, p95 4 s, max 4.031 s; 2 gaps >4 s, none >6 or >10. Kraken median 2.0235 s, p95 2.344 s, max 3.719 s; none >4. All 57 approximate references had OBSERVATION_GAP; diagnostic-clean 0/57 and post-warmup 0/29. EXCLUDED_ERROR_OBSERVATIONS=44, EXCLUDED_STALE_OBSERVATIONS=25; fallback 0. Matched exchange disagreement 53 pairs: USD median 4.8, p95 17.91, max 21.39; bps median 0.574979, p95 2.145249, max 2.556442. Receipt-offset median 0.203 s, max 0.25 s; not simultaneous trades. These are research diagnostics, not Chainlink or full-window usability.

## S. Smoke gate and pilot
Existing smoke_gate=false because diagnostic-clean references absent. No arbitrary percentage threshold added, no gate relaxed. ONE aligned 1200 pilot remains UNRUN because required smoke-pass condition failed empirically, not because 100% full-window gate was uncertain. No duration extension or second smoke performed. No pilot DB created. Full/research usability kept separate; numeric approximate values and exact replay do not imply diagnostic-clean quality.

## T. Tests
Final full command: `$env:PYTHONPATH='src'; & '.venv\Scripts\python.exe' -m unittest discover -s tests`: 622 tests, 254.990 seconds, OK after stale-identity fix. Previous pre-review GREEN: 621 tests, 255.089 seconds, OK. Prior full run: 621 tests, 263.379 seconds, one failure test_resolution_overrun_skips_target_and_does_not_redispatch_due_job due to old abandonment expectation; corrected as described above. Final focused command: `$env:PYTHONPATH='src'; & '.venv\Scripts\python.exe' -m unittest tests.test_cp5_safety tests.test_free_engine tests.test_free_coinbase tests.test_free_cli tests.test_free_mode_isolation tests.test_free_metrics tests.test_free_kraken tests.test_free_reference tests.test_cp3_review tests.test_cp3_regression tests.test_cp3_lifecycle`: 154 tests, 18.900 seconds, OK after stale-identity fix. Previous pre-review focused GREEN: 153 tests, 13.388 seconds, OK. Expected negative-test argparse/lifecycle output present.

## U. Pyright and lint
`pyright --pythonpath '.venv\Scripts\python.exe' scripts/cp5_run.py scripts/cp5_analyze.py tests/test_cp5_safety.py`: 0 errors, 0 warnings. `pyright --pythonpath '.venv\Scripts\python.exe' src/polymarket_bot/recorder/service.py`: 22 errors, 0 warnings (optional-member/access, optional-subscript, argument-type and possibly-unbound diagnostics). Initial run without --pythonpath reported 27 including five false missing-httpx imports. Exact identity-matched baseline comparison was NOT performed; no no-new-diagnostics claim. `git diff --check`: no whitespace errors, only CRLF conversion warnings. No configured lint command identified; no dependencies installed or unrelated debt changed.

## V. Fulfilled / unmet
Fulfilled: independent Medium stale-identity finding RED/GREEN; existing smoke supervisor/lifecycle/send evidence extraction; confirmed armed-target drop RED/GREEN; minimal production fix; batched timing transaction and no tail; multi-run grids; outcome-aware malformed/late evidence; historical 450-row read-only counts/gaps; actual new 120-second smoke; replay/integrity/isolation/provider quality; final full/focused tests; tooling Pyright. Unmet: future-market preparation and opening capture; smoke quality pass; conditional aligned 1200 pilot; saved full analyzer artifact; full timing attribution; identity-matched baseline service Pyright comparison. Original phase 0–12 text absent from repository; no fabricated phase mapping.

## W. Independent review
Medium finding addressed: CP5Recorder._timing retained prior armed identity after capture and tagged boundary/retry housekeeping with stale market/slot. Regression `test_timing_identity_ends_after_capture_or_changed_target` confirms matching armed housekeeping/wake/finalization retain identity, completed capture clears scope, and changed-target housekeeping/wait/wake remain unassigned. Exact RED/GREEN command: `$env:PYTHONPATH='src'; & '.venv\Scripts\python.exe' -m unittest tests.test_cp5_safety.CP5SafetyTests.test_timing_identity_ends_after_capture_or_changed_target`. RED: 1 test, 2 subtest failures, 0.118 seconds (identity not empty after FINALIZATION_END; market_id wrongly present on changed-target HOUSEKEEPING_START). Minimal fix clears identity after FINALIZATION_END and before merging a differing target_ns. GREEN: 1 test, 0.513 seconds, OK. No production scheduler/reference behavior changed this review. Existing smoke predates identity fix; its persisted timing identities may contain stale boundary/retry assignment and were not rewritten.

Final scoped Pyright commands in U rerun after fix: tooling 0 errors/0 warnings; service 22 errors/0 warnings. No clean-service or identity-matched baseline-delta claim. Final `git diff --check` clean except CRLF conversion warnings. Final `git status --short`:
```text
 M scripts/cp5_analyze.py
 M scripts/cp5_run.py
 M src/polymarket_bot/recorder/service.py
 M tests/test_cp5_safety.py
 M tests/test_recorder_service.py
?? docs/superpowers/cp5.1-checkpoint-2026-10-13.md
```

Review armed-target semantics against existing classify_wake (no unarmed backfill), private existing store batch use and precommit timing ceiling, multi-run output shape, outcome classification precedence, and smoke OBSERVATION_GAP evidence before any further live phase. CP5 remains FAIL/incomplete; safe code work verified, required opening and empirical pilot quality not achieved. No commits made. Historical 21 UNKNOWN remain UNKNOWN.
