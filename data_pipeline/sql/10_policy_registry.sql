-- =============================================================================
-- 10_policy_registry.sql  ->  table `{project}.{dataset}.policy_registry`
--
-- WHAT THIS BUILDS
--   One row per REAL FEMA NFIP v3 policy term (from raw_nfip_policies), with
--   readable column names, decoded deductibles, a derived status and two
--   GENERATED identity fields:
--     * policy_number      'FLD-<STATE>-XXXXXX'   (fictional, deterministic)
--     * policyholder_name  'First Last'           (fictional, deterministic)
--   FEMA removes names and policy numbers for privacy, so we generate them
--   from each record's real `id`. Same id -> same number and name on every
--   rebuild, so demo scripts and evals stay repeatable.
--
-- WHO READS IT
--   claimdesk/data_access/policy_registry.py:
--     SELECT policy_number, policyholder_name, status, policy_line,
--            property_state, reported_city, reported_zip_code,
--            rated_flood_zone, CAST(effective_start AS STRING),
--            CAST(effective_end AS STRING), building_coverage_usd,
--            contents_coverage_usd, building_deductible_usd,
--            contents_deductible_usd, primary_residence, source_record_id
--     FROM policy_registry WHERE policy_number_key = @policy_key LIMIT 1
--   The STRING columns above are never NULL here (the app's PolicyRecord
--   model expects text), so NULLs become '' or the row is skipped.
--
-- VOICE-FRIENDLY CODES
--   The 6 characters come from the alphabet 23456789ABCDEFGHJKMNPQRSTVWXYZ
--   (no 0/O, 1/I/L, U - speech-to-text confuses them). 30^6 = 729 million
--   possible codes per state.
--
-- COLLISIONS
--   Two ids can (rarely) hash to the same code. We resolve that in up to two
--   extra rounds: the lowest id keeps the code, the others re-hash with a
--   different "salt". Anything still colliding after that (astronomically
--   unlikely) is dropped so policy_number is guaranteed UNIQUE.
--
-- STATUS IS A SNAPSHOT
--   'active' / 'pending' / 'expired' / 'cancelled' is computed against
--   CURRENT_DATE() at build time (see column status_as_of). 'pending' means
--   the term starts in the future (FEMA lists policies written ahead of their
--   effective date). Rebuild to refresh it. The app
--   compares coverage with the DATE OF LOSS using effective_start/end, so a
--   stale status never decides coverage on its own.
--
-- SCOPE
--   Residential occupancy types only (ClaimDesk v2 is a residential flood
--   desk): 1, 2, 3, 11, 12, 13, 14, 15, 16. See data_pipeline/nfip_codes.py.
--
-- Mirrors of every temp function below live in data_pipeline/nfip_codes.py
-- and are cross-checked by tests/test_data_pipeline.py.
-- This product uses the FEMA OpenFEMA API, but is not endorsed by FEMA.
-- =============================================================================

-- FEMA dates arrive as text like '2025-01-01T00:00:00.000Z'. Keep the day.
CREATE TEMP FUNCTION parse_day(ts STRING) AS (
  SAFE.PARSE_DATE('%Y-%m-%d', SUBSTR(ts, 1, 10))
);

-- Coded deductible -> dollars (FEMA data dictionary).
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

-- Occupancy code -> product line label. NULL = not residential (filtered out).
CREATE TEMP FUNCTION policy_line(occupancy INT64) AS (
  CASE occupancy
    WHEN 1 THEN 'NFIP Dwelling - Single Family'
    WHEN 11 THEN 'NFIP Dwelling - Single Family'
    WHEN 2 THEN 'NFIP Dwelling - 2-4 Family'
    WHEN 12 THEN 'NFIP Dwelling - 2-4 Family'
    WHEN 14 THEN 'NFIP Dwelling - Manufactured Home'
    WHEN 16 THEN 'NFIP Dwelling - Residential Unit'
    WHEN 3 THEN 'NFIP General Property - Other Residential'
    WHEN 13 THEN 'NFIP General Property - Other Residential'
    WHEN 15 THEN 'NFIP RCBAP - Condominium Association'
  END
);

-- FEMA redacts reportedCity ('NA'). Fall back to the public NFIP community
-- name: 'HOUSTON, CITY OF' -> 'Houston', 'HARDIN COUNTY *' -> 'Hardin County'.
CREATE TEMP FUNCTION clean_city(reported STRING, community STRING) AS (
  IF(
    UPPER(TRIM(IFNULL(reported, ''))) NOT IN ('', 'NA', 'N/A', 'CURRENTLY UNAVAILABLE', 'UNKNOWN'),
    INITCAP(TRIM(reported)),
    NULLIF(INITCAP(TRIM(REGEXP_REPLACE(REPLACE(IFNULL(community, ''), '*', ''), r',.*$', ''))), '')
  )
);

-- Stable 64-bit hash of (record id, salt). FARM_FINGERPRINT never changes
-- between runs or projects, which is what makes the identities repeatable.
CREATE TEMP FUNCTION policy_fp(record_id INT64, salt INT64) AS (
  FARM_FINGERPRINT(CONCAT('claimdesk-policy:', CAST(record_id AS STRING), ':', CAST(salt AS STRING)))
);

-- 64-bit hash -> 6 characters: n = ABS(MOD(fp, 30^6)), written in base 30,
-- most significant digit first (python: nfip_codes.policy_code_from_fingerprint).
CREATE TEMP FUNCTION policy_code(fp INT64) AS ((
  SELECT STRING_AGG(
           SUBSTR('23456789ABCDEFGHJKMNPQRSTVWXYZ',
                  MOD(DIV(ABS(MOD(fp, 729000000)), CAST(POW(30, k) AS INT64)), 30) + 1, 1),
           '' ORDER BY k DESC)
  FROM UNNEST(GENERATE_ARRAY(0, 5)) AS k
));

CREATE TEMP FUNCTION make_policy_number(state STRING, record_id INT64, salt INT64) AS (
  CONCAT('FLD-', state, '-', policy_code(policy_fp(record_id, salt)))
);

-- Same rule as claimdesk normalize_policy_number: uppercase, keep A-Z and 0-9.
CREATE TEMP FUNCTION policy_key(policy_number STRING) AS (
  REGEXP_REPLACE(UPPER(policy_number), r'[^A-Z0-9]', '')
);

-- Fictional names (python: nfip_codes.FIRST_NAMES / LAST_NAMES). Keep the
-- lists identical in both places - a unit test checks it.
CREATE TEMP FUNCTION first_names() AS ([
  'Avery', 'Blake', 'Carmen', 'Dana', 'Elena', 'Felix', 'Grace', 'Hector',
  'Iris', 'Jonah', 'Keira', 'Lucas', 'Maya', 'Nolan', 'Olivia', 'Priya',
  'Quinn', 'Rosa', 'Samuel', 'Tessa', 'Victor', 'Wendy', 'Xavier', 'Yara',
  'Zane', 'Amara', 'Brooke', 'Caleb', 'Dmitri', 'Esther', 'Farah', 'Gavin',
  'Hana', 'Isaac', 'Jade', 'Kofi', 'Lena', 'Marcus', 'Nadia', 'Omar'
]);
CREATE TEMP FUNCTION last_names() AS ([
  'Alvarez', 'Bennett', 'Castillo', 'Dawson', 'Ellison', 'Fischer', 'Garner',
  'Holloway', 'Ibarra', 'Jennings', 'Kowalski', 'Lindqvist', 'Mercer',
  'Navarro', 'Okafor', 'Prescott', 'Quintero', 'Ramsey', 'Sorensen',
  'Thornton', 'Underwood', 'Valdez', 'Whitaker', 'Yamamoto', 'Zeller',
  'Ashby', 'Brennan', 'Calloway', 'Delgado', 'Everett', 'Fairbanks',
  'Galloway', 'Hartley', 'Iverson', 'Kendrick', 'Langston', 'Montoya',
  'Pemberton', 'Radcliffe', 'Sterling'
]);
CREATE TEMP FUNCTION fictional_name(record_id INT64) AS (
  CONCAT(
    first_names()[OFFSET(ABS(MOD(FARM_FINGERPRINT(CONCAT('claimdesk-first:', CAST(record_id AS STRING))), ARRAY_LENGTH(first_names()))))],
    ' ',
    last_names()[OFFSET(ABS(MOD(FARM_FINGERPRINT(CONCAT('claimdesk-last:', CAST(record_id AS STRING))), ARRAY_LENGTH(last_names()))))]
  )
);

CREATE OR REPLACE TABLE `{project}.{dataset}.policy_registry`
CLUSTER BY policy_number_key
OPTIONS (
  description = 'Real FEMA NFIP v3 residential policy terms (2025+) with GENERATED fictional policy_number and policyholder_name. Looked up by policy_number_key. This product uses the FEMA OpenFEMA API, but is not endorsed by FEMA.',
  labels = [('app', 'claimdesk')]
)
AS
WITH src AS (
  -- Clean + filter the raw rows. QUALIFY keeps one row per id in case the
  -- same record was downloaded twice (e.g. FEMA refreshed data mid-download).
  SELECT
    id,
    UPPER(TRIM(propertyState)) AS property_state,
    COALESCE(clean_city(reportedCity, nfipCommunityName), '') AS reported_city,
    REGEXP_EXTRACT(TRIM(reportedZipCode), r'^\d{5}') AS reported_zip_code,
    COALESCE(NULLIF(UPPER(TRIM(ratedFloodZone)), ''), '') AS rated_flood_zone,
    parse_day(policyEffectiveDate) AS effective_start,
    parse_day(policyTerminationDate) AS effective_end,
    parse_day(cancellationDateOfFloodPolicy) AS cancellation_date,
    policy_line(occupancyType) AS policy_line,
    occupancyType AS occupancy_type,
    CAST(ROUND(totalBuildingInsuranceCoverage) AS INT64) AS building_coverage_usd,
    CAST(ROUND(totalContentsInsuranceCoverage) AS INT64) AS contents_coverage_usd,
    decode_deductible(buildingDeductibleCode) AS building_deductible_usd,
    decode_deductible(contentsDeductibleCode) AS contents_deductible_usd,
    primaryResidenceIndicator AS primary_residence,
    nfipCommunityName AS nfip_community_name
  FROM `{project}.{dataset}.raw_nfip_policies`
  WHERE id IS NOT NULL
    AND REGEXP_CONTAINS(UPPER(TRIM(propertyState)), r'^[A-Z]{2}$')
    AND policy_line(occupancyType) IS NOT NULL                 -- residential only
    AND parse_day(policyEffectiveDate) IS NOT NULL
    AND parse_day(policyTerminationDate) IS NOT NULL
    AND REGEXP_CONTAINS(IFNULL(reportedZipCode, ''), r'^\s*\d{5}')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY asOfDate DESC) = 1
),

-- Round 0: everyone gets the salt-0 number; lowest id wins a collision.
round0 AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY policy_number ORDER BY id) AS rn
  FROM (SELECT *, make_policy_number(property_state, id, 0) AS policy_number FROM src)
),
kept0 AS (SELECT * EXCEPT (rn) FROM round0 WHERE rn = 1),

-- Round 1: losers re-hash with salt 1; already-kept numbers always win.
round1 AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY policy_number ORDER BY priority, id) AS rn
  FROM (
    SELECT *, 0 AS priority FROM kept0
    UNION ALL
    SELECT * EXCEPT (rn, policy_number), make_policy_number(property_state, id, 1), 1 FROM round0 WHERE rn > 1
  )
),
kept1 AS (SELECT * EXCEPT (rn, priority) FROM round1 WHERE rn = 1),

-- Round 2: same again with salt 2. Remaining collisions (if any) are dropped.
round2 AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY policy_number ORDER BY priority, id) AS rn
  FROM (
    SELECT *, 0 AS priority FROM kept1
    UNION ALL
    SELECT * EXCEPT (rn, priority, policy_number), make_policy_number(property_state, id, 2), 1 FROM round1 WHERE rn > 1 AND priority = 1
  )
),
numbered AS (SELECT * EXCEPT (rn, priority) FROM round2 WHERE rn = 1)

SELECT
  policy_number,
  policy_key(policy_number)                         AS policy_number_key,
  fictional_name(id)                                AS policyholder_name,
  CASE
    WHEN cancellation_date IS NOT NULL AND cancellation_date <= CURRENT_DATE() THEN 'cancelled'
    WHEN effective_end < CURRENT_DATE() THEN 'expired'
    WHEN effective_start > CURRENT_DATE() THEN 'pending'
    ELSE 'active'
  END                                               AS status,
  policy_line,
  property_state,
  reported_city,
  reported_zip_code,
  rated_flood_zone,
  effective_start,
  effective_end,
  building_coverage_usd,
  contents_coverage_usd,
  building_deductible_usd,
  contents_deductible_usd,
  primary_residence,
  CAST(id AS STRING)                                AS source_record_id,
  -- Extra context columns (not read by the app today, handy for analysis):
  cancellation_date,
  occupancy_type,
  nfip_community_name,
  CURRENT_DATE()                                    AS status_as_of
FROM numbered;
