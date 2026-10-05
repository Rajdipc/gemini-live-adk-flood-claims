#!/usr/bin/env bash
# =============================================================================
# 06_grant_iap_access.sh - allow exactly ONE person (you) through IAP.
# =============================================================================
# roles/iap.httpsResourceAccessor = "may pass through IAP to this service".
# It is granted on THIS Cloud Run service only, to your user account only.
# Nobody else (not even other people in your org) can open the app.
#
# To add someone later:   run the same command with --member="user:THEIR_EMAIL"
# To remove access:        gcloud iap web remove-iam-policy-binding ... (same flags)
#
# gcloud VERSION: `--resource-type=cloud-run` needs a recent gcloud (Cloud
# Shell is up to date). If it is rejected, run `gcloud components update`,
# or try the same command as `gcloud beta iap web ...`.
# =============================================================================
set -euo pipefail
: "${PROJECT_ID:?source deploy/00_variables.sh first}"
: "${USER_EMAIL:?set DEPLOY_IAP_USER_EMAIL in .env, then re-source deploy/00_variables.sh}"
[[ "${USER_EMAIL}" != "you@example.com" ]] || { echo "Edit DEPLOY_IAP_USER_EMAIL in .env (still you@example.com), then re-source deploy/00_variables.sh" >&2; exit 1; }

# Optional: more accounts, comma-separated, e.g.
#   DEPLOY_IAP_EXTRA_USERS=alice@example.com,bob@example.com
# Leave it empty (the default) to keep the app single-user.
IAP_USERS=("${USER_EMAIL}")
if [[ -n "${DEPLOY_IAP_EXTRA_USERS:-}" ]]; then
  IFS=',' read -r -a _extra <<< "${DEPLOY_IAP_EXTRA_USERS}"
  for _u in "${_extra[@]}"; do
    _u="${_u// /}"
    [[ -n "${_u}" ]] && IAP_USERS+=("${_u}")
  done
fi

_failed=()
for _u in "${IAP_USERS[@]}"; do
  # A grant can be refused, e.g. by the org policy
  # constraints/iam.allowedPolicyMemberDomains for accounts outside your
  # organization. Keep going so the other accounts still get access.
  if gcloud iap web add-iam-policy-binding \
      --member="user:${_u}" \
      --role="roles/iap.httpsResourceAccessor" \
      --region="${REGION}" \
      --resource-type=cloud-run \
      --service="${SERVICE_NAME}" >/dev/null; then
    echo "granted roles/iap.httpsResourceAccessor to ${_u}"
  else
    echo "WARNING: could not grant IAP access to ${_u} (see the error above)" >&2
    _failed+=("${_u}")
  fi
done

echo "Current IAP policy:"
gcloud iap web get-iam-policy --region="${REGION}" --resource-type=cloud-run --service="${SERVICE_NAME}"

URL="$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" --format='value(status.url)')"
echo
echo "Open ${URL} in a browser signed in as ${USER_EMAIL}."
echo "Test privacy: open it in an incognito window with another account -> you must get 'You don't have access'."
echo "Test no-auth: curl -s -o /dev/null -w '%{http_code}\n' ${URL}/api/health   # expect 302/401/403, never 200"

if (( ${#_failed[@]} > 0 )); then
  echo "WARNING: IAP access NOT granted to: ${_failed[*]}" >&2
  echo "  If the error mentions iam.allowedPolicyMemberDomains, the account is outside your organization." >&2
  exit 1
fi
