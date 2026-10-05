#!/usr/bin/env bash
# =============================================================================
# 03b_vertex_ai_search.sh: grounding on FEMA NFIP documents (Vertex AI Search)
# =============================================================================
# WHAT THIS CREATES
#   1. A Vertex AI Search DATA STORE  "${SEARCH_DATA_STORE_ID}" (default
#      fema-nfip-docs): an index of unstructured documents (our FEMA PDFs).
#   2. An IMPORT of gs://${BUCKET}/grounding/fema/*.pdf into that data store.
#      Vertex AI Search reads the PDFs with its DEFAULT (digital) PDF parser,
#      which uses the text layer inside the PDF (no OCR), and indexes them.
#   3. A search ENGINE (called an "app" in the Console) "${SEARCH_ENGINE_ID}"
#      (default fema-nfip-engine) on top of the data store. The Cloud Run
#      app queries this engine through the lookup_flood_guidance voice tool.
#
# WHY NO CHUNKING, LAYOUT PARSER OR OCR?
#   * The FEMA PDFs are "digital" PDFs (they contain real text), so the
#     default parser reads them fine. OCR is only needed for scanned images.
#   * The app (claimdesk/data_access/guidance_search.py) asks for EXTRACTIVE
#     ANSWERS / SEGMENTS, which come with the PDF page number we quote to the
#     claimant. A data store with a chunking config (which the layout parser
#     goes with) returns chunks instead, and extractive answers/segments with
#     page numbers are not available. So chunking and the layout parser are
#     deliberately NOT enabled.
#   * Parsing options can only be chosen when a data store is CREATED. If you
#     ever need scanned PDFs, create a NEW data store (new ID, or delete the
#     old one first) and add this to the creation body in step 1:
#       "documentProcessingConfig": {"defaultParsingConfig": {"ocrParsingConfig": {}}}
#
# REGION: THE ONE EXCEPTION
#   Vertex AI Search data stores only exist in "global", "us" or "eu". We use
#   the "us" MULTI-REGION (data stays in the United States). Everything else
#   in this project is in us-central1. The API endpoint for "us" is
#   https://us-discoveryengine.googleapis.com.
#
# WHY curl AND NOT gcloud?
#   gcloud has no complete command group for Vertex AI Search data stores
#   and engines yet, so we call the documented REST API with curl and your
#   own gcloud login token. Every call prints its JSON response so you can
#   see what happened.
#
# LONG-RUNNING OPERATIONS (LROs)
#   Creating a data store, importing documents and creating an engine are
#   background jobs. The API answers at once with an "operation" (a JSON
#   object with a "name"). We poll GET https://${HOST}/v1/<operation name>
#   until it says "done": true, then print the result (error, successCount,
#   failureCount, errorSamples). The script stops with exit code 1 if an
#   operation fails, so a failed import can never look like "0 documents".
#
# BEFORE RUNNING
#   * source deploy/00_variables.sh
#   * 01 (APIs incl. discoveryengine), 02 (SA) and 03 (bucket) are done.
#   * The FEMA PDFs are in the bucket:
#       uv run python -m grounding.fetch_fema_docs --upload
#
# SAFE TO RE-RUN
#   Existing data store / engine are detected and reused (HTTP 409 = exists).
#   Re-importing is incremental: changed files are updated, nothing is lost.
#
# OPTIONAL KNOBS (environment variables, in seconds)
#   CREATE_TIMEOUT_S (default 600)   wait for data store / engine creation
#   IMPORT_TIMEOUT_S (default 2700)  wait for the import operation
#   INDEX_TIMEOUT_S  (default 1200)  wait for documents to appear
#
# COST (see docs/cost_analysis.md): Enterprise edition search is roughly
#   US$4 per 1,000 queries, and index storage for a few PDFs stays within the
#   free storage allowance. Always confirm on the pricing page:
#   https://cloud.google.com/generative-ai-app-builder/pricing
# =============================================================================
set -euo pipefail
: "${PROJECT_ID:?source deploy/00_variables.sh first}"
: "${BUCKET:?CLAIMDESK_GCS_BUCKET must be set in .env}"
: "${PROJECT_NUMBER:?PROJECT_NUMBER is empty - is gcloud logged in? re-source deploy/00_variables.sh}"

LOC="${SEARCH_LOCATION:-us}"
DS="${SEARCH_DATA_STORE_ID:-fema-nfip-docs}"
ENGINE="${SEARCH_ENGINE_ID:-fema-nfip-engine}"
if [[ "${LOC}" == "global" ]]; then HOST="discoveryengine.googleapis.com"; else HOST="${LOC}-discoveryengine.googleapis.com"; fi
BASE="https://${HOST}/v1/projects/${PROJECT_ID}/locations/${LOC}/collections/default_collection"
GCS_GLOB="gs://${BUCKET}/grounding/fema/*.pdf"
CREATE_TIMEOUT_S="${CREATE_TIMEOUT_S:-600}"
IMPORT_TIMEOUT_S="${IMPORT_TIMEOUT_S:-2700}"
INDEX_TIMEOUT_S="${INDEX_TIMEOUT_S:-1200}"

# Small helper: authenticated JSON call. Sets API_CODE (HTTP status) and
# API_BODY (response JSON) and prints the body unless API_QUIET=1.
api() {
  local method="$1" url="$2" body="${3:-}"
  local out; out="$(mktemp)"
  API_CODE="$(curl -sS -o "${out}" -w '%{http_code}' -X "${method}" "${url}" \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "X-Goog-User-Project: ${PROJECT_ID}" \
    -H "Content-Type: application/json" \
    ${body:+-d "${body}"})" || API_CODE="000"
  API_BODY="$(cat "${out}")"; rm -f "${out}"
  if [[ "${API_QUIET:-0}" != "1" ]]; then printf '%s\n' "${API_BODY}"; fi
}

# Read the "name" of an operation out of a JSON response (python3 ships with
# gcloud, so this works everywhere, including Cloud Shell).
op_name_of() {
  python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("name", ""))
except ValueError: print("")'
}

# Summarise an operation JSON (on stdin). First output line is one word:
#   RUNNING  not finished yet
#   OK       finished without errors
#   PARTIAL  import finished, some files failed but at least one succeeded
#   FAILED   finished with an error, or every file failed
# Following lines are human-readable details (counts, error samples).
read -r -d '' OP_STATUS_PY <<'PY' || true
import json, sys

try:
    op = json.load(sys.stdin)
except ValueError:
    print("RUNNING")
    print("  (could not read the operation status; will retry)")
    sys.exit(0)

meta = op.get("metadata") or {}
response = op.get("response") or {}

def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

has_counts = any(k in meta for k in ("successCount", "failureCount", "totalCount"))
success, failure = as_int(meta.get("successCount")), as_int(meta.get("failureCount"))
counts = f"  successCount={success} failureCount={failure} totalCount={meta.get('totalCount', '?')}"

if not op.get("done"):
    print("RUNNING")
    if has_counts:
        print(counts)
    sys.exit(0)

details = []
if has_counts:
    details.append(counts)
for sample in (response.get("errorSamples") or [])[:5]:
    details.append(f"  error sample: code={sample.get('code')} {sample.get('message', '')[:300]}")

if "error" in op:
    err = op["error"]
    print("FAILED")
    print(f"  error: code={err.get('code')} {err.get('message', '')[:500]}")
elif has_counts and failure > 0 and success == 0:
    print("FAILED")
elif has_counts and failure > 0:
    print("PARTIAL")
else:
    print("OK")
print("\n".join(details))
PY

# wait_for_operation <operation name> <label> <timeout seconds>
# Polls the operation until done. Exits the script (code 1) on failure or
# timeout, with a hint on what to do next.
wait_for_operation() {
  local op_name="$1" label="$2" timeout_s="$3"
  local waited=0 interval=15 summary status
  if [[ -z "${op_name}" ]]; then
    echo "Could not read an operation name for '${label}' from the response above." >&2
    exit 1
  fi
  echo "  waiting for ${label} (operation ${op_name##*/}, timeout ${timeout_s}s)"
  while :; do
    API_QUIET=1 api GET "https://${HOST}/v1/${op_name}"
    summary="$(printf '%s' "${API_BODY}" | python3 -c "${OP_STATUS_PY}")"
    status="$(printf '%s\n' "${summary}" | head -n 1)"
    case "${status}" in
      OK)
        echo "  ${label}: done"; printf '%s\n' "${summary}" | tail -n +2 | sed '/^$/d'
        return 0 ;;
      PARTIAL)
        echo "  WARNING: ${label} finished, but some files failed:" >&2
        printf '%s\n' "${summary}" | tail -n +2 | sed '/^$/d' >&2
        return 0 ;;
      FAILED)
        echo "ERROR: ${label} failed:" >&2
        printf '%s\n' "${summary}" | tail -n +2 | sed '/^$/d' >&2
        echo "Full operation: curl -sS -H \"Authorization: Bearer \$(gcloud auth print-access-token)\" -H \"X-Goog-User-Project: ${PROJECT_ID}\" https://${HOST}/v1/${op_name}" >&2
        exit 1 ;;
    esac
    if (( waited >= timeout_s )); then
      echo "ERROR: ${label} is still running after ${timeout_s}s." >&2
      echo "  Check it later:  curl -sS -H \"Authorization: Bearer \$(gcloud auth print-access-token)\" -H \"X-Goog-User-Project: ${PROJECT_ID}\" https://${HOST}/v1/${op_name}" >&2
      echo "  Then re-run this script (safe), or raise the timeout, e.g. IMPORT_TIMEOUT_S=5400." >&2
      exit 1
    fi
    printf '%s\n' "${summary}" | tail -n +2 | sed '/^$/d'
    sleep "${interval}"; waited=$(( waited + interval ))
  done
}

echo "== 0. Check that the FEMA PDFs are in the bucket"
if ! gcloud storage ls "${GCS_GLOB}"; then
  echo "No PDFs at ${GCS_GLOB}. Run: uv run python -m grounding.fetch_fema_docs --upload" >&2
  exit 1
fi

echo "== 1. Create the data store '${DS}' in '${LOC}' (also provisions the Discovery Engine service agent)"
# No documentProcessingConfig = default digital PDF parser, no chunking (see header).
api POST "${BASE}/dataStores?dataStoreId=${DS}" "{
  \"displayName\": \"FEMA NFIP documents\",
  \"industryVertical\": \"GENERIC\",
  \"solutionTypes\": [\"SOLUTION_TYPE_SEARCH\"],
  \"contentConfig\": \"CONTENT_REQUIRED\"
}"
case "${API_CODE}" in
  200) echo "data store creation started"
       wait_for_operation "$(printf '%s' "${API_BODY}" | op_name_of)" "data store creation" "${CREATE_TIMEOUT_S}" ;;
  409) echo "data store already exists - reusing it" ;;
  *) echo "Unexpected HTTP ${API_CODE} creating the data store" >&2; exit 1 ;;
esac

echo "== 2. Let the Vertex AI Search service agent read the bucket"
# Vertex AI Search imports files as its own Google-managed identity (the
# "service agent"). Creating the data store in step 1 (plus services identity
# create below) guarantees the service account exists before we bind IAM.
gcloud beta services identity create --service=discoveryengine.googleapis.com --project="${PROJECT_ID}" >/dev/null 2>&1 || true
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com" \
  --role="roles/storage.objectViewer" --quiet >/dev/null
echo "granted roles/storage.objectViewer on gs://${BUCKET} to the Discovery Engine service agent"

echo "== 3. Import ${GCS_GLOB} (unstructured documents)"
api POST "${BASE}/dataStores/${DS}/branches/0/documents:import" "{
  \"gcsSource\": {\"inputUris\": [\"${GCS_GLOB}\"], \"dataSchema\": \"content\"},
  \"reconciliationMode\": \"INCREMENTAL\"
}"
[[ "${API_CODE}" == "200" ]] || { echo "Import request failed (HTTP ${API_CODE})" >&2; exit 1; }
# If the bucket permission from step 2 has not propagated yet, the import
# fails with PERMISSION_DENIED in errorSamples: wait a minute and re-run.
wait_for_operation "$(printf '%s' "${API_BODY}" | op_name_of)" "document import" "${IMPORT_TIMEOUT_S}"

echo "== 4. Create the search engine '${ENGINE}' (Enterprise tier: needed for extractive answers)"
api POST "${BASE}/engines?engineId=${ENGINE}" "{
  \"displayName\": \"FEMA NFIP guidance\",
  \"dataStoreIds\": [\"${DS}\"],
  \"solutionType\": \"SOLUTION_TYPE_SEARCH\",
  \"industryVertical\": \"GENERIC\",
  \"searchEngineConfig\": {\"searchTier\": \"SEARCH_TIER_ENTERPRISE\"}
}"
case "${API_CODE}" in
  200) echo "engine creation started"
       wait_for_operation "$(printf '%s' "${API_BODY}" | op_name_of)" "engine creation" "${CREATE_TIMEOUT_S}" ;;
  409) echo "engine already exists - reusing it" ;;
  *) echo "Unexpected HTTP ${API_CODE} creating the engine" >&2; exit 1 ;;
esac

echo "== 5. Wait for documents to be listed in the data store (timeout ${INDEX_TIMEOUT_S}s)"
# Counts the "documents" array of the list call with python3 (robust JSON
# parsing instead of grepping text). Prints -1 if the call itself failed.
count_documents() {
  API_QUIET=1 api GET "${BASE}/dataStores/${DS}/branches/0/documents?pageSize=100"
  if [[ "${API_CODE}" != "200" ]]; then
    echo "  list call failed (HTTP ${API_CODE}): ${API_BODY:0:300}" >&2
    echo "-1"; return 0
  fi
  printf '%s' "${API_BODY}" | python3 -c 'import json,sys
try: print(len(json.load(sys.stdin).get("documents", [])))
except ValueError: print(-1)'
}
COUNT=0; waited=0
while :; do
  COUNT="$(count_documents)"
  echo "  after ${waited}s: ${COUNT} document(s) in the data store"
  (( COUNT > 0 )) && break
  if (( waited >= INDEX_TIMEOUT_S )); then
    cat >&2 <<EOF
ERROR: no documents in data store '${DS}' after ${INDEX_TIMEOUT_S}s.
  * Check the import result printed in step 3 (errorSamples).
  * Check the PDFs exist:  gcloud storage ls ${GCS_GLOB}
  * Check the service agent can read the bucket (step 2), then re-run this
    script (safe), or wait longer with INDEX_TIMEOUT_S=2400.
  * Console: AI Applications > Data Stores > ${DS} > Activity
EOF
    exit 1
  fi
  sleep 30; waited=$(( waited + 30 ))
done

echo "== 6. Test query (results can lag the document count by a few minutes)"
api POST "${BASE}/engines/${ENGINE}/servingConfigs/default_search:search" '{
  "query": "how long do I have to send a proof of loss",
  "pageSize": 3,
  "contentSearchSpec": {"snippetSpec": {"returnSnippet": true},
                        "extractiveContentSpec": {"maxExtractiveAnswerCount": 1}}
}'

cat <<EOF

Done. If the test query above lists "results", turn grounding on:

  1. In .env set:
       CLAIMDESK_SEARCH_LOCATION=${LOC}
       CLAIMDESK_SEARCH_ENGINE_ID=${ENGINE}
       CLAIMDESK_ENABLE_GUIDANCE_SEARCH=true
  2. source deploy/00_variables.sh
  3. Deploy (or re-deploy) with deploy/05_deploy_cloud_run.sh

If "results" is empty, wait 10 minutes and re-run the script:
  bash deploy/03b_vertex_ai_search.sh   (safe to re-run)
Console view: Vertex AI Search (AI Applications) > Apps > ${ENGINE} > Preview
EOF
