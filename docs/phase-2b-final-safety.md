# Phase 2B final safety correction — local development evidence

Scope: development only. No VPS access, deployment, production data changes, approvals, or acceptance. PAPER_ONLY and eight pilot gates unchanged.

## Behavior

- Main shutdown never calls `systemctl stop` by main-unit name, even after failed identity verification. Registered identity must match timing and current InvocationID. Cgroup directory and process pidfds are opened before final identity check; only pinned processes receive SIGTERM, with bounded SIGKILL escalation. Repeated scans collect descendants while original invocation still matches. Replacements cause fail-closed refusal.
- Template defaults to pilot `RuntimeMaxSec=2040`, zero random runtime extension. Soak still requires separately reviewed phase-specific `RuntimeMaxSec=86640`. Prelaunch and pre-start checks inspect actual instance and reject instance-specific fragments/drop-ins differing from fingerprinted template. Timing checks actual started unit, not `@default`.
- Exclusive root-controlled role intents precede start submission. Root `ExecStartPre` records systemd InvocationID and cgroup before ingest executes. Registration cannot overwrite existing identity. Startup watchdog stops observer before cancelling pending registration under shared lock, then cleans only registered original verify/main invocations. Audit and exceptional deadline/observer cleanup use same registration path.
- Root hook uses systemd `!` prefix to override only service credentials; sandbox and capability limits stay in force. Evidence directory remains root-only; ingest cannot access it.

## Local verification

- Baseline: 108 Phase 2B tests passed before correction.
- Corrected affected suites after review fixes: 105 Phase 2B tests, 51 infrastructure, 10 transient-probe tests passed in WSL. Mocked unit tests run without root.
- `tests/check_phase2b_final_safety.py --disposable-only --wsl-only`: production controller registration/shutdown/startup code with disposable namespace and policy bootstrap. Root registration hook uses `!`, unprivileged service user, empty capabilities, NoNewPrivileges, and filesystem sandbox. No network or database. Actual-instance runtime read; disposable observer receives SIGKILL after start before timing; startup cleanup terminates main and descendant; distinct replacement and canary survive; same-name restart rejected; no disposable active services remain.
- Independent Superpowers review: changes requested; no Critical, three Important findings (empty activating transition, vanished proc entry, missing hook/registration admission). Each Important received failing regression then correction; ordinary-user registration test also fixed. Fresh affected suites pass. No second review claimed.
- Full project verification: dependency-free system Python discovery failed imports; dependency-complete discovery timed out at 120s and 600s. Module-isolated run with existing dependencies: 63/72 modules PASS (1,088 tests reported in passing modules, including skips), three failed modules, six 90-second module timeouts. Full-suite result is NOT GREEN. Unrelated code and historical fixtures were not changed.

### Broad-suite unresolved results

- `tests.test_cp57_arm_serialization_audit.ArmSerializationAuditTests.test_retained_numeric_event_reaches_actual_arm_before_publication`: missing retained `data/cp5_6c_replacement_20261017_0351adeb-6594-492e-9237-adf7d833dba0.sqlite3`.
- `tests.test_live_guard_races`: `test_real_job_kills_child_while_stop_waits_on_blocked_transport`, `test_delayed_assignment_after_child_waits_then_go_succeeds`, `test_premature_go_existing_but_unassigned_job_refuses`, `test_supervisor_delays_assignment_until_actual_child_waits`: `windows_job_required` under WSL.
- `tests.test_recorder_live_clob`: all six tests fail setup fixture hashes: `test_c_alias_does_not_override_gamma_identity`, `test_c_alias_preserves_token_and_outcome_rejection`, `test_captured_responses_acquire_selected_revision`, `test_each_alias_and_matching_combinations_accepted`, `test_malformed_or_conflicting_alias_never_hidden_by_matching_alias`, `test_missing_condition_alias_rejected`. Committed baseline archive with LF fixture bytes passes this module; current checkout hash failures are untouched fixture line endings.
- 90-second timeouts, last running test: `tests.test_full_window.AdversarialAssessmentTests.test_h1_deadline_equality_and_one_microsecond_beyond`; `tests.test_full_window_guard.RecorderIntegrationTests.test_real450_guarded_targets_assessor_and_replay_unchanged`; `tests.test_paper.CausalClockTests.test_one_anchor_missing_opportunity_blocks_both_tokens`; `tests.test_recorder_remediation.RemediationTests.test_stop_between_single_fallbacks_preserves_completed_attempts`; `tests.test_recorder_selected.SelectedTests.test_hostile_metadata_change_during_refresh_blocks`; `tests.test_recorder_service.RecorderServiceTests.test_actual_book_guard_stops_with_provider_transport_pending`.

Baseline archive also reproduces missing retained DB and Windows job errors; additional baseline CP57 errors arise because archive omitted `deploy/` and are not attributed to correction. No broad-suite pass claimed.

## Limits

Disposable WSL uses systemd 259, not target VPS systemd 255. Does not establish VPS sandbox, hook, timer scheduling, ingestion, database, approval, or 30-minute acceptance. Separately authorized VPS validation must verify deployed immutable release, template/drop-ins, root hook access under sandbox, exact fingerprint/approval, actual phase runtime, and all eight gates. No launch authorization included.
