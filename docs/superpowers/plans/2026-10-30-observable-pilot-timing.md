# Observable pilot timing implementation plan

Spec: user-approved revised contract in conversation; supersedes blocked draft, not historical evidence. Base 09c719c420dec74e04159dbeb4de3b616192aaa6. Inline implementation, one independent final review, scoped publication only after ACCEPT.

## Contract and choices

Observation 1800 seconds; absolute CLOCK_MONOTONIC deadline baseline+1860; main runtime final activation+2040. Admission requires at least 60 seconds between nominal deadline and runtime expiry (L<=120), and at least 60 seconds until deadline when verified. Deadline helper entry accepted only from deadline through deadline+30 seconds and before runtime expiry, including already-stopped main case. These are usefulness/acceptance thresholds, NOT scheduler guarantees. Actual completed inactive/empty-cgroup shutdown must precede runtime expiry; Result=success and unchanged invocation required. Runtime timeout, truncated observation, late/absent proof, startup failure or failed independent cleanup blocks acceptance.

Launch startup protection: absolute launch+360 watchdog, independent launch-time audit at launch+360+seconds+60+420. Startup ceiling360 is a fail-closed admission limit, not guaranteed time needed by hashing/filesystem work; no operation timeout increased. Startup watchdog stops trial services if no verified handoff; cancel only after baseline deadline proof persisted. Keep watchdog until successful cancel; timer failure blocks observer. Heartbeat and observer finite runtimes cover startup360+seconds+120; heartbeat lifetime uses same absolute monotonic launch deadline. Audit runtime600 covers eight*65 stop/evidence ceilings plus final checks; deadline/startup helper runtime360. Actual late helper exits block post-exit audit, never infer success.

Timer verification: D-Bus numeric JSON effective target, sole OnBootUSec schedule, exact nominal NextElapseUSecMonotonic, accuracy1s, no randomization/calendar/realtime/WakeSystem; active timer and same boot/clock. Effective properties in timing evidence bind launch fingerprint; source manifest/fingerprint binds deterministic contract. Fresh exact fingerprint/code owner approval, no approval generation.

## Checklist

- [x] Task 1: tests/test_phase2b_timing.py RED scheduling tests, implement timer verification/monotonic anchors, useful margin and timing evidence validation in deploy/infrastructure-trial.py. Expected RED: missing scheduling functions; GREEN focused suite.
- [x] Task 2: RED launch/watchdog and real observer/stop/deadline paths; integrate baseline scheduling, finite heartbeat coverage, startup cancel and observed quiescence; update affected old mocks only for new trust boundary. Full1800/truncation, crash/restart failure, expired timer, wrong boot/clock, runtime timeout, lingering PID, independent audit tested.
- [x] Task 3: bind fingerprint contract and new evidence inventory; verify_pilot and final cleanup reject missing timing proof; update runbook exact branch/SHA deployment prerequisites and unchanged eight gates. Relevant suites + Bash/offline systemd checks.
- [ ] Task 4: independent final ACCEPT, resolve significant findings with RED/GREEN, final regressions/diff check, scoped commit/push and exact SHA equality; stop, no deployment.

Pre-flight shared interfaces: Task1 produces arm_timer/start_observation_timing/validate_timing; Task2 consumes them, records observation end and terminal stop evidence; Task3 requires all evidence in frozen pilot bundle. Preserve existing owned positions0..5; append startup timer/service, replace unsafe audit slicing with explicit self-exclusion. Result remains PENDING_REVIEW, never automatic acceptance.

Review focus: watchdog/deadline handoff races; systemd monotonic OnBootUSec representation; actual Result and quiescence time vs runtime expiry; audit stopping own service; incomplete evidence and stale diagnostics compatibility. No services, VPS/SSH, reset-failed, approval generation, pilot/soak, historical edits, trading/risk, Phase3B or merges. Existing unrelated full-suite failures already classified; affected regressions only.
