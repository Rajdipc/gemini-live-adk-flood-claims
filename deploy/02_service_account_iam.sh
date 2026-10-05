#!/usr/bin/env bash
# =============================================================================
# 02_service_account_iam.sh - a dedicated identity for the Cloud Run service.
# =============================================================================
# WHY A DEDICATED SERVICE ACCOUNT?
#   By default Cloud Run uses the "Compute Engine default service account",
#   which usually has the very broad Editor role. Best practice is *least
#   privilege*: a service account that can do only what ClaimDesk needs.
#
# ROLES (and why)
#   Project level:
#     roles/aiplatform.user        call Gemini models on Vertex AI
#     roles/bigquery.jobUser       run BigQuery queries (jobs are project-level)
#     roles/datastore.user         read/write Firestore documents
#     roles/logging.logWriter      write structured logs
#     roles/cloudtrace.agent       send trace spans
#     roles/monitoring.metricWriter  (optional) custom metrics
#     roles/discoveryengine.viewer   query the Vertex AI Search engine (grounding)
#   Resource level (narrower = better):
#     roles/bigquery.dataEditor    ONLY on dataset "claimdesk" (read tables, insert packets/traces)
#     roles/storage.objectAdmin    ONLY on the evidence bucket
#   Reading public datasets (NOAA, geo) needs no extra role.
#
# ORDER: run 02 before 03. The two resource-level bindings (dataset, bucket)
# are granted in 03, right after those resources are created.
# =============================================================================
set -euo pipefail
: "${PROJECT_ID:?source deploy/00_variables.sh first (GOOGLE_CLOUD_PROJECT in .env)}"
: "${USER_EMAIL:?set DEPLOY_IAP_USER_EMAIL in .env, then re-source deploy/00_variables.sh}"
[[ "${USER_EMAIL}" != "you@example.com" ]] || { echo "Edit DEPLOY_IAP_USER_EMAIL in .env (still you@example.com), then re-source deploy/00_variables.sh" >&2; exit 1; }
: "${RUN_SA:?source deploy/00_variables.sh first}"

# A brand-new service account can take a minute to become visible to IAM, so
# the first binding may fail with "does not exist". retry() re-runs a command
# up to 5 times, waiting 10s, 20s, 30s, ... in between.
retry() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if "$@"; then return 0; fi
    echo "  (attempt ${attempt} failed - waiting $(( attempt * 10 ))s for IAM to catch up)" >&2
    sleep $(( attempt * 10 ))
  done
  echo "ERROR: still failing after 5 attempts: $*" >&2
  return 1
}

gcloud iam service-accounts create "${RUN_SA_NAME}" \
  --display-name="ClaimDesk Cloud Run runtime" \
  --description="Least-privilege identity for the ClaimDesk Cloud Run service" || echo "(already exists)"

for ROLE in roles/aiplatform.user roles/bigquery.jobUser roles/datastore.user \
            roles/logging.logWriter roles/cloudtrace.agent roles/monitoring.metricWriter \
            roles/discoveryengine.viewer; do
  retry gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${RUN_SA}" --role="${ROLE}" --condition=None --quiet >/dev/null
  echo "granted ${ROLE}"
done

# Allow YOU to deploy a service that runs as this service account.
retry gcloud iam service-accounts add-iam-policy-binding "${RUN_SA}" \
  --member="user:${USER_EMAIL}" --role="roles/iam.serviceAccountUser" --quiet >/dev/null
echo "granted you roles/iam.serviceAccountUser on ${RUN_SA}"
echo "Next: bash deploy/03_storage_firestore_bigquery.sh"
