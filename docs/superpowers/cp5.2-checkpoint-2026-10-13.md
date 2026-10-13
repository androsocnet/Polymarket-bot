# CP5.2 REFERENCE QUALITY REMEDIATION CHECKPOINT REPORT

## A. Verdict
CP5 FAIL — data/runtime quality remains insufficient
Independent reviews ses_ecc259581ffe6C7wwNTm2dhSxl and ses_ecc148a75ffe5ZxA248I0ZOhIu supplied by controller; final reviewer approved one fresh120, no remaining High/Medium. Evidence-loss and shutdown fixes RED/GREEN verified. Fresh120 strict gate failed empirically; live phases stopped. No opening preparation or pilot. Prior nested-tool blocker ruling withdrawn, not a project blocker.

## B. Authority and preservation
Starting branch feat/cp5-data-quality, HEAD 4adaccea23f8757e65b701cec65e7076b7603e9d verified. All inherited WIP preserved. No reset, restore, stash, clean, push, merge, schema change, trading, private API or credential access. One authorized bounded public-only smoke executed. Local verified commits and report/evidence creation explicitly authorized; inherited WIP preserved and reviewed before checkpoint commits.

## C. Read-only evidence identity
SQLite URI mode=ro used throughout. Source data/cp5_remaining_smoke_20261013.sqlite3 SHA256 8d9a9b84775a032e3aac7c5e83cd9f0bed1dffdd808ec5384942badb97bb08be. Reconstruction artifact docs/superpowers/cp5.2-reconstruction-2026-10-13.json includes all 57 reports, 114 observations, full analyzer output, anchors and every consecutive provider gap. Runnable check: PYTHONPATH=src;. .venv/Scripts/python.exe scripts/cp5_reconstruct.py data/cp5_remaining_smoke_20261013.sqlite3. Assertions verify report count, selected-provider contributing count, diagnostic reconciliation and exact replay.

## D. End-to-end trace
scripts/cp5_run.py constructs public-only audited transport, new v2 store, Coinbase/Kraken providers and CP5Recorder. Recorder._record arms future slot, runs housekeeping, waits, checks clock and classify_wake, reserves slot, drains/submits approximate worker, collects books, finalizes and drains. _approximate_submit snapshots anchor and uses bounded shared slot deadline. Session.fetch serially requests Coinbase then Kraken, adds per-provider history, computes report after both receipts, evicts stale history after calculation, selects primary whenever value exists. _approximate_drain persists observations, report and fetch-membership event. Analyzer replay rebuilds same histories and checks every report field. Provider parser clears price/bid/ask on rejection; after reviewed evidence fix it retains parsed source only for FUTURE_SOURCE_TIME, with rejection unchanged. Store retains hash/size, not exchange raw bytes.

## E. Per-report reconstruction
Each artifact record contains report receipt/window wall/server/monotonic values, provider accepted IDs, exclusions and error codes, oldest/newest contributing rows, every consecutive wall and monotonic gap, maximum gap, head/tail offsets, exact diagnostic arrays and post-report eviction IDs. Accepted means positive finite successful price passing engine exclusion checks; rejection reason precedence remains error, future, stale. Nothing inferred as source timestamp.

## F. Exact categories: 57 reconciled
OBSERVATION_GAP alone: 13.
EXCLUDED_ERROR_OBSERVATIONS + OBSERVATION_GAP: 19.
EXCLUDED_STALE_OBSERVATIONS + EXCLUDED_ERROR_OBSERVATIONS + OBSERVATION_GAP: 25.
Totals 57; OBSERVATION_GAP 57, error-exclusion 44, stale-exclusion 25. Selected primary diagnostic-clean 0. Exact replay 57 reports / 114 observations.

## G. Mathematical invariant
Engine independently evaluates each provider: first accepted receipt more than 2,000,000 us after sliding-window start, any consecutive accepted wall gap greater than 4,000,000 us, or last receipt more than 4,000,000 us before report end produces OBSERVATION_GAP. Equality permitted. Nominal slot cadence 2,000,000 us is not actual receipt cadence. Report end follows serial Kraken request; primary receipt therefore normally earlier than report receipt. No interpolation, synthetic points or look-ahead added.

## H. Gap causes, independently counted
Coinbase: head-only 21; head plus interior 10; interior-only 26. Thus head condition 31 and interior condition 36; tail condition zero. Maximum accepted interior wall gap 4,024,521 us.
Kraken: head-only 32; no gap conditions 25; interior/tail zero. Maximum accepted interior wall gap 3,732,348 us. Kraken diagnostic-clean-by-gap does not trigger fallback when primary still has finite in-window mean. These counts are evidence, not tuned thresholds.

## I. Warmup and jitter
Run-based post-warmup count 29. Early head gaps express unavailable pre-start history, not failed requests. Even after warmup Kraken head offsets include 2,045,859, 2,055,985 and 2,111,492 us, showing nominal cadence plus receipt phase/jitter can fail strict 2s head allowance. Coinbase interior gaps just above 4s follow rejected samples; all reports retaining those intervals are flagged. No evidence supports silently redefining warmup or relaxing limits to this dataset.

## J. Exclusions and eviction
Historical error rows 27, 41, 87, 95. Error exclusion persists while rejected row remains in queue; stale successful rows can emit stale diagnostic on calculation before post-report eviction. Artifact records exact evictions. At most 57 fetches occurred, fewer than capacity 64; no capacity eviction possible. Eviction is after computing each provider report, not before. No future-exclusion engine category in stored selected reports: provider-level future rejection becomes error observation first.

## K. Coinbase future-source evidence
FUTURE_SOURCE_TIME observations 27, 87, 95 correspond to references 14, 44, 48. Stored source timestamps and prices are NULL; raw bodies unavailable. Hashes and sizes retained (185, 183, 184 bytes); HTTP status 200. request_attempts has zero exchange ticker rows/raw payloads. Hash cannot reconstruct source time. Thus exact future delta, payload timestamp, cause and magnitude cannot be recovered.

## L. Clock limits
For observation 27 wall minus projected server = -82,431 us, anchor uncertainty 1,132,500 us. Observation 87: +252,818 us, uncertainty 1,101,500 us. Observation 95: +246,666 us, same uncertainty. Current CP5 providers are constructed without project callback, so parser future-source check used local wall, not server; session adds projected server after provider returns. CLOB uncertainty includes integer-second precision plus half RTT. Uncertainty does not prove local-wall/source skew, nor justify accepting missing rejected timestamps. No tolerance change made.

## M. Fallback per rejection
Reference 14 after FUTURE_SOURCE_TIME has 13 accepted primary observations; reference 21 after HTTP_ERROR has 19; reference 44 after FUTURE_SOURCE_TIME has 28; reference 48 after FUTURE_SOURCE_TIME has 27. Each source_provider=coinbase, was_fallback=0. This matches existing selection: finite nonstale primary history means no switch even if latest request rejected or OBSERVATION_GAP present. Gap-quality fallback would change policy.

## N. Approved design comparison
Approved docs/superpowers/specs/2026-10-12-free-reference-design.md defines sampled arithmetic mean, fewer observations across gaps, explicit missing/stale primary fallback and nominal REST cadence sufficient for development/research; it does not define 2s/4s quality allowances. Strict cadence confuses scheduled requests with receipt timing. Bounded-gap quality preserves an explicit coverage warning but requires principled boundary allowance, warmup semantics and independent review before changes. Provider-availability semantics matches mean/selection policy but does not certify full coverage or erase observed gaps. Independent recommendation and controller ruling preserve C value availability/fallback, retain A strict legacy gate, leave B separately principled assessment unspecified. No dataset thresholds tuned. Confirmed fixes independently approved before smoke; no strict threshold or acceptance change.

## O. Predeclared fresh-smoke gate
Before any future fresh smoke: independent semantic and WIP review complete; confirmed fixes covered RED/GREEN; full/focused suites green; tooling Pyright green and inherited debt documented; diff check clean. New disposable v2 DB only; public allowlist and bounded supervisor 120s +45s; lifecycle closed/no worker/no threads/writer reacquired. Existing analyzer smoke_gate unchanged: all values present, integrity OK, no FK errors, schema2, exact replay, zero linked Chainlink evidence, at least one gap/missing-free reference, at least one COMPLETE slot, all runs STOPPED. No new percentage, duration extension or gate relaxation. If fresh smoke fails, stop live phases and record CP5 FAIL. Pre-smoke declaration: controller final reviewer ses_ecc148a75ffe5ZxA248I0ZOhIu approved ONE fresh120 with no remaining High/Medium; reviewer ran129 offline tests and verified historical57/114 replay, unchanged SHA and FAIL gate. Fresh path data/cp5_2_fresh120_20261013.sqlite3; no existing DB overwrite. In addition to unchanged analyzer gate, require supervisor returncode=0, watchdog_killed=false, empty stderr, child result exit_code=0/state STOPPED, client/transport/store closed, worker inactive, no remaining threads and writer reacquired. STOPPED DB alone insufficient. Declaration saved before live command.

Fresh smoke evidence: runtime docs/superpowers/cp5.2-fresh120-runtime-2026-10-13.json; complete reconstruction/analyzer docs/superpowers/cp5.2-fresh120-analysis-2026-10-13.json. DB data/cp5_2_fresh120_20261013.sqlite3 SHA256 5b768bf1e710fb140fa6bf395a8d9501502c8436bc834ca4705b14c8836211f0. Run ec5c80ce-9df5-4b2d-b1ef-7e7352d29d8a started1791872959984352/end1791873080066518, span120.082166s. Supervisor returncode0, watchdog=false, elapsed120.593s, stderr empty. Result STOPPED exit_code0,57 finalized; worker inactive, client/transport/store closed, no threads, writer reacquired. Actual-send records179 match final sends179. Lifecycle gate passed, quality gate failed.

Integrity OK, FK errors none, schema2, Chainlink reports/fetches/linked associations zero. Replay exact57/114. All57 values available but OBSERVATION_GAP57, gap/missing-free0, run-age postwarmup31 and gap/missing-free0. Categories: gap-only29, stale+gap8, stale+error+gap18, error+gap2. Coinbase head-only17/head+interior13/interior-only27; Kraken head-only19/head+interior11/interior-only17/no-gap10. No tail violations. Strict gate not modified.

Coinbase53/57 accepted, FUTURE_SOURCE_TIME1,HTTP_ERROR2,TRANSPORT_TIMEOUT1; accepted monotonic gap median2s,p95 4.016s,max4.609s;3 gaps>4s,zero>6s. Kraken56/57,DEADLINE_EXPIRED1;median2.016s,p95 2.375s,max4.797s;1 gap>4s,zero>6s. Wall maxima4.608257s/4.801739s. Fallback0: references38/42/55/57 retain28/27/28/27 valid primary history after Coinbase rejection. Last fetch Coinbase timeout exhausted deadline, Kraken deadline-expired; no invented HTTP response. All exclusions and eviction IDs in artifact.

Fresh FUTURE_SOURCE_TIME observation75/reference38 retains source1791873042866047, wall1791873042839308, projected server1791873042265500: source-wall+26739us,source-server+600547us,anchor uncertainty1093500us. Price/bid/ask NULL,error retained; not accepted. CLOB uncertainty cannot justify wall-clock tolerance; raw payload unavailable, hash/size186 retained. No claim of source-clock cause.

Books56 COMPLETE,1 FAILED; old market5599350 slots433–449 paired17,closing present;next5599430 paired39 slots1–39,opening absent,slot40 ATTEMPTED_LATE. Grid diagnostic UNSENT433 old,UNSENT1/ABSENT409 new reflects partial bounded run, not full-window coverage. Full-window FAIL.53 matched exchange pairs: disagreement USD median9.81,p95 16.85,max21.61;bps median1.17306878,max2.58507078;receipt offset median0.203s,max0.282s,not simultaneous trades.

## P. Future market and opening
Current-only acquire_market occurs after rollover; existing smoke next market starts slot1, opening absent. Aligned runner sleeps until boundary before startup, so alignment alone cannot ensure slot0. Authoritative future-market preparation investigation/implementation conditional on reviewed green smoke; not reached. No guessed slug identity, timestamp substitution, unarmed backfill or architecture change. ONE 1200 pilot unrun; no pilot DB created.

## Q. Preserved WIP validation
Latest full unittest discover -s tests: 625 tests, 267.927 seconds, OK. Latest FREE_REFERENCE/CP5/CP3 focused command matching CP5.1 section T: 157 tests, 19.893 seconds, OK. New regression RED command ran timestamp persistence and shutdown tests: 2 failures in 0.093s, persisted timestamp None versus 2000000001000000; worker.stop called zero versus expected once. Same command GREEN: 2 tests, 0.091s. Added hand-authored replay fixture with rejected future timestamp and explicit expected Kraken report, no report generated by engine. Initial full625/focused157 each failed stale test_no_lookahead_future_timestamp expecting cleared timestamp; updated expectation to retained exact timestamp and NULL price. Negative argparse/lifecycle fixture output expected.

## R. Typecheck and whitespace
pyright --pythonpath .venv/Scripts/python.exe scripts/cp5_run.py scripts/cp5_analyze.py scripts/cp5_reconstruct.py tests/test_cp5_safety.py: final 0 errors/warnings. Initial reconstruction had two optional-operand errors, corrected explicit None guards and rerun. service.py: 22 diagnostics; test_recorder_service.py: 51 diagnostics. No unrelated debt fixed. Exact identity-matched HEAD baseline not performed; no no-new-diagnostics claim. git diff --check exits clean with CRLF warnings. pyproject.toml provides no lint command.

## S. Review gate and risks
Independent review ses_ecc259581ffe6C7wwNTm2dhSxl confirmed scheduler/public isolation/fallback correct. HIGH evidence destruction fixed: retain parsed FUTURE_SOURCE_TIME timestamp, keep price/bid/ask NULL and error status. HIGH clock inconsistency documented, acceptance unchanged pending explicit justified clock policy. MEDIUM shutdown flush failure fixed using finally to invoke base cleanup, duplicate flush removed. Final reviewer ses_ecc148a75ffe5ZxA248I0ZOhIu approved fixes and ONE fresh120, ran129 offline tests, confirmed historical replay57/114 and SHA unchanged. No remaining High/Medium. Metric named diagnosticclean actually means gap/missing-free; it does not mean all diagnostics empty. This report distinguishes that metric without schema or gate changes. Risk: reconstruction relies on same engine for exclusion checks; exact explicit gap math and persisted replay cross-check reduce but do not eliminate shared implementation blind spots. Script intentionally asserts this historical 57-report dataset, not general-purpose recorder architecture. Historical timing identities predate inherited identity fix. No assertion of causal explanation for earlier UNKNOWN slots.

## T. Commits and final state
Local code checkpoint 51a7645: fix: preserve rejected reference evidence and guarantee shutdown cleanup. Includes coherent inherited scheduler/timing/analyzer WIP and new confirmed fixes/tests/reconstruction; no WIP discarded. Separate documentation/evidence checkpoint follows this report. DBs remain untracked/ignored, no schema changes. Final git status checked after documentation commit; no push/merge. Exact identity-matched Pyright baseline not attempted; no delta claim. Service22 and test_recorder_service51 historical diagnostics remain documented, not fixed.

## U. Remaining requirements
Controller ruling: C provider-value availability/fallback matches approved design; A strict legacy coverage retained for fresh gate; B principled separate quality assessment not yet specified, no invented bounds. Availability is not coverage certification. Fix review complete and exactly one fresh120 ran. Strict gate=false; all later live phases stopped, no opening preparation or pilot. Historical future deltas remain unrecoverable. Fresh future rejection now retains timestamp but clock policy remains unchanged; availability does not certify research usability.

CP5 FAIL — data/runtime quality remains insufficient
