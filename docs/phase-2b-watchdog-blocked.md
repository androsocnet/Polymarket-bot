# Phase 2B deadline watchdog — BLOCKED

## Operator decision

**Do not arm a real observer-failure trial. No emergency watchdog is supplied.**
This operator-only branch preserves deployed release
`100dc27059f9ed96aa0a7239aef888e21192426c`. Do not deploy this branch,
run the infrastructure installer, change service configuration, or infer
pilot/soak/live-trading approval. Eight acceptance gates remain unchanged.

The requested alternative is BLOCKED: retained `cgroup.kill` descriptors bind
cgroup objects, not systemd invocations. A replacement can enter the same live
cgroup. No independent process plus retained descriptor can enforce the required
invocation boundary without an additional lifecycle guarantee.

Historical `pilot-20261031T022625Z` bundle and failed watchdog source are not
present locally. Normal pilot 8/8 PASS is user-supplied context, not independently
verified here. No historical evidence was changed. No VPS connection was used.

## Existing failure and rejected designs

`InvocationID` read followed by `systemctl stop UNIT` is check-to-action unsafe:
a replacement can start after the read but before systemd handles the stop.
Repeated checks only move this race. Controller reference:
`deploy/infrastructure-trial.py`, `stop_main()`, `record_main_shutdown()`.
No controller/runtime modification is included.

A previously validated pidfd binds a process independently of numeric PID reuse.
`pidfd_send_signal()` cannot target a later process occupying the same PID.
However, main-process exit is not descendant cleanup. Enumerating descendants
and opening pidfds leaves concurrent-fork and orphan-discovery gaps. This alone
cannot satisfy complete original-process-tree quiescence.

Retaining `cgroup.kill` removes the pathname lookup race but not invocation reuse:

1. Invocation A runs in cgroup C; watchdog retains C's kill FD.
2. A's main process exits; a descendant survives, so C cannot be removed.
3. Invocation B starts in the existing C with a new InvocationID.
4. Writing the retained FD kills both A's descendants and B.

Trial ID, boot ID, PID start time, invocation ID, inode identity and cgroup path
can all be recorded immutably. They do not give `cgroup.kill` an atomic
expected-invocation comparison. A pre-write identity check remains racy.

The source-backed counterexample also applies to default control-group cleanup:
systemd can enter failed state after final SIGKILL timeout with a surviving
D-state task; pruning fails, and later startup can reuse that populated cgroup.
The disposable experiment below uses `KillMode=process` only on its own transient
fixture to reproduce surviving descendants without manufacturing a D-state task.
It is **not** a reproduction of production's exact KillMode configuration.

## Retained-FD investigation

Examined upstream Linux v6.6 (relevant v5.14 kill paths also checked):

- [kernfs inode identity](https://github.com/torvalds/linux/blob/v6.6/fs/kernfs/inode.c):
  original kernfs node pinned in inode; open file retains original node.
- [kernfs write and active-reference handling](https://github.com/torvalds/linux/blob/v6.6/fs/kernfs/file.c):
  removed node rejects writes with `ENODEV`; no new pathname resolution.
- [kernfs removal](https://github.com/torvalds/linux/blob/v6.6/fs/kernfs/dir.c):
  node deactivation/draining prevents later callbacks on removed nodes.
- [cgroup implementation](https://github.com/torvalds/linux/blob/v6.6/kernel/cgroup/cgroup.c):
  `cgroup_kn_lock_live()` pins original cgroup, breaks active protection and
  checks liveness under `cgroup_mutex`; `cgroup_kill_write()` and destruction
  serialize under this mutex. Dead original can return `ENOENT`.
- [cgroup v2 documentation](https://docs.kernel.org/6.6/admin-guide/cgroup-v2.html):
  `cgroup.kill` handles concurrent forks in a subtree. `cgroup.events` describes
  hierarchical population, not invocation ownership. Offset-zero reads needed.

Actual remove/recreate does not retarget original FD. A stale events FD may
return `ENODEV`, not synthetic `populated 0`. A live cgroup's observed
`populated 0` is only a sample, not a barrier against future migrations.
These observations do not establish a universal vendor-kernel certification.

Examined systemd v255 and corresponding v257 paths:

- [v255 cgroup lifecycle](https://github.com/systemd/systemd/blob/v255/src/core/cgroup.c):
  `unit_prune_cgroup()` ignores destruction failure; existing cgroup realization
  is accepted. Populated cgroups can remain tracked.
- [v255 cgroup creation](https://github.com/systemd/systemd/blob/v255/src/shared/cgroup-setup.c):
  `cg_create()` accepts `EEXIST`.
- [v255 service lifecycle](https://github.com/systemd/systemd/blob/v255/src/core/service.c):
  `SERVICE_FINAL_SIGKILL` timeout can enter failed state despite remaining tasks.
  `service_adverse_to_leftover_processes()` does not universally reject startup
  when `SendSIGKILL=yes`. New invocation ID does not require new cgroup object.
- [v257 cgroup](https://github.com/systemd/systemd/blob/v257/src/core/cgroup.c)
  and [v257 service](https://github.com/systemd/systemd/blob/v257/src/core/service.c)
  retain corresponding behavior.

## Disposable-only runbook

This probe is not a watchdog, launcher, scheduler, or production helper. It takes
no target PID, unit, path, trial ID or production configuration. It creates two
random `wd-proof-<uuid>.service` transient fixtures sequentially, restarts them,
and kills only those fixtures. It leaves no installed service. Each fixture has
`RuntimeMaxSec=60`, `TasksMax=64`, `Restart=no`, `TimeoutStopSec=1`.
Root and a writable cgroup v2 systemd environment are required.

On an explicitly disposable Linux host, from this operator checkout:

```bash
python3 -m py_compile tests/check_watchdog_cgroup_reuse.py
sudo python3 tests/check_watchdog_cgroup_reuse.py --disposable-only
```

Never run against production. The `--disposable-only` flag acknowledges fixture
creation/termination, not trial approval. Capture stdout/stderr externally without
overwriting historical evidence. On an unexpected assertion or cleanup error,
stop and inspect the exact printed fixture name; do not use wildcard stops.

Expected observations:

- Removed/recreated: different inode and InvocationID; old kill FD fails
  `ENODEV` or `ENOENT`; replacement remains alive.
- Same live object: same inode, different InvocationID; original main pidfd
  becomes readable and signaling it returns `ESRCH`; replacement survives that
  pidfd attempt. Old kill FD then kills replacement and remaining descendants.
- Quiescence: retained events FD reports `populated 0` or `ENODEV` after removal.
  In this controlled fixture no process migrates out. This proves live-subtree
  cleanup in the fixture, not arbitrary production descendant containment or
  zombie reaping. A replacement pidfd separately proves replacement exit.

The same-object case deterministically puts replacement admission between
original binding and kill action. It is a concrete check/action counterexample,
not a probabilistic timing test. The probe's PASS means the unsafe design was
successfully disproved; it does not mean watchdog readiness.

## Actual local evidence

WSL Ubuntu, kernel `6.18.54.1-microsoft-standard-WSL2`,
`systemd 259 (259.5-0ubuntu3.4)`, Python `3.14.4`:

| Case | Original / replacement inode | Original / replacement PID | Result |
|---|---|---|---|
| Removed/recreated | 5506 / 5578 | 1370 / 1375 | old FD errno 19; replacement alive |
| Same live object | 5650 / 5650 | 1385 / 1391 | original pidfd harmless; old kill FD kills replacement and subtree |

Same-object invocation IDs:
`19d4cbbd660e4f7fb2f58b3e2bd8f215` and
`230669c7f79641988776f5d527f8c60e`.
The events FD became removed after kill; all live fixture tasks were gone.
Repeated runs use fresh random identities. This is WSL integration evidence,
**not Ubuntu VPS validation**.

Initial probe attempts failed on fixture Python quoting, then on treating
removed events FD as readable. Both diagnostic errors corrected; no production
code existed. No watchdog RED/GREEN implementation cycle is claimed. Tests
exercise OS behavior, not a completed watchdog API.

Baseline: `python3 -m unittest discover -s tests -p 'test_phase2b_timing.py'`:
18 tests passed. Final focused run:
`python3 -m unittest discover -s tests -p 'test_phase2b_*.py'`: 85 tests passed.
`python3 -m py_compile tests/check_watchdog_cgroup_reuse.py` passed.
Invocation without `--disposable-only` refused with exit 1 before service actions.
Existing suites use mocked service boundaries; printed trial metadata is
synthetic, not an executed pilot.

## Scheduling and outstanding acceptance

Unchanged independent deadline: `B+1860`; latest accepted helper entry:
`B+1890`. Requested emergency fallback: `B+1895`, five seconds after that ceiling.
Unchanged RuntimeMax fallback: `A+2040`. With `L=B-A` and `0<=L<=120`,
remaining nominal emergency budget is `145-L`, minimum **25 seconds**.
Existing 30-second stop policy and 45-second stop subprocess timeout cannot be
assumed to fit that worst-case budget. SIGKILL also does not guarantee bounded
exit for D-state tasks. No Linux scheduling-latency guarantee is asserted.
Emergency activation must always report trial FAILURE, never repair a gate.
No emergency scheduler or fail-closed result writer was built or verified.

Not completed: immutable full trial binding, independent watchdog supervision,
watchdog interruption recovery, Hermes-disconnect behavior, explicit numeric-PID
reuse test, real original-alive deadline action, independent-success cancellation,
emergency FAILURE result integration, unrelated-service canary, monotonic emergency
scheduling/readback and VPS disposable tests. Child survival and concurrent forks
are demonstrated only in the unsafe-design fixture. No eight-gate acceptance
claim or full-suite green claim is made.

Full-suite attempt using WSL system Python with `PYTHONPATH=src`:
430 tests, 1 failure, 129 errors, 4 skipped. Missing installed `httpx` and
`websockets` cause import errors; failure
`test_linux_mvp_deploy.DeploymentTests.test_ingest_cli_import_does_not_load_the_paper_engine`
also encounters missing dependency. No dependencies installed for this stdlib-only
probe. Initial infrastructure-focused attempt without `PYTHONPATH=src` showed
errors in `test_large_snapshot_hash_does_not_read_entire_file`,
`test_observe_rejects_or_clears_stop_sentinel`,
`test_old_restart_heartbeat_cannot_become_continuous_run`, and
`test_safe_wal_snapshot_counts_only_new_run`; corrected environment passed all
51 `test_infrastructure_trial.py` tests.

Independent review found cleanup could skip stop/FD closure after a write error.
Offline failing test added first; cleanup now attempts retained-FD kill,
disposable-unit kill/stop/reset and FD closure independently, preserving original
failure. Fixture identity prints before creation. `tests/test_watchdog_probe.py`
passes its injected-error regression. Repeated disposable integration passed:
removed/recreated inodes 5876/5948; same-live inode 6020, PIDs 2300/2306,
InvocationIDs `a14dacb0690c4bb7b883abab74fc2359` and
`1229542277f54777a3a51fe32ce54a89`.

## Unblocking requirement

Whole-tree operation needs a proven, fresh per-invocation cgroup that no
replacement can enter, or lifecycle serialization that prevents replacement
admission until old kill authority is revoked. Both require an independently
reviewed lifecycle change, outside this no-production-configuration-change task.
A pidfd-only fallback is deliberately not shipped: it would meet replacement
isolation while violating mandatory descendant cleanup.

Next VPS work, only after that design is authorized: verify actual kernel/systemd
versions and effective service policy, run disposable replacement/fork/interruption
matrix, verify independent supervision and monotonic timing with measured cleanup
margin, then obtain new independent acceptance. No real fault-injection trial is
authorized by this report.
