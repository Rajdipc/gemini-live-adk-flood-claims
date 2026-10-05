#!/usr/bin/env bash
# =============================================================================
# 05_deploy_cloud_run.sh - deploy the PRIVATE Cloud Run service.
# =============================================================================
# All values come from .env (RUNTIME section -> --env-vars-file, DEPLOY_* ->
# sizing flags). Defaults below are the single-user values.
#
# EVERY FLAG EXPLAINED
#   --no-allow-unauthenticated  No anonymous access. Every request must carry
#                               a Google identity with roles/run.invoker.
#   --iap                       Put Identity-Aware Proxy in front of the
#                               service: users sign in with Google in the
#                               browser; IAP checks they are on the allow-list
#                               (step 06) before any request reaches the app.
#   --service-account           Run as the least-privilege app SA (RUN_SA, default demo-tideline-run).
#   --timeout 3600              Max request length = 60 min. A voice call is
#                               one long WebSocket request; the app itself
#                               caps a live session at 20 minutes.
#   --session-affinity          Keep one browser on the same container, so
#                               the WebSocket and REST calls of an intake meet
#                               the same in-memory live session.
#   --concurrency 20            Requests one container may serve at once.
#                               A single user opens ~3-5 (socket + polling).
#   --min-instances 0           Scale to zero when idle -> $0 compute when
#                               you're not using it (cold start ~3-6 s).
#   --max-instances 1           Single-user demo: never more than 1 container
#                               (also a hard cost cap). See
#                               docs/scaling_to_1000_users.md to change this.
#   --cpu 1 --memory 2Gi        Enough for audio relay + image handling.
#   --no-cpu-throttling         CPU stays allocated while a container is
#                               running (instance-based billing). Needed
#                               because background tasks (pipeline refresh,
#                               trace flush) run outside a request.
#   --cpu-boost                 Extra CPU during start-up = faster cold start.
#   --execution-environment gen2  Full Linux compatibility, better network perf.
#   --ingress all               Traffic comes from the internet but ONLY via
#                               IAP + IAM; with no allow-listed identity
#                               nothing gets in. (Internal-only ingress would
#                               need a VPN/load balancer; overkill for a demo.)
#
# gcloud VERSION
#   `--iap` on `gcloud run deploy` (and `gcloud iap web ... --resource-type=cloud-run`
#   in step 06) need a recent gcloud. Cloud Shell is kept up to date; elsewhere
#   run `gcloud components update`. If your gcloud says "unrecognized
#   arguments: --iap", update it, or use `gcloud beta run deploy ... --iap`.
#
# WHICH IMAGE?
#   Step 05 deploys exactly "${IMAGE}". After a build, paste the
#   "export IMAGE=..." line printed by 04_build_image.sh. To redeploy the
#   image that is ALREADY running (e.g. after changing only .env):
#     export IMAGE=$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" \
#       --format='value(spec.template.spec.containers[0].image)')
# =============================================================================
set -euo pipefail
: "${PROJECT_ID:?source deploy/00_variables.sh first}"
: "${IMAGE:?is not set. Run deploy/04_build_image.sh and paste the export IMAGE=... line it prints (see WHICH IMAGE? at the top of this script)}"
: "${PROJECT_NUMBER:?PROJECT_NUMBER missing - is gcloud logged in? re-source deploy/00_variables.sh}"
cd "$(dirname "$0")/.."
echo "Deploying image: ${IMAGE}"

# 1) Build the Cloud Run env file from the RUNTIME section of .env, forcing the
#    few values that must differ on Cloud Run.
#
#    CLAIMDESK_IAP_AUDIENCE: the app verifies the IAP-signed JWT
#    (x-goog-iap-jwt-assertion header) as defence in depth. For Cloud Run with
#    IAP enabled directly on the service, the documented audience is
#      /projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME
#    (https://cloud.google.com/iap/docs/signed-headers-howto). We compute it
#    here, so it always matches this deployment, whatever .env says.
IAP_AUDIENCE="/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${SERVICE_NAME}"
if [[ -n "${CLAIMDESK_IAP_AUDIENCE:-}" && "${CLAIMDESK_IAP_AUDIENCE}" != "${IAP_AUDIENCE}" ]]; then
  echo "NOTE: CLAIMDESK_IAP_AUDIENCE in .env (${CLAIMDESK_IAP_AUDIENCE}) is replaced by ${IAP_AUDIENCE}" >&2
fi
ENV_YAML="$(mktemp -t claimdesk-env-XXXX.yaml)"
trap 'rm -f "${ENV_YAML}"' EXIT   # delete the temp file even if a step fails
python3 deploy/render_env_yaml.py --out "${ENV_YAML}" \
  --set CLAIMDESK_STORAGE_BACKEND=gcp \
  --set CLAIMDESK_ENABLE_CLOUD_TRACE=true \
  --set CLAIMDESK_IAP_AUDIENCE="${IAP_AUDIENCE}"
echo "--- runtime configuration sent to Cloud Run ---"; cat "${ENV_YAML}"; echo "-----------------------------------------------"

# 2) Deploy. Sizing comes from the DEPLOY_* keys in .env.
#    (Cloud Run sets K_SERVICE / K_REVISION itself; the region and service
#    name the app needs are in CLAIMDESK_REGION and CLAIMDESK_IAP_AUDIENCE.)
gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${RUN_SA}" \
  --no-allow-unauthenticated \
  --iap \
  --ingress=all \
  --timeout="${DEPLOY_TIMEOUT_SECONDS:-3600}" \
  --session-affinity \
  --concurrency="${DEPLOY_CONCURRENCY:-20}" \
  --min-instances="${DEPLOY_MIN_INSTANCES:-0}" \
  --max-instances="${DEPLOY_MAX_INSTANCES:-1}" \
  --cpu="${DEPLOY_CPU:-1}" --memory="${DEPLOY_MEMORY:-2Gi}" \
  --no-cpu-throttling \
  --cpu-boost \
  --execution-environment=gen2 \
  --env-vars-file="${ENV_YAML}" \
  --labels=app=claimdesk,env=demo

# IAP needs permission to call the service on the user's behalf. Ensure the
# Google-managed IAP service agent exists in a brand-new project first.
gcloud beta services identity create --service=iap.googleapis.com --project="${PROJECT_ID}" >/dev/null 2>&1 || true
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --region="${REGION}" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" --format='value(status.url)'
echo "Check 'Iap Enabled: true' with: gcloud run services describe ${SERVICE_NAME} --region=${REGION}"
echo "Next: bash deploy/06_grant_iap_access.sh"
# If gcloud warns that IAP must be set up in the Console first (projects
# without an organization), enable it once in Console > Cloud Run > service >
# Security > "Identity-Aware Proxy (IAP)", then re-run. See docs/deploy_runbook.md.
