-- =============================================================================
-- 30_claims_reference.sql  ->  table `{project}.{dataset}.nfip_claims_clean`
--
-- WHAT THIS BUILDS
--   A tidy copy of raw_nfip_claims (ALL occupancy types, loss date 2015+)
--   with snake_case names, real DATE columns and FEMA codes decoded into
--   readable labels:
--     * causeOfDamage             -> cause_of_damage   ("Stream, river, or lake overflow")
--     * nonPaymentReasonBuilding  -> non_payment_reason_building ("Seepage (not a flood)")
--   It is a reference table for analysis, for docs/blog examples and for
--   40_eval_seed_claims.sql. The running app does not query it.
--
-- MULTIPLE CAUSE CODES
--   FEMA allows more than one cause code on a claim, e.g. '2D' = river
--   overflow + remote adjustment. cause_of_damage_primary_code is the first
--   *physical* cause (0-9 or A); B/C/D only describe how the claim was
--   handled, so they are used only when nothing else is present.
--
-- Mirrors of the code tables live in data_pipeline/nfip_codes.py (tested).
-- This product uses the FEMA OpenFEMA API, but is not endorsed by FEMA.
-- =============================================================================

CREATE TEMP FUNCTION parse_day(ts STRING) AS (
  SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(ts, 1, 10))
);

CREATE TEMP FUNCTION decode_deductible(code STRING) AS (
  CASE UPPER(TRIM(code))
    WHEN '0' THEN 500
    WHEN '1' THEN 1000
    WHEN '2' THEN 2000
    WHEN '3' THEN 3000
    WHEN '4' THEN 4000
    WHEN '5' THEN 5000
    WHEN '9' THEN 750
    WHEN 'A' THEN 10000
    WHEN 'B' THEN 15000
    WHEN 'C' THEN 20000
    WHEN 'D' THEN 25000
    WHEN 'E' THEN 50000
    WHEN 'F' THEN 1250
    WHEN 'G' THEN 1500
    WHEN 'H' THEN 200
  END
);

-- '2D' -> '2', 'D' -> 'D', NULL -> NULL
CREATE TEMP FUNCTION primary_cause(code STRING) AS (
  COALESCE(REGEXP_EXTRACT(UPPER(code), r'[0-9A]'), REGEXP_EXTRACT(UPPER(code), r'[B-D]'))
);

CREATE TEMP FUNCTION cause_label(code STRING) AS (
  CASE code
    WHEN '0' THEN 'Other causes'
    WHEN '1' THEN 'Tidal water overflow'
    WHEN '2' THEN 'Stream, river, or lake overflow'
    WHEN '3' THEN 'Alluvial fan overflow'
    WHEN '4' THEN 'Accumulation of rainfall or snowmelt'
    WHEN '7' THEN 'Erosion - demolition'
    WHEN '8' THEN 'Erosion - removal'
    WHEN '9' THEN 'Earth movement, landslide, land subsidence, sinkholes'
    WHEN 'A' THEN 'Closed basin lake'
    WHEN 'B' THEN 'Expedited claim handling - without site inspection'
    WHEN 'C' THEN 'Expedited claim handling - follow-up site inspection'
    WHEN 'D' THEN 'Expedited claim handling - remote adjustment pilot'
    ELSE IF(code IS NULL, NULL, CONCAT('Unknown cause (code ', code, ')'))
  END
);

-- Coarse groups used to stratify the eval sample.
CREATE TEMP FUNCTION cause_group(code STRING) AS (
  CASE code
    WHEN '1' THEN 'coastal'
    WHEN '2' THEN 'riverine'
    WHEN '3' THEN 'riverine'
    WHEN 'A' THEN 'riverine'
    WHEN '4' THEN 'rainfall'
    ELSE 'other'
  END
);

-- Always 2 characters: '1' -> '01'.
CREATE TEMP FUNCTION non_payment_code(code STRING) AS (
  IF(NULLIF(TRIM(code), '') IS NULL, NULL, LPAD(TRIM(code), 2, '0'))
);

CREATE TEMP FUNCTION non_payment_label(code STRING) AS (
  CASE code
    WHEN '01' THEN 'Damage below deductible'
    WHEN '02' THEN 'Seepage (not a flood)'
    WHEN '03' THEN 'Backup of drains (not a flood)'
    WHEN '04' THEN 'Shrubs not covered'
    WHEN '05' THEN 'Sea wall'
    WHEN '06' THEN 'Not an actual flood'
    WHEN '07' THEN 'Loss in progress'
    WHEN '08' THEN 'Failure to pursue claim'
    WHEN '09' THEN 'Debris removal only'
    WHEN '10' THEN 'Fire'
    WHEN '11' THEN 'Fence damage'
    WHEN '12' THEN 'Hydrostatic pressure'
    WHEN '13' THEN 'Drainage clogged'
    WHEN '14' THEN 'Boat piers'
    WHEN '15' THEN 'Damage occurred before policy inception'
    WHEN '16' THEN 'Wind damage (not flood)'
    WHEN '17' THEN 'Erosion type not included in flood definition'
    WHEN '18' THEN 'Landslide'
    WHEN '19' THEN 'Mudflow type not included in flood definition'
    WHEN '20' THEN 'No demonstrable damage'
    WHEN '97' THEN 'Other'
    WHEN '98' THEN 'Error - claim deleted'
    WHEN '99' THEN 'Erroneous assignment'
    ELSE IF(code IS NULL, NULL, CONCAT('Other non-payment reason (code ', code, ')'))
  END
);

CREATE TEMP FUNCTION clean_city(reported STRING, community STRING) AS (
  IF(
    UPPER(TRIM(IFNULL(reported, ''))) NOT IN ('', 'NA', 'N/A', 'CURRENTLY UNAVAILABLE', 'UNKNOWN'),
    INITCAP(TRIM(reported)),
    NULLIF(INITCAP(TRIM(REGEXP_REPLACE(REPLACE(IFNULL(community, ''), '*', ''), r',.*$', ''))), '')
  )
);

CREATE OR REPLACE TABLE `{project}.{dataset}.nfip_claims_clean`
CLUSTER BY state, reported_zip_code
OPTIONS (
  description = 'Cleaned real FEMA NFIP v3 claims (loss date 2015+) with decoded cause-of-damage and non-payment labels. This product uses the FEMA OpenFEMA API, but is not endorsed by FEMA.',
  labels = [('app', 'claimdesk')]
)
AS
WITH base AS (
  SELECT
    *,
    primary_cause(causeOfDamage)                AS cause_code,
    non_payment_code(nonPaymentReasonBuilding)  AS np_building,
    non_payment_code(nonPaymentReasonContents)  AS np_contents
  FROM `{project}.{dataset}.raw_nfip_claims`
  WHERE id IS NOT NULL
    AND parse_day(dateOfLoss) >= DATE '2015-01-01'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY asOfDate DESC) = 1
)
SELECT
  id                                                          AS source_claim_id,
  UPPER(TRIM(state))                                          AS state,
  parse_day(dateOfLoss)                                       AS date_of_loss,
  yearOfLoss                                                  AS year_of_loss,
  parse_day(openDate)                                         AS open_date,
  DATE_DIFF(parse_day(openDate), parse_day(dateOfLoss), DAY)  AS report_lag_days,
  clean_city(reportedCity, nfipCommunityName)                 AS reported_city,
  REGEXP_EXTRACT(TRIM(reportedZipCode), r'^\d{5}')            AS reported_zip_code,
  countyCode                                                  AS county_code,
  censusGeoid                                                 AS census_geoid,
  nfipCommunityName                                           AS nfip_community_name,
  latitude,
  longitude,
  causeOfDamage                                               AS cause_of_damage_raw,
  cause_code                                                  AS cause_of_damage_primary_code,
  cause_label(cause_code)                                     AS cause_of_damage,
  cause_group(cause_code)                                     AS cause_group,
  NULLIF(TRIM(floodEvent), '')                                AS flood_event,
  waterDepth                                                  AS water_depth_inches,
  floodWaterDuration                                          AS flood_water_duration_hours,
  buildingDamageAmount                                        AS building_damage_usd,
  contentsDamageAmount                                        AS contents_damage_usd,
  IFNULL(buildingDamageAmount, 0) + IFNULL(contentsDamageAmount, 0) AS total_damage_usd,
  amountPaidOnBuildingClaim                                   AS amount_paid_building_usd,
  amountPaidOnContentsClaim                                   AS amount_paid_contents_usd,
  amountPaidOnIncreasedCostOfComplianceClaim                  AS amount_paid_icc_usd,
  IFNULL(amountPaidOnBuildingClaim, 0) + IFNULL(amountPaidOnContentsClaim, 0) AS amount_paid_usd,
  np_building                                                 AS non_payment_reason_building_code,
  non_payment_label(np_building)                              AS non_payment_reason_building,
  np_contents                                                 AS non_payment_reason_contents_code,
  non_payment_label(np_contents)                              AS non_payment_reason_contents,
  CASE
    WHEN IFNULL(amountPaidOnBuildingClaim, 0) + IFNULL(amountPaidOnContentsClaim, 0) > 0 THEN 'paid'
    WHEN np_building IS NOT NULL OR np_contents IS NOT NULL THEN 'closed_without_payment'
    ELSE 'unknown'
  END                                                         AS claim_outcome,
  NULLIF(UPPER(TRIM(ratedFloodZone)), '')                     AS rated_flood_zone,
  occupancyType                                               AS occupancy_type,
  occupancyType IN (1, 2, 3, 11, 12, 13, 14, 15, 16)          AS is_residential,
  primaryResidenceIndicator                                   AS primary_residence,
  CAST(ROUND(totalBuildingInsuranceCoverage) AS INT64)        AS building_coverage_usd,
  CAST(ROUND(totalContentsInsuranceCoverage) AS INT64)        AS contents_coverage_usd,
  decode_deductible(buildingDeductibleCode)                   AS building_deductible_usd,
  decode_deductible(contentsDeductibleCode)                   AS contents_deductible_usd,
  parse_day(asOfDate)                                         AS fema_as_of_date
FROM base;
