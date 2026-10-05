# Manual deployment runbook

> [!CAUTION]
> Nothing in this repository deploys automatically. Each step below creates real, billable Google Cloud resources. **Read each script before running it.** Run the steps in order, one at a time. Expected cost for a single user is about \$15/month, almost all of it Gemini usage; see [cost_analysis.md](cost_analysis.md).

> [!NOTE]
> **Demo scope reminder.** Flood-only (NFIP-style) claims in CO, TX, FL, LA and NC. Policy numbers and holder names are **generated**; every other policy attribute comes from real FEMA OpenFEMA records. See the [README](../README.md#1-scope-disclaimers-brand-vs-code-names).

## 0. Prerequisites (once)

| Need | How |
|---|---|
| A GCP project with billing | Console → *New project*. Note the **project ID**. |
| Your roles on the project | Easiest for a personal project is **Owner**. Minimum: Cloud Run Admin, IAP Policy Admin, Service Account Admin, Project IAM Admin, Storage Admin, BigQuery Admin, Cloud Build Editor, Artifact Registry Admin, Datastore Owner. |
| gcloud CLI | `gcloud version`. Then log in: `gcloud auth login` and `gcloud auth application-default login`. |
| Python deps | See [README step 4.2](../README.md#4-set-up-and-deploy-step-by-step-cloud-shell) (`uv sync --extra dev --extra data`). |

```bash
cd ~/gemini-live-adk-flood-claims   # the folder you cloned or unpacked
# ONE config file for the app AND these scripts:
cp .env.example .env
$EDITOR .env        # set GOOGLE_CLOUD_PROJECT, CLAIMDESK_GCS_BUCKET, DEPLOY_IAP_USER_EMAIL
source deploy/00_variables.sh   # loads .env safely and prints what will be used
```

## 1. Enable APIs: `deploy/01_enable_apis.sh`

```bash
bash deploy/01_enable_apis.sh
```

✅ **Check:** `gcloud services list --enabled | grep -E "run|aiplatform|iap|bigquery"`

## 2. Phase 0: confirm the models work for your project

```bash
# .env already exists from step 0 (do NOT copy .env.example again - it would overwrite your edits)
uv run --no-sync python scripts/check_models.py
```

Expected result: the flash, image and live models work on `global`. If live fails on `global` but works on `us-central1`, nothing needs to change. The app falls back automatically via `LIVE_MODEL_FALLBACK_LOCATION`.

## 3. Runtime service account: `deploy/02_service_account_iam.sh`

```bash
bash deploy/02_service_account_iam.sh
```

✅ **Check:** `gcloud projects get-iam-policy $PROJECT_ID --flatten=bindings --filter="bindings.members:$RUN_SA" --format="value(bindings.role)"`

## 4. Storage and Firestore: `deploy/03_storage_firestore_bigquery.sh` (first run)

The data pipeline writes raw FEMA files and the NOAA/ZIP export into the bucket, so create storage **before** loading data.

```bash
bash deploy/03_storage_firestore_bigquery.sh
```

On this first run it says the dataset isn't found yet. That's expected.

✅ **Check:**
- `gcloud storage buckets describe gs://$BUCKET --format="value(public_access_prevention)"` shows `enforced`.
- `gcloud firestore databases list`

## 5. Load the data into BigQuery, then grant access

Follow [data_loading.md](data_loading.md). It creates the dataset `claimdesk` in `us-central1`, with these tables:
- Staging: `raw_nfip_policies`, `raw_nfip_claims`
- Reference data: `policy_registry`, `loss_benchmarks`, `nfip_claims_clean`, `noaa_flood_events`, `zip_points`
- Evals: `eval_seed_claims`
- App outputs: `intake_packets`, `conversation_traces`

`data_pipeline/load_to_bigquery.py` runs 4 steps: `create`, then `load` (FEMA files from GCS), then `reference`, then `transform`. The `reference` step is the only thing that touches the `US` location. It runs an `EXPORT DATA` job of the filtered NOAA storms and ZIP rows (≈100 MB scanned, ≈\$0) into **your us-central1 bucket**, then loads them into `noaa_flood_events` and `zip_points` in us-central1. Nothing gets stored in `US`. Because the export writes to the bucket as *you*, your account needs `roles/storage.objectAdmin` on the bucket. A project Owner already has it.

If the BigQuery client returns 403 about a *quota project*, run `gcloud auth application-default set-quota-project $PROJECT_ID`.

Then **re-run** `bash deploy/03_storage_firestore_bigquery.sh`. It now finds the dataset and grants the service account `dataEditor` on it, and only on it.

### 5b. Optional, recommended: grounding with Vertex AI Search (`deploy/03b_vertex_ai_search.sh`)

This indexes FEMA NFIP documents in Vertex AI Search, in the **`us` multi-region**, the one resource outside us-central1. Maya can then answer general questions from them.
```bash
uv run --no-sync python -m grounding.fetch_fema_docs --upload   # manual browser download if fema.gov returns 403
bash deploy/03b_vertex_ai_search.sh
```
The script waits for each long-running operation (data store, import, engine) and stops with an error if the import fails or no documents appear, printing `successCount` / `failureCount` / `errorSamples`. Then set `CLAIMDESK_SEARCH_ENGINE_ID=fema-nfip-engine` and `CLAIMDESK_ENABLE_GUIDANCE_SEARCH=true` in `.env`, before step 7. Full guide: [grounding.md](grounding.md).

## 6. Build the image: `deploy/04_build_image.sh`

```bash
bash deploy/04_build_image.sh
export IMAGE=...   # paste the "export IMAGE=..." line the script prints
```

Every run of `04` picks a **new tag** (UTC time, plus the git commit if the folder is a git checkout), so re-building in the same terminal never reuses an old tag. `00_variables.sh` no longer sets `IMAGE`; step 7 refuses to run until you export it. Optional: `bash deploy/04_build_image.sh v1` to choose the tag.

## 7. Deploy the private service: `deploy/05_deploy_cloud_run.sh`

```bash
bash deploy/05_deploy_cloud_run.sh
```

- **Projects without an organization** (for example, personal Gmail projects): if gcloud says IAP must be set up in the Console first, open Console → Cloud Run → `demo-tideline` (your `SERVICE_NAME`) → **Security** → *Require authentication* → **Identity-Aware Proxy**, and save. Then re-run the script.
- The script sets `CLAIMDESK_IAP_AUDIENCE=/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME` on the service automatically (the documented audience for IAP on Cloud Run; any value in `.env` is replaced). The app verifies the IAP-signed JWT with it, as defence in depth. If you see 401s from the app (not from IAP), compare this value with the `aud` claim of the `x-goog-iap-jwt-assertion` header in the request logs.
- `--iap` needs a recent gcloud. Cloud Shell is up to date; elsewhere run `gcloud components update` (or use `gcloud beta run deploy`).
- To redeploy the image that is already running (for example after changing only `.env`): `export IMAGE=$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" --format='value(spec.template.spec.containers[0].image)')`, then run the script.

✅ **Check:** `gcloud run services describe $SERVICE_NAME --region=$REGION | grep -i "iap"` shows `Iap Enabled: true`.

## 8. Allow only yourself: `deploy/06_grant_iap_access.sh`

```bash
bash deploy/06_grant_iap_access.sh
```

✅ **Privacy tests:**
1. Browser signed in as you: the app loads.
2. Incognito window with a different Google account: *"You don't have access"*.
3. `curl -s -o /dev/null -w '%{http_code}\n' https://<url>/api/health` returns 302/401/403, **never 200**.

Then run the automated read-only checks, `bash scripts/post_deploy_checks.sh`, and the full test plan in [post_deployment_tests.md](post_deployment_tests.md).

## 9. Optional: budget and error alerts (`deploy/07_budget_and_alerts.sh`)

```bash
BUDGET_USD=25 bash deploy/07_budget_and_alerts.sh
```

Safe to re-run: the budget is created only if no budget named `<SERVICE_NAME>-monthly` exists, and the log metric is created or updated. It needs a billing account linked to the project (or `export BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX`) and billing permissions on it.

## 10. Using and observing it

| What | Where |
|---|---|
| Logs for one request | Logs Explorer: `resource.type="cloud_run_revision" resource.labels.service_name="demo-tideline"`. Click a line, then *Show entries for this trace*. |
| Logs for one intake | Add `jsonPayload.intake_id="..."` to the query. |
| Exceptions (grouped) | Console → **Error Reporting** |
| Latency breakdown | Console → **Trace explorer** (ADK agent/LLM spans and BigQuery node spans) |
| Packets | BigQuery: `SELECT * FROM claimdesk.intake_packets ORDER BY created_at DESC` |
| Conversation traces | BigQuery: `claimdesk.conversation_traces`, which feeds [evals](evals.md) |
| Files | `gcloud storage ls gs://$BUCKET/intakes/` |

## 11. Updating, rolling back, pausing, deleting

```bash
# Update: rebuild and redeploy (steps 6-7). Each build gets a new tag, each deploy a new revision.
#   bash deploy/04_build_image.sh && export IMAGE=<printed value> && bash deploy/05_deploy_cloud_run.sh
gcloud run revisions list --service=$SERVICE_NAME --region=$REGION

# Roll back instantly to an earlier revision:
gcloud run services update-traffic $SERVICE_NAME --region=$REGION --to-revisions=REVISION_NAME=100

# Pause: with min-instances=0 an unused service costs ~$0. To block access entirely:
gcloud iap web remove-iam-policy-binding --member="user:$USER_EMAIL" --role=roles/iap.httpsResourceAccessor \
  --region=$REGION --resource-type=cloud-run --service=$SERVICE_NAME

# Delete everything (irreversible): use the destroy script. It asks you to type the
# project id, then removes Cloud Run, Vertex AI Search, the image repo, the budget,
# BigQuery, the bucket and the service account (see RUNBOOK Phase 13).
bash deploy/99_destroy.sh            # --keep-data / --include-firestore
```

## 12. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| IAP page: "You don't have access" as yourself | Step 8 not done, or you're signed in with another account | Re-run `06_grant_iap_access.sh`. Check the account in the browser. |
| `403 Forbidden` from Cloud Run (not the IAP page) | The IAP service agent lacks `run.invoker` | Re-run the `add-iam-policy-binding` at the end of step 7. |
| App returns 401 "invalid IAP assertion" | Wrong `CLAIMDESK_IAP_AUDIENCE` (e.g. service renamed or moved) | Re-run step 7; it recomputes the audience. `bash scripts/post_deploy_checks.sh` (L0-06b) shows the value. |
| `PermissionDenied: aiplatform...` | SA missing `roles/aiplatform.user`, or the API is disabled | Steps 1 and 3. |
| `NotFound` for a model | Model not offered at that location | `scripts/check_models.py`, then change `*_LOCATION`. |
| `Access Denied: BigQuery ... dataset claimdesk` | Dataset grant missing | Re-run step 5 after loading the data. |
| `Not found: Dataset ... was not found in location US` | A job ran in the wrong location | Every app and transform query must run in `us-central1` (`CLAIMDESK_BQ_LOCATION`). Only the one-time NOAA/ZIP `EXPORT DATA` step runs in `US`. |
| WebSocket disconnects after exactly 60 min | Cloud Run request timeout | Expected. The app ends live sessions at 20 min. |
| Cold start of ~5 s on the first request | `min-instances=0` | Expected. Set `--min-instances=1` if it bothers you (about \$30–60/month). |
| `Guidance search unavailable` in the logs | SA missing `roles/discoveryengine.viewer`, or wrong `CLAIMDESK_SEARCH_ENGINE_ID` | Re-run step 3 (02 script) and check `.env`. See [grounding.md](grounding.md). |
| Container exits with "Set GOOGLE_CLOUD_PROJECT" | Env vars not passed | Check the runtime configuration that step 7 prints (generated from `.env` by `deploy/render_env_yaml.py`). |
