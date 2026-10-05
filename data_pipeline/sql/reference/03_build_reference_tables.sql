-- ===========================================================================
-- REFERENCE STEP 3/3: build `noaa_flood_events` and `zip_points`
-- ===========================================================================
-- WHAT THIS DOES
--   The loader has just loaded the Parquet files from steps 1-2 into two
--   staging tables in OUR dataset ({location}):
--       stg_noaa_flood_events, stg_zip_points
--   This script turns them into the two tables the app queries at runtime
--   (claimdesk/data_access/weather_events.py), rebuilding the GEOGRAPHY
--   points from lat/lon, and then drops the staging tables.
--
-- RUN THIS JOB IN OUR DATASET'S LOCATION ({location}), NOT in 'US'.
--
-- WHY PARTITION BY MONTH (not by day)?
--   A table partitioned by DATE(event_date) would get one partition per day
--   (~4,000+ since 2015). BigQuery caps how many partitions ONE job may
--   write (4,000), and tiny daily partitions waste metadata. Monthly
--   partitions (~140) still let a "loss date +/- 3 days" query read only
--   1-2 partitions. CLUSTER BY state_code, event_type sorts rows inside each
--   partition so filters on them read even less.
--
-- PLACEHOLDERS: {project} {dataset} {location}
-- ===========================================================================

CREATE OR REPLACE TABLE `{project}.{dataset}.noaa_flood_events`
PARTITION BY DATE_TRUNC(event_date, MONTH)
CLUSTER BY state_code, event_type
OPTIONS (
  description = 'Flood-type NOAA Storm Events for the supported states (copy of bigquery-public-data.noaa_historic_severe_storms, one row per event_id). Refresh with: load_to_bigquery --steps reference',
  labels = [('app', 'claimdesk'), ('source', 'noaa')]
)
AS
SELECT
  event_id,
  event_type,                     -- canonical name, e.g. 'Flash Flood'
  state,                          -- full uppercase name, e.g. 'TEXAS'
  state_code,                     -- 'TX'
  state_fips,                     -- 48
  cz_type,                        -- 'C' = county/parish, 'Z' = forecast zone
  cz_fips_code,
  cz_name,                        -- uppercase, e.g. 'ST. CHARLES'
  -- Same letters-only key as zip_points.county_name_normalized.
  REGEXP_REPLACE(cz_name, r'[^A-Z]', '') AS cz_name_normalized,
  event_begin_time,               -- DATETIME in the event's LOCAL time zone
  event_date,
  lat,
  lon,
  -- SAFE. returns NULL instead of failing on an out-of-range coordinate.
  IF(lat IS NULL OR lon IS NULL, NULL, SAFE.ST_GEOGPOINT(lon, lat)) AS event_point,
  damage_property,                -- USD as reported by NOAA (0 = none/unknown)
  source_rows,                    -- source rows merged (polygon corners)
  flood_cause,
  CURRENT_TIMESTAMP() AS refreshed_at
FROM `{project}.{dataset}.stg_noaa_flood_events`;

CREATE OR REPLACE TABLE `{project}.{dataset}.zip_points`
CLUSTER BY zip_code
OPTIONS (
  description = 'ZIP code internal points for the supported states (copy of bigquery-public-data.geo_us_boundaries.zip_codes). Refresh with: load_to_bigquery --steps reference',
  labels = [('app', 'claimdesk'), ('source', 'census')]
)
AS
SELECT
  zip_code,
  city,
  county,
  county_name_normalized,
  state_code,
  state_name,
  lat,
  lon,
  IF(lat IS NULL OR lon IS NULL, NULL, SAFE.ST_GEOGPOINT(lon, lat)) AS point,
  CURRENT_TIMESTAMP() AS refreshed_at
FROM `{project}.{dataset}.stg_zip_points`
WHERE TRUE
-- Defensive: exactly one row per ZIP so the app's lookup is unambiguous.
QUALIFY ROW_NUMBER() OVER (PARTITION BY zip_code ORDER BY county) = 1;

-- The staging copies are no longer needed (the Parquet files stay in GCS).
DROP TABLE IF EXISTS `{project}.{dataset}.stg_noaa_flood_events`;
DROP TABLE IF EXISTS `{project}.{dataset}.stg_zip_points`;
