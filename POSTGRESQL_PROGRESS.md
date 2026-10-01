# PostgreSQL foundation — 2026-09-30

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
