#!/usr/bin/env bash
# SSH-independent infrastructure test launcher.
# Submits bounded systemd jobs detached from the SSH session.
set -euo pipefail

INFRA_PREFIX="${INFRA_PREFIX:-infra}"
INFRA_UNIT="${INFRA_UNIT:-polymarket-btc-infrastructure.service}"
EVIDENCE_DIR="${EVIDENCE_DIR:-/var/lib/polymarket-btc/phase2b-evidence}"
PYTHON_BIN="${PYTHON_BIN:-/opt/polymarket-btc/.venv/bin/python}"
OBSERVER="${OBSERVER:-/opt/polymarket-btc/deploy/paper-validation.py}"
PHASE="${PHASE:-pilot}"
SECONDS_PARAM="${SECONDS_PARAM:-180}"

case "${PHASE}" in
    pilot|soak) ;;
    *) echo "PHASE must be pilot or soak"; exit 1 ;;
esac

SUFFIX="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${EVIDENCE_DIR}/${PHASE}-${SUFFIX}"

# Native heartbeat monitor (independent of observer).
HB_OUTPUT="${EVIDENCE_DIR}/${PHASE}-${SUFFIX}/heartbeat.json"
systemd-run --unit "${INFRA_PREFIX}-${PHASE}-heartbeat-${SUFFIX}" \
    --remain-after-exit=no \
    --property=Restart=no \
    --property=MemoryMax=128M \
    --property=CPUQuota=25% \
    /opt/polymarket-btc/deploy/infrastructure-host-heartbeat.sh "${HB_OUTPUT}" "${SECONDS_PARAM}"

sleep 3
if [[ ! -f "${HB_OUTPUT}" ]]; then
    echo "ERROR: heartbeat did not produce evidence at ${HB_OUTPUT}" >&2
    exit 1
fi

# Deadline timer: stops only the infrastructure unit and owned infra jobs.
DEADLINE_SECONDS=$(( SECONDS_PARAM + 120 ))
DEADLINE_DIR="${EVIDENCE_DIR}/${PHASE}-${SUFFIX}/deadline"
mkdir -p "${DEADLINE_DIR}"
systemd-run --unit "${INFRA_PREFIX}-${PHASE}-deadline-${SUFFIX}" \
    --remain-after-exit=no \
    --on-active="${DEADLINE_SECONDS}s" \
    --timer-property=AccuracySec=1s \
    --timer-property=RandomizedDelaySec=0 \
    --property=Type=oneshot \
    --property=TimeoutStartSec=60 \
    /bin/bash -c "systemctl stop ${INFRA_UNIT}; systemctl show ${INFRA_UNIT} -p ActiveState,MainPID,NRestarts > ${DEADLINE_DIR}/final.txt"

# Observer runs independently; survives SSH disconnect.
systemd-run --unit "${INFRA_PREFIX}-${PHASE}-observer-${SUFFIX}" \
    --remain-after-exit=no \
    --property=Restart=no \
    --property=MemoryMax=256M \
    --property=CPUQuota=50% \
    "${PYTHON_BIN}" "${OBSERVER}" "${PHASE}" \
        --seconds "${SECONDS_PARAM}" \
        --output "${EVIDENCE_DIR}/${PHASE}-${SUFFIX}" \
        --host-heartbeat "${HB_OUTPUT}" \
        --unit "${INFRA_UNIT}"

echo "Launched ${PHASE} infrastructure test. Evidence: ${EVIDENCE_DIR}/${PHASE}-${SUFFIX}"
echo "Units: ${INFRA_PREFIX}-${PHASE}-heartbeat-${SUFFIX}, ${INFRA_PREFIX}-${PHASE}-deadline-${SUFFIX}, ${INFRA_PREFIX}-${PHASE}-observer-${SUFFIX}"
echo "Service: ${INFRA_UNIT}"
echo "Runner exited. All processes run independently via systemd."
