# Installer audit guard correction

Spec: user's targeted installer request. Branch operator/phase2b-installer-audit-guard from approved69079bbce0c4a02ed1a7f81f3c5128b430841aab, not harness6731490. No VPS/install/trial/STOP/evidence/data operations.

Design: separate stdlib read-only admission helper invoked before installer mutations. Exact historical allowlist initially contains only infra-pilot-20261031T022625Z-audit.service; name must also match pilot/soak UTC audit grammar. Successful active/exited requires complete systemd terminal properties, no job, zero PIDs, empty ControlGroup plus independent recursive cgroup inspection. Enumerate all infrastructure units and jobs, reject every other live service/timer; repeat snapshot admission to detect transitions. Unreadable/missing evidence blocks. Never mutate units. Installer additionally requires approved base ancestry, identical controller/template Git blobs, and no portable harness in release tree. Documentation records no-concurrent-start prerequisite and preservation/restore gates.

- [x] Add focused RED tests exercising real read-only helper via fake systemctl boundary, malformed/missing properties, nested residual PID, pending jobs/timers, read errors and mutation refusal. Add release-source guard tests.
- [x] Minimal helper and installer integration; focused checks GREEN, bash syntax and relevant installer regressions.
- [x] Write Hermes preservation/recovery checklist; independent fresh review; fix material findings; record exact controller/template blob and SHA256.
- Publication: commit/push reviewed separate branch; verify remote; stop. Exact release SHA reported after publication.

Review focus: missing list/show evidence; failed systemctl pipelines; pending jobs and timer transitions; cgroup enumeration errors and nested processes; exact historical name only; admission before chown/marker/config; partial-install recovery without evidence mutation.
