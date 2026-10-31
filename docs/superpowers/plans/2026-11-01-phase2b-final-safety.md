# Phase 2B final safety corrections

> Execute inline with executing-plans and test-driven-development. User approved bounded controller/template correction.

**Goal:** Close unverified shutdown, actual-instance runtime, and pre-timing startup gaps.
**Architecture:** Persist exclusive unit intent before start. Root-only ExecStartPre registration binds systemd INVOCATION_ID, cgroup, boot, trial and actual runtime policy before ingest executes. Never issue name-based main-unit stops: pin original cgroup directory and process pidfds, recheck systemd identity after pinning, signal only pinned processes, verify quiescence. Same-name restart registration fails closed.
**Spec:** User's Final Safety Gaps request; original eight gates and timing requirements in docs/phase-2b-infrastructure-acceptance.md and docs/phase-2b-transient-failover.md.
**Constraints:** No VPS operations, approvals, live trading, production DB or historical evidence changes. Preserve PAPER_ONLY, sandbox, resource limits and timing: B+1800/B+1860/B+1890/A+2040/T+2280/T+2640; soak seconds+240 runtime.

## Review focus
- PID/cgroup/name reuse between inspection, pidfd acquisition and signaling.
- Observer dies after start submission before any observer-side capture.
- Runtime/drop-in mismatch on actual instance, hook failure and stale intents.
- Deadline/audit/observer/startup cleanup never bypass guarded shutdown.
- Pre-start root hook cannot accept caller-selected unrelated units or overwrite identity.

## Tasks
- [x] 1. RED: mismatch must issue no unit stop, race must not signal replacement. Implement pidfd-scoped shutdown; remove unsafe main-unit stop fallbacks across controller. GREEN focused tests.
- [x] 2. RED: actual-instance runtime missing/wrong blocks admission; identity exists before ingest and before timing. Implement exclusive intent/registration, actual policy/runtime queries, template hook and explicit pilot default runtime. Fingerprint hook/template/runtime. GREEN focused tests.
- [x] 3. RED: startup watchdog with registered original and replacement/canary stops original only; missing/corrupt registration fails closed. Implement startup and audit cleanup from registered identities. GREEN affected suites; disposable WSL actual-controller test.
- [ ] 4. Independent review all changes; resolve significant findings with tests. Run affected suites plus full project suite, record exact failures if environmental. Commit and normal push requested branch; report full SHA and local-only evidence.
