# Beacon — Niche Finder (standalone app)

You are working on **Beacon**, a standalone YouTube niche investigator & finder. It was split out
of a bigger app (the "Production Lab") so it can be worked on alone. A SEPARATE chat handles the
Production Lab — you focus on Beacon. Read this before editing anything.

The user is **non-technical**: explain in plain words (numbers/analogies) before code, and VERIFY UI
changes live (see **Verify every task** below) — never just claim it works.

## Verify every task (MANDATORY — the user insists)
For EVERY task, before saying it works: **launch the real app, click through the actual UI, and take
screenshots.** Headless checks, stubbed handlers, and logic-only tests are NOT enough on their own —
they can pass while the live UI is broken. The loop is:
1. Launch: `D:\Youtube\Quill\.venv\Scripts\python.exe D:\Youtube\Beacon\beacon_app.py` (background),
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

## Architecture — three parts (important)
1. **This app** — `D:\Youtube\Beacon\` — Beacon's UI + its own shell. Small; edit freely here.
2. **The SHARED engine** — `D:\Youtube\Quill\scripter\` — the discovery/scraper/DB/AI code, used by
   BOTH Beacon and the Production Lab. **Editing it changes BOTH apps** — say so when a change is
   engine-level, and remember the other chat owns the Lab.
3. **ONE shared database** — `D:\Youtube\Beacon\storage\youtube_index.sqlite3` (approximately
   24 GB as of 2026-09-11 — ~199,500 channels / 3.85M videos; it keeps growing). **NEVER move or
   copy it casually.** Beacon and the shared
   engine open it in place in WAL mode. Production Lab reads it through its shared-index role
   and keeps its normal writable session database separate.

Beacon reaches the engine + DB through `engine_link.py` (adds the Scripter repo to sys.path + sets the
`SIGNAL_INDEX_DB`/`YOUTUBE_INDEX_DB` env vars). Import `engine_link` FIRST in any entry point, before
importing `scripter.*`. Radar config/status/queue/log/model files stay under
`D:\Youtube\Quill\storage`; moving the index must never move those operational files with it.

The optional off-laptop Oracle node is an **edge collector, not a fourth copy of
the app or database**. It receives signed/versioned query plans, keeps only a
small operational JSON cursor, and produces signed immutable result bundles.
The laptop verifies and imports those bundles into the one canonical index.
Never copy, mount, expose, or remotely open the live SQLite file for this design.

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

## The finder tool (how Beacon talks to the engine)
- The finder = `IntelligenceAnalystTools` (~35 commands) in `scripter/intelligence/analyst_tools.py`,
  served over localhost HTTP by `scripter/intelligence/local_api.py`.
- Beacon calls it via `scripter/intelligence/finder_client.py` → `FinderClient` with the FULL key.
- Two keys: **FULL** (read+write, Beacon uses this) and **READ** (read-only, the Lab uses this). The
  service has 22 read tools + 14 write tools; the read key can't call write tools. (Read tools now
  include `recent_channels` — newest-DISCOVERED channels for the Real Time feed.)

## Run / test
- Open the app: `D:\Youtube\Quill\.venv\Scripts\python.exe D:\Youtube\Beacon\beacon_app.py`
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
