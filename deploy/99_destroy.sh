#!/usr/bin/env bash
# =============================================================================
# 99_destroy.sh: remove everything the numbered scripts created
# =============================================================================
# Deletes, in a safe order (things that USE resources before the resources):
#   1. Cloud Run service "${SERVICE_NAME}" (its IAP policy goes with it)
#   2. Vertex AI Search engine, then its data store (the "us" multi-region)
#   3. Artifact Registry repository "${AR_REPO}" with ALL its container images
#      (04's cleanup policy keeps the 5 newest images plus anything younger
#      than 30 days, so a few images are always left until this step)
#   4. Budget "${SERVICE_NAME}-monthly" and log metric "<service>_errors"
#   5. DATA (skipped with --keep-data):
#        BigQuery dataset "${BQ_DATASET}" (policy registry, packets, traces, evals)
#        Cloud Storage bucket "gs://${BUCKET}" (photos, packets, FEMA PDFs, raw data)
#   6. Firestore database: ONLY with --include-firestore (a project can have
#      a "(default)" database used by other apps, and a deleted database
#      name cannot be reused for a while)
#   7. Project-level IAM roles of the app service account, then the account
#
# WHAT IS NOT TOUCHED
#   * Enabled APIs (harmless and free when unused).
#   * The Cloud Build source bucket "${PROJECT_ID}_cloudbuild" (may be shared;
#     delete it yourself if this project was only for the demo).
#   * Cloud Logging logs (expire automatically after 30 days by default).
#
# SIMPLEST ALTERNATIVE: if the project exists ONLY for this demo, delete the
# whole project instead (stops all billing, 30-day recovery window):
#     gcloud projects delete "${PROJECT_ID}"
#
# USAGE (Cloud Shell, from the project folder)
#   source deploy/00_variables.sh
#   bash deploy/99_destroy.sh                      # everything except Firestore
#   bash deploy/99_destroy.sh --keep-data          # keep BigQuery + bucket
#   bash deploy/99_destroy.sh --include-firestore  # also delete Firestore DB
#
# Each step tolerates "not found", so re-running after a partial run is safe.
# =============================================================================
set -uo pipefail   # no -e: one missing resource must not stop the clean-up
: "${PROJECT_ID:?source deploy/00_variables.sh first}"

KEEP_DATA=false
INCLUDE_FIRESTORE=false
for arg in "$@"; do
  case "${arg}" in
    --keep-data) KEEP_DATA=true ;;
    --include-firestore) INCLUDE_FIRESTORE=true ;;
    *) echo "Unknown option ${arg}" >&2; exit 2 ;;
  esac
done

LOC="${SEARCH_LOCATION:-us}"
DS="${SEARCH_DATA_STORE_ID:-fema-nfip-docs}"
ENGINE="${SEARCH_ENGINE_ID:-fema-nfip-engine}"
if [[ "${LOC}" == "global" ]]; then HOST="discoveryengine.googleapis.com"; else HOST="${LOC}-discoveryengine.googleapis.com"; fi
BASE="https://${HOST}/v1/projects/${PROJECT_ID}/locations/${LOC}/collections/default_collection"
METRIC_NAME="${SERVICE_NAME//-/_}_errors"

cat <<EOF
This will DELETE from project ${PROJECT_ID}:
  - Cloud Run service       ${SERVICE_NAME} (${REGION})
  - Vertex AI Search        engine ${ENGINE}, data store ${DS} (${LOC})
  - Artifact Registry repo  ${AR_REPO} (${REGION})
  - Budget / log metric     ${SERVICE_NAME}-monthly / ${METRIC_NAME}
EOF
if [[ "${KEEP_DATA}" == false ]]; then
  echo "  - BigQuery dataset      ${PROJECT_ID}:${BQ_DATASET}   (ALL TABLES)"
  echo "  - Cloud Storage bucket  gs://${BUCKET}             (ALL OBJECTS)"
fi
[[ "${INCLUDE_FIRESTORE}" == true ]] && echo "  - Firestore database    ${FIRESTORE_DB}"
echo "  - Service account       ${RUN_SA} and its project roles"
echo
read -r -p "Type the project id (${PROJECT_ID}) to confirm: " CONFIRM
[[ "${CONFIRM}" == "${PROJECT_ID}" ]] || { echo "Cancelled."; exit 1; }

step() { echo; echo "== $*"; }
TOKEN="$(gcloud auth print-access-token)"

step "1. Cloud Run service"
gcloud run services delete "${SERVICE_NAME}" --region="${REGION}" --quiet || echo "(not found)"

step "2. Vertex AI Search engine, then data store (${LOC})"
# The engine must go first: a data store attached to an engine cannot be deleted.
curl -sS -X DELETE "${BASE}/engines/${ENGINE}" \
  -H "Authorization: Bearer ${TOKEN}" -H "X-Goog-User-Project: ${PROJECT_ID}"; echo
echo "waiting 60s for the engine deletion to finish..."; sleep 60
curl -sS -X DELETE "${BASE}/dataStores/${DS}" \
  -H "Authorization: Bearer ${TOKEN}" -H "X-Goog-User-Project: ${PROJECT_ID}"; echo
echo "(a NOT_FOUND error above just means it was already gone; FAILED_PRECONDITION = re-run in a minute)"

step "3. Artifact Registry repository"
gcloud artifacts repositories delete "${AR_REPO}" --location="${REGION}" --quiet || echo "(not found)"

step "4. Budget and log-based metric"
BILLING_ACCOUNT="$(gcloud billing projects describe "${PROJECT_ID}" --format='value(billingAccountName)' 2>/dev/null | sed 's#billingAccounts/##')"
if [[ -n "${BILLING_ACCOUNT}" ]]; then
  for BUDGET in $(gcloud billing budgets list --billing-account="${BILLING_ACCOUNT}" \
      --filter="displayName=${SERVICE_NAME}-monthly" --format='value(name)' 2>/dev/null); do
    gcloud billing budgets delete "${BUDGET}" --quiet && echo "deleted budget ${BUDGET}"
  done
fi
gcloud logging metrics delete "${METRIC_NAME}" --quiet || echo "(metric not found)"

if [[ "${KEEP_DATA}" == false ]]; then
  step "5a. BigQuery dataset ${BQ_DATASET}"
  bq --location="${BQ_LOCATION}" rm -r -f -d "${PROJECT_ID}:${BQ_DATASET}" || echo "(not found)"
  step "5b. Cloud Storage bucket gs://${BUCKET}"
  gcloud storage rm -r "gs://${BUCKET}" --quiet || echo "(not found)"
else
  step "5. Data kept (--keep-data): BigQuery ${BQ_DATASET} and gs://${BUCKET}"
fi

if [[ "${INCLUDE_FIRESTORE}" == true ]]; then
  step "6. Firestore database ${FIRESTORE_DB}"
  gcloud firestore databases delete --database="${FIRESTORE_DB}" --quiet || echo "(not found)"
else
  step "6. Firestore kept (add --include-firestore to delete it). Intake documents expire via TTL anyway."
fi

step "7. Service account roles, then the service account"
for ROLE in roles/aiplatform.user roles/bigquery.jobUser roles/datastore.user \
            roles/logging.logWriter roles/cloudtrace.agent roles/monitoring.metricWriter \
            roles/discoveryengine.viewer; do
  gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${RUN_SA}" --role="${ROLE}" --condition=None --quiet >/dev/null 2>&1 \
    && echo "removed ${ROLE}" || echo "(${ROLE} not bound)"
done
gcloud iam service-accounts delete "${RUN_SA}" --quiet || echo "(service account not found)"

echo
echo "Clean-up finished. Check nothing billable is left:"
echo "  gcloud run services list --region=${REGION}"
echo "  gcloud artifacts repositories list --location=${REGION}"
echo "  bq ls --project_id=${PROJECT_ID}"
echo "  gcloud storage ls"
echo "  Console > Billing > Reports (costs can take ~24h to stop appearing)"
