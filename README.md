# Beacon

Beacon finds and tracks YouTube channels with faceless, repeatable formats.
This repository contains the whole application: interface, Finder API, scraper,
background workers, research engine and PostgreSQL support. No Quill checkout,
Quill access, Quill environment or Quill credentials are needed.

## Run your own development copy

Install Python 3.11 or newer, then run these commands from this folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:SIGNAL_DISABLE_AUTOPILOT = "1"
.\.venv\Scripts\python.exe beacon_app.py
```

The new copy starts with its own empty index in `storage/`. This local development
index uses SQLite; **the live Oracle application remains on PostgreSQL**.
An empty channel list is normal before you collect or save channels. Actual
YouTube/AI features need your own service settings; keys are not included.
Keep `.env`, `beacon_host.env` and everything in `storage/` private and uncommitted.
Use a separate development folder and port when another Beacon instance is open.

For a browser preview, set `FLET_FORCE_WEB_SERVER=1`, `FLET_SERVER_IP=127.0.0.1`
and `FLET_SERVER_PORT=8571` before launching. Open http://127.0.0.1:8571.
Local changes affect this preview only; pushing a branch does not deploy Oracle.

## Check your changes

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests requiring real PostgreSQL skip unless `BEACON_TEST_POSTGRES_DSN` points to
an explicitly disposable test database. Never use the production database here.
For PostgreSQL runtime, use `YOUTUBE_INDEX_DB=postgresql:beacon-index` with private
`BEACON_POSTGRES_DSN`, `BEACON_POSTGRES_SCHEMA` and optional read-role DSN.
Schema provisioning is explicit; ordinary app starts do not create/upgrade it.

## Collaborating

Create a branch, make changes, check your local preview, then push that branch
and open a pull request. The owner reviews and deploys an approved release.
Only this repository is required. The collector lives in `deploy/oracle-edge/`;
server services and installation are in `deploy/server/`.
See `APP_SEPARATION.md` for the separation and safe deployment checklist.

Windowless Windows tasks use `run_python_hidden.vbs` with this checkout's Python;
they do not rely on another app's interpreter. The optional Oracle firewall helper
uses a separate private environment under `storage/oci_updater_venv` with `oci`
and `python-dotenv` installed. Keep its host credentials private.
