-- =============================================================================
-- 20_loss_benchmarks.sql  ->  table `{project}.{dataset}.loss_benchmarks`
--
-- WHAT THIS BUILDS
--   One row per state (CO, TX, FL, LA, NC) plus one row with state = 'ALL',
--   summarising REAL NFIP residential claims with a loss date in 2015+:
--
--     damage_p50/p90/p95_usd  percentiles of buildingDamageAmount +
--                             contentsDamageAmount, only claims with damage > 0
--     sample_size             how many claims those damage percentiles use
--     report_lag_p95_days     95th percentile of (openDate - dateOfLoss) in days
--
-- WHO READS IT
--   claimdesk/data_access/loss_benchmarks.py:
--     SELECT state, sample_size, damage_p50_usd, damage_p90_usd,
--            damage_p95_usd, report_lag_p95_days
--     FROM loss_benchmarks WHERE state IN (@state, 'ALL') ...
--   and rules/risk_signals.py uses them as "unusually high estimate" and
--   "unusually late report" thresholds (soft signals, never denials).
--
-- WHY APPROX_QUANTILES?
--   Exact percentiles need a full sort; APPROX_QUANTILES(x, 100) returns 101
--   cut points in one cheap pass. [OFFSET(95)] is the 95th percentile.
--   The approximation error is tiny for hundreds of thousands of rows.
--
-- This product uses the FEMA OpenFEMA API, but is not endorsed by FEMA.
-- =============================================================================

CREATE TEMP FUNCTION parse_day(ts STRING) AS (
  SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(ts, 1, 10))
);

CREATE OR REPLACE TABLE `{project}.{dataset}.loss_benchmarks`
OPTIONS (
  description = 'Per-state (and ALL) damage and report-lag percentiles from real FEMA NFIP v3 residential claims, loss date 2015+. This product uses the FEMA OpenFEMA API, but is not endorsed by FEMA.',
  labels = [('app', 'claimdesk')]
)
AS
WITH claims AS (
  SELECT
    UPPER(TRIM(state))                                           AS state,
    parse_day(dateOfLoss)                                        AS date_of_loss,
    parse_day(openDate)                                          AS open_date,
    IFNULL(buildingDamageAmount, 0) + IFNULL(contentsDamageAmount, 0) AS total_damage_usd
  FROM `{project}.{dataset}.raw_nfip_claims`
  WHERE id IS NOT NULL
    AND REGEXP_CONTAINS(UPPER(TRIM(state)), r'^[A-Z]{2}$')
    AND occupancyType IN (1, 2, 3, 11, 12, 13, 14, 15, 16)   -- residential only
    AND parse_day(dateOfLoss) >= DATE '2015-01-01'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY asOfDate DESC) = 1
),

-- Each claim is counted twice: once for its own state, once for 'ALL'.
scoped AS (
  SELECT state AS scope, * EXCEPT (state) FROM claims
  UNION ALL
  SELECT 'ALL' AS scope, * EXCEPT (state) FROM claims
),

damage AS (
  SELECT
    scope,
    COUNT(*)                                  AS sample_size,
    APPROX_QUANTILES(total_damage_usd, 100)   AS q
  FROM scoped
  WHERE total_damage_usd > 0
  GROUP BY scope
),

report_lag AS (
  SELECT
    scope,
    COUNT(*)                                                   AS lag_sample_size,
    APPROX_QUANTILES(DATE_DIFF(open_date, date_of_loss, DAY), 100) AS q
  FROM scoped
  WHERE open_date IS NOT NULL
    AND date_of_loss IS NOT NULL
    -- Ignore data-entry errors: negative lags and lags over 10 years.
    AND DATE_DIFF(open_date, date_of_loss, DAY) BETWEEN 0 AND 3650
  GROUP BY scope
),

coverage_window AS (
  SELECT scope, MIN(date_of_loss) AS losses_from, MAX(date_of_loss) AS losses_to
  FROM scoped
  GROUP BY scope
)

SELECT
  d.scope                                    AS state,
  d.sample_size                              AS sample_size,
  CAST(d.q[OFFSET(50)] AS FLOAT64)           AS damage_p50_usd,
  CAST(d.q[OFFSET(90)] AS FLOAT64)           AS damage_p90_usd,
  CAST(d.q[OFFSET(95)] AS FLOAT64)           AS damage_p95_usd,
  CAST(l.q[OFFSET(95)] AS FLOAT64)           AS report_lag_p95_days,
  -- Extra context columns (not read by the app today):
  CAST(l.q[OFFSET(50)] AS FLOAT64)           AS report_lag_p50_days,
  l.lag_sample_size                          AS report_lag_sample_size,
  w.losses_from,
  w.losses_to,
  CURRENT_TIMESTAMP()                        AS built_at
FROM damage AS d
LEFT JOIN report_lag AS l USING (scope)
LEFT JOIN coverage_window AS w USING (scope);
