# Disposable VPS controller compatibility harness

Development only; no target execution yet. Frozen approved controller: `69079bbce0c4a02ed1a7f81f3c5128b430841aab`. Harness commit is separate. No deployment, approvals, pilot, soak, trading, production DB or credentials. Original eight gates and historical evidence unchanged.

## Execution boundary

`tests/check_phase2b_vps.py` requires root, `/usr/bin/python3`, explicit absolute `--checkout`, and `--disposable-only`. Target must be non-WSL Ubuntu 24.04/systemd 255 with unified cgroup v2 and pidfd support. `--wsl-development` is separate explicit local evidence mode, not target acceptance. Existing WSL-only harness/guard remains untouched.

Source comes from `git show 69079bbce0c4a02ed1a7f81f3c5128b430841aab:deploy/infrastructure-trial.py`, not working-tree or installed controller. Raw bytes must match SHA256 `43a75dad91210a47ec92972fe7c2881d5f0b37f79cf0edcaccfcf3560c6d6799` before import; every staged import rechecks that hash. Git environment/config overrides and replace objects disabled. Root must trust checkout and harness; use root-owned checkout without group/other write permissions. Root execution of untrusted harness is never safe.

All new files under unique `/run/p2b-vps-<32hex>` and exact `/run/systemd/system/p2b-vps-<32hex>...` units. No wildcard stop, installed production service edits or reload of production configuration files. `daemon-reload` necessarily refreshes systemd manager configuration globally, without restarting production services. Disposable payload is shell/sleep as nobody, private network, empty capability set, read-only filesystem sandbox except root-owned staging. No ingest code runs.

Independent systemd cleanup timer uses fixed monotonic `OnBootSec` deadline 150 seconds after staging; `daemon-reload` cannot extend it. Timer arms before worker/payload; worker has external GNU `timeout` 120 seconds plus kill-after 10 and systemd runtime cap 130. Launcher should also run under external 240-second timeout. Cleanup stops worker first, then only exact allowlisted units; verifies terminal state and no cgroup, removes definitions/staging and disarms safeguard last. Signal/failure path invokes independent cleanup service. SIGKILL bypasses launcher finally; timer still owns cleanup. Isolation, evidence or cleanup failure means BLOCKED, never PASS. Host crash/power loss cannot guarantee immediate execution; `/run` is ephemeral and units have runtime bounds. Systemd dispatch requires functioning manager.

## Verified behaviors and limits

Real frozen `prepare_invocation`, root `register_invocation`, `registered_identity`, runtime D-Bus parser, cancellation, `startup_timeout` and pidfd shutdown execute unchanged. Harness adapts namespace/evidence paths and exact disposable helper list. Production admission requires installed venv, production user/env and ingest; harness substitutes exact inert hook admission. This is not production policy parser or full observer/timing/ingestion/DB acceptance.

Checks: durable invocation equals actual systemd ID/cgroup; actual-instance runtime is 2040 seconds with no random extension; missing and corrupt identity produce refusal without killing original; actual observer service is SIGKILLed before timing; independent shortened disposable startup timer calls real startup cleanup; root probe moves child into nested cgroup and pins original pidfds before shutdown; original main/children exit; separate canaries survive. Cancelled same-name restart must fail. Within disposable evidence only, harness resets registration to create same-name replacement; stale original shutdown must refuse and replacement/canaries survive. Production timing constants unchanged; short timer is synthetic test dispatch, not production latency proof. Runtime property enforcement/admission verified, not a 2040-second elapsed expiry run.

Frozen controller sometimes conservatively reports identity change after original already deactivated. Only that exact additional startup error may be accepted after all original pidfds report exit, original cgroup empty and main unit terminal with zero MainPID/ControlPID. Other errors block. This does not change controller or certify its original cleanup return as error-free.

## Local checks

```
wsl -d Ubuntu -- /usr/bin/python3 -m unittest tests.test_phase2b_vps_harness
```

Root disposable WSL integration uses separate root-owned Linux Git clone (Windows linked worktree Git metadata is not directly portable). No full project suite rerun: controller and existing harness unchanged. Final local results/review recorded with publication report.

## Later Hermes command

After separately authorized VPS execution, with trusted root-owned checkout at explicit absolute path (substitute only that path), verify harness commit and script SHA256 from publication report first:

```sh
sudo /usr/bin/timeout --signal=TERM --kill-after=20 240 /usr/bin/python3 -I -B /ABSOLUTE/CHECKOUT/tests/check_phase2b_vps.py --disposable-only --checkout /ABSOLUTE/CHECKOUT
```

Do not add `--wsl-development` on VPS. JSON PASS means disposable compatibility checks only, not original eight-gate acceptance or launch authority. Any nonzero exit, BLOCKED, timeout, missing report or leftover is failure; stop, retain logs, do not launch trial or manually broaden cleanup scope.
