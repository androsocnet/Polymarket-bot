# Native Ubuntu Phase 2B infrastructure testing

Status: proposed; implementation waits for owner approval of this document.

## Intent and constraints

Run ingest-only infrastructure pilot/soak on Ubuntu 24.04/systemd 255 (2 vCPU, about 2 GB RAM), without Windows/WSL runtime dependencies. Repository source: /home/ubuntu/Polymarket-bot. Existing polymarket-btc-paper.service and unrelated services must remain untouched. This is repository work only: no VPS access, installation or live trial. No strategy, trading engine, risk-limit or historical accounting changes. No force-push/main changes.

## Publication and review

This design document is committed before implementation for builder check. Implementation proceeds via TDD after approval. Independent fresh review and fresh verification-before-completion gate apply before push. Subagent review was unavailable (session identifiers invalid in this harness); review performed via documented checklist.

## Builder defaults (owner-approved choices)

- Add separate infrastructure installer and unit; do not repurpose existing paper installer.
- Defaults: target /opt/polymarket-btc; interpreter /opt/polymarket-btc/.venv; account/group polymarket-btc (nologin); environment /etc/polymarket-btc/infrastructure.env; unit polymarket-btc-infrastructure.service.
- Isolate DB, lock, STOP and evidence below /var/lib/polymarket-btc/infrastructure; never touch paper.sqlite/paper.env or existing state/STOP.
- Preserve existing risk defaults, including MVP_MAX_DAILY_LOSS=10.

## Deployment (safe, idempotent, isolated)

Inventory and report target, interpreter/venv (python3 >=3.12), account, unit fragment/drop-ins, enablement/activity, configuration, and path ownership before any write. Check required local tools (system python3, python3-venv documented prerequisite; no apt calls in installer), paths/symlinks and available resource capacity. Install only tracked required application files; no rsync --delete and no recursive chown of preexisting trees. Root owns application and environment; service can read code/config and write only infrastructure state. Service never starts at install; no boot-enable.

Clean target: create managed install, venv, and install project dependencies only. Idempotent repeat verifies content/ownership and succeeds without destructive writes. Existing unmanaged target: reuse only if required files/interpreter and permissions are compatible and verifiable; otherwise fail before any change with actionable conflict report. Never overwrite conflicting files/configuration/units, never adopt an existing privileged/login account, and never recursively change ownership of existing unrelated files.

Managed upgrades require inactive infrastructure jobs and unchanged previously installed files; preserve unrelated files/databases/units. If an existing paper service shares deployment files, do not change shared files/venv; require operator-approved migration instead of silently disrupting paper service. This is deliberate safe conflict handling, not automatic migration.

Infrastructure unit: ingest CLI only, PAPER_ONLY, Restart=no, separate [Install] without WantedBy=multi-user.target (no boot enable), unprivileged dedicated account; retain hardening and bounded CPU (50%), memory (512M), tasks (64), file descriptors (256), stop timeout, KillMode, NoNewPrivileges and protect/namespace directives. Verify effective systemd properties, account, paths and permissions before launch; reject unexpected overrides.

## Runtime and continuity

Replace infrastructure launcher's external host-helper dependencies with repository-local native Python/systemd facilities. Legacy paper host heartbeat preserved for the paper workflow; infrastructure mode uses a native continuity monitor only and never reads Windows paths, calls PowerShell, or requires Windows event logs.

Launch submits a scoped systemd job detached from the SSH connection using `systemd-run --unit ... --remain-after-exit=no` for bounded controller/heartbeat/deadline, plus `systemctl start` for the infrastructure service itself. Record durable job/evidence identity and provide journal/status/status-result commands. A narrow lock prevents concurrent infrastructure tests/deployments.

Native heartbeat runs as its own service independent of observer. Records boot_id, fresh session UUID, increasing sequence, wall time, CLOCK_MONOTONIC and CLOCK_BOOTTIME seconds, atomically writing current JSON and append-only JSONL. Reject boot/session changes, sequence regression/gaps in complete log, stale heartbeat, backward clocks, excessive sample gaps, and suspend discrepancies. SSH disconnect is not a session change: session means heartbeat process lifetime. Native evidence establishes guest continuity, not hypervisor or Windows power-event evidence.

Arm independent systemd deadline before starting the infrastructure service, covering the bounded restart step plus the requested continuous window and cleanup buffer; verify timer is active. Enforce service RuntimeMaxSec and Restart=no; deadline survives observer/controller failure and stops only the intended infrastructure jobs, confirms inactive state, MainPID=0, empty target cgroup, captures durable result and cleans up owned timers/jobs without touching other services. Do not infer success from the final command alone. Cleanup failures remain failures.

Operator may supply an explicit reset of the verified infrastructure STOP. Do not auto-remove STOP. Reset applies only to infrastructure STOP with inactive jobs and never touches shared paper sentinel. Reject reuse/suppression of STOP from an unrelated phase.

## Restart proof (separate bounded step)

Independent of the continuous observation window: stop service, confirm ActiveState=inactive and MainPID=0 and that observer would invalidate PID/InvocationID/restart-count changes during continuous runs. Then start service and confirm clean start. Record as `restart-verification.json` before any continuous phase. Continuous soak never performs an automatic restart inside its uninterrupted window.

## Eight acceptance gates

1. Continuous operation: 180-second pilot or 86,400-second soak, elapsed duration plus required sample coverage, unchanged service identity during the continuous window; no automatic acceptance from launch success.
2. Restart capability: separate bounded stop/start proof before continuous observation, distinct service invocations and clean stop.
3. Deadline cleanup: durable independent deadline evidence, verified inactive service, no target processes and no orphaned owned timers; tests include observer/controller failure.
4. Network ingestion: observation count/timestamp progress over the continuous window, positive before/after delta, bounded freshness; lifetime totals are not run deltas.
5. SQLite integrity: quick_check per sample; consistent before/after/deadline backups with integrity_check, bounded read-only access, no historical reset. Clean infra DB starts with no trading rows before baseline capture.
6. Host continuity: full native heartbeat coverage, one session/boot, consecutive sequences, valid wall/monotonic/boottime intervals; fail closed on unreadable/missing evidence.
7. No trades: ingest-only unit; before/after zero new/changed rows in orders, positions, settlements, decisions and equity while preserving history.
8. Hash/approval: exact identity binding deployed runtime sources/helpers, installer/unit templates and effective systemd unit/config. Only phase duration bound may be normalized and independently validated; normalized fields documented. Approval validated before service/config/STOP mutation; rechecked during/after observation. Approval never authorizes trading or deployment changes.

A 180-second pilot may run to collect evidence; result remains pending independent review and is not soak authorization. Soak requires reviewed native infrastructure approval with all eight pilot gates, restart/deadline evidence, exact hash, scope and explicit authorization. Do not reuse legacy paper approval or fabricate approval JSON. The legacy paper approval semantics remain supported separately by paper workflow; this is a distinct infrastructure approval.

## Verification approach

TDD: failing offline tests first for clean/repeat installs, existing-paper protection, native heartbeat discontinuities, SSH-independent launch shape, deadline order/failure/cleanup, resource property checks, DB initialization/integrity/zero-trade/ingestion-growth invariants and approval mismatch-before-mutation. Use stdlib unittest and mocks/temp directories; no new test framework. Use isolated local Linux systemd tests only when available and safe; otherwise report the limitation and state that remote VPS verification is required.

Independent fresh review before commit; resolve important findings; rerun the full relevant suite plus syntax/unit checks. Push only after fresh local and remote verification: ls-remote, fetch, rev-parse/rev-list counts, remote HEAD and exact required paths/blobs, unchanged main. Do not launch while blocked; flag runtime VPS readiness separately from publication.

Builder may choose the minimal file split, manifest format, native mode CLI shape and test names consistent with the repository. No new runtime dependencies beyond existing project packages/stdlib/systemd.
