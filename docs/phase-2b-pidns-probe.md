# Phase 2B — disposable PID namespace compatibility

Scope: compatibility probe only. No bot imports, trading, wallet, production unit
queries/actions, deployment, systemd upgrade, profile transition, AppArmor/sysctl
change, or installed package. Deployed release remains
`100dc27059f9ed96aa0a7239aef888e21192426c`.

VPS facts supplied by Hermes/user: Ubuntu 24.04.4, systemd255
255.4-1ubuntu8.17, Linux6.8.0-138-generic, Python3.12.3, cgroup v2;
AppArmor active, restrict_unprivileged_userns=1, unprivileged_userns_clone=1.
Actual namespace compatibility remains **UNVERIFIED** until Hermes runs probe.

## Design and boundaries

`tests/check_pidns_compat.py` is self-contained Python stdlib. Root coordinator
stages its own bytes under fresh `/run/p2b-pidns-<uuid>` and creates two transient
`p2b-pidns-<uuid>-probe.service` / `-canary.service` units. It never accepts a
caller-selected target or host-root namespace execution. Code/log evidence kept
under printed directory; do not overwrite old evidence. `/run` is reboot-volatile;
export evidence before reboot.

All namespace operations run inside system-manager service as existing
`polymarket-btc`, with NoNewPrivileges=yes, empty capability bounding set,
RestrictNamespaces=no, KillMode=control-group, Restart=no, source service
filesystem hardening, address-family restriction, TasksMax32, MemoryMax128M,
CPUQuota50%. No production EnvironmentFile, DB/state path or application code.
RuntimeMaxSec60 and TimeoutStopSec3 are disposable safety ceilings, not proposed
production policy. RemainAfterExit preserves terminal properties until cleanup.
Root only stages code, creates fixtures and cleans exact fixture units.

First attempt: `unshare --pid --fork --mount-proc`, without host CAP_SYS_ADMIN.
Failure and stderr retained. Second explicit route, only after EPERM:
`unshare --user --map-root-user --pid --fork --mount-proc` under same service
restrictions. Namespace UID0 maps to exactly existing host service UID/GID; this
is namespace-local authority, not host-root retry. AppArmor may deny either user
namespace creation or subsequent capability use. Such denial is BLOCKED; never
retry as root, `aa-exec`, disable policy, adjust sysctl, or grant capabilities.
Bare EPERM cannot prove AppArmor caused failure; preserve exact error and audit
evidence if available through approved read-only access.

When creation succeeds, toy PID1 asserts namespace identity, reaps intermediate
and adopted orphan with explicit exit statuses, then forwards SIGTERM to its
ready child. Outer unprivileged monitor pins init/child using pidfds after
readiness. Targets stay alive during binding. An inherited original `/proc`
directory FD supplies outer-visible PID, not a numeric FD transfer. This is
controlled-fixture binding, not authenticated production pidfd handoff.

Probe separately characterizes util-linux waiting parent's SIGTERM behavior.
Older util-linux does not forward it. If parent does not forward, direct original
init pidfd delivery is tested and reported; no claim unshare alone is a working
supervisor. Python PID1 supplies explicit signal handling/reaping only for toys.

Two sibling PID namespaces deliberately coexist in same disposable service
cgroup. Killing original init through retained pidfd must also terminate its live
child; sibling init/child must survive, including second stale-original pidfd
signal attempt. Separate canary service must remain active with unchanged PID.
This approximates replacement isolation, **not real systemd restart/cgroup reuse**,
not forced numeric PID reuse, concurrent-fork stress, production deadline behavior,
or complete watchdog acceptance. No emergency watchdog is supplied.

Worker records PASS before root cleanup, only after pidfd exit proofs. Coordinator
requires no live worker-cgroup tasks before cleanup, then stops exact fixtures and
verifies hierarchical cgroup emptiness plus terminal state. This separates namespace
cleanup proof from systemd safety cleanup. Signal/command exceptions must remain
FAIL; cleanup errors prohibit success. SIGKILL of coordinator cannot run finally;
independent RuntimeMax and control-group cleanup still bound fixture lifetime,
subject to Linux dispatch/D-state limitations. Retained exited units may require
explicit printed-name cleanup if coordinator itself is killed.

## Hermes execution

Use separate operator checkout, never `/opt` deployed release or installer.
Fetch published branch and verify supplied full commit before executing. Do not
merge branch into production. No packages needed. If account/tools missing, stop.

From verified checkout:

```bash
sha256sum tests/check_pidns_compat.py
python3 -m unittest discover -s tests -p test_pidns_probe.py
sudo /usr/bin/python3 -I -B tests/check_pidns_compat.py --disposable-only
```

Do **not** pass `--wsl-only` on VPS. That option asserts Microsoft kernel and uses
explicit nonroot SUDO_USER for WSL because production account absent locally.
It cannot certify VPS permissions or AppArmor.

Capture complete stdout/stderr and shell exit code as new evidence. Expected:
exit0 candidate PASS, exit2 permission BLOCKED, exit1 FAIL. Before accepting:

1. Verify worker host UID/GID match `polymarket-btc`; pre-unshare CapEff/CapPrm/
   CapBnd all zero, NoNewPrivs1, AppArmor label and effective hardening recorded.
2. Review every namespace-denied record, return code and stderr. Both routes
   denied means BLOCKED; remaining namespace tests not executed.
3. If allowed, verify namespace PID1, distinct PID/user namespace IDs, exact
   single-ID maps, orphan_reaped, explicit signal result, teardown and surviving
   sibling results. `util_linux_parent_forwarded=false` is material design limit.
4. Verify canary unchanged before teardown, worker has no live descendants before
   coordinator cleanup, cleanup_errors empty, exact fixture final states inactive/
   not-found with MainPID=ControlPID0 and empty hierarchical cgroups.
5. Preserve printed evidence directory/logs, invocation output, systemd/kernel/
   util-linux versions and relevant AppArmor denials if available. No security
   adjustment if evidence incomplete. Report BLOCKED/FAIL, never infer success.

If interrupted/error, inspect only exact printed fixture names and directory.
Never use wildcard kill/stop. Do not manually act on production unit. If cleanup
report shows residual tasks or unavailable state, return evidence for review;
compatibility not established.

## WSL evidence

WSL kernel6.18.54.1-microsoft-standard-WSL2, systemd259, util-linux2.41.3,
Python3.14.4, nonroot account androunix UID1000. AppArmor disabled locally;
VPS AppArmor behavior **not simulated or certified**.

Run `22478383e2344854ada1839ec266bd52`: plain namespace EPERM;
unprivileged user+PID route succeeded under sandbox. PID1 orphan reaping and
explicit forwarding passed. util-linux parent did not forward SIGTERM. Original
PID1/child3111/3114 died; sibling3116/3120 survived original kill and stale pidfd
attempt. Canary3091 untouched. Cleanup errors empty. WSL evidence is not VPS
validation; systemd255/util-linux installed VPS versions remain runtime test.

Offline tests: missing probe RED observed, then safety suite 8 PASS. Independent
review ACCEPT for disposable probe after addressing privilege and cgroup path
findings. Final additional test results recorded separately before publication.
