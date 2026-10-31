# Disposable PID namespace probe plan

Scope: diagnostic tooling only, never production watchdog. Existing systemd 255,
util-linux unshare, Python stdlib; no packages or security-policy changes.

- [x] Inspect controller, service hardening and retained-cgroup blocker.
- [x] Confirm WSL environment and isolated branch; baseline cleanup test passes.
- [ ] RED: cleanup continues through command errors; unsafe invocation refuses;
  permission denial is BLOCKED, never root retry.
- [ ] Build one self-contained script. Root coordinator creates random transient
  services as existing service account with empty caps, NNP and source hardening.
  Worker tries plain PID namespace then explicit user+PID namespace creation.
  Permission rejection ends compatibility path, preserving diagnostics.
- [ ] When allowed, worker tests namespace PID 1, orphan reaping, explicit pidfd
  signal delivery, sibling namespace isolation and live descendant cleanup.
  Check util-linux parent SIGTERM forwarding separately; do not assume it works.
- [ ] Bound subprocesses and service runtimes; cleanup only exact generated units;
  independently check hierarchical cgroup emptiness after stop. Keep evidence.
- [ ] Run offline RED/GREEN, WSL permission path and supported namespace tests;
  document skips, not substitute root namespace tests.
- [ ] Independent review and fix all Critical/Important findings.
- [ ] Publish script, tests, runbook, evidence/review summary on separate branch.

Decisions: no arbitrary unit/user/path CLI; worker runs as `polymarket-btc` on
VPS. WSL-only option uses invoking nonroot account because production account
absent; explicit WSL label, never VPS compatibility. Staged code under unique
root-owned `/run/p2b-pidns-*` directory, readable but not writable by worker.
Root creates/stops fixtures only; unshare never runs as host root. User namespace
mapping gives namespace-local root, not host root; AppArmor denial is terminal.
Existing AF_UNIX restriction preserved; use inherited pipes, not network sockets.
No production service read or lifecycle interaction. No historical files changed.
