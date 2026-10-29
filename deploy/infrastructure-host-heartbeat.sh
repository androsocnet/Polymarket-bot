#!/usr/bin/env bash
# Native Linux host continuity monitor for infrastructure testing.
# Records boot_id, session UUID, increasing sequence, wall time,
# CLOCK_MONOTONIC and CLOCK_BOOTTIME. Independent of observer.
# SSH disconnect is NOT a session change: session = this process lifetime.
set -euo pipefail

OUTPUT_PATH="${1:?usage: infrastructure-host-heartbeat.sh <output.json> <seconds>}"
DEADLINE="${2:?usage: infrastructure-host-heartbeat.sh <output.json> <absolute-monotonic-deadline>}"
python3 -c 'import math,sys; d=float(sys.argv[1]); assert math.isfinite(d)' "$DEADLINE"
INTERVAL=5

BOOT_ID="$(cat /proc/sys/kernel/random/boot_id)"
SESSION_ID="$(cat /proc/sys/kernel/random/uuid)"
SEQ=0
PREV_MONO=""
PREV_BOOT=""
INTERRUPTED=0

write_sample() {
    local seq="$1" wall="$2" mono="$3" boottime="$4" interrupted="$5"
    local now_epoch
    now_epoch=$(date +%s)
    local tmp="${OUTPUT_PATH}.json.tmp"
    python3 -c "
import json, sys
seq=int(sys.argv[1]); wall=float(sys.argv[2]); mono=float(sys.argv[3])
boottime=float(sys.argv[4]); interrupted=int(sys.argv[5]); boot=sys.argv[6]; sid=sys.argv[7]
data = {'session': sid, 'boot': boot, 'sequence': seq, 'wall': wall,
        'mono': mono, 'boottime': boottime, 'interrupted': interrupted}
print(json.dumps(data, sort_keys=True))
" "$seq" "$wall" "$mono" "$boottime" "$interrupted" "$BOOT_ID" "$SESSION_ID" > "$tmp"
    mv -f "$tmp" "$OUTPUT_PATH"
    cat "$OUTPUT_PATH" >> "${OUTPUT_PATH}.jsonl"
}

while true; do
    READING=$(python3 -c "
import time
print(time.time())
print(time.clock_gettime(time.CLOCK_MONOTONIC))
print(time.clock_gettime(time.CLOCK_BOOTTIME))
")
    WALL=$(echo "$READING" | sed -n '1p')
    MONO=$(echo "$READING" | sed -n '2p')
    BOOTTIME=$(echo "$READING" | sed -n '3p')

    SEQ=$((SEQ + 1))

    # Continuity regression checks: boot_id change, sequence regression/gaps,
    # backward clocks, stale heartbeat, suspend discrepancy.
    if [[ -n "$PREV_MONO" ]]; then
        if [[ "$BOOT_ID" != "$PREV_BOOT" ]]; then
            INTERRUPTED=1
        fi
        # gap > 20 seconds between samples indicates interruption/suspend
        local_elapsed=$(python3 -c "print(${MONO} - ${PREV_MONO})")
        if python3 -c "exit(0 if ${local_elapsed} > 20 else 1)"; then
            INTERRUPTED=1
        fi
    fi
    PREV_MONO="$MONO"
    PREV_BOOT="$BOOT_ID"

    write_sample "$SEQ" "$WALL" "$MONO" "$BOOTTIME" "$INTERRUPTED"

    if python3 -c 'import sys; exit(0 if float(sys.argv[1]) >= float(sys.argv[2]) else 1)' "$MONO" "$DEADLINE"; then
        break
    fi
    sleep "$INTERVAL"
done
