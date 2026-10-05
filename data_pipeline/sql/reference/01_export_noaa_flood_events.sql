-- ===========================================================================
-- REFERENCE STEP 1/3: copy NOAA flood events -> our Cloud Storage bucket
-- ===========================================================================
-- WHAT THIS DOES
--   Reads the public NOAA Storm Events tables
--   (bigquery-public-data.noaa_historic_severe_storms.storms_YYYY), keeps only
--   flood-type events in our supported states since 2015 (configurable), and writes
--   them as Parquet files to
--       gs://{bucket}/reference/noaa_flood_events/run={run_id}/part-*.parquet
--   The next steps load those files into our own us-central1 dataset.
--
-- WHY A COPY IS NEEDED (beginner note)
--   The public dataset lives in the BigQuery "US" multi-region. Our dataset
--   lives in us-central1. BigQuery can only JOIN tables that live in the
--   same location, so the app could not query NOAA data directly any more.
--   EXPORT DATA is the bridge: the job RUNS in location US (next to the
--   public data) and WRITES files to our us-central1 bucket. Nothing is
--   stored in the US multi-region; only the (free, public) data is read there.
--   A US-multi-region job writing to a us-central1 bucket is the "colocated"
--   combination in BigQuery's location rules, so there is no transfer fee.
--
-- RUN THIS JOB WITH LOCATION = 'US'
--   Python: load_to_bigquery.py does it for you (step "reference").
--   bq CLI: bq query --location=US --use_legacy_sql=false < this_file (rendered)
--
-- DATA QUIRKS WE FIX HERE (found by profiling the public tables)
--   * `state` is TRUNCATED to 2 characters ("Te" = Texas? Tennessee?), so we
--     identify the state by its FIPS number instead (`state_fips_code` is
--     "48" in NOAA and "48"/"08" zero-padded in geo_us_boundaries -> compare
--     as integers).
--   * `event_type` is lowercase ("flash flood"). We map it back to the
--     canonical spelling used by the app (claimdesk FLOOD_EVENT_TYPES,
--     e.g. "Flash Flood") so the app can filter with a plain IN.
--   * The same event_id appears several times (one row per corner of the
--     warning polygon); we keep one row per event_id with the average
--     (centre) of the corner coordinates.
--   * `flood_cause` sometimes holds the text 'nan' -> NULL.
--   * GEOGRAPHY columns don't round-trip cleanly through files, so we export
--     plain latitude/longitude numbers; step 3 rebuilds the point.
--
-- COST: the filtered read scans roughly 100 MB (well inside the free
--   1 TiB/month of query processing). The output is ~17k rows, < 2 MB.
--
-- PLACEHOLDERS (filled in by data_pipeline/load_to_bigquery.py)
--   bucket, run_id, states, event_types, storms_tables
--   (written without braces here on purpose: a placeholder inside a comment
--   would be replaced too, and a multi-line value would break the comment).
-- ===========================================================================
EXPORT DATA OPTIONS (
  uri = 'gs://{bucket}/reference/noaa_flood_events/run={run_id}/part-*.parquet',
  format = 'PARQUET',
  overwrite = true
) AS
WITH
  -- 2-letter code <-> FIPS number <-> full name, for our supported states.
  states AS (
    SELECT DISTINCT
      SAFE_CAST(state_fips_code AS INT64) AS state_fips,
      state_code,
      UPPER(state_name) AS state_name
    FROM `bigquery-public-data.geo_us_boundaries.zip_codes`
    WHERE state_code IN UNNEST({states})
  ),
  -- The app's canonical event names, e.g. 'Flash Flood'.
  event_types AS (
    SELECT name AS event_type, LOWER(name) AS event_type_lower
    FROM UNNEST({event_types}) AS name
  ),
  events AS (
    SELECT
      s.event_id,
      t.event_type,
      st.state_name AS state,
      st.state_code,
      st.state_fips,
      s.cz_type,
      s.cz_fips_code,
      UPPER(s.cz_name) AS cz_name,
      s.event_begin_time,
      DATE(s.event_begin_time) AS event_date,
      -- Prefer the GEOGRAPHY point; fall back to the numeric columns.
      COALESCE(ST_Y(s.event_point), s.event_latitude) AS lat,
      COALESCE(ST_X(s.event_point), s.event_longitude) AS lon,
      s.damage_property,
      NULLIF(NULLIF(TRIM(s.flood_cause), 'nan'), '') AS flood_cause
    -- The placeholder below becomes "(SELECT ... FROM storms_2015 UNION ALL
    -- SELECT ... FROM storms_2016 ...)": one yearly table per year from
    -- --noaa-from-year (default 2015) to the newest table that exists.
    -- EXPORT DATA refuses the storms_* wildcard ("meta table"), so the
    -- loader lists the years for us (see storms_union_sql in the loader).
    FROM {storms_tables} AS s
    JOIN states AS st
      ON st.state_fips = SAFE_CAST(s.state_fips_code AS INT64)
    JOIN event_types AS t
      ON t.event_type_lower = LOWER(s.event_type)
    WHERE s.event_id IS NOT NULL
  )
-- One row per NOAA event id. The source has several rows per event: one per
-- corner of the warning polygon (flash floods usually have 4). We keep the
-- event's attributes once and use the AVERAGE of the corner coordinates,
-- i.e. roughly the centre of the flooded area.
SELECT
  event_id,
  ANY_VALUE(event_type) AS event_type,
  ANY_VALUE(state) AS state,
  ANY_VALUE(state_code) AS state_code,
  ANY_VALUE(state_fips) AS state_fips,
  ANY_VALUE(cz_type) AS cz_type,
  ANY_VALUE(cz_fips_code) AS cz_fips_code,
  ANY_VALUE(cz_name) AS cz_name,
  MIN(event_begin_time) AS event_begin_time,
  MIN(event_date) AS event_date,
  AVG(lat) AS lat,
  AVG(lon) AS lon,
  MAX(damage_property) AS damage_property,
  ANY_VALUE(flood_cause) AS flood_cause,
  COUNT(*) AS source_rows
FROM events
GROUP BY event_id
