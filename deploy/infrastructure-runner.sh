#!/usr/bin/env bash
# Native systemd-run submission; controller sets --property=RemainAfterExit=no.
# INFRA_PREFIX is fixed by trial ownership, not caller input.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${APPROVAL:?reviewed approval path required}"
export PYTHONDONTWRITEBYTECODE=1
exec "${ROOT}/.venv/bin/python" "${ROOT}/deploy/infrastructure-trial.py" launch \
    --phase "${PHASE:-pilot}" --approval "${APPROVAL}" "$@"
