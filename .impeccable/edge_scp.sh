#!/usr/bin/env bash
# Copy a local file to the Oracle edge VM using the settings in Beacon/.env.
# Reuses the multiplexed master from edge_ssh.sh; values are never echoed.
#
# Usage: edge_scp.sh <local-path> <remote-path>
set -uo pipefail

ENVFILE="D:/Youtube/Beacon/.env"
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

LOCAL="$1"
REMOTE="$2"

# The laptop sits behind carrier-grade NAT, so the single OCI /32 rule goes
# stale mid-session. Refresh it and retry, exactly like edge_ssh.sh does.
refresh_allowlist() {
  ( cd "D:/Youtube/Beacon" && ./storage/oci_updater_venv/Scripts/python.exe \
      beacon_engine/edge/oci_allowlist.py \
      --status "storage/edge_sync/oci_allowlist_status.json" ) >/dev/null 2>&1
}

RC=91
OUT="EDGE_SCP_UNREACHABLE"
for try in 1 2 3 4 5 6 7 8; do
  OUT="$(scp -i "$K" -P "$P" \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new \
    -o LogLevel=ERROR \
    -o ConnectTimeout=20 \
    -o ServerAliveInterval=20 \
    -o ServerAliveCountMax=15 \
    "$LOCAL" "$U@$H:$REMOTE" 2>&1)"
  RC=$?
  [ $RC -eq 0 ] && break
  [ -S "$CTL" ] && rm -f "$CTL"
  refresh_allowlist
  sleep 3
done

printf '%s\n' "$OUT" | scrub
exit $RC
