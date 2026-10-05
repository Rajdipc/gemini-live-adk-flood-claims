#!/usr/bin/env bash
# =============================================================================
# 04_build_image.sh - build the container with Cloud Build, store it in
# Artifact Registry.
# =============================================================================
# WHY CLOUD BUILD (not `docker build` on your machine)?
#   It runs on Google's machines, needs no local Docker, and pushes straight to
#   Artifact Registry in the same region as Cloud Run (fast pulls, no egress).
#   First 2,500 build-minutes/month on the default machine are free.
#
# IMAGE TAGS
#   Every run picks a NEW tag: the UTC time, plus the git commit when the
#   folder is a git checkout (e.g. 20260929-101500-a1b2c3d). A unique tag per
#   build means Cloud Run always gets a new revision, and you can roll back to
#   any earlier tag. To choose the tag yourself:  bash deploy/04_build_image.sh v1
#
# AFTER IT FINISHES
#   Copy the printed line into your terminal, then run step 05:
#     export IMAGE=us-central1-docker.pkg.dev/<project>/<repo>/<service>:<tag>
#     bash deploy/05_deploy_cloud_run.sh
# =============================================================================
set -euo pipefail
: "${PROJECT_ID:?source deploy/00_variables.sh first}"
: "${IMAGE_REPO:?IMAGE_REPO is empty - re-source deploy/00_variables.sh}"
cd "$(dirname "$0")/.."   # project root (where the Dockerfile is)

# A fresh tag for THIS build (never inherited from the shell).
TAG="${1:-$(date -u +%Y%m%d-%H%M%S)}"
if [[ $# -eq 0 ]] && GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null)"; then
  TAG="${TAG}-${GIT_SHA}"
fi
BUILD_IMAGE="${IMAGE_REPO}:${TAG}"

gcloud artifacts repositories create "${AR_REPO}" \
  --repository-format=docker --location="${REGION}" \
  --description="ClaimDesk container images" || echo "(repository exists)"

# Keep storage costs down with two cleanup rules:
#   * "keep-recent": the 5 newest image versions are ALWAYS kept, and
#   * "delete-old":  versions older than 30 days are deleted,
# so what survives is: the 5 newest images PLUS anything younger than 30 days
# ("Keep" rules win over "Delete" rules). Cleanup runs about once a day.
CLEANUP_JSON="$(mktemp -t claimdesk-ar-cleanup-XXXX.json)"
trap 'rm -f "${CLEANUP_JSON}"' EXIT
cat > "${CLEANUP_JSON}" <<'JSON'
[{"name": "keep-recent", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 5}},
 {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"olderThan": "30d"}}]
JSON
gcloud artifacts repositories set-cleanup-policies "${AR_REPO}" \
  --location="${REGION}" --policy="${CLEANUP_JSON}" --no-dry-run || true

gcloud builds submit --tag "${BUILD_IMAGE}" --region="${REGION}" .
echo
echo "Built ${BUILD_IMAGE}"
echo "Now paste this line into your terminal, then run: bash deploy/05_deploy_cloud_run.sh"
echo "export IMAGE=${BUILD_IMAGE}"
