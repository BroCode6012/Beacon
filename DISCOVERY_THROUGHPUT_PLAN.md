# Discovery throughput — priority updated 2026-10-03

The user wants a much larger useful channel catalogue and prioritizes collection
speed over the remaining feature roadmap. This is a diagnosis and development
sequence; no discovery settings, workers or live data were changed for this audit.

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
