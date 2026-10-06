# Discovery throughput — priority updated 2026-10-03

## Committed-channel selection — 2026-10-04

Shared commit `dacaf25` teaches the collector from signed acknowledgements of
successful canonical imports. No direct database access is added to the collector.
It remembers the exact query/topic/provider/sort identities for each emitted
bundle, verifies its ACK signature, node, imported/duplicate status and matching
counts, and applies each known bundle once. Unknown historical ACKs, partial
provenance, missing ACKs and failed searches cannot become zero-success samples.
Invalid evidence is rejected without stopping collection. Delayed ACKs are
ordered by recorded collector run, so they cannot replace a newer 12-sample
window. Pending feedback is capped at 256 bundles per plan; dropped pending
evidence is counted. Topic, explicit sort or result-limit changes invalidate old
query feedback; superseded plan ACKs are discarded.

First-pass coverage and provider retry backoff still take priority. Without three
successful measurements for the current topic/sort, the old scheduler remains.
When measured evidence is available, the five-slot pattern is three productive,
one exploration and one refresh slot: **60% / 20% / 20%**. Small batches rotate
that pattern across ticks. Productive searches rank by actual credited new
channels per successful search in the recent window, with at least three
successes and positive yield. Exploration revisits unknown or low-yield searches;
refresh uses oldest-completed searches, including previously retired ones. Slots
never duplicate a query; unavailable roles fall back to age-ordered work and are
reported as fallback. A new video from an existing channel earns no discovery
credit. The refresh allocation rechecks query results/views; the separate Saved
worker still checks enabled saved channels every six hours. This allocation does
not guarantee that every indexed channel gets a full remote refresh.

This is the first selection improvement, not completion of the broad-coverage
plan. Search families, languages, date windows and deeper-page discovery have
not been expanded. Quota ceilings, workers, run cadence, signed plans, organizer,
classification and opportunity gates are unchanged. Existing free tools and the
existing Oracle instance are used; no paid source, proxy or service was added.
Beacon and Production Lab share the changed engine.

The collector release `20261004T181311Z-channel-feedback-dacaf25` is active.
The old collector exited cooperatively with code 0. Web, Finder, Saved worker,
radar and organizer PIDs stayed unchanged. The minimal older collector package
also received two unchanged committed dependencies: the process helper and the
existing attribution helper. Dependency omissions were caught by the pinned
release test gate before activation. The app release remains
`20261003T183332Z-quiet-saved-51a3515`; PostgreSQL stays canonical.

Pre-rollout baseline at **2026-10-04 18:13:11 UTC**: **272,272 channels**, with
**2,076 newly added in the preceding 24 hours**. Priority allocation ramps up as
new signed measurements reach the three-success threshold. No daily speedup or
Nexlev-sized coverage is claimed from these tests. Next: compare subsequent full
24-hour growth, new channels per 1,000 successful searches, failures and queue
age; then add bounded new search families/languages and evaluate their measured
yield. Retain a separate refresh allocation and Saved freshness throughout.

Verification: all four changed Python files compiled; 134 relevant shared tests
plus 18 subtests passed against private PostgreSQL, including real commit,
existing-channel, duplicate import and rollback-to-no-ACK checks. The clean
laptop Beacon suite passed 396 tests plus 45 subtests (8 platform skips).
Collector-runtime checks passed 55 tests (one private-PostgreSQL skip and one
controller-only dependency deselection); the exact minimal pinned release passed
54 tests with three application/database tests deselected, already verified in
the full application runtime. An attempted broad cloud Beacon run encountered
missing test Bedrock configuration, Git and Pillow; the complete clean laptop
suite passed with synthetic Bedrock configuration. A local test environment's
autopilot-disable flag was removed before its successful full rerun. These were
test setup issues; production credentials and dependencies were not altered.

Real isolated Flet verification used the shared cloud venv, separate Finder
port/token/state and private PostgreSQL port 18543. Screenshots were inspected in
`C:/Windows/Temp/beacon-discovery-feedback-20261004/` (not committed):

| Actual action | Capture | What was verified |
|---|---|---|
| Open Overview | `01-overview.png` | Connected Finder, fixture index and empty niche workspace |
| Click Channels | `02-channels.png` | Two qualifying synthetic channels and complete filters |
| Click Real Time | `03-real-time.png` | Discovery window, fresh cards and separate discovery-time label |
| Click Find | `04-find.png` | Complete sweep form and browse action |
| Click Saved | `05-saved-empty.png` | Empty shelf and complete save form |
| Enter fixture channel and click Save & analyse | `save-loading.png`, `save-success.png` | Queued request then one saved card; server probe confirmed the neighboring import was still open |
| Save a deliberately failing fixture | `save-error.png` | Retained request with Retry and explanatory failure |
| Click Retry after enabling fixture recovery | `retry-loading.png` | Request returned to pending, without losing the saved card |
| Click Agent | `06-agent.png` | Complete controls and expected missing-AI setup state; no live AI-answer claim |
| Return to Saved, reload and click Refresh | `save-reconnected.png` | Two saved cards, recovered channel, no persistent Ready panel |

Both fixture imports committed, the fixture save worker had no exceptions, and
the test UI journal had no unhandled exceptions. Production app health returned
200 locally; unauthenticated public requests correctly received 401. Private
release proof is `/var/tmp/beacon-pg-foundation/discovery-feedback-release-activated-20261004.json`.

Production end-to-end confirmation at **18:23:46 UTC**: the first normal import
committed **9 globally new channels** and **1,099 existing channel matches**.
Its signed feedback matched the canonical ledger: **87 successful searches**,
**13 provider JSON failures**, 87 measured contexts, no pending feedback and no
rejected ACKs. The importer returned 0. The existing retry controller backed off
from 100 to 75 searches after those provider failures; its behavior was not
changed in this task. Failures are excluded from successful yield samples. The
allocation correctly remains on the legacy scheduler while these first samples
are below the three-success threshold. One batch is not a daily speed result.
Proof: `discovery-feedback-normal-import-proof-20261004.json` under the private
server work root. Both full/read credentials could read the report (200), and
read credentials could not enqueue a save (403).

Cleanup stopped only the disposable test UI/Finder group, dropped only schema
`beacon_save_ui_e6d8e758e310`, stopped private PostgreSQL 18543, closed the test
browser tab and closed owned SSH forward 29428. Production PostgreSQL 5432 and
all six live service processes remained running. Cleanup proof is
`/var/tmp/beacon-pg-foundation/discovery-feedback-cleanup-20261004.json`.

The user wants a much larger useful channel catalogue and prioritizes collection
speed over the remaining feature roadmap. This is a diagnosis and development
sequence; no discovery settings, workers or live data were changed for this audit.

## Search-by-search discovery measurement - 2026-10-03

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

Separate browser evidence (generated files in
`C:/Windows/Temp/beacon-query-yield-20261003/`, not committed):

| Click / action | Capture | What was visually verified |
|---|---|---|
| Open Overview | `01-overview.png` | Connected Finder; 5 fixture channels and 24 videos |
| Activate Channels | `02-channels.png` | Exactly two qualified channels; three incomplete catalogue records suppressed |
| Activate Real Time | `Real-Time.png` | Discovery window and qualified cards still load |
| Activate Find | `Find.png` | Complete sweep field, start control and browse action |
| Activate Saved | `Saved.png` | Empty shelf and complete link form |
| Submit first fixture link | `save-loading.png`, `save-ready.png` | Request recorded, then Ready while neighboring import was still open (server probe confirmed overlap) |
| Submit controlled failing link | `save-error.png` | Saved request retained; explicit retry available |
| Activate Retry | `retry-ready.png` | Recovered second channel; two saved cards |
| Activate Agent | `Agent.png` | Complete controls and expected missing-AI setup message |
| Reload and reopen Saved | `save-reconnected.png` | Two Ready requests and two saved memberships retained |

## First implementation — 2026-10-03

Shared commits `caaa203` and `72e83b1` add exact committed import accounting and
remove a reproduced redundant API lookup. Application release:
`20261003T133022Z-discovery-yield-72e83b1`; collector release:
`20261003T133022Z-delta-channel-72e83b1`.

Each newly imported signed bundle now records `channels_new` (distinct IDs this
transaction actually inserted) and `channels_existing` (distinct IDs already in
the canonical index). The legacy `channels` field still counts input records.
Metrics persist in the existing import ledger and signed acknowledgements only
after commit. Duplicate delivery retains its original report and is excluded
from the sync's added totals. Failed bundles roll back both reserved IDs and
metrics. Discovery and organization remain distinguishable by bundle kind.
Historical bundles are not relabeled; query/family attribution is still pending.

The collector still checks current video views. It now requests channel details
only for videos that will actually produce imported changes. An unchanged repeat
needed one API request instead of two in the controlled regression; a changed
view still triggered official channel enrichment and a snapshot. This is an API
work saving, not proof of faster daily catalogue growth. Worker count, signed
plans, quotas, refresh behavior and opportunity gates are retained.

Verification passed 11 focused import tests, 8 collector tests, 186 relevant
shared tests plus 183 subtests (one skip), and 95 Beacon tests plus 19 subtests.
Both atomic patches passed separate real Flet checks of all six screens, queued
save → Ready during imports, empty Saved, retained failed analysis → Retry →
Ready, and browser reconnection. The collector fixture imported its signed
unchanged query run with zero new channels and no channel-detail API request.
No unhandled UI exceptions occurred. AI was deliberately unconfigured in these
fixtures; no live AI-answer claim is made.

The exact counter adds bounded parent reservations in the same transaction.
A private 500-record PostgreSQL benchmark measured roughly 22–47 ms additional
work per batch; no throughput acceleration is attributed to this measurement.
The next substantial work is query/source attribution and a bounded coverage
trial, followed by lightweight discovery versus deep-analysis separation.

Normal production confirmation: the 13:34:54 UTC organization import refreshed
11 existing channels and inserted zero. The 13:39:42 UTC discovery/organization
round processed 1,463 channel records, inserted 9 globally new channel IDs and
merged 1,454 existing entries (distinct within each bundle). Both bundles were
acknowledged after commit; sync exited 0 with no failures or local/remote backlog.
These are per-round observations, not a daily growth forecast. Only the discovery
worker restarted, after cooperative exit 0; the other app workers continued.
The temporary app, private PostgreSQL 18543, owned fixture schemas, browser tab
and SSH forward have been cleaned up. Production PostgreSQL remains active.

## Measured live baseline

Read-only checks around 10:35–10:37 UTC:

- 269,440 indexed channels; 6,380 first discovered in the preceding 72 hours.
- Preceding 24 hours: 2,091 new channels, 33,389 successful edge searches,
  61 failed edge searches, 4,346 distinct query phrases, 1,328,402 video-result
  observations and 293,861 results absent from their immediately previous page.
- That is about 63 new channels per 1,000 successful searches. This is an
  aggregate period ratio, not causal attribution of every new channel to edge
  discovery. A video absent from the previous page is not necessarily a globally
  new video or a new channel.
- Most query phrases have approximately 500 accumulated attempts; these attempt
  counts span the worker's history, not just the last 72 hours.
- The signed plan caps each run at 125 searches with 24 network workers and a
  300-second steady interval. Its displayed target is 450 channels/hour, but
  the current estimated maximum is only 225/hour even under its assumed yield.
- The yield estimator floors estimates at 150 channels per 1,000 searches,
  above the measured period ratio. The displayed estimate is not actual growth.
- Import backlog and recent sync errors were zero at inspection. Cloud
  organization's immediate queue was empty; the radar separately reported
  192 channels in 34 running organization batches. This is not proof that all
  organization work is complete.
- The host has 2 reported CPUs, load average around 2.54, and 15.36 GB free at
  inspection. Existing video/discovery/snapshot tables dominate storage.

## Next implementation sequence

1. Measure canonical new-channel yield for each discovery source/search family,
   with explicit discovery versus refresh accounting and accurate actual rates.
   Preserve signed result/acknowledgement guarantees and reversible filtering.
2. Reduce repeated work that adds no new channels. Expand coverage using bounded,
   deduplicated exploration and productive sources; retain a separate measured
   refresh budget so existing-channel growth still gets observed.
3. Separate lightweight catalogue discovery from expensive deep analysis.
   Keep one canonical PostgreSQL index; incomplete channels must not become
   qualified opportunities merely because their IDs were discovered.
4. Run a controlled comparable throughput trial using actual committed unique
   additions, provider failures, quota consumption, CPU, app latency and storage
   growth. A first proposed target is 10,000 new channels/day; it is not a promise
   or authorization to purchase infrastructure.
5. Increase concurrency or add collector capacity only after the trial identifies
   the limiting resource and estimates its cost. Do not increase workers blindly
   on the current 2-CPU host, change paid services, or weaken provider limits.

Quality/ranking remains necessary, but larger discovery coverage is now the
next development phase. Nexlev's advertised cumulative channel count is not a
measurement of its daily collection rate or freshness; its internal collection
architecture was not established by this audit.
