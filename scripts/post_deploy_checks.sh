#!/usr/bin/env bash
# =============================================================================
# post_deploy_checks.sh - automated, READ-ONLY checks after a deployment.
# =============================================================================
# WHAT IT DOES
#   Runs the "infrastructure" test cases from docs/post_deployment_tests.md
#   (section L0) and prints PASS / WARN / FAIL for each one:
#     * the service is private (no allUsers, IAP on, anonymous curl != 200)
#     * only YOUR account can pass IAP
#     * everything is in us-central1 (Cloud Run, bucket, BigQuery, Firestore)
#     * the reference tables have rows
#     * the service runs as the dedicated service account, with storage=gcp
#       and the IAP JWT audience (CLAIMDESK_IAP_AUDIENCE) set by deploy/05
#     * there are no ERROR logs in the last hour
#
# WHAT IT NEVER DOES
#   It creates, changes or deletes nothing. Every command is a describe / list /
#   get-iam-policy / metadata read. The only BigQuery "query" reads the free
#   __TABLES__ metadata view (0 bytes billed).
#
# HOW TO RUN
#   source deploy/00_variables.sh
#   bash scripts/post_deploy_checks.sh
#
# Exit code = number of FAILs (0 means all good), so you can use it in CI later.
# =============================================================================
set -uo pipefail   # no -e: we want to run every check even if one fails
: "${PROJECT_ID:?source deploy/00_variables.sh first}"

PASS=0; WARN=0; FAIL=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS + 1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; WARN=$((WARN + 1)); }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL + 1)); }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# Small helper: read one field out of JSON on stdin with Python (always installed
# with gcloud), so we don't depend on jq.
json_get() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1" 2>/dev/null; }

# -----------------------------------------------------------------------------
section "L0-01..05  Cloud Run service: exists, private, right region and identity"
# -----------------------------------------------------------------------------
SVC_JSON="$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" --format=json 2>/dev/null)"
if [[ -z "${SVC_JSON}" ]]; then
  fail "L0-01 Service ${SERVICE_NAME} not found in ${REGION}. Deploy first (deploy/05)."
  echo; echo "Summary: ${PASS} pass, ${WARN} warn, ${FAIL} fail"; exit "${FAIL}"
fi
pass "L0-01 Service ${SERVICE_NAME} exists in ${REGION}"
URL="$(echo "${SVC_JSON}" | json_get "d['status']['url']")"

# IAP flag lives in an annotation; the exact key has changed over time, so we
# search the whole description for an "iap ... true" pair.
if echo "${SVC_JSON}" | grep -qiE '"[^"]*iap[^"]*"\s*:\s*"?true'; then
  pass "L0-02 IAP is enabled on the service"
else
  fail "L0-02 IAP does not look enabled (see deploy_runbook.md step 7)"
fi

if gcloud run services get-iam-policy "${SERVICE_NAME}" --region="${REGION}" --format=json 2>/dev/null | grep -qE 'allUsers|allAuthenticatedUsers'; then
  fail "L0-03 allUsers/allAuthenticatedUsers can invoke the service. Remove that binding!"
else
  pass "L0-03 No public invoker binding (allUsers / allAuthenticatedUsers)"
fi

SA="$(echo "${SVC_JSON}" | json_get "d['spec']['template']['spec'].get('serviceAccountName','')")"
[[ "${SA}" == "${RUN_SA}" ]] && pass "L0-04 Runs as ${RUN_SA}" || fail "L0-04 Runs as '${SA}', expected ${RUN_SA}"

ENV_DUMP="$(echo "${SVC_JSON}" | json_get "{e['name']: e.get('value','') for c in d['spec']['template']['spec']['containers'] for e in c.get('env', [])}")"
if echo "${ENV_DUMP}" | grep -q "'CLAIMDESK_STORAGE_BACKEND': 'gcp'"; then
  pass "L0-05 CLAIMDESK_STORAGE_BACKEND=gcp (Firestore + GCS + BigQuery in use)"
else
  fail "L0-05 CLAIMDESK_STORAGE_BACKEND is not 'gcp' on Cloud Run"
fi
echo "${ENV_DUMP}" | grep -q "'CLAIMDESK_REGION': 'us-central1'" && pass "L0-06 CLAIMDESK_REGION=us-central1" || warn "L0-06 CLAIMDESK_REGION is not us-central1 in the service env"

# deploy/05 sets the audience the app uses to verify the IAP-signed JWT.
EXPECTED_AUD="/projects/${PROJECT_NUMBER:-?}/locations/${REGION}/services/${SERVICE_NAME}"
if echo "${ENV_DUMP}" | grep -qF "'CLAIMDESK_IAP_AUDIENCE': '${EXPECTED_AUD}'"; then
  pass "L0-06b CLAIMDESK_IAP_AUDIENCE=${EXPECTED_AUD}"
elif echo "${ENV_DUMP}" | grep -q "'CLAIMDESK_IAP_AUDIENCE'"; then
  warn "L0-06b CLAIMDESK_IAP_AUDIENCE differs from ${EXPECTED_AUD} - redeploy with deploy/05"
else
  warn "L0-06b CLAIMDESK_IAP_AUDIENCE is not set on the service - redeploy with deploy/05"
fi

# -----------------------------------------------------------------------------
section "L0-07..08  Privacy from the outside"
# -----------------------------------------------------------------------------
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "${URL}/api/health" || echo 000)"
if [[ "${CODE}" == "200" ]]; then
  fail "L0-07 Anonymous GET ${URL}/api/health returned 200 - the app is PUBLIC"
else
  pass "L0-07 Anonymous request is blocked (HTTP ${CODE}; 302/401/403 expected)"
fi

IAP_POLICY="$(gcloud iap web get-iam-policy --region="${REGION}" --resource-type=cloud-run --service="${SERVICE_NAME}" --format=json 2>/dev/null)"
MEMBERS="$(echo "${IAP_POLICY}" | json_get "sorted({m for b in d.get('bindings', []) if b['role']=='roles/iap.httpsResourceAccessor' for m in b['members']})")"
if [[ "${MEMBERS}" == "['user:${USER_EMAIL}']" ]]; then
  pass "L0-08 Only user:${USER_EMAIL} may pass IAP"
elif echo "${MEMBERS}" | grep -q "user:${USER_EMAIL}"; then
  warn "L0-08 You have access, but so do others: ${MEMBERS}"
else
  fail "L0-08 You are not in the IAP accessor list (${MEMBERS:-empty}). Run deploy/06."
fi

# -----------------------------------------------------------------------------
section "L0-09..12  Data stores: private and in us-central1"
# -----------------------------------------------------------------------------
BUCKET_JSON="$(gcloud storage buckets describe "gs://${BUCKET}" --format=json 2>/dev/null)"
LOC="$(echo "${BUCKET_JSON}" | json_get "d.get('location','')")"
PAP="$(echo "${BUCKET_JSON}" | json_get "d.get('public_access_prevention','')")"
[[ "${LOC,,}" == "us-central1" ]] && pass "L0-09 Bucket gs://${BUCKET} is in us-central1" || fail "L0-09 Bucket location is '${LOC}'"
[[ "${PAP}" == "enforced" ]] && pass "L0-10 Bucket public access prevention = enforced" || fail "L0-10 Bucket public access prevention = '${PAP}'"

BQ_LOC="$(bq show --format=json "${PROJECT_ID}:${BQ_DATASET}" 2>/dev/null | json_get "d.get('location','')")"
[[ "${BQ_LOC,,}" == "us-central1" ]] && pass "L0-11 BigQuery dataset ${BQ_DATASET} is in us-central1" || fail "L0-11 BigQuery dataset location is '${BQ_LOC}'"

FS_LOC="$(gcloud firestore databases describe --database="${FIRESTORE_DB}" --format='value(locationId)' 2>/dev/null)"
[[ "${FS_LOC}" == "us-central1" ]] && pass "L0-12 Firestore ${FIRESTORE_DB} is in us-central1" || warn "L0-12 Firestore location is '${FS_LOC}' (expected us-central1)"

# -----------------------------------------------------------------------------
section "L0-13  Reference data has rows (free metadata read)"
# -----------------------------------------------------------------------------
COUNTS="$(bq query --location="${BQ_LOCATION}" --use_legacy_sql=false --format=csv --quiet \
  "SELECT table_id, row_count FROM \`${PROJECT_ID}.${BQ_DATASET}.__TABLES__\` ORDER BY table_id" 2>/dev/null)"
for t in policy_registry loss_benchmarks noaa_flood_events zip_points eval_seed_claims; do
  n="$(echo "${COUNTS}" | awk -F, -v t="$t" '$1==t {print $2}')"
  if [[ -n "${n}" && "${n}" -gt 0 ]]; then pass "L0-13 ${t}: ${n} rows"; else fail "L0-13 ${t}: missing or empty (see docs/data_loading.md)"; fi
done
for t in intake_packets conversation_traces; do
  n="$(echo "${COUNTS}" | awk -F, -v t="$t" '$1==t {print $2}')"
  [[ -n "${n}" ]] && pass "L0-13 ${t} exists (${n} rows; streaming rows may not show here for ~90 min)" || fail "L0-13 ${t} table missing"
done

# -----------------------------------------------------------------------------
section "L0-14  Health of the running service (last hour of logs)"
# -----------------------------------------------------------------------------
ERRORS="$(gcloud logging read \
  "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE_NAME}\" AND severity>=ERROR" \
  --freshness=1h --limit=5 --format='value(timestamp,jsonPayload.message,textPayload)' 2>/dev/null)"
if [[ -z "${ERRORS}" ]]; then
  pass "L0-14 No ERROR logs in the last hour"
else
  warn "L0-14 ERROR logs in the last hour (open Error Reporting):"; echo "${ERRORS}" | sed 's/^/        /'
fi

# -----------------------------------------------------------------------------
section "L0-15  Grounding: Vertex AI Search engine (the one resource in the 'us' multi-region)"
# -----------------------------------------------------------------------------
if [[ "${CLAIMDESK_ENABLE_GUIDANCE_SEARCH:-false}" != "true" ]]; then
  warn "L0-15 Grounding is off (CLAIMDESK_ENABLE_GUIDANCE_SEARCH=false). Optional: RUNBOOK Phase 7."
else
  S_LOC="${SEARCH_LOCATION:-us}"
  [[ "${S_LOC}" == "global" ]] && S_HOST="discoveryengine.googleapis.com" || S_HOST="${S_LOC}-discoveryengine.googleapis.com"
  ENGINE_JSON="$(curl -sS "https://${S_HOST}/v1/projects/${PROJECT_ID}/locations/${S_LOC}/collections/default_collection/engines/${SEARCH_ENGINE_ID}" \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "X-Goog-User-Project: ${PROJECT_ID}" 2>/dev/null)"
  if echo "${ENGINE_JSON}" | grep -q '"solutionType"'; then
    pass "L0-15 Engine ${SEARCH_ENGINE_ID} exists in '${S_LOC}'"
  else
    fail "L0-15 Engine ${SEARCH_ENGINE_ID} not found in '${S_LOC}' (run deploy/03b_vertex_ai_search.sh)"
  fi
  if echo "${SVC_JSON}" | grep -q 'CLAIMDESK_ENABLE_GUIDANCE_SEARCH'; then
    pass "L0-15 Cloud Run has the grounding settings"
  else
    warn "L0-15 Cloud Run revision lacks CLAIMDESK_ENABLE_GUIDANCE_SEARCH - redeploy (RUNBOOK Phase 12)"
  fi
fi

echo
echo "Service URL: ${URL}"
printf 'Summary: \033[32m%s pass\033[0m, \033[33m%s warn\033[0m, \033[31m%s fail\033[0m\n' "${PASS}" "${WARN}" "${FAIL}"
exit "${FAIL}"
