#!/usr/bin/env bash
# Idempotent installer for the INFRASTRUCTURE-only service.
# Does NOT touch the existing paper service, paper database, or paper config.
# Does NOT start the service or enable it at boot.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/polymarket-btc}"
DATA_DIR="${DATA_DIR:-/var/lib/polymarket-btc}"
CONF_DIR="${CONF_DIR:-/etc/polymarket-btc}"
SERVICE_USER="${SERVICE_USER:-polymarket-btc}"
SERVICE_NAME="${SERVICE_NAME:-polymarket-btc-infrastructure}"
ENABLE="${ENABLE:-0}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
    echo "run as root (sudo)" >&2
    exit 1
fi

# Refuse to overwrite existing paper configuration.
if ls "${CONF_DIR}"/*paper* >/dev/null 2>&1; then
    echo "existing paper configuration in ${CONF_DIR}; infrastructure uses infrastructure.env. Aborting." >&2
    exit 1
fi

# Create dedicated system account (nologin, unprivileged).
if ! getent group "${SERVICE_USER}" >/dev/null 2>&1; then
    groupadd --system "${SERVICE_USER}"
fi
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --gid "${SERVICE_USER}" --create-home --home-dir "${APP_DIR}" \
        --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

# Isolated infrastructure state directory (NOT shared with paper).
INFRA_DATA_DIR="${DATA_DIR}/infrastructure"
install -d -m 0755 "${APP_DIR}"
install -d -m 0750 "${CONF_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${INFRA_DATA_DIR}" "${INFRA_DATA_DIR}/state"

# Install tracked application files (no rsync --delete, no recursive chown).
mkdir -p "${APP_DIR}/src/polymarket_bot"
mkdir -p "${APP_DIR}/deploy"
cp -a "${SOURCE_DIR}/src/polymarket_bot" "${APP_DIR}/src/"
cp -a "${SOURCE_DIR}/tests" "${APP_DIR}/tests" 2>/dev/null || true

# Copy deployment scripts (idempotent, overwrites only our own files).
for f in install-infrastructure.sh infrastructure-host-heartbeat.sh \
         infrastructure-runner.sh polymarket-btc-infrastructure.service \
         polymarket-btc-infrastructure.env.example paper-validation.py; do
    if [[ -f "${SOURCE_DIR}/deploy/${f}" ]]; then
        cp -a "${SOURCE_DIR}/deploy/${f}" "${APP_DIR}/deploy/"
    fi
done

# Python virtualenv (requires python3 and python3-venv on the target).
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
    "${PYTHON_BIN}" -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/pip" install --upgrade pip >/dev/null
# Install only required project dependencies via pyproject if present.
if [[ -f "${SOURCE_DIR}/pyproject.toml" ]]; then
    "${APP_DIR}/.venv/bin/pip" install -e "${SOURCE_DIR}[paper]" 2>/dev/null || \
    "${APP_DIR}/.venv/bin/pip" install -e "${SOURCE_DIR}"
fi

# Install infrastructure environment file (example -> concrete, owner must review).
install -m 0640 -o root -g "${SERVICE_USER}" \
    "${SOURCE_DIR}/deploy/polymarket-btc-infrastructure.env.example" \
    "${CONF_DIR}/infrastructure.env"

# Install systemd unit (managed by this installer only).
install -m 0644 -o root -g root \
    "${SOURCE_DIR}/deploy/polymarket-btc-infrastructure.service" \
    "/etc/systemd/system/${SERVICE_NAME}.service"

# Verify unit syntax before any activation.
systemctl daemon-reload
systemd-analyze verify "/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "${ENABLE}" == "1" ]]; then
    systemctl enable "${SERVICE_NAME}.service"
fi

echo "Infrastructure installed at ${APP_DIR} (not started). Review ${CONF_DIR}/infrastructure.env."
