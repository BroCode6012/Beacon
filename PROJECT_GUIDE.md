# Signal — Codebase Investigation and Working Plan

Investigation date: **2026-07-10**  
Implementation status updated: **2026-07-12**

This document is the durable map of Signal: what it is, why it exists, how it
works, what is currently running, what was verified, where the important risks
are, and the order in which the project should be improved.

The numbers in the “live state” section are a snapshot. The radar is actively
adding data, so counts will continue to rise.

---

## 1. Executive summary

Signal is a Windows desktop control room for finding YouTube channel and niche
opportunities. Its intended job is to help a non-technical operator:

- discover channels continuously;
- browse and combine channel filters;
- find unusually strong channels and videos;
- organize channels into niche workspaces;
- run targeted deep sweeps;
- monitor the background discovery pipeline; and
- ask an AI agent to search, organize, clean, and control the index.

Signal is not the scraper engine and it does not own a separate database. It is
a small Flet UI and orchestration layer in `D:\Youtube\Signal`. The heavy logic
lives in the shared Scripter engine in `D:\Youtube\Scripter`, and both Signal and
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
- off-laptop discovery still needs a transport adapter and Oracle provisioning,
  but its safe inbox architecture, worker, and Signal-side sync controls now
  exist.

The next work should remain a quality/auditing pass, not a large new feature.

### Verified implementation status — 2026-07-12

The first correctness pass is now implemented and verified:

| Area | Current verified state |
|---|---|
| Opportunity scope | Signal always requests faceless + replicable channels |
| Shared ranking | Optional `replicable_only` gate and stabilized `opportunity_score` |
| Loading state | Concurrent background tasks are counted; stale status text clears |
| Topic reset | Non-empty `__all_topics__` sentinel; legacy `All topics` is normalized |
| Cache freshness | TTLs, stale-while-refresh, 60-second Overview refresh, topic invalidation |
| Shared Finder cache | Successful writes clear cached derived reads |
| Autopilot queries | 450 max, 2 autonomous additions/call, 12-hour cooldown, family/near-duplicate rejection, yield/backlog gate |
| Research tracking | Successful empty provider responses count as tested; provider failures do not; incomplete passes never display as 100% |
| Always-on query top-up | Radar may add at most 2 reviewed, evidence-backed, cross-topic queries only after 100% first-pass coverage; disabled by default outside Signal |
| Agent contracts | Workspace roles aligned; Finder filters/sorts aligned; error payloads report error status |
| Radar throughput | Independent fast-discovery and organizer lanes; 4 parallel search fetchers; 1-minute discovery rest |
| Durable radar work | Coalescing lease-based queue beside the shared index; interrupted work safely returns to the queue |
| Off-laptop foundation | Signed recurring plans, lightweight Oracle-ready collector, durable scheduler, signed result bundles/acks, idempotent local import, Finder-gated status/manual sync, hardened immutable Oracle deployment package |
| Operator tracking | Overview distinguishes channels waiting from channels actively being organized |
| Dependencies | `scikit-learn` and `joblib` declared in shared requirements |
| Git baseline | Signal and Scripter now have separate safe repositories with secrets/data/generated state ignored |
| Signal tests | **41 passed, 13 parameterized subtests passed** |
| Shared tests | **178 passed** |
| Package health | `pip check`: no broken requirements |

Every atomic change above also passed a separate real Flet launch and visual
smoke test of Overview, Channels, Real Time, Find, and Agent. Verification
instances used isolated Finder ports and disabled Autopilot. Logs contained no
exceptions; the only stderr output was the known Python 3.10 support warning.

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
  Signal's live config instead of every cycle.

The split-pipeline pass was implemented and live-verified later on 2026-07-11:

- discovery no longer waits for enrichment, cataloging, filters, niche crawling,
  scoring, or workspace maintenance;
- Signal's live config uses `split_pipeline_enabled: true` and
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
- Signal Overview shows the two lanes plus exact waiting and actively-organizing
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
- the radar and Signal Autopilot share the same cross-process write lock,
  maximum size, duplicate-family rules, and cooldown record;
- the radar selects only from a reviewed faceless/replicable subject catalog
  and requires evidence across several already-indexed faceless channels;
- each run may add at most two queries from different topics, only after 100%
  first-pass coverage; and
- Production Lab/legacy configurations remain unchanged because the feature is
  disabled unless explicitly enabled.

Current live query-policy snapshot after rollout:

- 424 base queries;
- hard ceiling 450;
- 4,164 expanded searches;
- 4,164 searched at least once (100%);
- 0 expanded searches untested; and
- no radar query added during rollout because the existing Autopilot addition
  correctly kept the shared 12-hour cooldown active.

The off-laptop edge foundation was implemented and verified later on
2026-07-12:

- the canonical SQLite index stays physically in Signal storage and is never
  copied, mounted, or opened by Oracle;
- Signal can snapshot the current radar configuration into a signed,
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
- Signal accesses that service only through Finder: status is read-authorized,
  importing requires the full write token, successful imports invalidate local
  caches, and the Overview card explains that Oracle never owns the main
  database;
- the Oracle deployment package builds an allowlisted archive from one committed
  Git revision, refuses to include local/generated state, installs a
  self-contained Python 3.11+ release, and atomically switches versions without
  touching persistent worker state;
- its systemd service runs as an unprivileged `signal-edge` account, exposes no
  application port, grants write access only to `/var/lib/signal-edge`, and
  supports explicit health checks and symlink rollback;
- a real isolated network smoke collected one query into 3 videos/3 channels
  at zero API quota and imported it into a temporary index; and
- the full regression gate passed with 178 shared tests, 41 Signal tests plus
  13 subtests, all five Signal screens, and an isolated Production Lab window.

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
6. add the transport adapter, provision Oracle, and run a shadow pilot before
   enabling continuous off-laptop collection; and
7. upgrade the shared runtime to Python 3.11+ before **2026-10-04**.

---

## 2. System boundary

There are three code/data ownership zones.

### A. Signal-owned code

Location: `D:\Youtube\Signal`

Signal owns:

- the Flet desktop window and five screens;
- Finder service discovery/self-hosting;
- UI state, caching, cards, filters, and paging;
- start/stop controls for the radar worker;
- the Signal-specific Bedrock agent;
- the optional Autopilot loop; and
- the Windows screenshot/click verification helper.

Changes here affect Signal only.

### B. Shared Scripter engine

Location: `D:\Youtube\Scripter\scripter`

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

Changes here can affect both Signal and the Production Lab. Every engine change
must be labeled as shared-impact work.

### C. Shared live data

Primary database:

`D:\Youtube\Signal\storage\youtube_index.sqlite3`

Supporting operational state remains under `D:\Youtube\Scripter\storage` and
includes:

- `scraper_watchlist.json`
- `radar_status.json`
- `radar_worker.pid`
- `radar.log`
- `harvest_yield.json`
- `radar_work_queue.sqlite3`
- `faceless_text_model.joblib`

The database is approximately 10 GB and runs in WAL mode. It must never be moved
or copied casually. Signal owns the physical file. Production Lab resolves that
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
Signal Flet app (signal_app.py)
  |                         \
  | FinderClient             \ SignalAgent / Autopilot
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

Signal Overview
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

Signal/controller
  |
  +--> signed discovery plan
          |
          v
      transport (SSH/SFTP or OCI Object Storage; deployment pending)
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

Normal Signal UI features should go through `FinderClient`. They should not
embed direct SQLite queries or import internal scraper services. Direct database
access is acceptable for diagnostics such as `check_engine.py`, not for normal
screen behavior.

---

## 4. Startup and runtime flow

1. `signal_app.py` imports `engine_link` first.
2. `engine_link.py`:
   - places `D:\Youtube\Scripter` on `sys.path`; and
   - sets `YOUTUBE_INDEX_DB` unless the environment already overrides it.
3. Signal looks for a shared Finder token file, then tries the configured Finder
   URL.
4. If no Finder is reachable, Signal creates the Finder server in a daemon
   thread inside its own process.
5. Signal creates the optional Autopilot controller and starts it only when the
   saved state says it is enabled.
6. The Flet shell renders the current section.
7. Each data screen lazily starts background Finder calls and caches their
   results in memory.
8. The radar worker is independent. It keeps running after Signal closes.

The current running Signal instance is self-hosting its Finder on port `8799`.
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
- successful writes invalidate the relevant Signal and Finder caches;
- the research tracker shows exact tested/remaining counts; and
- near-complete progress uses enough precision that it cannot display 100%
  while searches remain.

### Channels

Purpose:

- browse the whole eligible channel index;
- filter by curated niche workspace;
- filter by clean topic;
- combine subscriber, average-view, age, outlier, category, and format filters;
- sort results; and
- page deeper in batches of 120.

The filters are combined in one engine SQL query. This is the right design.

The screen currently exposes raw data-quality problems very clearly. In a live
high-average-views view, examples included:

- Nirvana labeled `Other`;
- Chris Brown labeled `Tech & AI`;
- Ricky Martin labeled `Tech & AI`;
- MrBeast labeled `Nature & Wildlife`;
- Like Nastya labeled `Space & Physics`; and
- Akon labeled `Business & Companies`.

This is not primarily a card-rendering problem. It is classifier and corpus
quality leaking into the UI.

### Real Time

Purpose:

- show channels by index discovery time (`first_seen_at`);
- filter by 24 hours, 7 days, 30 days, or newest;
- combine topic and metric filters; and
- auto-refresh every 60 seconds.

Important distinction:

- `first_seen_at` means when this system found the channel;
- `channel_published_at` means when the YouTube channel was created.

Verified live defect:

The screen showed zero channels and explained that the active topic was
`All topics`, even though `All topics` should mean no topic filter. The direct
Finder call returned recent channels normally. The display label has leaked into
state as a literal topic value.

### Find

Purpose:

- launch a broad deep sweep over approximately 100 generated search angles; and
- load faceless channels from the index.

The current deep sweep:

- writes discoveries into the shared index;
- does not auto-track a workspace because Signal passes no `niche_id`;
- has no progress stream in the UI;
- has no cancellation control;
- uses the Finder client’s 600-second HTTP timeout; and
- can be started again while another background operation is already running.

### Agent

Purpose:

- expose a Bedrock Claude agent with Finder, scraper, and UI-control tools;
- let the agent search, count, organize, clean, and control the app; and
- manage Autopilot.

The agent is intentionally separate from Production Lab chat code.

The current Agent screen places the large Autopilot card before the chat input.
With activity present, the input is below the visible fold even though the code
comment says the input should be pinned first.

Autopilot summaries are rendered as plain text, so Markdown headings and tables
appear as raw `##` and `|` characters.

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

### `signal_app.py`

The 1,802-line desktop application:

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

### `signal_agent.py`

The Bedrock tool-use loop:

- 30 exposed tool specifications;
- engine tools;
- local scraper/watchlist tools;
- UI-control tools;
- up to 18 tool rounds; and
- optional restricted mode for Autopilot.

### `signal_autopilot.py`

Optional in-process maintenance:

- saved enable/interval state;
- a restricted tool set;
- a fixed maintenance prompt;
- an activity log in `_autopilot.json`; and
- a daemon scheduling thread.

### `sweep_control.py`

Signal-side radar control:

- read status;
- inspect the pidfile;
- launch the detached worker;
- write the stop flag; and
- read/write the watchlist.

### `engine_link.py`

The bridge to the shared engine and database.

### `finder_service.py`

A standalone Finder launcher. Signal currently also supports in-process
self-hosting.

### `check_engine.py`

Read-only database connectivity proof.

### `_win.ps1`

DPI-aware native UI verification:

- find and focus the Signal window;
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

## 8. Shared engine paths Signal relies on

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

- All seven Signal Python files parsed successfully with Python AST.
- Every Signal source file was inspected.
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

Current Signal suite:

- **25 passed**
- **13 parameterized subtests passed**

The stale `_MAX_LIST` assertion was corrected to test the configured cap.

The run also warned that Python 3.10.11 support in `google.api_core` ends on
**2026-10-04**. The runtime should move to Python 3.11+ before that date.

### Live desktop validation

The existing real Signal window was focused and the following screens were
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
   - Signal is a client/control layer.
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

### Foundation: there is no Git repository

Neither `D:\Youtube\Signal` nor `D:\Youtube\Scripter` is currently a Git
repository.

That means:

- no reliable diff;
- no safe rollback;
- no commit checkpoints;
- no branch for risky engine work; and
- no clear record of which chat changed what.

Before meaningful code work, initialize version control for code and
documentation while explicitly excluding:

- `.env`;
- `storage/`;
- databases and WAL files;
- model artifacts;
- logs;
- sessions;
- screenshots; and
- `__pycache__`.

The shared database must not be added to Git.

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

- Signal cache entries now have per-data TTLs;
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
- `opportunity_score` as Signal’s default ranking.

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
- maximum 450 base queries;
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
  evidence-backed query top-up described above even while Signal is closed.

### Resolved 2026-07-11: basic Agent/engine schema disagreement

Concrete mismatch:

- Signal’s `add_channel_to_niche` schema permits `competitor` or `member`.
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
- contract tests compare Signal properties with real shared method signatures.

### High: agent write transparency is prompt-only

The system prompt asks the model to report changes, but code does not enforce a
deterministic audit summary.

Risks:

- a model can omit or misstate its final change report;
- the full agent chat is only in memory and disappears when Signal closes;
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

Signal has no dependency manifest of its own and relies on the Scripter venv.

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

### Resolved 2026-07-11: Signal had no automated tests

Shared engine tests exist, but Signal-specific behavior is largely verified by
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

1. Initialize Git for Signal.
2. Decide whether Scripter should be:
   - a second repository; or
   - one parent repository with Signal included.
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
5. Align all Signal agent tool schemas with engine contracts.
6. Add deterministic write auditing and actual row-change counts.
7. Put a cap/cooldown/deduper around Autopilot watchlist changes.
8. Fix Autopilot rapid toggle behavior.
9. Add missing dependencies and environment checks.
10. Update the stale `_slim` test.
11. Add focused Signal unit tests for these defects.
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
turning Oracle into a second Signal installation.

Completed foundation:

1. Signed/versioned discovery plans and result bundles.
2. Bounded free-search plus official-enrichment collector.
3. Durable query cursor, retry, retirement, and reprobe scheduler.
4. Atomic/idempotent local import plus signed acknowledgements.
5. Lightweight `requirements-edge.txt` without Flet, OpenCV, ML, or Bedrock.
6. Finder-gated cloud status and bounded manual inbox import in Signal, with
   rejected bundles quarantined and accepted/duplicate bundles archived.
7. Immutable release builder, Python 3.11+ bootstrap, hardened unprivileged
   systemd service, health check, and rollback procedure for Oracle ARM.

Remaining deployment work:

1. Add a transport adapter while keeping the protocol transport-independent.
2. Create the Oracle Always Free ARM VM and apply the prepared hardening package.
3. Run shadow mode and compare cloud/local results before enabling continuous
   import.
4. Decide whether to enable safe automatic inbox import after the shadow pilot;
   manual import remains the verified default.
5. Keep heavy organization, faceless scoring, ranking, Finder, and the canonical
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

1. Place the chat input above the Autopilot activity area.
2. Render agent/Autopilot Markdown properly.
3. Add deep-sweep progress, duplicate-start protection, and cancellation.
4. Show data freshness timestamps.
5. Add exact match counts separately from displayed-page counts.
6. Add saved filter views.
7. Add a channel detail drawer with top videos and evidence.
8. Add a visible audit/history page.

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

1. Treat Signal and Production Lab as separate products.
2. Treat the shared engine and database as shared infrastructure.
3. Flag every shared-engine edit before making it.
4. Import `engine_link` before any `scripter.*` import in Signal entry points.
5. Keep normal UI data access behind `FinderClient`.
6. Never expose `.env` values or tokens in logs, screenshots, docs, or commits.
7. Never copy or relocate the live SQLite index as a casual development step.
8. Do not stop the live radar or alter the live watchlist unless the task
   explicitly requires it.
9. Prefer reversible changes and deterministic audit logs.
10. Run automated checks and real-window click/screenshot verification for every
    user-visible change.
11. Treat verification as a blocking gate after every atomic code change:
    launch an isolated real Signal instance, test the changed workflow, smoke
    all five screens, inspect screenshots and process errors, and fix any
    regression before beginning the next change. Do not batch unverified code
    changes.
12. Verification instances must set `SIGNAL_DISABLE_AUTOPILOT=1` so a second
    real window cannot start Bedrock work or mutate shared state.

---

## 16. Confirmed product decisions

Confirmed by the user on **2026-07-10**:

1. Signal should surface **faceless and replicable formats only**. It is an
   opportunity finder, not a general browser of every successful YouTube
   channel.
2. Autopilot may add scraper queries autonomously, but the behavior must be
   genuinely smart:
   - driven by measured discovery yield and useful topic gaps;
   - normalized and similarity-deduplicated;
   - bounded by size, cooldown, and per-topic budgets; and
   - prevented from adding near-duplicate filler every maintenance pass.
3. Signal work may modify the shared Scripter engine and database logic when
   needed. Those changes must improve the shared scraper/index and be verified
   for Production Lab compatibility.
4. “Replicable” means a repeatable faceless content format with enough uploads
   and performance evidence. Music artists, celebrities, children’s channels,
   major brands/networks, one-off viral accidents, malformed data, and unstable
   tiny-subscriber ratios should not dominate Signal’s opportunity views.
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
   and Signal-side manual sync controls now exist; transport, provisioning, and
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
