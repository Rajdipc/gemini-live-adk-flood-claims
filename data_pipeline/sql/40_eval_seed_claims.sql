-- =============================================================================
-- 40_eval_seed_claims.sql  ->  table `{project}.{dataset}.eval_seed_claims`
--
-- WHAT THIS BUILDS
--   ~200 REAL residential NFIP claims, picked so that every state, every kind
--   of flood (coastal / riverine / rainfall / other) and both outcomes
--   (paid / not paid) are represented. Each claim is paired with a policy
--   from policy_registry so an eval conversation can quote a policy number
--   that the app will actually find.
--
-- WHO READS IT
--   evals/build_eval_cases.py turns each row into a claimant narrative +
--   expected answers. Column meanings: docs/data_dictionary.md.
--
-- HOW THE SAMPLE IS PICKED (deterministic - same data -> same 200 rows)
--   1. Candidates: residential claims from nfip_claims_clean with a loss
--      date (not in the future), a 5-digit ZIP, and no administrative
--      non-payment code (98 deleted / 99 erroneous assignment).
--   2. Stratum = state | cause_group | paid-or-not  (5 x 4 x 2 = 40 strata).
--   3. Inside each stratum, claims with a loss date in 2025+ come first
--      (the registry only holds 2025+ policy terms, so those can be matched
--      to a policy that really covered the loss date); ties are broken by a
--      stable hash of the claim id (a "random but repeatable" order).
--   4. Round-robin across strata (every stratum's #1, then every #2, ...)
--      until 200 rows.
--
-- HOW THE POLICY IS MATCHED (best available, recorded in match_level)
--   'zip_and_term'   same state + same ZIP, loss date inside the policy term
--   'state_and_term' same state, loss date inside the policy term
--   'zip_only'       same state + same ZIP, term does NOT cover the loss date
--                    (useful as a "loss outside policy term" edge case)
--   'state_only'     same state only: different ZIP AND the term does NOT
--                    cover the loss date (last resort; treat like zip_only
--                    for the term, and have the caller use the policy's ZIP)
--   Cancelled policies are used only if nothing else matches.
--   A claim with no policy at all in its state is dropped.
--
-- This product uses the FEMA OpenFEMA API, but is not endorsed by FEMA.
-- =============================================================================

CREATE OR REPLACE TABLE `{project}.{dataset}.eval_seed_claims`
OPTIONS (
  description = 'Stratified sample of ~200 real FEMA NFIP v3 residential claims, each paired with a policy_registry policy (generated identity) for eval cases. This product uses the FEMA OpenFEMA API, but is not endorsed by FEMA.',
  labels = [('app', 'claimdesk')]
)
AS
WITH candidates AS (
  SELECT
    c.*,
    CONCAT(c.state, '|', c.cause_group, '|', IF(c.amount_paid_usd > 0, 'paid', 'not_paid')) AS stratum,
    c.date_of_loss >= DATE '2025-01-01'                                                   AS loss_in_registry_era
  FROM `{project}.{dataset}.nfip_claims_clean` AS c
  WHERE c.is_residential
    AND c.date_of_loss IS NOT NULL
    AND c.date_of_loss <= CURRENT_DATE()
    AND c.reported_zip_code IS NOT NULL
    -- 98/99 = administrative codes (claim deleted / erroneous assignment):
    -- not a real claim outcome, so useless as an eval scenario.
    AND IFNULL(c.non_payment_reason_building_code, '') NOT IN ('98', '99')
    -- Only states that actually have registry policies.
    AND c.state IN (SELECT DISTINCT property_state FROM `{project}.{dataset}.policy_registry`)
),

ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY stratum
      ORDER BY IF(loss_in_registry_era, 0, 1), FARM_FINGERPRINT(CONCAT('claimdesk-eval:', CAST(source_claim_id AS STRING)))
    ) AS stratum_rank
  FROM candidates
),

sampled AS (
  SELECT *
  FROM ranked
  WHERE stratum_rank <= 10
  QUALIFY ROW_NUMBER() OVER (
    ORDER BY stratum_rank, FARM_FINGERPRINT(CONCAT('claimdesk-eval-order:', CAST(source_claim_id AS STRING)))
  ) <= 200
),

matched AS (
  SELECT
    s.source_claim_id,
    p.policy_number,
    p.policyholder_name,
    p.status,
    p.policy_line,
    p.effective_start,
    p.effective_end,
    p.reported_city            AS policy_city,
    p.reported_zip_code        AS policy_zip_code,
    p.building_coverage_usd,
    p.contents_coverage_usd,
    p.building_deductible_usd,
    p.contents_deductible_usd,
    s.date_of_loss BETWEEN p.effective_start AND p.effective_end AS loss_within_policy_term,
    CASE
      WHEN p.reported_zip_code = s.reported_zip_code AND s.date_of_loss BETWEEN p.effective_start AND p.effective_end THEN 'zip_and_term'
      WHEN s.date_of_loss BETWEEN p.effective_start AND p.effective_end THEN 'state_and_term'
      WHEN p.reported_zip_code = s.reported_zip_code THEN 'zip_only'
      ELSE 'state_only'
    END AS match_level
  FROM sampled AS s
  JOIN `{project}.{dataset}.policy_registry` AS p
    ON p.property_state = s.state
  WHERE TRUE
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY s.source_claim_id
    ORDER BY
      CASE match_level WHEN 'zip_and_term' THEN 1 WHEN 'state_and_term' THEN 2 WHEN 'zip_only' THEN 3 ELSE 4 END,
      IF(p.status = 'cancelled', 1, 0),
      FARM_FINGERPRINT(CONCAT(CAST(s.source_claim_id AS STRING), ':', p.policy_number))
  ) = 1
)

SELECT
  CONCAT('nfip-', CAST(s.source_claim_id AS STRING))  AS eval_case_id,
  s.source_claim_id,
  s.stratum,
  -- What happened (REAL FEMA facts)
  s.state,
  s.date_of_loss,
  s.open_date,
  s.report_lag_days,
  s.reported_city,
  s.reported_zip_code,
  s.county_code,
  s.cause_of_damage_primary_code,
  s.cause_of_damage,
  s.cause_group,
  s.flood_event,
  s.water_depth_inches,
  s.water_depth_inches                         AS water_depth,         -- short alias (same value)
  s.building_damage_usd,
  s.contents_damage_usd,
  s.total_damage_usd,
  s.amount_paid_building_usd,
  s.amount_paid_contents_usd,
  s.amount_paid_usd,
  s.claim_outcome,
  s.non_payment_reason_building_code,
  s.non_payment_reason_building,
  s.non_payment_reason_building                AS non_payment_reason,  -- short alias (same value)
  s.non_payment_reason_contents,
  s.rated_flood_zone,
  s.occupancy_type,
  s.primary_residence,
  -- Which registry policy to use in the conversation (GENERATED identity)
  m.policy_number                 AS matched_policy_number,
  m.policyholder_name             AS matched_policyholder_name,
  m.status                        AS matched_policy_status,
  m.policy_line                   AS matched_policy_line,
  m.effective_start               AS matched_policy_effective_start,
  m.effective_end                 AS matched_policy_effective_end,
  m.policy_city                   AS matched_policy_city,
  m.policy_zip_code               AS matched_policy_zip_code,
  m.building_coverage_usd         AS matched_building_coverage_usd,
  m.contents_coverage_usd         AS matched_contents_coverage_usd,
  m.building_deductible_usd       AS matched_building_deductible_usd,
  m.contents_deductible_usd       AS matched_contents_deductible_usd,
  m.loss_within_policy_term,
  m.match_level
FROM sampled AS s
JOIN matched AS m USING (source_claim_id)
ORDER BY s.state, s.stratum, s.date_of_loss;
