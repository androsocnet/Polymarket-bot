# CP5.6A independent review — 2026-10-16

## Verdict

**SPEC/QUALITY: CHANGES REQUIRED — H1 / M4.** Historical recovery remains **PARTIAL**; original child launch, network activity, watchdog enforcement and child outcome remain **UNKNOWN**. CP5 live gate remains BLOCKED. No retry, new boundary, acquisition, pilot or live execution authorized by this review.

## Scope and evidence discipline

Fresh static review of complete actual `scripts/cp5_supervise_durable.py` (209 lines), complete `tests/test_cp5_supervise_durable.py` (135 lines), recovery checkpoint including appended implementation A–S (230 lines), current ledger entries at `.superpowers/sdd/2026-10-14-cp5-3-research-quality-plan/progress.md:3-9,529-537`, and original supervisor/runner lifecycle and verdict context at `scripts/cp5_run.py:255-283,300-441`. Root AGENTS.md empty. Only this requested report written. No production/test changes, tests, imports/execution of either supervisor or runner, network, acquisition, DB access, process control, subagents, staging or commits.

Read-only Git status reported 31 pre-existing untracked files, with output compressed after ten names. Combined Git invocation did not expose reliable individual diff/HEAD output; no independently verified fresh HEAD, complete status inventory or clean-tree claim made from that output. Actual new files read directly rather than relying on tracked diff coverage of untracked files.

Findings below are static, source-backed defects and acceptance gaps. Proposed RED procedures are **not executed RED results**. They require only disposable offline fixtures/toy children during remediation, never CP5 acquisition.

## Historical recovery authority

Checkpoint lines52–76 and ledger533 distinguish recovered original tool record from child outcome correctly: exact command/preinvoke, completed tool record ending08:47:13.308Z, metadata exit4, and exact-command PowerShell start event are documentary recovered evidence. This review did not reopen original application storage or OS logs and does not independently reauthenticate their hashes/events.

Tool exit4 is **not** child returncode, semantic BLOCKED, watchdog expiry or evidence of no network. Later logged API connection error does not establish cause of earlier tool exit4. Proven root is supervision observability gap in PIPE/communicate plus response-only retention; underlying exit4 mechanism remains unresolved. Missing original DB/streams do not prove no child ever ran. No authoritative original child terminal outcome recovered here.

## Findings

### H1 — Final stdout classification reintroduces unbounded memory and unbounded post-watchdog work

**Source:** `scripts/cp5_supervise_durable.py:163-177`, especially170–172. Checkpoint170 describes bounded stream chunks; checkpoint178 describes watchdog/collection ceilings.

Incremental collection itself uses bounded chunks and bounded queue. After child exit and stream drain, however, `for line in stream` materializes an entire stdout line with no limit, then `json.loads(line)` allocates parsed data. A successful toy child can emit arbitrarily large newline-free stdout; supervisor then allocates that whole line, potentially duplicating it for JSON parsing. This work occurs after watchdog enforcement has stopped and outside exception/cleanup handling. MemoryError or external termination can leave no terminal record despite successful child exit. Hashing is streaming but also outside watchdog; total finalization work is not bounded by the stream drain deadline. This is a code-controlled input-size issue, distinct from disclosed unavoidable fsync/OS scheduling delays.

**Reproducible RED recommendation:** disposable toy child emits many fixed-size chunks without newline, then exits0. Under a constrained supervisor memory budget, assert bounded peak memory and terminal emission; current line iteration necessarily scales with total line length and can fail before terminal. Add a small deterministic fixture/stream double that rejects unbounded line reads so regression does not need huge allocations. Include an oversized JSON result line. Define maximum classification record size; never truncate/reinterpret a partial record as authoritative BLOCKED. Preserve full binary evidence, report classification unavailable/oversize honestly, and keep COMPLETED explicitly process-only where appropriate. Document finalization time limits separately from child watchdog.

### M1 — Catchable interruption is ignored during drain and finalization

**Source:** `scripts/cp5_supervise_durable.py:103-104,127-149,156-177`; test82–85.

Signal handler only appends to `interrupted`; only loop127 checks it. Once direct child exits, drain loop never checks signals or interrupt file. A SIGINT/SIGTERM caught during draining can therefore be silently followed by COMPLETED. Previous handlers are restored before hashing/classification/terminal append, creating another catchable-interruption window where final evidence is lost rather than best-effort SUPERVISOR_INTERRUPTED. Existing interrupt-file test triggers only while child remains alive and proves neither actual signal delivery nor these phases.

**Reproducible RED recommendation:** toy child exits after starting a bounded descendant that briefly retains stdout; wait until supervisor enters drain, deliver a catchable signal on supported platform, and assert durable SIGNAL plus SUPERVISOR_INTERRUPTED, with actual direct-child returncode retained. Use scoped deterministic file/hash doubles to inject KeyboardInterrupt during finalization and assert best-effort terminal behavior. On Windows use supported console-control setup or injected handler invocation; do not label `os.kill(SIGTERM)` catchable. Do not demand uncatchable TerminateProcess survival or whole-tree kill.

### M2 — BLOCKED classification trusts any earlier result rather than final authoritative result

**Source:** `scripts/cp5_supervise_durable.py:168-176`; test68–70. Compare existing runner `scripts/cp5_run.py:431-439`, which retains last result-bearing dictionary.

Any stdout JSON dictionary with `result.reason == opening_blocked` changes terminal state to BLOCKED, even when later result evidence supersedes it. Child exit0 with an early blocked diagnostic and final successful result is labeled BLOCKED. A successful direct child that prints nested/wrapped supervisor evidence also has no specified classification contract. This is not CP5 gate acceptance; it is an inaccurate wrapper semantic outcome.

**Reproducible RED recommendation:** toy child prints `{"result":{"reason":"opening_blocked"}}`, then `{"result":{"state":"STOPPED","exit_code":0,"reason":"done"}}`, exits0. Assert final result controls classification; current code returns BLOCKED. Specify accepted top-level result shape and ordering, retain classifier provenance, and keep nonzero exit precedence. Add reverse-order and malformed/unrelated JSON controls. Do not infer CP5 PASS from process exit0.

### M3 — Consumer-loss test does not validate abrupt controller death or asserted breakaway failure behavior

**Source:** `tests/test_cp5_supervise_durable.py:120-127`; `scripts/cp5_supervise_durable.py:199-204`; checkpoint182,202,218.

Test waits for short-lived launcher to exit normally through `communicate`; it meaningfully proves worker persistence after normal launcher exit and retained output/watchdog. It does not kill a long-lived controlling parent, disconnect a consumer mid-run, or exercise Windows job close/termination. Checkpoint202 correctly limits this claim, but controller-disconnect survival remains incompletely verified. Windows flags are valid subprocess constants and combine detached process/new process group/breakaway; breakaway denial can raise synchronously. Once worker spawn succeeds, later worker reservation/PREPARED failure is invisible to launcher because stdio is DEVNULL and launcher returns0 without handshake. Launch return0 is therefore spawn acknowledgment only, not durable execution acceptance, as checkpoint182 admits.

**Reproducible RED/acceptance recommendation:** disposable long-lived controller calls launch then stays alive; wait for worker PREPARED/STARTED, abruptly terminate only that controller, verify subsequent stream persistence and observed watchdog terminal without touching real processes. Add supported Windows disposable Job Object case with kill-on-job-close and permitted breakaway, plus denied-breakaway Popen failure control proving no fallback. Separately test successful launcher spawn followed by worker reservation rejection; ensure caller/operator contract never treats launch0 as PREPARED. These are missing acceptance checks, not claims that current flags demonstrably fail. Worker death/power loss and universal host survival remain explicitly out of scope.

### M4 — Offline inspection crashes on valid JSON records with invalid journal shape

**Source:** `scripts/cp5_supervise_durable.py:23-32`; test112–118.

Inspector catches JSON syntax/encoding errors but accepts arbitrary decoded JSON values into records and calls `.get` on final value. Journal ending with valid JSON `null`, `[]`, or `1` raises AttributeError instead of returning honest UNKNOWN/partial evidence. A terminal-only dictionary is also accepted without validating execution identity or terminal fields; this is weaker than the checkpoint claim of a valid observed terminal. No hostile-file security boundary is assumed: offline reconciliation should distinguish syntactically valid corruption/foreign content from authoritative execution evidence.

**Reproducible RED recommendation:** inspect disposable journals containing PREPARED followed by `null\n`, `[]\n`, or `1\n`; assert no mutation, no traceback, outcome UNKNOWN and explicit invalid-record classification. Add mismatched execution UUID and terminal lacking returncode/child_stopped controls; define minimum valid journal/terminal contract and reject unsupported shapes without guessing process state. Preserve bytes and do not repair/retry.

## Twelve-area assessment

| Area | Assessment |
|---|---|
| 1. Original authority/outcome/root | Recovery distinction sound; exit4 remains tool-only, underlying cause unknown. |
| 2. Durable exclusive PREPARED | Exclusive journal creation; flush/fsync before child spawn; UUID, argv, DB, boundary, UTC/monotonic, watchdog, parent/supervisor PIDs, planned role retained106–109. Actual child PID correctly only STARTED120. No directory/power-loss durability promise. |
| 3. Exact-once/existing evidence | Retained exclusive per-DB guard47–50; pre/post reservation DB/sidecar/evidence checks43/51; binary evidence exclusive116. Provides at-most-one cooperating attempt per resolved DB path, not exactly-one guaranteed execution. Other noncooperating writers/path aliases and removal of locks are not prevented. |
| 4. No automatic retry | No respawn loop or fallback. Failed reservation/launch remains consumed; intentional retained guard, not recoverable retry authority. |
| 5. Detached controller survival | DEVNULL/close_fds and POSIX new session/Windows flags plausible; normal parent exit covered, abrupt parent/job loss gap M3. Worker itself still owns watchdog. |
| 6. Incremental durable streams | Concurrent os.read4096, queue256, separate exact binary files plus base64 journal; fsync before chunk acknowledgment. Reader queue blocking is bounded backpressure; no main-thread join or unbounded wait on reader. Final parser violates memory bound, H1. |
| 7. PID/watchdog/signals/terminal | STARTED PID and action records present. Direct-child kill only, wait(timeout15), drain ceiling15. Kill request distinguished from returncode/child_stopped; WATCHDOG_KILLED label alone must not be read as confirmed termination when child_stopped=false. No whole-tree guarantee. |
| 8. Catchable cleanup | Running-child path best effort, but drain/finalization gap M1. IO failure may prevent terminal append; no guaranteed write under failed storage. |
| 9. Crash/partial inspection | Torn JSON tail returns UNKNOWN without mutation; no process probe/PID reuse guesses. Shape/provenance validation gap M4. Spawn-to-STARTED ambiguity and uncatchable worker death honestly disclosed. |
| 10. Existing bounds/safe kill | Existing cp5_run remains separate and unchanged by these new files. process.kill targets live Popen object after poll, not a recovered numeric PID. No speculative kill from inspection. No unconditional process.wait()/communicate() without timeout found. H1 concerns postprocessing, not hidden wait. |
| 11. Gates/network/acquisition | New wrapper imports only stdlib and executes explicitly supplied argv. No embedded acquisition import, retry or hidden network path found. Arbitrary supplied command can network; wrapper not sandbox. Existing phase_verdict and live gates not replaced by COMPLETED. |
| 12. Tests/verification quality | 15 original numbered cases plus added BLOCKED case =16 test methods, not15 total. Useful smoke/regression coverage, but no blanket proof of fsync ordering, actual signals, abrupt parent death, crash reconciliation or all IO failure paths. Findings identify bounded missing checks rather than unlimited matrix. |

## Actual test audit

- 01 checks PREPARED visible to child and required keys; not fsync ordering/durability fault injection, UUID validity or exact field values.
- 02 checks positive recorded PID, not equality to toy child's actual PID.
- 03 meaningfully observes stdout before child exit. 04 checks retained stderr after exit, not incremental timing while child lives.
- 05/06 meaningfully distinguish exit0/nonzero and retain7. Additional BLOCKED case covers single happy-path marker only; M2 missing.
- 07 checks watchdog label and KILL_REQUESTED, but not child_stopped/actual child termination or elapsed ceiling.
- 08 checks interrupt-file path and returncode, not actual signal behavior; M1 missing.
- 09/10 preserve existing journal/DB and reject, but do not enumerate sidecars/streams or a true simultaneous reservation race.
- 11 rejects second same-DB attempt after first STARTED, useful cross-process retained guard check; not simultaneous race or path-alias coverage.
- 12 verifies child marker written once on failure; useful no-retry regression.
- 13 covers torn syntax and preservation, not actual worker death or shape-invalid JSON; M4 missing.
- 14 proves normal launcher exit with durable output and watchdog, not abrupt controller death/job loss; M3 missing.
- 15 installs socket denial inside toy child and checks that child's modules. It does not instrument supervisor network calls, cannot prove arbitrary command sandboxing, and does not install suite socket guard in subprocesses. Static stdlib-only wrapper inspection supplies complementary evidence; checkpoint222 correctly disclaims sandboxing.

## Retained verification, not fresh rerun

Accepted producer evidence: checkpoint206/full **780 PASS621.472s**, checkpoint210/focused **533 PASS624.639s**, checkpoint202/wrapper **16 PASS5.848s**, ledger7 matching final counts. Earlier779/532 and original15-case GREEN are historical, not final evidence. No full or focused rerun performed, per review constraint. Counts establish exercised cases, not absence of findings.

Read retained `C:\Users\ANDROS~1\AppData\Local\Temp\opencode\cp56a-pyright-bsgqln0a\comparison.json`: baseline6e1f582, both new paths absent with cat-file128, added=[], removed=[], equal=true. Read adjacent current-pyright.json: Pyright1.1.414, filesAnalyzed2, errors/warnings/informations0. Checkpoint214 distinguishes this initial retained comparison from producer-reported final post-BLOCKED zero-diagnostic rerun; no fresh mapper/typecheck or independent source-snapshot equivalence asserted. Zero-added claim is scoped to these two files, not whole-project clean typecheck. Lint N/A per ledger93. Documentation-only review adds no Python type delta.

## Disposition

Fix H1 and M1/M2/M4; close M3 with explicit offline acceptance evidence and precise supported-platform limits. Rerun focused disposable regressions after repair; preserve original historical records and at-most-one reservation policy. Independent acceptance remains pending. Historical CP5.6 actual execution outcome stays UNKNOWN; this report does not manufacture failure, success, no-network evidence or root cause for tool exit4.
