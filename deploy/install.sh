#!/usr/bin/env bash
# Idempotent installer for the PAPER_ONLY service. It never creates a live-trading path.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/polymarket-btc}"
DATA_DIR="${DATA_DIR:-/var/lib/polymarket-btc}"
CONF_DIR="${CONF_DIR:-/etc/polymarket-btc}"
SERVICE_USER="${SERVICE_USER:-polymarket}"
SERVICE_NAME="${SERVICE_NAME:-polymarket-btc-paper}"
ENABLE="${ENABLE:-0}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root (sudo)" >&2
  exit 1
fi
if ! getent group "${SERVICE_USER}" >/dev/null 2>&1; then
  groupadd --system "${SERVICE_USER}"
fi
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --gid "${SERVICE_USER}" --create-home --home-dir "${APP_DIR}" \
    --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -m 0755 "${APP_DIR}"
install -d -m 0750 "${CONF_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${DATA_DIR}" "${DATA_DIR}/state"

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required" >&2
  exit 1
fi
if [[ "$(readlink -f "${SOURCE_DIR}")" != "$(readlink -f "${APP_DIR}")" ]]; then
  rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude 'data' --exclude 'logs' \
    --exclude '__pycache__' --exclude '.pytest_cache' \
    "${SOURCE_DIR}/" "${APP_DIR}/"
fi
chown -R root:root "${APP_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${DATA_DIR}" "${DATA_DIR}/state"

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"${APP_DIR}/.venv/bin/python" -m pip install --disable-pip-version-check "httpx>=0.28.1"

if [[ ! -f "${CONF_DIR}/paper.env" ]]; then
  install -m 0600 "${APP_DIR}/deploy/polymarket-btc.env.example" "${CONF_DIR}/paper.env"
  echo "installed default ${CONF_DIR}/paper.env; review it before starting"
fi

install -m 0644 "${APP_DIR}/deploy/polymarket-btc-paper.service" \
  "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "/etc/systemd/system/${SERVICE_NAME}.service"
fi

if [[ "${ENABLE}" == "1" ]]; then
  systemctl enable --now "${SERVICE_NAME}"
  systemctl --no-pager status "${SERVICE_NAME}" || true
else
  echo "installed. Review ${CONF_DIR}/paper.env, then run:"
  echo "  systemctl enable --now ${SERVICE_NAME}"
fi
