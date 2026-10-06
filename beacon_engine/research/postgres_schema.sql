-- PostgreSQL research index, version 6. Run only with the schema-owner role.
-- Text timestamps and JSON preserve existing records exactly. Date/JSON helpers
-- below define the known research queries' semantics, including malformed data.
CREATE TABLE IF NOT EXISTS beacon_schema_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL
);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM beacon_schema_meta WHERE version > 6) THEN
        RAISE EXCEPTION 'Database schema is newer than this release';
    END IF;
END $$;

CREATE COLLATION IF NOT EXISTS beacon_unicode (provider = libc, locale = 'C.UTF-8');

-- SQLite NOCASE folds ASCII only. Preserve that exact uniqueness behavior.
CREATE OR REPLACE FUNCTION beacon_nocase(value TEXT) RETURNS TEXT
LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT translate(value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')
$$;

-- EXCEPTION blocks establish subtransactions. PostgreSQL forbids them during
-- parallel execution, including in the leader. Wrappers inherit that limit.
CREATE OR REPLACE FUNCTION json_valid(value TEXT) RETURNS BOOLEAN
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL UNSAFE AS $$
BEGIN
    -- Validate the original syntax. JSONB also converts unrelated string values
    -- to PostgreSQL text and rejects otherwise-valid JSON containing \u0000.
    PERFORM value::json;
    RETURN true;
EXCEPTION WHEN invalid_text_representation OR untranslatable_character THEN RETURN false;
END $$;

CREATE OR REPLACE FUNCTION json_extract(value TEXT, path TEXT) RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL UNSAFE AS $$
BEGIN
    IF left(path, 2) != '$.' THEN
        RAISE EXCEPTION 'Unsupported research JSON path';
    END IF;
    IF strpos(value, $nul$\u0000$nul$) > 0 THEN
        -- The stored JSON stays byte-for-byte intact. Remove active NUL escapes
        -- only for this read, preserving literal escaped backslashes. Validate
        -- first so an invalid document cannot become valid by removing a token.
        PERFORM value::json;
        value := regexp_replace(value,
            $pattern$(?<!\\)((?:\\\\)*)\\u0000$pattern$,
            $replacement$\1$replacement$, 'g');
    END IF;
    RETURN value::jsonb #>> string_to_array(substr(path, 3), '.');
EXCEPTION WHEN invalid_text_representation OR untranslatable_character THEN RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION beacon_timestamp(value TEXT) RETURNS TIMESTAMPTZ
LANGUAGE plpgsql STABLE STRICT PARALLEL UNSAFE AS $$
BEGIN
    IF value = 'now' THEN RETURN statement_timestamp(); END IF;
    IF btrim(value) = '' THEN RETURN NULL; END IF;
    RETURN value::timestamptz;
EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION julianday(value TEXT) RETURNS DOUBLE PRECISION
LANGUAGE SQL STABLE STRICT PARALLEL UNSAFE AS $$
    SELECT extract(epoch FROM beacon_timestamp(value))::double precision / 86400.0 + 2440587.5
$$;
CREATE OR REPLACE FUNCTION julianday(value TEXT, modifier TEXT) RETURNS DOUBLE PRECISION
LANGUAGE SQL STABLE STRICT PARALLEL UNSAFE AS $$
    SELECT extract(epoch FROM (beacon_timestamp(value) + modifier::interval))::double precision
           / 86400.0 + 2440587.5
$$;
CREATE OR REPLACE FUNCTION datetime(value TEXT) RETURNS TEXT
LANGUAGE SQL STABLE STRICT PARALLEL UNSAFE AS $$
    SELECT to_char(beacon_timestamp(value) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
$$;
CREATE OR REPLACE FUNCTION datetime(value TEXT, modifier TEXT) RETURNS TEXT
LANGUAGE SQL STABLE STRICT PARALLEL UNSAFE AS $$
    SELECT to_char((beacon_timestamp(value) + modifier::interval) AT TIME ZONE 'UTC',
                   'YYYY-MM-DD HH24:MI:SS')
$$;

-- Two-argument bounds are scalar operations. Single-argument MIN/MAX remain
-- PostgreSQL aggregates. STRICT preserves SQLite's NULL propagation.
CREATE OR REPLACE FUNCTION max(a DOUBLE PRECISION, b DOUBLE PRECISION)
RETURNS DOUBLE PRECISION LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
AS $$ SELECT greatest(a, b) $$;
CREATE OR REPLACE FUNCTION min(a DOUBLE PRECISION, b DOUBLE PRECISION)
RETURNS DOUBLE PRECISION LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
AS $$ SELECT least(a, b) $$;
CREATE OR REPLACE FUNCTION max(a BIGINT, b BIGINT)
RETURNS BIGINT LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
AS $$ SELECT greatest(a, b) $$;
CREATE OR REPLACE FUNCTION min(a BIGINT, b BIGINT)
RETURNS BIGINT LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
AS $$ SELECT least(a, b) $$;

-- Signed imports merge ISO timestamp strings in the same order as SQLite.
CREATE OR REPLACE FUNCTION max(a TEXT, b TEXT)
RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
AS $$ SELECT greatest(a COLLATE "C", b COLLATE "C") $$;
CREATE OR REPLACE FUNCTION min(a TEXT, b TEXT)
RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
AS $$ SELECT least(a COLLATE "C", b COLLATE "C") $$;

CREATE OR REPLACE FUNCTION pragma_table_info(table_name TEXT)
RETURNS TABLE(name TEXT) LANGUAGE SQL STABLE STRICT PARALLEL SAFE AS $$
    SELECT column_name::text FROM information_schema.columns
    WHERE table_schema = current_schema() AND information_schema.columns.table_name = $1
$$;

-- Research tables in dependency order.

CREATE TABLE IF NOT EXISTS channel_save_requests (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    input_link TEXT NOT NULL,
    link_kind TEXT NOT NULL,
    link_value TEXT NOT NULL,
    channel_id TEXT NOT NULL DEFAULT '',
    niche_id BIGINT,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','working','ready','failed')),
    phase TEXT NOT NULL DEFAULT 'resolve' CHECK (phase IN ('resolve','analyse')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    lease_owner TEXT NOT NULL DEFAULT '',
    saved_at TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_channel_save_requests_work
ON channel_save_requests(status, available_at, lease_until, id);

CREATE TABLE IF NOT EXISTS background_jobs (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    scope TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    schedule TEXT DEFAULT '',
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

CREATE TABLE IF NOT EXISTS channel_classifications (
                    channel_id TEXT PRIMARY KEY,
                    likely_faceless BIGINT NOT NULL DEFAULT 0,
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                    score DOUBLE PRECISION NOT NULL DEFAULT 0,
                    sample_count BIGINT NOT NULL DEFAULT 0,
                    positive_title_count BIGINT NOT NULL DEFAULT 0,
                    negative_title_count BIGINT NOT NULL DEFAULT 0,
                    positives_json TEXT DEFAULT '[]',
                    negatives_json TEXT DEFAULT '[]',
                    classifier TEXT NOT NULL DEFAULT 'text-v2',
                    classified_at TEXT NOT NULL
                );

CREATE TABLE IF NOT EXISTS channel_exclusions (
                    channel_id TEXT PRIMARY KEY,
                    reason TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );

CREATE TABLE IF NOT EXISTS channel_face_check (
                    channel_id TEXT PRIMARY KEY,
                    thumbnails_checked BIGINT NOT NULL DEFAULT 0,
                    faces_found BIGINT NOT NULL DEFAULT 0,
                    recurring_count BIGINT NOT NULL DEFAULT 0,
                    verdict TEXT NOT NULL DEFAULT 'unknown',
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                    checked_at TEXT NOT NULL
                );

CREATE TABLE IF NOT EXISTS channel_snapshots (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    subscribers BIGINT NOT NULL DEFAULT 0,
                    total_views BIGINT NOT NULL DEFAULT 0,
                    video_count BIGINT NOT NULL DEFAULT 0,
                    average_views DOUBLE PRECISION NOT NULL DEFAULT 0,
                    median_views DOUBLE PRECISION NOT NULL DEFAULT 0,
                    upload_cadence_days DOUBLE PRECISION NOT NULL DEFAULT 0,
                    captured_at TEXT NOT NULL,
                    source TEXT DEFAULT ''
                );

CREATE TABLE IF NOT EXISTS channel_text_model (
                    channel_id TEXT PRIMARY KEY,
                    faceless BIGINT NOT NULL DEFAULT 0,
                    proba DOUBLE PRECISION NOT NULL DEFAULT 0,
                    model_version TEXT DEFAULT '',
                    scored_at TEXT NOT NULL
                );

CREATE TABLE IF NOT EXISTS channel_vlm_check (
                    channel_id TEXT PRIMARY KEY,
                    verdict TEXT NOT NULL DEFAULT 'unclear',
                    model TEXT DEFAULT '',
                    raw TEXT DEFAULT '',
                    checked_at TEXT NOT NULL
                );

CREATE TABLE IF NOT EXISTS channels (
                    channel_id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '',
                    handle TEXT DEFAULT '',
                    subscribers BIGINT DEFAULT 0,
                    video_count BIGINT DEFAULT 0,
                    view_count BIGINT DEFAULT 0,
                    description TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    raw_json TEXT DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                , category TEXT DEFAULT '', channel_published_at TEXT DEFAULT '', topic TEXT DEFAULT '');

CREATE TABLE IF NOT EXISTS comments (
                    comment_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    author TEXT DEFAULT '',
                    like_count BIGINT DEFAULT 0,
                    published_at TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    raw_json TEXT DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

CREATE TABLE IF NOT EXISTS crawl_runs (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    kind TEXT NOT NULL,
                    query TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    message TEXT DEFAULT '',
                    counts_json TEXT DEFAULT '{}'
                );

CREATE TABLE IF NOT EXISTS edge_import_batches (
    bundle_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    counts_json TEXT NOT NULL DEFAULT '{}',
    source_path TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS edge_organization_import_jobs (
    source_job_revision TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL UNIQUE,
    node_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL,
    counts_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS edge_organization_runs (
    node_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source_job_revision TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    status TEXT NOT NULL,
    catalog_attempted BIGINT NOT NULL DEFAULT 0,
    catalog_status TEXT NOT NULL DEFAULT '',
    catalog_attempts BIGINT NOT NULL DEFAULT 0,
    videos_collected BIGINT NOT NULL DEFAULT 0,
    categories_filled BIGINT NOT NULL DEFAULT 0,
    face_status TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL,
    PRIMARY KEY (node_id, job_id, run_id, channel_id)
);

CREATE TABLE IF NOT EXISTS edge_query_runs (
    node_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    query TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    result_count BIGINT NOT NULL DEFAULT 0,
    new_result_count BIGINT NOT NULL DEFAULT 0,
    sort TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (node_id, job_id, run_id, query)
);

CREATE TABLE IF NOT EXISTS enrichment_log (
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    attempts BIGINT NOT NULL DEFAULT 0,
                    last_status TEXT DEFAULT '',
                    last_attempt_at TEXT NOT NULL,
                    PRIMARY KEY (entity_type, entity_id)
                );

CREATE TABLE IF NOT EXISTS labs (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    channel_id TEXT DEFAULT '',
                    niche TEXT DEFAULT '',
                    about TEXT DEFAULT '',
                    audience TEXT DEFAULT '',
                    voice TEXT DEFAULT '',
                    goals TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

CREATE TABLE IF NOT EXISTS niches (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                , autonomous BIGINT DEFAULT 1);

CREATE TABLE IF NOT EXISTS search_results (
                    query TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    position BIGINT NOT NULL,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY(query, provider, video_id)
                );

CREATE TABLE IF NOT EXISTS transcripts (
                    video_id TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL,
                    generated BIGINT,
                    source TEXT DEFAULT '',
                    raw_json TEXT DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY(video_id, language)
                );

CREATE TABLE IF NOT EXISTS video_discoveries (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    query TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    discovery_kind TEXT NOT NULL DEFAULT 'search',
                    position BIGINT NOT NULL DEFAULT 0,
                    run_id BIGINT,
                    discovered_at TEXT NOT NULL,
                    UNIQUE(
                        video_id, query, provider, discovery_kind, discovered_at
                    )
                );

CREATE TABLE IF NOT EXISTS videos (
                    video_id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '',
                    channel TEXT DEFAULT '',
                    channel_id TEXT DEFAULT '',
                    views BIGINT DEFAULT 0,
                    published_at TEXT DEFAULT '',
                    published_at_normalized TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    duration_seconds BIGINT DEFAULT 0,
                    description TEXT DEFAULT '',
                    thumbnail_url TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    raw_json TEXT DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                , category_id TEXT DEFAULT '');

CREATE TABLE IF NOT EXISTS alerts (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    niche_id BIGINT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'info',
                    title TEXT NOT NULL,
                    message TEXT DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    fingerprint TEXT NOT NULL UNIQUE,
                    detected_at TEXT NOT NULL,
                    read_at TEXT,
                    FOREIGN KEY(niche_id) REFERENCES niches(id) ON DELETE CASCADE
                );

CREATE TABLE IF NOT EXISTS idea_runs (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    niche_id BIGINT NOT NULL,
                    mode TEXT NOT NULL,
                    input_json TEXT NOT NULL DEFAULT '{}',
                    ideas_json TEXT NOT NULL DEFAULT '[]',
                    model TEXT DEFAULT '',
                    cost DOUBLE PRECISION NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(niche_id) REFERENCES niches(id) ON DELETE CASCADE
                );

CREATE TABLE IF NOT EXISTS lab_notes (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    lab_id BIGINT NOT NULL REFERENCES labs(id),
                    kind TEXT NOT NULL DEFAULT 'note',
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

CREATE TABLE IF NOT EXISTS lab_videos (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    lab_id BIGINT NOT NULL REFERENCES labs(id),
                    title TEXT NOT NULL,
                    idea TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'idea',
                    script_path TEXT DEFAULT '',
                    video_url TEXT DEFAULT '',
                    performance TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

CREATE TABLE IF NOT EXISTS niche_channels (
                    niche_id BIGINT NOT NULL,
                    channel_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'competitor',
                    enabled BIGINT NOT NULL DEFAULT 1,
                    source TEXT DEFAULT 'manual',
                    note TEXT DEFAULT '',
                    added_at TEXT NOT NULL,
                    PRIMARY KEY(niche_id, channel_id),
                    FOREIGN KEY(niche_id) REFERENCES niches(id) ON DELETE CASCADE
                );

CREATE TABLE IF NOT EXISTS outlier_snapshots (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    outlier_score DOUBLE PRECISION NOT NULL,
                    baseline_views BIGINT NOT NULL,
                    views BIGINT NOT NULL,
                    age_days BIGINT NOT NULL,
                    views_per_day DOUBLE PRECISION NOT NULL,
                    captured_at TEXT NOT NULL,
                    source TEXT DEFAULT '',
                    FOREIGN KEY(video_id) REFERENCES videos(video_id) ON DELETE CASCADE
                );

CREATE TABLE IF NOT EXISTS research_findings (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    niche_id BIGINT NOT NULL,
                    finding_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    explanation TEXT DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(niche_id) REFERENCES niches(id) ON DELETE CASCADE
                );

CREATE TABLE IF NOT EXISTS saved_views (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    niche_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    entity_type TEXT NOT NULL DEFAULT 'feed',
                    filters_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(niche_id, name),
                    FOREIGN KEY(niche_id) REFERENCES niches(id) ON DELETE CASCADE
                );

CREATE TABLE IF NOT EXISTS video_snapshots (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    views BIGINT NOT NULL,
                    captured_at TEXT NOT NULL,
                    source TEXT DEFAULT '',
                    FOREIGN KEY(video_id) REFERENCES videos(video_id) ON DELETE CASCADE
                );

-- Research tables in dependency order.

CREATE INDEX IF NOT EXISTS idx_alerts_niche
                    ON alerts(niche_id, read_at, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_background_jobs_status
                    ON background_jobs(status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_channel_classifications_faceless
                    ON channel_classifications(
                        likely_faceless, confidence DESC, score DESC
                    );

-- Research tables: explicitly provisioned, preserving original column order.

CREATE INDEX IF NOT EXISTS idx_channel_snapshots_channel
                    ON channel_snapshots(channel_id, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_channels_category
                ON channels(category)
                ;

CREATE INDEX IF NOT EXISTS idx_channels_topic
                ON channels(topic)
                ;

CREATE INDEX IF NOT EXISTS idx_comments_likes ON comments(like_count DESC);

CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(video_id);

CREATE INDEX IF NOT EXISTS idx_discoveries_query
                    ON video_discoveries(query, discovery_kind, discovered_at DESC);

CREATE INDEX IF NOT EXISTS idx_discoveries_video
                    ON video_discoveries(video_id, discovered_at DESC);

CREATE INDEX IF NOT EXISTS idx_edge_organization_runs_finished
ON edge_organization_runs(finished_at DESC, status)
;

CREATE INDEX IF NOT EXISTS idx_edge_query_runs_finished
ON edge_query_runs(finished_at DESC, status)
;

CREATE INDEX IF NOT EXISTS idx_findings_niche
                    ON research_findings(niche_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_idea_runs_niche
                    ON idea_runs(niche_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_lab_notes_lab
                    ON lab_notes(lab_id, kind);

CREATE INDEX IF NOT EXISTS idx_lab_videos_lab
                    ON lab_videos(lab_id, status);

CREATE INDEX IF NOT EXISTS idx_niche_channels_channel
                    ON niche_channels(channel_id, enabled);

CREATE INDEX IF NOT EXISTS idx_outlier_snapshots_score
                    ON outlier_snapshots(outlier_score DESC);

CREATE INDEX IF NOT EXISTS idx_outlier_snapshots_video
                    ON outlier_snapshots(video_id, captured_at);

CREATE INDEX IF NOT EXISTS idx_search_query ON search_results(query, provider, position);

CREATE INDEX IF NOT EXISTS idx_snapshots_video ON video_snapshots(video_id, captured_at);

CREATE INDEX IF NOT EXISTS idx_videos_category
                ON videos(category_id)
                ;

CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);

-- Workspace counts/activity and format averages need these small scalar fields,
-- not the videos' large provider payloads. One covering index serves both reads.
CREATE INDEX IF NOT EXISTS idx_videos_channel_metrics
ON videos(channel_id) INCLUDE (last_seen_at, duration_seconds);

-- Exact full-index counts can scan compact deduplicated posting lists instead
-- of the wide history/discovery records. These are ordinary transactional
-- indexes: inserts, removals, rollback and read snapshots preserve exact counts.
CREATE INDEX IF NOT EXISTS idx_video_snapshots_count ON video_snapshots ((1));
CREATE INDEX IF NOT EXISTS idx_video_discoveries_count ON video_discoveries ((1));

CREATE INDEX IF NOT EXISTS idx_videos_published_normalized
                ON videos(published_at_normalized DESC)
                ;

CREATE INDEX IF NOT EXISTS idx_videos_seen ON videos(last_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_videos_views ON videos(views DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_niches_name_nocase ON niches(beacon_nocase(name));

CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_views_name_nocase ON saved_views(niche_id, beacon_nocase(name));

CREATE OR REPLACE FUNCTION opportunity_name_rejection(title TEXT) RETURNS INTEGER
LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN t LIKE '% - topic' OR t LIKE '%musical'
         OR strpos(t, 'vevo') > 0 OR t ~ '\y(?:netflix|tedx|ted\-ed|wwe|wall\ street\ journal|wall\ street\ journal|business\ insider|dw\ documentary|free\ documentary|pbs\ frontline|frontline\ pbs|5\-minute\ crafts|123\ go|crafty\ panda|buzzfeed|history\ channel|vogue|espn|national\ geographic|nat\ geo|natgeo|bbc|bbc\ earth|bbc\ news|discovery\ channel|discovery\ uk|animal\ planet|smithsonian\ channel|pbs|pbs\ nova|cnbc|cnn|abc\ news|nbc\ news|cbs\ news|sky\ news|al\ jazeera|vice\ news|vice|bloomberg|reuters|fox\ news|curiositystream|nova\ pbs|history\ uk|cbbc|wired\ investigations|nibble\ app)\y' OR t ~ '\y(?:meme|memes|dank\ meme|dank\ memes|funny\ moments|funny\ fails|fails\ compilation|tiktok\ compilation|best\ vines|vine\ compilation|try\ not\ to\ laugh|funny\ compilation|shorts\ compilation)\y'
         OR EXISTS (SELECT 1 FROM unnest(ARRAY['peppa','bluey','ben & holly','ben and holly','paw patrol','cocomelon','baby shark','pj masks','wolfoo','blippi','little baby bum','chuchu','morphle','masha and the bear','kids diana','vlad and niki','ryan''s world','spidey and his amazing','gecko''s garage','nursery rhyme','kids tv','kids academy','kids learning','kids cartoon','kids songs','kids channel','kidz','cartoons for kids','cartoon for kids']::text[]) AS k WHERE strpos(t,k) > 0)
         OR strpos(t, 'k-pop') > 0 OR strpos(t, 'kpop') > 0
         THEN 1 ELSE 0 END
    FROM (SELECT lower(btrim(COALESCE(title,''), E' \t\n\r\v\f') COLLATE beacon_unicode) AS t) folded
$$;

INSERT INTO beacon_schema_meta(id,version) VALUES (1,6) ON CONFLICT(id) DO UPDATE SET version=excluded.version;
