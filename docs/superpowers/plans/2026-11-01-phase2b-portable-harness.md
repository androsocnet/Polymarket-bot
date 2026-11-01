# Portable disposable controller compatibility harness

Approved bounded design: user's Phase 2B request and follow-up approval. Frozen controller 69079bbce0c4a02ed1a7f81f3c5128b430841aab. No VPS activity; publication authorized. Existing linked worktree retained.

- [x] Task 1: Add focused stdlib tests first for source pinning, absolute checkout, compatibility, exact unit allowlist and command isolation. Run RED. Add separate tests/check_phase2b_vps.py, run GREEN.
- [x] Task 2: Add /run-only staging, systemd cleanup timer before worker/payload, external timeout, guarded real controller bootstrap and inert network-isolated units. Verify registration, identity refusal, actual runtime, nested termination, observer death without timing, stale/replacement/canary survival and final cleanup. Run focused tests and WSL disposable integration.
- [ ] Task 3: Document boundaries/commands; verify frozen files unchanged; obtain fresh independent review. Correct material findings with regression checks. Commit/push only reviewed harness files; verify remote SHA and canonical script SHA256; report and stop.

Interfaces: source verifier supplies exact Git bytes and pinned hash to staged bootstrap; bootstrap and cleanup share deterministic allowlist derived only from validated random /run namespace. Systemd cleanup runs independently of worker and survives parent interruption. Controller runtime and registration stay real; only namespace, paths and disposable policy are adapted (production policy needs different user, interpreter and ingest command). No full project suite rerun: explicitly excluded as unnecessary by user.

Review focus: root trust boundaries; Git execution configuration; SIGKILL/interruption; watchdog actually dispatched without observer; cleanup timer race and residue; exact allowlist including startup helpers; no network/credentials/production paths; source pinning before every import; systemd255 versus WSL evidence limits.
