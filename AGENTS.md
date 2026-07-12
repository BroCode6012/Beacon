# Signal working instructions

Read `PROJECT_GUIDE.md` and `CLAUDE.md` before changing this project.

## Product

Signal is a standalone Flet desktop control room for investigating YouTube
niches and channels. The user is non-technical. Explain behavior in plain
language before implementation detail.

The product target is strictly **faceless, replicable channel formats**. Signal
is not intended to surface every high-performing channel. Opportunity views
should suppress music, celebrities, children’s content, major brands/networks,
one-off viral accidents, malformed records, and unstable tiny-data ratios.

Signal is not the shared scraper engine. It physically owns the one shared
index; it does not maintain a private second database.

## Boundaries

- Signal-owned code: `D:\Youtube\Signal`
- Shared engine: `D:\Youtube\Scripter\scripter`
- Shared live database:
  `D:\Youtube\Signal\storage\youtube_index.sqlite3`
- Shared radar config, status, queue, logs, and model state:
  `D:\Youtube\Scripter\storage`

An edit under the shared engine affects both Signal and the Production Lab.
State that impact explicitly before making such an edit.

Do not move, copy, reset, replace, or casually back up the live database.

Do not stop the radar, kill Signal, run a write tool, or change the watchlist
unless the user’s task requires that state change.

## Architecture rules

1. Import `engine_link` before importing `scripter.*` from a Signal entry point.
2. Normal Signal UI features must use `FinderClient`.
3. Do not add direct SQLite queries to `signal_app.py`.
4. Keep Signal’s Bedrock agent separate from Production Lab chat/agent modules.
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
- Signal cache TTLs, automatic Overview refresh, and write invalidation;
- shared Finder cache invalidation after writes;
- mandatory faceless + replicable opportunity filtering/ranking;
- bounded, yield-aware Autopilot query additions;
- Agent schema/error-status alignment;
- missing scikit-learn/joblib declarations; and
- Signal/shared regression suites (25 Signal tests; 113 shared tests).

Completed and verified on 2026-07-12:

- the shared live index was checkpointed, hash-verified, integrity-checked, and
  relocated into Signal storage;
- the old Scripter database file and temporary legacy fallback were removed;
- radar and monitor resumed against the Signal-owned index while their
  operational state remained in Scripter storage; and
- successful empty searches now complete first-pass query tracking while
  provider failures remain retryable;
- the durable radar can add at most two reviewed, evidence-backed queries after
  100% coverage, sharing Signal's cap, cooldown, deduplication, and file lock;
- the live rollout reached 4,164/4,164 searches with zero remaining and correctly
  added nothing while the Autopilot cooldown was active; and
- Signal/shared regression suites passed (37 Signal tests plus 13 subtests; 148
  shared tests).

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
3. Run the relevant shared test suite when engine code changes.
4. Launch a separate real Flet app instance with the shared venv and an isolated
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
- Do not silently broaden a Signal-only task into shared-engine work.
- Update `PROJECT_GUIDE.md` when architecture, core behavior, or the work plan
  materially changes.
