# PostgreSQL foundation — 2026-09-30

## Laptop backup connections hidden — 2026-10-04

The recurring visible OpenSSH terminal belonged to the laptop's
`BeaconPostgresBackupTransfer` task. Its parent already used `pythonw.exe`,
but both metadata and streaming SSH subprocesses lacked `CREATE_NO_WINDOW`.
Both launch sites now apply that Windows flag (zero on other platforms).
The verified scheduled operator copy under
`D:/BeaconBackups/PostgreSQL/operators/schema6/` received the same patch.
The existing identified connection window was hidden without killing it;
the laptop task was subsequently reloaded to apply the flag to every retry.
Partial recovery files were preserved. No cloud service, live index, schema,
retention policy or schedule changed.

Both Python files compiled; 14 backup/transfer tests passed, including resumable
downloads, corruption rejection, acknowledgment ordering and the two hidden
launch sites. A real `pythonw.exe` probe exercised local OpenSSH and a held-open
console child: no child window, exact streamed output, successful completion.
The actual scheduled task relaunched using `pythonw.exe`; desktop inspection
confirmed the unwanted terminal was absent. The backup remains asynchronous;
these checks do not claim that today's whole archive transfer has finished.

## Saved completion notices hidden — 2026-10-03

The user requested removal of persistent Ready notifications. Beacon now filters
completed requests before the progress panel's display limit and omits the panel
when none remain. Queued/working requests and failures with Retry remain visible;
durable request records and the six-hour refresh worker are unchanged.
Two regressions cover all-complete and mixed lists, including eight completed
rows before pending/failed ones. Compilation and 69 focused Beacon tests plus
10 subtests passed. A separate real Flet app clicked Save & analyse (pending,
then completion without a panel), a deliberate failure, Retry, and Refresh
(recovered channel present, panel absent). Overview, Channels, Real Time, Find,
Saved and Agent were screenshot-inspected; the isolated Agent showed its expected
missing-setup state. Test-worker/UI journals had no exceptions.
Beacon commit `51a3515` is live in `20261003T183332Z-quiet-saved-51a3515`.
Only the web service restarted; Finder, Saved worker, gateway, radar, discovery
and organizer PIDs stayed unchanged. PostgreSQL activation and source hashes
passed. The actual live Saved page shows all three channels directly below the
form with no Latest save requests panel. All four completed durable records
remain intact, health returned 200, and the web journal had no exceptions.
Owned test Flet/PostgreSQL resources, browser tabs and SSH forwards were cleaned
up after verification.

## Saved freshness and daily movement — 2026-10-03

Cause confirmed read-only: Bertha's last recorded measurement was October 2
01:46 UTC. The durable PostgreSQL save worker only claimed explicit requests;
opening Saved deliberately skipped the old browser-driven refresh. Shared
commit `0cbfcd4` now schedules enabled Saved members after six hours, in batches
of at most 20 per five-minute tick. Existing jobs win; manual requests have
priority. The existing lease, short write transaction, retry and membership
preservation guarantees remain. No schema migration or collector change.

Read-only `saved_channel_tracking` uses only actual remote `manual-save` history,
including a distinct hidden-subscriber source. The compact UI shows check time,
signed subscriber/channel-total-view/upload changes across an actual 24–30 hour
span (labelled ~24h with the interval in the tooltip), or explicit missing/stale
history. Readings older than eight hours cannot claim current daily movement.
Subscriber counts inherit YouTube's rounding; hidden counts stay unavailable.
One quiet clickable video signal requires at least five measured recent uploads,
10,000 gained views, and three times typical hourly movement; it is not a viral
prediction. At most 30 recent uploads are compared. Existing discovery/opportunity
gates and separate full/read authorization remain unchanged.

Verification: seven changed Python files compiled; 64 relevant shared tests and
153 subtests passed against private PostgreSQL, plus 79 relevant Beacon tests
and 12 subtests both locally and in the isolated server runtime. A broader
server-only Agent/icon test selection exposed fixture dependencies (Bedrock
configuration, Pillow and Git) absent from that private clone; those unrelated
tests were not used to claim this feature's verification. The focused feature
suites passed. Real separate Flet checks clicked Saved (empty), Refresh, Refresh
live (pending then ready during neighboring signed imports), Save & analyse
(deliberate failure), Retry (ready), and reloaded the session. Automatic scheduling
changed a stale card without Refresh live; repeated scheduling queued zero
duplicates. Synthetic +45 subs/+200K channel views and a +90K pickup proved the
measured layout; stale and single-point cards withheld comparisons. The pickup
button opened its synthetic video's exact YouTube URL. Overview, Channels, Real
Time, Find, Saved and Agent were clicked and screenshot-inspected. The isolated
Agent showed its missing-setup state without an exception. Full/read measurement
requests returned 200; a read-token write returned 403. Worker/UI journals had no
exceptions. A header collision was repaired and recaptured at 1280px and the
1040px desktop minimum. Impeccable finish-review disposition: ship.

Cloud release `20261003T173748Z-saved-watch-0cbfcd4` is active with Beacon commit
`219ce93` and shared commit `0cbfcd4`. Committed-file SHA-256 checks passed before
switching the release. The save worker was idle at the restart boundary; only
save worker, Finder and web restarted. Gateway, radar, discovery and organizer
PIDs stayed unchanged. PostgreSQL activation checks passed; no database/schema
switch occurred. Normal signed imports continued after rollout.

All three real Saved channels refreshed automatically on October 3 at
17:38:37–17:38:42 UTC; every job finished ready in one attempt. Live UI screenshot
inspection showed Disaster Breakdown +17,159 channel views over 24.34 hours,
with unchanged rounded subscriber/upload counts. Bertha now showed 101K
subscribers, 42 uploads and 218K average views, plus its fresh check time.
Bertha and Dark Wild Archives lack a comparable verified reading from the prior
24–30 hours, so their daily movement remains collecting until another day of
checks. No historical daily numbers are invented and no current video pickup
was claimed. Live full/read measurements returned 200 in 0.279/0.133 seconds;
read-token writes returned 403, health returned 200, and unauthenticated public
HTTPS remained 401. All application/collection services and import/backup timers
were active; updated-service journals showed zero exception lines.
Verification cleanup completed: the owned Flet rehearsal unit stopped, private
fixture schemas were removed from the isolated PostgreSQL instance, that test
instance stopped, and the two owned test/live-check SSH forwards and browser
tabs closed. Production services keep running.

## Resume here: search-by-search discovery measurement - 2026-10-03

Shared commit `55acfbb` records which search first matched each globally new
channel inserted by a signed discovery import. Collector query-run rows now
carry bounded channel provenance. Importer credit uses actual successful inserts;
overlapping searches get one credit per new channel, with the earliest completed
matching search credited. This is recorded provenance, not an isolated causal
experiment. Failed imports and replayed bundles cannot inflate the result.

The read-only Finder tool `discovery_yield` reports query/topic/provider/sort,
successes, failures, credited new channels and emitted existing-channel matches.
It requires three successful samples by default before ranking a query, bounds
hours to 1-168, and reads at most 2,048 discovery ledger entries. Historical or
partial records remain explicitly unmeasured. Its counted-new total covers only
bundles with the newer insert counters; it is not a replacement for whole-index
historical growth counts. Existing-channel matches refer to emitted delta parents,
not every unchanged result on a search page. No table/schema migration, historical
backfill, plan, quota or scheduling change was made. The existing signed ledger
and post-commit ACK retain integer-valued counters. Beacon and Production Lab
share this engine; the Lab routes this read tool to the shared discovery index.

Verification: seven Python files compiled; 29 focused tests plus 43 subtests;
150 relevant shared tests plus 176 subtests against private PostgreSQL; 95 Beacon
tests plus 19 subtests; and eight collector tests in the actual candidate edge
runtime. Separate real Flet verification clicked Overview, Channels, Real Time,
Find, Saved and Agent. Screenshots proved only the two qualified fixture channels
surfaced after three incomplete catalogue inserts, Saved was initially empty,
Save & analyse completed while the neighboring import remained open, deliberately
failed analysis retained its request, Retry reached Ready, and browser reload
retained two saved results. Agent's missing-AI setup state was expected; no AI
answers were tested. Signed overlapping fixtures credited three new channels once
across six successful searches; a real unchanged collector fixture added one
successful search and no new channels. New read-tool calls returned 200 for full
and read credentials; read-token writes returned 403. No unhandled exceptions
were found in the test app or save worker.

Live app: `20261003T145500Z-query-yield-55acfbb`; collector:
`20261003T145500Z-query-provenance-55acfbb`. Only Finder and the discovery worker
restarted. The collector exited cooperatively with status 0. Web, gateway, radar,
save worker and organizer process IDs stayed unchanged. PostgreSQL stays canonical.
Both live health checks and read-tool calls passed; the initial report correctly
said collecting, with older bundles unmeasured. No increased daily growth rate is
claimed. Next: collect comparable samples, then test a bounded productive/search
exploration allocation with a separate refresh budget before expanding workers.

Production end-to-end confirmation at 15:02:53 UTC: the normal timer imported
two discovery/organization bundles, 1,420 channel records, six globally new
channel IDs and 1,414 existing entries (distinct within each bundle). All six new
IDs received search provenance, with zero unattributed new IDs in that discovery
bundle. The report separated 124 successful searches and one failed search;
125 query identities remain below the default three-success minimum. Both signed
ACKs reached the collector after commit; zero failed imports and zero local or
remote backlog. Older counted additions remain explicitly unattributed. This
single round does not establish a daily growth improvement.

Cleanup stopped `beacon-query-yield-ui-20261003.service`, dropped only disposable
schema `beacon_save_ui_6540d1dc9d94`, stopped private PostgreSQL 18543, closed test
tab 16 and stopped owned SSH forward 12684. Production PostgreSQL 5432, the
original gateway tunnel and recovery assets remain retained. Private proof files
are `query-yield-release-activated-20261003.json`,
`query-yield-normal-import-proof-20261003.json` and
`query-yield-cleanup-20261003.json` in `/var/tmp/beacon-pg-foundation`.

## Resume here: exact discovery accounting and first API saving — 2026-10-03

The canonical live index remains PostgreSQL. Shared commits `caaa203` and
`72e83b1` are committed/pushed on `codex/postgresql-foundation`. Current app:
`/opt/beacon/releases/20261003T133022Z-discovery-yield-72e83b1`. Current collector:
`/opt/signal-edge/releases/20261003T133022Z-delta-channel-72e83b1`.

The importer now persists distinct `channels_new`/`channels_existing` counts
after a successful commit, separately from the compatible input-record count.
The existing ledger and signed ACK carry these counts; no schema migration or
second catalogue is introduced. Replayed bundles retain their original counts
and do not inflate sync totals. Failed imports leave no reserved IDs or growth
claim. Organization versus discovery is identifiable by bundle kind, but
per-query attribution and correcting the historical yield-estimate floor remain
next work. Previously imported bundles are intentionally not backfilled.

The collector continues official video-view checks, then enriches only channels
whose videos produce delta records. The all-unchanged regression used 1 API
request instead of 2; a changed view retained enrichment and snapshots. This
does not establish a higher daily new-channel rate. Worker/plan/quota settings,
filters, operator membership and the video refresh path remain intact.

Each atomic patch passed compilation and focused tests before the next patch.
Final verification: 11 focused importer tests; 8 collector tests (also against
the actual isolated collector runtime); 186 shared tests plus 183 subtests and
one skip; 95 Beacon tests plus 19 subtests. Separate real Flet checks clicked
Overview, Channels, Real Time, Find, Agent and Saved for each patch. Captures
proved 2 fixture channels/24 videos remained browsable, Saved was initially
empty, Save & analyse showed Queued then Ready during an open neighboring
import, analysis failure retained membership and Retry recovered to Ready,
and reconnect retained both saved requests. Agent's missing-AI setup state was
expected; no AI answers were tested. A signed unchanged collector run imported
with `channels_new=0`, one query run and zero channel-detail calls. Journals and
the fixture save worker contained no unhandled exceptions.

Release preparation copied only clean deployed code, preserving app state
symlinks and executable helper modes. The edge release shares the previous
immutable virtual environment and overlays only the committed collector file.
The old discovery worker exited cooperatively with status 0 before restart.
The web, Finder, gateway, radar, save worker and organizer remained running.
Full/read health returned 200 and a read-token write returned 403. Production
organization at 13:34:54 UTC reported 0 new / 11 existing channels. Normal sync
at 13:39:42 UTC processed two discovery/organization bundles: 1,463 channel
records, 9 globally new IDs, 1,454 existing entries (distinct within each bundle),
125 query runs. Both signed acknowledgements returned successfully after
commit; result 0, no failures, zero local/remote backlog. This is a single round,
not proof of improved daily growth. The collector's process cwd resolves to
the new immutable edge release.

Cleanup stopped the owned `beacon-channel-yield-ui-20261003.service` and
`beacon-collector-delta-ui-20261003.service`, removed only their disposable
fixture schemas, stopped private PostgreSQL 18543, closed test tab 15 and
stopped local SSH forward 41468. Production PostgreSQL 5432 and the original
gateway tunnel remain retained. No one-off test job remains running. Private
proof files: `yield-release-activated-20261003.json`,
`yield-normal-import-proof-20261003.json` and `yield-cleanup-20261003.json` in
`/var/tmp/beacon-pg-foundation`.

Next steps: inspect new signed import counts by bundle kind; implement bounded
query/source attribution; use it for productive-search and separate refresh
budgets; run a comparable coverage trial; then split lightweight discovery from
deep analysis. Do not claim the proposed 10,000/day target has been reached.
Protected SQLite recovery assets and other-agent working files remain retained.

## Current priority: increase actual discovery throughput — 2026-10-03

The user moved new-channel collection speed ahead of the remaining roadmap.
Read-only measurements and the next sequence are in
`DISCOVERY_THROUGHPUT_PLAN.md`. The index remains PostgreSQL on live release
`20261003T102250Z-postgres-ranked-b16d173`. This audit changed no production
workers, discovery plans, provider limits or index records. The previous test
resources remain stopped. Next measure globally new-channel yield by source,
reduce redundant exploration and separate lightweight discovery from deep
analysis before choosing additional capacity.

## Resume here: save contention and format-search fixes live — 2026-10-03

The live index remains PostgreSQL. Current release:
`/opt/beacon/releases/20261003T102250Z-postgres-ranked-b16d173`.
Shared commits `8fee1be` and `b16d173` are committed/pushed on
`codex/postgresql-foundation`. The first commits signed imports per bundle to
release finished channel edits promptly; the second checks video duration in
stable ranking order and stops once enough format matches fill the page.
Counts still examine all matches, and SQLite behavior is unchanged.

The new query matched every returned field of the old query in one repeatable
read transaction against the full live data. Long-form: 22.379s before versus
4.555s after; Short-form: 14.629s versus 3.602s. A separate earlier prototype
was 4.895s versus the 14.493s initial Long-form baseline. These are measured
runs, not guaranteed response times. The duration-read regression failed
before the change (40 lookups) and passed afterward (3 lookups to get the first
matching channel). Missing lengths, boundary duration 90s, combined filters,
counts, descending/ascending ranking and zero/negative RPM comparisons passed.
Focused PostgreSQL store tests passed 22; relevant shared verification passed
180 tests plus 183 subtests, one skip; relevant Beacon verification passed
102 tests plus 19 subtests.

Separate real Flet verification opened all six screens. Channels Long-form
showed the History fixture; Filters → Short → Apply showed the Space fixture;
Short plus Min subs 999999 showed a genuine empty result; Best Opportunities
cleared filters and recovered both cards. Stopping only private test PostgreSQL
18543 produced a handled database-unavailable card; restarting that private
cluster and clicking Retry recovered both cards. Production PostgreSQL 5432
remained active. Real Time → Long-form showed the matching discovered fixture.
Overview, Find, Saved's empty shelf and Agent's expected missing-AI setup state
were captured and visually inspected. No unhandled journal tracebacks or SQL
query errors occurred. This does not claim live AI-answer verification.

The cloud release was copied from the previous clean pinned release, preserving
state symlinks/executable modes, then overlaid with the exact two committed
store/test files. Only Finder restarted. The real production Long-form screen
loaded 40+ cards; full/read health returned 200 and a read-token write returned
403. Normal imports continue succeeding. User data, production collectors,
gateway tunnel, web, radar and save worker were retained.

The deployed Short-form screen also loaded 40+ cards. Final cleanup stopped
only the owned `beacon-save-wait-ui-20261003.service`, private PostgreSQL 18543,
two temporary browser tabs and local SSH forwards 46724/44440. All eight
checked production app/database/import/backup services or timers stayed active.
No one-off test or benchmark job remains running. Private proof:
`/var/tmp/beacon-pg-foundation/product-fixes-20261003-complete.json`.

Continue the authorized roadmap in `BEACON_ROADMAP.md`: next create a reviewed
ranking/classification benchmark from representative real channel evidence
before changing ranking thresholds. Steps 3–7 remain product development work;
the ordinary 2–3-second search goal also needs broader measurements under load.
Do not undo these fixes or restart the migration. Retained SQLite/recovery
copies are recovery assets; all new index writes stay on PostgreSQL.

## Current work: product roadmap, step 1 deployed — 2026-10-03

PostgreSQL remains the live index. Shared commit `8fee1be` releases imported
channel-row locks after each signed bundle instead of retaining them across
20 unrelated bundles. SQLite batching is unchanged. This fixes a reproduced
importer contention mechanism; the writer behind October 2's historical lock
timeouts was not identified, and active edits to the same row still serialize.

The regression failed before the patch and passed afterward. Focused PostgreSQL
importer tests passed (6); the relevant shared selection passed 179 tests with
177 subtests and one skip; the relevant Beacon selection passed 102 tests with
19 subtests. An optional broad source-only Linux run retained the same nine
baseline failures (391 passed); it is not a clean full-suite claim.

Real isolated Flet verification clicked Saved, submitted a synthetic channel,
and observed Queued then Ready while the next importer bundle was still staged.
The finished first bundle was committed; both signed acknowledgements followed
their commits. A deliberately failed analysis retained membership, Retry reached
Ready, reconnect retained progress, and a repeat save retained exactly two
requests/two memberships. Overview, Channels, Real Time, Find, Saved and Agent
were opened and screenshots inspected. Agent showed its expected missing-AI
setup state in this credential-free fixture; AI answers were not tested. No
fixture worker errors or journal tracebacks occurred. The first harness pause
exceeded its private idle-transaction limit; a corrected fresh fixture passed.
Production safety limits were unchanged.

Current release:
`/opt/beacon/releases/20261003T100650Z-postgres-import-8fee1be`.
Only the two exact committed importer/test files were overlaid on the pinned
clean package. Existing state links and executable helper permissions were
preserved. The first package check exposed missing executable permissions on
two helpers; restoring their previous modes resolved it. The next normal sync
imported/acknowledged one bundle, returned 0, and left no backlog/errors. Web,
Finder, radar, save worker and gateway processes stayed running. The live Saved
screen retained three channels and Disaster Breakdown Ready.

Step 2 is now measuring actual query work. Read-only baseline: Best 3.72s,
History 2.68s, Long-form 14.49s, recent 24h 5.93s. Long-form's measured plan
looked up durations for 10,708 candidates before selecting 40. A ranked-read
prototype is being compared before any further application code change.
Private test resources and SSH forwards are still active during verification;
stop only the owned fixtures/tunnels at a stopping point.

The authorized sequence is documented in `BEACON_ROADMAP.md`. Steps 2–7 are
not complete. Earlier migration checkpoints below are historical.

## Resume here: migration complete; next fix same-channel write waits — 2026-10-02

Final migration checkpoint at 20:36 UTC: PostgreSQL is live. Final reconciliation
compared all 30 legacy tables exactly; schema 6 has 32 tables. Statistics preparation and the
full native restore rehearsal passed. The retained original SQLite and verified
laptop recovery sets remain available. **Do not switch back to SQLite after new
PostgreSQL writes.** A code rollback must retain the PostgreSQL backend.

The live release is
`/opt/beacon/releases/20261002T183918Z-postgres-format-bd6f9a4`.
Shared commit `bd6f9a4` is committed/pushed on `codex/postgresql-foundation`.
Only the owned `scripter/research/store.py` and `tests/test_postgres_store.py`
were staged; all unrelated Quill changes and Beacon's taskbar change remain
untouched. The release was built from the clean pinned schema-6 package plus
those two exact committed files. Only Finder restarted; web, gateway, radar,
save worker, the gateway tunnel and collectors retained their processes.

The reproduced Long-form statement timeout is fixed by first selecting eligible
channels, then looking up their indexed video durations. The format predicate
still applies before ranking and page limits; SQLite behavior is unchanged.
Focused PostgreSQL tests passed (21); the relevant shared suite passed (128,
one skipped); relevant Beacon tests passed (113). A broad source-only Linux
Beacon run had the same three failures and seven errors before and after;
missing Git/assets/AI environment prevented claiming all 317 tests passed.
The separate real Flet instance passed Overview, Channels, Real Time, Find,
Saved and Agent, including Long/Short, combined filters and a genuine empty
result. Its journal had no runtime exceptions. The deployed production
Long-form Retry subsequently loaded 40+ cards, captured in
`live-pg-20261002-19-long-form-result.png`. Under backup load the new query took
25.4s for Long-form and 18.4s for Short-form; further speed work remains.

Actual production Saved checks passed the Disaster Breakdown save, repeat
submission without duplicate membership, browser reconnection retaining three
channels, its real analysis dialog, retry and a further successful refresh.
Read-token writes returned 403. A previous refresh exhausted its retries
without losing membership. PostgreSQL's server log now confirms three
channel-row lock timeouts at 17:21:22, 17:21:47 and 17:22:31 UTC. The blocking
writer's identity was not proven. The importer backlog was subsequently drained.
Shortening PostgreSQL importer transactions is a follow-up, not an implemented
fix. Do not claim that changing databases eliminates every same-row wait.

The actual importer is `beacon-edge-sync.service`/`.timer`; production rounds
completed with result 0 and no remaining bundles. All active index services
passed the PostgreSQL selection guard. The gateway tunnel remains PID 658711.
A fresh private live-deployment/roles/configuration/operational-queue recovery
kit was also hash-verified on the laptop. It contains secrets, stays outside Git,
and must never be displayed or unpacked into a public directory.

The first production native backup completed successfully around 18:18 UTC:
`beacon-20261002T174328Z-bfd6d4e6f3b0.dump`, 4,988,633,178 bytes, with all 32 table
fingerprints and a completed manifest. At 18:56 UTC its laptop copy completed
with matching size/hash, matching manifest and the server's exact off-host
acknowledgement. Both copies are retained. Destination:
`D:/BeaconBackups/PostgreSQL/archives`. The measured archive size is now in the
private backup settings. About 22.6 GB free on the server covers two further
archives plus the protected 8 GiB reserve, with about 4.0 GB additional margin
at this measurement; data growth must still respect the reserve.

Both backup schedules are enabled and verified. The daily server timer is
active, with its next run October 3 at 02:05:21 UTC (daily 02:00 plus up to 15
minutes of random delay). The normal pinned laptop task ran at 20:33 UTC,
completed its local hash/server acknowledgement at 20:34:17 UTC and returned
actual Task Scheduler result 0. Its next run is 21:00 UTC; it also runs at logon
and repeats every four hours with network checks, missed-run recovery and two
15-minute failure retries. The laptop must be awake, online and logged in for
its interactive-user transfer; the cloud backup runs independently. Server
retention preserves all unmirrored archives and refuses a third unmirrored set.

The first actual scheduled transfer attempt reported result 1 for a private
server operation. Its precise cause was not established. A temporary diagnostic
action subsequently passed catalog, manifest and acknowledgement calls with
result 0. The original pinned action was restored and independently passed.
The diagnostic action is no longer installed. No app code was changed for this
unreproduced failure; nonzero task/status reporting and retries are retained.

Final live checks at 20:36 UTC passed full/read health (200), active PostgreSQL
selection for all index services, successful importer result 0, preserved
original SQLite size/inode and the original gateway tunnel PID 658711. Private
`migration-complete-20261002.json` records all closing proofs; activation and
maintenance records now mark post-activation checks complete with writers active.
No migration gate or one-off test/transfer job remains running.

Next development steps:

1. Reproduce contention between an import and an analysis editing the same
   channel in a disposable PostgreSQL schema. Keep production data untouched.
2. Shorten PostgreSQL importer transactions; preserve commit-before-ack,
   idempotence, signed-bundle isolation and SQLite behavior. Complete the focused,
   shared and separate real six-screen Flet gate before deploying another change.
3. Measure Long/Short and combined-filter latency under active importing;
   improve it with evidence instead of raising timeouts blindly.
4. Improve topic/faceless/replicable classification and ranking with a labeled
   benchmark, particularly the remaining false positives seen in browsing.

Cleanup is complete for my temporary app verification: the separate Flet unit
and disposable PostgreSQL cluster on port 18543 are stopped; owned laptop
forwards 39456/53632 are closed and owned tabs 10/11 are closed. Production
PostgreSQL on 5432, the live app, original gateway tunnel and user YouTube tab
were preserved. Verified recovery copies and the enabled schedules are retained.

After this migration milestone, address the confirmed same-channel write waits
with a small, separately verified importer transaction change, then measure
search speed and improve classification/ranking against a labeled benchmark.
Those follow-ups are not completed by the database migration.

### Historical checkpoint: final reconciliation running

The laptop receiver completed successfully around 15:10 UTC. The recovered
34,982,883,328-byte source passed full integrity and foreign-key checks, and
the 8,701,371,465-byte archive and recovered source match the sealed cloud
hashes exactly. `final-source-offhost-verified.json` is verified on both hosts;
the independent pause gate and all source/archive manifests agree.

After a fresh full hash of the server archive matched the verified laptop copy,
only the redundant generated cloud `cutover-quiescent.sqlite3.gz` was removed.
Its manifest, seal and proof remain. The laptop compressed archive and healthy
recovered database, canonical cloud SQLite file and native PostgreSQL recovery
archive are retained. Private `final-source-compressed-reclaim-20261002.json`
records the exact deletion and 27,713,564,672 bytes of remaining server space.

`beacon-postgres-final-catchup-20261001.service` started at approximately 15:14
UTC with its existing guarded reconciliation operator, a six-hour limit and
6 GiB memory ceiling after checking available RAM. Its logs are
`/var/tmp/beacon-pg-foundation/final-catchup-20261002.log` and `.err`.
Require unit completion, Result success, ExecMainStatus 0 and the matching
verified 30-table `final-reconciliation-result.json` before proceeding.
Next run the prepared `vacuum_final_catchup.py`, capture/verify the final
private settings kit, then plan/apply coordinated activation. Actual production
Saved/reconnect/all-screen/auth/importer checks and a verified first production
backup/off-host copy plus enabling both schedules remain pending.
The app is still in maintenance; PostgreSQL has not been activated.

At approximately 16:58 UTC, all 30 legacy tables had finished their initial
comparison. The video table contains 5,120,792 records with 162,286 upserts;
video discovery history contains 23,716,263 records with 318,729 upserts; video
snapshot history contains 40,091,740 records with 687,861 upserts. These three
tables reported zero deletions. The second, complete target-content verification
is running before transaction commit. The unit remains active, the final proof
is not yet present and its error log is empty; this is not a completed cutover.

At approximately 15:44 UTC, 19 tables had completed their initial comparison;
the large video-discovery history had compared 23.7 million records and was
applying new records beyond the rehearsal baseline. The unit was still active,
its error log empty and server free space approximately 27.8 GB. No final
reconciliation proof or commit success is claimed from this intermediate state.
A stronger isolated populated-table VACUUM ANALYZE test also passed: all 32
before/after content fingerprints matched. Its first verification attempt used
a server cursor while autocommit was still enabled; restoring transaction mode
for the final fingerprint check corrected the test harness. The actual cleanup
procedure was unchanged. Sources remain outside Git in the private operators
recovery folder. An older `youtube_index.sqlite3.previous` file was observed
while measuring backup capacity and remains untouched.

### Earlier final-source recovery checkpoints

The cooperative drain completed successfully without force-killing the radar.
All index services and the importer timer are stopped, the retained SQLite
deployment still selects SQLite, and the existing gateway tunnel is preserved.
The independent pause guard confirmed zero unexpected open source handles.
`beacon-postgres-final-recovery-20261002.service` completed successfully:
inactive, MainPID 0, Result success and ExecMainStatus 0. It checkpointed
committed WAL and sealed the guarded compressed final recovery archive.
Its private logs are
`/var/tmp/beacon-pg-foundation/final-recovery-20261002.log` and `.err`.
The final seal and archive manifest agree on the 34,982,883,328-byte original
and 8,701,371,465-byte compressed archive; full integrity and relationship
checks passed, and the retained original/private alias share the same inode.
The laptop `download_final_quiescent.py` receiver is running (owned launcher
PID 20932), transferring into `D:/BeaconBackups/20261001/`. Its logs are
`C:/Windows/Temp/beacon-pg-store-ui/final-recovery-transfer-20261002.log` and
`.err`. The read-only SSH status monitor ended when the source job completed.
Transfer alone is not recovery verification: require the receiver's successful
exact decompression/hash/integrity checks and `final-source-offhost-verified.json`
on both hosts. Final reconciliation, activation and post-cutover checks remain
pending.
The app is in maintenance; PostgreSQL has not been activated.

The complete archive arrived around 14:01 UTC and passed its local SHA-256
check. All 34,982,883,328 source bytes were recovered by approximately 14:07
UTC. A separate readback independently hashed both stored files and matched
the sealed source/archive exactly; its proof is
`D:/BeaconBackups/20261001/final-source-readback-verified.json`. The receiver's
full database integrity/relationship check is still running (actual child
PID 4544 under launcher 20932); increasing process read counters confirm
continued work. No final off-host recovery proof has been published yet.

At 14:56 UTC the receiver was still advancing with an empty error log. A second
laptop public-address change was repaired through the existing isolated OCI
allowlist updater; SSH access was reverified without widening the `/32` rule.
The private final statistics operator `vacuum_final_catchup.py` is prepared
outside Git on both hosts, with non-secret sources retained in
`D:/BeaconBackups/20261001/operators/maintenance-20261002/`. It reuses the tested
VACUUM ANALYZE procedure, requires a completed successful catch-up unit plus
matching final recovery/reconciliation proofs, preserves the prior report, and
continuously checks the coordinated pause. Compilation and five refusal/proof
tests passed locally and on Oracle. The real disposable PostgreSQL test then
verified VACUUM ANALYZE on all 32 tables with rows preserved. Its initial run
failed before database access because the disposable test DSN was unset; the
explicit isolated-cluster setting corrected the harness. The final statistics
operator has not been run against the full target.

At approximately 12:43 UTC, the full source integrity and foreign-key checks
passed and the final job advanced to compression. At 13:02 UTC, the completed
unit and final seal were observed; the independent pause/manifest/inode gate
passed before the laptop receiver was started.

Next: verify off-host recovery, run the guarded final catch-up unit
`beacon-postgres-final-catchup-20261001.service`, then refresh the inactive
target's visibility maps/statistics with the existing tested VACUUM ANALYZE
procedure and a new final-catch-up report. Capture/verify the final private
settings kit before the activation plan/apply steps. After activation, test
the real Disaster Breakdown save, reconnection, all six screens, authorization
and importer acknowledgements. Create/verify a real production backup and its
off-host copy before enabling the prepared server/laptop schedules.

The final job started at approximately 10:44 UTC on October 2. At 11:44 UTC
its full SQLite integrity/relationship check was still advancing with no
reported error; compression had not begun. The checkpointed source is
34,982,883,328 bytes. Only this owned job's runtime memory ceiling was raised
from 1 GiB to 4 GiB, then to 6 GiB after checking available server RAM; its PID
remained 907853 throughout. This did not restart the job or activate PostgreSQL.
An active unit displaying `Result=success` is not a completion proof.

While that check runs, the pinned production backup service/timer templates
were installed and passed `systemd-analyze verify`. Their private administrator
settings and measured baseline archive-size setting are prepared, but the
service remains inactive and the timer disabled. The laptop task
`BeaconPostgresBackupTransfer` is registered and verified disabled, using
`D:/BeaconBackups/PostgreSQL/operators/schema6/` rather than working-tree code.
Actual post-activation backup creation, exact off-host transfer/acknowledgement,
and enabling both schedules still remain. Use the registered pinned task;
do not replace it with the temporary working-tree registration helper.

## Update: overnight recovery and full cloud preview passed — 2026-10-02

The detached schema-6 recovery unit finished successfully after 7,939.6 seconds.
All 32 restored table fingerprints matched, restricted role checks passed, and
all 32 tables completed VACUUM ANALYZE. Reader writes/DDL and writer DDL/schema
metadata changes remain forbidden. The verified native archive remains off-host.

A separate read-role-only Flet preview on Oracle used the pinned schema-6
release and private Finder port 18598. Real clicks covered Overview, Channels,
Long-form, Filters, Apply, Real Time, Newest, Find, Saved and Agent. Inspected
`cloud-20261002-01` through `cloud-20261002-21` screenshots show the complete
265,217-channel / 5,091,451-video index counts, retained two Saved memberships,
combined subscriber/format results, an explicit 500-million-subscriber empty
result, the empty 24-hour discovery window and populated Newest view, the Find
form, and successful browser reload/reconnection. Agent displayed its expected
missing-AI-settings state in this credential-free preview; no AI request was
sent. Some intermediate captures retain loading/prior-screen states; only the
completed captures establish the result. Screenshots remain outside Git under
`C:/Windows/Temp/beacon-pg-store-ui/`. The preview created zero saved requests
and its journal contained no Python traceback.

The preview app and its owned laptop SSH tunnel were stopped before beginning
the cooperative maintenance drain. The read-only maintenance plan passed with
9,921,428,129 bytes reserved for the final archive, in addition to the protected
8 GiB disk reserve. `beacon-postgres-maintenance-drain-20261002.service` is now
waiting for existing work to finish before stopping index services. Its private
logs are `/var/tmp/beacon-pg-foundation/maintenance-drain-20261002.log` and `.err`.
No worker is force-killed. The original SQLite file and gateway tunnel stay in
place. Final sealed/off-host recovery, reconciliation, coordinated activation,
live Saved checks and production backup scheduling remain pending.

During the drain, a real HTTPS check reproduced a maintenance-proxy defect:
removing the route host matchers stopped Caddy's automatic certificate handling.
The private operator now retains the original hostname matchers while returning
503. Changed Python compiled, nine focused tests passed locally and on Oracle,
and an isolated actual Caddy/WebSocket fixture verified maintenance, stream
closure and restoration. A normal certificate-verified HTTPS request to the
configured hostname now returns 503 with the upgrade message. The browser tool
could not open that public hostname (`ERR_BLOCKED_BY_CLIENT`); no browser safety
barrier was bypassed, so public-page visual verification remains a limitation.
A separate read-only Flet recheck clicked all six navigation controls;
`cloud-recheck-20261002-01` through `06` screenshots show loaded Overview,
Channels, Real Time's empty window, Find, both Saved cards and the expected
Agent setup state. Its journal has no Python traceback. That recheck and its
tunnel were stopped. Non-secret repaired operator/test sources are preserved
under `D:/BeaconBackups/20261001/operators/maintenance-20261002/` and the private
Oracle foundation directory. The running drain remains cooperative.

Disk preparation verified the complete 5,417,124,928-byte ordered import
package against its sealed manifest on both the laptop and Oracle (identical
SHA-256). After the complete native recovery/off-host proofs passed and its
dependent jobs were confirmed stopped, only the redundant Oracle transfer
package was removed. Its laptop copy and manifest, the original SQLite index,
and every native recovery archive remain. Free server space increased from
21,982,969,856 to 27,400,097,792 bytes. The private reclaim record identifies
the exact removed file and its retained verified copy.

## Resume here tomorrow — 2026-10-02

This section records the previous overnight stopping point; use the newer
update above for the current maintenance stage.

**Safe stopping point: the live app stays on SQLite.** No cloud preview,
maintenance pause or PostgreSQL activation has started. Our finished code is
committed on `codex/postgresql-foundation` (Beacon `2ccf1ac`, Quill `2997275`
before this resume-note commit). Other agents' working-tree changes are excluded.
Local test app instances and their PostgreSQL SSH tunnel have been stopped.

Finished: the full import and exact-result comparisons passed; schema 6 and its
statistics indexes passed the tests and isolated screen checks. The complete
32-table native backup and its protected laptop copy both passed verification.
The pinned deployment is prepared but inactive. Backup schedules are prepared,
not enabled.

**Still running on Oracle:** `beacon-postgres-schema6-recovery-20261001.service`.
It restores only the inactive PostgreSQL target, checks all 32 tables and role
permissions, then runs `VACUUM ANALYZE`. It runs independently of this laptop
and stops before preview or activation. Leave it alone tonight.

Tomorrow, run this read-only check from PowerShell:

```powershell
ssh -o BatchMode=yes -o ConnectTimeout=10 -i C:\Users\user\.ssh\signal_edge_oracle_ed25519 ubuntu@51.170.132.206 "sudo -n systemctl show beacon-postgres-schema6-recovery-20261001.service --property=ActiveState,SubState,Result,ExecMainStatus,MainPID; sudo -n cat /var/tmp/beacon-pg-foundation/rehearsal-schema6-status.json; sudo -n tail -n 20 /var/tmp/beacon-pg-foundation/schema6-recovery.log"
```

An active unit is still working, even if `Result=success` is displayed. Require
a completed successful unit, final state
`recovery_verified_preview_and_cutover_still_pending`, and verified 32-table
reports in `native-recovery-result.json` (schema 6 and role checks) and
`native-read-preparation-after-restore.json`. Reports are under
`/var/tmp/beacon-pg-foundation/`. If it failed, inspect the logs and repair only
the inactive target; do not blindly restart the import or remove recovery files.

Next steps, in order:

1. Verify those recovery proofs and confirm the live services still use SQLite.
2. After recovery passes, use `prepare_full_cloud_preview.py` with the pinned
   `/opt/beacon/releases/20261001T223800Z-postgres-schema6` release. Run a separate
   read-only preview and check all six screens. No full-target save jobs yet.
3. Check disk space and the recovery archives; announce the planned maintenance
   window before draining writers. Capture and verify the final SQLite recovery
   copy off-host, catch up PostgreSQL, and verify the private recovery settings.
4. Activate only after those gates pass. Test saving DisasterBreakdown, duplicate
   saves, reconnect/retry, authorization, and collector/radar progress. Verify a
   real production backup and off-host transfer before enabling backup schedules.

The local Python 3.11+ upgrade remains open before October 4; coordinate it with
the other agent's shared-runtime work. Oracle already uses Python 3.12.3.

## Update: full native archive verified off-host — 2026-10-01

The native schema-6 archive completed with all 32 table fingerprints matching
the imported records. Its measured size is 4,947,483,261 bytes. The protected
laptop copy passed the exact file-size/hash checks, together with the private
deployment recovery set, before Oracle accepted the off-host acknowledgement.
The recovery gate is now rebuilding only the inactive PostgreSQL target from
that archive. Full restored-content/role checks and visibility-map preparation
must finish before the separate cloud preview; activation remains pending.
All six existing live SQLite services remain active.

The permanent laptop transfer operator is prepared from the same pinned
release under the protected `D:/BeaconBackups/PostgreSQL/` directory. Its
four-hour/logon task and seven-archive laptop retention are prepared, not
registered or enabled. Production scheduling still requires the post-cutover
native backup and actual transfer verification. The existing Oracle SSH-access
refresh task remains healthy. Final-pause recovery commands now consistently
select the verified pinned release, with seven private-kit/transport checks and
a disposable 30-table quiescent-alias migration/recovery check passing.

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
contain no application exception. Complete native recovery, the full cloud
preview, final catch-up and production activation remain pending.

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
