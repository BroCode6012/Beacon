# Beacon — Codebase Investigation and Working Plan

Investigation date: **2026-07-10**  
Implementation status updated: **2026-08-17**

### Current web-app review — 2026-09-30

The next work is the reliable cloud foundation and PostgreSQL migration plan
in `POSTGRESQL_PLAN.md`. The current code is being recorded on paired Beacon
and shared-engine branches before migration development. The first product
milestone is durable saving with analysis in the background; PostgreSQL and
the existing live SQLite index must be compared in an isolated rehearsal
before production cutover. Shared-engine compatibility with Production Lab
and full-token/read-token authorization remain mandatory.

The first PostgreSQL foundation is now verified on the
`codex/postgresql-foundation` branches: an explicit, optional connection layer
with commit/rollback, read-only transactions, bounded lock/query waits and
credential-safe connection errors. The shared YouTubeStore now also supports
an explicitly selected PostgreSQL backend and a versioned 30-table schema.
Synthetic comparisons and a separate real web instance verified scores,
filters, memberships, exclusions, all screens and connection-error recovery.
The private test cluster remains isolated from production. Importer integration
and runtime ownership/permission checks have also passed. The durable save engine
has verified queued requests, committed membership, background analysis, bounded
retries and worker recovery. The operator's Disaster Breakdown link saved and
analysed successfully in the test schema. UI progress controls, production worker
deployment and migration/recovery gates remain before cutover. See
`POSTGRESQL_PROGRESS.md`; no live index has been copied or migrated.

Beacon now runs as a web app on Oracle. The older desktop descriptions and
measurements below are historical. The live web/Finder/gateway/radar services
run from a versioned release, and Oracle's application database is private to
that host. The laptop index mentioned below is a separate legacy/local state;
do not treat it as the web app's current database.

The September review found a Find tab that showed only its heading while the
server remained healthy. The UI now serializes redraws and displays a retry
card if a screen cannot be drawn. Background screen refreshers stop when a
browser session disconnects and start afresh on reconnect; its Autopilot
scheduler pauses without changing the operator's saved On setting. The Find
screen preserves the subject and reports a completed sweep's discovered leads,
including clear empty and error states. Cloud status reflects the scheduled
importer instead of implying that the web process needs its own signing key.

Opportunity views now reject high-confidence music, children's, and major
brand/channel names in the shared Finder's faceless/replicable query path;
this shared-engine rule also affects Production Lab queries that request that
path. Excluded records remain in the index. Agent writes have a durable
started/finished audit pair written before and after execution, with pending
changes shown for manual review and without an unsafe Undo button.

These changes were checked with regression tests and separate real Flet web
sessions using isolated Finder ports and a scratch database. The production
browser showed the recovered Find form and other screens after deployment.
The exact trigger of the original frozen tab was not captured in a stack trace,
so the redraw and session-lifetime changes are resilience fixes rather than a
claim that every possible freeze is eliminated. Off-host backup and migration
of the laptop/shared venv from Python 3.10 remain open work; Oracle's web
runtime is already Python 3.12.3.

This document is the durable map of Beacon: what it is, why it exists, how it
works, what is currently running, what was verified, where the important risks
are, and the order in which the project should be improved.

The numbers in the “live state” section are a snapshot. The radar is actively
adding data, so counts will continue to rise.

---

## 1. Executive summary

Beacon is a Windows desktop control room for finding YouTube channel and niche
opportunities. Its intended job is to help a non-technical operator:

- discover channels continuously;
- browse and combine channel filters;
- find unusually strong channels and videos;
- organize channels into niche workspaces;
- run targeted deep sweeps;
- monitor the background discovery pipeline; and
- ask an AI agent to search, organize, clean, and control the index.

Beacon is not the scraper engine and it does not own a separate database. It is
a small Flet UI and orchestration layer in `D:\Youtube\Beacon`. The heavy logic
lives in the shared Scripter engine in `D:\Youtube\Quill`, and both Beacon and
the Production Lab use the same SQLite index.

The architectural separation is good. Basic connectivity, cache correctness,
background-operation accounting, bounded query additions, Git baselines, and
the split radar pipeline are now established. The main remaining problem is
**signal quality and durable operator safety**:

- the current outlier score promotes many tiny, old, noisy, music, or malformed
  channels;
- the topic classifier visibly mislabels famous channels;
- Agent writes still need one deterministic durable audit trail;
- manual topic corrections still need provenance and locks; and
- off-laptop discovery now has a provisioned Oracle worker plus a signed SSH
  transport; continuous quality monitoring and future job types remain.

The next work should remain a quality/auditing pass, not a large new feature.

### Verified implementation status — 2026-07-12

The first correctness pass is now implemented and verified:

| Area | Current verified state |
|---|---|
| Opportunity scope | Beacon always requests faceless + replicable channels |
| Shared ranking | Optional `replicable_only` gate and stabilized `opportunity_score` |
| Loading state | Concurrent background tasks are counted; stale status text clears |
| Topic reset | Non-empty `__all_topics__` sentinel; legacy `All topics` is normalized |
| Cache freshness | TTLs, stale-while-refresh, 60-second Overview refresh, topic invalidation |
| Shared Finder cache | Successful writes clear cached derived reads |
| Autopilot queries | 600 max, 2 autonomous additions/call, 12-hour cooldown, family/near-duplicate rejection, yield/backlog gate |
| Research tracking | Successful empty provider responses count as tested; provider failures do not; incomplete passes never display as 100% |
| Always-on query top-up | Radar may add at most 2 reviewed, evidence-backed, cross-topic queries only after 100% first-pass coverage; disabled by default outside Beacon |
| Agent contracts | Workspace roles aligned; Finder filters/sorts aligned; error payloads report error status |
| Radar throughput | Independent fast-discovery and organizer lanes; 4 parallel search fetchers; 1-minute discovery rest |
| Durable radar work | Coalescing lease-based queue beside the shared index; interrupted work safely returns to the queue |
| Off-laptop foundation | Signed recurring plans, lightweight Oracle-ready collector, durable scheduler, signed result bundles/acks, idempotent local import, Finder-gated status/manual sync, hardened immutable Oracle deployment package |
| Operator tracking | Overview distinguishes channels waiting from channels actively being organized |
| Dependencies | `scikit-learn` and `joblib` declared in shared requirements |
| Git baseline | Beacon and Scripter now have separate safe repositories with secrets/data/generated state ignored |
| Beacon tests | **41 passed, 13 parameterized subtests passed** |
| Shared tests | **178 passed** |
| Package health | `pip check`: no broken requirements |

Every atomic change above also passed a separate real Flet launch and visual
smoke test of Overview, Channels, Real Time, Find, and Agent. Verification
instances used isolated Finder ports and disabled Autopilot. Logs contained no
exceptions; the only stderr output was the known Python 3.10 support warning.

### Verified interface redesign — 2026-07-31

Beacon now uses a light “Daylight Flight Deck” design across all five screens:
an ink-blue navigation rail, cool paper canvas, white evidence surfaces,
signal-green state marks, cobalt analysis accents, and restrained motion. The
direction takes inspiration from Nexlev's creator-tool clarity without copying
its branding or weakening Beacon's operations-and-evidence identity.

The redesign is Beacon-only. It did not change the shared engine, live database,
watchlist, radar state, Finder authorization model, or Agent write boundaries.

Verified behavior:

- Overview separates research coverage, index health, and discovery operations
  into plain-language operational surfaces.
- Channels and Real Time visibly enforce the faceless + replicable scope.
- Opportunity cards show eligibility, core metrics, evidence strength, named
  qualification factors, and a plain-language score rationale.
- Find clearly separates starting a deep sweep from reviewing the current
  opportunity shortlist.
- Agent places the investigation input first, renders Markdown responses, and
  presents Autopilot history as compact expandable audit entries.
- Controls wrap and cards remain readable at the enforced 1040×700 minimum
  window size without clipping sort values or metric labels.
- All **45 Beacon tests** passed, `pip check` reported no broken requirements,
  and an isolated real Flet instance passed visual checks of loading, empty,
  success, expanded-history, and responsive states across all five screens.

The radar speed pass was also verified against the live worker:

- the 50-query harvest stage fell from **7 minutes 54 seconds** to
  **1 minute 48 seconds** in measured cycles;
- the faster cycle still added **85 new channels**;
- network searches overlap across four workers, while SQLite writes and
  per-query yield accounting remain serialized and deterministic;
- post-harvest faceless/category work now processes only missing channels
  (about 3.4 seconds for four live missing channels instead of rereading the
  full 61,000-channel index);
- workspace snapshots are batched; and
- expensive workspace alert rebuilding is limited to once per 60 minutes in
  Beacon's live config instead of every cycle.

### Verified Niche Watch and Hermes Niche Desk — 2026-08-17

Beacon now exposes the niche-intelligence layer without adding a sixth screen:

- Overview places **Niche Watch** below the refresh-status row. It combines a
  deduplicated unread-alert feed with workspace channel/video sizes,
  automatic-upkeep state, signal counts, and direct workspace/evidence actions.
  Severity is written in every alert label, read acknowledgement has an
  immediate undo, and large alert/workspace lists expand progressively.
- Channels shows a contextual **Niche workspace** brief when a workspace is
  selected. It keeps workspace and Topic concepts separate, shows a 2×2 metric
  layout at the 1040×700 minimum, and provides reversible pause/resume controls.
- Agent includes one **Autonomous work** ledger for Oracle discovery and
  organization, niche upkeep, Hermes's latest brief, and Beacon Autopilot.
- Alert acknowledgement and automatic-upkeep changes go through full-token
  Finder writes. No direct SQLite access was added to `beacon_app.py`.

The shared Finder registry gained additive, schema-free tools:

- `list_niche_signals` returns the newest alert per
  `(niche_id, entity_type, entity_id, alert_type)` plus workspace summaries;
- `mark_niche_alert_read` changes exact alerts or one niche and is reversible;
- `set_niche_autonomous` pauses/resumes workspace snapshots, alerts, and upkeep;
  and
- `publish_niche_signal` accepts only bounded, validated operator/Hermes
  notification types.

Hermes now has a bounded Niche Desk coordinator:

- `signal_niche_operator.py` reads and writes only through Finder and keeps a
  durable JSONL audit;
- the scheduled Hermes skill contract requires one bounded packet to be
  delegated to exactly three leaf roles—Scout, Classifier, and Curator—while
  `signal_niche_operator.py` remains the single write coordinator;
- topic writes are missing-only, organization is capped at eight fresh
  candidates with confidence at least `0.85`, and notifications are capped at
  three;
- cron job `9de4bd55d5a2` runs daily at **08:30 local time**; its first scheduled
  run is **2026-08-18 08:30 +01:00**; and
- the six-hour freshness watchdog now targets Beacon's real live index. Its
  forced **2026-08-17 15:31 +01:00** run completed successfully without the
  former missing-table crash and reported three stale pipeline signals for
  attention.

The first live bounded operator cycle completed on **2026-08-17**:

- missing-only topic classification changed `0` rows;
- six reviewed channels were added to existing workspaces, with
  `untrack_competitor` as the documented undo;
- three Hermes notifications were published with `0` duplicates; and
- Beacon visibly showed the three fresh briefs and updated workspace sizes.

Verification passed **53 Beacon tests**, `pip check`, and **155 shared tests**
with only the pre-existing dirty thumbnail-persistence module excluded. An
isolated seeded-index workflow covered loading, success, empty, error,
recovery, mark-read/undo, progressive alert/workspace expansion, pause/resume,
and 1040×700 responsiveness. A second isolated Finder/app pair then opened the
live index read-only for verification and smoke-tested Overview, Channels,
Real Time, Find, and Agent. Logs contained no exceptions; only the known
Python 3.10 support warning appeared.

The split-pipeline pass was implemented and live-verified later on 2026-07-11:

- discovery no longer waits for enrichment, cataloging, filters, niche crawling,
  scoring, or workspace maintenance;
- Beacon's live config uses `split_pipeline_enabled: true` and
  `discovery_interval_minutes: 1`;
- the existing heavy radar cycle runs as the organizer with harvest disabled;
- newly found channels create small durable organization batches in
  `storage/radar_work_queue.sqlite3`;
- queued batches are coalesced, leased to one organizer, recovered after stale
  leases, and returned to the queue if a cooperative stop interrupts the cycle;
- the organizer waits for the first discovery pass at process startup, so the
  first new batch is claimed immediately instead of waiting a full maintenance
  cycle;
- Production Lab remains on legacy behavior by default because split mode is
  opt-in at configuration level; and
- Beacon Overview shows the two lanes plus exact waiting and actively-organizing
  channel counts.

Measured live burst:

- **24 discovery passes** between 22:28 and 23:26;
- **1,830 new channels** found;
- **76.2 new channels per pass** on average; and
- passes completed roughly every two to three minutes, including the configured
  one-minute rest.

This is a measured burst, not a permanent daily guarantee. Yield will change as
queries become saturated and are revisited.

The always-on query-top-up pass was deployed and live-verified on 2026-07-12:

- never-attempted variations are pulled into the next discovery slice instead
  of waiting for a full rotation;
- a successful provider response counts as tested even when it finds zero
  videos, while provider failures remain retryable;
- the radar and Beacon Autopilot share the same cross-process write lock,
  maximum size, duplicate-family rules, and cooldown record;
- the radar selects only from a reviewed faceless/replicable subject catalog
  and requires evidence across several already-indexed faceless channels;
- each run may add at most two queries from different topics, only after 100%
  first-pass coverage; and
- Production Lab/legacy configurations remain unchanged because the feature is
  disabled unless explicitly enabled.

Current live query-policy snapshot after rollout:

- 424 base queries;
- hard ceiling 600;
- 4,164 expanded searches;
- 4,164 searched at least once (100%);
- 0 expanded searches untested; and
- no radar query added during rollout because the existing Autopilot addition
  correctly kept the shared 12-hour cooldown active.

The off-laptop edge foundation was implemented and verified later on
2026-07-12:

- the canonical SQLite index stays physically in Beacon storage and is never
  copied, mounted, or opened by Oracle;
- Beacon can snapshot the current radar configuration into a signed,
  immutable, versioned discovery plan (the live snapshot contains exactly
  4,164 expanded searches);
- the edge worker prioritizes every never-tested query before revisiting old
  ones, then switches repeat searches to newest-upload order;
- unchanged searches are temporarily retired and periodically reprobed;
- provider failures remain retryable, while successful empty searches count as
  tested;
- free `scrapetube` searches run in bounded parallel batches, then the rotating
  YouTube key pool is used only for cheap batched video/channel enrichment;
- per-run quota is capped, exhausted-key knowledge is retained for the UTC day,
  and the pool resets for the next quota day;
- output is an append-only signed `youtube.discovery.v1` bundle containing
  channels, videos, discovery links, snapshots, and per-query outcomes;
- the laptop importer verifies signature/checksum, imports all rows in one
  transaction, records the bundle in an idempotency ledger, and writes a signed
  acknowledgement;
- the shared sync service reports only non-secret inbox/worker state, imports a
  bounded inbox slice, archives accepted or duplicate bundles, and leaves
  rejected bundles quarantined for inspection;
- Beacon accesses that service only through Finder: status is read-authorized,
  importing requires the full write token, successful imports invalidate local
  caches, and the Overview card explains that Oracle never owns the main
  database;
- the Oracle deployment package builds an allowlisted archive from one committed
  Git revision, refuses to include local/generated state, installs a
  self-contained Python 3.11+ release, and atomically switches versions without
  touching persistent worker state;
- the laptop SSH transport streams signed bundles over port 22, verifies them
  before publishing to the inbox, imports them atomically, returns signed
  acknowledgements, and removes only acknowledged remote outbox copies;
- its systemd service runs as an unprivileged `signal-edge` account, exposes no
  application port, grants write access only to `/var/lib/signal-edge`, and
  supports explicit health checks and symlink rollback;
- a real isolated network smoke collected one query into 3 videos/3 channels
  at zero API quota and imported it into a temporary index; and
- the full regression gate passed with 178 shared tests, 41 Beacon tests plus
  13 subtests, all five Beacon screens, and an isolated Production Lab window.

The laptop synchronization path was accelerated and live-verified on
2026-07-13:

- up to 20 immutable bundles are downloaded through one tar stream instead of
  opening one SSH process per file;
- signed acknowledgements are verified locally, uploaded in bounded batches,
  and remote files are still removed only after successful local import;
- one initialized SQLite connection is reused across the drain while every
  bundle keeps its own verification, savepoint, ledger row, acknowledgement,
  and archive step;
- the Windows task now drains up to 100 bundles every two minutes through a
  windowless `wscript.exe` wrapper, returns the real sync exit code, and never
  opens a terminal while the operator is using the laptop;
- a live optimized sync imported two new bundles end-to-end in about 18 seconds
  with zero errors and left the remote queue empty;
- an isolated 20-bundle benchmark imported 15,873 channel records in 18.64
  seconds; and
- the regression gate passed with 190 shared tests and 42 Beacon tests plus
  13 subtests.

The first cloud-organization stage was deployed and live-verified on
2026-07-13:

- the Oracle collector now assigns every discovered channel a deterministic
  clean topic before signing its result bundle;
- it scores the videos already present in that bounded batch with the shared
  `text-v3` faceless classifier in balanced mode;
- signed bundles may include `channel_classifications`; Beacon imports those
  records into the one canonical database and keeps a stronger local
  classification when it has a larger evidence sample;
- imported cloud topics only fill channels that do not already have a local
  topic, so existing local/manual topic decisions still win;
- the deployed Oracle release produced a real bundle containing 895 channels,
  895 non-empty topics, and 895 text classifications;
- Beacon imported all 895 channel/topic/classification records, signed the
  acknowledgement, and left the remote queue empty; and
- the regression gate passed with 192 shared tests, 42 Beacon tests plus
  13 subtests, and visual smoke tests of Overview, Channels, Real Time, Find,
  and Agent.

The changing-home-IP gap was closed and live-verified on 2026-07-13:

- a separate OCI API signing key is stored under the Windows user profile with
  a restricted local ACL; its public half is registered in Oracle;
- the sync runner checks the laptop's public IPv4 before SSH and keeps exactly
  one managed TCP/22 ingress rule restricted to that address as a `/32`;
- the updater adopts only one unambiguous SSH rule, uses Oracle's ETag, and
  preserves every unrelated ingress rule, egress rule, display name, and tag;
- the OCI SDK runs in `storage/oci_updater_venv`, isolated from the shared
  scraper environment because Lightning pins an older `urllib3`;
- the laptop IP changed during rollout, the prior scheduled sync failed, the
  updater replaced the stale `/32`, and SSH/sync recovered automatically;
- subsequent hidden two-minute tasks completed with result `0`, no transport
  errors, and zero remote bundles pending; and
- the regression gate passed with 197 shared tests, 42 Beacon tests plus
  13 subtests, and another visual smoke test of all five Beacon screens.

The organization-throughput and cloud-control path was tightened and
live-verified later on 2026-07-13:

- every successful laptop sync now compares the live radar watchlist and edge
  settings with the last signed discovery plan and publishes a new immutable
  plan only when that specification changes;
- the current 4,206-search plan was accepted by Oracle and all 4,206 query
  states are present in the durable worker state;
- automatic plan publication is disabled for custom/test sync roots unless it
  is explicitly enabled, preventing isolated tests from touching the live VM;
- the laptop radar is intentionally running with local harvesting disabled
  while Oracle supplies discovery, so its cycles prioritize imported-channel
  organization instead of repeating the same searches;
- organization-only cycles skip network expansion, lab sync, intel fuel,
  remote VLM, niche crawling, whole-index outlier rescoring, and competitor
  tracking, while retaining enrichment, cataloging, categorization, topic,
  junk, face, and text-classification work;
- stale query text no longer masks the organizer's current status in the radar
  status file or Beacon Overview;
- Oracle free-search failures now receive at most three bounded retries only
  for transient JSON, connection, timeout, HTTP 429, and HTTP 503 failures,
  with exponential backoff and jitter;
- the first production bundle after that deployment completed 49 of 50
  searches, recovered 14 transient attempts, and delivered 931 channels with
  topics and classifications, compared with 8-12 failed searches in nearby
  pre-deployment batches; and
- the final regression gate passed with 205 shared tests, 43 Beacon tests plus
  13 subtests, and visual verification of Overview, Channels, Real Time, Find,
  and Agent in a separate real Flet window.

The durable deep-organization worker was deployed and live-verified later on
2026-07-13:

- Oracle now runs a second hardened systemd service,
  `signal-edge-organizer.service`, independently from discovery;
- discovery emits signed `youtube.organization.job.v1` work only for channels
  attached to newly discovered videos, splits it into 100-channel jobs, and
  keeps the protocol hard limit at 500 channels/10,000 videos;
- the organizer keeps only operational cursor/retry state on Oracle. It never
  opens, copies, mounts, or replaces Beacon's canonical SQLite index;
- channel enrichment resolves each official uploads playlist, catalogs up to
  30 recent uploads through low-cost `playlistItems.list`, enriches videos and
  categories in batches, and uses the free channel scraper only as a fallback;
- every result also carries deterministic topic and `text-v3` classification
  evidence plus bounded CPU face checks using models stored under Oracle's
  persistent state directory;
- signed `youtube.organization.v1` results are imported atomically by the
  laptop sync. Stronger/newer local classification and face evidence remains
  authoritative, exclusions stay reversible, and duplicate job revisions are
  ignored;
- the first two official-catalog production jobs completed consecutively with
  zero channel errors: 124 channels/2,510 videos in 147.7 seconds, then
  157 channels/3,443 videos in 213.0 seconds. They collected 5,292 uploads,
  filled 5,666 category assignments, and consumed 300 YouTube quota units;
- the latest discovery run created three bounded jobs for 280 channels with
  newly discovered videos, no enqueue error, eight catalog workers, and two
  face workers. Repeated results without a new video are no longer requeued;
- the automatic two-minute laptop sync imported and acknowledged both
  production results with no transport/import errors and no remote outbox
  backlog; and
- the regression gate passed with 226 shared tests, 43 Beacon tests plus
  13 subtests, and real-window verification of all five Beacon screens.

The laptop split-radar organizer was repaired and live-verified on
2026-07-14:

- a live stack capture showed the organizer lane had exited while the main
  thread waited forever for the still-running discovery thread;
- the split pipeline now owns an in-process shutdown event and always wakes the
  discovery lane before joining it, including unexpected organizer exits;
- starter-workspace refresh now reuses persisted channel classifications and
  falls back to classification only on a fresh index, instead of materializing
  all 1.6 million indexed videos on every maintenance pass;
- the deadlocked worker was stopped cooperatively and replaced without stopping
  Beacon's canonical database, Oracle discovery, Oracle organization, or the
  hidden two-minute sync;
- the repaired worker completed a full live organizer cycle at 01:12
  Casablanca time, crossed the former failure point, refreshed alerts across
  11 workspaces, and started its next cycle normally;
- normal non-vision stages held roughly 130 MB of private memory instead of the
  old process's 2.38 GB whole-index rescan. The bounded 10-worker OpenCV face
  stage still has a temporary higher memory peak;
- the regression gate passed with 228 shared tests, 43 Beacon tests plus
  13 subtests, and two separate real-window smoke passes covering Overview,
  Channels loading/loaded, Real Time loading/loaded, Find, and Agent.

The overnight catch-up and repeated-payload bottleneck was repaired and
live-verified later on 2026-07-14:

- the Oracle organizer now removes both channels and their attached videos when
  its durable state says that channel was already organized inside the 30-day
  refresh window; video-only repeat jobs are skipped without producing another
  large result bundle;
- discovery results are change-aware: every query run is still signed and
  retained, but unchanged channel/video rows, classifications, discovery links,
  and snapshots are omitted; new results are emitted immediately and an
  existing video is emitted again when its observed view count changes;
- the first production discovery bundle after deployment examined 2,000 result
  observations, suppressed 1,444 unchanged repeats, retained all 50 query-run
  records, and emitted only 481 video rows plus 336 channel rows;
- a controlled 40-result repeat bundle fell from 3,061 compressed bytes to 653
  bytes, a 78.7% reduction;
- the laptop importer verifies each immutable bundle independently, isolates
  each one with a SQLite savepoint, durably commits groups of 20, and writes
  signed acknowledgements only after that group commits; a malformed middle
  bundle no longer prevents valid neighboring bundles from importing;
- the hidden task still starts every two minutes, but after a wake it now keeps
  draining consecutive 100-bundle slices until both queues are empty or its
  bounded 90-minute catch-up budget ends;
- the immutable Oracle release at shared-engine revision `bb567a5` passed its
  checksum and pre-switch smoke test; both `signal-edge.service` and
  `signal-edge-organizer.service` are active on that release;
- a temporary internet interruption left 15 bundles waiting, after which the
  OCI `/32` updater and hidden task recovered automatically; the final task
  returned `0`, imported a 28-channel/365-video organization result, and left
  both the laptop and Oracle queues at zero; and
- the final regression gate passed with 232 shared tests, 43 Beacon tests plus
  13 subtests, `pip check`, and repeated real-window verification of Overview,
  Channels, Real Time, Find, and Agent.

The multi-key throughput path was then upgraded and live-verified later on
2026-07-14:

- the old rotating API pool was replaced by a thread-safe scheduler that gives
  each healthy key one concurrent lane, preserves batch ordering, and moves
  pending work away from a key that becomes exhausted or unusable;
- batched channel/video enrichment, Oracle upload-playlist cataloging, and
  category enrichment now run across those lanes while existing per-job quota
  ceilings remain enforced;
- concurrency scales automatically as future keys are added, using a
  configurable 16-lane default ceiling and a hard defensive maximum;
- a hidden-input enrollment command securely merges any number of future keys
  into the laptop and Oracle environments, deduplicates them, sends values
  through SSH standard input rather than command arguments, and displays only
  counts;
- the immutable production release is shared revision `cdea38e`; both Oracle
  services are active;
- all three newly supplied keys passed live calls. One permanently forbidden
  older key was removed, leaving six configured and six healthy keys;
- a real 300-channel test created six simultaneous 50-ID batches and reported
  exactly one quota unit on every one of the six key lanes;
- the next hidden sync returned `0`, imported and acknowledged fresh work, and
  left local and Oracle pending counts at zero; and
- the final regression gate passed with 240 shared tests, 43 Beacon tests plus
  13 subtests, `pip check`, and repeated visual verification of all five Beacon
  screens.

Turbo Discovery was then deployed and live-verified later on 2026-07-14:

- the signed Oracle plan now schedules up to 125 searches every five minutes
  through 24 bounded network workers, replacing the former 50-search/10-worker
  setting while preserving the 5,000-result protocol ceiling;
- the laptop measures recent canonical yield over a six-hour window and places
  only a rounded, bounded estimate in the signed plan. A 30-point hysteresis
  prevents small measurement changes from publishing needless revisions;
- the controller targets at least 450 new channels/hour. At the current
  conservative 350 channels per 1,000 successful searches, full speed estimates
  525 channels/hour before normal yield variation;
- if more than 5% of a cycle's searches fail, Oracle reduces the next batch by
  25 searches, doubles the rest interval, and never drops below 50. Two healthy
  cycles restore one 25-search step until full speed returns;
- the bounded base-query ceiling increased from 450 to 600, while the two-query
  autonomous limit, evidence requirement, normalized similarity deduplication,
  per-topic limits, and 12-hour cooldown remain unchanged;
- the first four imported production Turbo cycles completed 500/500 searches
  with zero failures. The canonical index gained 161 genuinely new channel IDs
  and 5,181 new video IDs from the same window; 161 channels per 500 searches
  projects to about 483 channels/hour, or 11,592/day, if that measured yield
  holds;
- discovery now reads the organizer's durable 30-day channel history before
  enqueueing signed jobs, so repeat channels never enter the queue. This is a
  read-only SQLite lookup against Oracle's small operational state, not
  Beacon's canonical index;
- the first post-dedup production cycle created two jobs instead of the prior
  roughly nine repeat-heavy jobs. The existing backlog fell from 71 to 67 while
  fresh work continued arriving;
- immutable shared revision `a3ab046` is active on both Oracle services; and
- the final regression gate passed with 243 shared tests, 38 Beacon tests,
  `pip check`, and repeated visual verification of Overview, Channels loading
  and loaded, Real Time loading and loaded, Find, and Agent.

The laptop catch-up importer was then permanently accelerated and instrumented
on 2026-07-15:

- repeated per-row Python-to-SQL calls are now buffered into prepared bulk
  writes while preserving every bundle savepoint, idempotency ledger,
  conditional merge rule, transaction boundary, and signed acknowledgement;
- the import connection uses a larger connection-local SQLite page cache and
  memory-backed temporary storage without weakening commit durability;
- cloud records no longer carry or store redundant full YouTube API objects.
  Video records retain the category ID used by the canonical index, and channel
  records retain the thumbnail data used by Beacon cards; parsed titles,
  descriptions, counts, duration, URLs, timestamps, and categories remain in
  their normal columns;
- on an identical disposable 500,000-video index, five large production bundles
  fell from 5.434 seconds to 1.352 seconds, a measured **4.02x speedup**. The
  resulting database was about 70 MB smaller for that sample, and equivalent
  future compressed bundles measured 28.8% smaller;
- the sync now writes durable committed-bundle progress, current-round totals,
  combined laptop/Oracle backlog, rate, and ETA. Beacon's Cloud worker card
  displays that progress and disables overlapping manual sync attempts;
- immutable shared revision `060c8f2` is active on Oracle release
  `20260715T132751Z-330208`. Its first production bundle contained 1,410
  channels and 2,219 videos, used only the approved slim raw fields, imported
  end-to-end in 13.859 seconds, and left the remote outbox empty; and
- the final regression gate passed with 245 shared tests, 44 Beacon tests plus
  13 subtests, and real-window verification of the progress card and all five
  Beacon screens.

Cloud-import progress visibility was tightened on 2026-07-16:

- the importer now publishes non-secret live phases before a transaction
  commits: verifying, waiting for the database writer, staging each bundle,
  committing, and acknowledging;
- the durable committed count remains separate and still advances only after
  the full transaction commits, preserving the existing safety guarantee;
- Beacon shows both counters, explains why `0 committed` can still mean active
  work, and refreshes the Cloud worker card every three seconds during an
  import without requiring the operator to press Refresh;
- imports started by the previous release show an explicit compatibility
  explanation, while the next sync automatically gains detailed live steps;
  and
- verification passed with 249 shared tests, 48 Beacon tests plus 13 subtests,
  and real-window checks of both progress modes and all five Beacon screens.

Cloud-import batch handoff and WAL maintenance were corrected on 2026-07-17:

- signed local acknowledgements are still created only after their database
  transaction commits, but a single coalescing background worker now sends
  them to Oracle in the existing 20-file SSH batches while the importer starts
  the next database transaction;
- the importer waits for any final acknowledgement batch before returning.
  Failed uploads leave the signed local acknowledgement in place for the next
  scheduled retry, so transport overlap does not weaken delivery safety;
- a separate coalescing WAL worker performs passive checkpoints after committed
  import groups. When the WAL reaches 128 MiB it makes a bounded truncate
  attempt: 250 ms during active importing and 1.5 seconds after the importer
  connection closes. Busy attempts remain retryable instead of blocking the
  next batch;
- each sync result now records WAL attempts, truncate attempts/successes, busy
  attempts, errors, and the last before/after byte counts;
- a concurrency regression test held the first SSH acknowledgement batch open
  and proved the next database transaction continued while Oracle could see
  only already-committed bundles. A real SQLite regression test grew and then
  truncated an oversized WAL; and
- verification passed with 252 shared tests, 55 Beacon tests plus 16 subtests,
  and two isolated real-window passes of Overview, Channels, Real Time, Find,
  and Agent.

This layer is deliberately transport-agnostic. SSH/SFTP, OCI Object Storage, or
another transport can move the same immutable files later without changing the
worker, canonical database, or import contract. New cloud capabilities should
be added as new versioned job/result kinds rather than hard-coded Oracle-only
behavior.

The remaining high-value work is:

1. add deterministic durable write auditing for Agent actions;
2. build a human-reviewed opportunity benchmark and improve score/topic quality;
3. protect manual topic corrections with provenance/locks;
4. improve Agent/Autopilot Markdown and input layout;
5. continue replacing remaining full-index organizer scans with incremental work;
6. move additional measured organization stages to signed, versioned cloud job
   types without giving Oracle database access; and
7. upgrade the shared runtime to Python 3.11+ before **2026-10-04**.

---

## 2. System boundary

There are three code/data ownership zones.

### A. Beacon-owned code

Location: `D:\Youtube\Beacon`

Beacon owns:

- the Flet desktop window and five screens;
- Finder service discovery/self-hosting;
- UI state, caching, cards, filters, and paging;
- start/stop controls for the radar worker;
- the Beacon-specific Bedrock agent;
- the optional Autopilot loop; and
- the Windows screenshot/click verification helper.

Changes here affect Beacon only.

### B. Shared Scripter engine

Location: `D:\Youtube\Quill\scripter`

The engine owns:

- the Finder HTTP API and tool registry;
- channel metrics and filter SQL;
- niche workspaces;
- crawling and provider routing;
- the versioned edge plan/collector/worker/import protocol;
- the continuous radar cycle;
- topic classification;
- faceless classification;
- category backfilling;
- junk/language/kids exclusions;
- outlier and velocity logic; and
- Production Lab services.

Changes here can affect both Beacon and the Production Lab. Every engine change
must be labeled as shared-impact work.

### C. Shared live data

Primary database:

`D:\Youtube\Beacon\storage\youtube_index.sqlite3`

Supporting operational state remains under `D:\Youtube\Quill\storage` and
includes:

- `scraper_watchlist.json`
- `radar_status.json`
- `radar_worker.pid`
- `radar.log`
- `harvest_yield.json`
- `radar_work_queue.sqlite3`
- `faceless_text_model.joblib`

The database is approximately 24 GB (2026-09-11) and runs in WAL mode. It must never be moved
or copied casually. Beacon owns the physical file. Production Lab resolves that
same shared index separately from its writable per-session database.

`radar_work_queue.sqlite3` is not a second product database. It contains only
small operational work records and can never replace the shared YouTube index.
Likewise, the Oracle edge worker may keep a small JSON cursor/status file and
append-only result bundles, but it does not own a searchable YouTube database.

The relocation was completed and verified on 2026-07-12: writers were stopped
cooperatively, the WAL was checkpointed, source and staged-copy SHA-256 hashes
matched, both copies passed SQLite checks before cutover, both desktop apps and
all regression suites passed on the new path, and the old Scripter database
file plus its temporary code fallback were removed.

---

## 3. Architecture

```text
User
  |
  v
Beacon Flet app (beacon_app.py)
  |                         \
  | FinderClient             \ BeaconAgent / Autopilot
  v                           \        |
Local Finder HTTP API                  AWS Bedrock
(local_api.py)                         |
  |                                    v
  v                              tool dispatch
IntelligenceAnalystTools          / finder / sweep / UI
  |
  +--> channel metrics / topics / exclusions
  +--> niche workspace services
  +--> deep sweep and crawler commands
  |
  v
YouTubeStore
  |
  v
Shared SQLite database

Beacon Overview
  |
  v
sweep_control.py
  |
  v
detached radar worker
  |
  +--> fast discovery lane --> durable organization queue
  |
  +--> organizer lane
        +--> YouTube API enrichment/category pool
        +--> topic + faceless + junk classifiers
        +--> cataloging + niche crawling
        +--> outlier scoring + workspace maintenance
  |
  v
Shared SQLite database

Beacon/controller
  |
  +--> signed discovery plan
          |
          v
      signed SSH transport over the existing management connection
          |
          v
      Oracle edge worker
          +--> free parallel searches
          +--> bounded official API enrichment
          +--> durable query cursor/yield state
          |
          v
      signed immutable result bundles
          |
          v
      local verified/idempotent importer
          |
          v
      Shared SQLite database
```

### Important architectural rule

Normal Beacon UI features should go through `FinderClient`. They should not
embed direct SQLite queries or import internal scraper services. Direct database
access is acceptable for diagnostics such as `check_engine.py`, not for normal
screen behavior.

---

## 4. Startup and runtime flow

1. `beacon_app.py` imports `engine_link` first.
2. `engine_link.py`:
   - places `D:\Youtube\Quill` on `sys.path`; and
   - sets `YOUTUBE_INDEX_DB` unless the environment already overrides it.
3. Beacon looks for a shared Finder token file, then tries the configured Finder
   URL.
4. If no Finder is reachable, Beacon creates the Finder server in a daemon
   thread inside its own process.
5. Beacon creates the optional Autopilot controller and starts it only when the
   saved state says it is enabled.
6. The Flet shell renders the current section.
7. Each data screen lazily starts background Finder calls and caches their
   results in memory.
8. The radar worker is independent. It keeps running after Beacon closes.

The current running Beacon instance is self-hosting its Finder on port `8799`.
The default port `8765` was not listening during this investigation.

---

## 5. The five screens

### Overview

Purpose:

- show index counts;
- show radar status;
- start or cooperatively stop the radar; and
- edit the watchlist.

Data:

- index counts come from the `db_stats` Finder tool and are cached;
- radar status and watchlist are read directly through `sweep_control`.

Current behavior:

- Overview counts refresh automatically every 60 seconds;
- successful writes invalidate the relevant Beacon and Finder caches;
- Niche Watch shows deduplicated unread alerts and true workspace sizes;
- workspace evidence time and alert-detection time remain separate clocks;
- the research tracker shows exact tested/remaining counts; and
- near-complete progress uses enough precision that it cannot display 100%
  while searches remain;
- research coverage, index counts, scraper state, queue state, and cloud
  collection are grouped into distinct plain-language operational surfaces.

### Channels

Purpose:

- browse the whole eligible channel index;
- filter by curated niche workspace;
- filter by clean topic;
- combine subscriber, average-view, age, outlier, category, and format filters;
- sort results; and
- page deeper in batches of 120.

The filters are combined in one engine SQL query. This is the right design.

Current presentation:

- the faceless + replicable eligibility gate is visible beside the controls;
- niche, topic, sort, filters, targeted scraping, and presets remain combinable;
- a selected niche shows its curated workspace size, unread alerts, upkeep
  state, and reversible pause/resume control before the channel grid;
- cards use a flat divided metric band rather than nested metric tiles;
- every card explains why it qualifies using sample size, view leverage,
  channel recency, evidence strength, and a plain-language rationale; and
- controls wrap and evidence cards remain legible at the minimum window size.

Classifier and corpus quality remain ranking concerns rather than UI concerns.
The interface now makes the evidence easier to inspect instead of hiding those
quality signals.

### Real Time

Purpose:

- show channels by index discovery time (`first_seen_at`);
- filter by 24 hours, 7 days, 30 days, or newest;
- combine topic and metric filters; and
- auto-refresh every 60 seconds.

Important distinction:

- `first_seen_at` means when this system found the channel;
- `channel_published_at` means when the YouTube channel was created.

Current behavior:

- the `All topics` sentinel is normalized and no longer becomes a literal
  filter;
- discovery time is explicitly explained as different from channel age;
- loading, empty, and populated windows have distinct plain-language states;
  and
- recent cards reuse the same qualification evidence as Channels while adding
  discovery recency.

### Find

Purpose:

- launch a broad deep sweep over approximately 100 generated search angles; and
- load faceless channels from the index.

The current deep sweep:

- writes discoveries into the shared index;
- does not auto-track a workspace because Beacon passes no `niche_id`;
- has no progress stream in the UI;
- has no cancellation control;
- uses the Finder client’s 600-second HTTP timeout; and
- can be started again while another background operation is already running.

The redesigned surface clearly separates launching a new sweep from loading the
current replicable-opportunity shortlist and explains that both use Beacon's one
live index.

### Agent

Purpose:

- expose a Bedrock Claude agent with Finder, scraper, and UI-control tools;
- let the agent search, count, organize, clean, and control the app; and
- manage Autopilot.

The agent is intentionally separate from Production Lab chat code.

Current presentation:

- the investigation input is the first primary surface;
- the Autonomous work ledger distinguishes discovery/organization, niche
  upkeep, the latest Hermes brief, and local Autopilot;
- the empty state offers plain-language example prompts;
- writes are visibly described as reversible;
- Agent and Autopilot Markdown renders as structured content;
- tool calls are progressive-disclosure technical details; and
- Autopilot history is summarized in compact, timestamped, expandable audit
  entries.

---

## 6. Core data concepts

### Topic

A topic is one keyword-classified label stored on every channel, such as:

- History
- Space & Physics
- True Crime & Scams
- Tech & AI
- Money & Finance
- Other

It is derived from discovery category, title, and the first 300 description
characters. Rules are ordered and first-match-wins.

### Niche

A niche is a curated workspace in the `niches` and `niche_channels` tables. A
channel can belong to multiple niche workspaces.

Topic and niche are not interchangeable.

### YouTube category

`videos.category_id` is YouTube’s first-party content category. It is used
especially to identify music channels. It is independent of topic and niche.

### Faceless status

The channel browser uses this precedence:

1. VLM verdict;
2. recurring-face CPU verdict;
3. text-model prediction;
4. legacy keyword classifier.

An explicit `on_camera` verdict overrides lower-priority faceless predictions.

### Outlier score

The current channel outlier formula is approximately:

```text
average lifetime views per upload
---------------------------------  × age boost
          subscribers
```

The age boost favors newer channels.

This formula is fast, but it is not yet robust enough to be the primary
opportunity signal. It can explode when subscribers are extremely low or
incorrect and can reward old channels with one historical viral upload.

---

## 7. Local file map

### `beacon_app.py`

The ~5,250-line desktop application:

- service resolution;
- navigation;
- agent UI;
- Autopilot UI;
- card rendering;
- in-memory cache;
- Overview;
- Channels;
- Real Time; and
- Find.

### `beacon_agent.py`

The Bedrock tool-use loop:

- 30 exposed tool specifications;
- engine tools;
- local scraper/watchlist tools;
- UI-control tools;
- up to 18 tool rounds; and
- optional restricted mode for Autopilot.

### `beacon_autopilot.py`

Optional in-process maintenance:

- saved enable/interval state;
- a restricted tool set;
- a fixed maintenance prompt;
- an activity log in `_autopilot.json`; and
- a daemon scheduling thread.

### `sweep_control.py`

Beacon-side radar control:

- read status;
- inspect the pidfile;
- launch the detached worker;
- write the stop flag; and
- read/write the watchlist.

### `engine_link.py`

The bridge to the shared engine and database.

### `finder_service.py`

A standalone Finder launcher. Beacon currently also supports in-process
self-hosting.

It never holds a console window, however it is started. `start_finder_hidden.vbs` runs
it under `pythonw.exe` directly; starting it with plain `python.exe` makes it hand
itself off to `pythonw.exe` and exit, which closes the console. Output goes to
`storage/finder_service.log`. Launching twice is safe: the second one sees a healthy
`/health` and steps aside. Set `FINDER_CONSOLE=1` to watch it in the foreground.

### `check_engine.py`

Read-only database connectivity proof.

### `_win.ps1`

DPI-aware native UI verification:

- find and focus the Beacon window;
- click fractional coordinates;
- type;
- scroll; and
- capture the desktop.

### `CLAUDE.md`

The previous operational memory and implementation history. It remains useful,
but some counts, tool totals, behavior notes, and “current state” claims are now
stale. Use this guide as the verified current map and `CLAUDE.md` as historical
context.

---

## 8. Shared engine paths Beacon relies on

- `scripter/intelligence/finder_client.py`
- `scripter/intelligence/local_api.py`
- `scripter/intelligence/analyst_tools.py`
- `scripter/intelligence/workspaces.py`
- `scripter/research/store.py`
- `scripter/research/radar.py`
- `scripter/research/crawler.py`
- `scripter/research/channel_discovery.py`
- `scripter/research/deep_sweep.py`
- `scripter/research/topic_classifier.py`
- `scripter/research/junk_filter.py`
- `scripter/research/faceless_text_model.py`
- `scripter/research/categorize.py`
- `scripter/research/youtube_pool.py`
- `scripter/research/providers/scrapetube_provider.py`

---

## 9. Verified live state

Snapshot taken while the radar was actively writing:

| Item | Verified value |
|---|---:|
| Channels | 59,955 |
| Videos | 1,316,583 |
| Eligible visible channels | 58,833 |
| Excluded channels | 803 |
| Niche workspaces | 11 |
| Niche memberships | 417 |
| Channels missing a topic | 0 |
| Channels missing creation date | 14 |
| Channels reporting zero subscribers | 1,754 |
| Combined eligible faceless channels | 39,296 |
| Text-model predictions | 59,943 |
| CPU face checks | 50,712 |
| VLM checks | 26,075 |
| Categorized videos | 385,929 |
| Videos still missing category | 930,654 |
| Channels added in the prior 24 hours | 5,780 |
| Radar watchlist queries | 412 |
| Radar interval after a cycle | 10 minutes |

Exclusion reasons:

| Reason | Count |
|---|---:|
| language | 406 |
| kids | 162 |
| music | 132 |
| brand | 83 |
| meme | 19 |
| restored_after_test | 1 |

The radar was healthy and continuing through harvest, enrichment, cataloging,
language filtering, category fetching, junk filtering, and face checking.

On Windows, the venv `pythonw.exe` launcher and the base Python child appear as
two processes but represent one logical radar worker.

---

## 10. Verified service behavior

The active Finder service:

- accepted the full token;
- accepted the read token;
- exposed 46 total engine tools;
- exposed 25 read tools to the read token;
- hid 21 write tools from the read token; and
- returned HTTP 403 when the read token attempted a write endpoint.

Measured calls against the live index:

| Call | Approximate time |
|---|---:|
| exact filtered count | 0.22 s |
| top five filtered channels | 0.73 s |
| topic list | 1.08 s |
| database stats | 1.50 s |
| five recent channels | 1.64 s |

The service boundary and read/write split are working.

---

## 11. Verification results

### Source validation

- All seven Beacon Python files parsed successfully with Python AST.
- Every Beacon source file was inspected.
- The relevant shared engine implementations were traced.

### Database validation

- `check_engine.py` reached the shared database successfully.
- SQLite journal mode is `WAL`.
- Schema/user version is `8`.
- No database writes were performed for the investigation.

### Automated tests

Current shared test suite:

- **113 passed**
- **0 failed**

Current Beacon suite:

- **25 passed**
- **13 parameterized subtests passed**

The stale `_MAX_LIST` assertion was corrected to test the configured cap.

The run also warned that Python 3.10.11 support in `google.api_core` ends on
**2026-10-04**. The runtime should move to Python 3.11+ before that date.

### Live desktop validation

The existing real Beacon window was focused and the following screens were
opened and visually inspected:

- Overview
- Channels
- Real Time
- Find
- Agent

No scraper, database, or agent write action was triggered during UI inspection.

---

## 12. What is working well

1. **Separation of concerns**
   - Beacon is a client/control layer.
   - Shared engine logic remains centralized.

2. **Single shared index**
   - No dangerous data duplication.
   - WAL supports concurrent reader/writer processes.

3. **Finder authorization**
   - Full and read-only tokens are enforced at the service.

4. **Combined filters**
   - Channel filters compose in one SQL query rather than inaccurate client-side
     post-filtering.

5. **Progressive paging**
   - The UI does not attempt to render tens of thousands of cards at once.

6. **Cache invalidation generations**
   - In-flight results can be rejected when a key is invalidated.

7. **Reversible exclusions**
   - Channels are hidden through `channel_exclusions`, not deleted.

8. **Autopilot tool allowlist**
   - Restricted mode is enforced both in the exposed tool list and dispatch.

9. **Radar resilience**
   - Provider failures are isolated.
   - The process continues through individual pipeline failures.
   - A process-level lock protects the radar command.

10. **Discovery is genuinely active**
    - The live radar added hundreds of channels during this investigation.

---

## 13. Important findings and risks

### Resolved 2026-08-17: Git baselines exist

Both `D:\Youtube\Beacon` and `D:\Youtube\Quill` are Git worktrees with existing
commit history. Continue to keep secrets and generated state outside version
control, especially `.env`, `storage/`, databases and WAL files, model
artifacts, logs, sessions, screenshots, and `__pycache__`. The shared live
database must never be added to Git.

### Resolved 2026-07-11: global busy state

`run_bg()` can run multiple loads concurrently, but `_busy` is a single boolean.
The first request to finish turns the spinner off even if others are still
running.

`_set_busy(False, "")` also does not clear the previous status text because it
only assigns `status.value` when the new message is non-empty.

Verified result:

`Loading channels...` remained visible in the bottom bar while every inspected
screen was already loaded.

Required repair:

- use a background-operation counter or task registry;
- clear the status message when the final operation completes; and
- avoid rendering/updating the page concurrently without a controlled UI
  scheduling path.

Resolution:

- background operations now receive unique task tokens;
- the spinner remains active until the final concurrent task ends; and
- the status line is explicitly cleared.

### Resolved 2026-07-11: literal `All topics` filter

The empty dropdown option uses `key=""` and display text `All topics`. Live
state contained the literal display text as `rt_topic`, producing:

- a topic filter that matches no database topic;
- a zero-result feed;
- an empty-state message claiming `All topics` is active.

Required repair:

- use a non-empty sentinel such as `__all__`;
- convert the sentinel to `None` in one place;
- apply the same fix to Channels and Real Time; and
- add a UI-level regression test.

Resolution:

- Channels and Real Time use the non-empty `__all_topics__` key;
- topic state is normalized again at the query boundary; and
- real UI tests proved selecting History and returning to All topics restores
  the full feed on both screens.

### Resolved 2026-07-11: unbounded UI/Finder caches

The cache has no TTL.

Examples:

- Overview counts remain stale until manual Refresh.
- Topic counts are never invalidated by the Refresh handlers.
- Agent or Autopilot writes do not invalidate affected cached lists.
- Engine read caches are not invalidated after write tools.

Required repair:

- assign TTLs by data type;
- refresh Overview automatically at a modest interval;
- invalidate `topics`, `niches`, and active result keys after related writes;
- invalidate engine read caches after workspace mutations; and
- make “Live” labels reflect actual refresh behavior.

Resolution:

- Beacon cache entries now have per-data TTLs;
- stale entries remain visible while refreshing;
- Overview refreshes silently every 60 seconds and shows its update time;
- manual Refresh invalidates topic counts; and
- successful Agent/shared-Finder writes invalidate cached derived reads.

### Partially resolved 2026-07-11: noisy opportunity score

The top outlier results included examples such as:

- 1 subscriber and a few hundred average views;
- very old channels with one historical viral upload;
- likely commercial/product accounts;
- malformed titles;
- music/celebrity channels; and
- channels with enormous ratios caused by tiny subscriber denominators.

Examples from the live top results included scores from roughly 700× to 3,766×.

The score is mathematically valid but not a reliable business opportunity score.

Required repair:

- add data-quality/confidence flags;
- use minimum evidence thresholds;
- separate “raw ratio” from “replicable opportunity”;
- prefer recent uploads and recent velocity over lifetime totals;
- cap or log-transform unstable ratios;
- penalize single-video and stale-channel effects;
- explicitly suppress music, brands, compilations, and malformed records; and
- evaluate the ranking against a human-labeled sample.

Implemented first pass:

- mandatory faceless + replicable eligibility;
- 8–500 uploads, minimum average views, maximum age, and positive subscribers;
- stabilized subscriber denominator;
- evidence and age factors; and
- `opportunity_score` as Beacon’s default ranking.

Still required:

- recent-performance/consistency signals and a human-reviewed benchmark.

### High: topic quality is not good enough for a primary navigation filter

The classifier is an ordered keyword matcher with broad substrings. It provides
100% coverage but not reliable semantic accuracy.

Risks:

- earlier broad rules steal matches from more appropriate later rules;
- substrings such as `compan`, `sport`, or generic description language can
  cause false matches;
- famous channels have visibly incorrect topics;
- manual overrides have no “locked” provenance;
- a full reclassification overwrites manual corrections; and
- arbitrary labels can be written through `set_channel_topic`.

Required repair:

- store topic source, classifier version, confidence, and manual-lock state;
- protect manual overrides;
- create a labeled evaluation set;
- add an explicit “music/artist/celebrity” exclusion or classification path;
- improve rules or use a trained classifier; and
- show an `Uncertain`/review queue instead of forcing every weak match.

### Resolved 2026-07-11: unbounded Autopilot query growth

Autopilot is currently enabled every 30 minutes.

Its activity log shows the watchlist growing from about 396 to 412 queries in a
few hours. It added one to three queries on most passes.

Problems:

- `get_watchlist` gives the model only a count and the first 25 items;
- exact deduplication cannot catch near-duplicates;
- there is no maximum watchlist size;
- there is no per-topic budget;
- adding queries every pass dilutes how often existing queries are revisited;
- expansion adds more modifiers to already long phrases; and
- unattended Bedrock calls incur cost.

Required repair:

- make query addition opt-in or much rarer;
- expose a normalized summary of the full watchlist;
- add similarity-based deduplication;
- enforce a size cap and per-topic quota;
- use measured harvest yield rather than “thin topic” alone;
- add a cooldown for each topic/query family; and
- log exact configuration changes deterministically.

Resolution:

- full measured watchlist/yield summary replaces the arbitrary first 25;
- autonomous additions are blocked while the existing expanded backlog is
  insufficiently explored;
- maximum 600 base queries;
- maximum two autonomous additions per call;
- 12-hour cooldown;
- normalized family and 0.80 token-Jaccard near-duplicate rejection; and
- exact accepted/rejected query results are persisted in Autopilot activity.

### Resolved 2026-07-12: research completion stalled at one or two searches

The old yield file counted only searches that returned videos. A valid provider
response with zero results therefore looked permanently untested, and the last
variation could wait for a full cursor rotation.

Resolution:

- `attempts` is recorded separately from productive `searches`;
- empty successful responses complete first-pass research without falsely
  increasing zero-yield retirement streaks;
- failed/unavailable provider calls remain untested and retryable;
- up to ten never-attempted variations are prioritized in each discovery slice;
- Overview shows exact remaining counts and never rounds an incomplete pass to
  100%; and
- after full coverage, the durable radar may perform the bounded,
  evidence-backed query top-up described above even while Beacon is closed.

### Resolved 2026-07-11: basic Agent/engine schema disagreement

Concrete mismatch:

- Beacon’s `add_channel_to_niche` schema permits `competitor` or `member`.
- The workspace engine accepts `competitor`, `inspiration`, or `owned`.
- `member` will fail at runtime.

Other schema gaps:

- agent sort choices omit `revenue`;
- agent count filters expose fewer fields than the engine supports;
- agent recent-channel filters expose only a subset of the live feed filters;
- documentation tool totals are stale.

Required repair:

- derive or test tool schemas against callable signatures;
- share enums from one source;
- add a schema-contract test; and
- update documentation from generated tool metadata.

Resolution:

- niche roles are `competitor`, `inspiration`, or `owned`;
- find/count/recent filter schemas match their Finder method signatures;
- Revenue sorting is available; and
- contract tests compare Beacon properties with real shared method signatures.

### High: agent write transparency is prompt-only

The system prompt asks the model to report changes, but code does not enforce a
deterministic audit summary.

Risks:

- a model can omit or misstate its final change report;
- the full agent chat is only in memory and disappears when Beacon closes;
- successful dispatch status can still contain an `{error: ...}` payload;
- exclusion/topic update counts return requested IDs, not necessarily rows that
  actually existed or changed; and
- reaching the 18-round cap can return an earlier partial sentence rather than a
  clear termination warning.

Required repair:

- record every write tool call and result in a durable audit log;
- compute the final “What I did” section in code;
- report actual database row changes;
- distinguish tool-level error payloads from success;
- persist full-agent actions; and
- produce an explicit max-round termination message.

### Partially resolved 2026-07-11: dependency reproducibility

Beacon has no dependency manifest of its own and relies on the Scripter venv.

The shared `requirements.txt` does not list:

- `scikit-learn`; or
- `joblib`.

Both are required by the faceless text model and happen to be installed locally.
A clean environment would not reproduce the working system.

Required repair:

- add the missing dependencies;
- document the shared-runtime arrangement;
- add a lock/constraints file;
- add a one-command environment check; and
- plan the Python 3.11+ upgrade.

Resolution:

- `scikit-learn>=1.7.2,<1.8` and `joblib>=1.5.3,<1.6` are declared;
- the complete requirements file passes a no-install dry run; and
- `pip check` reports no broken requirements.

Still required:

- a full lock/constraints strategy and Python 3.11+ migration.

### Resolved 2026-07-11: Beacon had no automated tests

Shared engine tests exist, but Beacon-specific behavior is largely verified by
manual screenshots.

Missing regression coverage includes:

- topic dropdown sentinel behavior;
- cache invalidation;
- background-operation accounting;
- Real Time empty states;
- paging reset rules;
- agent schema compatibility;
- Autopilot scheduling and rapid toggle behavior; and
- write audit behavior.

Current coverage includes loading concurrency, topic sentinels, mandatory
opportunity gating, cache TTL/invalidation, Autopilot environment safety,
bounded query policy, Agent schema contracts, error status, and round limits.
Durable write-audit coverage remains part of the next phase.

### Resolved 2026-07-11: stale shared assertion

The 200-row `_slim` expectation was not updated when production moved to 5,000.
The suite is therefore not fully green even though the intended production
change is understood.

The test now follows the configured `_MAX_LIST`; the shared suite is green.

### Medium: deep sweep is difficult to operate safely

The UI does not stream progress, disable duplicate starts, support cancellation,
or clearly explain that no workspace is auto-filled without a `niche_id`.

The HTTP request may time out while server-side work continues.

### Medium: Autopilot scheduling has a quick-toggle race

Turning Autopilot off and immediately back on can encounter a still-alive thread.
`start()` then returns without creating a replacement while the old thread exits
because its stop event was set.

### Medium: format definitions are inconsistent

Whole-index `channel_metrics` treats short-form channels as average duration
under 90 seconds. Workspace feeds treat individual videos up to 180 seconds as
short. Users can receive inconsistent results between screens/tools.

### Medium: current labels can be misleading

- `db_stats.excluded_kids` counts all exclusions, not just kids.
- `faceless_verified` counts VLM-faceless rows, not the full combined faceless
  population.
- Overview says “Live counts” despite indefinite caching.

### Low/cleanup

- `_rt_toggle_faceless` is unused.
- video-card and tracking parameters remain from older/possible future views.
- `json.load(open(...))` in service resolution does not use a context manager.
- Finder transport errors are not wrapped consistently in every client method.
- status JSON retains some stale fields between phases.

---

## 14. Recommended work plan

### Phase 0 — establish a safe baseline

Goal: make future changes reversible and measurable.

1. Initialize Git for Beacon.
2. Decide whether Scripter should be:
   - a second repository; or
   - one parent repository with Beacon included.
3. Add strict ignore rules before the first commit.
4. Record current environment/package versions.
5. Save a sanitized configuration snapshot containing keys and values that are
   safe to store, never credentials.
6. Make the current test result explicit: 107 pass, one stale assertion.

Exit condition:

- code changes can be diffed and rolled back;
- secrets/data are excluded; and
- the baseline commit is reproducible.

### Phase 1 — correctness, safety, and trust

Goal: make the existing product dependable before adding features.

1. Replace `_busy` with operation accounting and clear stale status text.
2. Fix the `All topics` sentinel bug on both browse screens.
3. Add cache TTLs and write-triggered invalidation.
4. Fix the niche-role enum mismatch.
5. Align all Beacon agent tool schemas with engine contracts.
6. Add deterministic write auditing and actual row-change counts.
7. Put a cap/cooldown/deduper around Autopilot watchlist changes.
8. Fix Autopilot rapid toggle behavior.
9. Add missing dependencies and environment checks.
10. Update the stale `_slim` test.
11. Add focused Beacon unit tests for these defects.
12. Verify every fix in the real Flet window with screenshots.

Exit condition:

- no false zero Real Time state;
- no permanent loading status;
- caches refresh predictably;
- agent writes are auditable;
- Autopilot cannot grow configuration without bounds; and
- tests are green.

### Phase 1B — off-laptop edge deployment

Goal: keep lightweight discovery running while the laptop is off without
turning Oracle into a second Beacon installation.

Completed foundation:

1. Signed/versioned discovery plans and result bundles.
2. Bounded free-search plus official-enrichment collector.
3. Durable query cursor, retry, retirement, and reprobe scheduler.
4. Atomic/idempotent local import plus signed acknowledgements.
5. Lightweight `requirements-edge.txt` without Flet, OpenCV, ML, or Bedrock.
6. Finder-gated cloud status and bounded manual inbox import in Beacon, with
   rejected bundles quarantined and accepted/duplicate bundles archived.
7. Immutable release builder, Python 3.11+ bootstrap, hardened unprivileged
   systemd service, health check, and rollback procedure for Oracle ARM.

Completed deployment:

1. Provisioned the Oracle Always Free ARM VM with SSH restricted to the
   operator's current public `/32`.
2. Installed the immutable unprivileged worker and a 4,164-query signed plan.
3. Verified a real shadow import: 100/100 searches succeeded, both bundles had
   valid signatures, imports were atomic, and duplicate replay was rejected.
4. Added SSH pull, signed acknowledgement, acknowledged-remote cleanup, and a
   hidden Windows scheduled-sync installer.

Remaining:

1. Monitor yield, quota use, disk growth, and provider reliability during the
   first sustained run.
2. Keep heavy organization, faceless scoring, ranking, Finder, and the canonical
   index on the laptop until a separately planned full service migration exists.

### Phase 2 — improve discovery signal quality

Goal: rank channels that are genuinely useful and replicable.

1. Create a human-reviewed benchmark of good/bad opportunity channels.
2. Add data-quality checks for subscriber/view anomalies.
3. Create Opportunity Score v2 using:
   - recent uploads;
   - recent view velocity;
   - median views rather than lifetime mean where possible;
   - minimum evidence;
   - upload consistency;
   - age;
   - faceless confidence;
   - topic confidence; and
   - exclusion penalties.
4. Separate:
   - raw viral ratio;
   - recent momentum;
   - proven repeatability; and
   - ease of replication.
5. Improve music/brand/celebrity suppression.
6. Continue the video-category backlog safely.
7. Add topic confidence, provenance, and manual locks.
8. Re-evaluate the default preset thresholds.

Exit condition:

- top results pass a human relevance review;
- obvious music/brand/noise channels are rare;
- manual topic corrections survive reclassification; and
- score meaning is understandable in plain language.

### Phase 3 — improve operator experience

Goal: make the app comfortable for daily non-technical use.

Completed:

1. Place the chat input above the Autopilot activity area.
2. Render Agent and Autopilot Markdown properly.
3. Redesign all five screens around one light, evidence-first control-room
   system with minimum-width responsive behavior.
4. Add opportunity-card qualification factors and plain-language rationale.
5. Make Autopilot history compact, readable, and expandable.

Remaining:

1. Add deep-sweep progress, duplicate-start protection, and cancellation.
2. Show data freshness timestamps.
3. Add exact match counts separately from displayed-page counts.
4. Add saved filter views.
5. Add a channel detail drawer with top videos and evidence.
6. Add a dedicated visible audit/history page.

### Phase 4 — expand product capability

Only after the earlier phases:

- richer workspace operations;
- per-niche video and outlier modes;
- language filters;
- top-video strips on cards;
- run/history browsers;
- review queues for topic/faceless uncertainty; and
- better report/export workflows.

---

## 15. Working rules

1. Treat Beacon and Production Lab as separate products.
2. Treat the shared engine and database as shared infrastructure.
3. Flag every shared-engine edit before making it.
4. Import `engine_link` before any `scripter.*` import in Beacon entry points.
5. Keep normal UI data access behind `FinderClient`.
6. Never expose `.env` values or tokens in logs, screenshots, docs, or commits.
7. Never copy or relocate the live SQLite index as a casual development step.
8. Do not stop the live radar or alter the live watchlist unless the task
   explicitly requires it.
9. Prefer reversible changes and deterministic audit logs.
10. Run automated checks and real-window click/screenshot verification for every
    user-visible change.
11. Treat verification as a blocking gate after every atomic code change:
    launch an isolated real Beacon instance, test the changed workflow, smoke
    all five screens, inspect screenshots and process errors, and fix any
    regression before beginning the next change. Do not batch unverified code
    changes.
12. Verification instances must set `SIGNAL_DISABLE_AUTOPILOT=1` so a second
    real window cannot start Bedrock work or mutate shared state.

---

## 16. Confirmed product decisions

Confirmed by the user on **2026-07-10**:

1. Beacon should surface **faceless and replicable formats only**. It is an
   opportunity finder, not a general browser of every successful YouTube
   channel.
2. Autopilot may add scraper queries autonomously, but the behavior must be
   genuinely smart:
   - driven by measured discovery yield and useful topic gaps;
   - normalized and similarity-deduplicated;
   - bounded by size, cooldown, and per-topic budgets; and
   - prevented from adding near-duplicate filler every maintenance pass.
3. Beacon work may modify the shared Scripter engine and database logic when
   needed. Those changes must improve the shared scraper/index and be verified
   for Production Lab compatibility.
4. “Replicable” means a repeatable faceless content format with enough uploads
   and performance evidence. Music artists, celebrities, children’s channels,
   major brands/networks, one-off viral accidents, malformed data, and unstable
   tiny-subscriber ratios should not dominate Beacon’s opportunity views.
5. Implementation order remains:
   - reliability and safety;
   - opportunity/ranking quality;
   - operator experience; and
   - larger features.
6. On **2026-07-11**, the user reported direct authorization from Google to
   create and use multiple YouTube API keys across separate accounts/projects.
   Treat that as project-specific user-provided authorization within the exact
   scope Google gave them. Do not document, print, or commit any key. Before
   implementing pooled quota behavior, preserve the written authorization/scope
   outside source control and make the implementation match it.
7. The cloud/off-laptop collector must use the signed inbox architecture and
   must never copy, mount, or remotely share the live SQLite file. Its foundation
   and Beacon-side manual sync controls now exist; transport, provisioning, and
   shadow validation remain pending.

Initial conservative eligibility rule for implementation and evaluation:

- faceless verdict required;
- 8–500 uploads;
- at least 3,000 average views;
- YouTube channel age no more than three years;
- positive subscriber count;
- exclusions still apply; and
- opportunity ranking uses a stabilized subscriber denominator and evidence
  factor so extremely tiny denominators cannot dominate merely by arithmetic.

These thresholds are a safe starting point, not a permanent truth. They must be
tested against real channels and refined with a human-reviewed benchmark.
