#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE="$(readlink -f /opt/signal-edge/current)"
PYTHON="${RELEASE}/.venv/bin/python"
ROOT="/var/lib/signal-edge"

cd "${RELEASE}"
systemctl --no-pager --full status signal-edge.service
echo
"${PYTHON}" -m beacon_engine.edge status --root "${ROOT}"
echo
systemctl --no-pager --full status signal-edge-organizer.service
echo
"${PYTHON}" -m beacon_engine.edge organizer-status \
  --root "${ROOT}/organizer" \
  --outbox "${ROOT}/outbox"
echo
printf 'plans=%s organizer_jobs=%s outbox_bundles=%s acknowledgements=%s\n' \
  "$(find "${ROOT}/jobs" -maxdepth 1 -type f -name '*.signal-edge.json.gz' | wc -l)" \
  "$(find "${ROOT}/organizer/jobs" -maxdepth 1 -type f -name '*.signal-edge.json.gz' | wc -l)" \
  "$(find "${ROOT}/outbox" -maxdepth 1 -type f -name '*.signal-edge.json.gz' | wc -l)" \
  "$(find "${ROOT}/acks" -maxdepth 1 -type f -name '*.ack.json' | wc -l)"
