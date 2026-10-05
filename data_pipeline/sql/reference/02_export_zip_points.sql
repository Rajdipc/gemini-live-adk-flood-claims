-- ===========================================================================
-- REFERENCE STEP 2/3: copy ZIP code points -> our Cloud Storage bucket
-- ===========================================================================
-- WHAT THIS DOES
--   Reads the public ZIP code table
--   (bigquery-public-data.geo_us_boundaries.zip_codes, US multi-region) for
--   our supported states only and writes it as Parquet files to
--       gs://{bucket}/reference/zip_points/run={run_id}/part-*.parquet
--   Step 3 loads the files into `zip_points` in our us-central1 dataset.
--   Same reason as file 01: a us-central1 dataset cannot JOIN a US table.
--
-- RUN THIS JOB WITH LOCATION = 'US' (the Python loader does this for you).
--
-- WHAT WE KEEP
--   Only the ZIP's "internal point" (a lat/lon inside the ZIP area), not the
--   full polygon: the weather check needs a point for its distance test,
--   and polygons would make the table ~1000x bigger.
--
-- COUNTY NAME NORMALIZATION (must match file 03 / the NOAA cz_name)
--   "St. Charles Parish" -> strip suffix -> "St. Charles" -> uppercase ->
--   keep letters only -> "STCHARLES". NOAA writes "ST. CHARLES", which
--   normalizes to the same key; "DeSoto County" and "DE SOTO" both become
--   "DESOTO".
--
-- COST: ~5k rows for 5 states; the read scans a few MB (free tier).
--
-- PLACEHOLDERS: {bucket} {run_id} {states}
-- ===========================================================================
EXPORT DATA OPTIONS (
  uri = 'gs://{bucket}/reference/zip_points/run={run_id}/part-*.parquet',
  format = 'PARQUET',
  overwrite = true
) AS
SELECT
  zip_code,
  -- Census place names carry a type suffix ("Luling CDP", "Bellville city").
  REGEXP_REPLACE(city, r' (city|town|village|CDP|borough|municipality)$', '') AS city,
  county,
  REGEXP_REPLACE(
    UPPER(REGEXP_REPLACE(county, r' (County|Parish|Borough|Census Area|Municipality|city)$', '')),
    r'[^A-Z]', ''
  ) AS county_name_normalized,
  state_code,
  state_name,
  internal_point_lat AS lat,
  internal_point_lon AS lon
FROM `bigquery-public-data.geo_us_boundaries.zip_codes`
WHERE state_code IN UNNEST({states})
