# CP5.6A scoped independent rereview — 2026-10-16

## Verdict

**SPEC/QUALITY: CHANGES REQUIRED — H0 / M2. Offline READY withheld.** H0/M0 is required before offline READY; neither offline readiness nor this review authorizes execution. Historical recovery remains **PARTIAL**; original child launch, network activity and outcome remain **UNKNOWN**. Underlying tool-exit4 mechanism remains unresolved. CP5 live gate remains BLOCKED.

## Scope and method

Fresh independent static rereview of the five findings in `docs/superpowers/cp5.6a-independent-review-2026-10-16.md`, complete current `scripts/cp5_supervise_durable.py` (295 lines), complete current `tests/test_cp5_supervise_durable.py` (333 lines), checkpoint fix-round1 A–S (232–308), and ledger entries 3–17 and 537–545. Root AGENTS.md is empty. Actual new-file contents, not merely tracked diff, were reviewed. Read-only Git status reported 32 untracked files with compressed listing and no tracked diff stat; this does not establish a complete inventory or source-snapshot equivalence. Original pre-fix source was available through the previous review's exact excerpts/references, not an independently recovered pre-fix file snapshot.

Only this requested report written. No network, live access, acquisition launch, cp5_run execution/import, supervisor execution/import, process control, subagents or commits. No test, full-suite, focused-suite, lint or typecheck rerun. This is static acceptance review with explicitly attributed retained execution evidence, not a fresh GREEN claim.

## Original findings: individual verdicts

| Finding | Verdict | Source-backed assessment |
|---|---|---|
| H1 | **ADDRESSED** | `scripts/cp5_supervise_durable.py:99–125,148–181,219–232,258–262`: fixed 4096-byte reads, queue256, parser buffer capped65536, incremental hashes; no final whole-file scan or unbounded stdout line read. Oversize clears semantic authority without truncating binary evidence in the ordinary collection path. Tests142–145,193–204 cover oversized JSON/raw output and a parser-size assertion. New parser exception regression below remains separate from the original unbounded-line defect. |
| M1 | **NOT ADDRESSED** | Drain interrupt checks221/228 and protected flush cleanup244–256 fix the demonstrated earlier windows. But final check251 precedes terminal write/fsync258–262; a caught signal during that append is only added to `interrupted` and is never examined before handlers restore264–265. Residual counted once below. |
| M2 | **ADDRESSED** | Last bounded top-level dictionary containing dictionary `result` controls177–179; nonzero exit precedence233–236. Tests174–177,225–237 cover ordering, malformed/unrelated/wrapped records, nonzero exit and oversize uncertainty. Contract explicitly ignores malformed/unrelated bounded lines; no CP5 PASS inferred. |
| M3 | **ADDRESSED** | Tests260–274 kill a real long-lived disposable controller after STARTED and assert later stream data plus watchdog/child_stopped. Tests276–316 exercise a real Windows kill-on-close job with permitted breakaway and output after job close. Denied spawn is a scoped PermissionError injection318–325, not a real denied-job experiment. Spawn acknowledgment versus PREPARED rejection252–258 is explicit. Retained report says both Windows acceptance cases ran, not skipped. No universal host survival guarantee. |
| M4 | **ADDRESSED** | Inspector20–65 validates dictionary/event, UUID/timestamps, initial PREPARED, identity/order, STARTED and minimum terminal observation/exit consistency; invalid records yield UNKNOWN/partial without mutation or execution. Test179–191 is weak because its invalid first PREPARED masks every tail subcase; complementary valid-generated/foreign-terminal/missing-returncode test239–250 and direct branch inspection supply coverage, not a claim that every invalid-tail branch was exercised. |

## Remaining/new breakage only

### M1 residual — caught interruption during terminal append still disappears

**Source:** `scripts/cp5_supervise_durable.py:141–146,251–265`.

After the last `check_interrupt()` at251, `record(state, ...)` serializes the already chosen COMPLETED/BLOCKED/etc. state and performs write/flush/fsync94–97. If the installed SIGINT/SIGTERM handler runs during this operation, it merely appends to `interrupted`. The append completes and previous handlers are restored with no further check or SIGNAL record. A caught interruption can still be silently followed by COMPLETED, exactly the original M1 acceptance concern. Existing callback test206–223 targets fifth fsync (output finalization), not terminal append; it does not close this window.

**Required bounded acceptance:** inject the installed handler specifically during terminal append fsync, using a disposable toy execution. Verify durable interruption evidence and an authoritative interruption outcome consistent with inspector terminal ordering; do not silently append a second terminal that inspector rejects. Define terminal commit/late-signal behavior explicitly. No requirement for uncatchable worker death, atomic signal-plus-storage transactions, or guaranteed writes under failed storage.

### M2 new — bounded JSON record can abort stream retention through uncaught decoder recursion

**Source:** `scripts/cp5_supervise_durable.py:119–125,157–179,237–256`; tests193–204 assert size only.

The new incremental parser catches ValueError/UnicodeError only174. Python's JSON decoder can raise **RecursionError** on deeply nested JSON well below65536 bytes. A toy stdout line consisting of thousands of nested arrays around a scalar is size-bounded yet reaches this path. Because classification runs inside `consume`, the exception escapes to the supervisor-wide failure handler237: state becomes FAILED, live child is killed, and pending reader/queue data is not drained. Even an already-exited child can have further output discarded. This changes otherwise successful child behavior and defeats durable full-output retention because optional semantic classification failed, not because storage/read/watchdog failed. This is introduced by moving parsing into collection; the original final scan could fail terminal emission but did not terminate ongoing collection at this point.

**Required bounded acceptance:** disposable toy emits a deeply nested, sub-cap JSON line followed by a distinct trailing stdout/stderr marker. Assert exact retained binary output/hash, observed child exit0 and a durable process-only terminal with honest unavailable/invalid semantic provenance. Contain decoder recursion failures at the classifier boundary; do not reinterpret them as authoritative BLOCKED or kill the child solely for this optional parsing failure. No such test executed in this review; this is a source-backed deterministic failure path, not reported RED output.

## Cross-cutting validation and limits

- **Bounded processing:** per-read/per-parser allocations bounded by4096/65536 plus bounded queue; total persisted evidence intentionally grows. Chunk parsing and hashing occur during collection, not an output-size-proportional post-watchdog pass. No separate hard finalization wall-time guarantee: fsync/OS scheduling remain external limits. Decoder-recursion breakage above still needs repair.
- **Durable full output and hashes:** ordinary chunks are written/flushed/fsynced before hash updates and journal chunk acknowledgment115–125. Separate SHA256 objects preserve stdout/stderr distinction; digest extraction is bounded. On exceptional exits hashes cover consumed/persisted chunks, not an assertion of all bytes child attempted to emit. Tests currently check size rather than independently recomputing exact-byte hashes. No independent hash-equality execution claim made here.
- **Strict identity/offline partial inspection:** UUID matching, finite timestamps, ordering and returncode/child_stopped checks are present. Inspection opens journal read-only and never probes PIDs, launches, repairs or retries. It validates journal structure/observations, **not cryptographic authenticity or current stream-file hash equality**: `evidence_hashes` is reported metadata, not verified by inspect. It does not establish original historical provenance. Whole-journal read/result memory is not bounded by the child stdout classifier cap; original H1 concerned collection/final parsing, not a new bounded offline-inspector requirement.
- **CP5 semantics fail closed:** oversized stdout invalidates earlier semantic authority until a later accepted bounded result. Nonzero exit remains FAILED. COMPLETED explicitly process_only262; NO_RESULT/UNAVAILABLE_OVERSIZE must never authorize CP5 PASS. BLOCKED remains wrapper semantic classification, not gate acceptance. Existing acquisition/gate code not modified by these new files; arbitrary explicit argv is not sandboxed.
- **Cleanup/survival:** stop_child130–139 targets only its Popen direct child, checks liveness, waits at most15 seconds and records wait expiry. Controller-death/job-close acceptance is now materially stronger. Worker death, power loss, denied breakaway, spawn-to-STARTED uncertainty and descendants remain disclosed limits. launch0 is spawn acknowledgment, not durable PREPARED or execution success.
- **No automatic execution:** inspect has no execution path. launch/run require explicit invocation/argv. No retry/fallback loop or automatic guard removal found. Retained guard provides at-most-one cooperating attempt per resolved DB path, not guaranteed exactly-once execution or exclusion of noncooperating writers/path aliases.

## Retained evidence audit — no rerun

Checkpoint270–284 and ledger7 agree on **target29 PASS15.514s**, **full793 PASS629.240s**, **focused546 PASS624.393s**, zero reported failures/errors, after Python edits. Earlier23/29 development runs and prior780/533 results remain historical. These are producer-reported retained results; raw full/focused/target command transcripts were not independently read here, and present source identity to those runs was not independently hashed. Counts do not negate either source-backed finding.

Read retained `C:\Users\ANDROS~1\AppData\Local\Temp\opencode\cp56a-r1-lzkb7nw7\current-pyright.json`: Pyright1.1.414, filesAnalyzed2, generalDiagnostics[], errors/warnings/informations0. Read adjacent comparison.json: baseline6e1f582, both new paths absent (cat-file128), baseline_diagnostics[], added[], removed[], equaltrue. Thus retained **baseline0 / added0** is scoped to these two paths, not whole-project zero diagnostics; absent baseline files need no line mapping. No fresh typecheck or current-source snapshot equivalence asserted. Lint N/A per retained report; documentation-only rereview changes no Python diagnostics.

## Disposition

Original findings: H1 ADDRESSED; M1 NOT ADDRESSED; M2 ADDRESSED; M3 ADDRESSED; M4 ADDRESSED. Remaining **H0/M2**, comprising one residual M1 window and one newly introduced parser/retention regression. Repair only these bounded issues, add targeted disposable checks, then obtain independent H0/M0 before offline READY. Do not rerun acquisition or infer authorization from toy tests or this report. Root recovery PARTIAL, original child/network/outcome UNKNOWN, and live BLOCKED remain unchanged.
