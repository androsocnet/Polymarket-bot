# Phase 2B final soak remediation

**Status: BLOCKED. Not Phase 2B acceptance, deployment authorization, or soak authorization.**
Historical trial: `soak-20261026T040455Z`. Approved historical base: `f8ccedc5fe8d0b7465cb65aa87ee0e15b0737f4a`. User reports six of eight gates passed; gates 3 and 8 blocked. Exact VPS runtime files are not bundled locally. No direct VPS access performed.

## Root causes reproduced in repository

- Deadline command stops only main infrastructure service. Shell semicolon lets final `systemctl show` hide stop failure. No helper/timer/cgroup cleanup proof.
- Heartbeat moves temporary JSON then reads removed temporary path under `set -e`; real local regression exits after first sample. JSONL absent.
- Native runner passes `--remain-after-exit=no`, unsupported by local systemd CLI; correct native property is `--property=RemainAfterExit=no`.
- Runner precreates output directory; legacy paper observer requires nonexistent output. Legacy observer reads paper environment/database and Windows heartbeat schema. Native infrastructure cannot safely reuse it unchanged.
- Historical `code_hash` excludes shell helpers, hashes subset of Python files, and reads paper config. Installer copied mutable working bytes and editable-installed source checkout. Git SHA alone did not describe installed runtime.
- Cumulative DB totals mix run IDs. No consistent trial snapshots means historical delta cannot be reconstructed confidently.
- Trial suffix identifies launch metadata time, not service observation start. Manual timezone arithmetic is ambiguous. New metadata records UTC `Z`, WIB `+07:00`, launch versus observation start separately. Example: `2026-10-26T04:04:55Z` is `2026-10-26T11:04:55+07:00`, not evidence that service started at that instant.

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

Independent deadline stops main service, heartbeat, observer and deadline timer, preserving errors individually. Durable `deadline-stop.json` precedes DB processing. Snapshot subprocess has 60-second whole-operation timeout; deadline helper gets 360 seconds for four bounded stops/queries plus snapshot, audit starts 420 seconds after deadline and gets 480 seconds for six bounded stops/queries plus fingerprint. These are ceilings, not permission to accept missing proof. Later independent audit verifies/stops deadline service and both timers after deadline exits. Cgroup descendants checked, not only MainPID. `active/exited` plus PID 0 and empty cgroup means no running process; timers still must be inactive. Failed stop never becomes success through later show.

SQLite before/after/deadline snapshots use online backup API, bounded backup progress, integrity checks, exclusive creation and streaming hashes. Trial ingestion filtered by bot/run ID; samples retain per-run counts and timestamps. Trading rows compared by ordered content digests, including historical positions lacking run IDs.

## Hermes redeployment — only after verified commit and explicit deployment authorization

Do not use untracked helper scripts. Do not edit runtime code on VPS. Do not run these steps until review/test verdict is clear and final published SHA is provided.

1. Preserve/export historical evidence above. Confirm infrastructure inactive, all old owned trial jobs/timers stopped through separately authorized operations, no target cgroup PIDs. Existing paper service/config/database remain untouched. Inspect disk, ownership, Python >=3.12 and installed `uv`; no automatic package-manager operation.
2. In `/home/ubuntu/Polymarket-bot`, fetch `origin linux-mvp`; verify supplied full SHA equals `origin/linux-mvp`, inspect `git status`. Use separate clean checkout of exact SHA if local tracked edits exist; preserve them. Never reset/delete local edits. Verify no `feat/strategy-learning` merge.
3. Run source tests using repository dependencies and `PYTHONPATH=src`, Bash syntax checks. Execute `sudo env PATH="$PATH" bash deploy/install-infrastructure.sh` from clean exact checkout. Installer must not start service. If release path already exists, stop; inspect/verify it, do not delete or overwrite.
4. Set `R=/opt/polymarket-btc-infrastructure/releases/<VERIFIED_FULL_SHA>`. Review preserved `/etc/polymarket-btc/infrastructure.env`; keep `MODE=PAPER_ONLY` and risk values unchanged. New DB path must be `/var/lib/polymarket-btc-infrastructure/data/infrastructure.sqlite`, with service-owned parent for WAL/SHM. Set `MVP_STATE_DIR=/var/lib/polymarket-btc-infrastructure/data/state` too. Archive old config before authorizing only these storage-path changes; any old infrastructure DB/evidence stays in place. Installer checks service traversal of new data/state paths; it never grants access to paper-owned parent. Leave old DB/history in place; new trial initializes separate schema. Do not copy a live DB file or silently rewrite config. Review `systemctl cat polymarket-btc-infrastructure.service` and every drop-in. Main `ExecStart` must use `$R/.venv/bin/python -m polymarket_bot.linux_mvp ingest` and `PYTHONPATH=$R/src`.
5. Install phase-bound drop-in through explicit operator authorization, not local code edits: `[Service]`, `Restart=no`, `RuntimeMaxSec=300` for pilot. Run `systemctl daemon-reload`; verify raw D-Bus RuntimeMaxUSec `300000000`, KillMode control-group, user, resource limits, no unexpected drop-ins. Config/unit changes occur **before** fingerprint approval.
6. Run `sudo env PYTHONDONTWRITEBYTECODE=1 "$R/.venv/bin/python" "$R/deploy/infrastructure-trial.py" fingerprint`. Export manifest, effective unit/config, full SHA and fingerprint for independent owner review. Fingerprinting is not approval.
7. Owner supplies root-owned approval file, outside release, with `scope=INFRASTRUCTURE_ONLY`, `phase=pilot`, `seconds=180`, exact `git_sha` and `fingerprint`, `authorized=true`, real `approved_by`, real `approved_at_utc` ending `Z`. Do not generate approval by copying newly calculated hash without review. Never alter historical approval.

## Fresh 180-second pilot

After deployment and exact fingerprint authorization:

```bash
R=/opt/polymarket-btc-infrastructure/releases/<VERIFIED_FULL_SHA>
sudo env PHASE=pilot APPROVAL=/etc/polymarket-btc/approved-native-pilot.json \
  bash "$R/deploy/infrastructure-runner.sh"
```

If infrastructure STOP exists, pause. Owner must explicitly authorize infrastructure-only reset; rerun command with `--reset-stop`. No automatic sentinel deletion. Main paper STOP untouched.

Runner prints single trial ID and evidence path. Separate bounded restart proof precedes continuous window. Do not disconnect-dependent tail, restart during observation, edit runtime, or stop safety timers. Continuous pilot is 180 seconds; deadline scheduled at launch +360 seconds, audit at launch +780 seconds. Wait for audit to finish before judging cleanup; inspect audit service itself afterward for inactive/failed with MainPID=ControlPID=0, empty cgroup, no jobs. `PENDING_REVIEW` is not acceptance.

After audit exits, run `sudo env PYTHONDONTWRITEBYTECODE=1 "$R/.venv/bin/python" "$R/deploy/infrastructure-trial.py" inspect-cleanup --folder /var/lib/polymarket-btc-infrastructure/evidence/<TRIAL_ID>` and preserve stdout as new external post-audit evidence. After reviewing successful exit, explicitly install that output as root-owned mode-0600 `<TRIAL_FOLDER>/post-audit.json` before freezing pilot bundle; no overwrite of historical files. Soak preflight requires this post-exit artifact, snapshots/summaries, samples, full host JSONL, launch/approval/restart/deadline/cleanup records and journal, with consistent trial/run metadata and snapshot hashes. This read-only check includes audit service, cgroups and pending jobs. Nonzero exit blocks gate 3. Export complete fresh trial folder and journals for all exact owned unit names, post-audit unit/cgroup evidence, runtime fingerprint and SHA inventory. Require before/after/deadline DB integrity; check trial-run ingestion and unchanged trading digests. Do not substitute lifetime totals. Resource/continuity gaps block review.

## Eight unchanged acceptance gates

1. Continuous 180s pilot / 86400s soak, required samples (at least seconds/10), elapsed monotonic time, unchanged PID/InvocationID/NRestarts.
2. Separate restart proof with distinct InvocationID and verified clean stop before continuous window.
3. Independent deadline + post-exit cleanup audit, main/helpers/timers/cgroup PIDs and audit service terminal state. Test observer-failure path on separately authorized bounded pilot; do not infer deadline effectiveness only from already-stopped service.
4. Per-run positive ingestion, fresh sample progress across complete window, attributable run ID and timestamp range. Historical rows excluded.
5. Every quick_check and before/after/deadline full integrity; safe snapshot hashes and no missing baseline.
6. Full native host JSONL starts sequence 1, consecutive samples, one boot/session, no suspend/clock/gap flags. Guest continuity only, not hypervisor power evidence.
7. No new/changed orders, positions, settlements, decisions or equity; history retained.
8. Pretrial independent approval matches source-controlled deployed artifacts/config/effective unit and final fingerprint. No retrospective approval of modified historical runtime.

## Conditions for repeating 24-hour soak

All eight fresh pilot gates independently reviewed and accepted; exact Hermes historical deltas exported or explicitly documented unavailable; successful bounded observer-failure/deadline test; deployment and full runtime identity reviewed; no unresolved important code-review findings. Owner explicitly authorizes new soak trial, not old trial repair.

Set/review soak `RuntimeMaxSec=86520` before new fingerprint; no code/config changes except reviewed phase-bound setting. New soak approval requires `phase=soak`, `seconds=86400`, exact full SHA/fingerprint, all `pilot_gates` keys `1` through `8` true, real `pilot_trial_id`, and `pilot_evidence_sha256`: SHA-256 of compact sorted JSON map of trial-relative file paths to SHA-256, covering frozen complete pilot evidence. Controller verifies referenced local evidence. Approval remains separate owner act. Run PHASE=soak only after that authorization. Any runtime edit, fingerprint mismatch, cleanup gap, timestamp conflict, missing snapshot, ingestion/continuity/trading violation blocks run and review.
