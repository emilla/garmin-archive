-- Raw archive of Garmin Connect data. Every table keeps the API response as
-- returned, so nothing is lost if Garmin adds or renames fields. Typed views
-- for dashboards are built on top of these tables.

CREATE SCHEMA IF NOT EXISTS archive;

-- One row per data source per calendar day. payload is NULL when Garmin
-- returned nothing for that day, so a backfill does not ask again.
CREATE TABLE IF NOT EXISTS archive.daily (
    source      text        NOT NULL,
    day         date        NOT NULL,
    payload     jsonb,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, day)
);

-- One row per activity. summary comes from the activity list, details from
-- the single-activity endpoint. file_status is NULL until the original file
-- has been downloaded ('ok') or Garmin has none for it ('none').
CREATE TABLE IF NOT EXISTS archive.activity (
    activity_id    bigint      PRIMARY KEY,
    start_time     timestamptz,
    activity_type  text,
    summary        jsonb       NOT NULL,
    details        jsonb,
    file_status    text,
    fetched_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS activity_start_time_idx ON archive.activity (start_time);

-- Original files (usually one .fit) from each activity's download.
CREATE TABLE IF NOT EXISTS archive.activity_file (
    activity_id  bigint      NOT NULL REFERENCES archive.activity ON DELETE CASCADE,
    filename     text        NOT NULL,
    content      bytea       NOT NULL,
    sha256       text        NOT NULL,
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (activity_id, filename)
);

-- Account data that is not tied to a day (profile, devices, gear, records).
-- A row is added only when the content differs from the latest one, so the
-- table holds a history of changes.
CREATE TABLE IF NOT EXISTS archive.snapshot (
    id          bigserial   PRIMARY KEY,
    source      text        NOT NULL,
    sha256      text        NOT NULL,
    payload     jsonb       NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS snapshot_source_idx ON archive.snapshot (source, id DESC);

-- One row per run of the collector. Dashboards can alert when the newest
-- 'ok' run is too old.
CREATE TABLE IF NOT EXISTS archive.sync_run (
    id           bigserial   PRIMARY KEY,
    command      text        NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text        NOT NULL DEFAULT 'running',
    detail       jsonb
);
