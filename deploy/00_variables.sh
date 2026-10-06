#!/usr/bin/env bash
# =============================================================================
# 00_variables.sh - load .env (the single config file) for the deploy scripts.
# =============================================================================
# HOW TO USE
#   1. cp .env.example .env   and edit the values marked <-- EDIT.
#   2. In every new terminal:   source deploy/00_variables.sh
#   3. Run the numbered scripts IN ORDER, one at a time, reading each first.
#      They are reference scripts for a MANUAL deployment.
#
# WHY NOT JUST `source .env`?
#   Values such as "Demo Tideline" (a space) or "(default)" (parentheses) are
#   fine for Python but are shell syntax errors when sourced directly. The
#   small loop below reads KEY=VALUE lines literally and exports them.
#
# Nothing here creates resources; it only sets shell variables.
# =============================================================================

_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_ENV_FILE="${ENV_FILE:-${_ROOT}/.env}"
if [[ ! -f "${_ENV_FILE}" ]]; then
  echo "Missing ${_ENV_FILE}. Run: cp .env.example .env  (then edit it)" >&2
  return 1 2>/dev/null || exit 1
fi

while IFS= read -r _line || [[ -n "${_line}" ]]; do
  _line="${_line#"${_line%%[![:space:]]*}"}"          # trim leading spaces
  [[ -z "${_line}" || "${_line}" == \#* || "${_line}" != *=* ]] && continue
  _line="${_line#export }"
  _key="${_line%%=*}"; _val="${_line#*=}"
  _val="${_val%\"}"; _val="${_val#\"}"; _val="${_val%\'}"; _val="${_val#\'}"
  # Real environment variables win over .env (same rule as the Python app).
  if [[ -z "${!_key+x}" ]]; then export "${_key}=${_val}"; fi
done < "${_ENV_FILE}"
unset _line _key _val

# ---- Short names used by the numbered scripts (all derived from .env) --------
export PROJECT_ID="${GOOGLE_CLOUD_PROJECT}"
export CLOUDSDK_CORE_PROJECT="${PROJECT_ID}"
export REGION="${CLAIMDESK_REGION:-us-central1}"
export BQ_LOCATION="${CLAIMDESK_BQ_LOCATION:-${REGION}}"
export BQ_DATASET="${CLAIMDESK_BQ_DATASET:-claimdesk}"
export BUCKET="${CLAIMDESK_GCS_BUCKET}"
export FIRESTORE_DB="${CLAIMDESK_FIRESTORE_DATABASE:-(default)}"
export USER_EMAIL="${DEPLOY_IAP_USER_EMAIL}"
export SERVICE_NAME="${DEPLOY_SERVICE_NAME:-demo-tideline}"
export RUN_SA_NAME="${DEPLOY_RUN_SA_NAME:-demo-tideline-run}"
export RUN_SA="${RUN_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
export AR_REPO="${DEPLOY_AR_REPO:-demo-tideline}"
# Where container images live (WITHOUT a tag). The tag is chosen fresh by
# deploy/04_build_image.sh on every build, which prints an
# "export IMAGE=...:<tag>" line for step 05. We deliberately do NOT compute
# IMAGE here: a tag frozen when this file is sourced would make a second
# build in the same terminal overwrite/redeploy the same tag.
export IMAGE_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}"
# Vertex AI Search (grounding). The ONE resource outside us-central1: search
# data stores only exist in the "global", "us" or "eu" multi-regions.
export SEARCH_LOCATION="${CLAIMDESK_SEARCH_LOCATION:-us}"
export SEARCH_DATA_STORE_ID="${DEPLOY_SEARCH_DATA_STORE_ID:-fema-nfip-docs}"
export SEARCH_ENGINE_ID="${CLAIMDESK_SEARCH_ENGINE_ID:-fema-nfip-engine}"

# ---- Guard rails ----------------------------------------------------------------
if [[ "${PROJECT_ID}" == "your-project-id" || -z "${PROJECT_ID}" ]]; then
  echo "WARNING: set GOOGLE_CLOUD_PROJECT in .env" >&2
fi
if [[ "${USER_EMAIL}" == "you@example.com" || -z "${USER_EMAIL}" ]]; then
  echo "WARNING: set DEPLOY_IAP_USER_EMAIL in .env" >&2
fi
if [[ "${BQ_LOCATION}" != "${REGION}" ]]; then
  echo "WARNING: CLAIMDESK_BQ_LOCATION (${BQ_LOCATION}) differs from CLAIMDESK_REGION (${REGION})." >&2
fi

# Project number is needed for the IAP service agent identity (read-only call).
if command -v gcloud >/dev/null 2>&1 && [[ "${PROJECT_ID}" != "your-project-id" && -n "${PROJECT_ID}" ]]; then
  export PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)' 2>/dev/null)"
fi

echo "PROJECT_ID=${PROJECT_ID}  PROJECT_NUMBER=${PROJECT_NUMBER:-?}  REGION=${REGION}"
echo "SERVICE=${SERVICE_NAME}  SA=${RUN_SA}"
echo "BUCKET=gs://${BUCKET}  BQ=${PROJECT_ID}:${BQ_DATASET} (${BQ_LOCATION})"
echo "Models: flash/image @ ${GOOGLE_CLOUD_LOCATION:-global}, live @ ${LIVE_MODEL_LOCATION:-global} (fallback ${LIVE_MODEL_FALLBACK_LOCATION:-${REGION}})"
echo "Vertex AI Search: engine ${SEARCH_ENGINE_ID} in '${SEARCH_LOCATION}' (grounding enabled: ${CLAIMDESK_ENABLE_GUIDANCE_SEARCH:-false})"
echo "IAP user: ${USER_EMAIL}"
echo "IMAGE (used by step 05): ${IMAGE:-<not set yet - run deploy/04_build_image.sh and paste its export line>}"
