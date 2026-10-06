# Beacon — Niche Finder (standalone app)

> **Current architecture — October 6, 2026:** Beacon is self-contained. Its discovery,
> Finder, scraper and PostgreSQL code live in `beacon_engine/` in this repository.
> Use Beacon's own `.venv`; never import `scripter`, read a sibling app's `.env`,
> or use Quill's code, storage or environment. Quill owns its separate code/data.
> The older shared-engine descriptions below are historical and superseded.
> The existing Oracle release is unchanged until a separately verified deployment.
> See `README.md` and `APP_SEPARATION.md` for setup and rollout boundaries.

You are working on **Beacon**, a standalone YouTube niche investigator & finder. It was split out
of a bigger app (the "Production Lab") so it can be worked on alone. A SEPARATE chat handles the
Production Lab — you focus on Beacon. Read this before editing anything.

The user is **non-technical**: explain in plain words (numbers/analogies) before code, and VERIFY UI
changes live (see **Verify every task** below) — never just claim it works.

## Verify every task (MANDATORY — the user insists)
For EVERY task, before saying it works: **launch the real app, click through the actual UI, and take
screenshots.** Headless checks, stubbed handlers, and logic-only tests are NOT enough on their own —
they can pass while the live UI is broken. The loop is:
1. Launch: `D:\Youtube\Beacon\.venv\Scripts\python.exe D:\Youtube\Beacon\beacon_app.py` (background),
   wait ~30s for boot.
2. Bring the window to front (Windows blocks focus-steal — attach-thread-input trick), then **click the
   real controls** at coordinates computed from the window rect (nav tiles, niche chips, cards, stars),
   scroll if needed.
3. **Screenshot after each meaningful step** and READ the screenshot to confirm it actually worked.
4. Only then report — show what the screenshot proved. If a click opens a browser/external side effect,
   that's fine; do it and confirm.
Headless/DB probes are still useful for *data* facts, but they never replace the live click-through.
FAST PATH (2026-09-16): no code change needed to drive Beacon in the in-app browser pane. Launch an
isolated copy with `FLET_FORCE_WEB_SERVER=1 FLET_SERVER_PORT=8570 FLET_SERVER_IP=127.0.0.1
FINDER_PORT=8799 FINDER_TOKEN_FILE=<nonexistent.json> SIGNAL_DISABLE_AUTOPILOT=1` and open
http://127.0.0.1:8570 — it serves the real app without opening Chrome, and several click+screenshot
steps batch into one call. Gotchas: Flet draws on a canvas (no DOM text — `find`/`read_page` see
nothing, click by coordinates); after resizing the viewport RELOAD the page or it renders a cropped,
mis-scaled view; 1100x690 stays readable; widths under Beacon's 1040px minimum squash the layout.
Opening Saved in a test copy runs a real live refresh (writes a snapshot), and picking a provider
writes `_ai_choice.json`, which the real app reads — delete it after testing.

## What Beacon is
A Flet desktop app (its own window titled "Beacon — Niche Finder") that DRIVES the finder tool to
discover channels, sweep niches, score outliers, and manage niche workspaces. Beacon is the finder's
"control room" — it uses the finder with FULL (read + write) access.

## Architecture — one application

1. Beacon UI calls its authenticated Finder through `FinderClient`.
2. `beacon_engine/` owns discovery, research, intelligence, worker and storage code.
3. Beacon owns its database and worker state. Production uses PostgreSQL; a clean
   development checkout can use its own small local SQLite index.

Import `engine_link` before `beacon_engine.*` from a Beacon entry point. It loads
only Beacon settings. Use `BEACON_ENGINE_STATE_DIR` for worker configuration/state;
otherwise that state stays in this checkout's `storage/`. Do not use sibling folders.
Quill is an independent application. No automatic connection is configured.
Future integration must be explicit, authenticated and opt-in through an API.

## Files here
- `beacon_app.py` — the app / entry point. Screens: **Overview**, **Channels**, **Real Time**, **Find**,
  **Saved**, **Agent** (six). (The old
  separate **Niches** tab was removed — niches are now a **dropdown filter on the Channels tab**. Per-card
  ★ tracking was also removed — the Channels tab is a pure browser/filter, not a curation tool.) The
  Channels browser is NexLev-style and all filters **COMBINE** in one `find_channels` query. The main
  page is kept clean (progressive disclosure): a toolbar with the niche dropdown + a **Topic dropdown**
  (clean topic niche filter — History/Space/True Crime/…, every channel has one, counts shown; see
  ENGINE NOTE 8; state = `self.ch_topic`, injected in `_ch_query_args`, survives preset chips like the
  niche does) + Sort-by dropdown +
  a **Filters button** (shows an active-count badge, e.g. "Filters (2)") + preset chips + removable
  active-filter chips, then the results grid. All the metric filters live in a **Filters modal**
  (`_open_filters` → `ft.AlertDialog`) with grouped sections (Channel type / Audience & reach /
  Performance & age), **segmented toggles** (`ft.SegmentedButton`: Any/Faceless-only, All/Long/Short),
  number fields (min/max subs, min/max avg views, max age, min-outlier, category), and Apply/Reset.
  Picking a niche filters to that niche's channels (via `find_channels(niche_id=…)`) with a
  "Whole index / This niche's channels" toggle. **Channel cards** are dashboard-style: avatar + name +
  faceless pill header, a 2×2 grid of **stat tiles** (Avg views / Subscribers / Uploads / Age via
  `_stat_tile`), and an **outlier-score bar meter** (`_outlier_meter`: segmented fill, colour bands
  muted<1× / blue 1–2× / green ≥2×, exact ×number). Cards are clickable → open on YouTube. Own shell
  (nav, run_bg, status bar, in-memory cache, card builders, palette). Drives the finder via `FinderClient`.
  **Progressive paging (no fixed cap):** all three grids (Channels, Real Time, Find) start at `CH_PAGE`
  (120) cards and a **"Show more (+120)"** button pages deeper — the shown count is baked into the query
  limit + cache key; `_ch_base_key` resets paging when the filter/sort/niche changes. Headers read
  "N+ … showing the top — Show more below" vs "all matches loaded". ENGINE NOTE 3 (shared): the finder's
  `_slim` result-trimmer capped every API list at 200 rows — raised to `_MAX_LIST = 5000`
  (`analyst_tools.py`) so paging UIs can browse deep (blobs are still stripped, so it's just a high
  backstop). Past a few thousand cards the real limiter is Flet rendering, not a cap.
  RESPONSIVENESS (2026-09-16) — why the app no longer "always loads". Measured with a runtime
  stopwatch (renders / finder calls / busy bar) on the same walk-through, before → after:
  loading bar visible Overview 100% → 25%, Channels 99% → 7%; Overview counts `db_stats` 143.8s →
  13.1s; `list_topics` 23.4s → 3.8s; `count_channels` 23.3s → 4.0s; Channels page 10,163 → 3,443
  elements, slowest redraw 1,484 → 484 ms. Five rules, keep them:
  (1) The finder runs in its OWN process. `_resolve_service` reuses a running one, else launches
  `finder_service.py` with the venv's `pythonw.exe` (`sys.prefix/Scripts` — the base interpreter
  lacks the engine's packages) and waits up to `FINDER_START_TIMEOUT`; in-process `serve_in_thread`
  is only the fallback. Hosting it inside the Flet process made UI redraws and SQL requests queue on
  one interpreter. `atexit` → `_stop_finder_process` kills the whole tree (the venv pythonw is a
  launcher stub), so it keeps the old in-process lifetime and a restart always runs current engine
  code. Verified: closing the window stopped the service in 3.1s. GOTCHA: a FORCE-killed Beacon
  leaves the service orphaned and the next launch reuses it — after engine edits, check no
  `finder_service.py` process survives before trusting a restart. Header pill: "Finder connected".
  (2) The busy bar belongs to a screen: `_busy_tasks[token] = (msg, screen)`; only screen=None (user
  actions) or the current section shows; `_go` re-applies it. `run_bg(..., quiet=True)` shows nothing.
  (3) `_ensure` refreshes stale data QUIETLY (old values stay visible) and its `done` redraws only if
  `_shows(key)` (the key was used by the current screen's last render, recorded in `_section_keys`
  by `_render`; `_shows` takes `_render_lock` so a fast load cannot miss a render in progress) AND
  the data actually changed. Before, every finished load rebuilt the whole current page.
  (4) `CH_PAGE = 40` (was 120; ~85 elements per channel card). (5) Longer cache life: channels 10
  min, topics/niches 30 min, niche signals 5 min, Real Time 2 min (its 60s ticker keeps it live),
  Overview auto-refresh 5 min (`SIGNAL_OVERVIEW_REFRESH_SECONDS`). `edge_sync` stays 3s on purpose
  (pinned by test_beacon_cloud for live import progress) — quiet + unchanged-skip make it cheap.
  Tests: test_beacon_responsiveness.py (13). Harness gotcha: an instrument wrapper around
  `_begin_busy` must accept the `screen` argument, or every render crashes inside the wrapper.
  The **Real Time** screen (`_render_realtime`) is a NexLev-style live feed of channels the scraper has
  most-recently DISCOVERED (sorted by `first_seen_at`, NOT the channel's YouTube age): a "● live" header,
  a window segmented toggle (Last 24h / 7 days / 30 days / Newest → `rt_window`), and the same dashboard
  cards with an extra green "added Xm ago" freshness badge (`_ago` relative-time formatter). It
  **auto-refreshes every 60s** via `_watch_realtime`/`_rt_tick` (a `_watch_sweep`-style daemon), which
  reloads silently and only re-renders when the newest channel actually changed (so scroll isn't reset).
  It calls the finder's `recent_channels` tool. It now ALSO has **filters** (like Channels): a **Topic
  dropdown** (`rt_topic`) + a **Filters button** that opens the SAME modal (`_open_filters(target="realtime")`
  → edits `rt_filters`) + removable active-filter chips — all COMBINE with the window. State: `rt_filters`/
  `rt_topic`; `_rt_query_args` builds the `recent_channels` call; `_rt_key` includes the filters so changing
  them re-queries. It also has the SAME **preset chips** as Channels (reuses `CH_CHIPS` via `_rt_pick_chip`,
  which drops the preset's `sort` key since RT always sorts by discovery time; `rt_chip` tracks the active
  one). Verified: "Low Subs & High Views" → subs≤50K + avg≥50K → surfaced a 107.7× outlier in the feed. The Filters modal + `_read_filter_fields`/`_apply_filters`/`_reset_filters`/
  `_active_filter_count` were made **target-aware** (`"channels"` vs `"realtime"`), and the Topic dropdown is
  a shared `_topic_dd(current, on_change)` helper. `recent_channels` (shared engine, additive) was widened
  with `topic`/`min_avg_views`/`max_avg_views`/`min_outlier_score`/`max_days_since_start`/`min_views_per_sub`/
  `video_format` (forwarded to `channel_metrics`; the Lab's callers are unaffected).
  TRANSIENT-EMPTY SELF-HEAL (`_ensure`): a browse view (a `channels:`/`realtime:` cache key) that loads with
  ZERO rows is often a momentary hiccup (finder busy while the scraper writes, or a just-shifted discovery
  window), which previously stuck as a misleading "0 channels". Now `_ensure` silently re-checks such a key
  ONCE ~4s later; if rows appeared it swaps them in + re-renders. One recheck per key (tracked in
  `_rechecked`, cleared on `_invalidate`) → no loop; genuine empties stay empty. Both browse loaders also
  raise on a non-list finder response (→ retryable error card, never a cached "0"). STRONGER GUARD:
  `_finder_rows(tool, args)` (used by BOTH `_load_channels` + `_load_realtime`) retries the finder call up to
  3× (~1.2s apart) on an EMPTY list before accepting it — under heavy scraper writes the in-process finder
  occasionally returns [] for a fraction of a second; retrying inline means a transient empty is caught
  before it can cache + freeze as a misleading "0". Verified (fresh instance): RT presets "Above 50k Avg
  Views" + "New Channels" (age≤90) both return rows; the recurring stuck-"0" was a stale long-open instance.
  ROOT CAUSE OF THE "0 channels" REPORTS (diagnosed 2026-07-10 via temp debug logging in `_load_realtime`
  that recorded the exact args + returned length): it was NOT a bug — the app's in-process finder returns
  EXACTLY what the DB has (debug log: 0=0, 120=120, 6=6; headless confirms). Stacking Real-Time filters
  COMBINES them (topic + window + subs + age + avg-views all AND together), so a narrow slice is genuinely
  empty — e.g. `recent_channels(topic="Business & Companies", days=1, min_subs=1000, max_subs=10000,
  max_days_since_start=90)` = 0 in both app AND DB, while the broader `days=30 + all topics` version = 254.
  The old empty-state message ("start the background scraper") wrongly implied breakage. FIX: `_render_realtime`
  now shows a SMART empty state — when filters/topic are active it says "No channels match your current
  filters (N metric filters) in this window. These filters COMBINE … Widen the window, or clear the filters."
  with a **"Clear all filters"** button (`_rt_clear_all`: wipes rt_filters + rt_topic, resets to the wide
  Last-30-days window). Only when NO filter is set does it fall back to the "wider window / start the scraper"
  hint. Verified live: Min-subs=1B → smart message + Clear button → click Clear → feed recovers to
  "Newest 120+ channels · last 30 days". (Harness note: row-2 preset chips + the window segmented toggle are
  flaky to click via `_win.ps1`; the Filters-modal fields + Apply + row-1 chips are reliable.)
  ENGINE NOTE (shared, additive): `find_channels`/`store.channel_metrics` were widened to accept a
  `niche_id` membership filter + all metric params + return `avatar_url`/`views_per_sub`; `channel_metrics`
  also gained an `added_at` sort key + `added_within_days` filter (both from `channels.first_seen_at`), and
  a new **read** tool `recent_channels` (write:False, so the Lab's read key sees it too) was added to
  `analyst_tools.py`. All new args/tools are optional/additive so the Lab's existing calls are unaffected.
  `first_seen_at` = when WE first indexed a channel (discovery); `channel_published_at` = its YouTube
  creation date (used for `days_since_start`/age) — different clocks, don't confuse them.
  ENGINE NOTE 4 (shared, faceless detection): the paid GPU VLM step is DISABLED (`vlm_remote_enabled:false`
  in scraper_watchlist.json) — it was the only $ cost + the long-cycle spikes; the free CPU face-check stays.
  A new FAST, vision-free faceless classifier was added: `scripter/research/faceless_text_model.py`
  (TF-IDF + LogisticRegression over channel title + **description** + top video titles, trained on the
  ~40k face-check/VLM labels, ~73% held-out; persisted to `storage/faceless_text_model.joblib`; writes a
  new `channel_text_model` table — schema bumped to v6). It's wired into the faceless verdict CASE in
  `store.channel_metrics` (+ the rising_videos + faceless-count copies) as the fallback: vlm > face-check >
  **text-model** > old keyword classifier. `radar.py` scores new channels with it each cycle (trains once
  if the model file is missing). Needs `scikit-learn` (installed in the shared venv). NOTE: some music/topic
  channels still show faceless — that's a PRE-EXISTING frozen VLM verdict, not the text model (which
  correctly calls them on-camera); a mega-brand/music/`- topic` blocklist hard-veto would clean it up.
  ENGINE NOTE 5 (shared, junk filtering + no-scrape gate): `scripter/research/junk_filter.py` auto-excludes
  obvious junk — music (by NAME `- Topic`/`VEVO` AND by YouTube category: `find_music_channels` =
  channels whose videos are predominantly YouTube category 10 Music — now reads the `videos.category_id`
  column populated by the categorize job, see ENGINE NOTE 6; ~113 caught so far, grows as more videos get a category),
  memes/compilations, big brands/networks (netflix/tedx/wwe/…) — via `channel_exclusions` (hidden from every view AND skipped by scraping;
  reversible with `store.unexclude_channels`). Runs as a radar cycle step (`junk_filter_enabled`). Separately,
  a NO-SCRAPE sub-count gate (`RadarConfig.min_subs_to_process`, default **700**) makes `channels_needing_catalog`
  + `channels_for_face_check` (+ `face_check.run_face_check*`) skip channels under that many subs — so the
  expensive catalog/face-check work isn't spent on tiny channels. IMPORTANT: it's a SCRAPE gate only —
  tiny channels are still enriched, text-labeled, and VISIBLE in the app, so low-sub OUTLIERS are NOT
  hidden (verified live: 18-sub channels still show). Both are shared-engine (affect the Lab), config-tunable.
  ENGINE NOTE 2 (shared radar discovery, additive): the background scraper's harvest was made
  smarter (`radar.py` `_harvest_slice` + `RadarConfig.harvest_adaptive`/`harvest_retire_after`/
  `harvest_reprobe_every`, `channel_discovery.harvest`, `crawler.crawl_search`/`_discover_search`,
  `scrapetube_provider.search`): it tracks each query's NEW-channel yield in
  `storage/harvest_yield.json`, auto-retires queries that stop yielding (re-probing periodically),
  and re-searches queries with history newest-first (scrapetube `sort_by=upload_date`) to catch
  channels posted since the last sweep. All new params are optional/default-preserving (`sort` is
  passed ONLY to the scrapetube provider by name; `harvest_adaptive=False` restores the old path), so
  the Lab's callers are unaffected. Verified headlessly + the engine's own test suite stays green.
  ENGINE NOTE 11 (shared, durable query completion + always-on top-up): first-pass coverage now uses
  a separate `attempts` counter. A successful provider response counts even when it returns zero
  videos; a provider failure or fully blocked provider set does not count and remains retryable.
  `_select_harvest_queries` pulls up to ten never-attempted variations into the next fast-discovery
  slice, preventing the last one or two searches from waiting a full rotation. Once expanded coverage
  reaches 100%, optional `query_topup_enabled` logic in `query_topup.py` may add at most two reviewed
  faceless/replicable subjects from different topics. Candidates require cross-channel evidence from
  classified faceless channels plus related harvest-yield support, and pass the shared 600-query cap,
  normalized family/similarity deduplication, 12-hour cooldown, per-topic cap, and cross-process config
  lock used by Beacon Autopilot. The feature defaults OFF, so Production Lab/legacy configs keep their
  old behavior; Beacon's live config explicitly enables it. Live verification on 2026-07-12 reached
  4,164/4,164 tested searches with 0 remaining and added 0 queries because the existing Autopilot
  cooldown correctly won. Regression gates: 41 Beacon tests + 13 subtests; 178 shared tests; all five
  Beacon screens and an isolated Production Lab profile passed real-window smoke tests.
  ENGINE NOTE 12 (shared, off-laptop edge collector): `scripter/edge/` now provides a
  transport-independent `signal-edge` v1 protocol. `plan.py` snapshots up to 10,000 normalized
  queries into signed immutable `youtube.discovery.plan.v1` files; the current live radar config
  validates at exactly 4,206 searches. `collector.py` runs bounded parallel no-quota scrapetube
  searches and, in hybrid mode, uses `YouTubePool` only for cheap batched video/channel
  enrichment. The pool retains exhausted-key state within one UTC day, resets the next day, and
  reports quota per batch. `worker.py` persists a JSON cursor, gives never-tested searches absolute
  priority, immediately runs catch-up batches, treats successful empty responses as tested, retries
  provider failures with backoff, switches repeat searches to newest-first, retires unchanged
  queries, and reserves periodic reprobe slots. It writes signed `youtube.discovery.v1` bundles;
  `importer.py` verifies checksum/HMAC, imports channels/videos/search links/snapshots/query outcomes,
  records an `edge_import_batches` idempotency ledger, and writes signed acks.
  This is staging, not a second YouTube index. `requirements-edge.txt` intentionally excludes Flet,
  scikit-learn, and Bedrock; it now includes headless OpenCV/NumPy for the separate bounded face
  worker. Real isolated smoke:
  one free query produced 3 videos + 3 channels, imported into a temporary DB, and acknowledged.
  `edge/sync.py` adds the laptop-side secure inbox service: non-secret status is read-authorized;
  bounded import is a full-token Finder write; accepted/duplicate bundles are archived and invalid
  bundles stay quarantined. Beacon Overview now exposes refresh/manual sync controls and invalidates
  its caches after successful imports while stating that Oracle never owns the canonical database.
  The provisioned laptop transport batches up to 20 downloads or acknowledgements per SSH session,
  reuses one initialized SQLite connection, gives each immutable bundle its own savepoint/ledger
  outcome, durably commits groups of 20, and writes acknowledgements only after commit. It runs a
  fully windowless 100-bundle drain every two minutes while the laptop is online. A
  `wscript.exe` wrapper hides the complete PowerShell process tree while waiting and returning its
  real exit code, so transport/import failures still fail the task without opening a terminal. Live verification
  on 2026-07-13 imported two new bundles end-to-end in about 18 seconds with zero errors; an isolated
  20-bundle benchmark imported 15,873 channel records in 18.64 seconds.
  After a sleeping laptop wakes, one hidden task invocation now keeps draining consecutive
  100-bundle slices until both queues are empty or a bounded 90-minute catch-up budget expires.
  The first cloud-organization stage is also live: Oracle gives each discovered channel a clean
  deterministic topic and a `text-v3` faceless score from the videos in that signed batch. Beacon
  only fills a missing topic and only replaces a text classification when the incoming release is
  current and its evidence sample is stronger/newer, so local decisions remain authoritative. A
  real post-deployment bundle imported 895 channels, 895 topics, and 895 classifications with a
  signed acknowledgement and zero remaining remote bundles.
  The laptop's changing public IP is now handled before every sync by an OCI-native updater. A
  separate locally protected API signing key updates exactly one managed TCP/22 security-list rule
  to the current public IPv4 `/32`, uses ETag concurrency control, and preserves all unrelated
  ingress, egress, and tag state. Its pinned OCI SDK lives in an isolated storage venv so it cannot
  conflict with Lightning's `urllib3` requirement. During rollout the public IP changed, the stale
  rule caused one expected failed sync, the updater repaired the `/32`, and later scheduled tasks
  returned result 0 with no transport errors and no remote backlog.
  The sync runner now snapshots the live watchlist and edge settings after a successful connection,
  signs and publishes a new immutable plan only when that specification changes, and reports plan
  state separately. Automatic plan publication is blocked for custom/test sync roots unless
  explicitly enabled; this prevents isolated tests from mutating the production jobs directory.
  While Oracle supplies discovery, Beacon's live radar has local harvesting disabled and executes
  organization-only cycles. Those cycles retain enrichment, thin cataloging, video categorization,
  topic/junk/face/text classification, and workspace bookkeeping, but skip network expansion, lab
  sync, intel fuel, remote VLM, niche crawling, whole-index outlier scoring, and competitor tracking.
  Beacon Overview prefers the fresh organizer message over stale query text.
  The Oracle collector now retries only transient free-search JSON, timeout, connection, HTTP 429,
  and HTTP 503 failures, at most three attempts with exponential backoff and jitter. Its first real
  post-deployment bundle completed 49/50 searches, recovered 14 transient attempts, and delivered
  931 channels with topics and classifications; nearby earlier batches had 8-12 failures each.
  A separate `signal-edge-organizer.service` now consumes signed
  `youtube.organization.job.v1` files and emits signed `youtube.organization.v1` results. Discovery
  queues only channels attached to newly discovered videos, normally in 100-channel jobs; the
  protocol remains capped at 500 channels and 10,000 videos. Oracle keeps only operational
  organizer state, retries, models, jobs, and result files. It never opens or receives the canonical
  SQLite index. The organizer enriches channels, resolves official uploads playlists, catalogs up
  to 30 uploads/channel through `playlistItems.list`, batches video/category enrichment, runs
  deterministic topic and `text-v3` classification, and performs bounded CPU face checks. The
  importer atomically preserves stronger/newer local evidence and deduplicates source job revisions.
  Two consecutive production jobs completed with zero channel errors: 124 channels/2,510 videos in
  147.7 seconds and 157 channels/3,443 videos in 213.0 seconds. Together they collected 5,292
  uploads, filled 5,666 category assignments, and used 300 quota units. Scheduled sync imported and
  acknowledged both with no remaining remote outbox bundles.
  Repeat suppression was tightened on 2026-07-14. The organizer now drops the attached videos of
  channels already processed inside its refresh window, including video-only repeat jobs.
  Discovery bundles always retain signed query-run tracking but emit channel/video/classification/
  discovery/snapshot rows only for new results or an existing video whose observed view count
  changed. The first production delta bundle examined 2,000 result observations, suppressed 1,444
  unchanged repeats, retained all 50 query runs, and emitted 481 video rows plus 336 channel rows.
  A controlled repeat bundle was 78.7% smaller. The deployed immutable revision is `bb567a5`;
  both Oracle services are active. Following a temporary network interruption, the dynamic `/32`
  updater and hidden task recovered automatically, returned result 0, and left both queues empty.
  `deploy/oracle-edge/` now builds an allowlisted archive from a committed revision and installs an
  immutable Python 3.11+ release behind an unprivileged, no-inbound-port, systemd-hardened service.
  Persistent jobs/outbox state stays under `/var/lib/signal-edge`; health checks and symlink rollback
  are explicit. Full verification: 232 shared tests, 43 Beacon tests + 13 subtests, and all five
  Beacon screens. Future cloud organization must continue through signed, versioned records rather
  than copying or remotely opening the canonical database.
  The authorized YouTube key pool was made concurrent later on 2026-07-14.
  `YouTubePool` now assigns one in-flight API request to each healthy key,
  preserves batch order, tracks non-secret per-key unit counts, removes
  exhausted/forbidden/invalid keys from the active process pool, and moves
  pending work to the remaining keys. Batched `videos.list` and
  `channels.list`, Oracle upload-playlist cataloging, and category enrichment
  all use this pool while respecting existing quota ceilings. Concurrency grows
  automatically with future keys up to a configurable 16-lane default and a
  hard safety cap. `scripts/add_edge_youtube_keys.ps1` provides hidden-input,
  deduplicated laptop + Oracle enrollment through SSH stdin; key values never
  enter command arguments or result output. Immutable revision `cdea38e` is
  active. A real six-batch check used all six configured keys simultaneously
  (`quota_units_by_key` was one for every lane); one permanently forbidden
  legacy key was removed. Final sync returned result 0 with zero backlog.
  Verification: 240 shared tests, 43 Beacon tests + 13 subtests, `pip check`,
  and repeated visual checks of all five Beacon screens.
  Turbo Discovery was deployed later on 2026-07-14. The signed plan now uses
  up to 125 searches every five minutes across 24 bounded workers, still within
  the protocol's 5,000-result ceiling. The laptop places a rounded six-hour
  canonical yield estimate in the plan and keeps it stable across changes
  smaller than 30 channels per 1,000 searches. Oracle targets at least 450 new
  channels/hour; more than 5% query failures reduce the batch by 25 and double
  the interval, while two healthy cycles recover one step. The base-query
  ceiling is 600, but the two-query autonomous limit, evidence gate,
  deduplication, per-topic limits, and 12-hour cooldown remain. The first four
  imported production cycles completed 500/500 searches with zero failures and
  added 161 genuinely new channel IDs plus 5,181 new video IDs to the canonical
  index. That measured yield projects to about 483 channels/hour or 11,592/day
  if it holds. Discovery now checks Oracle's small durable organizer history
  before enqueueing jobs, so channels organized within 30 days never enter the
  queue. The first production cycle after that change created two jobs instead
  of roughly nine, and the inherited backlog fell from 71 to 67 while fresh
  discovery continued. Immutable revision `a3ab046` is active.
  Verification: 243 shared tests, 38 Beacon tests, `pip check`, and repeated
  real-window checks of all five Beacon screens.
  The laptop catch-up path was permanently accelerated on 2026-07-15. Edge
  datasets now use prepared bulk SQLite writes with a larger connection-local
  page cache while preserving per-bundle savepoints, idempotency, commit
  durability, and signed acknowledgements. Discovery and organization bundles
  retain only raw fields still consumed by the canonical index: channel
  thumbnails and video category IDs; all parsed fields remain in normal
  columns. A disposable 500,000-video benchmark improved from 5.434 seconds to
  1.352 seconds for the same five production bundles (4.02x), and equivalent
  future bundles were 28.8% smaller. `sync_progress.json` now records committed
  count, rate, ETA, and combined laptop/Oracle backlog; Beacon displays it on
  the Cloud worker card and disables overlapping sync. Shared revision
  `060c8f2` is active on Oracle release `20260715T132751Z-330208`; its first
  1,410-channel/2,219-video production result imported in 13.859 seconds.
  Verification: 245 shared tests, 44 Beacon tests + 13 subtests, and a complete
  real-window pass of all five screens plus the in-progress cloud card.
  On 2026-07-16 the progress contract gained live, non-secret transaction
  activity (`verifying`, `waiting_for_database`, `staging`, `committing`,
  `acknowledging`) while keeping `processed_bundles` strictly committed-only.
  Beacon shows the committed bar separately from the live transaction bar and
  polls every three seconds during imports. Older already-running imports get
  a compatibility explanation; the next process automatically emits detailed
  steps. Verification: 249 shared tests, 48 Beacon tests + 13 subtests, and
  real-window checks of both progress modes plus all five screens.
  On 2026-07-17 post-commit Oracle acknowledgements moved to one coalescing
  background SSH worker. It retains 20-file batches and the durable-commit
  barrier, overlaps upload with the next import transaction, waits for the
  final upload before returning, and leaves failed signed acknowledgements for
  retry. A second background worker passively checkpoints the canonical WAL
  after commits and makes bounded truncate attempts above 128 MiB (250 ms while
  active, 1.5 seconds after close). Sync status records the maintenance outcome.
  Verification: 252 shared tests, 55 Beacon tests + 16 subtests, real SQLite
  WAL growth/truncation, blocked-SSH overlap, and two five-screen app passes.
  ENGINE NOTE 13 (shared, split-radar recovery): on 2026-07-14 a live `py-spy`
  capture proved that the organizer lane had exited while `_run_split_pipeline`
  waited forever for the still-running discovery thread. The split worker now
  owns a process-local shutdown event and sets it before joining discovery on
  every organizer exit, including unexpected exceptions. The same investigation
  found starter-workspace refresh materializing the full 1.6-million-video index
  even though channel classifications were already persisted. Workspace
  bootstrap now reuses those saved scores and only classifies as a fresh-index
  fallback. The old process held 2.38 GB private memory; the repaired live cycle
  held about 130 MB outside the bounded OpenCV face stage, crossed the former
  failure point, refreshed 11 workspaces, completed at 01:12 Casablanca time,
  and began cycle 2 normally. Verification: 228 shared tests, 43 Beacon tests +
  13 subtests, plus two full real-window passes of all five Beacon screens.
  ENGINE NOTE 6 (shared, API-key pool + video categorizing): YouTube Data API quota is allocated
  per Google Cloud project. On 2026-07-11 the user reported direct Google authorization to use
  multiple keys across separate accounts/projects for this workload. Treat that as user-provided,
  project-specific authorization and keep the implementation inside its exact written scope; the
  code itself is not evidence of policy permission. Never print, document, or commit a key.
  `scripter/research/youtube_pool.py` `YouTubePool` reads `YOUTUBE_API_KEYS` (comma-separated, in
  `storage/.env`, NEVER in code), uses one key until it 403s `quotaExceeded` then rotates to the next,
  and marks invalid/restricted keys dead + skips them (verified: starting on the known-bad key #2 rotated
  past it and still returned data; ~40 units per 2000 videos). `scripter/research/categorize.py` fills the
  new `videos.category_id` column (SCHEMA_VERSION **7**): stage 1 backfills FREE from stored `raw_json`
  (official-API videos already carry `categoryId` — did 329,729 at 0 quota); stage 2 fetches the rest via
  the pool (1 unit/50 videos; `'na'` marks deleted/private so they're not re-fetched). `channel_categories`
  rolls it up to a dominant category per channel. The remaining backlog (~620k scrapetube videos, ~12k units
  total) is DRIP-drained by a radar cycle step (`categorize.run`, gated by `RadarConfig.categorize_enabled`
  + `categorize_per_cycle` default **3000** videos/cycle ≈ 60 units — runs BEFORE the junk filter so
  newly-tagged music channels get excluded the same cycle). No quota spike; finishes over a few days while
  leaving headroom for discovery/enrichment. All additive/optional; the Lab is unaffected. NOTE: the drip
  only advances while the background scraper is running (Beacon Overview → Start, or the login launcher).
  BUG FIX (shared, Windows): `radar._pid_is_running` used `os.kill(pid, 0)`, which on Windows maps to
  CTRL_C_EVENT and raises WinError 87 for an arbitrary pid — so the worker's singleton check CRASHED on
  startup whenever a `radar_worker.pid` file already existed (i.e. every restart after the first). Fixed to
  use the Win32 `OpenProcess`/`GetExitCodeProcess` liveness probe (same as `sweep_control._pid_alive`). This
  is why restarting the scraper to pick up new engine code previously appeared to "do nothing". Affects the
  Lab too (strict improvement). A running worker only reloads engine edits on a full process restart
  (Stop→Start); a hung worker can be force-killed safely (WAL rolls back the open transaction).
  ENGINE NOTE 7 (shared, scoring cadence for more discovery): the whole-index outlier-scoring step
  (`run_cycle` "Scoring fresh and global outliers": `video_metadata_rows` + two `find_outliers` over the
  WHOLE index) is the heaviest cycle step — ~10-20 min of pegged CPU that adds NO channels, crowding out
  harvest/discovery. New `RadarConfig.score_outliers_every_n_cycles` gates it to run only every Nth cycle
  (`(cycle-1) % N == 0` → cycles 1,4,7… for N=3); skipped cycles set `fresh_outs/global_outs=[]` and log
  "Skipping whole-index outlier scoring…", reaching the next harvest sooner. Default **1** = every cycle
  (unchanged, so the Lab is unaffected); the shared config `scraper_watchlist.json` sets it to **3** for
  Beacon's scraper. Tradeoff: outlier scores refresh every 3rd cycle instead of every cycle. Verified live:
  after deploying, a fresh cycle's harvest added ~128 channels in ~3 min (~40/min).
  ENGINE NOTE 8 (shared, clean TOPIC niches for every channel): `scripter/research/topic_classifier.py`
  assigns EVERY channel one human-readable topic (History / Space & Physics / True Crime / Home, DIY &
  Self-Sufficiency / … 21 topics + 'Other') via ordered keyword rules over its `category` (the discovering
  search query — 100% coverage but 43k+ messy variants) + title + description; generic harvest suffixes
  ('explained'/'mystery'/'documentary'/'facts'/'exposed'/'case study') are deliberately NOT signals.
  Stored in a new `channels.topic` column (SCHEMA_VERSION **8**, indexed), backfilled for all ~55k (100%
  coverage, ~16% land in 'Other'), and kept fresh each radar cycle (`topic_classifier.backfill(only_missing=True)`
  gated by `RadarConfig.topic_classify_enabled`, runs before the junk filter). Finder: `channel_metrics`
  gained a `topic` filter + returns `topic`; `find_channels(topic=…)` forwards it + includes topic in its
  rows; new **read** tool `list_topics` (write:False → Lab's read key sees it) returns `[{topic,count}]`
  (excluded channels hidden) for the dropdown. All additive/optional; the Lab is unaffected. NOTE: `topic`
  (clean niche, this note) ≠ `videos.category_id` (YouTube content type, ENGINE NOTE 6) ≠ `niche_channels`
  (the 11 curated workspaces). Beacon shows it as the Channels-tab Topic dropdown + each card's subtitle.
  OPS GOTCHA (Windows): the radar worker shows as TWO processes — the venv `pythonw.exe` is a STUB that
  re-execs the base interpreter (system Python), so `[stub, child]` = ONE logical worker (not a collision).
  A naive `wmic | grep | kill` can miss them and leave an orphan running the OLD code (so a "restart"
  silently keeps the old behavior). To truly restart: enumerate BOTH by CommandLine match ('radar-worker'),
  taskkill each, verify none remain, THEN `start_worker`. Also DON'T judge worker health by
  `radar_status.json`'s `message` (it goes stale mid-step) — check DB row growth or `radar.log` (which
  timestamps every step) or the worker pid's CPU. A stale status + high CPU + a `radar.log` line like
  "Scoring fresh and global outliers" = a healthy long step, not a hang.
  ENGINE NOTE 9 (shared, exact counts): `store.channel_metrics(count_only=True, …)` reuses the SAME filter
  build but returns `[{"count": N}]` via `SELECT COUNT(*) FROM (m-CTE) WHERE …` (the m-CTE has no bind
  params, so it binds `params[1:-1]` — dropping rpm[0] + limit[-1]). New **read** tool `count_channels`
  (write:False) exposes it with find_channels-style filters, so the Beacon agent answers "how many …" in
  ONE cheap exact query instead of listing + tallying (find_channels is limit-capped). Additive; Lab unaffected.
  ENGINE NOTE 10 (shared, agent write tools): new **write** finder tools back the Beacon agent's edit/clean
  powers — `exclude_channels`/`unexclude_channels`/`exclude_by_filter` (bulk by criteria; refuses with no
  filter; reversible via channel_exclusions), `run_junk_filter` (the same music/meme/brand cleaner the radar
  runs), `set_channel_topic` (new `store.set_channel_topic`), `reclassify_topics` (topic_classifier.backfill),
  `create_niche` (workspaces.create_workspace). All write:True → the Lab's READ key can't call them; Beacon's
  FULL key can. Additive; the Lab is unaffected. NOTE: exclusions are reversible (hidden, never deleted) —
  that reversibility + the agent's "✓ What I did" report are the safety model for its autonomy.
- `beacon_save.py` — the **Saved** tab: a channel YOU found on YouTube, saved and analysed on the
  spot. Every other screen shows what the crawler found; this is the opposite direction.
  `parse_link` classifies a pasted link (channel id / @handle / /c/ / /user/ / watch?v= / youtu.be /
  shorts — a VIDEO link is the shape you actually have when something good appears in your feed, so
  it is tried FIRST because a watch URL can carry a UC-looking string in other params).
  `resolve_channel_id` costs nothing for a channel link, one cheap lookup for a video (pool
  `videos()` → `snippet.channelId`), and falls back to `ChannelAnalyzer._resolve` for handles/names
  (it already implements YouTube's legacy URL forms + a search fallback). A handle that does not
  exist raises `SaveError` rather than saving a wrong guess — verified live.
  `save_channel` order matters: **seed → file → classify → read back**. The workspace only accepts
  channels the index already knows, so seeding first is what lets a brand-new channel be saved at all.
  Saved channels live in an ordinary niche workspace, **"Saved from YouTube"** (created on first
  save), so they inherit alerts/snapshots/upkeep for free. The card row is read back through
  `find_channels` — the SAME call the Channels grid uses — NOT `compare_channels`, which returns
  growth figures without avg_views/outlier_score/opportunity_score.
  `saved_channels` deliberately passes `faceless_only=False, replicable_only=False`: every other view
  applies Beacon's opportunity gate, but these were chosen by a person, and hiding one for failing an
  automatic test would overrule the operator about their own find. The card says when one falls short
  instead. Verified live: a video link resolved to GrowVeg (already indexed, **0.0× opportunity** —
  1.1M subs, 16.8y old, correctly judged as NOT an opportunity), and "The Restorer" was genuinely
  NEW to the 200k index (seeded + enriched + 15 videos pulled in seconds, 0.5×).
  ENGINE NOTE 14 (shared, additive): new **write** tool `seed_channels(channel_ids, source?, enrich?)`
  in `analyst_tools.py` — the one primitive that did not exist. `track_competitor` refuses anything
  not already indexed, and `add_channel_seeds` writes only a bare row, so `enrich=True` pulls live
  stats + recent uploads immediately (that is the difference between a fresh save showing real
  numbers and showing blanks until the next radar cycle). Wrapped in the engine's own `_retry_locked`
  with SHORT timings (4 tries × 2.5s, not enrichment's 20s) because an operator is waiting on a
  button — the radar and cloud importer write this DB too and a bulk import outlasts the 30s
  busy_timeout. Enrich failures are REPORTED, not raised: the rows are already seeded, so a quota
  blip degrades to "saved, numbers pending" instead of losing the find. write:True → the Lab's read
  key cannot call it; additive, so the Lab is unaffected.
  GOTCHA: `engine_link` now loads the ENGINE's `.env` (`_load_engine_env`, override=False so the DB
  path always wins). `scripter.config` calls `load_dotenv()` with no path, which searches from the
  CWD — started from `D:\Youtube\Beacon` it never found `Quill\.env`, so `YouTubePool` came up with
  zero keys inside Beacon while working fine in the radar worker (cwd = Quill). That asymmetry is
  invisible until something in Beacon calls YouTube directly.
  LIVE TRACKING (2026-09-15): saved channels go stale fast — the fast movers are exactly the
  ones worth saving. One saved at 40,700 subs was really on 45,800 the next evening (+12.5%),
  and the card showed the old figure. `refresh_saved` calls `sync_competitors` (live stats +
  uploads + a `channel_snapshots` growth point) and then reads the rows BACK to report what
  actually moved — necessary because `sync_competitors` swallows a transient API failure and
  still returns `refreshed: 0`, which is indistinguishable from "already current". The Saved
  tab has a **Refresh live** button plus `_saved_autorefresh` (silent, on open, only when older
  than `SAVED_STALE_SECONDS` = 30 min, so opening the tab does not burn quota every time), and
  shows "live numbers checked Xm ago" so staleness is never hidden. Each card carries a growth
  pill — **"+5.6K subs since saved · 6.4K/day"** — from `get_competitor_growth`; it renders
  NOTHING from a single snapshot, because one reading is a number, not a trend, and a "+0"
  would read as "flat" when the truth is "not measured yet". Growth is fetched once per refresh
  for ≤40 channels, never per card render (that would be one finder call per card per repaint).
  SHARED BUG FIXED (affects the Lab's competitor tracking too): `refresh_competitor_uploads`
  fetched a channel's uploads via yt-dlp, which returns them with `channel_id=""` — so every
  row landed in the index ORPHANED, detached from its channel. It still counted them as new
  uploads, so the damage was invisible: the card kept showing an avg-views figure silently
  derived from lifetime views/uploads while not one video was actually attached. Now stamps the
  id it was asked for. `seed_channels` also uses the cataloguing crawler rather than that path.
  SHELF FIXES (2026-09-16), all Beacon-side: (1) a failed refresh was shown as "Could not save that
  link", stamped "checked just now", and wiped the growth pills — it now gets its own "Couldn't
  refresh live numbers" banner, `_saved_refresh_failed_at` is tracked separately, and automatic retry
  pauses `SAVED_RETRY_SECONDS` (2 min) so a busy DB is not retried in a render loop (the button always
  retries). (2) `refresh_saved`/`remove_saved`/filing now go through `_call_when_free`: retries ONLY
  "database is locked"-type errors, 4 x 2.5s, matching seed_channels. (3) the shelf is ordered by
  `niche_channels.added_at` (when YOU saved it), not `first_seen_at` (when the crawler found it).
  (4) `saved_channels` makes ONE `list_competitors` call that returns save time + growth for the
  whole shelf and attaches `saved_at`/`growth` to each row (was one `get_competitor_growth` per card,
  and pills only appeared after a refresh). (5) `growth_summary` says "since saved" only when the
  first snapshot came after the save, else "since first measured". (6) the Analysis dialog gets the
  card's avatar (get_channel_details pops raw_json, where the avatar lives).
- `engine_link.py` — bridge to the shared engine + DB.
- `finder_service.py` — runs the finder as a local HTTP service. Beacon STARTS it as its own child process on boot if none
  is already running (header pill "Finder connected"; in-process self-hosting, pill "Local finder", is only
  the fallback — see RESPONSIVENESS). Start the standalone one any way you like —
  `start_finder_hidden.vbs`, pythonw, or plain `python.exe`: started from a console it re-launches itself
  under pythonw and exits, so it never leaves a Windows Terminal window parked on the desktop. That handoff
  happens *before* the engine imports (~5s) so the console barely blinks. Output goes to
  `storage/finder_service.log`, and a second launch checks `/health` and steps aside — on Windows
  SO_REUSEADDR lets a duplicate *steal* the port instead of failing, so the bind never raises.
  `FINDER_CONSOLE=1` opts out and keeps it in the foreground.
- `sweep_control.py` — Beacon-only control for the shared background scraper. REUSES the engine's
  existing radar worker (does NOT run its own): Start launches `python -m scripter.cli radar-worker`
  detached (cwd=Scripter root); Stop writes the `radar_stop.flag`; status is read straight from
  `storage/radar_status.json`; niches/queries are the `watchlist` in `storage/scraper_watchlist.json`.
  All paths absolute, anchored to `dirname(YOUTUBE_INDEX_DB)`. Overview has the Start/Stop + editor card.
- `beacon_agent.py` — Beacon's OWN AI control agent (the **Agent** tab). A self-contained AWS Bedrock
  (Claude **Opus 4.6** = `global.anthropic.claude-opus-4-6-v1`; 4.8/4.7 are access-denied on this AWS
  account) tool-use loop via boto3 `converse`. DELIBERATELY SEPARATE from the Lab: imports NOTHING from
  `scripter.chat`/`scripter.agent_tools`/`scripter.llm` — its only engine links are Beacon's `FinderClient`
  (read+write finder) + `sweep_control` (start/stop scraper), the same bridges the app already uses. ~26
  curated tools — **read** (db_stats, find_channels, count_channels, list_topics, list_niches,
  recent_channels, find_rising_videos, get_channel_details, get_niche_channels, discover_channels,
  radar_status) + **write/edit** (eliminate: exclude_channels, exclude_by_filter, run_junk_filter,
  unexclude_channels; categorize: reclassify_topics, set_channel_topic; organize: create_niche,
  add_channel_to_niche; control scraper: crawl_niche, start/stop scraper, get/add/remove watchlist
  queries, set_scraper_config; control the APP's VIEWS: show_channels (applies Channels-tab filters +
  switches to it so the user SEES it), open_view, clear_channel_filters). Finder tools forward to
  `FinderClient.call(name, **args)`; Beacon-local ones hit `sweep_control`/edit the config JSON; view tools
  call `ui.agent_show_channels`/`agent_open_view`/`agent_clear_channels` on the app (passed as
  `BeaconAgent(.., ui=self)`) which set `ch_filters`/`ch_topic`/`ch_niche`/`section` + `_render`. It works AUTONOMOUSLY within a task (multi-step, no mid-task
  prompts) but is UNDER CONTROL by TRANSPARENCY — everything it excludes is REVERSIBLE (unexclude), and it
  ends any state-changing turn with a "✓ What I did" report (action, count, how to undo). `BeaconAgent.run`
  loops (cap `MAX_TOOL_ROUNDS=18`), streaming `text`/`tool`
  events to the UI. Config in **Beacon's OWN `.env`** (`D:\Youtube\Beacon\.env`, gitignore-equivalent since
  Beacon isn't a repo): `AWS_REGION`/`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (mirror the engine's .env —
  same AWS account) + `SIGNAL_BEDROCK_MODEL`. `beacon_app.py` has the **Agent** nav tab (`_render_agent`):
  a chat box (input pinned at top so it's always reachable) + transcript with tool-call chips; sends via
  `run_bg` streaming. Verified live: real Opus 4.6 answered "how many faceless History channels under 5k
  subs" → called `count_channels` → **2,290** (exact, matches the DB).
- `beacon_autopilot.py` — OPTIONAL background maintenance loop (**OFF by default**), an "Autopilot" card in
  the Agent tab. When enabled it wakes every N min (default 30) and runs a **restricted** `BeaconAgent`
  (`allowed_tools=SAFE_TOOLS` — only reclassify_topics / run_junk_filter / add_watchlist_queries + read
  tools; destructive tools like exclude_by_filter/set_channel_topic/create_niche/start-stop are BLOCKED at
  both the tool-list AND `_dispatch` layers) with a fixed MAINTENANCE_GOAL prompt (categorize new channels,
  hide new junk, check scraper, brief on what's new, sparingly top up ≤2-3 queries). Each pass appends a
  timestamped entry {at, tools, summary} to `_autopilot.json` (state: enabled/interval/log[≤50]) shown in the
  card's "Recent activity". Runs INSIDE the Beacon app (stops when Beacon closes — the durable radar handles
  discovery regardless). `BeaconApp.autopilot` is created on boot + auto-starts only if previously enabled;
  card has Off/On + interval + **Run now**. Verified live: Run now → real pass (reclassify+junk+reads) →
  logged report. NOTE: the loop calls Bedrock each wake (cost); it's off until the user turns it on.
- `check_engine.py` — headless sanity check that reads the shared DB.
- `beacon_ai.py` + `ai_providers.json` + `beacon_mcp.py` — the AI SWITCHBOARD (2026-09-16). The model
  is a SETTING, not code. Four adapter kinds cover nearly every service: `openai` (any
  OpenAI-compatible API: Azure Foundry/OpenAI, OpenAI, Gemini, Vertex AI, OpenRouter, Ollama…),
  `anthropic` (Claude API key), `bedrock` (any AWS account/profile), `claude_code` (Claude on a
  SUBSCRIPTION via the `claude` CLI). Built-ins `foundry`/`foundry-vision`/`bedrock`/`claude-code`
  read the old .env names; `ai_providers.json` adds more. Keys ONLY in Beacon/.env, referenced as
  `${NAME}`, `${A|B}`, `${A:-default}`; a literal key in the JSON is refused. Choice order: the Agent
  tab picker (`_ai_choice.json`, gitignored) > `BEACON_AI_<ROLE>` > JSON `use` > legacy
  `BEACON_AGENT_PROVIDER`. Roles: agent, autopilot (follows agent unless pinned), vision.
  `beacon_agent.py` keeps ONE loop + NEUTRAL history ({role,text,tool_calls} / {role:tool,results});
  adapters translate per call, so switching provider keeps the conversation. Provider-specific
  echo-back data (Claude blocks, Gemini thought signatures) rides under `wire[provider_name]`.
  Claude Code path: `ClaudeCodeRunner` runs `claude -p --output-format json --tools ""
  --strict-mcp-config --allowedTools mcp__beacon`; `beacon_mcp.py` (stdlib MCP stdio server) calls
  back into Beacon through `ToolBridge` (127.0.0.1, per-turn token), so tools run in-process through
  the SAME dispatcher (Autopilot restrictions + view switching still apply). The runner strips all
  inherited CLAUDE*/ANTHROPIC_* vars — launched from inside another Claude session they point the CLI
  at that session's gateway and it reports "Not logged in". Standalone: `claude mcp add beacon --
  <venv python> D:\Youtube\Beacon\beacon_mcp.py` lets any Claude Code chat manage Beacon (needs the
  finder running). Agent tab: provider dropdown + Test. "Settings filled in" is never shown as
  working — only a real reply from Test is. Verified live 2026-09-16: Foundry Test = HTTP 401 (the Azure
  account is gone). CLAUDE CODE IS NOW THE ACTIVE PROVIDER (`_ai_choice.json` = claude-code) and works
  end to end on the operator's subscription after `claude auth login`: Test = "answered in 4.4s:
  READY"; a headless turn called `count_channels` through the bridge and answered 658 in 6.6s; in the
  app, "Show me faceless History channels under 5000 subs with at least 3x opportunity" made it call
  `show_channels` and Beacon switched to Channels with Topic=History + chips `subs <= 5K` /
  `outlier >= 3.0x` and 120+ cards (Lost History 201.0x). GOTCHA that broke the first real turn: the
  CLI reads `~/.claude/settings.json`, which held `model: deepreinforce-ai--ornith-10-397b` (no
  subscription access). The runner now passes `--setting-sources project` (the work folder has none,
  so no user model/hooks/status line/permissions leak in) and ALWAYS names a model —
  `${BEACON_CLAUDE_CODE_MODEL:-sonnet}`; set `BEACON_CLAUDE_CODE_MODEL=opus` in .env for the strongest.
  Each turn spawns the CLI fresh (~4-7s) and spends subscription usage, so Autopilot (ON, every 30
  min) runs on it too unless `use.autopilot` pins something else. Standalone MCP returned a real
  count (659) from the finder.
  NEW AZURE ACCOUNT (2026-09-17) — ACTIVE PROVIDER IS NOW "Foundry" (`_ai_choice.json` = foundry).
  `.env` BEACON_FOUNDRY_* now point at a new Azure OpenAI resource (`*.openai.azure.com/openai/v1`,
  auth "both" works): text model **DeepSeek V4 Flash** (`deepseek-ai--deepseek-v4-flash-0731`,
  replaces GLM, which failed to deploy there) + vision **Qwen3-VL** (`qwen--qwen3-vl-30b-a3b-instruct`).
  Verified: Test READY 1.4-1.8s; agent tool turns exact vs DB (664 / 513 faceless History <5K subs,
  >=3x) and a follow-up ("of those…") → Lost History 200.97x, ~5s/turn; Qwen read a Beacon screenshot
  exactly; real-app question answered 664/513 with a "Counted channels" chip. DeepSeek notes: normal
  mode returns no separate reasoning and sometimes answers in LaTeX (`oxed{}`); `reasoning_effort`
  ("low|medium|high") switches on thinking (`reasoning_content`) — `chat_template_kwargs` and
  `thinking` are REJECTED (HTTP 400). New optional provider field `reasoning_effort` (sent only when
  set; builtin foundry reads `BEACON_FOUNDRY_REASONING_EFFORT`) and dropdown entry **"Foundry ·
  thinking"** (same env, effort high via `BEACON_FOUNDRY_THINKING_EFFORT`, 8000 tokens). Harder
  reasoning checks: normal 3/4 (7.9s), thinking 4/4 (17.5s); thinking + tools 6/6 correct.
  `post_json` retries connection errors (`CONNECT_TRIES`=3, 1.5s/3s) — a DNS blip once killed a
  thinking turn between two good calls; lookup failures now read "could not find the address X —
  check the endpoint … or your internet connection". GOTCHA: the first endpoint sent (`fkdopre`)
  did not exist in DNS; the real one was `fkdoprre` — ALWAYS `Resolve-DnsName` the host before
  debugging keys. The account/key is rotated often (a third resource + key arrived the same
  evening, `sjiorpepkmme`, same two deployments): the swap is six .env lines + Test, no code
  change — on the new resource Test READY 1.4s headless / 8.3s first call in-app, tools exact
  (666 / 513, follow-up Lost History 200.97x), Qwen exact. Also: `.env`
  reads can be denied by the permission system; edit it only with the operator's explicit OK, via a
  script that prints names only, with the backup outside the repo. Autopilot follows the agent,
  so it now runs on DeepSeek (Azure usage) when enabled.
  AUTOPILOT POLICY PER PROVIDER (2026-09-17, operator's rule "when Azure is the provider, always
  turn it on"): new optional provider field `autopilot` — "auto" keeps Autopilot ON while that
  provider is selected, "manual" (the default) leaves the Off/On switch to the operator. Azure
  credits are prepaid, so background passes cost nothing extra; a Claude subscription has usage
  limits, so claude-code/bedrock stay manual. The Azure entries carry
  `${BEACON_FOUNDRY_AUTOPILOT:-auto}` / `${BEACON_AZURE_AUTOPILOT:-auto}`, so setting either to
  `manual` in .env hands control back. `Autopilot.apply_provider_policy()` runs on boot AND on every
  provider pick (`_ai_pick`), switches it on when needed, writes a log note explaining why, and
  never switches an operator's ON off — picking a manual provider only warns in the toast whose
  usage it now spends. The card shows the reason + the .env escape hatch.
  A real pass on DeepSeek took ~12 MINUTES (7 tools). My first reading ("mostly waiting on the
  model") was WRONG — per-tool timing showed it was the finder scanning the whole index:
  run_junk_filter 452s (excluded 0), db_stats 151s, recent_channels 40s, find_rising_videos 36s,
  reclassify_topics 1s (classified 0). The log stamp is the START time, which made
  a finished pass look stuck, so entries now carry `took_secs` ("· took 12m" in the card) and the live
  line reads "running a full sweep of the index… 8m so far" (`_pass_took` / `_pass_running_text`).
  LIGHT vs DEEP PASSES (2026-09-17/19): the two expensive steps found NOTHING because the radar
  already runs the same junk filter + topic classifier every cycle. `Autopilot.plan()` now picks a
  LIGHT pass by default (scraper_status / recent_channels / find_rising_videos / watchlist, then a
  brief); `HEAVY_TOOLS` (run_junk_filter, reclassify_topics, db_stats) are removed from the
  allowlist, not just discouraged in the prompt. A DEEP pass (the old full sweep) runs only when the
  scraper is OFF (nobody else does the upkeep) or none has succeeded for
  `DEEP_EVERY_HOURS` (12, env `BEACON_AUTOPILOT_DEEP_HOURS`); `last_deep_at` is persisted. Entries
  carry `kind`/`why`/`ok`; the card says "Light check" / "Full sweep" / "Full sweep (failed)", the
  status line says which kind the next run will be and why. Measured on Claude Code: a light check
  took 5m (4 tools). TWO BUGS found live 2026-09-19 and fixed: (1) a pass that FAILED (Azure 401
  after 0s) was credited as the full sweep, postponing the real one 12h — now only a pass whose
  summary is not a failure (`pass_failed`: "⚠"/"error:"/"Not configured") sets `last_deep_at`,
  and the log fallback (`last_sweep_in_log`) skips failures + Autopilot's own notes; (2) `ok, why =
  agent.ready()` overwrote the plan's reason, so entries logged why="ready". Also the card showed a
  failed pass with a green tick + "Maintenance completed" — now a red icon + "Didn't run — <error>".
  AZURE CLOSED (2026-09-19): the Azure account behind Foundry is gone (the 11:44 pass: HTTP 401).
  The operator switched the agent to **Claude Code** (`_ai_choice.json` = claude-code). The Foundry
  entries stay in the dropdown (never remove options) but will not answer until new keys arrive —
  do not use them in tests. Claude Code's policy is "manual", so the operator's saved Autopilot ON
  (every 30 min) stands and spends SUBSCRIPTION usage — light passes keep that small.
  MY OWN ERROR to avoid repeating: I reported "Autopilot is off in your settings" on 2026-09-16 —
  it was ON in `_autopilot.json` all along; every test copy runs with `SIGNAL_DISABLE_AUTOPILOT=1`,
  which shows "Off" in the card WITHOUT touching the saved state. Never read a test copy's Autopilot
  badge as the operator's setting; read `_autopilot.json`.
  BUSY DATABASE AT STARTUP (2026-09-19): folding a 1.8 GB write-ahead log back into the index
  locked every NEW connection out from ~15:20:45 to ~15:23 (the cloud import failed on it too).
  The finder service died on its first "database is locked", the in-process fallback hit the same
  lock, and the window showed only Flet's bare "There was an error while processing your request:
  database is locked". Now `finder_service.main` retries busy-database errors for
  `FINDER_DB_WAIT_SECONDS` (600) and logs `FINDER waiting for the database`; `_resolve_service`
  keeps waiting past `FINDER_START_TIMEOUT` ONLY while the latest start block of the log says so
  (`_finder_waiting_for_db`), the start line reads "Waiting for the database — … · 73s", the
  fallback is patient too, and anything else lands on `_boot_failure` ("The database is still
  busy" / the real error) with a working **Try again** (`_boot` is re-runnable). Verified live on a
  SCRATCH db held by an exclusive lock (never lock the real index to test): 75s and 150s locks →
  opened 2s after release with "Finder connected"; a 20s budget → failure screen → Try again →
  opened. WEB-MODE GOTCHA: every browser session builds its own BeaconApp + Autopilot and there is
  no teardown, so reloading a test copy with Autopilot live leaves the old session's loop running
  (double passes). Size the pane BEFORE the first navigate and never reload such a copy.

- `beacon_gateway.py` — MUSE LINK (2026-09-28): the one door an OUTSIDE agent may use.
  The operator has no paid AI API; Meta's Muse (muse.ai, free consumer chat) is the agent
  they have. Muse has NO MCP settings — its connector list is fixed — but it builds a
  "custom connector" to any REST API, storing the key through its own secure page, and it
  runs on Meta's cloud computer, so the API must be reachable from the internet.
  WHY NOT expose the finder directly: its keys were the code DEFAULTS (`signal-full` /
  `lab-read`, verified in storage/finder.tokens.json) — a public URL would be a lock whose
  key is in the repo, and the full key can bulk-exclude channels and stop the scraper.
  The gateway: own 43-char random key in `storage/muse_gateway.token.json` (never printed,
  gitignored via storage/); allowlist of the finder's READ tools (27) plus a WRITE_TOOLS
  deny-list; forwards over 127.0.0.1 with the finder's READ key (writes → 403 twice over);
  60 calls/min cap; logs every call and refusal to `storage/muse_gateway.log`; binds
  127.0.0.1:8770 only. Also speaks MCP at POST /mcp. `db_stats` is served from a
  background count (refreshed every 6h, stamped "counted N minutes ago"): live it took
  421s, and Cloudflare's quick tunnel drops answers slower than ~100s, so Muse's two live
  tries failed; `CALL_TIMEOUT`=90 so slow calls say so instead of hanging.
  Public road: `cloudflared tunnel --url http://127.0.0.1:8770` (binary in the session
  scratchpad, not installed system-wide). The quick-tunnel URL CHANGES on every restart —
  Muse must be told the new one. Needs the finder + gateway + tunnel all running.
  VERIFIED LIVE: Muse connected (operator pasted the key; Claude must never type keys into
  a third-party site), approved "always allow" for the domain, then answered with numbers
  that match the DB exactly: 254,108 visible channels (sum of topics), top-5 faceless
  opportunities identical to `find_channels(faceless_only, replicable_only, max_subs=100000,
  min_views_per_sub=2, sort=opportunity_score)`, and after the cache fix 260,095 channels /
  5,012,585 videos / 21,422 faceless_verified. Tests: test_beacon_gateway.py (16).
  CARETAKER `muse_link.py` + scheduled task **BeaconMuseLink** (install_muse_link.ps1, every
  2 min, /IT no admin, StartWhenAvailable so a run missed during sleep fires on wake,
  IgnoreNew). Each run: finder up? (else start finder_service, wait ≤90s — the door is never
  opened before its data) → door up? (else start beacon_gateway) → road checked FROM OUTSIDE
  (`public_up` on the public URL, as Muse sees it). A live road that fails is given
  `REPLACE_AFTER_FAILURES`=3 runs (~6 min) to reconnect by itself before being replaced, so a
  just-woken laptop does not lose its address; a dead road process (reboot) is replaced at
  once. A new working address is announced ONCE: Windows toast + `storage/muse_link.json`
  `message_for_muse` (address only — Muse keeps the key, which never changes). State/logs:
  storage/muse_link.json, muse_link.log, muse_tunnel.log. cloudflared lives in `tools/`
  (gitignored). `python muse_link.py --status` = current address + ready message.
  Verified live 2026-09-28: first run took over the hand-opened road (new address
  strongly-planes-yesterday-cayman); killing the door → reopened, same address; killing the
  finder → restarted in 5s, same address; then door killed and left alone → the SCHEDULED
  task reopened it at 21:33:05 unattended, result 0. Toast call exits 0 with no errors.
  Tests: test_muse_link.py (12 + inherited).
  REAL SLEEP TEST (2026-09-28, slept 22:06:26 → woke 22:17:32, 11 min): my claim "a quick
  tunnel keeps its address through sleep" was WRONG. Cloudflare deleted the quick tunnel while
  the laptop was offline; the still-running cloudflared (same pid) logged `Unauthorized:
  Tunnel not found` every ~30s forever. Also: Windows did NOT fire the missed repetition on
  wake — the next round came at 22:21:00, ~3.5 min later. Fix: `tunnel_gone()` (that error
  newer than the last "Registered tunnel connection") → replace at once instead of waiting 3
  rounds. Consequence: on a sleeping laptop the address changes after nearly every sleep, and
  each change costs a key re-paste in Muse — the Oracle step (always-on host) is ESSENTIAL.
  ADDRESS CHANGE = KEY RE-ENTRY (verified 2026-09-28): Muse binds a saved key to the exact
  domain. Told the new address, it replied "the connector needs to be re-saved… enter the same
  Bearer token again", and raised a fresh "Allow Muse to share information with <new domain>?"
  approval. Its first knock was 401, then 200 six seconds after the key was re-entered. So
  every reboot costs the operator a key paste — the strongest argument for a permanent address.
  NOTIFICATIONS: the toast call succeeds (exit 0) but the operator saw nothing — a full-screen
  capture showed the taskbar bell with "zZ": Windows Do Not Disturb is on, so toasts go
  silently to the notification list. That is the operator's setting; do not change it.
  QUIET LOG: the caretaker sends User-Agent `beacon-muse-link` and the door skips logging its
  /health checks (2 lines/run buried Muse's visits and fooled me once into reading my own
  checks as Muse's) — any other visit, including the caretaker's other paths, is logged.
  Chrome gotcha: typing into muse.ai before the chat finished loading silently dropped the
  message; wait for the last assistant reply to be findable, and confirm the sent message
  appears before assuming it was sent.
  ORACLE ACCOUNT FACTS (checked 2026-09-28/29, console + SSH): Free Tier account (trial
  ended, cannot be charged), region Morocco West (Casablanca) af-casablanca-1, ONE instance
  `signal-edge-casablanca` (VM.Standard.A1.Flex, Ubuntu 24.04 aarch64, 51.170.132.206, up since
  2026-07-12, ~25% load, only TCP/22 open — host iptables + OCI /32 rule). Its A1 service limit
  is **2 OCPU / 12 GB** — NOT the advertised 4/24: raising to 4 OCPU was refused with
  "service limits were exceeded: standard-a1-core-count, standard-a1-core-regional-count"
  (nothing changed, no reboot); memory field says "restricted by account limits". More needs a
  limit-increase request or a Pay-As-You-Go upgrade — the operator's call. Boot volume grown
  47 → 150 GB (Always Free allows 200 GB total) and root grown online (growpart sda 1 +
  resize2fs; sda1 is physically last) → 145 GB usable, 139 GB free, workers never stopped.
  OS updates are pending a reboot (/var/run/reboot-required) — unrelated, not done.
  PORTABILITY GOAL (operator, 2026-09-29): "let's test Oracle; when we find better options we
  move, so make the app flexible" — the HOST must be a setting like the AI provider: one
  settings file, a one-command Linux installer, a reusable move kit. Nothing Oracle-specific.
  ORACLE SANDBOX TEST (2026-09-29, /home/ubuntu/beacon-sandbox, 686 MB, processes stopped):
  code-only package (246 files, 1.1 MB, scanned: no .env/tokens/DB/key-shaped text) → venv on
  Python 3.12 aarch64 → Quill requirements minus flet-desktop/streamlit/edge-tts + boto3 ALL
  installed from prebuilt ARM wheels (EXIT=0) → finder_service answered /health → Beacon in
  Flet web mode answered HTTP 200 → viewed through an SSH -L tunnel in the browser pane:
  Overview ("Finder connected"), Channels, Agent all render and navigate. Findings to fix for
  portability: (1) clock shows server UTC (11:30) — needs a per-host TZ; (2) the Agent setup
  message hard-codes `D:\Youtube\Beacon\.env`; (3) Oracle resolved newer libs (OpenCV 5.0,
  numpy 2.5) than the laptop — pin exact versions; (4) the Windows helpers (scraper
  start/stop, caretaker, edge-sync task) need systemd equivalents; (5) Research progress was
  0/0 because Quill/storage configs are not in the code package — the move kit must carry them.
  Laptop upload measured 33.9 Mbit/s → ~1-2 h for the 31.6 GB DB. Oracle disk 72 MB/s vs laptop
  NVMe/SATA SSDs; Oracle core 1.9s vs laptop 2.5s on the same Python loop (2 vs 6 cores).
  THE HOST IS A SETTING (built 2026-09-29, tests: test_beacon_host 7, test_beacon_move 9):
  `beacon_host.py` loads `beacon_host.env` (gitignored; template `beacon_host.example.env`)
  FIRST in engine_link and beacon_gateway — env always wins, no file = the laptop's original
  layout (verified: same engine dir + DB). It applies TZ (tzset) and `default_engine_dir()`
  finds a Quill/ beside Beacon/ before falling back to D:\Youtube\Quill.
  `deploy/server/install.sh` (sudo bash install.sh beacon_code.tar.gz) installs/upgrades on
  ANY Linux box: releases under /opt/beacon/releases/<utc> + atomic `current` symlink (keeps 3),
  shared venv /opt/beacon/venv rebuilt only when requirements-server.txt's hash changes, data
  only under /var/lib/beacon (beacon_host.env 0640 written ONCE with random finder keys, data/,
  storage/, engine-storage/, state/ — .env/_ai_choice/_autopilot/_targets are symlinked there),
  systemd units beacon-finder + beacon-web (hardened: ProtectSystem=strict, ReadWritePaths=DATA)
  started, beacon-radar installed but NOT enabled until the switch-over. It ends with a real
  health check and exits 1 if either service does not answer.
  CAUGHT BY THAT CHECK: Flet's web mode needs `flet-web` and silently pip-installs it at first
  start — worked in the writable sandbox, crash-looped in the read-only service ("Read-only
  file system", restart counter 28). Now pinned in requirements-server.txt with fastapi/uvicorn/
  websockets at the laptop's versions; the re-run reported HEALTHY (release 20260929T114959Z).
  `beacon_move.py` — the move kit (stdlib only): `code` (git ls-files of Beacon + engine,
  refuses secret-looking names/contents), `pack` (SQLite online backup in ONE step, pages=-1,
  so concurrent writers never restart it; gzip parts with sha256; the snapshot is deleted once
  split), `send` (scp, skips parts the target already holds intact → resumable, retries 3x,
  manifest last), `receive` (verifies every part, rebuilds, checks size+sha256, PRAGMA
  quick_check, compares channel/video counts, then swaps — the old DB AND its -wal/-shm move to
  .previous, since an old WAL beside a new DB would be replayed into it). First real run:
  34.0 GB snapshot in 288s (261,153 channels / 5,029,785 videos) → 64 parts, 8.5 GB, packed in
  ~10 min; upload 64 parts / 8.5 GB in 25.1 min, 0 retries.
  FIRST REAL MOVE, laptop → Oracle (2026-09-29): parts verified (2 min), library rebuilt (9.5
  min) and its sha256 MATCHED the snapshot — then `PRAGMA quick_check` with the DEFAULT page
  cache read 99.5 GB for a 34 GB file and was still running after an hour (it re-reads pages).
  I stopped it and finished by hand (finish: counts via `immutable=1` + 1 GB cache took 6 s:
  261,153 / 5,029,785 = snapshot; old DB + -wal/-shm → .previous; chown beacon). FIXED IN THE
  KIT: `health_check()` always sets cache_size=-1000000; `pack` checks the snapshot on the
  SOURCE's fast disk (`--check quick` default, deletes a failed snapshot); `receive` defaults to
  `--check none` because sha256 equality already proves byte identity. Tests: 18.
  VERIFIED LIVE ON ORACLE: the finder answered find_channels(faceless+replicable, ≤100K subs,
  ≥2 views/sub, by opportunity) with the SAME top 5 as the laptop/Muse (Wild Secrets 1,620 /
  313,617 …) — 39 s cold, 3 s warm; list_topics sum 255,159; the web screens (via ssh -L
  18571:127.0.0.1:8570) show "Finder connected" and real Channels cards (Wild Secrets 367.8×).
  STATE NOW: /opt/beacon release 20260929T114959Z; beacon-finder + beacon-web active on
  127.0.0.1 only; beacon-radar installed but INACTIVE; library /var/lib/beacon/data (a snapshot
  from 11:46 — the laptop is still the live writer, so Oracle's copy ages until the switch-over);
  parts kept in ~/beacon-incoming/lib-20260929 (8 GiB) and D:\Youtube\beacon_move\lib-20260929.
  Oracle disk 47 G used / 98 G free. NEXT (switch-over): carry the engine's storage configs
  (scraper_watchlist.json, harvest_yield.json, faceless model) + the needed keys (with the
  operator's OK), stop the laptop radar, final delta copy, enable beacon-radar, point Muse and
  the edge import at Oracle, add a login before any public web access.
  SWITCH-OVER PREP (built + verified 2026-09-29, all installed on Oracle but OFF):
  (1) `beacon_move.py settings` / `apply-settings` carry the scraper's memory: allowlisted
  scraper_watchlist.json, harvest_yield/cursor, niche_cursor, the two face .onnx models, the
  faceless .joblib (engine reads it BESIDE the DB), the work queue (sqlite backup), and when
  present edge_sync/plan_sync_status.json + edge_health.json (so the target sees the collector's
  plan as "unchanged" instead of re-sending it). Config `db_path` → the target's library,
  `work_queue_path` → "" (= beside it); `--set name=value` sizes it for the host and refuses
  misspelled names; fingerprints checked before anything is written; old files → .previous.
  Real dry run: 8 files, 36.8 MB, 442 searches kept, no D:\ path left. Tests: test_beacon_move 31.
  (2) Cloud import ON the collector's machine: `beacon-edge-sync.service` + `.timer` (every 2
  min after the previous run ends) run the ENGINE's unchanged `python -m scripter.edge sync`
  as root (the collector's outbox is 0600 signal-edge), with SIGNAL_EDGE_SSH_BIN pointing at
  `deploy/server/local-ssh` — a stand-in that runs the transport's command locally, turns
  `sudo -n X` into X (real sudo refuses under NoNewPrivileges) and refuses any non-local host.
  Verified under the exact lock-down: real outbox listed + worker/organizer status read; against
  a FAKE collector folder with a test key: batch + single download, batch + single ack, acks land
  signal-edge 0600, outbox emptied; real folder untouched. SQLite hands -wal/-shm that ROOT
  creates to the DB's owner (verified: owned by beacon), so a root import never locks beacon out.
  ORDER RULE: disable the laptop's BeaconEdgeSync BEFORE enabling this timer — two importers
  would split the collector's bundles between two libraries.
  (3) `beacon-radar.service`: gets ONLY the YOUTUBE_API_KEYS lines of the key file (root copies
  them to /run/beacon-radar; the collector's signing key stays out), `ExecCondition` skips it
  while `signal_scraper_paused.flag` exists (a Stop survives reboot), `Restart=on-failure` (a
  clean Stop stays stopped). Key file = installer var BEACON_KEYS_FILE, default
  /etc/signal-edge/edge.env (holds SIGNAL_EDGE_NODE_ID, SIGNAL_EDGE_SIGNING_KEY,
  YOUTUBE_API_KEYS — names checked, values never shown). Using it needs the operator's OK.
  (4) `sweep_control` SERVER MODE: `BEACON_RADAR_UNIT=beacon-radar.service` (installer appends
  missing settings to an existing beacon_host.env, never changes old ones) → Start =
  `systemctl start --no-block` (polkit rule `50-beacon-radar.rules`: account beacon may
  start/stop/restart THAT unit only — verified allowed for it, refused for
  signal-edge-organizer), Stop = the same flag files, autostart = `is-enabled`. Laptop mode
  unchanged. Tests: test_beacon_server_mode 18 (runs local-ssh for real under sh).
  (5) Muse's door on Oracle: `beacon-gateway.service` + `beacon-tunnel.service` (cloudflared
  quick tunnel to 127.0.0.1:${BEACON_GATEWAY_PORT}, log → storage/muse_tunnel.log). cloudflared
  is NOT installed on Oracle yet (needs the operator's OK to download it). Carry
  storage/muse_gateway.token.json at the switch-over so Muse keeps the same key.
  OPERATOR OKs (2026-09-29): (a) Oracle's scraper may use the YouTube keys already in
  /etc/signal-edge/edge.env; (b) cloudflared may be installed there. DONE: cloudflared 2026.9.3
  arm64 from Cloudflare's GitHub release, sha256 matched BOTH the release asset digest and the
  release notes before `dpkg -i`; the official .deb installs /usr/local/bin/cloudflared (not
  /usr/bin), so the unit runs `/usr/bin/env cloudflared`. The door's key file was carried
  (0600 beacon) so Muse keeps its key. END-TO-END TEST, then switched OFF again: road registered
  in ~10 s; from the laptop over the internet: no key → 401, Muse's key → 200 (0.2 s), top-5
  faceless opportunities from Oracle's library = the same list (Wild Secrets …, 54 s cold), a
  write tool → 403. The laptop's Muse link stayed WORKING throughout. Switch-over TIME not yet
  chosen by the operator.
  THE SWITCH-OVER (operator: "just go", 2026-09-29 ~15:10). Laptop side, in this order:
  `install_oracle_access.ps1` → task **BeaconOracleAccess** (every 5 min, runs the engine's
  standalone oci_allowlist.py): BeaconEdgeSync used to refresh Oracle's SSH /32 before each sync,
  so disabling it would otherwise lock the laptop out of its own server at the next IP change.
  Then BeaconEdgeSync DISABLED (after its running round finished: inbox 0, backlog 0) →
  BeaconRadarAutostart DISABLED → stop_worker; the organizer had been in ONE cycle since 11:59
  (212 channels "running") and did not exit in 9 min, so both worker pids were force-killed and
  the 9 half-done batches handed back to 'queued' with the queue's own expiry statement (else
  Oracle would wait for the 6 h lease: ~18:00). Final pack: snapshot 264 s, 261,441 channels /
  5,033,929 videos; source quick_check with the 1 GB cache took **30 min** on the laptop (read
  ~81 GB total incl. the snapshot) and returned ok; 64 parts 8.5 GB; send 25.2 min, 0 skipped
  (every 512 MB region had changed since the morning), 0 retries.
  `open_beacon_cloud.py` + desktop shortcut **"Beacon (cloud)"** (pythonw, beacon icon): private
  `ssh -N -L 8571:127.0.0.1:8570` (reused if the screens already answer), refreshes the SSH
  allowlist once and retries on refused/timeout, opens a Chrome/Edge `--app` window, and shows a
  message box instead of failing silently. Tunnel verified: up in 3.7 s, survives the launcher
  exiting. The old "Beacon" shortcut (laptop app, frozen copy) was left untouched.
  Server side: `receive` 11 min (parts verified 2 min), counts EXACT (261,441 / 5,033,929),
  settings applied with `--set face_check_workers=2 --set catalog_workers=4`. First local cloud
  import by hand: 35 waiting → 36 bundles imported, 0 failed, 226 s, plan "unchanged" (4,346),
  acks flushed, 182 genuinely new channels by 16:59; then the timer (runs of 2, 2 … 0 errors).
  receive's db_facts had left EMPTY root-owned `.incoming-wal/-shm` beside the renamed file
  (removed; harmless, but the kit should open the incoming copy immutable=1). beacon-radar
  enabled: fast discovery + organizer; the 15 handed-back batches (257 channels) arrived.
  FOUND LIVE: "Channel enrich skipped: no YOUTUBE_API_KEY" — radar.py's enrich step reads the
  SINGLE `YOUTUBE_API_KEY` (scripter.config), Oracle's key file lists only the pool →
  `deploy/server/radar-keys` (run as root by ExecStartPre) copies only YOUTUBE lines and derives
  the single key from the pool's first when missing; verified: scraper env = YOUTUBE_API_KEY +
  YOUTUBE_API_KEYS, no signing key; log "Enriching channel stats". Tests: server_mode 22.
  SAFE SCRAPER RESTART on a server = stop → requeue 'running' organize batches (the queue's own
  expiry UPDATE) → start. `systemctl restart` alone parks them for the 6 h lease, so install.sh
  NO LONGER restarts a running beacon-radar (says "Stop, then Start it in Overview").
  Muse: beacon-gateway + beacon-tunnel enabled on Oracle; address
  https://into-enlarge-sparc-deposit.trycloudflare.com (changes only if beacon-tunnel restarts —
  install.sh never restarts it); outside check 401 / 200 / live newest channels. Laptop
  BeaconMuseLink DISABLED, its gateway + cloudflared stopped. The operator re-pastes the SAME key.
  Server wording: `ON_SERVER` in beacon_app ("This server", "Starts with the server" switch
  disabled, "(this server + the cloud scout)", restart-not-laptop queue text) — verified in the
  "Beacon (cloud)" window. Laptop tasks now: BeaconEdgeSync / BeaconRadarAutostart /
  BeaconMuseLink DISABLED (reversible), BeaconOracleAccess Ready.
  WATCH: Oracle WAL 1.43 GB after the catch-up import; the importer's truncate attempts report
  busy (readers active); load ~5-8 on 2 cores (mostly disk wait), 9 GB RAM free, disk 65 GB free.
  Keep for now, delete after a good day: Oracle youtube_index.sqlite3.previous (this morning's
  copy, 34 GB) + ~/beacon-incoming/lib-20260929; laptop D:\Youtube\beacon_move\lib-20260929 and
  lib-cutover (8.5 GB each).
  PUBLIC WEB ADDRESS + MUSE AS THE AGENT (2026-09-29 evening; operator chose "free, no account"
  and "same powers as Beacon's agent"; "the agent is Muse" = no separate AI on the server).
  (a) `muse_powers.py`: with `BEACON_GATEWAY_ACCESS=agent` (set ONLY in Oracle's
  beacon_host.env; default "read") the door runs every agent tool through
  `BeaconAgent.execute` (headless, FinderClient with the FULL key from finder.tokens.json) —
  same faceless gate, query policy, locked config edits. UI tools (show_channels/open_view/
  clear_channel_filters) are not offered. Every change → `storage/muse_changes.jsonl` with the
  exact undo: exclude_by_filter's reason is tagged `muse:<change_id>: …` and its channel ids read
  back from channel_exclusions (the tool only returns a count); junk filter = before/after diff;
  topics/config capture the old values; queries undo only the accepted ones. New tools
  `recent_changes` / `undo_change` (undo recorded too, refused twice). `/guide` + MCP
  `instructions` carry the agent's rules ("✓ What I did" with change ids). 48 tools live.
  Verified over the internet: scraper_status (systemd worker) running; hid Lost History →
  History <5K count 732→731 → undo → 732; double undo 422; no-filter bulk hide 422 with the
  finder's message at the top level; open_view 403 "not available on this connection".
  Tests: test_muse_powers 14, gateway 26. GOTCHA fixed: do_GET logged AFTER answering, so a
  fast client could read the log before the line existed (flaky caretaker test) — now logs first.
  (b) https://51.170.132.206 — Caddy 2.11.4 (GitHub .deb, sha256 = release digest AND sha512 =
  checksums file) with a Let's Encrypt 6-DAY IP CERTIFICATE (`issuer acme { profile shortlived }`,
  http-01 from 4 vantage points, auto-renewed). OCI security list: +TCP 80/443 from anywhere via
  the updater's own client/ETag (scratch open_web_ports.py; SSH stays the laptop /32); host
  iptables: 80/443 inserted before the REJECT, saved with netfilter-persistent. Template
  `deploy/server/caddy/Caddyfile.template` + `render_caddyfile.py` (validates as the caddy user,
  keeps Caddyfile.previous). `/muse/*` → door (own key, NO password prompt) = a PERMANENT Muse
  address; everything else → basic_auth (user `beacon`) → Beacon's screens. Password: 16 chars
  lowercase+digits in groups (phone-friendly), plaintext ONLY in laptop
  storage/beacon_web_login.txt (gitignored), bcrypt fed to `caddy hash-password` on stdin.
  THREE LIVE GOTCHAS: (1) `caddy validate` as root created the access log root-owned 0600 →
  reload failed; (2) bare-IP visits send NO SNI → "TLS internal error" until `default_sni`;
  (3) Windows Python `print` piped the password with CRLF, so the hash was of "pw\r" and the
  right password got 401 — write bytes with a bare "\n". Verified from outside: trusted LE
  cert, no/wrong password 401, right password 200 (Beacon page), websocket /ws 101 only with
  the password, http→308 https, /muse/health 200 with Muse's key.
  MUSE'S CONNECTOR REFUSES BOTH a bare IP ("only accepts a public domain name") AND wildcard-DNS
  names (51-170-132-206.sslip.io / nip.io: "only a regular registered domain") — though Caddy
  serves both with valid LE certs (`--name` in render_caddyfile.py; @SITES@ in the template).
  It DOES accept *.trycloudflare.com, so Muse stays on Oracle's quick tunnel
  https://into-enlarge-sparc-deposit.trycloudflare.com (beacon-tunnel — do NOT retire it; the
  installer never restarts it). Operator pasted the key via Muse's Connect card + "Always
  allow". Verified 2026-09-29 18:58-19:01: door log GET /guide 200 + /health 200 with the key;
  Muse reported "access agent, 48 tools", then called recent_changes and described the test
  trail exactly (1 exclude + undo, 2 refused no-filter bulk hides). A truly permanent Muse
  address needs a registered domain (~$10/yr) → Caddy `--name`, A record to the IP.
  FULL REVIEW (operator asked "review and test everything", 2026-09-29 19:03-19:15):
  OK — 9 services active+enabled, 0 restarts; 416 new channels since 15:14 (99 in the last hour);
  88 collector bundles imported, the 4 that hit "database is locked" (17:24-17:30, scraper and
  importer writing at once) all imported on retry (archived, none quarantined); organizer not
  stuck but slow on Oracle's disk (whole-index steps 15-35 min each, a cycle ≈ 2 h vs ≈ 30 min on
  the laptop); 146 front-door 401s in 90 min were generic scanners (l9scan, libredtail) probing
  /index.php, /console, /server-status — NONE sent a password; all six screens clicked through
  in the "Beacon (cloud)" window; Muse used recent_changes correctly.
  FIXED IN REVIEW: (1) only beacon-radar had YouTube keys — the finder, screens and door had none,
  so Saved "Refresh live" silently refreshed nothing (while stamping "checked just now") and
  saving a channel could not enrich; `radar-keys` → `deploy/server/youtube-keys`, used by all
  four units; verified live: Dark Wild Archives 21→24 uploads, Bertha's House Hacks 81K→93.3K
  subs after reload. (2) install.sh kept "the 3 newest releases" while NOT restarting the
  scraper, so the next deploy would have deleted the release the scraper runs from (its later
  lazy imports would fail mid-cycle); pruning now skips any release a running beacon service's
  /proc/PID/cwd points into — verified "keeping 20260929T170152Z — a running service still uses
  it". (3) stale "start only at the switch-over" installer message.
  OPEN FINDINGS: the server Agent tab shows a red "Couldn't load this — Foundry needs …" (no AI
  on the server by design: Muse is the agent) → should say so plainly; Overview "Index snapshot"
  takes ~6 min on the server (db_stats; the door's background count took 364 s) → reuse a
  background count; NO BACKUP of the live library (only copies: Oracle's live file, its
  .previous from the morning on the same disk, the laptop's frozen 15:24 copy); Oracle wants a
  reboot for OS updates, and any reboot changes Muse's quick-tunnel address (key re-paste) — a
  registered domain removes that; today's work is not committed/pushed.
  "NICER" FIXES (operator: "do your nicer fixes now", 2026-09-29 19:20-19:45):
  (1) AGENT TAB WITH AN OUTSIDE AGENT: `BEACON_OUTSIDE_AGENT=Muse` + `BEACON_OUTSIDE_AGENT_URL=
  https://muse.ai` (Oracle's beacon_host.env only; empty = laptop behaviour unchanged). The tab
  then opens with "Your agent is Muse" (last visit from the door's log via
  `muse_powers.last_outside_visit`, which skips REFUSED/start-up/db_stats lines), an Open Muse
  button, and "Changes made by Muse" — `muse_powers.describe()` plain sentences ("Hid 1 channel",
  "Tried to … — refused, nothing changed", "Undo: …") with an Undo button per undoable change
  (runs `MusePowers.undo` in-app through the agent's own dispatcher; the undo is recorded in the
  same muse_changes.jsonl, so the door sees it too) and an "Undone" pill afterwards. No red
  "needs setup" card; the Ask box is replaced by "Set up an AI inside Beacon (optional)" (the
  provider picker appears only after clicking it) — a usable-looking box that could not work
  plus a red line read as broken. Verified live: hid "Nibble app" through Muse's door (Tech & AI
  <3K subs 469→468), clicked Undo in the Agent tab → row "Undone" within ~1 s, count 469.
  (2) INSTANT INDEX SNAPSHOT: `index_stats.py` — ONE shared count in storage/index_stats.json
  used by the Overview (`_index_stats`, shows "counted N min ago") and the door's StatsCache
  (adopts the shared count, so a restarted door answers at once). Stale after
  `BEACON_INDEX_STATS_REFRESH` (3600 s) → quiet background recount; a lock file (O_EXCL, stale
  after 1 h) guarantees one count at a time across processes; only the very first count ever
  makes anyone wait. Tests: test_index_stats 7, gateway +2.
  ALSO FIXED: beacon_host.example.env had become CRLF (a Python write_text of mine on Windows) —
  a NEW server would have received "TZ=…\r"-style settings; now LF, covered by the line-ending
  test, and install.sh also normalises *.env. Oracle's own settings file was verified clean.
  WEB ICON + DESKTOP ICON (operator, 2026-09-29 20:00: "why does it open as a desktop app" and
  "the Beacon icon disappeared, it's Flet's low-quality default"): the "Beacon (cloud)" window
  was Chrome `--app` (no address bar) — the shortcut now runs chrome.exe https://51.170.132.206,
  a normal tab (Chrome had the login saved, no prompt). Flet web serves index/manifest/icons
  from the app's assets dir before its own web dir (FletStaticFiles.all_directories order;
  assets resolved next to beacon_app.py), and Beacon had only beacon_icon.ico there → Flet's
  logo. `tools_make_icons.py` redraws the lighthouse (measured from the 256 px .ico, 8x
  supersampled; beams clipped to the tile, rings solid) → assets/favicon.png (32),
  icons/icon-192/512, icon-maskable-192/512 (full-bleed, 78% safe zone),
  apple-touch-icon-192, loading-animation (512, the splash), beacon_icon.ico (16-256, replaces
  the old one — original in git history), beacon_icon_512.png; assets/manifest.json names the
  web app "Beacon". GOTCHA: `.gitignore` ignores *.png (screenshots) → the icons would never
  reach the server; `!assets/*.png` + `!assets/icons/*.png`. Verified from outside: favicon,
  icon-512 and loading-animation served byte-identical to ours; manifest short_name Beacon.
  Tests: WebIconTests (sizes, not git-ignored, name).
  STILL FLET'S LOGO in the operator's Chrome after that — NOT the favicon cache: Flet's web
  client installs flutter_service_worker.js, which answers every file in its RESOURCES list
  (favicon.png, manifest.json, icons/*.png) CACHE-FIRST from its own old copies, forever (its
  script never changes, so it never updates; "?v=" is stripped from keys). Files it does not
  list go to the network, and "/" is online-first. Fix: tools_make_icons.py also writes
  Beacon-only names (beacon-favicon.png, icons/beacon-*.png, beacon-manifest.json) and
  assets/index.html = Flet's installed index.html with those names + title/description/
  apple title "Beacon" (FletStaticFiles copies assets/index.html and still patches it).
  `beacon_index_html()` asserts every replaced string exists, and a test compares the file
  with a fresh render of the installed Flet page, so a Flet upgrade cannot drift silently.
  Verified in the operator's Chrome: a newly opened tab shows the lighthouse; a tab opened
  before the fix shows Flet's logo until it is reloaded once.
  AI FACELESS REVIEWER (operator 2026-09-29: "make Muse or another AI we choose responsible for
  this", after seeing "faceless verified 21,422"). That stat = channel_vlm_check verdict
  'faceless' only — the paid Qwen2.5-VL step, frozen since 2026-07-09. Live coverage of 255,900
  visible channels: AI vision 25,459; face check sure 45,332; face check UNSURE 55,919 (55,485
  with 700+ subs); text judge only 129,125 (just 2,601 with 700+ subs); nothing 91. The engine
  already ranks an AI verdict first (channel_metrics CASE: vlm > face check > text > keywords;
  'unclear' falls through) and stores it with `store.save_vlm_verdicts` — so NO engine change.
  `faceless_review.py`: review_queue = find_channels(faceless+replicable, opportunity order,
  scanning 300→1000→3000, cached 10 min) filtered to weak labels (read-only look-up of vlm/face/
  text), skipping AI-decided and reviewer-'unclear' (except review:undo), each with link + top
  6 videos (title, thumbnail; NOTE the videos column is `views`, not view_count); save_verdicts
  → engine store, model "review:<who>". Agent tools `faceless_review_queue` /
  `set_faceless_verdict` (beacon_agent, so ANY AI in the Agent tab can do it too); Muse's door
  forces reviewer = BEACON_OUTSIDE_AGENT ("muse"), records the change, undo restores the prior
  AI verdict (none → 'unclear' via review:undo, which puts it back in the queue); describe()
  "Checked faceless: 2 faceless, 1 unclear". Tests: test_faceless_review 12 (on an engine-built
  scratch DB). Door: 50 tools. LIVE: queue answered in 4 s — Beacon's top 3 opportunities were
  all "face check unsure"; Muse re-read /guide, called the queue, used inspect_channel_visuals +
  get_channel_details, then change 1795bac99b: Wild Secrets faceless, Next Wonder Places
  faceless (thumbnails: animal/AI footage, hands only), Mind Fragments UNCLEAR — Muse found the
  channel apparently DELETED (videos "Not Found", RSS 404). Saved as review:muse, undoable.
  DEDICATED MUSE REVIEWER (operator: "make Muse run always, a dedicated agent for only this"):
  the queue never runs dry — after Beacon's ranked list (scans 300→1000→3000→5000, the finder's
  cap) `_deep()` offers every other channel shown as faceless on shaky evidence, 700 to
  2,000,000 subs (MEGA_SUBSCRIBERS: ranked by raw views the first picks were Rihanna and Kanye
  West), ordered by views per subscriber; `remaining()` = the backlog (35,336 on 09-29), shown
  as left_to_review_estimate. SQL GOTCHA fixed before it shipped: `NOT (v.verdict = 'unclear'
  AND …)` is NULL for channels with no AI row, silently dropping every never-reviewed channel —
  COALESCE both sides. Tests: test_faceless_review 18. Muse side chat "This side chat is your
  dedicated job…" (thread 175f8ee8…) created two Muse scheduled tasks: "Beacon faceless review
  (hourly)" — 10 channels/hour — and "Beacon faceless daily summary (21:00)". First run 21:08-
  21:10: change a9001e0983, 9 faceless / 1 unclear ("Nibble app"), ~35.3k left; it flagged junk
  (Roblox Legends, a music re-upload channel) without touching it, as told. At 10/hour ≈ 240/day
  the backlog takes months; the queue max is 25/run if Muse's plan allows more.
  FLAKY-TEST NOTE: 1-2 intermittent failures appeared twice in the full suite and never again
  after test_faceless_review stopped popping YOUTUBE_INDEX_DB (mock.patch.dict now); a deploy
  had gone out after a failing run — check the suite result BEFORE deploying.
  NEXT: fail2ban for repeated 401s (bcrypt per guess on 2 cores); a phone layout (screens need
  ≥1040 px); a server-side notice if the quick tunnel's address ever changes; detect deleted
  channels (Mind Fragments) so they drop out of the opportunity lists.
  STILL TO DO after the switch-over: the
  Production Lab on the laptop reads the laptop's library, which freezes once Oracle is the writer.
  Full suite after this: 292 passed + 36 subtests. Release on Oracle: 20260929T141011Z.
  GOTCHA (Git Bash → ssh.exe): an argument that starts with `P=/var/...` is rewritten to a
  Windows path (`C:/Program Files/Git/var/...`) — export MSYS_NO_PATHCONV=1 first.
  GOTCHA: quoting Python f-strings with \" inside a heredoc inside ssh '…' fails to parse —
  scp a script file instead (did this twice).
  GOTCHA: `pkill -f` with a pattern that appears in its own ssh command line kills the session
  — use the `[x]` bracket trick, and match what the cmdline really contains (relative paths).
  NOT DONE: write access for Muse (operator chose read-only first); a permanent address
  (named tunnel — needs a free account the operator creates); Muse's door on Oracle so it
  can answer while the laptop sleeps (planned next — Oracle holds no library, by design).

## The finder tool (how Beacon talks to the engine)
- The finder = `IntelligenceAnalystTools` (~35 commands) in `scripter/intelligence/analyst_tools.py`,
  served over localhost HTTP by `scripter/intelligence/local_api.py`.
- Beacon calls it via `scripter/intelligence/finder_client.py` → `FinderClient` with the FULL key.
- Two keys: **FULL** (read+write, Beacon uses this) and **READ** (read-only, the Lab uses this). The
  service has 22 read tools + 14 write tools; the read key can't call write tools. (Read tools now
  include `recent_channels` — newest-DISCOVERED channels for the Real Time feed.)

## Run / test
- Open the app: `D:\Youtube\Beacon\.venv\Scripts\python.exe D:\Youtube\Beacon\beacon_app.py`
  (or double-click `D:\Youtube\Open Beacon.bat`). Uses the Lab's venv (has flet, requests, etc.).
- Boot takes ~25s. Window title matches `*Niche Finder*`.

## Current state (2026-07-09)
- Built + working live (verified by launching + clicking + screenshotting): Overview (live index stats
  + background-scraper Start/Stop card), Channels (clean toolbar + Filters modal + niche filter +
  combinable filters + dashboard-style stat-tile/meter cards), Real Time (live feed of newest-discovered
  channels, window toggle, "added Xm ago" badges, 60s auto-refresh — via the `recent_channels` tool),
  Find (deep-sweep a niche + load faceless channels). The old Niches tab and per-card ★ tracking were removed.
- Verification harness: `_win.ps1` (DPI-aware) focuses the window, optionally clicks (fractional coords
  of the window rect) / mouse-wheel scrolls, and screenshots the full screen. Launch an isolated
  instance with `FINDER_PORT=8799 FINDER_TOKEN_FILE=<nonexistent.json>` (forces self-host, no collision).
- NOT yet ported from the Lab's old finder page (good next tasks): operations, workspaces, per-niche
  outlier modes, video/run browsers, language filters, and a per-card "Most Popular Videos" strip
  (needs `get_channel_details` per channel — deferred to keep the grid fast).

## Operational state (2026-09-11) — READ THIS BEFORE TRUSTING ANY "IT'S RUNNING" SIGNAL
- **Index**: ~199,500 channels / 3.85M videos / 24 GB / 11 workspaces. (The older "~55k channels,
  ~10 GB, 61k-channel index" figures elsewhere in these docs are historical, not current.)
- UPDATE 2026-09-19: the cloud lane is WORKING again — `sync_status.json` shows a 15:18 import of
  2 bundles / 1,493 channels, 0 errors, 0 remote backlog (repaired outside this chat). The note
  below is the 09-11 history. Still judge it by new `first_seen_at` rows, not by the task result.
- **The Oracle cloud lane is DOWN.** Last import 2026-09-01; new channels were ZERO 09-03→09-10.
  Three independent breaks, found 2026-09-11:
  1. `Quill/scripts/run_edge_sync_hidden.vbs` was deleted from the working tree (tracked in git);
  2. `Quill/storage/oci_updater_venv/Scripts/` is gone and the shared venv has no `oci` module, so
     the SSH-allowlist refresh cannot run and `run_edge_sync.ps1` throws before syncing at all;
  3. the Oracle host answers neither ICMP nor TCP/22, and the OCI free trial ended 2026-09-10, so
     non-Always-Free resources may have been reclaimed.
  A batch of git-tracked files vanished together (`run_edge_sync_hidden.vbs`, `scripter/llm/bedrock.py`,
  `scripter/research/lightning_vlm.py`, `scripts/vlm_face_check.py`, plus that venv's `Scripts/`).
  Nobody edits a file by deleting a venv's interpreter — suspect antivirus/cleanup, not a commit.
- **`harvest_enabled` is back to `true`** (was `false` while Oracle supplied discovery). The local
  radar must be able to find channels on its own; a cloud outage must never mean zero discovery.
  Both radar lanes call `load_config()` every cycle, so config edits need no worker restart.
- **The agent now runs on GLM-5.2 via Foundry, not Bedrock** (switched 2026-09-12).
  SUPERSEDED 2026-09-16: the Azure account was lost (Foundry Test = HTTP 401). Which model answers
  is now a setting — see `beacon_ai.py` in Files here. The notes below describe the old wiring. Bedrock died
  with `UnrecognizedClientException: The security token included in the request is invalid` — an AWS
  *credential* rejection, not a model problem, so changing `SIGNAL_BEDROCK_MODEL` could never fix it.
  `beacon_agent.py` now has TWO interchangeable backends chosen by `BEACON_AGENT_PROVIDER`:
  `foundry` (default) and `bedrock` (kept intact — a new AWS key switches straight back).
  The Foundry pair is the SAME one Quill uses: **GLM-5.2** (`zai-org--glm-52-fp8`) for thinking and
  tool calling, **Qwen3-VL** (`qwen--qwen3-vl-30b-a3b-instruct-fp8`) for images, on two different
  resources because the best writer cannot read images. Verified live: the GLM deployment returns
  real OpenAI `tool_calls`, so this is a native tool-use loop, not prompt scraping.
  `TOOLS` stays in Bedrock toolSpec shape as the ONE definition; `_openai_tools` translates.
  GLM is a reasoning model: its scratchpad arrives inline (stripped by `_strip_reasoning`) and is
  billed against the ANSWER's budget, so `max_tokens` must stay generous or it thinks until empty.
  Keys live in Beacon's own `.env` as `BEACON_FOUNDRY_*`, falling back to Quill's `AZURE_FOUNDRY_*`.
- **The Agent tab no longer blocks on background work.** `_agent_send` used to return early on the
  global `_busy` flag, which ANY background task sets — including the 3-second cloud-import poller —
  so during an import every question was silently discarded, making "Ask Beacon" look dead. It now
  gates on `_agent_running` (one turn at a time) and says so when it refuses. The input also keeps a
  `_agent_draft` across re-renders, because `_render()` rebuilds every control and background ticks
  were wiping half-typed questions. Scroll position is preserved the same way: `_render()` replaces
  every control (sending the view to the top), and `_ensure`'s completion re-renders with NO section
  guard — so any background load anywhere yanked a half-read answer back to the top. `_on_scroll`
  records the offset and `_restore_scroll` puts it back after each render, but only within the same
  section (`_go` zeroes it, so switching tabs still starts at the top). Do NOT "fix" this by making
  `_ensure` skip the render on the Agent tab — the Agent Work ledger reads `_ensure` keys
  (niche signals + `edge_sync_status`), so suppressing it there would freeze those cards.
- **A green `BeaconEdgeSync` task no longer means data moved.** `edge_sync_coordinated.py` now
  returns the real exit code and logs the reason to `storage/edge_sync_coordinated.log`. Before
  2026-09-11 it swallowed launch failures and returned 0 forever — that is how #1 above hid for
  ten days. Judge cloud health by `storage/edge_sync/sync_status.json` mtime and by
  `channels.first_seen_at` row growth, never by the task's result code.
- **A sync that dies mid-import no longer strands the scraper paused** (fixed 2026-09-14).
  `stop_worker` writes a PERSISTENT pause marker that only an explicit Start clears — correct,
  so the keep-alive can never override a real Stop, but it made a pause WE created look
  identical to one the operator created. On 2026-09-13 the laptop slept ~1 min into a sync
  (last log 21:29:56, marker written 21:31:02, then 22.5h of silence); every later run read
  "already stopped", assumed the operator meant it, and walked past. Local scraping stayed off
  for 23.8 hours (`radar.log` gap 09-13 21:30 → 09-14 21:21). The cloud lane covered discovery,
  so the index still grew — which is exactly why nobody noticed.
  `edge_sync_coordinated.py` now writes `storage/edge_sync_pause_owner.json` (`_claim_pause`,
  BEFORE `stop_worker` so a death between the two is still attributable), deletes it only after
  a successful resume, and calls `_recover_orphaned_pause()` at the top of EVERY run: owner file
  present + that pid gone ⇒ our orphan ⇒ resume. No owner file ⇒ the operator's Stop ⇒ untouched.
  It deliberately does NOT require the worker to be fully stopped (it exits only after its
  current step, and gating on that would skip recovery during the shutdown window).
  Verified live by reproducing the exact failure: claim with a dead pid + pause flag → the next
  run logged "resuming the scraper — it was left paused by a sync that died" and cleared all
  three markers.
  The same run also holds off IDLE sleep for the whole pause→import→resume window
  (`_keep_awake`, `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)`). ES_DISPLAY_REQUIRED
  is deliberately NOT set, so the screen still sleeps. LIMIT: this only stops *idle* sleep —
  closing the lid, choosing Sleep, or a flat battery still cut the import, because Windows always
  honours an explicit human instruction and overriding it would mean editing the operator's power
  settings. So pause recovery remains essential, not redundant. The flag is per-process and released
  on exit, so a killed sync can never leave the laptop unable to sleep. Verified from the OS by
  reading the API's previous-state return: bit off → ON inside the block → off after.
  TEST GOTCHA: `test_beacon_pause_recovery.py` MUST patch `esc.LOG_FILE` as well as `esc.OWNER_PATH`.
  Patching only the latter made the suite write real-looking "resuming the scraper" lines into the
  production log — fabricating incidents that never happened, in the one log this project relies on
  to tell the truth. Ten such lines were written and then removed.

- **Broadcasters no longer raise alerts** (fixed 2026-09-15). National Geographic (26.4M subs)
  sat in 8 workspaces generating **525 alerts**, and the feed's top entries were NatGeo full
  episodes filed under "Traditional Self-Sufficiency & Frugal Living". Three causes, all fixed:
  1. `junk_filter._BRAND_WORDS` had 16 entries and no broadcasters. Added NatGeo/BBC/Discovery/
     PBS/CNN/Vice/Bloomberg/etc. Whole-word matching, so "Geography Now" and "Geographics"
     are correctly KEPT — verified against both lists before applying.
  2. `detect_alerts` read the workspace feed and **never consulted `channel_exclusions`**, even
     though exclusions are documented as "hidden from every view". A channel the junk filter had
     already thrown out kept notifying forever. It now filters excluded channels out.
  3. Alerts raised BEFORE an exclusion stayed unread forever, so excluding a channel did not
     clean up after itself. `_retire_excluded_alerts` deletes (not marks-read — these were never
     seen and dismissed, they should never have existed) that niche's video alerts whose channel
     is excluded. Un-excluding the channel lets them be raised again; no channel/video row is
     touched. Use **EXISTS, not IN**: the IN form materialises every video of every excluded
     channel (millions of rows) — measured minutes vs **2.4s for 5,141 alerts**.
  Live result: 31,854 → 24,555 alerts, 0 National Geographic alerts left, exclusions 3,920 → 4,911.
  NOTE a name blocklist is whack-a-mole. The robust version is a subscriber ceiling (a 26M-sub
  broadcaster is never a replicable format) — deliberately NOT added, because tracking a large
  competitor in a workspace can be intentional and that is the operator's call.

## Rules
- Beacon and the Lab are independent; they share ONLY the database. Don't add cross-app coupling.
- Keep Beacon driving the finder through `FinderClient` — do NOT bake scraper/DB queries into the UI.
- When you must change the shared engine (`scripter/`), flag that it affects the Lab too.
- Full background/history lives in the Scripter project's memory file `signal-lab-split.md`.
