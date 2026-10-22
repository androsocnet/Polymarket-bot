"""Deployment artifacts: Linux controls are present and no live-trading path leaks into the package."""
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'src' / 'polymarket_bot' / 'linux_mvp'

FORBIDDEN_SOURCE_TOKENS = (
    'private_key', 'privatekey', 'api_secret', 'apisecret', 'passphrase', 'py_clob', 'py-clob',
    'eth_account', 'web3', 'sign_order', 'signed_order', '/order', 'wallet',
)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.unit = (ROOT / 'deploy' / 'polymarket-btc-paper.service').read_text(encoding='utf8')
        self.env = (ROOT / 'deploy' / 'polymarket-btc.env.example').read_text(encoding='utf8')
        self.installer = (ROOT / 'deploy' / 'install.sh').read_text(encoding='utf8')
        self.runbook = (ROOT / 'docs' / 'linux-mvp-runbook.md').read_text(encoding='utf8')
        self.unit_lines = self._directives(self.unit)
        self.env_lines = self._directives(self.env)

    @staticmethod
    def _directives(text):
        return tuple(line.strip() for line in text.splitlines()
                     if line.strip() and not line.lstrip().startswith('#'))

    def test_unit_runs_unprivileged_paper_only_service(self):
        for required in (
            'User=polymarket', 'Group=polymarket', 'WorkingDirectory=/opt/polymarket-btc',
            'EnvironmentFile=/etc/polymarket-btc/paper.env', 'Environment=MODE=PAPER_ONLY',
            'Environment=PYTHONPATH=/opt/polymarket-btc/src',
            'ExecStart=/opt/polymarket-btc/.venv/bin/python -m polymarket_bot.linux_mvp ingest',
            'Restart=on-failure', 'RestartPreventExitStatus=75', 'RestartSec=5',
            'KillSignal=SIGTERM', 'TimeoutStopSec=30',
            'StateDirectory=polymarket-btc', 'ReadWritePaths=/var/lib/polymarket-btc',
            'WantedBy=multi-user.target',
        ):
            self.assertIn(required, self.unit_lines)
        self.assertNotIn('User=root', self.unit_lines)

    def test_unit_has_hard_linux_resource_controls(self):
        for required in ('MemoryAccounting=true', 'MemoryMax=512M', 'CPUAccounting=true',
                         'CPUQuota=50%', 'TasksMax=64', 'LimitNOFILE=256'):
            self.assertIn(required, self.unit_lines)

    def test_unit_is_hardened(self):
        for required in ('NoNewPrivileges=true', 'PrivateTmp=true', 'ProtectSystem=strict',
                         'ProtectHome=true', 'RestrictSUIDSGID=true', 'LockPersonality=true',
                         'CapabilityBoundingSet=', 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6'):
            self.assertIn(required, self.unit_lines)

    def test_env_example_is_paper_only_and_secret_free(self):
        for required in ('MODE=PAPER_ONLY',
                         'MVP_DB_PATH=/var/lib/polymarket-btc/paper.sqlite',
                         'MVP_STATE_DIR=/var/lib/polymarket-btc/state',
                         'MVP_POLL_SECONDS=2',
                         'MVP_BACKOFF_BASE_SECONDS=2',
                         'MVP_BACKOFF_MAX_SECONDS=30'):
            self.assertIn(required, self.env_lines)
        lowered = self.env.lower()
        for token in ('private', 'secret', 'passphrase', 'api_key', 'apikey', 'credential'):
            self.assertNotIn(token, lowered)

    def test_installer_is_idempotent_shaped_and_does_not_start_by_default(self):
        self.assertTrue(self.installer.startswith('#!/usr/bin/env bash'))
        for required in ('set -euo pipefail', 'install -d', 'rsync -a --delete',
                         'groupadd --system', '--gid "${SERVICE_USER}"',
                         'systemctl daemon-reload', 'systemd-analyze verify',
                         'if [[ "${ENABLE}" == "1" ]]'):
            self.assertIn(required, self.installer)
        self.assertNotIn('curl ', self.installer)
        self.assertIn('systemctl enable --now', self.installer)

    def test_runbook_documents_stop_recovery_report_and_host_only_verification(self):
        for required in ('STOP', 'systemctl stop', 'report', 'MemoryMax', 'CPUQuota',
                         'UNKNOWN_NOT_INFERRED', 'systemd-analyze verify',
                         'not executed', 'no orders'):
            self.assertIn(required, self.runbook)

    def test_ingest_cli_import_does_not_load_the_paper_engine(self):
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join(
            (str(ROOT / 'src'), str(ROOT), env.get('PYTHONPATH', '')))
        code = ('import sys; import polymarket_bot.linux_mvp.__main__; '
                "raise SystemExit(0 if 'polymarket_bot.linux_mvp.engine' not in sys.modules else 1)")
        result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_package_contains_no_live_trading_surface(self):
        sources = sorted(PACKAGE.glob('*.py'))
        self.assertTrue(sources)
        for path in sources:
            text = path.read_text(encoding='utf8').lower()
            with self.subTest(path=path.name):
                for token in FORBIDDEN_SOURCE_TOKENS:
                    self.assertNotIn(token, text)

    def test_runtime_dependency_surface_is_unchanged(self):
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf8'))
        self.assertEqual(project['project']['dependencies'],
                         ['httpx>=0.28.1', 'websockets>=17.2'])


if __name__ == '__main__':
    unittest.main()
