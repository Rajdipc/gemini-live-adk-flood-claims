#!/usr/bin/env bash
# =============================================================================
# 01_enable_apis.sh - turn on the Google Cloud services ClaimDesk uses.
# =============================================================================
# A new project has most APIs switched off. Enabling an API is free; you pay
# only for usage. Each line below says why we need it.
#
# Run:  source deploy/00_variables.sh && bash deploy/01_enable_apis.sh
# =============================================================================
set -euo pipefail
: "${PROJECT_ID:?source deploy/00_variables.sh first}"

gcloud config set project "${PROJECT_ID}"

APIS=(
  run.googleapis.com                  # Cloud Run - hosts the web app
  artifactregistry.googleapis.com     # stores the container image
  cloudbuild.googleapis.com           # builds the image from the Dockerfile
  aiplatform.googleapis.com           # Vertex AI - Gemini models (live, flash, image) + Gen AI evaluation
  bigquery.googleapis.com             # policy registry, benchmarks, NOAA check, packets, traces
  storage.googleapis.com              # evidence photos, sketches, packet ZIPs, raw FEMA files
  firestore.googleapis.com            # live intake state (survives restarts)
  iap.googleapis.com                  # Identity-Aware Proxy - makes the app private
  logging.googleapis.com              # Cloud Logging (structured JSON logs)
  cloudtrace.googleapis.com           # Cloud Trace (OpenTelemetry spans)
  clouderrorreporting.googleapis.com  # Error Reporting (groups exceptions from logs)
  monitoring.googleapis.com           # dashboards / alerts
  iam.googleapis.com                  # service accounts
  billingbudgets.googleapis.com       # budget alert (see step 07, optional)
  discoveryengine.googleapis.com      # Vertex AI Search - grounding on FEMA NFIP documents (step 03b)
)

gcloud services enable "${APIS[@]}"
echo "Enabled: ${APIS[*]}"
# NOTE: Pub/Sub is intentionally NOT enabled - not needed yet (see docs/architecture.md).
