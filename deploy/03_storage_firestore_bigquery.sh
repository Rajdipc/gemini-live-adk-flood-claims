#!/usr/bin/env bash
# =============================================================================
# 03_storage_firestore_bigquery.sh - the three data stores.
# =============================================================================
#   1. Cloud Storage bucket (us-central1) - evidence photos, sketches, packet
#      ZIPs, raw FEMA downloads. PRIVATE:
#        * --public-access-prevention : nobody can ever make an object public
#        * --uniform-bucket-level-access : IAM only, no per-object ACLs
#      The app streams files to the browser itself; no signed URLs are issued.
#      A lifecycle rule deletes intake files after 30 days (demo data hygiene).
#   2. Firestore (Native mode, us-central1) - intake state while a call is in
#      progress, so a container restart does not lose the claim. A TTL policy
#      on field `expires_at` deletes old intakes automatically.
#   3. BigQuery dataset "claimdesk" in us-central1 (same region) - created by
#      data_pipeline/sql/00_create_dataset_and_tables.sql (see
#      docs/data_loading.md). This script only grants the service account
#      access to it once it exists.
#
# NOTE: No Pub/Sub topic - packets are archived to GCS + BigQuery directly.
# =============================================================================
set -euo pipefail
: "${PROJECT_ID:?source deploy/00_variables.sh first}"

# ---- 1. Cloud Storage -------------------------------------------------------
gcloud storage buckets create "gs://${BUCKET}" \
  --location="${REGION}" \
  --uniform-bucket-level-access \
  --public-access-prevention || echo "(bucket exists)"

cat > /tmp/claimdesk-lifecycle.json <<'JSON'
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 30, "matchesPrefix": ["intakes/"]}}]}
JSON
gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file=/tmp/claimdesk-lifecycle.json

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${RUN_SA}" --role="roles/storage.objectAdmin" >/dev/null
echo "bucket gs://${BUCKET} ready (private, 30-day lifecycle on intakes/)"

# ---- 2. Firestore -------------------------------------------------------------
gcloud firestore databases create --database="${FIRESTORE_DB}" \
  --location="${REGION}" --type=firestore-native || echo "(firestore database exists)"

# TTL: Firestore deletes documents in collection "intakes" after `expires_at`.
gcloud firestore fields ttls update expires_at \
  --collection-group=intakes --enable-ttl --database="${FIRESTORE_DB}" --async || true
echo "firestore ${FIRESTORE_DB} ready (TTL on intakes.expires_at)"

# ---- 3. BigQuery access (dataset created by the data pipeline) ------------------
if bq --location="${BQ_LOCATION}" show --format=none "${PROJECT_ID}:${BQ_DATASET}" 2>/dev/null; then
  # Dataset-scoped role: the service account can read/write ONLY this dataset.
  bq query --use_legacy_sql=false --location="${BQ_LOCATION}" \
    "GRANT \`roles/bigquery.dataEditor\` ON SCHEMA \`${PROJECT_ID}.${BQ_DATASET}\` TO 'serviceAccount:${RUN_SA}'"
  echo "granted dataEditor on ${BQ_DATASET}"
else
  echo "Dataset ${BQ_DATASET} not found yet. Load data first (docs/data_loading.md), then re-run this script."
fi
