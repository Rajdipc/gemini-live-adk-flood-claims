#!/usr/bin/env bash
# =============================================================================
# 07_budget_and_alerts.sh (OPTIONAL but strongly recommended)
# =============================================================================
# A budget does NOT stop spending - it EMAILS you at 50/90/100% of the amount.
# For a single-user demo, US$25/month is generous (see docs/cost_analysis.md).
# Also creates a log-based metric that counts ERROR logs from the service.
#
# SAFE TO RE-RUN
#   The budget is looked up by its display name and created only if missing
#   (billing budgets allow duplicate names, so a blind "create" would add a
#   second budget every run). The log metric is created, or updated if it
#   already exists.
#
# PERMISSIONS: creating a budget needs Billing Account Administrator or
#   Billing Account Costs Manager on the billing account (not just project
#   Owner). Without it, create the budget in Console > Billing > Budgets.
# =============================================================================
set -euo pipefail
: "${PROJECT_ID:?source deploy/00_variables.sh first}"
: "${SERVICE_NAME:?source deploy/00_variables.sh first}"
BUDGET_USD="${BUDGET_USD:-${DEPLOY_BUDGET_USD:-25}}"
# Names are derived from SERVICE_NAME so 99_destroy.sh can find them again.
BUDGET_NAME="${SERVICE_NAME}-monthly"
METRIC_NAME="${SERVICE_NAME//-/_}_errors"
METRIC_FILTER="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE_NAME}\" AND severity>=ERROR"

# Billing account linked to the project (e.g. 012345-6789AB-CDEF01).
# You can also set BILLING_ACCOUNT yourself before running this script.
if [[ -z "${BILLING_ACCOUNT:-}" ]]; then
  BILLING_ACCOUNT="$(gcloud billing projects describe "${PROJECT_ID}" --format='value(billingAccountName)' 2>/dev/null | sed 's#billingAccounts/##')" || true
fi
: "${BILLING_ACCOUNT:?no billing account found for ${PROJECT_ID}. Link one (Console > Billing) or export BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX and re-run}"

echo "== Budget '${BUDGET_NAME}' (US\$${BUDGET_USD}/month) on billing account ${BILLING_ACCOUNT}"
if ! BUDGET_NAMES="$(gcloud billing budgets list --billing-account="${BILLING_ACCOUNT}" --format='value(displayName)')"; then
  echo "Could not list budgets (need Billing Account Costs Manager/Administrator). Create it in Console > Billing > Budgets." >&2
  exit 1
fi
EXISTING_BUDGET="$(printf '%s\n' "${BUDGET_NAMES}" | grep -Fx -- "${BUDGET_NAME}" || true)"
if [[ -n "${EXISTING_BUDGET}" ]]; then
  echo "(budget ${BUDGET_NAME} already exists - not creating a duplicate)"
  echo "  To change the amount: Console > Billing > Budgets & alerts, or delete it and re-run."
else
  gcloud billing budgets create \
    --billing-account="${BILLING_ACCOUNT}" \
    --display-name="${BUDGET_NAME}" \
    --budget-amount="${BUDGET_USD}USD" \
    --filter-projects="projects/${PROJECT_ID}" \
    --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
  echo "created budget ${BUDGET_NAME}"
fi
# Budget emails go to billing admins of the account. To route elsewhere,
# attach a Monitoring notification channel in the Console (Billing > Budgets).

echo "== Log-based metric '${METRIC_NAME}' (ERROR+ logs from ${SERVICE_NAME})"
if gcloud logging metrics describe "${METRIC_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud logging metrics update "${METRIC_NAME}" --project="${PROJECT_ID}" \
    --description="ERROR+ logs from the ${SERVICE_NAME} Cloud Run service" \
    --log-filter="${METRIC_FILTER}"
  echo "updated metric ${METRIC_NAME}"
else
  gcloud logging metrics create "${METRIC_NAME}" --project="${PROJECT_ID}" \
    --description="ERROR+ logs from the ${SERVICE_NAME} Cloud Run service" \
    --log-filter="${METRIC_FILTER}"
  echo "created metric ${METRIC_NAME}"
fi
echo "Create an alert on metric logging.googleapis.com/user/${METRIC_NAME} in Console > Monitoring > Alerting,"
echo "and view grouped exceptions in Console > Error Reporting."
