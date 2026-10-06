#!/usr/bin/env bash
# Restart signal-edge when its worker status stops advancing.
#
# Why this exists: on 2026-07-20 the collector deadlocked on a socket read that
# had no timeout. The process stayed alive, so systemd reported
# "active (running)" and Restart=on-failure never fired. Discovery was dead for
# 17 days while every laptop-side cycle still reported success.
#
# A release that carries the in-process stall watchdog exits 75 on its own and
# systemd recovers it. This unit is the belt-and-braces layer: it also rescues a
# release that predates that fix, because it judges liveness by PROGRESS
# (updated_at moving) rather than by the process merely existing.
set -Eeuo pipefail

ROOT="${SIGNAL_EDGE_ROOT:-/var/lib/signal-edge}"
STATUS="${ROOT}/worker_status.json"
# Overridable only so the fire path can be rehearsed against a throwaway unit;
# production always uses the default.
UNIT="${SIGNAL_EDGE_UNIT:-signal-edge.service}"
MAX_STALL_MINUTES="${SIGNAL_EDGE_MAX_STALL_MINUTES:-30}"

# Only police a unit that is meant to be running; never fight a manual stop.
if ! systemctl is-active --quiet "${UNIT}"; then
  exit 0
fi

if [ ! -f "${STATUS}" ]; then
  echo "signal-edge-watchdog: no status file yet; not acting"
  exit 0
fi

updated="$(python3 - "${STATUS}" <<'PY'
import datetime as dt
import json
import sys

try:
    raw = json.load(open(sys.argv[1], encoding="utf-8"))
    stamp = str(raw.get("updated_at") or "").replace("Z", "+00:00")
    print(int(dt.datetime.fromisoformat(stamp).timestamp()))
except Exception:
    print(0)
PY
)"

if [ "${updated}" -le 0 ]; then
  echo "signal-edge-watchdog: unreadable updated_at; not acting"
  exit 0
fi

now="$(date -u +%s)"
age=$(( now - updated ))
limit=$(( MAX_STALL_MINUTES * 60 ))

if [ "${age}" -le "${limit}" ]; then
  exit 0
fi

echo "signal-edge-watchdog: STALLED — worker status has not advanced for ${age}s (limit ${limit}s). Restarting ${UNIT}."
systemctl restart "${UNIT}"
echo "signal-edge-watchdog: restart issued for ${UNIT}"
