# Signal — Niche Finder (standalone app)

You are working on **Signal**, a standalone YouTube niche investigator & finder. It was split out
of a bigger app (the "Production Lab") so it can be worked on alone. A SEPARATE chat handles the
Production Lab — you focus on Signal. Read this before editing anything.

The user is **non-technical**: explain in plain words (numbers/analogies) before code, and VERIFY UI
changes live (see **Verify every task** below) — never just claim it works.

## Verify every task (MANDATORY — the user insists)
For EVERY task, before saying it works: **launch the real app, click through the actual UI, and take
screenshots.** Headless checks, stubbed handlers, and logic-only tests are NOT enough on their own —
they can pass while the live UI is broken. The loop is:
1. Launch: `D:\Youtube\Scripter\.venv\Scripts\python.exe D:\Youtube\Signal\signal_app.py` (background),
   wait ~30s for boot.
2. Bring the window to front (Windows blocks focus-steal — attach-thread-input trick), then **click the
   real controls** at coordinates computed from the window rect (nav tiles, niche chips, cards, stars),
   scroll if needed.
3. **Screenshot after each meaningful step** and READ the screenshot to confirm it actually worked.
4. Only then report — show what the screenshot proved. If a click opens a browser/external side effect,
   that's fine; do it and confirm.
Headless/DB probes are still useful for *data* facts, but they never replace the live click-through.

## What Signal is
A Flet desktop app (its own window titled "Signal — Niche Finder") that DRIVES the finder tool to
discover channels, sweep niches, score outliers, and manage niche workspaces. Signal is the finder's
"control room" — it uses the finder with FULL (read + write) access.

## Architecture — three parts (important)
1. **This app** — `D:\Youtube\Signal\` — Signal's UI + its own shell. Small; edit freely here.
2. **The SHARED engine** — `D:\Youtube\Scripter\scripter\` — the discovery/scraper/DB/AI code, used by
   BOTH Signal and the Production Lab. **Editing it changes BOTH apps** — say so when a change is
   engine-level, and remember the other chat owns the Lab.
3. **ONE shared database** — `D:\Youtube\Signal\storage\youtube_index.sqlite3` (approximately
   10 GB; live counts keep growing). **NEVER move or copy it casually.** Signal and the shared
   engine open it in place in WAL mode. Production Lab reads it through its shared-index role
   and keeps its normal writable session database separate.

Signal reaches the engine + DB through `engine_link.py` (adds the Scripter repo to sys.path + sets the
`SIGNAL_INDEX_DB`/`YOUTUBE_INDEX_DB` env vars). Import `engine_link` FIRST in any entry point, before
importing `scripter.*`. Radar config/status/queue/log/model files stay under
`D:\Youtube\Scripter\storage`; moving the index must never move those operational files with it.

The optional off-laptop Oracle node is an **edge collector, not a fourth copy of
the app or database**. It receives signed/versioned query plans, keeps only a
small operational JSON cursor, and produces signed immutable result bundles.
The laptop verifies and imports those bundles into the one canonical index.
Never copy, mount, expose, or remotely open the live SQLite file for this design.

## Files here
- `signal_app.py` — the app / entry point. Screens: **Overview**, **Channels**, **Real Time**, **Find**. (The old
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
  classified faceless channels plus related harvest-yield support, and pass the shared 450-query cap,
  normalized family/similarity deduplication, 12-hour cooldown, per-topic cap, and cross-process config
  lock used by Signal Autopilot. The feature defaults OFF, so Production Lab/legacy configs keep their
  old behavior; Signal's live config explicitly enables it. Live verification on 2026-07-12 reached
  4,164/4,164 tested searches with 0 remaining and added 0 queries because the existing Autopilot
  cooldown correctly won. Regression gates: 41 Signal tests + 13 subtests; 178 shared tests; all five
  Signal screens and an isolated Production Lab profile passed real-window smoke tests.
  ENGINE NOTE 12 (shared, off-laptop edge collector): `scripter/edge/` now provides a
  transport-independent `signal-edge` v1 protocol. `plan.py` snapshots up to 10,000 normalized
  queries into signed immutable `youtube.discovery.plan.v1` files; the current live radar config
  validates at exactly 4,164 searches. `collector.py` runs bounded parallel no-quota scrapetube
  searches and, in hybrid mode, uses `YouTubePool` only for cheap batched video/channel
  enrichment. The pool retains exhausted-key state within one UTC day, resets the next day, and
  reports quota per batch. `worker.py` persists a JSON cursor, gives never-tested searches absolute
  priority, immediately runs catch-up batches, treats successful empty responses as tested, retries
  provider failures with backoff, switches repeat searches to newest-first, retires unchanged
  queries, and reserves periodic reprobe slots. It writes signed `youtube.discovery.v1` bundles;
  `importer.py` verifies checksum/HMAC, imports channels/videos/search links/snapshots/query outcomes
  in one transaction, records an `edge_import_batches` idempotency ledger, and writes signed acks.
  This is staging, not a second YouTube index. `requirements-edge.txt` intentionally excludes Flet,
  OpenCV, scikit-learn, and Bedrock so an Oracle ARM node stays lightweight. Real isolated smoke:
  one free query produced 3 videos + 3 channels, imported into a temporary DB, and acknowledged.
  `edge/sync.py` adds the laptop-side secure inbox service: non-secret status is read-authorized;
  bounded import is a full-token Finder write; accepted/duplicate bundles are archived and invalid
  bundles stay quarantined. Signal Overview now exposes refresh/manual sync controls and invalidates
  its caches after successful imports while stating that Oracle never owns the canonical database.
  `deploy/oracle-edge/` now builds an allowlisted archive from a committed revision and installs an
  immutable Python 3.11+ release behind an unprivileged, no-inbound-port, systemd-hardened service.
  Persistent jobs/outbox state stays under `/var/lib/signal-edge`; health checks and symlink rollback
  are explicit. Full verification: 178 shared tests, 41 Signal tests + 13 subtests, all five Signal
  screens, and an isolated Production Lab window. Pending: transport adapter, Oracle VM creation,
  then shadow mode before any automatic or continuous import.
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
  only advances while the background scraper is running (Signal Overview → Start, or the login launcher).
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
  Signal's scraper. Tradeoff: outlier scores refresh every 3rd cycle instead of every cycle. Verified live:
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
  (the 11 curated workspaces). Signal shows it as the Channels-tab Topic dropdown + each card's subtitle.
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
  (write:False) exposes it with find_channels-style filters, so the Signal agent answers "how many …" in
  ONE cheap exact query instead of listing + tallying (find_channels is limit-capped). Additive; Lab unaffected.
  ENGINE NOTE 10 (shared, agent write tools): new **write** finder tools back the Signal agent's edit/clean
  powers — `exclude_channels`/`unexclude_channels`/`exclude_by_filter` (bulk by criteria; refuses with no
  filter; reversible via channel_exclusions), `run_junk_filter` (the same music/meme/brand cleaner the radar
  runs), `set_channel_topic` (new `store.set_channel_topic`), `reclassify_topics` (topic_classifier.backfill),
  `create_niche` (workspaces.create_workspace). All write:True → the Lab's READ key can't call them; Signal's
  FULL key can. Additive; the Lab is unaffected. NOTE: exclusions are reversible (hidden, never deleted) —
  that reversibility + the agent's "✓ What I did" report are the safety model for its autonomy.
- `engine_link.py` — bridge to the shared engine + DB.
- `finder_service.py` — runs the finder as a local HTTP service. Signal self-hosts this on boot if none
  is already running (Overview shows "finder: self-hosted").
- `sweep_control.py` — Signal-only control for the shared background scraper. REUSES the engine's
  existing radar worker (does NOT run its own): Start launches `python -m scripter.cli radar-worker`
  detached (cwd=Scripter root); Stop writes the `radar_stop.flag`; status is read straight from
  `storage/radar_status.json`; niches/queries are the `watchlist` in `storage/scraper_watchlist.json`.
  All paths absolute, anchored to `dirname(YOUTUBE_INDEX_DB)`. Overview has the Start/Stop + editor card.
- `signal_agent.py` — Signal's OWN AI control agent (the **Agent** tab). A self-contained AWS Bedrock
  (Claude **Opus 4.6** = `global.anthropic.claude-opus-4-6-v1`; 4.8/4.7 are access-denied on this AWS
  account) tool-use loop via boto3 `converse`. DELIBERATELY SEPARATE from the Lab: imports NOTHING from
  `scripter.chat`/`scripter.agent_tools`/`scripter.llm` — its only engine links are Signal's `FinderClient`
  (read+write finder) + `sweep_control` (start/stop scraper), the same bridges the app already uses. ~26
  curated tools — **read** (db_stats, find_channels, count_channels, list_topics, list_niches,
  recent_channels, find_rising_videos, get_channel_details, get_niche_channels, discover_channels,
  radar_status) + **write/edit** (eliminate: exclude_channels, exclude_by_filter, run_junk_filter,
  unexclude_channels; categorize: reclassify_topics, set_channel_topic; organize: create_niche,
  add_channel_to_niche; control scraper: crawl_niche, start/stop scraper, get/add/remove watchlist
  queries, set_scraper_config; control the APP's VIEWS: show_channels (applies Channels-tab filters +
  switches to it so the user SEES it), open_view, clear_channel_filters). Finder tools forward to
  `FinderClient.call(name, **args)`; Signal-local ones hit `sweep_control`/edit the config JSON; view tools
  call `ui.agent_show_channels`/`agent_open_view`/`agent_clear_channels` on the app (passed as
  `SignalAgent(.., ui=self)`) which set `ch_filters`/`ch_topic`/`ch_niche`/`section` + `_render`. It works AUTONOMOUSLY within a task (multi-step, no mid-task
  prompts) but is UNDER CONTROL by TRANSPARENCY — everything it excludes is REVERSIBLE (unexclude), and it
  ends any state-changing turn with a "✓ What I did" report (action, count, how to undo). `SignalAgent.run`
  loops (cap `MAX_TOOL_ROUNDS=18`), streaming `text`/`tool`
  events to the UI. Config in **Signal's OWN `.env`** (`D:\Youtube\Signal\.env`, gitignore-equivalent since
  Signal isn't a repo): `AWS_REGION`/`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (mirror the engine's .env —
  same AWS account) + `SIGNAL_BEDROCK_MODEL`. `signal_app.py` has the **Agent** nav tab (`_render_agent`):
  a chat box (input pinned at top so it's always reachable) + transcript with tool-call chips; sends via
  `run_bg` streaming. Verified live: real Opus 4.6 answered "how many faceless History channels under 5k
  subs" → called `count_channels` → **2,290** (exact, matches the DB).
- `signal_autopilot.py` — OPTIONAL background maintenance loop (**OFF by default**), an "Autopilot" card in
  the Agent tab. When enabled it wakes every N min (default 30) and runs a **restricted** `SignalAgent`
  (`allowed_tools=SAFE_TOOLS` — only reclassify_topics / run_junk_filter / add_watchlist_queries + read
  tools; destructive tools like exclude_by_filter/set_channel_topic/create_niche/start-stop are BLOCKED at
  both the tool-list AND `_dispatch` layers) with a fixed MAINTENANCE_GOAL prompt (categorize new channels,
  hide new junk, check scraper, brief on what's new, sparingly top up ≤2-3 queries). Each pass appends a
  timestamped entry {at, tools, summary} to `_autopilot.json` (state: enabled/interval/log[≤50]) shown in the
  card's "Recent activity". Runs INSIDE the Signal app (stops when Signal closes — the durable radar handles
  discovery regardless). `SignalApp.autopilot` is created on boot + auto-starts only if previously enabled;
  card has Off/On + interval + **Run now**. Verified live: Run now → real pass (reclassify+junk+reads) →
  logged report. NOTE: the loop calls Bedrock each wake (cost); it's off until the user turns it on.
- `check_engine.py` — headless sanity check that reads the shared DB.

## The finder tool (how Signal talks to the engine)
- The finder = `IntelligenceAnalystTools` (~35 commands) in `scripter/intelligence/analyst_tools.py`,
  served over localhost HTTP by `scripter/intelligence/local_api.py`.
- Signal calls it via `scripter/intelligence/finder_client.py` → `FinderClient` with the FULL key.
- Two keys: **FULL** (read+write, Signal uses this) and **READ** (read-only, the Lab uses this). The
  service has 22 read tools + 14 write tools; the read key can't call write tools. (Read tools now
  include `recent_channels` — newest-DISCOVERED channels for the Real Time feed.)

## Run / test
- Open the app: `D:\Youtube\Scripter\.venv\Scripts\python.exe D:\Youtube\Signal\signal_app.py`
  (or double-click `D:\Youtube\Open Signal.bat`). Uses the Lab's venv (has flet, requests, etc.).
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

## Rules
- Signal and the Lab are independent; they share ONLY the database. Don't add cross-app coupling.
- Keep Signal driving the finder through `FinderClient` — do NOT bake scraper/DB queries into the UI.
- When you must change the shared engine (`scripter/`), flag that it affects the Lab too.
- Full background/history lives in the Scripter project's memory file `signal-lab-split.md`.
