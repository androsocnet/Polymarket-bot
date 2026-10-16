# CP5.6A final scoped independent rereview — 2026-10-16

## Verdict

**SPEC/QUALITY: PASS — H0 / M0 within the two-finding round2 scope. Both remaining Medium findings ADDRESSED. Offline READY for this scoped repair only; no whole-project or runtime acceptance.** Historical recovery remains **PARTIAL**. Original child launch, network activity and outcome remain **UNKNOWN**; original tool exit4 is not a child exit result. CP5 live gate remains **BLOCKED**. No live authorization.

## Scope and method

Fresh independent static review of only the two remaining findings in `docs/superpowers/cp5.6a-rereview-2026-10-16.md:25–39`, against actual `scripts/cp5_supervise_durable.py`, round2 regression bodies and surrounding cleanup/classification/inspection code in `tests/test_cp5_supervise_durable.py`, checkpoint round2 A–S (`docs/superpowers/cp5.6a-supervision-evidence-recovery-checkpoint-2026-10-16.md:310–386`), and latest ignored ledger `.superpowers/sdd/2026-10-14-cp5-3-research-quality-plan/progress.md:3–9`. Root AGENTS.md is empty. Review used actual files, not a claimed tracked diff or prior verdict alone.

Only this requested report written. No network, live access, acquisition, cp5_run or supervisor import/execution, process control, test-suite execution, subagents or commits. No production/test edits or ledger mutation. Earlier findings not reopened; only new breakage introduced by these repairs considered.

## Individual findings

| Remaining finding | Verdict | Assessment |
|---|---|---|
| M1 residual: terminal-append interruption disappears | **ADDRESSED** | `scripts/cp5_supervise_durable.py:268–279` snapshots caught-signal count before terminal append, writes exactly one process terminal, restores prior handlers in `finally`, then durably appends `LATE_SIGNAL` for signals caught during commit through restoration. Inspector at45–49,67–69 accepts only post-terminal, same-execution, correctly shaped late observations and reports authoritative `SUPERVISOR_INTERRUPTED`, preserving original terminal facts without a duplicate terminal. Test260–279 injects the installed SIGINT callback specifically at COMPLETED terminal fsync and asserts delivery, inspector interruption, exactly one terminal and retained LATE_SIGNAL. |
| M2 new: decoder recursion interrupts full stream retention | **ADDRESSED** | Classifier179–186 catches `RecursionError` locally, clears prior semantic authority and records `UNAVAILABLE_DECODER_RECURSION`; it does not escape `consume` into supervisor-wide kill/abort. Persistence/hash/journal flow118–131 and normal drain230–243 continue. Test281–294 emits a40002-byte nested JSON line, then distinct delayed stdout/stderr markers; asserts exact binary files, COMPLETED/exit0, unavailable semantic provenance and independently recomputed SHA256 for both streams. Terminal process_only is explicit at274. |

## Spec and quality checks

- **Terminal commit semantics:** LATE_SIGNAL is an append-only observation, not a second member of TERMINAL17. Inspector retains the single terminal row and overlays the authoritative interruption outcome. Subsequent ordinary records or another terminal still fail ordering45–46. Late observation requires terminal, phase `terminal_commit` and a nonempty integer signal list47–49; UUID and finite timestamps remain checked before acceptance.
- **Cleanup:** previous signal handlers are restored in the terminal-write `finally`275–277, including when terminal persistence raises. No second child cleanup/terminal emission is introduced by the late observation path. Existing output closing and bounded direct-child cleanup remain unchanged. If storage fails or the worker is uncatchably terminated before acknowledgment, durable interruption cannot be guaranteed; this is explicitly disclosed, not silently claimed solved.
- **Signal boundary:** caught callbacks through restoration are counted before LATE_SIGNAL emission. After restoration, signals use prior handler policy; no claim of atomic signal-plus-storage transaction or protection from SIGKILL/TerminateProcess/power loss. Test injects the installed callback, not a real Windows console signal. Test does not independently exercise every restoration interleaving; source inspection supplies that bounded-path assessment.
- **Decoder boundary:** raw stdout is persisted and hashed before optional classification125–127; recursion failure now returns normally so chunk acknowledgment and future queue consumption continue. Unavailable provenance cannot reuse earlier `opening_blocked`; only a later accepted bounded top-level result can supersede it under the existing contract187–190. Nonzero child exit still takes precedence244–247. No new allocation, dependency, retry or execution path introduced.
- **Retention limits:** exact retained bytes/hashes in this regression support successful ordinary collection, not universal retention under watchdog, interruption, storage/read failure or worker loss. Inspector still does not authenticate journal contents or recompute stream-file hashes. No unrelated inspector hardening requirement added.

**New repair-induced High/Medium breakage found: none.** No repeated earlier finding or expanded whole-project acceptance implied.

## Retained verification evidence — not rerun

Checkpoint344–358 and latest ledger7 agree on final results after all Python edits:

| Evidence | Retained result | Review qualification |
|---|---|---|
| Targeted durable-supervisor suite | **31 PASS**,18.726s,0 failures/errors | Producer execution evidence; regression bodies independently read. |
| Full suite | **795 PASS**,624.842s,0 failures/errors | Producer evidence; full command recorded at checkpoint350. |
| Focused suite | **548 PASS**,597.638s | Same23-module selection; serial after full with no intervening Python edits, per checkpoint354. |
| Scoped Pyright | **0 errors /0 warnings /0 informations**,2 files | Retained JSON independently read; not whole-project clean typecheck. |
| Baseline comparison | **baseline0 / added0**,removed0,equaltrue | Baseline `6e1f582`; both new paths absent, cat-file128 each. No absent-file line mapping required. |
| Lint / whitespace | Lint N/A; reported checks clean except CRLF notices | Existing producer evidence; not freshly rerun. |

Read `C:\Users\ANDROS~1\AppData\Local\Temp\opencode\cp56a-r2-_ob86rut\current-pyright.json`: Pyright1.1.414, filesAnalyzed2, generalDiagnostics[], counts0/0/0. Read adjacent `comparison.json`: baseline6e1f582, both specified paths absent128, baseline_diagnostics[], added[], removed[], equaltrue. This corroborates the scoped **baseline0 / added0** claim, not diagnostics for unrelated files.

Raw target/full/focused execution transcripts were not independently read, suites/typecheck not rerun, and current source identity to retained runs not independently hashed. Counts are attributed evidence, not proof by themselves. Corrected depth20000 pre-fix retention RED is producer-reported checkpoint320–322 evidence; initial insufficient depth2000 and fixture/path/typing corrections342 are not misrepresented as additional product failures.

## Disposition

Remaining M1 **ADDRESSED**; remaining M2 **ADDRESSED**. Scoped spec/quality **H0/M0**; no new High/Medium repair regression found. Earlier recovery uncertainty unchanged: **PARTIAL**, original child/network/outcome **UNKNOWN**, tool-exit4 mechanism unresolved. No acquisition retry, boundary, pilot, live execution, automatic cleanup or gate bypass authorized. CP5 live remains **BLOCKED**.
