#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_ROOT="/opt/signal-edge"
STATE_ROOT="/var/lib/signal-edge"
CONFIG_ROOT="/etc/signal-edge"
SERVICE_USER="signal-edge"
SERVICE_GROUP="signal-edge"

usage() {
  cat <<'EOF'
Usage: sudo ./bootstrap.sh SOURCE_DIRECTORY [--start]

Installs one immutable Signal edge release. SOURCE_DIRECTORY must contain
requirements-edge.txt, beacon_engine/, and deploy/oracle-edge/.

Without --start the service is installed and enabled but left stopped. This is
the safe first-run default while the private environment and signed plan are
being configured.
EOF
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 2
fi

SOURCE_DIR="${1:-}"
START_AFTER_INSTALL="${2:-}"
if [[ -z "${SOURCE_DIR}" || ( -n "${START_AFTER_INSTALL}" && "${START_AFTER_INSTALL}" != "--start" ) ]]; then
  usage >&2
  exit 2
fi
SOURCE_DIR="$(readlink -f "${SOURCE_DIR}")"

for required in \
  "${SOURCE_DIR}/requirements-edge.txt" \
  "${SOURCE_DIR}/beacon_engine/edge/__main__.py" \
  "${SOURCE_DIR}/deploy/oracle-edge/signal-edge.service" \
  "${SOURCE_DIR}/deploy/oracle-edge/signal-edge-organizer.service"; do
  if [[ ! -f "${required}" ]]; then
    echo "Release is missing required file: ${required}" >&2
    exit 2
  fi
done

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  python3 \
  python3-pip \
  python3-venv \
  libgomp1 \
  unattended-upgrades

python3 - <<'PY'
import sys

if sys.version_info < (3, 11):
    raise SystemExit(
        "Signal edge requires Python 3.11 or newer; use Ubuntu 24.04 ARM."
    )
PY

if ! getent group "${SERVICE_GROUP}" >/dev/null; then
  groupadd --system "${SERVICE_GROUP}"
fi
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd \
    --system \
    --gid "${SERVICE_GROUP}" \
    --home-dir "${STATE_ROOT}" \
    --shell /usr/sbin/nologin \
    "${SERVICE_USER}"
fi

install -d -m 0755 -o root -g root "${INSTALL_ROOT}" "${INSTALL_ROOT}/releases"
install -d -m 0750 -o root -g "${SERVICE_GROUP}" "${CONFIG_ROOT}"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" \
  "${STATE_ROOT}" \
  "${STATE_ROOT}/jobs" \
  "${STATE_ROOT}/outbox" \
  "${STATE_ROOT}/acks" \
  "${STATE_ROOT}/organizer" \
  "${STATE_ROOT}/organizer/jobs" \
  "${STATE_ROOT}/organizer/archive" \
  "${STATE_ROOT}/organizer/failed" \
  "${STATE_ROOT}/organizer/models"

release_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
stage="${INSTALL_ROOT}/releases/.staging-${release_id}"
release="${INSTALL_ROOT}/releases/${release_id}"
install -d -m 0755 -o root -g root "${stage}"

cleanup_stage() {
  if [[ -d "${stage}" ]]; then
    rm -rf --one-file-system "${stage}"
  fi
}
trap cleanup_stage EXIT

cp -a "${SOURCE_DIR}/." "${stage}/"
rm -rf \
  "${stage}/.git" \
  "${stage}/storage" \
  "${stage}/verification" \
  "${stage}/__pycache__"

python3 -m venv "${stage}/.venv"
"${stage}/.venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"${stage}/.venv/bin/python" -m pip install \
  --disable-pip-version-check \
  --requirement "${stage}/requirements-edge.txt"
(
  cd "${stage}"
  ./.venv/bin/python -m beacon_engine.edge --help >/dev/null
)
"${stage}/.venv/bin/python" -m pip freeze \
  > "${stage}/requirements-resolved.txt"

chown -R root:root "${stage}"
chmod -R u=rwX,go=rX "${stage}"
mv "${stage}" "${release}"
trap - EXIT

next_link="${INSTALL_ROOT}/current.next"
rm -f "${next_link}"
ln -s "${release}" "${next_link}"
mv -Tf "${next_link}" "${INSTALL_ROOT}/current"

# Prune old immutable releases so the small ARM boot volume can't fill over time. Keep the
# live release (current symlink target) plus the newest few for the documented rollback.
KEEP_RELEASES=3
current_target="$(readlink -f "${INSTALL_ROOT}/current" || true)"
mapfile -t all_releases < <(find "${INSTALL_ROOT}/releases" -mindepth 1 -maxdepth 1 -type d \
  -not -name '.staging-*' -printf '%f\n' 2>/dev/null | sort)
prune_count=$(( ${#all_releases[@]} - KEEP_RELEASES ))
if (( prune_count > 0 )); then
  for old in "${all_releases[@]:0:prune_count}"; do
    old_path="${INSTALL_ROOT}/releases/${old}"
    if [[ "$(readlink -f "${old_path}")" != "${current_target}" ]]; then
      rm -rf --one-file-system "${old_path}"
    fi
  done
fi

install -m 0644 -o root -g root \
  "${release}/deploy/oracle-edge/signal-edge.service" \
  /etc/systemd/system/signal-edge.service
install -m 0644 -o root -g root \
  "${release}/deploy/oracle-edge/signal-edge-organizer.service" \
  /etc/systemd/system/signal-edge-organizer.service
# Out-of-process stall watchdog. It judges liveness by PROGRESS rather than by
# the process existing, so it also rescues a release predating the worker's own
# stall watchdog. See the 2026-07-20 outage note in signal-edge-watchdog.sh.
install -m 0755 -o root -g root \
  "${release}/deploy/oracle-edge/signal-edge-watchdog.sh" \
  /usr/local/sbin/signal-edge-watchdog.sh
install -m 0644 -o root -g root \
  "${release}/deploy/oracle-edge/signal-edge-watchdog.service" \
  /etc/systemd/system/signal-edge-watchdog.service
install -m 0644 -o root -g root \
  "${release}/deploy/oracle-edge/signal-edge-watchdog.timer" \
  /etc/systemd/system/signal-edge-watchdog.timer
install -m 0600 -o root -g root \
  "${release}/deploy/oracle-edge/edge.env.example" \
  "${CONFIG_ROOT}/edge.env.example"

if [[ -f "${CONFIG_ROOT}/edge.env" ]]; then
  chown root:"${SERVICE_GROUP}" "${CONFIG_ROOT}/edge.env"
  chmod 0640 "${CONFIG_ROOT}/edge.env"
fi

systemctl daemon-reload
systemctl enable signal-edge.service
systemctl enable signal-edge-organizer.service
# Safe to arm immediately: the watchdog exits 0 whenever signal-edge is not
# active, so it never fights a first install or a deliberate stop.
systemctl enable --now signal-edge-watchdog.timer
systemctl enable --now unattended-upgrades.service >/dev/null 2>&1 || true

if [[ "${START_AFTER_INSTALL}" == "--start" ]]; then
  if [[ ! -f "${CONFIG_ROOT}/edge.env" ]]; then
    echo "Refusing to start: ${CONFIG_ROOT}/edge.env is not configured." >&2
    exit 3
  fi
  if ! find "${STATE_ROOT}/jobs" -maxdepth 1 -type f \
      -name '*.signal-edge.json.gz' -print -quit | grep -q .; then
    echo "Refusing to start: no signed discovery plan exists in ${STATE_ROOT}/jobs." >&2
    exit 3
  fi
  systemctl restart signal-edge.service
  systemctl restart signal-edge-organizer.service
fi

echo "Installed immutable release: ${release}"
echo "Current release: $(readlink -f "${INSTALL_ROOT}/current")"
if [[ "${START_AFTER_INSTALL}" != "--start" ]]; then
  echo "Next: configure ${CONFIG_ROOT}/edge.env, upload a signed plan, then start signal-edge and signal-edge-organizer."
fi
