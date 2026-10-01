# PostgreSQL foundation — 2026-09-30

## Update: exact-statistics indexes — 2026-10-01

All ten actual restricted-reader queries now pass with the normal 30-second
statement limit. Measurements: Overview 11.327 s, topics 3.282 s, workspaces
1.792 s, opportunities 5.053 s, History 3.377 s, low-subscriber/high-view
2.611 s, long-form 9.487 s, recent discoveries 8.549 s, matching count 3.222 s
and Saved membership 0.064 s. These are one full-size trial, not latency promises.
The SQLite long-form comparison took 228.188 s versus PostgreSQL's 9.487 s.
All ten comparisons match every returned field and order. One channel's age
crossed a day boundary while the slower SQLite comparison ran; an immediate
native/SQLite recheck matched all recent fields including age. The initial
clock-boundary comparison is retained privately. Full-size native backup,
off-host verification and recovery are running; activation remains pending.
The new staged release contains only pinned Beacon `12ed83d` and Quill
`2997275` source, excluding unrelated concurrent working-tree edits.

Schema version 6 adds one covering video index for workspace/format reads and
two compact indexes for exact discovery/history counts. The explicit owner
upgrade preserves application records and remains separate from service startup.
The inactive full-size target completed the upgrade in 260 seconds; the three
indexes occupy approximately 874 MB. Production still selects SQLite.

Verification passed 48 real PostgreSQL checks (one skip), 387 shared tests with
137 subtests and 393 Beacon tests with 45 subtests. The new regression verifies
repeat upgrades, unchanged workspace/format results, an index-only video read,
and exact counts through a rolled-back write. Changed Python compiled.

Real Flet verification clicked Overview Retry, Channels Retry, Long-form,
Filters, minimum subscribers 50,000, Apply, Real Time, Find, Saved and Agent.
Inspected `indexes-04` through `indexes-12` screenshots show connection-error
recovery, unchanged channel statistics, an explicit empty result, all six screens
and the expected Agent setup message in the secret-free private clone. Logs
contain no application exception. Full-data timings/comparisons, complete native
recovery and production activation remain pending.

## Update: bounded opportunity reads and stable ties — 2026-10-01

The full-size PostgreSQL opportunity read no longer exceeds its 30-second
statement limit. Its eligibility/classification stage now precedes expensive
metric predicates, and provider avatars are decoded only for the selected page.
Zero-upload rows remain excluded safely. SQLite keeps its existing query plan.
Both backends now use channel IDs to order equal scores/discovery timestamps,
and PostgreSQL explicitly preserves SQLite's missing-value sort placement.
This resolves an actual full-data ordering mismatch without changing scores.

Five full-data comparisons passed for opportunities, History, low-subscriber /
high-view channels, recent discoveries and matching counts, including every
returned field and order. Native measurements were 3.1–4.9 seconds in the first
read trial, and 4.1–14.7 seconds while server verification was also running.
Long-form, workspace and Overview reads still require separate performance work.
Production continues selecting SQLite; migration activation remains pending.

Changed Python compiled; 47 real PostgreSQL tests passed (one additional skip),
387 shared tests passed with 137 subtests, and 393 Beacon tests passed with 45
subtests. New checks cover stable page boundaries, missing dates, zero uploads,
filter boundaries, deferred malformed payloads and classification-query plans.
The forced-parallel regression now disables debug worker creation inside each
trivial PL/pgSQL statement after upgrade; normal low-cost parallel thresholds
still verify that corrected helpers cannot produce a parallel outer plan.
An attempted simultaneous migration test run collided on its intentional
database-wide administrator lock; the isolated serial server run passed.

Real Flet verification clicked Channels, Long-form, Filters (minimum subscribers
50,000), Apply, Real Time, Find, Saved and Agent. Inspected `metrics-01` through
`metrics-10` captures prove retained statistics, loaded/empty states, explicit
fixture-error handling and successful Retry recovery. A fresh final-query instance
also verifies all six screens in `stable-01` through `stable-06`. Agent shows the
expected setup message in this private clone, which excludes production secrets.
No production process or canonical index was stopped or changed.

## Update: full-data queries exposed a PostgreSQL parallel-execution defect — 2026-10-01

The first real-data query gate stopped before backup, recovery or activation.
Several JSON/date compatibility helpers used exception handling but incorrectly
allowed parallel execution. PostgreSQL rejects their internal subtransactions.
Schema version 5 explicitly marks those helpers unsafe for parallel execution;
the inactive full-size copy has received this owner-only upgrade. Production
continues using SQLite.

A real regression reproduced the original error, then verified the upgrade,
malformed-data recovery and unchanged results. Verification passed 94 real
PostgreSQL checks, 387 shared tests and 393 Beacon tests. A separate real Flet
instance passed all six screens, the long-form filter, an empty result, an
explicit read-error card and successful Retry recovery. Private fixture records
were retained. Full-data query timings also exposed slow Overview, workspace
and opportunity reads; these still need measured fixes before proceeding.

## Update: full-size import committed and matched — 2026-10-01

The bounded host-local import completed at 20:32:38 UTC after 12,023.8 seconds.
All 30 legacy tables passed their complete typed-content comparisons before
commit: 265,217 channels, 5,091,451 videos and 39,403,879 video-history rows.
The four previously reviewed provider-description NUL normalizations remain
explicitly recorded in the protected audit; every other cell compares exactly.
The sealed SQLite recovery snapshot and the original live SQLite deployment
remain unchanged. The explicit owner upgrade to schema version 4 also passed.
Observed native database size is approximately 33.49 GB, separately from WAL,
archive and disk-reserve measurements.

The inactive target is now preparing its visibility maps and statistics before
actual restricted-reader timings and the ten full-data SQLite comparisons.
Native full-size backup, verified off-host transfer, complete recovery, real
cloud preview, final reconciliation and production activation remain pending.
Neither the save worker nor the production backup/transfer schedules is enabled.

Private operator preparation passed eight maintenance-plan checks, six final
activation checks and four transient-SSH readiness checks on the appropriate
hosts. Actual isolated Caddy HTTP/WebSocket tests proved the maintenance message,
existing stream closure and restored access. A separate Linux network namespace
proved that SYN-only gating refuses new requests while an existing request can
finish, then restores new connections after removing the exact owned rule.
The public proxy, production firewall and running application services were not
modified by those fixtures. Both operator plans refuse incomplete recovery proofs;
neither coordinated pause nor activation has been executed.

## Update: resumable off-host native recovery transfers — 2026-10-01

The Beacon transfer helper now downloads only completed native archives and
their matching manifests over the existing private SSH connection. Interrupted
downloads resume from their actual local byte count. Exact size/hash checks and
sealed local files precede server acknowledgement. Existing mismatching files,
path traversal, malformed manifests and insufficient disk reserve are refused;
unapproved files remain available for inspection. Local retention preserves
the requested number of verified sets (at least two) and all unmirrored files.
Failures publish private status and return a nonzero process result.

Changed Python compiled; 13 focused backup/transfer safety tests passed with
three subtests. Beacon regression passed 393 tests and 45 subtests, eight skips.
A newly generated real 32-table PostgreSQL fixture archive restored exactly,
then transferred from Oracle to the laptop through the actual SSH helper:
151,461 bytes, matching manifest/hash, accepted server acknowledgement.
A separate hidden Windows task ran that transfer successfully with result 0.
A second controlled task with an absent test helper published failure status
and returned result 1. Both owned test tasks were exported privately and removed
after completion; no production backup schedule was enabled.

Another fresh isolated Flet instance passed Overview, Channels, Real Time,
Find, Saved and Agent. Clicks were the five sidebar destinations after Overview.
Inspected `pull-01b` through `pull-06b` captures show the same retained
channel/four videos/workspace, ranked and Last 24h cards, full Find form, saved
card and Agent form/ledger; loading captures were followed by loaded captures.
No Web/Finder exception appeared. The laptop's historical ledger remains
unrelated to current Oracle service health. No shared-engine source changed.

All 39,403,879 video-history rows have reached the inactive full import, which
is still validating COPY relationships. Final verification/commit, full-size
backup/recovery, measured retention, cloud preview and activation remain pending.

## Update: guarded recurring native backups — 2026-10-01

Beacon now has an administrator-only scheduled backup wrapper and daily systemd
templates. They remain uninstalled/unenabled in production. Creation requires
all three PostgreSQL aliases and a measured archive size, keeps at least 8 GiB
reserve, serializes recovery operations, and monitors the backup's own named
database sessions. Interrupted files never receive an approval manifest. The
off-host acknowledgement must match the completed archive's name, size and hash;
the server verifies its hash again. Retention preserves the latest two confirmed
sets and every unmirrored set. Two unmirrored sets block another creation.
Failures publish a credential-safe private status and fail the service.

Python compiled; 14 focused backup/deployment checks passed with nine subtests.
Beacon regression passed 388 tests and 45 subtests, eight real-server skips.
An actual disposable PostgreSQL database passed scheduled native creation,
acknowledgement and exact 32-table recovery, preserving one channel/four videos.
The rendered service/timer passed systemd verification without installation.
The private rehearsal connection label also exposed an open configuration
transaction before snapshot selection; committing only that session setup fixed
the administrator wrapper. Its real 32-table backup/restore check then passed.
No shared-engine source or running application process changed for this fix.

A fresh isolated real Flet instance passed all six screens. Clicks were Channels,
Real Time, Find, Saved and Agent after Overview loaded. Inspected `schedule-01b`
through `schedule-06b` screenshots show one channel/four videos/one workspace,
the retained ranked card, a loaded Last 24h card, complete sweep form, saved
membership and the Agent form/ledger. Loading captures were followed by loaded
captures. Web/Finder output contained only the existing local Python warning;
the historical laptop Agent ledger does not measure Oracle service health.

The full bounded import remains in progress on Oracle, now copying video-history
rows. Full-size comparison, native recovery, final pause and activation remain
pending. Scheduled off-host transfer and actual size-based retention setup must
also be verified before enabling the new timer.

## Update: compressed recovery for the final coordinated pause — 2026-10-01

An explicit administrator helper can compress the original SQLite file after
all index activity is drained and its WAL is checkpointed. It requires both
an explicit writer-pause assertion and a continuous operator guard. Nonempty
WAL/journal files, a changed source, a lost pause, integrity failure or broken
relationships prevent a success manifest. It reads the source immutably and
does not change its bytes or settings. Recovery creates only a new off-host
file and verifies its original size/hash, full integrity and relationships.
This avoids rebuilding tens of millions of logical rows during maintenance.
It must never be used to copy an online database.

Changed Python compiled. Seven local safety checks passed; all eight checks
passed on Oracle, including actual PostgreSQL migration and final reconciliation
from recovered files. The relevant real-server migration/reconciliation gate
passed 30 tests. Shared regression passed 387 tests and 137 subtests, with
67 local real-server skips; Beacon passed 380 tests and 42 subtests, eight skips.
Whitespace checks passed.

A separate Flet instance used `beacon_quiescent_ui_20261001`, migrated and
reconciled from compressed/recovered fixtures. Overview retained one channel,
four uploads and a workspace; Channels loaded its card; Real Time loaded it
in both Last 24h and Newest after clicking Newest; Find showed the complete form;
Saved retained membership. Scrolling and clicking Analysis showed loading then
the accented/Chinese description and four uploads. Close and Agent opened the
ledger. Every `quiescent-01` through `quiescent-09b` screenshot was inspected.
The filename `quiescent-03b-realtime-empty.png` actually shows a loaded channel;
no empty-state claim is made from that capture. Output had only the existing
local Python support warning and no application exception.

The bounded full host-local import is running independently on Oracle. Its
producer memory was approximately 55 MB after ten minutes, compared with the
previous unbounded 1.4 GB. The live SQLite release and all index/collector services
remain unchanged. No final maintenance pause or PostgreSQL activation has begun.

The private operator alias procedure also passed a disposable real-server check:
all 30 legacy tables migrated from the same-inode alias, original bytes and the
existing sidecar remained unchanged, an independently recovered file matched
exactly, and a later source mutation invalidated the alias manifest. The initial
fixture with pending WAL was correctly refused; only its test checkpoint was
performed before retrying. The canonical live file was not opened by this check.

## Update: bounded administrator COPY buffering — 2026-10-01

The full ordered archive is complete: 5,417,124,928 bytes, with matching SHA-256
on the laptop and Oracle. Its source remained the sealed 30.9 GB recovery copy.
The host-local rehearsal exposed Psycopg/libpq buffering: the producer reached
23.2 million discovery rows while PostgreSQL had accepted about 13.2 million,
and Python retained approximately 1.4 GB. Only the inactive import was cancelled;
its transaction rolled back. The live SQLite app and scraper were untouched.

Both administrator migration paths now use a local COPY writer that splits
data into at most 128 KiB chunks and flushes each chunk before continuing.
There is no global driver modification. Existing commit/rollback and COPY error
handling remain intact. A deliberately blocked real server reproduced the
default writer's excessive buffering; the regression verifies bounded progress,
exact Unicode/escape preservation, rollback and subsequent connection reuse.

Changed Python compiled. Nine focused COPY/archive tests passed, followed by
all 90 real PostgreSQL connection/store/migration/reconciliation/importer/runtime/
save-job checks. Shared regression: 380 tests and 137 subtests passed, with
66 local real-server skips (run separately on Oracle). Beacon: 380 tests and
42 subtests passed, eight skips. Whitespace checks passed.

A fresh bounded-import/reconciled fixture passed real Flet navigation through
Overview, Channels, Real Time, Find, Saved and Agent. Overview showed one channel,
four videos and a workspace; both grids loaded the retained card; Find showed
its form; Saved retained membership. Scrolling and clicking Analysis showed
loading, then the accented/Chinese description and all four uploads. Close and
Agent opened the ledger. Every `bounded-01` through `bounded-09b` screenshot
was inspected; immediate loading captures were followed by loaded captures.
Web output contained only the existing local Python support warning and no
application exception. Agent's historical laptop ledger does not measure
Oracle health. Full-size retry, recovery rehearsal and activation remain pending.

## Update: host-local migration from a sealed archive — 2026-10-01

The second full rehearsal rolled back after both operator SSH tunnels reset.
The native PostgreSQL process remained running, with no restart or OOM evidence.
Production still selects SQLite. Bulk import now has an administrator-only path
that builds a compressed, ordered record archive from the verified recovery
snapshot and runs COPY on Oracle through its private Unix socket. A dropped
operator connection will not own that import process. The original recovery
SQLite file is opened read-only and remains unchanged.

The archive retains original cells, schema inventory, primary keys, sequence
high-water values and complete-stream hashes. Tables precede their dependants;
rows are strictly ordered by primary key. Import verifies the sealed file,
refuses populated targets, uses one transaction, checks relationships and
compares typed hashes for all 30 legacy tables before commit. Unsupported
provider-description characters still require explicit, privately audited
normalization. Truncated/tampered archives, reordered rows and interrupted COPY
roll back rather than publishing a success report.

Changed Python compiled. Twelve focused archive/stream tests passed on real
PostgreSQL. The broader real-server checks passed 62 connection/store/migration/
reconciliation/stream/archive tests, 16 importer/runtime/description tests and
10 save-job tests. Shared regression suite: 380 tests and 137 subtests passed,
64 real-server skips locally. Beacon: 380 tests and 42 subtests passed, eight
skips. Whitespace checks passed. Initial focused failures identified an incorrect
reserved table name and dependency order; both were repaired before this gate.

A fresh archive-imported and reconciled `beacon_archive_ui_20261001` fixture
passed real Flet clicks: Overview showed one channel/four videos/workspace;
Channels and Real Time loaded the retained channel; Find showed its full form;
Saved showed retained membership; scrolling and clicking Analysis showed loading
then the readable accented/Chinese description and all four videos; Close and
Agent opened the ledger. Every `archive-01` through `archive-09` screenshot was
visually inspected and remains outside Git in the temporary UI test directory.
There were no application exceptions. Agent's operational ledger reads historical
laptop state and is not evidence of Oracle health. Full-size host-local import,
native recovery and production activation remain pending.

## Update: safe reads of preserved provider metadata — 2026-10-01

A real PostgreSQL probe reproduced `UntranslatableCharacter` when a valid JSON
description contained an escaped NUL, even while reading an unrelated avatar.
Schema version 4 validates original JSON syntax without converting every string
to JSONB. Extraction removes active NUL escapes only in its temporary read value;
the stored JSON remains identical. Escaped literal backslashes survive. Invalid
documents are validated before normalization and remain rejected. Other malformed
Unicode returns a missing field rather than aborting the listing transaction.
The versioned schema-owner upgrade is explicit; normal app starts do not run DDL.
The inactive full target will upgrade after its import transaction commits.

Two new real regressions cover complete channel listings/avatars, unchanged stored
metadata, adjacent NULs, literal escapes, malformed syntax and repeated schema
upgrade with retained records. Fifteen focused tests and all 76 real PostgreSQL
tests passed. Shared suite: 380 tests/137 subtests passed, 57 skips. Beacon:
380 tests/42 subtests passed, eight skips. The first Beacon run had a global test
environment conflict with its mocked Autopilot policy and a local HTTP transport
failure; an unchanged isolated rerun with normal test settings passed. The real
Flet verification instance retained `SIGNAL_DISABLE_AUTOPILOT=1` throughout.
Changed Python compiled and whitespace checks passed.

Real Flet navigation against a fresh `beacon_json_ui_20261001` migration/catch-up
fixture with the original escaped-NUL JSON passed all six screens. Inspected
`json-01` through `json-08` screenshots show Overview membership/counts, loaded
Channels and Real Time cards, the complete Find form, loaded Saved membership,
Saved > Analysis loading and its readable Unicode description/four videos, then
Close > Agent's ledger. Output had no application exceptions. The screenshots
are temporary evidence outside Git, not a production-size performance benchmark.
The full import continues while the live SQLite services remain active.

## Update: verified recovery and audited descriptions — 2026-10-01

The off-host recovery snapshot completed its original full integrity and
foreign-key checks, fsync and SHA-256 manifest at 12:30 UTC. It contains all
30 legacy tables, 265,217 channels and 5,091,451 videos in 30,913,024,000 bytes.
Its verified recovery files remain in protected `D:/BeaconBackups/20261001`,
outside Git. The canonical Oracle SQLite file was not changed or paused.

The first full PostgreSQL import rolled back safely because PostgreSQL rejects
NUL characters in text. A full source-text scan found exactly two channel and
two video descriptions affected; every other text column had zero NULs.
Administrator migration/catch-up remains strict by default. Explicit
`--normalize-provider-descriptions` allows only these two description columns,
with bounded, private audits retaining original UTF-8 bytes and the source file
hash. Every other cell remains subject to exact typed-content comparison.
Provider and signed-import PostgreSQL writes retain the original description
in raw metadata without mutating the provider object or signed bundle. SQLite
behavior and the sealed snapshot remain unchanged.

Changed Python compiled and whitespace checks passed. Focused real PostgreSQL
verification passed 43 tests, followed by all 74 real PostgreSQL/store/importer/
runtime/save-worker/migration tests together. Shared suite: 380 tests/137 subtests passed,
55 skips; Beacon: 380 tests/42 subtests passed, eight skips. The regressions
cover strict rejection, explicit normalization, recoverable original bytes,
unchanged signed input, bounded audits and idempotent catch-up.

A fresh migrated-and-reconciled `beacon_descriptions_ui_20261001` fixture passed
real Flet clicks: Overview showed one workspace/channel and four videos;
Channels and Real Time loaded the retained channel; Find showed its full form;
Saved loaded its membership; Saved > Analysis showed loading then the readable
description and four videos, retaining accented and Chinese text; Close and
Agent opened its ledger. Inspected `description-01` through `description-08`
screenshots remain in the temporary test directory outside Git. Output had no
app exceptions. The Agent ledger describes historical laptop state, not Oracle
health. Native PostgreSQL is still inactive; full import/recovery/query gates
remain before coordinated production cutover.

## Update: bounded administrator scan memory — 2026-10-01

SQLite administrator readers and recovery-file validators now use a bounded
512 MiB connection-local page cache. Full integrity and foreign-key checks remain
mandatory. The setting is not persisted to the source or applied to normal app
connections. This addresses repeated page reads seen during the full off-host
validation; the already-running receiver retains its original settings.

The new regression proves source writes are denied and cache settings do not
persist. All 25 streaming/migration/reconciliation tests passed on Oracle's real
PostgreSQL test environment. Full shared suite: 377 tests/137 subtests passed,
51 skips; Beacon: 380 tests/42 subtests passed, eight skips. Python compilation
and whitespace checks passed. A fresh migrated-and-reconciled fixture in
`beacon_cached_ui_20261001` passed real Flet clicks through all six screens.
Inspected `cached-01` through `cached-06` images show workspaces, loaded channel
results, Find/Saved/Agent controls and retained membership. Saved > Analysis
showed loading (`07`) then the four preserved video records (`08`). Screenshots
remain in the temporary test directory outside Git. No app exceptions appeared.
Full-size import and production activation remain pending.

## Update: ordered verification reads — 2026-10-01

Administrator migration now provisions byte-order indexes for text primary
keys after its empty-target checks. Exact-content verification prefers ordered
reads and restores the caller's query-planner setting on success; rollback
restores it on failure. These indexes avoid sorting millions of identifiers
under a different database collation. Normal app starts do not change the schema.

Twelve migration/snapshot tests and all 66 real PostgreSQL tests passed. The
shared suite passed 376 tests/137 subtests (51 real-server skips locally);
Beacon passed 380 tests/42 subtests (eight skips). Changed Python compiled.
A fresh migrated-and-reconciled fixture in `beacon_ordered_ui_20261001` passed
real Flet navigation: Overview showed three retained workspaces; Channels and
Real Time finished loading the channel; Find showed its complete form; Saved
showed membership and background-refresh controls; Agent showed its form and
local historical ledger. Saved > Analysis loaded four videos, including the
updated Unicode title. Inspected `ordered-01` through `ordered-08` screenshots
remain under `C:/WINDOWS/TEMP/beacon-pg-store-ui/`, outside Git. No application
exception appeared. This is a fixture regression check, not a full-size benchmark.

The off-host recovery file has received all 30 tables and rebuilt its indexes;
integrity/relationship validation is still running. No success manifest has
been published yet. The guarded full PostgreSQL import waits for that manifest.
The live app remains on SQLite. PostgreSQL's staged state directory points to
`/var/lib/beacon/data`, preserving the existing radar queue and classification
model in place. No live service has switched databases.

## Update: final catch-up verified — 2026-10-01

`postgres_reconcile` compares all 30 legacy tables by primary key and typed
per-row SHA-256, applies differences in one transaction and verifies every
table before commit. Native row hashes use the same byte framing as migration
hashes, including Unicode, nulls, 64-bit integers and floating-point values.
The operator must explicitly confirm that writers are paused; a populated
saved-channel request queue refuses reconciliation. Sequence clocks never rewind.
Unrepresentable late records roll back earlier valid changes.

Tests reproduced and repaired a unique-name swap conflict. Changed rows with
secondary uniqueness are staged before old labels are replaced; cascaded children
are restored from the final snapshot. Lab NO ACTION relationships defer checks
temporarily inside the transaction and regain their original constraint timing
before commit. Seven real catch-up tests passed, including 5,500 batched updates/
inserts, 2,000 removals, workspace/Lab swaps and retained notes/ideas. All 65 real
PostgreSQL/store/importer/runtime/migration/worker tests passed together. Final
shared regression: 376 tests/137 subtests passed, 50 skips. Beacon: 380 tests/42
subtests passed, eight skips. Changed Python compiled and whitespace checks passed.

A fresh 30-table fixture was migrated and caught up into
`beacon_reconcile_ui2_20261001`. Real Flet clicks and inspected
`reconcile-final-01` through `reconcile-final-06` screenshots showed all six
screens, three retained workspace memberships and the updated Saved channel.
Saved > Analysis showed loading (`07`) then four videos with the edited Unicode
title (`08`). Screenshots remain outside Git in the temporary test directory.
Overview's census cache and Agent's historical local collection ledger are not
measurements of the production Oracle database. Output had no app exceptions.

The full off-host recovery copy is running, with the canonical SQLite services
active. Native PostgreSQL's empty 32-table schema (30 legacy tables plus schema
metadata and save jobs) and restricted roles passed real permissions checks:
writer DML allowed, reader DML denied, neither role allowed schema changes.
Release `20261001T101400Z-postgres-stage` is staged only; the running release
pointer is unchanged. Additional 100,000-row history samples supersede the first
30–35 GB size estimate: about 45 GB is now a working estimate before safety
allowance. Actual full-size disk, performance and recovery checks remain mandatory.

## Update: off-host snapshot path verified — 2026-10-01

Server deployment templates now pass optional private PostgreSQL settings to
all five index services and include a separately enabled save worker with only
YouTube keys, cooperative termination and no collector signing key. The pinned
server requirements include Psycopg. Before switching release pointers, the
installer checks all running index processes and radar's explicit database path;
mixed SQLite/PostgreSQL activation is refused. Without the private activation
file, SQLite upgrades behave as before. Active importer/save-worker releases are
also retained during upgrades; only an already active save worker is restarted.

Deployment checks: changed Python compiled; focused tests passed 34 tests/six
subtests, eight skips; full Beacon passed 380 tests/42 subtests, eight skips.
Linux `bash -n` and rendered `systemd-analyze verify` passed (only pre-existing
Oracle monitoring-unit permission warnings). Running the guard on Oracle reported
“SQLite deployment retained.” No live unit was installed or restarted.
A fresh isolated Flet instance was clicked through Overview, Channels, Real Time,
Find, Saved and Agent. Inspected `deploy-01` through `deploy-06` images show the
complete screens and retained test membership; no application exceptions. These
are regression checks, not production worker activation. The full off-host
recovery snapshot has started; verification and PostgreSQL migration are pending.

`sqlite_snapshot_stream` exports a pinned read transaction through authenticated
SSH and constructs a new recovery file off-host. Framed compressed records have
content hashes, table counts and sequence positions. Indexes, integrity and
relationships are checked before publishing the success manifest. Interrupted
or corrupted transfers leave an incomplete file with no success manifest;
existing destinations are never overwritten. Concurrent writes remain consistent
with the snapshot's original point in time. The online backup path now explicitly
pins its read transaction too.

Five streaming tests and eleven snapshot/migration/recovery tests passed against
the private PostgreSQL test environment. Full shared regression: 376 tests and
137 subtests passed, 43 skips. Beacon: 374 tests and 36 subtests passed, eight
skips. Changed Python compiled and Git whitespace checks passed.

A streamed complete 30-table fixture was migrated into `beacon_stream_ui_20261001`.
Real Flet clicks covered Overview, Channels, Real Time, Find, Saved and Agent;
`stream-01` through `stream-06` screenshots show the screens and preserved Saved
membership. Clicking Saved's Analysis produced the loading state (`stream-07`)
and four preserved video records (`stream-08`). Screenshots are outside Git under
`C:/WINDOWS/TEMP/beacon-pg-store-ui/`. Overview used the earlier fixture count
cache; the Agent ledger is local historical state, not Oracle status. After an
internet address change, the existing OCI /32 updater restored SSH; the isolated
app recovered after reconnecting its test tunnel. Output had no app exceptions.

Oracle now has an unused native PostgreSQL 16 cluster with checksums, Unix-socket
access only, restricted owner/writer/reader roles and private staged settings.
Live services still use SQLite. A limited representative import suggests roughly
30–35 GB for PostgreSQL; this is an estimate, not a full-size benchmark. With
about 62 GB free on Oracle and a roughly 32 GB canonical SQLite file, a second
full SQLite recovery copy belongs off-host. The planned destination is
`D:/BeaconBackups/20261001/`; neither canonical database is replaced by this step.
Full migration, recovery, performance and reconciliation are still outstanding.

## Update: administrator migration and recovery tools verified

`scripter.research.postgres_migration` provides explicit snapshot, initialize,
migrate, backup and restore commands. Services never run these automatically.
SQLite snapshots use the online backup API, finish as standalone rollback-mode
files, pass integrity checks and receive a file-hash manifest. Migration requires
that manifest, refuses populated targets/schema drift/broken relationships,
copies columns by name and preserves every cell through typed hashes over ordered
primary keys. IDs and SQLite sequence high-water marks are preserved. Unsupported
text/numbers cause a transaction rollback, not silent rewriting.

Native PostgreSQL backups use an exported repeatable-read snapshot, `pg_dump`
custom format, exact per-table hashes and private credential environment variables.
Recovery uses `pg_restore` in a single transaction into an empty database only,
then compares every restored record with the backup manifest. Roles and deployment
settings still require their separate recovery procedure. Incomplete output is
never marked as a completed backup.

All ten migration/snapshot/recovery tests passed on PostgreSQL 16.15, including a
real native dump and restore, Unicode/newlines, legacy video column ordering,
sequence positions, modified snapshots, source WAL isolation, rollback and overwrite
refusal. Shared regression: 371 tests/137 subtests passed, 42 real-server skips;
Beacon: 374 tests/36 subtests passed, eight skips. Changed Python compiled.

A complete 30-table synthetic SQLite snapshot was migrated to the separate
`beacon_migration_ui_20260930` schema. Real Flet browser clicks and inspected images
under `C:/WINDOWS/TEMP/beacon-pg-store-ui/` showed Overview's one channel/four videos/
one membership (`migration-01-overview.png`), the preserved Saved channel
(`migration-02-saved.png`), its 16.7x ranking in Channels/Real Time
(`migration-03-channels.png`, `migration-04-realtime.png`), and complete Find/Agent
forms (`migration-05-find.png`, `migration-06-agent.png`). Output had no application
exceptions. No live data has been copied or migrated yet. Full-size disk,
performance, off-host recovery and coordinated cutover checks remain.

## Update: durable save screen verified

Beacon detects durable-save support through Finder health. PostgreSQL saves
record a request before returning, show persisted progress, retain the channel
when analysis fails, and expose Retry. Shelf refresh queues a bounded background
check rather than calling YouTube during the browser request. Legacy SQLite
behavior remains unchanged. The link input control survives background redraws.
Finder read tokens cannot enqueue, retry, or request refresh writes.

Verification: changed Python compiled; 374 Beacon tests and 36 subtests passed
(eight skips); 367 shared tests and 137 subtests passed (36 real-server skips).
All 48 real PostgreSQL tests ran; a new refresh test initially omitted the worker's
lease-release step, was corrected to exercise the actual worker, and the 19
save/runtime tests then passed. The other 29 passed in the full real-server run.
The Impeccable detector returned no findings for the changed UI/helper files.

Real Flet web evidence under `C:/WINDOWS/TEMP/beacon-pg-store-ui/` was inspected:

| Click/action | Screenshot | Proof |
|---|---|---|
| Save Disaster Breakdown | `save-ui-03-request-recorded.png`, `save-ui-04-queued.png` | Disabled while recording, then pending analysis and released input/button |
| Deliberately fail the isolated worker, reload browser | `save-ui-05-reconnected.png` | Saved channel kept; persisted failure with Retry |
| Click Retry, run worker against YouTube | `save-ui-06-retry.png`, `save-ui-07-ready.png` | Request accepted; analysis becomes Ready |
| Click Refresh live | `save-ui-08-refresh-queued.png` | Brief recording state; background job recorded, no false freshness timestamp |
| Save a foreign URL | `save-ui-12-simple-error.png` | Simple instruction, link retained, button usable |
| Overview, Channels, Real Time, Find, Agent | `save-ui-11-final-overview.png`, `save-ui-13-channels.png`, `save-ui-14-realtime.png`, `save-ui-15-find.png`, `save-ui-16-agent.png` | All six screens render; opportunity filtering and forms remain intact |
| Save a valid link again in the final build | `save-ui-17-final-ready.png` | Input clears after recording, persisted jobs finish Ready, one shelf membership |

No application exceptions appeared in the isolated instance's output. Overview
uses its earlier synthetic count snapshot; the local Agent operational ledger
does not represent live Oracle service state. The earlier web fast path replaces
the unreliable native desktop focus checks described below. These checks do
not mean production has switched: the live database is still SQLite.

## Update: research store and schema verified

The shared engine now has an explicitly selected PostgreSQL research store and
a versioned schema covering all 30 application tables found in the cloud index.
Literal SQLite paths retain their existing behavior, including Lab session
databases. No live records have been migrated and production still uses SQLite.

The store preserves large counters, row access, generated IDs, duplicate-safe
discovery writes, case-insensitive workspace names, Saved views and reversible
exclusions. Query comparisons include channel scores and combined filters.
PostgreSQL schema initialization is a separate explicit step; normal service
starts do not run schema changes. SQL changes are explicit, with a lexical
parameter adapter and server functions for existing date/JSON contracts.

Verification: 25 tests passed against real PostgreSQL in the private Oracle
test cluster; the shared suite passed 363 tests and 119 subtests (17 real-server
tests skipped locally and run separately). Beacon passed 366 tests and 36
subtests, with eight skips. Changed Python compiled and diff checks passed.

A separate real Flet web instance used a synthetic PostgreSQL schema, Finder
port 8798, separate token/log paths and disabled Autopilot. Browser clicks and
visually inspected screenshots in `C:/WINDOWS/TEMP/beacon-pg-store-ui/` proved:

| Action | Screenshot | Result |
|---|---|---|
| Open Overview | `web-01-overview.png` | One synthetic channel and empty niche/video counts |
| Click Channels; wait | `web-02-channels.png`, `web-03-channels-result.png` | Loading, then the test channel and its 16.7x score |
| Filters; maximum subscribers 500; Apply | `web-04-filter.png`, `web-05-filter-empty.png` | Loading, then clear empty-result state |
| Click Real Time; wait | `web-06-realtime-loading.png`, `web-07-realtime-result.png` | Test channel with discovery freshness separate from channel age |
| Click Find | `web-08-find.png` | Complete sweep controls |
| Click Saved | `web-09-saved.png` | Link field and Nothing saved yet |
| Click Agent; wait | `web-10-agent.png` | Question field, provider controls and ledger |
| Disconnect only the private test tunnel; Refresh | `web-11-database-error.png` | Readable connection error and Retry; no blank screen or credentials |
| Restore test tunnel; Retry | `web-12-retry-recovered.png` | Query succeeds again with the applied empty filter |
| Clear all filters | `web-13-cleared-result.png` | The test channel returns |

There were no application exceptions in the web instance's output. An initial
desktop check used `_win.ps1`, but focus interference made later desktop clicks
unreliable; those screenshots are not counted as workflow proof. The documented
web fast path above completed the checks. The local Agent ledger still reads
legacy operational state; this is not evidence about live Oracle worker health.

Importer integration, full/read role checks, durable save jobs, representative
performance, full migration and recovery remain required before live cutover.
The historical first milestone below describes the earlier connection-only work.

## Update: signed PostgreSQL importer verified

The importer now selects the correct transaction mechanism for each backend.
PostgreSQL serializes competing importer sessions through a transaction-scoped
advisory lock while unrelated operator writes remain free to commit. Its normal
import sessions run no SQLite PRAGMAs, WAL worker or schema-owner DDL. Version 2
adds timestamp merge helpers preserving SQLite's string-order and NULL rules.
SQLite batching and WAL maintenance retain their existing behavior.

All 29 real PostgreSQL connection/store/importer tests passed. Importer tests
proved signed discovery and organization imports, duplicate handling, wrong-key
rejection, 64-bit counters, commit-before-ack visibility from a separate reader,
and recovery from an actual database error in the middle of a three-bundle group.
An operator insert committed while a different bundle transaction remained open.
Shared regression: 363 tests and 119 subtests passed, 21 real-server skips run
separately. Beacon: 366 tests and 36 subtests passed, eight skips.

A signed synthetic bundle was then imported into the isolated web-test schema.
After restarting only the owned test process tree, browser clicks through
Channels (`import-02-channels.png`) and Real Time (`import-03-realtime.png`) showed
both sample channels, including the newly imported card and its discovery time.
Find, Saved and Agent clicks showed their complete forms in `import-04-find.png`,
`import-05-saved.png` and `import-06-agent.png`. Overview's existing count cache
was refreshed explicitly in the test setup; the reopened browser showed two
channels in `import-07-overview-updated.png`. All screenshots are under the same
temporary evidence directory above. Web output contained no application
exceptions. These checks do not constitute a production save or migration.

## Update: runtime ownership and permissions verified

Shared-index readers recognize the public PostgreSQL target without treating it
as a missing local file. They can use a separate restricted read-role connection.
Lab SQLite sessions remain separate. Radar queue/model files stay in the explicit
operational state directory. PostgreSQL schemas have separate score caches.
Competitor title aggregation and recent-yield telemetry now support PostgreSQL.
Maintenance uses PostgreSQL catalog checks and native vacuum; SQLite backup and
restore actions refuse PostgreSQL targets rather than opening a misleading file.
Native PostgreSQL backup/recovery tooling is still a later migration requirement.

There are 37 real-server connection/store/importer/runtime tests: the first 35
passed together, and all eight runtime checks passed again after adding yield
and cache-isolation cases. The checks include Finder full/read/unauthorized HTTP
tokens and a native restricted database role that rejects writes even when the
transaction itself is writable. Shared regression passed 364 tests and 119
subtests, with 28 real-server skips; Beacon passed 366 tests and 36 subtests,
with eight skips. Compilation and diff checks passed.

The isolated real web app was restarted using the current source. Clicks through
Overview, Channels, Real Time, Find, Saved and Agent were captured and visually
inspected in `runtime-01-overview.png` through `runtime-06-agent.png` in the same
temporary evidence directory. Overview showed two sample channels, both channel
grids showed the correct cards, Find/Saved showed complete forms and empty Saved
state, and Agent showed its controls/ledger. No web application exception was
found. The connection-error/retry workflow was already checked in the store gate.

A read-only inventory also compared all 30 live Oracle tables with the test
schema. Column names match; legacy videos have one historical column at a
different position. Migration must map columns by name, never use blind positional
copies. No live records were changed or copied by this inventory.

## Update: durable save engine verified

Schema version 3 adds persisted channel save requests. Finder accepts a request
only after commit; a separate worker resolves it, commits Saved membership,
then analyses up to 30 uploads outside write transactions. The request survives
browser/process loss. Leases have owner fencing, bounded retry budgets and
recovery after interruption. Duplicate active links reuse their request; channel
membership is idempotent. Analysis failures preserve saved membership and expose
readable errors without raw provider/driver diagnostics. Classification is scoped
to the saved channel; it does not scan or relabel the whole index.

The Saved workspace keeps its existing automatic upkeep default. Shared Finder
reads invalidate their derived caches when the separate worker's persisted state
changes. Full/read authorization also applies to enqueue/retry tools. The dedicated
worker has bounded provider HTTP timeouts and is independent of browser sessions.
Its production service and the new UI progress controls remain to be deployed.

Verification passed 47 real PostgreSQL tests together, plus all ten save checks
again after preserving the upkeep default. Shared regression passed 367 tests
and 137 subtests (35 real-server skips run separately); Beacon passed 366 tests
and 36 subtests, eight skips. Compilation and diff checks passed.

The operator's actual `@DisasterBreakdown` link was tested against the isolated
PostgreSQL schema with real YouTube reads. It committed a queued request in
0.359 seconds, then committed membership and reached Ready with 30 uploads.
This is one development measurement, not a full-load production guarantee.
No live index rows were affected. A restarted real web instance displayed the
Saved card (`jobs-02-saved-channel.png`); clicking Analysis showed its description
and indexed upload evidence (`jobs-07-analysis-loading.png`,
`jobs-08-analysis-ready.png`). Clicks through Overview, Channels, Real Time, Find
and Agent also passed (`jobs-01-overview.png`, `jobs-03-channels.png` through
`jobs-06-agent.png`). All screenshots were inspected; web output had no application
exception. Screenshots remain in the temporary evidence directory above.

## What this means in plain language

Beacon now has the first tested building block for talking to PostgreSQL.
Successful changes are committed; interrupted changes are rolled back.
Connections close when the work finishes. A slow query or conflicting write
has a time limit. Read-only transactions reject writes.

The live app still uses SQLite. This milestone does not resolve its busy-index
save error. The tables, research queries, Finder operations and importer still
need a verified PostgreSQL port before switching the live app.

## Scope and boundaries

- Branch in both source repositories: `codex/postgresql-foundation`.
- Shared engine: `scripter/research/postgres.py`, optional
  `requirements-postgres.txt`, and `tests/test_postgres.py`.
- The module requires explicit construction. No environment variable or existing
  YouTubeStore constructor silently selects it, creates a second live index,
  or changes Production Lab's SQLite session behavior.
- SQL uses native Psycopg parameters; there is no blanket SQLite SQL rewrite.
- Read-only transactions are a database safeguard, not a replacement for
  restricted database roles or Finder full/read token authorization. Those
  production integration checks remain necessary.
- Connection strings are excluded from object representations. Connection
  failures have generic messages rather than exposed driver diagnostics.

## Real PostgreSQL verification

Ubuntu PostgreSQL 16.15 binaries were extracted into
`/var/tmp/beacon-pg-foundation` on Oracle, without installing a system service
or altering the live release/virtual environment. A separate virtual environment
contained Psycopg. The synthetic cluster used a private Unix socket, no TCP
listener, 32 MiB shared buffers and a 12-connection ceiling. Its parent and
socket directories were mode 0700. The cluster was stopped after testing.
Only the new module and its tests were uploaded; no live database was copied.

All **13 tests passed** against the real server:

- successful writes commit and connections close;
- a raised exception rolls back a pending write and closes the connection;
- read-only connections reject writes;
- an independent insert commits while another insert transaction remains open;
- another reader sees the committed insert but not the uncommitted insert;
- a conflicting insert expires under the configured 150 ms lock limit;
- a three-second query is cancelled under the configured 200 ms query limit;
- query, lock and idle-transaction limits are applied to each connection;
- invalid connection diagnostics do not expose the test password;
- configuration rejects missing strings, invalid flags and unbounded timeouts.

These small synthetic checks prove connection behavior. They are not a
whole-index performance benchmark or a successful migration rehearsal.

## Existing application checks

Changed Python compiled successfully on the shared local Python 3.10 runtime.
The shared pytest suite passed **360 tests and 115 subtests**, with eight real
PostgreSQL tests skipped locally because no local test DSN was supplied. Those
eight tests passed separately against the real Oracle test cluster above.
Beacon's pytest suite passed **366 tests and 36 subtests**, with eight skips.

A separate real Flet desktop instance used a newly initialized empty scratch
database, Finder port 8797 and a separate token-file path. Autopilot was disabled
with `SIGNAL_DISABLE_AUTOPILOT=1`. The user's running app and cloud services were
not stopped. `_win.ps1` performed the navigation and screenshot capture; every
screenshot below was visually inspected. No application exception appeared in
the instance's output (only the existing Python support warning).

Screenshots are local generated evidence, not committed source:
`C:/WINDOWS/TEMP/beacon-pg-foundation-ui/`.

| Action | Screenshot | What was visible |
|---|---|---|
| Open the separate app on Overview | `01-overview.png` | Connected Finder, empty index counts and Niche Watch cards |
| Click Channels | `02-channels.png` | Research controls and the initial loading state |
| Wait for Channels to finish | `03-channels-empty.png` | Zero channels and the explicit empty-result message |
| Click Real Time | `04-realtime.png` | Window/filter controls and the initial loading state |
| Wait for Real Time to finish | `05-realtime-empty.png` | Zero newest channels and the empty-result message |
| Click Find | `06-find.png` | Complete subject field, Start sweep button and browse action |
| Click Saved | `07-saved.png` | Complete link field, Save & analyse button and Nothing saved yet |
| Click Agent | `08-agent.png` | Question field, provider controls and the work ledger |

No new PostgreSQL UI workflow exists yet, so this was a navigation/regression
smoke test, not a claim that saving or sweeping uses PostgreSQL. The Agent ledger
in this local instance reads the laptop's legacy operational status; it does not
prove the Oracle worker is stuck. All four live Oracle services were separately
confirmed active after the database tests.

## Next implementation gate

Port the research-index schema and store operations explicitly, retaining
SQLite session compatibility. Compare results on synthetic fixtures and then
representative data, including rankings, exclusions, Saved membership and
history. Add durable save jobs with analysis outside the save transaction.
Verify Finder permissions and importer savepoints/commit-before-ack behavior.
Full-size migration, backup/recovery rehearsal and production cutover remain
the later gates in `POSTGRESQL_PLAN.md`.
