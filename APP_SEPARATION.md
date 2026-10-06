# Independent applications

Beacon owns `beacon_engine/`, its dependencies, local environment, settings and
worker state. The engine was forked from the previously shared research code on
October 6, 2026. The fork is independent: edits do not affect Quill. No Quill
writing pipeline, chat module, Production Lab implementation, prompts or secrets
are included. Small generic research/LLM helpers are owned copies, not imports.

Quill keeps `scripter/`, its writing application and its own catalog/session data.
There is no configured code import or automatic database connection between apps.
A future API integration must be separately implemented and explicitly authorized.
Historical migration SQL retains empty legacy table definitions for compatible
restores; those definitions do not expose Quill's writing implementation.

## Runtime and deployment

`beacon_move.py code` packages only `Beacon/`. Finder, radar, Saved and edge
workers import `beacon_engine`. Server services run inside the Beacon checkout.
`BEACON_ENGINE_STATE_DIR` selects Beacon's operational state; local defaults are
Beacon `storage/`. No sibling repository is searched for code or settings.

The code separation does not switch the currently running immutable Oracle
release. Production remains PostgreSQL. Before rolling out the independent release:

1. Package this checked revision; compare the archive against the reviewed code.
2. Rehearse on isolated server ports, database/schema and operational state.
3. Set the explicit existing Beacon PostgreSQL and worker-state locations in
   private host settings. Preserve canonical data, token roles and signed edge ACKs.
4. Roll out the Beacon app and collector code as coordinated independent releases;
   only restart affected services after the rehearsal and readiness checks pass.
5. Verify Saved, discovery, import/ACK processing, authentication and all six screens.
   Retain the prior immutable releases for rollback.

Do not run a development collector against the production queue or credentials.
No production data migration, worker stop or deployment is part of this code split.

## Verification on October 6

The standalone import probe rejects every `scripter` import and validates that
loaded engine files belong to this checkout. The code-only archive contains
only `Beacon/` and passes the secret-name/content scan.

Beacon's local suite passed 661 tests plus 110 subtests before the launcher change.
The independent package passed 98 PostgreSQL tests plus 106 subtests on a new
isolated PostgreSQL 16 cluster, including native backup/migration checks.
A Windows migration/COPY test stalled over the temporary SSH tunnel and was
stopped; the same migration checks passed on the server. Windows normal UI reads,
writes and Saved queue processing passed against that isolated cluster. This
is not a claim that Windows administrator bulk migration was verified.

Actual isolated Flet clicks: Overview showed the one-channel/24-video test index;
Channels showed a qualified synthetic channel; Real Time showed its discovery;
Find loaded its sweep and browse controls; Saved showed an empty shelf, rejected
a foreign URL, accepted a valid channel, displayed queued/loading states, then
showed the worker-completed channel without completed request notices; Agent
showed its expected setup message because no private AI credentials were loaded.
The read token refused a write. App output showed no exceptions.

Quill passed 523 tests plus 139 subtests and 86 real PostgreSQL tests plus
109 subtests. In its isolated real UI, Pull from catalog copied one fixture
channel/video into a separate Quill session; Save to catalog confirmed the Quill
catalog target and completed successfully. No Beacon data was opened or copied.
All test databases, ports and app copies were separate from production.

The final Beacon suite passed 662 tests plus 110 subtests after adding the
windowless-launcher regression. The launcher preserves a worker's nonzero
exit code. All five existing laptop Beacon task actions now use Beacon-owned
interpreters/scripts. Disabled tasks remained disabled; active Oracle-access
and backup-transfer schedules remained enabled and hidden. The independent
Oracle-access helper returned `unchanged`, preserving the managed firewall rule.
Only the required Oracle-access settings were placed in Beacon's private `.env`;
Quill writing/provider credentials were not copied or published.
