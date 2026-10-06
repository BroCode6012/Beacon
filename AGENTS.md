# Beacon working instructions

Read `PROJECT_GUIDE.md` and `CLAUDE.md` before changing this project.

## Product

Beacon is a standalone Flet desktop control room for investigating YouTube
niches and channels. The user is non-technical. Explain behavior in plain
language before implementation detail.

The product target is strictly **faceless, replicable channel formats**. Beacon
is not intended to surface every high-performing channel. Opportunity views
should suppress music, celebrities, children’s content, major brands/networks,
one-off viral accidents, malformed records, and unstable tiny-data ratios.

Beacon owns its scraper engine and index. Quill owns its own research catalog;
no database or runtime package is shared between these applications.

## Boundaries

- Beacon owns this repository, `beacon_engine/`, its `.venv`, credentials,
  database and all worker state. Quill owns a separate repository and runtime.
- Never import another application's Python package, read its settings, or point
  defaults at its data. There is no active Beacon–Quill integration.
- Production Beacon uses PostgreSQL on Oracle. A fresh local checkout may use a
  separate empty SQLite index for development; never substitute it for production.
- Never move, copy, reset or replace a live database casually. Preserve secrets.
- Do not stop production workers or alter the watchlist unless required by the task.
- Historical entries below describing the shared engine/database are superseded
  by the standalone architecture established on October 6, 2026.

## Architecture rules

1. Import `engine_link` before importing `beacon_engine.*` from a Beacon entry point.
2. Normal Beacon UI features must use `FinderClient`.
3. Do not add direct SQLite queries to `beacon_app.py`.
4. Keep Beacon’s Bedrock agent separate from Production Lab chat/agent modules.
5. Preserve full-token versus read-token authorization.
6. Exclusions must stay reversible; do not delete channel/video rows.
7. Treat topic, niche workspace, and YouTube category as different concepts.
8. Treat discovery time and YouTube channel age as different clocks.

## Secrets and generated state

Never print, read into a response, commit, or document secret values from:

- `.env`
- AWS credentials
- YouTube API keys
- Finder tokens

Never commit:

- `storage/`
- SQLite, WAL, or SHM files
- logs
- model binaries
- screenshots
- session output
- `__pycache__`

## Current priority order

Unless the user overrides it:

1. establish Git and a safe baseline;
2. add deterministic durable Agent write auditing;
3. improve opportunity ranking and classification quality with a labeled benchmark;
4. add topic provenance/manual locks;
5. improve Agent/Autopilot UX; and
6. migrate the shared runtime to Python 3.11+ before 2026-10-04.

Completed and verified on 2026-07-11:

- concurrent background-operation accounting and status clearing;
- the `All topics` sentinel bug;
- Beacon cache TTLs, automatic Overview refresh, and write invalidation;
- shared Finder cache invalidation after writes;
- mandatory faceless + replicable opportunity filtering/ranking;
- bounded, yield-aware Autopilot query additions;
- Agent schema/error-status alignment;
- missing scikit-learn/joblib declarations; and
- Beacon/shared regression suites (25 Beacon tests; 113 shared tests).

Completed and verified on 2026-07-12:

- the shared live index was checkpointed, hash-verified, integrity-checked, and
  relocated into Beacon storage;
- the old Scripter database file and temporary legacy fallback were removed;
- radar and monitor resumed against the Beacon-owned index while their
  operational state remained in Scripter storage; and
- successful empty searches now complete first-pass query tracking while
  provider failures remain retryable;
- the durable radar can add at most two reviewed, evidence-backed queries after
  100% coverage, sharing Beacon's cap, cooldown, deduplication, and file lock;
- the live rollout reached 4,164/4,164 searches with zero remaining and correctly
  added nothing while the Autopilot cooldown was active; and
- Beacon/shared regression suites passed (37 Beacon tests plus 13 subtests; 148
  shared tests).

Completed and verified on 2026-07-13:

- Oracle-to-laptop synchronization now batches SSH transfers, reuses one
  initialized importer connection, and drains up to 100 bundles every two
  minutes through a fully windowless task that preserves real failure codes;
- the Oracle collector now performs signed deterministic topic assignment and
  `text-v3` faceless pre-classification without receiving or opening the live
  database;
- a real cloud bundle imported 895 channels, topics, and classifications with
  a signed acknowledgement and zero remaining remote bundles;
- the laptop now updates Oracle's TCP/22 ingress source to its current public
  IPv4 `/32` before sync, using an isolated OCI SDK environment so Lightning's
  dependency pins remain untouched;
- a live IP change was detected and repaired automatically, after which repeated
  scheduled syncs completed with result `0`, no transport errors, and zero
  remote backlog;
- sync now publishes a newly signed Oracle discovery plan only when the live
  watchlist or edge settings change; the current plan contains 4,206 searches,
  and custom/test sync roots cannot publish automatically unless explicitly
  enabled;
- local harvesting is disabled while Oracle supplies discovery, and local radar
  cycles prioritize the durable organization backlog while skipping unrelated
  discovery and whole-index work;
- Oracle free search now makes at most three bounded retries for transient JSON,
  timeout, connection, HTTP 429, and HTTP 503 failures; the first deployed
  bundle completed 49/50 searches after recovering 14 transient attempts and
  delivered 931 pre-organized channels; and
- Beacon/shared regression suites passed (43 Beacon tests plus 13 subtests; 205
  shared tests), with visual smoke tests of all five Beacon screens.

Completed and verified on 2026-07-14:

- Oracle organization no longer returns attached videos for channels already
  handled inside its refresh window, including video-only repeat jobs;
- repeat discovery bundles retain every signed query-run record but omit
  unchanged channel/video/classification/discovery/snapshot rows, while still
  emitting new results and changed view observations;
- the first production delta bundle examined 2,000 result observations,
  suppressed 1,444 repeats, retained 50 query runs, and emitted 481 videos plus
  336 channels; a controlled repeat bundle was 78.7% smaller;
- the laptop importer now isolates bundles with savepoints, commits groups of
  20, and writes signed acknowledgements only after commit;
- the hidden two-minute task continuously drains 100-bundle slices after wake
  until both queues are empty or its 90-minute safety budget ends;
- Oracle revision `bb567a5` is active with both discovery and organizer
  services healthy; and
- after a temporary internet interruption, sync recovered automatically,
  returned result `0`, imported 28 channels/365 videos, and left both queues
  empty. Final verification passed 232 shared tests, 43 Beacon tests plus 13
  subtests, package checks, and all five real Beacon screens.

Completed and verified later on 2026-07-14:

- the shared YouTube API pool now distributes batched channel/video requests
  concurrently across every healthy key, with a configurable 16-lane default
  ceiling and a hard safety cap for future key growth;
- exhausted, forbidden, disabled, or invalid keys are removed from the active
  process pool while pending work automatically moves to healthy keys;
- Oracle upload-playlist cataloging and category enrichment now use the same
  concurrent pool while preserving per-job quota ceilings and free-provider
  fallback;
- `scripts/add_edge_youtube_keys.ps1` adds any future number of keys through
  hidden input, deduplicates them, merges both private environments atomically,
  transmits values through SSH standard input rather than arguments, and
  reports counts only;
- immutable Oracle revision `cdea38e` is active. Six configured keys were each
  proven live in one simultaneous six-batch production check; one permanently
  forbidden legacy key was removed;
- the final hidden sync returned `0` with zero failures and zero local/remote
  backlog; and
- verification passed 240 shared tests, 43 Beacon tests plus 13 subtests,
  package checks, and repeated real-window checks of all five Beacon screens.

Completed and verified on 2026-07-17:

- post-commit Oracle acknowledgements now upload through one coalescing
  background worker in batches of up to 20 while the next import transaction
  proceeds;
- the commit-before-ack guarantee remains mandatory, final uploads are joined
  before return, and failed signed acknowledgements remain locally retryable;
- WAL maintenance now runs on a separate coalescing worker, passively
  checkpoints after committed groups, and makes bounded truncate attempts above
  128 MiB without turning database cleanup into another long batch pause;
- sync results expose WAL attempt, busy, success, error, and size diagnostics;
  and
- verification passed 252 shared tests, 55 Beacon tests plus 16 subtests,
  blocked-SSH overlap and real-WAL truncation regressions, and two isolated
  real-window passes of all five Beacon screens.

Autopilot may add scraper queries without asking each time, but only through
bounded, measured logic: yield-aware selection, normalized similarity
deduplication, maximum size, per-topic limits, and cooldowns. Do not rely on the
LLM prompt alone to enforce this.

## Verification

Verification is a HARD GATE after every atomic code change. Do not begin the
next code change until the current one passes.

For every atomic code change:

1. Parse or compile the changed Python.
2. Run focused tests.
3. Run the Beacon engine test suite when engine code changes.
4. Launch a separate real Flet app instance with Beacon's own venv and an isolated
   Finder port/token-file path so the user's open app is not disturbed.
5. Use `_win.ps1` to click the changed controls and verify the complete affected
   workflow, including success, empty, loading, and error states when relevant.
6. Smoke-test all five screens: Overview, Channels, Real Time, Find, and Agent.
7. Capture and visually inspect screenshots after every meaningful step.
8. Check the launched process/output for exceptions.
9. Stop and repair any regression before continuing to another code change.
10. Report exactly what was clicked and what each screenshot proved.

Do not claim a user-visible fix from headless checks alone.
Do not batch several unverified code changes and test only at the end.

## Live-process caution

On Windows, the radar can appear as a venv `pythonw.exe` launcher plus a base
Python child. That is normally one logical worker.

Do not infer a hang only from stale `radar_status.json`. Check:

- `radar.log`;
- database growth;
- process liveness; and
- current CPU activity.

The stop control is cooperative and may wait for the current long step.

## Change discipline

- Preserve unrelated user files and live state.
- Prefer small, reversible patches.
- Add tests for every reproduced defect.
- Do not silently broaden a Beacon-only task into shared-engine work.
- Update `PROJECT_GUIDE.md` when architecture, core behavior, or the work plan
  materially changes.
