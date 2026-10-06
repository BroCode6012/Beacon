#!/usr/bin/env bash
# Install — or upgrade — Beacon on ANY Linux server. One command, safe to run again.
#
#   sudo bash install.sh beacon_code.tar.gz
#
# Nothing here names a particular provider: Oracle today, whatever is better tomorrow. The
# package comes from `python beacon_move.py code` on the laptop (Beacon/ + Quill/, no secrets).
#
# Layout (the code and the data never mix, so an upgrade can't touch the library):
#   /opt/beacon/releases/<time>/{Beacon,Quill}   one folder per install — read-only code
#   /opt/beacon/current -> releases/<time>       what runs; rollback = point it back
#   /opt/beacon/venv                             Python packages (rebuilt only when they change)
#   /var/lib/beacon/                             EVERYTHING that must survive an upgrade:
#       beacon_host.env    this machine's settings + keys (written once, never overwritten)
#       data/              the library (filled by `beacon_move.py receive`)
#       storage/           Beacon's own state        engine-storage/  the engine's state
#       state/             .env, _ai_choice.json, _autopilot.json, _targets.json
#
# Starts the data service and the web screens (both only on 127.0.0.1). The scraper and the
# cloud-collector import are installed but NOT started — they take over from the laptop only at
# the switch-over, and in this order: the laptop's sync off FIRST, then these on.
set -euo pipefail

PKG=${1:?usage: sudo bash install.sh beacon_code.tar.gz}
APP=${BEACON_HOME:-/opt/beacon}
DATA=${BEACON_DATA:-/var/lib/beacon}
SVC_USER=${BEACON_USER:-beacon}
TZ_NAME=${BEACON_TZ:-Africa/Casablanca}
# The machine's key file (YouTube keys, and the cloud collector's signing key when it runs
# here). Read by systemd as root; the scraper gets only its YouTube lines (beacon-radar.service).
KEYS_FILE=${BEACON_KEYS_FILE:-/etc/signal-edge/edge.env}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
REL="$APP/releases/$STAMP"

say() { echo "[beacon-install $(date +%H:%M:%S)] $*"; }
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ -f "$PKG" ] || { echo "package not found: $PKG"; exit 1; }

# 1. a service account that owns only the data ------------------------------------------------
if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$SVC_USER"
    say "created service account '$SVC_USER'"
fi
install -d -m 0755 "$APP" "$APP/releases"
install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" \
    "$DATA" "$DATA/data" "$DATA/storage" "$DATA/engine-storage" "$DATA/state"

# 2. the code, as a new release ---------------------------------------------------------------
install -d -m 0755 "$REL"
tar -xzf "$PKG" -C "$REL" --no-same-owner
[ -d "$REL/Beacon" ] && [ -d "$REL/Quill/scripter" ] || { echo "package must hold Beacon/ and Quill/scripter/"; exit 1; }
# Packages built on Windows carry Windows line endings and permissive modes: normalise both.
find "$REL" -type f \( -name '*.sh' -o -name '*.service' -o -name '*.timer' -o -name '*.rules' -o -name '*.env' -o -name local-ssh -o -name youtube-keys \) \
    -exec sed -i 's/\r$//' {} +
chown -R root:root "$REL"
chmod -R u=rwX,go=rX "$REL"
chmod 0755 "$REL/Beacon/deploy/server/local-ssh"
# Everything that must outlive this release lives under $DATA, reached through links.
rm -rf "$REL/Beacon/storage" "$REL/Quill/storage"
ln -s "$DATA/storage" "$REL/Beacon/storage"
ln -s "$DATA/engine-storage" "$REL/Quill/storage"
for f in .env _ai_choice.json _autopilot.json _targets.json; do
    ln -sfn "$DATA/state/$f" "$REL/Beacon/$f"
done
say "code unpacked as release $STAMP"

# 3. Python packages — pinned, rebuilt only when the list changes ----------------------------
REQ="$REL/Beacon/deploy/server/requirements-server.txt"
VENV="$APP/venv"
WANT=$(sha256sum "$REQ" | cut -d' ' -f1)
if [ ! -x "$VENV/bin/python" ] || [ "$(cat "$VENV/.requirements.sha256" 2>/dev/null)" != "$WANT" ]; then
    say "installing Python packages (a few minutes the first time)"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip -q
    "$VENV/bin/pip" install -q -r "$REQ"
    echo "$WANT" > "$VENV/.requirements.sha256"
    say "packages installed"
else
    say "packages unchanged — reusing them"
fi

# 4. this machine's settings — written once; they hold keys, so never overwritten -------------
HOSTFILE="$DATA/beacon_host.env"
if [ ! -f "$HOSTFILE" ]; then
    FULL=$("$VENV/bin/python" -c "import secrets; print(secrets.token_urlsafe(32))")
    READ=$("$VENV/bin/python" -c "import secrets; print(secrets.token_urlsafe(32))")
    sed -e "s#^FACELESS_ENGINE_DIR=.*#FACELESS_ENGINE_DIR=$APP/current/Quill#" \
        -e "s#/var/lib/beacon#$DATA#g" \
        -e "s#^TZ=.*#TZ=$TZ_NAME#" \
        -e "s#^FINDER_TOKEN=.*#FINDER_TOKEN=$FULL#" \
        -e "s#^FINDER_READ_TOKEN=.*#FINDER_READ_TOKEN=$READ#" \
        "$REL/Beacon/beacon_host.example.env" > "$HOSTFILE"
    chown root:"$SVC_USER" "$HOSTFILE"
    chmod 0640 "$HOSTFILE"
    say "wrote $HOSTFILE with new random keys for the data service"
fi
# Settings added in later versions reach machines installed earlier — appended, never changed.
while IFS= read -r line; do
    name=${line%%=*}
    grep -q "^$name=" "$HOSTFILE" || { echo "$line" >> "$HOSTFILE"; say "added setting $name"; }
done <<'NEW_SETTINGS'
BEACON_RADAR_UNIT=beacon-radar.service
NEW_SETTINGS
ln -sfn "$HOSTFILE" "$REL/Beacon/beacon_host.env"

# 5. switch to the new release (atomic), keep the last three ---------------------------------
# A PostgreSQL settings file is activated only after every old writer has drained.
# Later PostgreSQL upgrades may keep the PostgreSQL radar running normally.
"$VENV/bin/python" "$REL/Beacon/deploy/server/check_postgres_activation.py" "$DATA"
ln -sfn "$REL" "$APP/current.new" && mv -Tf "$APP/current.new" "$APP/current"
# ...but never one a running service still runs from: the scraper is NOT restarted on upgrade,
# so its release must outlive newer installs — deleting it would make its next not-yet-loaded
# import fail mid-cycle (caught in review, 2026-09-29, one deploy before it would have happened).
in_use=""
for s in beacon-radar beacon-finder beacon-web beacon-gateway beacon-edge-sync beacon-save-worker; do
    pid=$(systemctl show -p MainPID --value "$s" 2>/dev/null || true)
    if [ -n "$pid" ] && [ "$pid" != 0 ]; then
        in_use="$in_use $(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
    fi
done
ls -1dt "$APP"/releases/* | tail -n +4 | while read -r old; do
    case "$in_use" in
        *"$old/"*) say "keeping $(basename "$old") — a running service still uses it" ;;
        *) rm -rf "$old" ;;
    esac
done

# 6. services ---------------------------------------------------------------------------------
[ -f "$KEYS_FILE" ] || say "note: no key file at $KEYS_FILE yet — the scraper and cloud import need one"
for unit in "$REL"/Beacon/deploy/server/systemd/*.service "$REL"/Beacon/deploy/server/systemd/*.timer; do
    sed -e "s#@APP@#$APP#g; s#@HOME@#$APP#g; s#@DATA@#$DATA#g; s#@USER@#$SVC_USER#g; s#@KEYS@#$KEYS_FILE#g" \
        "$unit" > "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload
# The one system action Beacon's account may take: start/stop its own scraper service.
if [ -d /etc/polkit-1/rules.d ]; then
    sed -e "s#@USER@#$SVC_USER#g" "$REL/Beacon/deploy/server/polkit/50-beacon-radar.rules" \
        > /etc/polkit-1/rules.d/50-beacon-radar.rules
    chmod 0644 /etc/polkit-1/rules.d/50-beacon-radar.rules
else
    say "note: no polkit here — the Overview's Start button cannot start the scraper service"
fi
for s in beacon-finder beacon-web; do
    systemctl enable "$s" >/dev/null 2>&1
    systemctl restart "$s"
done
# The scraper and the cloud import ARE the switch-over: installed here, never started here.
# A running scraper is NOT restarted: killing it mid-cycle parks the batches it holds for the
# queue's 6-hour lease (seen on 2026-09-29). Engine changes reach it at its next Stop → Start;
# the cloud import is a fresh process every 2 minutes, so it picks up new code by itself.
if systemctl is-active --quiet beacon-radar; then
    say "the scraper keeps running its current code — Stop, then Start it in Overview to update it"
fi
# Muse's door holds no work in progress, so it simply restarts onto the new code. Its public
# road (beacon-tunnel) is deliberately NOT restarted: a quick tunnel gets a new address on
# every restart, and each new address costs the operator a key re-paste in Muse.
if systemctl is-active --quiet beacon-gateway; then systemctl restart beacon-gateway; fi
# Only restart an already active save worker. Initial activation is a cutover action.
if systemctl is-active --quiet beacon-save-worker; then systemctl restart beacon-save-worker; fi
running=""
for s in beacon-radar beacon-edge-sync.timer beacon-gateway beacon-tunnel beacon-save-worker; do
    systemctl is-active --quiet "$s" && running="$running ${s%.timer}"
done
say "restarted: beacon-finder, beacon-web; also running:${running:- nothing else yet}"

# 7. prove it — never report "installed" on faith ---------------------------------------------
READ_KEY=$(grep '^FINDER_READ_TOKEN=' "$HOSTFILE" | cut -d= -f2-)
FPORT=$(grep '^FINDER_PORT=' "$HOSTFILE" | cut -d= -f2-)
WPORT=$(grep '^FLET_SERVER_PORT=' "$HOSTFILE" | cut -d= -f2-)
finder=000; web=000
for _ in $(seq 1 60); do
    finder=$(curl -s -o /dev/null -w '%{http_code}' -m 3 -H "Authorization: Bearer $READ_KEY" "http://127.0.0.1:$FPORT/health" || true)
    web=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$WPORT/" || true)
    [ "$finder" = 200 ] && [ "$web" = 200 ] && break
    sleep 3
done
if [ "$finder" = 200 ] && [ "$web" = 200 ]; then
    say "HEALTHY — data service and web screens both answer (release $STAMP)"
else
    say "NOT healthy: data service HTTP $finder, web HTTP $web — see: journalctl -u beacon-finder -u beacon-web"
    exit 1
fi
