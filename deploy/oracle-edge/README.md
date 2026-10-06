# Oracle edge deployment

This package installs only Signal's lightweight, signed discovery collector.
It never receives, opens, mounts, or owns
`D:\Youtube\Beacon\storage\youtube_index.sqlite3`.

## Target machine

Use one Oracle Ampere A1 Flex VM in the home region:

- Ubuntu 24.04 Minimal, ARM/aarch64;
- 2 OCPUs and 12 GB RAM;
- a 50 GB boot volume;
- one public IPv4 address for initial SSH management; and
- no inbound application ports.

Allow TCP 22 only from the operator's current public IP address (`/32`). Do not
open HTTP, HTTPS, Finder, SQLite, or a custom worker port. The worker only makes
outbound HTTPS requests.

If Oracle reports temporary A1 capacity exhaustion, leave the architecture
unchanged and retry another availability domain or later time.

## Build a clean release on the laptop

The packager archives a committed Git revision through an explicit allowlist.
It cannot include `.env`, storage, databases, logs, sessions, screenshots, or
uncommitted files.

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\oracle-edge\build-release.ps1 `
  -OutPath "$HOME\Downloads\signal-edge.tar.gz"
```

Keep the generated `.sha256` file beside the archive and verify it after
uploading.

## Install without starting

Upload the archive with SSH/SFTP, then on the VM:

```bash
mkdir -p ~/signal-edge-upload
tar -xzf ~/signal-edge.tar.gz -C ~/signal-edge-upload
cd ~/signal-edge-upload/signal-edge
sudo ./deploy/oracle-edge/bootstrap.sh .
```

The installer:

- requires Python 3.11 or newer;
- creates an unprivileged `signal-edge` service account;
- builds a self-contained virtual environment inside an immutable release;
- atomically switches `/opt/signal-edge/current`;
- records exact resolved Python packages for rollback/auditing;
- installs a hardened systemd service; and
- deliberately leaves the worker stopped until its private environment and
  signed plan exist.

## Add private settings

Never place secrets in the release archive. On the VM, use `sudoedit` so values
are not written into shell history:

```bash
sudo cp /etc/signal-edge/edge.env.example /etc/signal-edge/edge.env
sudo chown root:signal-edge /etc/signal-edge/edge.env
sudo chmod 0640 /etc/signal-edge/edge.env
sudoedit /etc/signal-edge/edge.env
```

Replace both placeholders with the shared signing secret and the user's
authorized YouTube API-key pool. The signing value must exactly match the
laptop's private `SIGNAL_EDGE_SIGNING_KEY`.

The API pool is dynamic: batched `videos.list`, `channels.list`, upload
playlist, and category-enrichment work is distributed across healthy keys at
the same time. Exhausted or invalid keys are removed from the active pool for
the current process session. The default concurrency ceiling is 16 and may be
adjusted with `YOUTUBE_API_WORKERS` up to the built-in safety limit; adding more
keys never requires a source-code change.

After the first deployment, add future keys from the laptop through the hidden
prompt instead of editing either environment manually:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\add_edge_youtube_keys.ps1 `
  -Count 3
```

The values are read without echo, sent to Oracle through SSH standard input
rather than command arguments, deduplicated, and merged atomically into both
private environments. Only counts are printed. Oracle restarts the discovery
and organizer services so the expanded pool becomes active immediately.

## Upload a signed plan and start

Place a laptop-generated `youtube.discovery.plan.v1` file in:

```text
/var/lib/signal-edge/jobs/
```

Then:

```bash
sudo chown signal-edge:signal-edge /var/lib/signal-edge/jobs/*.signal-edge.json.gz
sudo chmod 0600 /var/lib/signal-edge/jobs/*.signal-edge.json.gz
sudo systemctl start signal-edge
sudo systemctl start signal-edge-organizer
sudo /opt/signal-edge/current/deploy/oracle-edge/healthcheck.sh
```

The normal laptop sync now handles this automatically. After it connects, it
compares the live normalized watchlist and edge settings with the last
published specification. It signs and atomically uploads a new immutable plan
only when those inputs change. The current production plan contains 4,206
searches.

For safety, automatic publication defaults off when
`SIGNAL_EDGE_SYNC_ROOT` points at a custom/test state directory. A deliberately
isolated environment may opt in with `SIGNAL_EDGE_AUTO_PLAN_SYNC=1`; tests
should otherwise leave production jobs untouched.

Worker output accumulates in `/var/lib/signal-edge/outbox/`. A separate
SSH transport adapter moves those immutable signed bundles to the laptop inbox.
Signal verifies them again before any row reaches the canonical database, sends
a signed acknowledgement, and removes the remote outbox copy only after the
local import succeeds.

The collector also performs bounded deterministic pre-organization before it
signs each bundle:

- every channel receives one clean topic from the shared topic rules; and
- the videos already present in that batch receive a `text-v3` faceless-format
  score.

The VM still never opens or copies Signal's SQLite database. On import, Signal
fills only missing topics and preserves a stronger local text classification.

Free-search collection retries only transient JSON decoding, timeout,
connection, HTTP 429, and HTTP 503 failures. Each query receives no more than
three total attempts with exponential backoff and small jitter. Attempt and
recovery counts are written into the signed result metadata for measurement;
permanent failures are not retried in a tight loop.

Repeat searches now produce change-aware result bundles. Query-run tracking is
always retained, but unchanged channel/video rows, classifications, discovery
links, and snapshots are omitted. New results are sent immediately, and an
existing video is sent again when its observed view count changes. This keeps
the signed audit trail while avoiding repeated transfer and SQLite upserts for
the same unchanged records.

## 24/7 deep organization

Discovery automatically creates signed `youtube.organization.job.v1` files
under `/var/lib/signal-edge/organizer/jobs/` only for channels attached to
newly discovered videos. Normal jobs contain at most 100 channels; the protocol
rejects more than 500 channels or 10,000 seed videos in one job.

`signal-edge-organizer.service` processes those jobs independently from the
discovery service. It:

- enriches channel metadata and resolves official uploads playlists;
- catalogs up to 30 recent uploads per eligible channel through
  concurrent healthy-key `playlistItems.list` lanes, with the free channel
  scraper as fallback;
- enriches video/category data in concurrent quota-efficient batches;
- assigns deterministic topic and `text-v3` faceless-format evidence; and
- performs bounded CPU face checks using persistent models under
  `/var/lib/signal-edge/organizer/models/`.

Results are signed `youtube.organization.v1` bundles in the shared outbox.
The laptop verifies and imports them atomically, preserves stronger/newer local
evidence, and ignores a previously imported source job revision. Oracle stores
only operational state and immutable job/result files; it never receives the
canonical SQLite database.

Useful non-secret checks:

```bash
sudo systemctl status signal-edge signal-edge-organizer
sudo -u signal-edge /opt/signal-edge/current/.venv/bin/python \
  -m beacon_engine.edge organizer-status \
  --root /var/lib/signal-edge/organizer \
  --outbox /var/lib/signal-edge/outbox
```

The laptop may publish a bounded read-only backfill slice with
`publish-organization-backfill`; do not copy the database to Oracle. Automatic
discovery jobs use eight catalog workers, two face workers, a 30-day refresh
window, and a per-job quota ceiling.

Configure these private laptop `.env` values:

```text
SIGNAL_EDGE_SSH_HOST=<public IP>
SIGNAL_EDGE_SSH_USER=ubuntu
SIGNAL_EDGE_SSH_KEY=<absolute private-key path>
SIGNAL_EDGE_SSH_PORT=22
SIGNAL_EDGE_REMOTE_ROOT=/var/lib/signal-edge
```

For a laptop whose public IP changes, also configure the OCI `/32` updater:

```text
SIGNAL_EDGE_OCI_CONFIG=C:\Users\<you>\.oci\config
SIGNAL_EDGE_OCI_PROFILE=SIGNAL_EDGE_IP
SIGNAL_EDGE_OCI_SECURITY_LIST_ID=<security-list OCID>
SIGNAL_EDGE_OCI_MANAGED_DESCRIPTION=Signal laptop dynamic SSH /32
SIGNAL_EDGE_OCI_VERIFY_SECONDS=600
```

Register the matching public API-signing key in Oracle, keep the private key
outside the repository with a user-only ACL, then install the isolated updater:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_edge_oci_updater.ps1
```

The updater intentionally uses `requirements-oci-updater.txt` in
`storage\oci_updater_venv`; it is not installed into the shared scraper venv.
Before SSH, it discovers the current public IPv4, adopts exactly one
unambiguous TCP/22 rule, and updates only that rule's source to `<ip>/32`.
All unrelated ingress rules, egress rules, tags, and the security-list display
name are preserved. A recent verified address is cached briefly, but any IP
change bypasses the cache immediately.

`Sync now` in Signal pulls and imports a bounded slice. The SSH adapter batches
downloads and acknowledgements while preserving per-bundle signature checks.
To drain up to 100 waiting bundles every 2 minutes while the laptop is on:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_edge_sync_task.ps1
```

The installed task runs through `wscript.exe` with a zero window style. It
waits for the real PowerShell sync and returns that exit code to Task Scheduler,
so routine runs remain invisible without turning failures into false successes.
When a sleeping laptop wakes to a backlog, that one task invocation keeps
draining consecutive 100-bundle slices until both the laptop inbox and Oracle
outbox are empty, or until its bounded 90-minute catch-up budget is reached.
The importer verifies every bundle independently, uses one savepoint per
bundle, and durably commits groups of 20 before writing acknowledgements.

With cloud discovery active, set the laptop radar's `harvest_enabled` setting
to false. The laptop then spends its radar cycles finishing canonical-database
organization instead of duplicating Oracle searches. The organization-only
path retains enrichment, thin cataloging, video categorization, topic, junk,
face, and text classification while skipping unrelated discovery and expensive
whole-index work. The canonical database remains on the laptop.

## Stall recovery

A hung process never exits, so `Restart=on-failure` cannot see it. On
2026-07-20 the collector deadlocked on a timeout-less socket read and systemd
reported `active (running)` for 17 days while nothing was collected. Two layers
now prevent a silent repeat:

- the worker exits `75` when no status write happens for
  `SIGNAL_EDGE_STALL_SECONDS` (default 1800), which systemd can act on; and
- `signal-edge-watchdog.timer` runs every five minutes, compares
  `worker_status.json`'s `updated_at` against
  `SIGNAL_EDGE_MAX_STALL_MINUTES` (default 30), and restarts `signal-edge`
  when progress has stopped. It exits without acting whenever the unit is not
  active, so it never fights a deliberate stop.

Check it with `systemctl list-timers signal-edge-watchdog.timer` and
`journalctl -u signal-edge-watchdog.service`.

## Safe updates and rollback

Run `bootstrap.sh` with a new release. It builds and smoke-tests the new release
before atomically changing `current`. Existing state, plans, and outbox files
remain under `/var/lib/signal-edge`.

To roll back, point `/opt/signal-edge/current` at a previous directory under
`/opt/signal-edge/releases/`, then restart both `signal-edge` and
`signal-edge-organizer`. Never copy the live Signal database to the VM as part
of an update or recovery.
