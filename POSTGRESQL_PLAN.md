# Beacon: reliable cloud foundation and PostgreSQL plan

Planning baseline: 2026-09-30. This document is a plan, not a completed migration.

## Goal in plain language

Build a powerful private research tool now, with a structure that can support
customers later. The first product milestone is dependable channel saving while
collection continues. A saved request must survive a closed browser or a worker
restart, and the app must distinguish a queued request from a completed save.

## Recorded starting point

- Live Oracle release: `20260930T155438Z-session-cleanup`.
- All 129 deployed Python source files compared matched their laptop copies.
- Shared-engine baseline commit: `9435743805d514297a26e4695212197894dc7087`.
- Both repositories use baseline branch `codex/cloud-baseline-2026-09-30`.
- The canonical cloud index is approximately 33 GiB. The server has two CPU
  cores, approximately 12 GiB RAM, and 64 GiB free disk at inspection time.
- The local/laptop index is legacy state, not the cloud app's current index.
- Secret-pattern scanning of eligible source files found no matches; private
  environments, tokens, databases, logs, model files and generated state are
  excluded from Git. A pattern scan does not guarantee the absence of every secret.
- Prior regression results on this source: 366 Beacon tests plus 36 subtests,
  with 8 skipped; 355 shared-engine tests plus 92 subtests. Baseline recording
  itself changes no runtime behavior and does not redeploy Oracle.

## What needs adapting

| Part | Its job | Migration work |
|---|---|---|
| Beacon screens | Display data and accept user actions | Continue through FinderClient; introduce clear queued/analysis states |
| Finder and store | Authorize requests, query and write the index | PostgreSQL connections, SQL, schema migrations and equivalent result contracts |
| Workspaces and metrics | Saved membership, groups, history and scores | Preserve memberships, identifiers, relationships and computed results |
| Radar and collection | Discover and organize data continuously | PostgreSQL writes, bounded batches and sensible worker concurrency |
| Signed importer | Verify cloud bundles and commit their contents | Preserve signature verification, savepoint isolation and commit-before-ack |
| Agent gateway | Approved AI actions and their audit history | Preserve full/read authorization, reversible exclusions and durable auditing |
| Deployment | Run services and publish versions | PostgreSQL provisioning, private connection settings, repeatable releases and health checks |

The shared engine also serves Production Lab. Its discovery-index access must
support PostgreSQL without silently converting its independent session database
or weakening its read-only role. Small operational SQLite queues are separate
from the canonical index; whether to move them is an explicit later decision.

## Sequence and completion gates

### 1. Preserve and document the current version

Record both source repositories on reviewable branches, with no private state.
Record the live release and paired engine revision. Choose an off-host backup
destination and a verified recovery procedure before a production database move.

### 2. Build an isolated PostgreSQL development version

Start with synthetic data, not a casual copy of the live index. Inventory every
SQLite-specific query and writer, including the importer and Agent tools.
Define the PostgreSQL schema and connection configuration. Adapt explicit SQL
and result contracts rather than relying on blanket string substitutions.

Known portability areas include `?` parameters, PRAGMAs, `INSERT OR IGNORE`,
SQLite schema inspection, autoincrement/lastrowid behavior, JSON/date functions,
aggregate functions, and SQLite row objects. Preserve IDs, timestamps, missing
values and exclusions; inspect foreign keys and sequence positions explicitly.

Keep SQLite support during development so the existing app and Lab contracts
remain testable. PostgreSQL access stays private to the services. Browser users
do not receive database credentials.

Gate: equivalent queries and writes pass against synthetic data on both
backends, including read-role denial, concurrent collection and duplicate jobs.

### 3. Make save requests durable and analysis asynchronous

1. Persist the request and return its job ID; display Queued only after commit.
2. Resolve the channel and file it into the Saved workspace with an idempotent
   write; display Saved only after that membership commits.
3. Fetch statistics and upload evidence in a background analysis job.
4. Display Analysis pending, Ready, or a readable failure with an appropriate
   retry action. Refresh state after browser reconnect from persisted job data.

Jobs need leases, bounded retries, safe duplicate handling and recovery after
worker interruption. Network calls and heavy scans stay outside write
transactions. Background batches must not monopolize database resources.
Analysis must not relabel unrelated channels or drop a user's saved request.

Gate: close/reopen the browser, interrupt/restart a test worker, submit the
same link twice, and simulate provider/database failures while collection runs.
Proposed target: accept a save request within two seconds under the agreed
test workload. This is a target, not a measured result or guarantee.

### 4. Rehearse the full data migration

Plan sufficient disk for PostgreSQL tables, indexes, WAL, temporary import
space and the approved recovery copy. Current free space alone does not prove
that the full rehearsal fits. Measure PostgreSQL size with representative data
before allocating the full migration environment.

2026-10-01 decision: retain the canonical SQLite file on Oracle, provision an
unused private native PostgreSQL cluster there, and build the consistent recovery
snapshot under `D:/BeaconBackups/20261001/` on the laptop. Representative samples
initially suggested 30–35 GB; expanded history samples now suggest about 45 GB
before safety allowance. The full rehearsal must measure actual size.
Do not allocate another full SQLite copy on Oracle's approximately 62 GB free
space. Keep recovery artifacts private and outside Git.

Use a consistent, planned database snapshot. Compare every table's counts,
identifiers, relationships and representative records, not just channel/video
totals. Compare metrics, filters, rankings, Saved membership, exclusions and
history. Track collector/importer changes made after the snapshot and define
how they are reconciled during cutover.

Verified final catch-up tooling compares the final checked snapshot with the
inactive PostgreSQL target, applies differences and checks all 30 legacy tables
before commit. It explicitly requires a writer pause. Until the full rehearsal
measures the pause duration, do not promise an instant cutover. Preserve the
SQLite source and recovery copy. Any application rollback after cutover must
keep the current PostgreSQL data; switching to stale SQLite would lose new writes.

Gate: full workflow, query-performance and recovery rehearsal passes. Any data
or behavior mismatch is resolved before scheduling production cutover.

### 5. Switch the live app deliberately

Use a scheduled cutover with a defined write pause and in-flight job drain.
Switch every canonical-index writer and reader together; avoid two live indexes
silently accepting different writes. Verify all screens, real saving, importer
acknowledgements, radar progress and Agent permissions before resuming normal
work. Define how post-cutover writes are recovered if rollback is necessary;
switching back to an old database alone would lose those writes.

## Work after the foundation

Improve classification and ranking against labeled examples, then fresh data,
growth comparisons, niche evidence and useful alerts. Customer accounts,
private workspaces and usage limits follow once the private research workflow
is dependable. PostgreSQL is infrastructure; it does not by itself improve
classification accuracy, data coverage or the usefulness of the interface.

## Open decisions

- Recovery retention policy after successful cutover.
- Disk and resource budget for parallel SQLite/PostgreSQL migration work.
- The exact performance workload and acceptance measurements.
- How existing collection/import progress is reconciled at cutover.

These decisions must be resolved before the steps that depend on them, while
independent schema and code inventory work can continue.
