-- =============================================================================
-- 00_create_dataset_and_tables.sql
--
-- WHAT THIS DOES
--   1. Creates the BigQuery *dataset* (BigQuery's word for a folder of
--      tables; in SQL it is called a SCHEMA) named {dataset}.
--   2. Creates the two tables the running app WRITES to:
--        * intake_packets       - one row per finished claim packet
--        * conversation_traces  - every turn / tool call of every call
--      (Written by webapp/handoff.py and webapp/trace_logger.py.)
--
-- SAFE TO RE-RUN
--   Everything uses "IF NOT EXISTS", so running it again never deletes data.
--
-- WHY LOCATION us-central1 (the value of the location placeholder)?
--   Project rule: EVERY resource (Cloud Run, Cloud Storage, Firestore,
--   BigQuery) lives in us-central1, so data never leaves the region.
--   A BigQuery query can only read tables that are all in the same location.
--   The NOAA storm events and ZIP code tables live in bigquery-public-data
--   in the US multi-region, so we cannot join them directly; instead the
--   "reference" step of load_to_bigquery.py copies the small subset we need
--   into this dataset (tables noaa_flood_events and zip_points; see
--   sql/reference/).
--   A dataset's location can NEVER be changed after creation (you would
--   have to delete and re-create it).
--
-- PLACEHOLDERS
--   {project}, {dataset} and {location} are replaced by load_to_bigquery.py
--   (or by you, if you paste this into the BigQuery console).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS `{project}.{dataset}`
OPTIONS (
  location = '{location}',
  description = 'ClaimDesk flood-claim intake demo. Real FEMA NFIP v3 policies/claims (redacted by FEMA; policy numbers and names are GENERATED and fictional) plus app output. This product uses the FEMA OpenFEMA API, but is not endorsed by FEMA.',
  labels = [('app', 'claimdesk')]
);

-- One row per adjuster hand-off packet. Partitioned by day so queries that
-- filter on created_at only scan the days they need (= cheaper).
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.intake_packets` (
  intake_id          STRING    OPTIONS (description = 'Server-generated intake id'),
  created_at         TIMESTAMP OPTIONS (description = 'When the packet was written (UTC)'),
  claim_type         STRING    OPTIONS (description = 'home_flood | internal_water | out_of_scope | unclear'),
  routing_decision   STRING    OPTIONS (description = 'ready_for_adjuster | needs_docs | policy_review | special_investigation | emergency_escalation | human_triage'),
  severity           STRING    OPTIONS (description = 'low | medium | high | urgent'),
  intake_status      STRING    OPTIONS (description = 'valid | missing_info'),
  policy_number      STRING    OPTIONS (description = 'Policy number as captured (generated, fictional)'),
  loss_state         STRING    OPTIONS (description = '2-letter state of the loss'),
  loss_zip_code      STRING    OPTIONS (description = '5-digit ZIP of the loss'),
  date_of_loss       DATE      OPTIONS (description = 'Date of loss as captured'),
  estimated_loss_usd FLOAT64   OPTIONS (description = 'Claimant estimate, USD'),
  missing_count      INT64     OPTIONS (description = 'Number of missing required facts'),
  packet_gcs_uri     STRING    OPTIONS (description = 'gs:// URI of the packet ZIP'),
  packet_json        STRING    OPTIONS (description = 'Full IntakePacket as JSON text')
)
PARTITION BY DATE(created_at)
OPTIONS (
  description = 'Adjuster hand-off packets written by the ClaimDesk app.',
  labels = [('app', 'claimdesk')]
);

-- Every conversation event. CLUSTER BY intake_id keeps one call's rows
-- physically together, so "show me intake X" reads very little data.
-- Partitions older than 30 days are deleted automatically (privacy + cost).
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.conversation_traces` (
  intake_id         STRING    OPTIONS (description = 'Intake this event belongs to'),
  event_time        TIMESTAMP OPTIONS (description = 'When the event happened (UTC)'),
  seq               INT64     OPTIONS (description = 'Order of the event within the intake'),
  event_type        STRING    OPTIONS (description = 'turn | tool_call | tool_result | system'),
  role              STRING    OPTIONS (description = 'user | agent | tool'),
  text              STRING    OPTIONS (description = 'Redacted transcript text'),
  tool_name         STRING    OPTIONS (description = 'Tool called, if any'),
  tool_args_json    STRING    OPTIONS (description = 'Tool arguments as JSON text'),
  tool_result_json  STRING    OPTIONS (description = 'Tool result as JSON text'),
  service_revision  STRING    OPTIONS (description = 'Cloud Run revision (K_REVISION) that served the call')
)
PARTITION BY DATE(event_time)
CLUSTER BY intake_id
OPTIONS (
  partition_expiration_days = 30,
  description = 'Conversation turns and tool calls (feeds live-agent evals). Auto-deleted after 30 days.',
  labels = [('app', 'claimdesk')]
);
