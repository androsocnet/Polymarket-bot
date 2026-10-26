"""Native Ubuntu infrastructure deploy/runtime artifacts.

Offline, no systemctl/WSL/Windows required. Validates repository artifacts
that must remain Windows-independent and must never disturb the existing
paper service/database.
"""
import ast
import re
import tomllib
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
REPO_DEPLOY = ROOT / 'deploy'
REPO_SRC = ROOT / 'src' / 'polymarket_bot'

# Tokens forbidden in native infrastructure runtime paths.
WIN_TOKENS = ('powershell', '/mnt/c/', 'C:\\Users', 'C:\\Program Files',
              'phase2b-guard-tests.py', 'phase2b-final-234452-host.ps1')


class InfrastructureArtifactTests(unittest.TestCase):
    def setUp(self):
        self.heartbeat = (REPO_DEPLOY / 'infrastructure-host-heartbeat.sh').read_text(encoding='utf8')
        self.installer = (REPO_DEPLOY / 'install-infrastructure.sh').read_text(encoding='utf8')
        self.unit = (REPO_DEPLOY / 'polymarket-btc-infrastructure.service').read_text(encoding='utf8')
        self.env = (REPO_DEPLOY / 'polymarket-btc-infrastructure.env.example').read_text(encoding='utf8')
        self.runner = (REPO_DEPLOY / 'infrastructure-runner.sh').read_text(encoding='utf8')

    # --- native heartbeat (no Windows/WSL) ---
    def test_heartbeat_has_no_windows_dependencies(self):
        lowered = self.heartbeat.lower()
        for token in WIN_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_heartbeat_reads_native_clocks_and_boot(self):
        self.assertIn('/proc/sys/kernel/random/boot_id', self.heartbeat)
        self.assertIn('CLOCK_BOOTTIME', self.heartbeat)
        self.assertIn('CLOCK_MONOTONIC', self.heartbeat)
        self.assertIn('date +%s', self.heartbeat)

    def test_heartbeat_writes_current_and_jsonl(self):
        self.assertIn('.json.tmp', self.heartbeat)
        self.assertIn('.jsonl', self.heartbeat)
        self.assertIn('mv -f', self.heartbeat)

    def test_heartbeat_detects_continuity_regressions(self):
        self.assertIn('seq', self.heartbeat)
        self.assertIn('boot', self.heartbeat)
        self.assertIn('interrupted', self.heartbeat)

    def test_heartbeat_script_is_valid_bash(self):
        import subprocess
        result = subprocess.run(['bash', '-n', str(REPO_DEPLOY / 'infrastructure-host-heartbeat.sh')],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    # --- runner is SSH-independent and survives disconnects ---
    def test_runner_submits_detached_systemd_jobs(self):
        for token in ('systemd-run', '--unit', '--remain-after-exit=no'):
            self.assertIn(token, self.runner)

    def test_runner_does_not_hold_ssh_session(self):
        lowered = self.runner.lower()
        self.assertNotIn('&disown', lowered)
        self.assertNotIn('nohup', lowered)
        # no blocking tail/journalctl that would keep SSH open
        self.assertNotIn('tail -f', self.runner)

    def test_runner_uses_owned_infra_jobs_only(self):
        self.assertIn('INFRA_PREFIX', self.runner)

    # --- installer is safe, idempotent, isolated ---
    def test_installer_uses_separate_infra_state(self):
        lowered = self.installer.lower()
        self.assertIn('infrastructure', lowered)
        self.assertNotIn('rsync -a --delete', self.installer)
        self.assertNotIn('chown -R', self.installer)

    def test_installer_never_touches_paper_service_or_db(self):
        self.assertNotIn('polymarket-btc-paper.service', self.installer)
        self.assertNotIn('paper.env', self.installer)

    def test_installer_is_idempotent_and_refuses_start(self):
        self.assertIn('set -euo pipefail', self.installer)
        self.assertIn('install -d', self.installer)
        self.assertIn('systemd-analyze verify', self.installer)
        self.assertIn('ENABLE', self.installer)
        self.assertNotIn('systemctl enable --now', self.installer)

    def test_installer_refuses_non_root(self):
        self.assertIn('EUID', self.installer)

    def test_installer_uses_dedicated_account_and_unit(self):
        for token in ('polymarket-btc', 'polymarket-btc-infrastructure.service',
                      'infrastructure.env'):
            self.assertIn(token, self.installer)

    def test_installer_does_not_apt_or_curl(self):
        lowered = self.installer.lower()
        self.assertNotIn('apt-get', lowered)
        self.assertNotIn('curl ', lowered)
        self.assertNotIn('curl$', lowered)

    def test_installer_allows_paper_coexistence(self):
        """Installer must not block existing paper.env; only infra conflicts."""
        # Broad glob *paper* would match paper.env and block coexistence.
        self.assertNotIn('*paper*', self.installer)
        # Must check for infrastructure-specific conflict only.
        self.assertIn('infrastructure.env', self.installer)

    def test_installer_shell_syntax_ok(self):
        import subprocess
        result = subprocess.run(['bash', '-n', str(REPO_DEPLOY / 'install-infrastructure.sh')],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    # --- infrastructure unit hardening ---
    def test_unit_is_ingest_only_paper_only(self):
        for token in ('MODE=PAPER_ONLY', 'ingest', 'Restart=no'):
            self.assertIn(token, self.unit)
        self.assertNotIn('run', self.unit.split('ExecStart=',1)[1].split('\n')[0])

    def test_unit_no_boot_enable_target(self):
        self.assertIn('[Install]', self.unit)
        # No WantedBy=multi-user.target so it never auto-starts at boot.
        self.assertNotIn('multi-user.target', self.unit)

    def test_unit_has_hardening(self):
        for token in ('User=polymarket-btc', 'Group=polymarket-btc',
                      'NoNewPrivileges=true', 'ProtectSystem=strict',
                      'MemoryMax=512M', 'CPUQuota=50%', 'TasksMax=64',
                      'ReadWritePaths=/var/lib/polymarket-btc',
                      'EnvironmentFile=/etc/polymarket-btc/infrastructure.env'):
            self.assertIn(token, self.unit)

    # --- env example is isolated and secret-free ---
    def test_env_example_isolated_paths(self):
        self.assertIn('MVP_DB_PATH=/var/lib/polymarket-btc/infrastructure.sqlite', self.env)
        self.assertIn('MVP_STATE_DIR=/var/lib/polymarket-btc/infrastructure/state', self.env)
        self.assertIn('MODE=PAPER_ONLY', self.env)

    def test_env_no_secrets(self):
        lowered = self.env.lower()
        for token in ('private', 'secret', 'passphrase', 'api_key', 'apikey', 'credential'):
            self.assertNotIn(token, lowered)


class InfrastructureObserverTests(unittest.TestCase):
    """Observer must be native (no Windows/WSL) and reuse safe primitives."""

    def test_observer_source_no_windows_tokens(self):
        src = (REPO_DEPLOY / 'paper-validation.py').read_text(encoding='utf8').lower()
        for token in WIN_TOKENS:
            self.assertNotIn(token.lower(), src)

    def test_observer_has_native_clock_and_boot(self):
        src = (REPO_DEPLOY / 'paper-validation.py').read_text(encoding='utf8')
        self.assertIn('/proc/sys/kernel/random/boot_id', src)
        # clock() should use monotonic and wall together.
        self.assertIn('time.monotonic', src)


if __name__ == '__main__':
    unittest.main()
