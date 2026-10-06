# PostgreSQL cutover procedure

This is the operator's procedure. PostgreSQL became live on October 2 at
approximately 17:12 UTC after verified final reconciliation and preparation.
The original SQLite index remains in place as recovery material. New saves and
imports write to PostgreSQL; do not revert to the old SQLite deployment after
activation. First production native backup, exact off-host verification and
both recurring schedules passed their final checks on October 2 at 20:36 UTC.
The normal laptop scheduled task returned result 0. Same-channel write waits
and further search/ranking improvements remain separate development work.
See `POSTGRESQL_PROGRESS.md` for the current checkpoint. Private
settings and recovery files stay outside Git.

## Finish the rehearsal first

1. Preserve the verified off-host recovery file and its file-hash manifest.
   The first full snapshot passed integrity and relationship checks on October 1.
2. Import all 30 legacy tables into the inactive PostgreSQL target. Verify the
   typed content hashes, relationships and sequence high-water marks before
   commit. The metadata and durable-save tables bring the target to 32 tables.
   Four legacy provider descriptions contain NUL characters. Use the explicit
   description-normalization option with a new private report; preserve its
   original-byte audit with the snapshot. All other cells compare exactly.
   After import commit, run the explicit schema-owner upgrade to version 6 and
   verify safe JSON/date reads and the covering/exact-count indexes before
   previewing the full target. Exception-handling helpers must remain parallel
   unsafe. Do not run DDL during
   the import transaction or from ordinary service starts.
3. Measure actual PostgreSQL disk use, temporary/WAL use and remaining disk.
   Preserve at least 8 GiB free throughout administrator operations. Estimates
   and small fixtures do not replace this full measurement.
4. Compare Overview counts, topics, workspaces, rankings, combined filters,
   Real Time and Saved membership against the sealed SQLite recovery file.
   Measure actual server-side reads and the real cloud preview separately from
   laptop-to-server tunnel timings. Resolve mismatches and timeouts first.
5. Create a native PostgreSQL archive with the verified administrator tool.
   Transfer it and its hash manifest to protected off-host storage. Verify the
   transferred file hash. Rehearse a complete restore and content comparison
   into an empty, noncanonical target. If two full targets do not fit, recreate
   only the unused rehearsal target after checking the off-host archive and
   retained SQLite source. Never drop the active application database.
   Prepare the restored tables with VACUUM ANALYZE before reader preview; a
   native restore does not preserve the original heap visibility maps.
6. Verify restricted reader/writer roles, schema permissions and full/read Finder
   authorization. Restoring an archive without privileges requires reapplying
   the restricted grants; preserve private role/deployment recovery settings.

Use a separate read-role-only preview and inspect all six real screens before
maintenance. Keep administrator recovery storage private. Its secret-free
launcher belongs in the separate preview state directory with access for the
application account; do not widen the administrator directory's permissions.

## Drain and catch up

Record the active release and service/timer states before changing them. Explain
the maintenance pause before starting it. The pause includes the final consistent
snapshot and reconciliation; it is not assumed to be instant.

Stop the importer timer from launching another import, and let any current
import finish its commit-before-ack cycle. Pause public write entry points and
request the radar's cooperative stop through its existing stop/pause markers.
Wait for the radar's current step and discovery lane to finish. Stop remaining
canonical-index readers/writers only after draining their in-flight work. Check
actual process liveness, not a stale status file. Do not force-kill a worker to
save time. The edge discovery/organizer may continue queuing signed bundles.

When replacing Caddy routes with the maintenance response, retain the original
route hostname matchers. Automatic certificate handling depends on those names;
preserving only the TLS configuration is insufficient. Verify HTTPS with normal
certificate validation and require the 503 maintenance response before advancing.

Confirm no index service is still running against SQLite. Build and verify a new
off-host snapshot while writers remain paused. The verified quiescent option
first checks actual open database handles and continuously guards the stopped
index units, importer timer, cooperative markers and unchanged release. After
a successful WAL truncate checkpoint and zero open database handles, use
`sqlite_quiescent_backup.compress_quiescent_sqlite` with the explicit pause and
guard. It checks integrity, relationships, absence of pending WAL/journal bytes
and an unchanged source before sealing its compressed archive. Transfer the
archive and manifest off-host; use `recover_quiescent_sqlite` to verify exact
original bytes, full integrity and relationships in a new off-host file. Preserve
that proof and the retained original's verified manifest privately. Reconciliation
may read the sealed quiescent original locally on Oracle only while all these
pause guards remain true. If the canonical file already has an older sidecar,
use a new private hard-link read alias and a new manifest rather than overwriting
that sidecar. Prove the alias and original share the same inode; continuously
check the canonical file's handles and WAL/journal bytes, not just the alias's
filename. The alias is a pointer to the retained original, not an independent
backup. The compressed archive and recovered off-host file supply the independent
recovery copy. This alias path passed a disposable real PostgreSQL migration of
all 30 legacy tables, exact off-host recovery, unchanged-source/sidecar checks
and refusal after a later source mutation.

Do not copy a running WAL file or reuse this option
without the coordinated pause. The existing online logical snapshot remains
available when a consistent online backup is needed.

Reconcile the verified snapshot into PostgreSQL with
`postgres_reconcile` and the explicit writer-pause assertion. Verify every legacy
row before committing. The target's saved-channel job table must still be empty;
do not use the full rehearsal target for test save jobs before final catch-up.
If provider descriptions contain NULs, use the same explicit normalization option
and preserve the catch-up report's original-byte audit in the recovery set.

After final reconciliation commits, run VACUUM ANALYZE across all 32 inactive
target tables while the coordinated pause remains guarded. Keep a new final
preparation report anchored to the reconciled source hash; preserve the earlier
restore-preparation report. The private `vacuum_final_catchup.py` operator
requires the completed successful catch-up unit and matching sealed/off-host/
reconciliation proofs before reusing the verified preparation procedure.
Its refusal gates and real disposable PostgreSQL tests passed, including exact
before/after fingerprints of populated tables. This prepares visibility maps
and planner statistics without changing application records.

At this point, preserve a private recovery set for service definitions, host and
database settings, watchlist configuration, the radar queue, and the classifier
model. Take a consistent queue backup rather than a raw copy of an active WAL
database. Do not expose credentials in commands, reports, Git or screenshots.

## Activate together

Use the verified paired release and service templates. Set all three index
aliases to `postgresql:beacon-index` in the private activation settings. Change
only the radar config's `db_path` to that alias; preserve its queries and other
settings. Keep `BEACON_INDEX_STATE_DIR=/var/lib/beacon/data` so the existing
`radar_work_queue.sqlite3` and `faceless_text_model.joblib` remain in use. Config,
status and logs continue under `/var/lib/beacon/engine-storage`.

Run the activation guard before switching the release pointer. Start Finder,
web and gateway against PostgreSQL, enable the separately deployed save worker,
then resume the previously active radar/import schedule. Preserve the existing
gateway tunnel process and address. Do not run the generic installer's release
cleanup blindly during this controlled migration.

Check every active index unit's selected backend and actual process environment
without displaying credentials. Check role grants again after a restore. Confirm
all six real web screens, the operator's actual channel save, queued/analysis
progress, reconnect/retry behavior, collector acknowledgements, queue progress
and logs. A queued request is successful acceptance, not completed analysis.

## Recovery after activation

After any PostgreSQL write, application rollback must keep the current PostgreSQL
data and use a compatible prior code release. Switching to the old SQLite file
would lose new saves and imports. Preserve the original SQLite file and verified
off-host pre-cutover recovery files; do not delete them as routine release cleanup.

Before closing the migration, establish a tested recurring native backup and
off-host transfer procedure with visible failure reporting and enough disk reserve
for a new archive. Choose retention from actual archive sizes. Never discard the
last verified recovery set to make room for an unverified replacement.
`postgres_backup_schedule.py` and the daily `beacon-postgres-backup` units are
verified and enabled in the current deployment. For another deployment, install
them disabled until its own full-size recovery/off-host gates pass. Set the private
`postgres-backup.env` measured `BEACON_BACKUP_EXPECTED_BYTES` only after the
full native archive is measured, and preserve the private administrator settings
as `postgres-admin.env`. The wrapper keeps two off-host verified sets, preserves
all unmirrored sets and refuses another creation after two unmirrored archives.
Transfer each completed archive and manifest, verify its exact bytes/hash
off-host, then pass that proof on standard input to the `ack` action. The server
hash is checked again before retention can remove an older acknowledged archive.
Inspect `recovery/scheduled/schedule-status.json` and the service result for
failure visibility. Verify the off-host scheduled transfer before enabling the
timer; an installed template alone is not a complete backup system.

The laptop `postgres_backup_pull.py --config <private-json>` accepts explicit
SSH destination/key and remote Python/script/directory paths, a protected local
recovery directory, and `keep_verified` (at least two). Use a hidden task under
the existing user's interactive account, with network availability, missed-run
recovery, bounded execution and failure retries. Preserve its true exit code.
The transfer status records success/failure privately; the Windows task result
must report a failure instead of silently treating it as success. The laptop
must be awake/online for an off-host transfer; the server retains unmirrored
archives while it is unavailable. The scheduled task should run again on login.
Record the activated release, data checks, observed timings and recovery result in
`POSTGRESQL_PROGRESS.md` and update `PROJECT_GUIDE.md`.
