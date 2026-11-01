# Service-scoped preservation candidate — local review only

Supersedes **only after explicit owner decision** the exclusivity interpretation in [approved preservation checklist](phase-2b-installer-preservation.md). Approved installer `68c7276510318e2d86ffa988543ebee62ef80cdc`, controller `69079bbce0c4a02ed1a7f81f3c5128b430841aab`, guard and template stay unchanged. Candidate is uncommitted; no VPS commands, installation or production restore performed. Commands below are proposed for separately authorized Hermes work on Ubuntu 24.04.4/systemd 255.4, not permission to execute.

## Boundary: bot-owned operations, not exclusive VPS administration

Normal cloud monitoring/security agents coexist. No Tencent disablement, all-admin agreement or host-wide maintenance mechanism. Coordinate only actual services, users, schedulers and automation that launch this bot or write its three DB families/configuration/evidence. If a known conflicting writer cannot be paused, or an unidentified writer remains, stop at that concrete blocker. Unrelated root capability alone is not a blocker; root can bypass any application control.

Installer guard already checks infrastructure units/jobs/cgroups twice, including legacy static service and template instances. Its exact historical-audit exception remains intact. It is an admission snapshot, not a lock. Paper coexistence is allowed by installer, but paper writer must pause for the coordinated three-DB preservation boundary. No historical helper stop/reset/delete. STOP stays unchanged: it does not exclude DB writes (run registration, events and observer schema initialization still write).

## 1. Approve identities and inventory actual paths

Owner records decision adopting this scoped requirement, exact approved installation SHA, separately reviewed helper artifact/hash, and maintenance window for **bot-owned** launch paths. Installing approved SHA does not require installing this candidate code: run reviewed helper from separately verified checkout. Publishing candidate/helper needs separate approval; do not deploy uncommitted files. Existing release and old fingerprints remain preserved, not silently reapproved.

Hermes records trusted effective configs privately, without printing credentials, and confirms all three existing regular, distinct DBs:

| Role | Known path; actual configuration/existence must be confirmed |
|---|---|
| Paper | `/var/lib/polymarket-btc/paper.sqlite` |
| Legacy infrastructure | `/var/lib/polymarket-btc/infrastructure.sqlite` |
| Isolated infrastructure | `/var/lib/polymarket-btc-infrastructure/data/infrastructure.sqlite` |

Missing DB is **BLOCKED pending owner reconciliation**, not permission to create empty DB or silently omit history. Inventory main DB, `-wal`, `-shm`, `-journal`, state directories and actual STOP paths from each `MVP_STATE_DIR`; record absent paths too. Check no unlisted DB override or alternate state directory. Reject unexpected symlinks/hard-link aliases pending review. Record owners/modes/ACLs/xattrs, hashes, unit/drop-in/link targets, prior enablement and absence, release/runtime manifests and controller-owner bytes. Keep raw config/unit contents in restricted storage only; command/environment listings can contain secrets.

## 2. Pause known launch pathways and verify quiet state

Use one retained root shell with `umask 077` and parent-shell `set +e` (failure must not silently exit shell and release lock). Command blocks below use explicit subshell failure gates; any nonzero result stops progression, not coordination. Keep shell alive and launchers paused for reviewed recovery. Never paste all blocks as an unattended script. Review exact list before mutation: paper service, legacy infrastructure static service, concrete infrastructure instances, bot-owned timers/path/socket activation and external cron/deployment/manual sessions. Do not wildcard-stop `infra-*`: retained historical audit must stay intact. Disable/pause only actual launchers, record exact inverse, stop only current writer services with separate authorization. No automatic restart after installation or failure.

Hold existing controller coordination lock throughout preservation/install/recheck, without replacing/unlinking lock inode:

```bash
# Proposed only; state root must already exist and have reviewed ownership.
if exec 9<>/var/lib/polymarket-btc-infrastructure/trial.lock; then
  flock -n 9
  LOCK_STATUS=$?
else
  LOCK_STATUS=1
fi
# Proceed only if LOCK_STATUS=0; otherwise STOP, do not run subsequent blocks.
```

Controller `launch` cooperates with this lock only during launch submission. Existing observers, direct `systemctl` starts, legacy installers, arbitrary CLI/store writers and same DB with another state directory do not. Pause these **known bot-owned pathways** separately; do not claim flock excludes them. `service.lock` only coordinates CLI processes sharing one state directory. Do not hold a paper lock to claim DB-level exclusion. Do not replace frozen launch/controller architecture.

Verify exact units: no pending relevant jobs, active writer services, MainPID/ControlPID, or populated descendant cgroups. Retained historical exited audit must still satisfy approved guard. Inspect relevant bot processes and DB file users locally (`fuser` if installed, `/proc` otherwise), including observers/direct writers, without exposing full command lines in output. Open-file inspection alone is not writer exclusion; combine actual launcher inventory, paused owner-controlled paths, process checks and subsequent fingerprints. Unreadable/ambiguous checks block. Run approved read-only guard; paper/process checks remain separate. Record safe properties and baseline hashes/metadata in restricted evidence. No repeat cloud-agent investigation.

## 3. Preserve logical DB snapshots and raw evidence separately

Use owner-selected existing root-owned backup parent outside repo/releases and outside every source tree. No shared or writable parent. Check space for all raw data, snapshots and scratch; no automatic pruning. Root-only storage, encryption according to backup policy, `0700` directories/`0600` files. Select unique absent destinations; never reuse failed bundles.

```bash
# Fill reviewed absolute paths; no eval, no secret expansion in logs.
HELPER=/reviewed/checkout/deploy/preserve-sqlite.py
B=/restricted/backup-parent/UNIQUE_RUN
BACKUP_OK=0
(
  set -e
  test "${LOCK_STATUS:-1}" -eq 0
  mkdir -m 0700 "$B"                   # exclusive; existing destination fails
  /usr/bin/python3 -I -B "$HELPER" backup --output "$B/sqlite" \
    /var/lib/polymarket-btc/paper.sqlite \
    /var/lib/polymarket-btc/infrastructure.sqlite \
    /var/lib/polymarket-btc-infrastructure/data/infrastructure.sqlite
)
BACKUP_STATUS=$?
if test "$BACKUP_STATUS" -eq 0; then BACKUP_OK=1; fi
# Nonzero BACKUP_STATUS: STOP progression; parent shell retains FD9.
```

Helper uses SQLite supported backup API, then snapshot integrity/schema/table-count/hash inventory and durable `COMPLETE.json` manifest last. Default 30-second budget is checked during backup progress and between stages; hashing, integrity and filesystem calls are not hard real-time bounded. A slow or interrupted stage never proves success. Its output is not production DB restore permission. Failed, timed-out, interrupted or incomplete output is NOT backup proof; preserve failure evidence, keep launchers paused, choose new destination only after cause reviewed. SIGKILL/power loss cannot run cleanup; marker presence alone never proves successful command completion or durability. Require successful command result, independently hashed bundle and isolated verification; interrupted run needs review even if marker exists. Never promote partial files manually.

**Consistency:** each backup is transaction-consistent, including committed WAL content. Three backups are sequential, **not one atomic cross-DB transaction**. Coordinated quiet boundary depends on actual known writers being stopped; before/after DB/WAL/journal fingerprints detect changes, not all transient writes or hostile cooperation. SHM is reader-lock metadata: read-only SQLite can create/touch SHM. Record SHM existence/metadata, do not use SHM changes alone as data-change proof. No `immutable=1` against live WAL; no forced checkpoint, WAL deletion or journal recovery on production. Any detected DB-family mutation invalidates coordinated preservation and blocks installation until reconciled.

Privately build root-owned NUL-delimited absolute-relative-to-`/` path list `$B/paths.nul` from **existing inventory**, plus separate absent-path list. Include `/etc/polymarket-btc`, exact static/template/instance units and drop-ins/enablement links, all three DB families and state/STOP trees, controller-owner, full historical evidence, old installed releases/runtime manifests, effective unit/approval/fingerprint evidence, and metadata of shared ancestors the installer changes. Avoid recursive `/etc` or host-wide capture. No symlink dereference. Do not include backup/scratch destinations. Existing raw DB families are historical artifacts, not substitutes for logical snapshots.

```bash
# GNU tar; reviewed paths.nul and absence/metadata inventory already captured.
ARCHIVE_OK=0
(
  set -e
  set -o noclobber
  test "${BACKUP_OK:-0}" -eq 1
  tar --acls --xattrs --numeric-owner --atime-preserve=system \
    -C / --null --verbatim-files-from -T "$B/paths.nul" -cpf - > "$B/files.tar"
  chmod 0600 "$B/files.tar"
  sha256sum "$B/files.tar" > "$B/files.tar.sha256"
)
ARCHIVE_STATUS=$?
if test "$ARCHIVE_STATUS" -eq 0; then ARCHIVE_OK=1; fi
# Nonzero ARCHIVE_STATUS: STOP; archive incomplete even if file exists.
```

Record archive completion/exit status; any tar warning about changed files blocks. Capture before/after file hashes and metadata for listed config/STOP/evidence/manifests/DB families; require equality except documented SQLite reader SHM effects. No concurrent bot changes through install. Archive preserves owner/mode/timestamps/ACLs/xattrs/link targets; separately inventory root/ancestor metadata and absent paths. Restrict archive/list/hash permissions and retain originals. Review path list to avoid duplicate subtree entries. Check DB-family fingerprints again after archive; recheck immediately before installer and after installer. Any unexpected change means stop, preserve evidence, investigate only changed bot scope.

## 4. Prove isolated restore before installation

Scratch must be a new root-owned `0700` directory outside live config/state/releases; never under `/etc`, live `/var/lib` trees or production release paths. Do not boot/start restored services. Before extraction review archive member names and hard-link targets: no absolute paths, `..` traversal or writes through symlink ancestors. Preserve reviewed symlink target bytes unchanged, including legitimate absolute systemd enablement links. Never follow restored symlinks; compare intended absolute targets through explicit scratch-path mapping, not live destinations. Archive comes from trusted root-owned capture; if damaged or identity differs, block. Confirm same-host ACL/xattr namespace compatibility: unsupported `security.*`/`trusted.*` restoration blocks; do not silently omit attributes. Extract only to scratch, including ACL/xattr/numeric ownership as captured, compare full inventory/hash/metadata/link targets and absent-path records. Do not follow restored links out of scratch during comparison. Raw DB files are not opened for recovery; logical copies below are used.

```bash
# RESTORE_PARENT reviewed root-only existing parent, RESTORE absent.
RESTORE=/restricted/scratch-parent/UNIQUE_RUN
RESTORE_OK=0
(
  set -e
  test "${ARCHIVE_OK:-0}" -eq 1
  mkdir -m 0700 "$RESTORE"             # exclusive; no existing scratch accepted
  sha256sum --check "$B/files.tar.sha256"
  mkdir -m 0700 "$RESTORE/files"
  tar --acls --xattrs --xattrs-include='*' --numeric-owner --same-owner --same-permissions \
    -xpf "$B/files.tar" -C "$RESTORE/files"
  /usr/bin/python3 -I -B "$HELPER" verify "$B/sqlite" "$RESTORE/sqlite"
)
RESTORE_STATUS=$?
if test "$RESTORE_STATUS" -eq 0; then RESTORE_OK=1; fi
# Nonzero RESTORE_STATUS: STOP; do not install or reuse scratch.
# RESTORE_OK still requires separate full metadata/history/absence comparison below.
```

Require helper verification publication plus all three copied snapshot hashes/integrity/schema/counts equal manifest. Require native archive metadata/history/STOP equality too; SQLite-only success is not complete preservation. Validate old release executable/venv and host-Python manifest still usable; helper does not recreate lost external runtime dependencies. Record old/new mapping and exact reviewed metadata inverse. No install if scratch verification fails.

## 5. Install approved release only after separate authorization

Recheck bot launcher pause/lock, units/processes, guard, source identities and preservation fingerprints. `daemon-reload` is global: owner confirms no unrelated pending unit-file edits or concurrent reloads. Execute only separately authorized approved installer from clean verified approved checkout, not this uncommitted candidate. Installer itself never starts/enables service. Keep STOP intact. Review effective unit/drop-ins, environment `MODE=PAPER_ONLY`, isolated DB/state paths, unchanged risk limits, controller-owner and runtime manifest/fingerprint against approved bytes. No launch just because install succeeded. No retry on failure.

Installer is deliberately unchanged, **not transactional**. Mutations can precede dependency failure: account creation, directory permissions, marker, partial release, optional default environment. Unit replacement/reload/verification can fail later. Existing `.pre-*` fragment is supplemental evidence, not full rollback. Existing release directory blocks retry; never delete it blindly.

## 6. Failure/recovery: configuration inverse, never data rollback

1. Keep bot launchers paused, coordination lock held and STOP unchanged. Record failure stage/exit status and post-failure hashes, metadata, unit properties and partial release identity privately. Preserve all partial release/evidence; no start, retry, reset-failed, old approval reuse or broad cleanup.
2. Compare pre-install inventory with post-failure state. Build explicit per-path inverse for changed unit/config/marker and directory metadata only. Require current bytes/metadata still match recorded post-failure state before each inverse. If anything changed since recording, stop; no overwrite of newer work. Recovery needs separate owner authorization and review of exact paths/absence/modes.
3. From verified scratch, restore prior regular unit/config/marker with `cp --preserve=all --no-dereference -- EXACT_VERIFIED_SCRATCH_FILE EXACT_REVIEWED_DESTINATION` only after identity and non-symlink checks; restore directory metadata with exact `chown`, `chmod` and recorded ACL/xattr commands **without recursion**. Prior absent files may be removed only if proven created by this attempt and still byte/metadata-identical to recorded failed artifact. Do not remove directories containing history or DBs. New service accounts/partial releases stay quarantined pending separate review; no automatic user deletion or release cleanup.
4. **Never restore archived DB, WAL, SHM, journals, STOP, or historical evidence over production.** Preserve current and old versions; reconcile any writes separately. Metadata inverse cannot certify a whole-system rollback or resurrect deleted newer data. No forced production SQLite recovery/checkpoint.
5. Validate restored unit fragments before separately authorized reload, recheck selected properties, no jobs/PIDs/cgroup writers, STOP/config/history/DB hashes and metadata, then obtain fresh review. Do not automatically start old services or re-enable launchers. Release coordination lock only at approved handoff. Restore prior launcher enablement only by exact recorded inverse and separate authorization; do not resume writers before preservation review completes.

Local `tests/test_installer_preservation_recovery.py` rehearses this metadata-only inverse with synthetic partial-install mutations and a newer synthetic DB in temporary storage. It does not run installer/systemd, prove target permissions, or test a live rollback.

## Local candidate verification

141 targeted tests passed on WSL Python 3.14.4 / SQLite 3.46.1: installer admission/artifacts/controller, service locks, STOP, paper/live separation, 19 SQLite preservation tests and two synthetic metadata-recovery rehearsals. New helper tests demonstrated RED before implementation and RED→GREEN for five independent safety findings. No real installer execution. Installer and four proposed shell blocks passed syntax checks; frozen Git blobs unchanged. Python 3.12 grammar checked, **3.12 runtime execution not verified**. One temp-only GNU tar roundtrip checked bytes/modes/uid/gid/mtime/user xattr and absolute-link bytes; no target-root ACL/security-xattr proof implied. No full-suite rerun or historical failure reclassification.

Independent whole-candidate Superpowers reviewer `ses_e66b7f9e2ffeJy9Z002LBGGxOj`: **SPEC PASS — APPROVED LOCAL CANDIDATE**, no remaining Important/Critical findings. No publication, installation, production recovery, pilot or soak authorization. Nonblocking coverage limit: changed-identity recovery test covers newer bytes, not metadata-only changes; procedure requires both. Plan crash-guarantee wording corrected to match procedure.

## Acceptance unchanged

Original eight infrastructure gates remain: continuous operation, restart capability, deadline cleanup, network ingestion, database integrity, host continuity, no new trades, hash integrity. See [infrastructure acceptance](phase-2b-infrastructure-acceptance.md) and approved controller policy (`pilot_gates` 1–8); [Phase 2B validation](phase-2b-validation.md) remains historical context. Preservation/install success replaces none of them. No pilot/soak/live approval, profit claim, risk-limit change, historical verdict rewrite or production readiness follows. Target admission/cgroup compatibility evidence already accepted remains historical evidence, not a newly rerun test.

Operational blockers are concrete: owner adoption/authorization; verified actual three DB paths/writers and paused own launch pathways; restricted complete archive + isolated restore evidence; any unexpected bot-scoped changes; effective installed fingerprint review. Normal monitoring agents are not blockers. Single next action after local review: owner decides whether to adopt this service-scoped preservation boundary, then separately authorize Hermes preservation-only work.
