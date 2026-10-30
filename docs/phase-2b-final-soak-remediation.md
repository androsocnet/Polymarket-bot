# Phase 2B final soak remediation

**Status: BLOCKED. Not Phase 2B acceptance, deployment authorization, or soak authorization.**
Historical trial: `soak-20261026T040455Z`. Approved historical base: `f8ccedc5fe8d0b7465cb65aa87ee0e15b0737f4a`. User reports six of eight gates passed; gates 3 and 8 blocked. Exact VPS runtime files are not bundled locally. No direct VPS access performed.

**Review status:** Revised timing review identified numeric runtime formatting, late-deadline acceptance and helper-timeout acceptance defects. Focused regressions cover corrections; final independent verdict recorded in publication report. Prior diagnostic wall-time correction remains intact. Local tests and code review do not grant deployment, pilot, soak, or Phase 2B acceptance.

## Root causes reproduced in repository

- Deadline command stops only main infrastructure service. Shell semicolon lets final `systemctl show` hide stop failure. No helper/timer/cgroup cleanup proof.
- Heartbeat moves temporary JSON then reads removed temporary path under `set -e`; real local regression exits after first sample. JSONL absent.
- Native runner passes `--remain-after-exit=no`, unsupported by local systemd CLI; correct native property is `--property=RemainAfterExit=no`.
- Runner precreates output directory; legacy paper observer requires nonexistent output. Legacy observer reads paper environment/database and Windows heartbeat schema. Native infrastructure cannot safely reuse it unchanged.
- Historical `code_hash` excludes shell helpers, hashes subset of Python files, and reads paper config. Installer copied mutable working bytes and editable-installed source checkout. Git SHA alone did not describe installed runtime.
- Cumulative DB totals mix run IDs. No consistent trial snapshots means historical delta cannot be reconstructed confidently.
- Trial suffix identifies launch metadata time, not service observation start. Manual timezone arithmetic is ambiguous. New metadata records UTC `Z`, WIB `+07:00`, launch versus observation start separately. Example: `2026-10-26T04:04:55Z` is `2026-10-26T11:04:55+07:00`, not evidence that service started at that instant.
- Stale ingestion failures now write `stale_ingestion_diagnostics` to `result.json` capturing exact count, last_received_us, wall_time, computed_age_seconds, run_id, and failed_branch. Failure itself is NOT suppressed — strict thresholds preserved. See Fix 3 in `deploy/infrastructure-trial.py`.

## Hermes evidence request — read-only, no code modifications

Hermes must export existing facts before any redeployment. Do not repair files, reset units, run installers, regenerate approvals, vacuum/reset DB, or delete evidence. Write exports only into a new evidence-export directory; never overwrite historical trial files. No secrets/private environment values in public output.

1. Export complete original trial directory, launch metadata, results, samples, full host JSONL, deadline logs, journals and original pretrial approval. Include SHA-256 inventory, byte sizes, UTC capture time, hostname/boot ID, and exact original paths.
2. Export raw installed `infrastructure-runner.sh`, `infrastructure-host-heartbeat.sh`, observer file actually executed, installer/unit fragment and every drop-in. Include original SHA-256 and `stat` ownership/mode/timestamps. Export exact command lines and any saved pre-edit copies/diffs. Preserve bytes, not pasted reconstructions.
3. Export `git rev-parse HEAD`, branch, `git status --porcelain=v1`, tracked diff, `git show f8ccedc5fe8d0b7465cb65aa87ee0e15b0737f4a:<path>` comparison for each installed runtime. List every VPS-local edit with when/who/why, pre/post hashes if available. Unknown pre-edit hashes remain unknown.
4. For exact unit names from original launch evidence, export `systemctl cat`, `systemctl show` with `LoadState,ActiveState,SubState,MainPID,ControlPID,Result,ExecMainStatus,InvocationID,NRestarts,ControlGroup,FragmentPath,DropInPaths,NextElapseUSecMonotonic`; export corresponding cgroup `cgroup.procs` recursively, `systemctl list-jobs`, `list-timers --all`, and scoped journal with `--utc -o short-iso-precise`. No wildcard stops.
5. Back up current DB through SQLite online backup API with read-only source; label **post-hoc**, not historical after snapshot. Export integrity result, runs `(run_id,bot_id,started_us)`, counts grouped by run ID and min/max `received_us`, all trading-table counts. Do not copy live WAL database file alone. Explicitly report missing historical before/after snapshots.
6. Export raw epoch timestamps; derive ISO UTC and WIB via timezone conversion, never change timezone labels on manually shifted UTC values. Reconcile launch ID, actual run start, observer baseline, deadline and stop timestamps.

No Hermes transport is exposed in this session. These exports remain prerequisite to exact historical VPS diagnosis. Old trial is never retroactively approved.

## Changes

`infrastructure-trial.py` is native-only controller, separate from unchanged legacy paper observer. Approval checks precede lock, evidence creation, STOP reset or scheduling. Fingerprint binds full release manifest, environment bytes, complete effective unit text and relevant effective properties. No duration normalization; pilot and soak require separate explicit approval. Only systemctl-show ExecStart runtime bookkeeping (`start_time`, `stop_time`, PID, exit code/status) is removed from effective-property fingerprint; complete unit text and command path/argv remain bound. PID/InvocationID/NRestarts checked independently during window. Controller checks ingest command, runtime bound via numeric D-Bus property, resource policy, service identity, heartbeat continuity, ingestion freshness, and trading-table digests.

Installer creates `/opt/polymarket-btc-infrastructure/releases/<GIT_SHA>` from committed Git objects only; existing paper files/venv remain untouched. Frozen `uv.lock` dependencies use hash-verified installation; runtime manifest includes installed dependencies, copied interpreter and external base-Python stdlib/native-extension file hashes. Host OS, shared system libraries, systemd and shell tools remain separately trusted OS baseline; this is not whole-OS attestation. Freeze/review host packages before approval; no host upgrades during trial. No automatic start/enable. Existing config is preserved; previous unit fragment saved before replacement. Existing release refuses overwrite.

Root-owned `/var/lib/polymarket-btc-infrastructure` separates root-only evidence/lock from service-owned `data` child; no paper parent ownership/permissions change. Approval reads require root-owned, non-group/world-writable regular file and every ancestor, with symlink-free descriptor traversal. Release-wide extra files are rejected, including root-level Python packages.

Independent deadline checks observed main shutdown, stops observer/deadline timer and startup protection; heartbeat remains independent through its monotonic expiry, and preserves errors individually. Durable `deadline-stop.json` precedes DB processing. Snapshot subprocess has 60-second whole-operation timeout; deadline/startup helpers have finite 360-second runtimes. Launch-time audit remains armed even when startup fails, at launch monotonic + startup ceiling 360 + observation seconds + deadline grace 60 + cleanup allowance 420. Audit has finite 600-second runtime for eight sequential unit stops/queries (each up to 65 seconds) plus fingerprint/checks; runtime exhaustion or missing evidence blocks review. These are ceilings, not scheduler guarantees. Audit excludes itself, checks deadline/startup helpers after exit; post-audit inspection includes audit itself. Deadline and audit must have observable `inactive`, `Result=success`, `ExecMainStatus=0` terminal evidence; any loaded helper failure/timeout blocks review. Heartbeat must self-exit with `ExecMainCode=1`, `ExecMainStatus=0`, `Result=success` before audit cleanup; SIGTERM or active pre-cleanup state blocks review. Full host JSONL must preserve finite clocks, sequence/boot/session continuity and coverage through T+2280 (pilot), with a terminal sample no more than 20 seconds later. Current main evidence must also remain inactive/successful with no processes, even when historical shutdown proof passed. Canceled/unstarted optional transient helpers may be absent, but missing required deadline/audit terminal evidence fails. Cgroup descendants checked, not only MainPID. Main shutdown acceptance requires `inactive`, `Result=success`, PID 0 and empty cgroup before runtime expiry. Failed stop never becomes success through later show.

Observer `started` flag set immediately after first successful `systemctl start`, before heartbeat or startup/restart verification, so `finally` stops trial-started main service on early startup and observation failures. Pre-existing active service is never stopped by observer. Missing ingestion timestamp records `computed_age_seconds=null`; exact failed branch and strict 30-second thresholds remain unchanged.

SQLite before/after/deadline snapshots use online backup API, bounded backup progress, integrity checks, exclusive creation and streaming hashes. Trial ingestion filtered by bot/run ID; samples retain per-run counts and timestamps. Trading rows compared by ordered content digests, including historical positions lacking run IDs.

## Hermes redeployment — only after verified commit and explicit deployment authorization

Do not use untracked helper scripts. Do not edit runtime code on VPS. Do not run these steps until review/test verdict is clear and final published SHA is provided.

1. Preserve/export historical evidence above. Confirm infrastructure inactive, all old owned trial jobs/timers stopped through separately authorized operations, no target cgroup PIDs. Existing paper service/config/database remain untouched. Inspect disk, ownership, Python >=3.12 and installed `uv`; no automatic package-manager operation.
2. In `/home/ubuntu/Polymarket-bot`, fetch `origin fix/phase2b-soak-remediation`; verify supplied full SHA equals `origin/fix/phase2b-soak-remediation`, inspect `git status`. Use separate clean checkout of exact SHA if local tracked edits exist; preserve them. Never reset/delete local edits. Verify no `feat/strategy-learning` merge.
3. Run source tests using repository dependencies and `PYTHONPATH=src`, Bash syntax checks. Execute `sudo env PATH="$PATH" bash deploy/install-infrastructure.sh` from clean exact checkout. Installer must not start service. If release path already exists, stop; inspect/verify it, do not delete or overwrite.
4. Set `R=/opt/polymarket-btc-infrastructure/releases/<VERIFIED_FULL_SHA>`. Review preserved `/etc/polymarket-btc/infrastructure.env`; keep `MODE=PAPER_ONLY` and risk values unchanged. New DB path must be `/var/lib/polymarket-btc-infrastructure/data/infrastructure.sqlite`, with service-owned parent for WAL/SHM. Set `MVP_STATE_DIR=/var/lib/polymarket-btc-infrastructure/data/state` too. Archive old config before authorizing only these storage-path changes; any old infrastructure DB/evidence stays in place. Installer checks service traversal of new data/state paths; it never grants access to paper-owned parent. Leave old DB/history in place; new trial initializes separate schema. Do not copy a live DB file or silently rewrite config. Review `systemctl cat polymarket-btc-infrastructure.service` and every drop-in. Main `ExecStart` must use `$R/.venv/bin/python -m polymarket_bot.linux_mvp ingest` and `PYTHONPATH=$R/src`.
5. Install phase-bound drop-in through explicit operator authorization, not local code edits: `[Service]`, `Restart=no`, `RuntimeMaxSec=1800+240=2040` for pilot, `RuntimeMaxSec=86640` for soak. Run `systemctl daemon-reload`; verify raw D-Bus RuntimeMaxUSec `2040000000` (pilot) or `86640000000` (soak), KillMode control-group, user, resource limits, no unexpected drop-ins. Config/unit changes occur **before** fingerprint approval. Runtime remains `seconds+240`; baseline deadline is `seconds+60`. Require `RuntimeRandomizedExtraUSec=0`, no restart and unchanged 30-second main stop policy. Admission checks measured activation-to-baseline lag; actual observed shutdown decides success, not nominal timer ordering.
6. Run `sudo env PYTHONDONTWRITEBYTECODE=1 "$R/.venv/bin/python" "$R/deploy/infrastructure-trial.py" fingerprint`. Export manifest, effective unit/config, full SHA and fingerprint for independent owner review. Fingerprinting is not approval.
7. Owner supplies root-owned approval file, outside release, with `scope=INFRASTRUCTURE_ONLY`, `phase=pilot`, `seconds=1800`, exact `git_sha` and `fingerprint`, `authorized=true`, real `approved_by`, real `approved_at_utc` ending `Z`. Do not generate approval by copying newly calculated hash without review. Never alter historical approval.

## Fresh 1800-second pilot

`PILOT_SECONDS=1800`, minimum samples 180. Revised observable contract supersedes old launch-relative scheduling and blocked baseline+1980 draft. Define launch monotonic `T`, final service activation `A` (numeric `ActiveEnterTimestampMonotonic`), verified observation baseline `B` after durable restart proof, `L=B-A`.

| Event | Pilot anchor / value |
|---|---|
| Startup watchdog | `T+360`; startup ceiling, not promised completion time |
| Continuous observation | `[B, B+1800]`, unchanged PID/InvocationID/NRestarts |
| Independent deadline | absolute `OnBootSec=B+1860` on `CLOCK_MONOTONIC` |
| Runtime backstop | `A+2040`, never disabled |
| Nominal deadline/runtime gap | `180-L`; require at least 60 seconds, hence `0<=L<=120` |
| Timer admission | at least 60 seconds remain until intended expiry before and after readback |
| Heartbeat lifetime | required absolute monotonic `T+2280` (`360+1800+120`), finite helper runtime 2340 from helper activation (60-second exit margin) |
| Observer runtime ceiling | 2280 seconds from its activation; deadline independently stops it |
| Independent final audit | absolute monotonic `T+2640` (`360+1800+60+420`), runtime600 |

Accuracy is 1 second with zero randomized delay, no calendar/realtime triggers, `WakeSystem=no`, exact effective expiry/target readback and unchanged boot. These checks provide useful nominal budget, **not guaranteed Linux dispatch latency**. Deadline helper entry must fall within `[B+1860, B+1890]` (at most 30 seconds observed lateness) and strictly precede runtime expiry; later entry fails even if main already stopped cleanly. This is a fail-closed acceptance ceiling, not promised dispatch latency. Runtime timeout, truncated window or unavailable shutdown proof fails. Watchdog cancels only after deadline verified and `timing.json` persisted; watchdog firing during handoff fails. Audit stays independent even if observation never starts. Heartbeat extends beyond latest admitted observation end; stale/missing heartbeat still fails.

`timing.json` records fingerprint, boot, activation, baseline, deadline, runtime expiry and effective timer; `result.json` records observation end; `shutdown-pre.json` exclusively persists active unit/InvocationID, boot and continuous run identity before controlled stop; `shutdown.json` exclusively records both pre/post snapshots, request time, post-stop quiescence, runtime fallback status, Result and original target cgroup evidence. A shutdown lock serializes observer/deadline finalization; retries reuse existing proof, never replace it. Post-stop InvocationID may be empty; missing/mismatched active pre-stop identity still fails; `deadline-stop.json` records helper-entry trigger time. Quiescence confirmation is conservative (after stop plus evidence queries), not guessed kernel exit time. Timer dispatch timestamp and helper entry are distinct. Source manifest/fingerprint binds new timing contract and helper ownership; effective readbacks are preserved with that fingerprint. Fresh exact-code/fingerprint owner approval required even for previous 1800-second approvals. Historical approvals/evidence remain untouched and cannot substitute missing timing evidence.

After deployment and exact fingerprint authorization:

```bash
R=/opt/polymarket-btc-infrastructure/releases/<VERIFIED_FULL_SHA>
sudo env PHASE=pilot APPROVAL=/etc/polymarket-btc/approved-native-pilot.json \
  bash "$R/deploy/infrastructure-runner.sh"
```

If infrastructure STOP exists, pause. Owner must explicitly authorize infrastructure-only reset; rerun command with `--reset-stop`. No automatic sentinel deletion. Main paper STOP untouched.

Runner prints single trial ID and evidence path. Separate bounded restart proof precedes continuous window. Do not disconnect-dependent tail, restart during observation, edit runtime, or stop safety timers. Continuous pilot is 1800 seconds; deadline at baseline +1860 seconds, audit at launch +2640 seconds. Wait for audit to finish before judging cleanup; inspect audit service itself afterward for inactive/failed with MainPID=ControlPID=0, empty cgroup, no jobs. `PENDING_REVIEW` is not acceptance.

After audit exits, run `sudo env PYTHONDONTWRITEBYTECODE=1 "$R/.venv/bin/python" "$R/deploy/infrastructure-trial.py" inspect-cleanup --folder /var/lib/polymarket-btc-infrastructure/evidence/<TRIAL_ID>` and preserve stdout as new external post-audit evidence. After reviewing successful exit, explicitly install that output as root-owned mode-0600 `<TRIAL_FOLDER>/post-audit.json` before freezing pilot bundle; no overwrite of historical files. Soak preflight requires this post-exit artifact, snapshots/summaries, samples, full host JSONL, launch/approval/restart/deadline/cleanup records and journal, with consistent trial/run metadata and snapshot hashes. This read-only check includes audit service, cgroups and pending jobs. Nonzero exit blocks gate 3. Export complete fresh trial folder and journals for all exact owned unit names, post-audit unit/cgroup evidence, runtime fingerprint and SHA inventory. Require before/after/deadline DB integrity; check trial-run ingestion and unchanged trading digests. Do not substitute lifetime totals. Resource/continuity gaps block review.

## Eight unchanged acceptance gates

1. Continuous 1800s pilot / 86400s soak, required samples (at least seconds/10), elapsed monotonic time, unchanged PID/InvocationID/NRestarts.
2. Separate restart proof with distinct InvocationID and verified clean stop before continuous window.
3. Independent deadline + post-exit cleanup audit, main/helpers/timers/cgroup PIDs and audit service terminal state. Require full-window timing proof, completed main quiescence strictly before `A+2040` (pilot), successful main Result, no runtime timeout, successful deadline cleanup and startup handoff; absent evidence fails. Test observer-failure path on separately authorized bounded pilot; do not infer deadline effectiveness only from already-stopped service.
4. Per-run positive ingestion, fresh sample progress across complete window, attributable run ID and timestamp range. Historical rows excluded.
5. Every quick_check and before/after/deadline full integrity; safe snapshot hashes and no missing baseline.
6. Full native host JSONL starts sequence 1, consecutive samples, one boot/session, no suspend/clock/gap flags. Guest continuity only, not hypervisor power evidence.
7. No new/changed orders, positions, settlements, decisions or equity; history retained.
8. Pretrial independent approval matches source-controlled deployed artifacts/config/effective unit and final fingerprint. No retrospective approval of modified historical runtime.

## Heartbeat timeout remediation (2026-10-30)

Failed pilot `pilot-20261030T092719Z` remains **FAIL**: reported 1800-second observation passed, but heartbeat covered only 2278.821 seconds of required launch-relative 2280 seconds; systemd recorded `Result=timeout`, signal 15. Independent audit and post-exit cleanup were BLOCKED. Historical evidence is not changed or retroactively accepted.

Heartbeat previously had the same numeric runtime ceiling as its required coverage duration. Its five-second loop must sample at/after monotonic expiry, append the terminal record and exit; a hard limit at that boundary can terminate it first. Only heartbeat `RuntimeMaxSec` gains a bounded 60-second margin for sampling, startup and terminal work. Required expiry stays `launch + 360 + seconds + 120`; main runtime, observer runtime, observation duration and all eight gates stay unchanged. This margin is not a scheduler guarantee: delayed sampling, incomplete coverage, timeout, SIGTERM, failed helper exits or late/missing independent deadline evidence still block acceptance.

| Configuration | Required coverage from launch | Heartbeat hard limit from helper activation | Main hard limit from main activation |
|---|---|---|---|
| Pilot (1800 seconds) | 2280 seconds | 2340 seconds | 2040 seconds |
| Soak (86400 seconds) | 86880 seconds | 86940 seconds | 86640 seconds |

Local command-boundary regressions cover both configurations; a controlled-clock execution of the real Bash loop covers five-second sampling across expiry and clean terminal exit. These checks are not VPS runtime acceptance or proof of independent deadline effectiveness under observer failure. That still requires a separately authorized bounded runtime test against an active main service, not inference from an already-stopped service. Follow Hermes redeployment steps above using the new published full SHA, then obtain fresh exact-SHA/fingerprint owner pilot approval. Previous approvals cannot authorize this release. No deployment or new trial is authorized by this change.

### Publication verification and known test limitations

Publication authorized with baseline platform errors and incomplete full-suite verification explicitly disclosed. Focused heartbeat/infrastructure suites: **122 PASS**. Linux MVP regressions: **159 tests OK, one skipped**. Python source parsing and deploy Bash syntax checks: **PASS**. Fresh independent Superpowers reviewer: **ACCEPT**, no Critical, Important or Minor findings; verdict covers final source and targeted test changes. No unrelated Windows tests repaired.

Full test suite **did not complete successfully**. Harness reported timeouts; the retained verbose log later recorded completion after 2444.360 seconds: **1337 tests, six failures, six errors, 29 skipped**. Four pre-existing WSL errors reproduce on unchanged base `e737b6ac810d1da39440a9c4f4e38878a5c8515f`. All raise `OSError: windows_job_required`:

- `tests.test_live_guard_races.BlockedTransportDeadlineTests.test_real_job_kills_child_while_stop_waits_on_blocked_transport`
- `tests.test_live_guard_races.StartupRaceTests.test_delayed_assignment_after_child_waits_then_go_succeeds`
- `tests.test_live_guard_races.StartupRaceTests.test_premature_go_existing_but_unassigned_job_refuses`
- `tests.test_live_guard_races.StartupRaceTests.test_supervisor_delays_assignment_until_actual_child_waits`

Additional full-suite limitations, outside heartbeat changes:

- `tests.test_cp57_arm_serialization_audit.ArmSerializationAuditTests.test_retained_numeric_event_reaches_actual_arm_before_publication`: missing retained SQLite evidence `data/cp5_6c_replacement_20261017_0351adeb-6594-492e-9237-adf7d833dba0.sqlite3`; reproduces in archived base environment.
- Six `tests.test_recorder_live_clob.LiveClobTests` failures in `setUp`: retained response hashes differ from expected hashes. Base Git fixture bytes equal worktree bytes; all six failures reproduce on base: `test_c_alias_does_not_override_gamma_identity`, `test_c_alias_preserves_token_and_outcome_rejection`, `test_captured_responses_acquire_selected_revision`, `test_each_alias_and_matching_combinations_accepted`, `test_malformed_or_conflicting_alias_never_hidden_by_matching_alias`, `test_missing_condition_alias_rejected`.
- `tests.test_read_only_boundary.ReadOnlyBoundaryTests.test_offline_suite_blocks_network`: full run received `ConnectionRefusedError` instead of expected offline guard assertion. Isolated base run passes; full-suite interaction cause not established. Test and boundary sources are unchanged; no claim that this full-suite interaction was reproduced on base.

Those tests, fixtures and their production code are unchanged. Publication is not a claim of a green full suite. Previous pilot remains **FAIL**; new VPS deployment, fresh exact-code/fingerprint approval and pilot acceptance remain pending. No VPS, deployment, approval creation, trial or trading operations performed during this remediation.

## Conditions for repeating 24-hour soak

All eight fresh pilot gates independently reviewed and accepted; exact Hermes historical deltas exported or explicitly documented unavailable; successful bounded observer-failure/deadline test; deployment and full runtime identity reviewed; no unresolved important code-review findings. Owner explicitly authorizes new soak trial, not old trial repair.

Set/review soak `RuntimeMaxSec=86640` before new fingerprint; no code/config changes except reviewed phase-bound setting. New soak approval requires `phase=soak`, `seconds=86400`, exact full SHA/fingerprint, all `pilot_gates` keys `1` through `8` true, real `pilot_trial_id`, and `pilot_evidence_sha256`: SHA-256 of compact sorted JSON map of trial-relative file paths to SHA-256, covering frozen complete pilot evidence. Controller verifies referenced local evidence. Approval remains separate owner act. Run PHASE=soak only after that authorization. Any runtime edit, fingerprint mismatch, cleanup gap, timestamp conflict, missing snapshot, ingestion/continuity/trading violation blocks run and review. Runtime remains `seconds+240`; baseline deadline is `seconds+60`. Require `RuntimeRandomizedExtraUSec=0`, no restart and unchanged 30-second main stop policy. Admission checks measured activation-to-baseline lag; actual observed shutdown decides success, not nominal timer ordering.
