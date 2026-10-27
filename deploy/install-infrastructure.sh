#!/usr/bin/env bash
# Isolated, Git-object-only infrastructure release. Never starts or boot-enables service.
set -euo pipefail
umask 0022
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "$(git -C "${SOURCE_DIR}" status --porcelain --untracked-files=no)" ]]; then
    echo 'dirty source: commit/review tracked changes before deployment' >&2
    exit 1
fi
SHA="$(git -C "${SOURCE_DIR}" rev-parse HEAD)"
APP_DIR="/opt/polymarket-btc-infrastructure/releases/${SHA}"
CONF_DIR=/etc/polymarket-btc
DATA_DIR=/var/lib/polymarket-btc-infrastructure
SERVICE_USER=polymarket-btc
UNIT=/etc/systemd/system/polymarket-btc-infrastructure.service
ENABLE=0 # No boot enablement, even on repeat install.
if [[ "${EUID}" -ne 0 ]]; then echo 'run as root (sudo)' >&2; exit 1; fi
command -v uv >/dev/null || { echo 'uv required for frozen hash-verified dependencies' >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3,12)'
if systemctl is-active --quiet polymarket-btc-infrastructure.service; then
    echo 'infrastructure service must be inactive' >&2; exit 1
fi
if systemctl list-units --all --plain --no-legend 'infra-*' | grep -Eq ' (active|activating|deactivating) '; then
    echo 'infrastructure trial jobs still active' >&2; exit 1
fi
if [[ -L "${APP_DIR}" || -e "${APP_DIR}" ]]; then
    echo "release already exists: ${APP_DIR}; verify manifest, do not overwrite" >&2; exit 1
fi
if [[ -L "${CONF_DIR}/infrastructure.env" || -L "${UNIT}" ]]; then
    echo 'managed config/unit must not be symlinks' >&2; exit 1
fi
if ! getent group "${SERVICE_USER}" >/dev/null; then groupadd --system "${SERVICE_USER}"; fi
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --gid "${SERVICE_USER}" --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
if [[ "$(id -u "${SERVICE_USER}")" == 0 ]] || ! getent passwd "${SERVICE_USER}" | grep -Eq ':(/usr/sbin/nologin|/bin/false)$'; then
    echo 'unsafe service account' >&2; exit 1
fi
install -d -m 0755 "${APP_DIR}"
install -d -m 0750 "${CONF_DIR}"
install -d -o root -g "${SERVICE_USER}" -m 0750 "${DATA_DIR}"
install -d -o root -g root -m 0700 "${DATA_DIR}/evidence"
printf '%s' "${SHA}" > "${DATA_DIR}/controller-owner"
chown root:root "${DATA_DIR}/controller-owner"
chmod 0600 "${DATA_DIR}/controller-owner"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${DATA_DIR}/data" "${DATA_DIR}/data/state"
runuser -u "${SERVICE_USER}" -- test -x "${DATA_DIR}/data/state"
# Archive extracts committed blobs only. Existing checkout extras never enter release.
git -C "${SOURCE_DIR}" archive "${SHA}" src deploy pyproject.toml uv.lock | tar -x -C "${APP_DIR}"
python3 -m venv --copies "${APP_DIR}/.venv"
uv export --project "${APP_DIR}" --frozen --no-dev --no-emit-project --format requirements-txt --output-file "${APP_DIR}/requirements.txt" >/dev/null
uv pip sync --link-mode=copy --python "${APP_DIR}/.venv/bin/python" --require-hashes "${APP_DIR}/requirements.txt"
find "${APP_DIR}" -type d -name __pycache__ -prune -exec rm -r -- {} +
# Manifest covers committed source AND installed dependency/interpreter bytes.
PYTHONDONTWRITEBYTECODE=1 python3 - "${APP_DIR}" "${SHA}" "${SOURCE_DIR}" <<'PY'
import hashlib, json, pathlib, subprocess, sys
root, sha = pathlib.Path(sys.argv[1]), sys.argv[2]
files, aliases = {}, {}
for path in sorted(root.rglob('*')):
    name = path.relative_to(root).as_posix()
    if path.is_symlink():
        if name != '.venv/lib64' or path.readlink().as_posix() != 'lib':
            raise SystemExit('unexpected runtime symlink: ' + name)
        aliases[name] = 'lib'
        continue
    if path.is_file():
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if name.startswith(('src/', 'deploy/')) or name in ('pyproject.toml', 'uv.lock'):
            blob = subprocess.check_output(['git', '-C', sys.argv[3], 'show', sha + ':' + name])
            if hashlib.sha256(blob).hexdigest() != files[name]:
                raise SystemExit('installed source differs from Git: ' + name)
# venv copies executable, but base stdlib/extensions stay external. Bind their bytes too.
import sysconfig
roots = sorted({sysconfig.get_path('stdlib'), sysconfig.get_config_var('DESTSHARED')})
host = {}
for folder in roots:
    for path in sorted(pathlib.Path(folder).rglob('*')):
        if any(part in ('site-packages', 'dist-packages') for part in path.parts):
            continue
        if path.is_file():
            with path.open('rb') as stream:
                host[str(path)] = hashlib.file_digest(stream, 'sha256').hexdigest()
(root / 'runtime-manifest.json').write_text(json.dumps({'git_sha': sha, 'files': files, 'symlinks': aliases,
    'host_python': {'roots': roots, 'files': host}}, sort_keys=True, indent=2))
PY
if [[ ! -e "${CONF_DIR}/infrastructure.env" ]]; then
    install -m 0640 -o root -g "${SERVICE_USER}" "${APP_DIR}/deploy/polymarket-btc-infrastructure.env.example" "${CONF_DIR}/infrastructure.env"
fi
# Preserve previous effective fragment before replacing; drop-ins remain for explicit review.
if [[ -e "${UNIT}" ]]; then cp -a "${UNIT}" "${UNIT}.pre-${SHA}-$(date -u +%Y%m%dT%H%M%SZ)"; fi
sed "s|/opt/polymarket-btc|${APP_DIR}|g" "${APP_DIR}/deploy/polymarket-btc-infrastructure.service" > "${UNIT}"
chmod 0644 "${UNIT}"
systemctl daemon-reload
systemd-analyze verify "${UNIT}"
echo "Installed ${SHA} at ${APP_DIR}; not started. Review infrastructure.env, effective unit/drop-ins and fingerprint before approval."
