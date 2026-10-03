# Beacon development roadmap — 2026-10-03

Goal: a dependable research tool for finding faceless, repeatable YouTube
formats, with foundations suitable for a possible future paid product.
The user authorized steps 1–7. Billing and public customer launch are outside
this development sequence. Explain each milestone in beginner-friendly terms.

1. **Reliable saving and background work.** Release completed imports promptly;
   preserve saved requests through errors/restarts; verify concurrent importing
   and channel analysis. First importer contention fix deployed as `8fee1be`.
   Same-row edits must still take turns. Continue observing genuine production
   contention; do not promise that PostgreSQL eliminates all waits.
2. **Fast, correct searches.** Measure real plans and page timings, optimize
   unnecessary work, preserve filter-before-limit semantics and stable ranking.
   Compare result identities against the current query. Aim for ordinary pages
   around 2–3 seconds, with measured limits under collector/backup load.
   First format-search optimization deployed as `b16d173`: full rows matched
   the previous query; Long-form 22.379s → 4.555s, Short-form 14.629s → 3.602s
   in measured full-data runs. Further measurements/optimization remain.
3. **Trustworthy recommendations.** Maintain a reviewed labeled benchmark for
   faceless/repeatable formats and exclusions (music, celebrities, children,
   brands/networks, viral accidents, malformed/tiny samples). Measure false
   positives before changing thresholds. Keep exclusions reversible.
4. **Recent growth and freshness.** Show when evidence was checked; distinguish
   measured view/subscriber growth from lifetime totals, channel age and the date
   Beacon discovered it. Missing or insufficient history must remain explicit.
5. **Deeper channel research.** Make recent-upload performance, typical results,
   evidence/history charts and useful comparable channels accessible from each
   channel, with clear sample size and stale-data handling.
6. **Organized research.** Durable saved searches, research workspaces and video/
   thumbnail collections, plus meaningful niche comparisons. Reuse existing
   niche infrastructure where appropriate; do not build a second index.
7. **Evidence-grounded AI and durable corrections.** AI should cite the actual
   records behind conclusions and distinguish facts from suggestions. Human
   topic/classification corrections must carry provenance and survive automatic
   refreshes. Preserve full/read-token authorization and durable write auditing.

For every application change: compile, run focused/relevant shared tests, then
verify the real isolated Flet workflow and all six screens with inspected
screenshots before another application change. Stage only owned files. Build
cloud releases from committed clean code; preserve unrelated Quill work and
the live PostgreSQL index. Current evidence and deployment checkpoints are in
`POSTGRESQL_PROGRESS.md`.
