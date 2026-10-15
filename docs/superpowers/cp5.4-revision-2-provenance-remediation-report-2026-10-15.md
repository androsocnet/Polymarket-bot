# CP5.4 REVISION-2 PROVENANCE REMEDIATION REPORT — 2026-10-15

## Pre-run frozen declaration

User explicitly authorizes one new disposable120-second public read-only smoke after clean independent review, local commits, and current targeted checks. No second run regardless of outcome. No opening, rollover or pilot execution in this phase. No code fixes or tuning.

Reviewed scope exactly: src/polymarket_bot/recorder/service.py, src/polymarket_bot/recorder/store.py, tests/test_free_persistence.py, tests/test_recorder_service.py. Independent cp5.4-rereview-2026-10-15.md accepts scoped SPEC/QUALITY H0/M0, not runtime PASS. Full latest remediation report read. Native Git diff/status/log10 inspected; current targeted ownership/delayed membership/repeat-drain checks 3 PASS /2.039s. Full723 PASS/985.289s and scoped Pyright current132/baseline134, added identities[], two removed, warnings/informations0 remain attributed final evidence; no Python edits since. Lint N/A.

Implementation/evidence commit: 983ef534082b61e7b423733dba7cbf4213060ee3, parent e9f254a5eb5251c5d99d4c9b6ee2304ee1ec2824. CP5.3 history preserved. Nine intended files committed (four Python and five CP5.4 reports). Initial staged diff check caught report-only trailing blank line; removed, staged check clean before commit. No DB/unrelated artifacts staged.

Runner scripts/cp5_run.py read in full. Original failed command from cp5.3-task9-final-report-2026-10-14.md:98–106 uses --seconds120 and original DB data/cp5_3_task9_once120_20261014.sqlite3. Original SHA256 expected cae48b3e05f446f02a2ac77b9f460d2b7385d1bee42f0451f6207925017a823f; preserve identity, no repair/reuse.

New command, frozen before execution:

```powershell
$env:PYTHONPATH='src'; & '.venv\Scripts\python.exe' 'scripts/cp5_run.py' --db 'data/cp5_4_task9_once120_20261015.sqlite3' --seconds 120
```

Absolute new DB: C:\Users\Andro Saptajulianto\Downloads\Polymarket-bot\.worktrees\cp5-data-quality\data\cp5_4_task9_once120_20261015.sqlite3. Test-Path data=True, docs/superpowers=True, new DB=False. Disposable DB never committed. Cooperative deadline120s; supervisor watchdog165s then kill/15s communicate; tool timeout210000ms; no alignment wait. Child rejects existing path and bounds outside120/1200.

HTTPS public allowlist only: Coinbase BTC-USD ticker, Kraken XBTUSD ticker, Gamma discovery/metadata, CLOB time/books/metadata. Public POST /books is read-only. Strict methods/selectors/body; credentials/auth/cookie/key/signature headers and redirects rejected; trust_env=False, auth=None, transport retries0. No wallet, signing, trading, private endpoints or Chainlink auth.

Analyze new DB read-only. First every completedv2 fetch must have exactly one durable original submitted owner, unique observations/reference/completion, exact run/market/slot/index/target/scheduled identity and full padded embedded scheduler membership, then exact replay. Any missing authority means immediate FAIL and no escalation. Frozen gates: every postwarmup selected assessment VALID and COMPLETE/DEGRADED, none INSUFFICIENT;15 consecutive scheduled opportunities with own completion span>=28000000000ns;>=1 COMPLETE paired slot. Lifecycle supervisor0/watchdogfalse/STOPPED exit0/empty stderr/closed client,transport,store/no retained threads/writer reacquired. Integrity ok/FK empty/schema2/zero exact contamination. Partial output never establishes PASS.

Runtime outcome pending; Task9 remediation reviewed and committed, smoke not yet executed at declaration. Original Task9 FAIL remains historical. Runtime artifact paths and A–W outcome follow after single run.

## A. Starting branch and history

feat/cp5-data-quality; original HEAD e9f254a5eb5251c5d99d4c9b6ee2304ee1ec2824; reviewed remediation commit983ef534082b61e7b423733dba7cbf4213060ee3. No amend/push/merge/reset/rewrite. CP5.3 commits preserved.

## B. Scope and authorization

Exactly one120-second smoke executed; no second run, code fix, tuning, subagent or escalation. verification-before-completion invoked. Current user authorization supersedes historical no-smoke wording in earlier reports only for this bounded phase.

## C. Original defect

Padded skipped scheduler membership pruned before serialization; ordinary recalibration changed nominal monotonic cadence; independent persistence calls allowed interrupted partial publication. See cp5.4-provenance-forensics-2026-10-15.md:13–18,59–101.

## D. Exact historical five rows

Historical failed DB remains FAIL, never repaired or reinterpreted. Full owner/send/slot traces: forensic report27–43; omitted rows49–55; retention63–69.

| Historical fetch | Original submitted opportunity | Reference | Observations | Missing skipped opportunity | Receipt ns |
|---|---|---|---|---|---|
|600|598|26|51,52|277|25811687000000|
|694|615|28|55,56|278|25815437000000|
|699|697|29|57,58|279|25817078000000|
|708|704|30|59,60|280|25819218000000|
|713|711|31|61,62|281|25821062000000|

These are five completed fetches missing embedded historical authority, not five missing provider acquisitions. Original skipped IDs277/278 BEFORE_START,279/280/281 PROCESSING_OVERRUN. No invented observations/completions for skipped rows.

## E. Reviewed remediation

Atomic single-writer publication, strict original/current owner binding, receipt/origin padded retention, bounded authority4096 IDs with explicit overflow rejection, nominal approximate grid separate from active acquisition safety. Schema/analyzer/gates/clock uncertainty/freshness/discontinuity/revision1 unchanged. Full changes and fault evidence: cp5.4-remediation-report-2026-10-15.md.

## F. Independent review

cp5.4-rereview-2026-10-15.md:5,24–59: SPEC/QUALITY accepted scoped pre-live, H1–H4/M1 addressed, H0/M0. Runtime acceptance comes only from evidence below.

## G. Tests

Attributed final full discovery723 PASS985.289s exit0; focused266 PASS238.834s; scheduler188 PASS863.114s. No Python edits since. Fresh current targeted delayed-window exact replay/current owner/repeat-drain3 PASS2.039s. Exact targeted command: `.venv\Scripts\python.exe -m unittest tests.test_free_persistence.ResearchPersistenceTests.test_delayed_window_dispositions_replay_exactly tests.test_free_persistence.ResearchPersistenceTests.test_current_acquisition_owner_and_original_types_are_bound tests.test_free_persistence.ResearchPersistenceTests.test_duplicate_actual_drain_preserves_every_row_and_memory -v`, PYTHONPATH=src. Full suite deliberately not rerun per current instruction.

## H. Pyright and lint

Attributed final four-file comparison against untouched6e1f582: current132/baseline134 errors; exact unchanged-line mapped added identities[], two removed service optional-anchor diagnostics; warnings/informations0. Not globally clean. Raw JSON/comparison C:\Users\ANDROS~1\AppData\Local\Temp\opencode\cp54-dnb5ovnq; existing cp54_compare.py harness. Lint N/A per owner. No Python edits requiring rerun.

## I. Frozen run command and path

Exact command/path/bounds recorded above BEFORE run. New data/cp5_4_task9_once120_20261015.sqlite3, never existing/failed path.120s acquisition/165s watchdog/210000ms tool; no alignment, extension or repeat.

## J. Public boundary

Runner unchanged and read in full. Strict HTTPS allowlist/auth rejection described above. No credentials, wallet, trading, signing, private endpoint or Chainlink auth.

## K. Runtime identity and result

Run41aba517-ef47-41ac-af56-a6b8b616e829; market5689566. Supervisor returncode0, watchdog_killed=false, elapsed120.67100000000028s, stderr empty. Child STOPPED/exit_code0/STOP_REQUESTED,60 finalized slots. Persisted start1792078226757712us/end1792078346865442us. Bounded cleanup is not additional acquisition authorization.

## L. Durable ownership reconciliation FIRST

Read-only immutable connection, all durable event JSON scanned.60 completedv2 fetches,120 observations,60 references. Every fetch has original durable submitted opportunity, exactly one completed terminal, identical run/market/slot ID/index/target/scheduled identity. Every observation/reference uniquely current-fetch-owned, no orphan or duplicate. Every padded scheduler membership equals full durable same-run authority, no missing/extra/duplicate IDs. Reconciliation errors[]. Per-fetch exact rows preserved in raw reconciliation artifact below. Frozen exact replay independently validates embedded contexts, original identities, provider/transport ownership and full membership.

## M. Exact replay

PASS: matched60,observations120,exact=true,qualification=RECONSTRUCTED. Read-only reconciliation/replay completed before frozen gate analysis. No partial replay claim.

## N. Postwarmup quality

30 FULL reports, all selected value_status VALID:22 COMPLETE,8 DEGRADED,0 INSUFFICIENT.30 WARMING_UP reports do not qualify. Across all60 reports:23 COMPLETE,35 DEGRADED,2 INSUFFICIENT; those two insufficient are warmup only. No warmup result substituted for postwarmup sufficiency.

## O. Exact continuity

Frozen smoke_continuity PASS,reasons[],post_warmup_exclusions[]. Exactly15 consecutive opportunities,reference31–45,slot index44–58,nominal spacing2000000000ns. Opportunity IDs441,451,458,465,472,479,486,493,568,575,582,589,596,603,610. First completion12646906000000ns,last12675593000000ns; own completion span28687000000ns>=28000000000ns. No compressed longer chain substituted.

## P. Paired books

60 COMPLETE slots,14 MISSED. Frozen paired-book predicate and smoke_gate=true establish at least one COMPLETE slot with exactly UP/DOWN VALID books. No pilot/full-market availability claim follows.

## Q. Lifecycle and storage

worker_active=false;client_closed=true;transport_closed=true;store_closed=true;remaining_threads=[];writer_reacquired=true. Integrity['ok'];FK[];user_version2. Exact contamination reference_reports0,reference_fetches0,linked_associations0; reference_activation DISABLED. Supervisor/lifecycle requirements PASS.

## R. Custody and hashes

Original failed data/cp5_3_task9_once120_20261014.sqlite3 SHA256 freshly matches cae48b3e05f446f02a2ac77b9f460d2b7385d1bee42f0451f6207925017a823f. Hash read only, no original SQLite reopen/repair. New disposable DB SHA256 e7cbc8b03a06113cba319c92f6ab44f0432c24bdbe5a1f16d4d7412e3462413a. Neither DB committed.

## S. Durable runtime artifacts and analysis command

- Full supervisor/child/send/lifecycle JSON: C:\Users\Andro Saptajulianto\.local\share\opencode\tool-output\tool_140317ad7001pQiaPP22VEAVGj
- Full first reconciliation per-fetch rows, exact replay reports, lifecycle and hashes: C:\Users\Andro Saptajulianto\.local\share\opencode\tool-output\tool_14032020a001G3ho1EgqA9iJNj
- Full frozen analyzer JSON: C:\Users\Andro Saptajulianto\.local\share\opencode\tool-output\tool_140322ccf001cIr5mMLDYbb2KL

Exact frozen analysis command: `$env:PYTHONPATH='src'; & '.venv\Scripts\python.exe' scripts/cp5_analyze.py data/cp5_4_task9_once120_20261015.sqlite3`. Analysis read-only; no gates changed. Raw outputs complete, structured summaries parsed from entire JSON, not truncated console.

## T. Commits and recovery ledger

Implementation/evidence983ef534082b61e7b423733dba7cbf4213060ee3. Runtime report documentation commit follows, hash recorded in ignored recovery ledger to avoid self-referential commit hash. Ledger .superpowers/sdd/2026-10-14-cp5-3-research-quality-plan/progress.md updated. Only explicitly intended reports staged; unrelated CP5.3 reports/plans/root reviews remain untouched/untracked.

## U. Remaining risks

One bounded smoke proves these frozen gates for this dataset only. No universal availability, true-UTC ordering, settlement/trading readiness, opening, rollover,1200-second pilot or full-market causal coverage certification. Existing132 scoped baseline-related Pyright errors remain. Bounded4096-event authority overflow fails closed; runner audit deque retains existing limit. Original historical failed dataset remains FAIL.

## V. Controller handoff

Task9 remediation offline accepted/committed and this replacement bounded smoke PASS. Tasks1–7 remain DONE. Opening/rollover/pilot NOT RUN. No second smoke. Gate evidence returned only; controller must activate approved Task8. This phase does not execute opening itself.

## W. Final verdict

CP5.4 bounded replacement smoke PASS — exact provenance/replay, frozen research-quality continuity and lifecycle/storage gates satisfied. Controller handoff only; no opening/pilot activation executed.
