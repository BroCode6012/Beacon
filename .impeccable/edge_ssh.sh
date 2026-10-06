#!/usr/bin/env bash
# Run a command on the Oracle edge VM using settings from Beacon/.env.
# Values are never echoed; host/user/key are scrubbed from all output.
#
# The laptop sits behind carrier-grade NAT: its public IPv4 changes per
# connection across a pool, so the single OCI /32 rule is often stale. We keep
# ONE persistent multiplexed SSH master connection alive and reuse it, and we
# retry (refreshing the /32) until a connection lands.
#
# Usage: edge_ssh.sh '<remote command>'
set -uo pipefail

ENVFILE="D:/Youtube/Beacon/.env"
SCRIPTER="D:/Youtube/Beacon"
CTL="/tmp/signal-edge-ctl.sock"

getval() {
  sed -n "s/\r$//; s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENVFILE" | head -1 | sed 's/^"//; s/"$//'
}

H="$(getval SIGNAL_EDGE_SSH_HOST)"
U="$(getval SIGNAL_EDGE_SSH_USER)"
K="$(getval SIGNAL_EDGE_SSH_KEY)"
P="$(getval SIGNAL_EDGE_SSH_PORT)"
[ -z "$P" ] && P=22

if [ -z "$H" ] || [ -z "$U" ] || [ -z "$K" ]; then
  echo "MISSING_SSH_SETTINGS" >&2
  exit 90
fi

scrub() { sed -e "s|$H|<edge-host>|g" -e "s|$K|<edge-key>|g" -e "s|$U@|<edge-user>@|g"; }

refresh_allowlist() {
  ( cd "$SCRIPTER" && ./storage/oci_updater_venv/Scripts/python.exe \
      beacon_engine/edge/oci_allowlist.py \
      --status "storage/edge_sync/oci_allowlist_status.json" ) >/dev/null 2>&1
}

SSH_BASE=(ssh -i "$K" -p "$P"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -o LogLevel=ERROR
  -o ControlMaster=auto
  -o ControlPath="$CTL"
  -o ControlPersist=30m
  -o ServerAliveInterval=20
  -o ServerAliveCountMax=15)

master_alive() {
  ssh -O check -o ControlPath="$CTL" -i "$K" -p "$P" "$U@$H" >/dev/null 2>&1
}

open_master() {
  for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if master_alive; then return 0; fi
    [ -S "$CTL" ] && rm -f "$CTL"
    refresh_allowlist
    if "${SSH_BASE[@]}" -o ConnectTimeout=15 -f -N "$U@$H" >/dev/null 2>&1; then
      master_alive && return 0
    fi
    sleep 2
  done
  return 1
}

transient() {
  printf '%s' "$1" | grep -qiE \
    'mux_client|read from master|timed out|no route|refused|reset by peer|broken pipe|Connection closed|disabling multiplexing'
}

RC=91
OUT="EDGE_SSH_UNREACHABLE"
for try in 1 2 3 4 5 6; do
  if ! open_master; then sleep 3; continue; fi
  OUT="$("${SSH_BASE[@]}" "$U@$H" "$@" 2>&1)"
  RC=$?
  if [ $RC -eq 0 ] && ! transient "$OUT"; then break; fi
  # A partial/interrupted session: drop the master and try again.
  if transient "$OUT"; then
    ssh -O exit -o ControlPath="$CTL" -i "$K" -p "$P" "$U@$H" >/dev/null 2>&1
    rm -f "$CTL"
    sleep 2
    continue
  fi
  break
done

printf '%s\n' "$OUT" | scrub
exit $RC
