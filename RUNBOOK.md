# Demo Tideline runbook (Cloud Shell edition)

This is the one ordered path from an empty Google Cloud project to a private, grounded and tested **Demo Tideline** flood-claims agent, and back to an empty project. **Every command runs in [Cloud Shell](https://shell.cloud.google.com)**. You don't need anything installed on your computer except Chrome or Edge.

> [!CAUTION]
> Nothing deploys by itself: you run each step. From Phase 5 on, real billable resources are created. For a single user, expect roughly:
> - **\$15–20/month**, mostly Gemini, plus Vertex AI Search if you enable it;
> - about **\$5** for a full round of testing.
>
> See [cost_analysis.md](docs/cost_analysis.md). Phase 13 deletes everything.

> [!IMPORTANT]
> **Demo scope.**
> - **What it is:** a flood-only (NFIP-style) intake desk for the fictitious insurer **Demo Tideline**, covering CO, TX, FL, LA and NC.
> - **Data:** policy numbers and names are **generated**. All other policy attributes come from real FEMA OpenFEMA records. *This product uses the FEMA OpenFEMA API and FEMA documents, but is not endorsed by FEMA.*
> - **Decisions:** the agent never makes coverage decisions.
>
> **Regions.** Everything runs in **`us-central1`**, and the Gemini models are called on the **`global`** endpoint. The **one exception** is **Vertex AI Search**, in the **`us` multi-region**, because search data stores only exist in `global`, `us` or `eu`.

> [!NOTE]
> **Names.**
> - The GitHub repository and the Cloud Shell folder are `gemini-live-adk-flood-claims`.
> - What people see is the brand: the *Demo Tideline* page, the `demo-tideline` Cloud Run service, the `demo-tideline-run` service account and the `demo-tideline` image repo.
> - Internal code names stay `claimdesk`: the Python package, the BigQuery dataset, the Firestore collection and the `app=claimdesk` labels.
> - Change the brand with `CLAIMDESK_BRAND_NAME` in `.env`.

---

## At a glance

| # | Phase | Creates resources? | Time |
|---|---|---|---|
| 1 | [Open Cloud Shell and bring the code in](#phase-1-open-cloud-shell-and-bring-the-code-in) | No | 10 min |
| 2 | [Install and run the offline tests](#phase-2-install-and-run-the-offline-tests) | No | 5 min |
| 3 | [Fill in `.env`](#phase-3-fill-in-env-the-only-config-file) | No | 5 min |
| 4 | [Project, APIs, model check](#phase-4-project-apis-and-model-check) | APIs only | 10 min |
| 5 | [Service account, bucket, Firestore](#phase-5-service-account-bucket-firestore) | **Yes** | 5 min |
| 6 | [Load the data into BigQuery](#phase-6-load-the-data-into-bigquery) | **Yes** | 30–60 min |
| 7 | [Grounding: Vertex AI Search on FEMA documents](#phase-7-grounding-vertex-ai-search-on-fema-documents) | **Yes** (`us`) | 30 min |
| 8 | [Pipeline evals, before and after the skill](#phase-8-pipeline-evals-before-and-after-the-skill) | No | 20 min |
| 9 | [Build and deploy privately](#phase-9-build-and-deploy-privately) | **Yes** | 15 min |
| 10 | [Test the deployed app](#phase-10-test-the-deployed-app) | Test data | 2–4 h |
| 11 | [Grade the test calls (live evals)](#phase-11-grade-the-test-calls-live-evals) | Eval results | 15 min |
| 12 | [Update, roll back, pause](#phase-12-update-roll-back-pause) | – | – |
| 13 | [Destroy everything](#phase-13-destroy-everything) | Deletes | 10 min |

### Four Cloud Shell habits

1. **Work inside `tmux`**, so commands keep running if the browser tab closes. Start a session with `tmux new -s claimdesk`. After reconnecting, rejoin it with `tmux attach -t claimdesk`.
2. **In every new terminal tab, run** `cd ~/gemini-live-adk-flood-claims && source deploy/00_variables.sh`.
3. **Cloud Shell is already logged in as you.** `gcloud`, `bq` and the Python libraries all use your account, so you don't need `gcloud auth login` or API keys.
4. **Only your 5 GB home folder survives between sessions.** The code, `.venv` and data all live under `~/gemini-live-adk-flood-claims`. After a long idle period, re-run step 2.1 to put `uv` back on your PATH.

---

## Phase 1: Open Cloud Shell and bring the code in

1. Open https://shell.cloud.google.com, signed in as the account you'll use (the same account as `DEPLOY_IAP_USER_EMAIL`).
2. Clone the repository into `~/gemini-live-adk-flood-claims`:
   ```bash
   git clone https://github.com/Rajdipc/gemini-live-adk-flood-claims.git ~/gemini-live-adk-flood-claims
   cd ~/gemini-live-adk-flood-claims && ls
   ```

> [!TIP]
> Click **Open Editor** in Cloud Shell for a VS Code-style editor. You'll use it for `.env` in Phase 3.

---

## Phase 2: Install and run the offline tests

**2.1 Install `uv`**, the Python package manager this project uses. You do this once per Cloud Shell home.
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc          # puts ~/.local/bin on PATH
uv --version
```

**2.2 Install the project's packages**
```bash
cd ~/gemini-live-adk-flood-claims
uv sync --extra dev --extra data
```
From now on, run everything with `uv run --no-sync ...`.

**2.3 Run the offline tests.** They're free, because Gemini, BigQuery and Vertex AI Search are faked.
```bash
GOOGLE_CLOUD_PROJECT=x uv run --no-sync pytest -q -p no:warnings     # ~560 tests
node tests/test_desk_ui.cjs                                           # 44 UI tests (Node is preinstalled)
```
✅ All green. The tests include the Agent Skill: it loads through ADK's `load_skill_from_dir`, and has no braces in its files.

**2.4 Install the eval CLI**, used in Phases 8 and 11:
```bash
uv tool install "google-agents-cli~=1.3.1"
agents-cli --version
```

---

## Phase 3: Fill in `.env` (the only config file)

```bash
cp .env.example .env
cloudshell edit .env      # or: nano .env
```

Change the three lines marked `<-- EDIT`:

| Key | Example | Why |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | `my-tideline-123` | The project that holds and pays for everything |
| `CLAIMDESK_GCS_BUCKET` | `my-tideline-123-claimdesk` | Must be globally unique. Holds photos, packets, raw data and FEMA PDFs. |
| `DEPLOY_IAP_USER_EMAIL` | `you@example.com` | The **only** account allowed to open the app |

Leave everything else as it is for now:
- region `us-central1`, models on `global`;
- brand *Demo Tideline*, service `demo-tideline`;
- `CLAIMDESK_USE_SKILL=true`;
- `CLAIMDESK_ENABLE_GUIDANCE_SEARCH=false` (Phase 7 turns it on);
- default limits.

Every line in the file has a comment explaining it.

> [!NOTE]
> Don't `source .env` directly: values with spaces break it. `deploy/00_variables.sh` loads it safely, and `deploy/render_env_yaml.py` turns the same file into Cloud Run's environment, so one file drives everything.

---

## Phase 4: Project, APIs and model check

```bash
cd ~/gemini-live-adk-flood-claims
source deploy/00_variables.sh            # prints PROJECT_ID, REGION=us-central1, SERVICE=demo-tideline, ...
gcloud config set project "$PROJECT_ID"
bash deploy/01_enable_apis.sh            # Cloud Run, Vertex AI, IAP, BigQuery, Firestore, Storage, Discovery Engine, ...
```

Check that the three fixed models answer for your project:
```bash
uv run --no-sync python scripts/check_models.py        # add --skip-image to save a few cents
```
✅ **Expect:**
- `gemini-3.8-flash` and `gemini-3.1-flash-image` OK on `global`;
- `gemini-3.8-live` OK on `global` **or** on `us-central1`. The app falls back to `us-central1` automatically.

❌ **If it fails:**
- `PermissionDenied`: your account needs the Vertex AI User role.
- `SERVICE_DISABLED`: wait a minute and retry.

---

## Phase 5: Service account, bucket, Firestore

```bash
bash deploy/02_service_account_iam.sh          # least-privilege identity "demo-tideline-run"
bash deploy/03_storage_firestore_bigquery.sh   # private bucket, Firestore + TTL (first run)
```
- **Expect:** 03 ends with *"dataset not found yet"*. That's normal: Phase 6 creates the dataset.
- ✅ **Check:**
  ```bash
  gcloud storage buckets describe "gs://$BUCKET" --format="value(location,public_access_prevention)"
  #  US-CENTRAL1   enforced
  ```

---

## Phase 6: Load the data into BigQuery

- What each table holds: [data_dictionary.md](docs/data_dictionary.md).
- Detailed guide: [data_loading.md](docs/data_loading.md). You can skip its sections 1, 2 and 4, because Phases 2, 4 and 5 already did them.

```bash
tmux new -s data
cd ~/gemini-live-adk-flood-claims && source deploy/00_variables.sh
```

**6.1 How much data?** This downloads nothing.
```bash
uv run --no-sync python -m data_pipeline.fetch_openfema --counts-only
```

**6.2 Download from FEMA and upload to your bucket.** This takes 20–40 minutes and resumes if interrupted.
```bash
uv run --no-sync python -m data_pipeline.fetch_openfema --gzip --upload
gcloud storage du -s -r "gs://$BUCKET/raw/openfema/"
```
To detach, press `Ctrl+B` then `D`. To re-attach: `tmux attach -t data`.

**6.3 Preview** (dry run, changes nothing):
```bash
uv run --no-sync python -m data_pipeline.load_to_bigquery --dry-run
```

**6.4 Load.** Four steps, about 5 minutes:
```bash
uv run --no-sync python -m data_pipeline.load_to_bigquery
```

| Step | Result |
|---|---|
| `create` | Dataset `claimdesk` in **us-central1**, plus the app tables `intake_packets` and `conversation_traces` |
| `load` | FEMA files → `raw_nfip_policies`, `raw_nfip_claims` |
| `reference` | NOAA storm events and ZIP locations for your 5 states → `noaa_flood_events`, `zip_points` |
| `transform` | `policy_registry`, `loss_benchmarks`, `nfip_claims_clean`, `eval_seed_claims` |

**6.5 Verify**
```bash
bq show --format=prettyjson "$PROJECT_ID:claimdesk" | grep '"location"'    # "us-central1"
bq query --location=us-central1 --use_legacy_sql=false \
  "SELECT table_id, row_count FROM \`$PROJECT_ID.claimdesk.__TABLES__\` ORDER BY table_id"
```
✅ All the tables exist, and all except the two app tables have rows.

**6.6 Let the app's service account read the dataset.** This is the second run of 03:
```bash
bash deploy/03_storage_firestore_bigquery.sh
```

---

## Phase 7: Grounding: Vertex AI Search on FEMA documents

This phase is optional but recommended for accuracy. It lets Maya answer general questions, such as *"is my basement covered?"* or *"when is the proof of loss due?"*, **from FEMA's own documents** instead of from memory. The full explanation is in [grounding.md](docs/grounding.md).

> [!IMPORTANT]
> Vertex AI Search is created in the **`us` multi-region**. That's the project's only resource outside `us-central1`, because search data stores don't exist in single regions. Cost: roughly **\$4 per 1,000 searches** (Enterprise tier). One tester makes a few dozen.

**7.1 Get the FEMA PDFs into the bucket**
```bash
cd ~/gemini-live-adk-flood-claims && source deploy/00_variables.sh
uv run --no-sync python -m grounding.fetch_fema_docs --upload
```
fema.gov often blocks scripted downloads with **403**. For each `[MISSING]` line the script prints a FEMA page and a file name.
1. Open that page in your browser and download the PDF.
2. In Cloud Shell, click **⋮ → Upload**, then rename the file into place:
   ```bash
   mv ~/<downloaded-file>.pdf ~/gemini-live-adk-flood-claims/grounding/raw/sfip_dwelling_form.pdf      # use the name the script printed
   ```
3. Re-run `uv run --no-sync python -m grounding.fetch_fema_docs --upload`.

✅ You see `Uploaded N file(s)`, with at least `sfip_dwelling_form.pdf` and `nfip_claims_handbook.pdf`.

**7.2 Create the data store, import the PDFs, create the search engine**
```bash
bash deploy/03b_vertex_ai_search.sh
```
The script now **waits** for each long-running step: creating the data store, importing the PDFs (5–45 minutes), creating the engine, then indexing. It prints the import's success and failure counts. ✅ It ends with a test query result containing `"results"`. ❌ It **exits with an error** (and a hint) if the import fails or no documents are indexed in time. It's safe to re-run. Longer waits: `IMPORT_TIMEOUT_S=3600 INDEX_TIMEOUT_S=1800 bash deploy/03b_vertex_ai_search.sh`.

**7.3 Turn grounding on.** Run this in Cloud Shell (or set `CLAIMDESK_ENABLE_GUIDANCE_SEARCH=true` in `.env` by hand):
```bash
sed -i 's/^CLAIMDESK_ENABLE_GUIDANCE_SEARCH=.*/CLAIMDESK_ENABLE_GUIDANCE_SEARCH=true/' .env
source deploy/00_variables.sh
```
The next deploy (Phase 9) picks this up automatically.

**Optional:** try it in the Console, under **AI Applications → Apps → fema-nfip-engine → Preview**.

---

## Phase 8: Pipeline evals, before and after the skill

This measures how accurately the ADK pipeline extracts facts, identifies the water source and routes, on 16 hand-checked cases. Running it **without** and then **with** the Agent Skill shows what the skill adds. The full guide is [evals.md](docs/evals.md).

```bash
cd ~/gemini-live-adk-flood-claims && source deploy/00_variables.sh

# 8.1 BEFORE: pipeline without the skill (calls gemini-3.8-flash, a few cents)
CLAIMDESK_USE_SKILL=false GOOGLE_CLOUD_PROJECT=$PROJECT_ID uv run --no-sync python -m evals.generate_traces \
  --dataset evals/datasets/pipeline_core.json --dataset evals/datasets/pipeline_edge.json
BEFORE=$(ls -t evals/results/traces/*.json | head -1)

# 8.2 AFTER: pipeline with the skill (the default and what gets deployed)
GOOGLE_CLOUD_PROJECT=$PROJECT_ID uv run --no-sync python -m evals.generate_traces \
  --dataset evals/datasets/pipeline_core.json --dataset evals/datasets/pipeline_edge.json
AFTER=$(ls -t evals/results/traces/*.json | head -1)

# 8.3 Grade both the same way: free code metrics first, then the LLM-judge metrics
for T in "$BEFORE" "$AFTER"; do
  agents-cli eval grade --traces "$T" --config evals/eval_config.yaml --output "evals/results/grade_$(basename "$T" .json)" \
    --metrics fact_extraction_accuracy,routing_correct,water_source_correct,claim_type_correct
done
GOOGLE_CLOUD_PROJECT=$PROJECT_ID agents-cli eval grade --traces "$AFTER" \
  --config evals/eval_config.yaml --output evals/results/grade_after_full
```
- ✅ **Pass bar for AFTER:** code metrics 1.0 on the core set, and `no_coverage_promise` 1.0. AFTER should be at least as good as BEFORE on every metric.
- **Per-case explanations:** run `cloudshell download evals/results/grade_after_full/<results>.html` and open the file in your browser.

---

## Phase 9: Build and deploy privately

```bash
cd ~/gemini-live-adk-flood-claims && source deploy/00_variables.sh

bash deploy/04_build_image.sh          # Cloud Build -> Artifact Registry "demo-tideline" (us-central1), fresh tag every run
export IMAGE=...                       # paste the "export IMAGE=<repo>:<YYYYMMDD-HHMMSS>" line it prints (05 stops if IMAGE is not set)

bash deploy/05_deploy_cloud_run.sh     # Cloud Run "demo-tideline": no public access, IAP on, env from .env
bash deploy/06_grant_iap_access.sh     # only DEPLOY_IAP_USER_EMAIL may enter; prints the URL
bash deploy/07_budget_and_alerts.sh    # optional: budget e-mails + a metric on ERROR logs
```
- **Projects without an organization:** if 05 says IAP must be set up in the Console, do it there and run 05 again. Go to Console → **Cloud Run** → `demo-tideline` → **Security** → *Identity-Aware Proxy*, then save.
- 05 prints the runtime settings it sends. ✅ Check that the list includes `CLAIMDESK_ENABLE_GUIDANCE_SEARCH: "true"` if you did Phase 7, and `CLAIMDESK_IAP_AUDIENCE: "/projects/<number>/locations/us-central1/services/demo-tideline"`. 05 always computes that audience; the app uses it to **verify IAP's signed header** and refuses requests without it.
- `--iap` and `gcloud iap web ... --resource-type=cloud-run` need a recent gcloud. If 05 or 06 says a flag is unknown, run `gcloud components update` (Cloud Shell is normally current), or use `gcloud beta ...`.
- 07 is safe to re-run: it creates the budget and the log metric only if they're missing. To skip the billing-account prompt, run `export BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX` first.
- More help: [deploy_runbook.md](docs/deploy_runbook.md).

---

## Phase 10: Test the deployed app

All testing happens **in the deployed app**, in your browser, signed in as `DEPLOY_IAP_USER_EMAIL`. Get the URL:
```bash
gcloud run services describe "$SERVICE_NAME" --region="$REGION" --format='value(status.url)'
```
1. Open the URL. ✅ The **Demo Tideline** page loads, with your email in the header.
2. Open `<URL>/api/health`. ✅ You should see `"skill_loaded": true`, and `"guidance_search": true` if you did Phase 7.
3. Incognito window with a different account. ✅ *"You don't have access"*.

Then follow **[docs/post_deployment_tests.md](docs/post_deployment_tests.md)**. It tells you what to **type**, **say** and **show on camera**, and what Maya and the claim panel should do:

| Level | What you test | Tests |
|---|---|---|
| 1 | Getting to know Maya: greeting, scope, honesty | T-01…T-05 |
| 2 | Simple complete claims by voice and typing; packet download | T-10…T-13 |
| 3 | Where the water came from, including mixed causes, mudflow and hurricane | T-20…T-28 |
| 4 | Policy checks: expired, cancelled, wrong name or state, unknown number, outside the 5 states | T-30…T-35 |
| 5 | Camera and photos: honesty, captures, water line, list, injection in an image, upload, sketch | V-01…V-10 |
| 6 | Realistic complex calls: full claim, emergency, big estimate, late report, corrections | C-01…C-07 |
| 7 | Edge cases and attacks: "am I covered?", jailbreaks, bad dates, Spanish, offline, limits | E-01…E-25 |
| 8 | Knowledge and FEMA grounding: basement, proof of loss, flood definition, living expenses | G-01…G-08 |

**Optional plumbing check** from Cloud Shell. It's read-only and checks privacy, regions, data and errors:
```bash
bash scripts/post_deploy_checks.sh
```

---

## Phase 11: Grade the test calls (live evals)

Every Phase 10 call is stored in BigQuery `conversation_traces`. Grade them with the Vertex AI evaluation service:
```bash
cd ~/gemini-live-adk-flood-claims && source deploy/00_variables.sh
TODAY=$(date +%F)
GOOGLE_CLOUD_PROJECT=$PROJECT_ID uv run --no-sync python -m evals.export_live_traces --start-date "$TODAY" --max-intakes 50
GOOGLE_CLOUD_PROJECT=$PROJECT_ID agents-cli eval grade \
  --traces "evals/results/live_traces/live_${TODAY}_${TODAY}.json" \
  --config evals/eval_config_live.yaml --output evals/results/grade_live
# Several days at once: --start-date "$(date -d '7 days ago' +%F)" --end-date "$TODAY" (the file is then live_<start>_<end>.json)
```
- **Metrics:** task success, tool use (including `lookup_flood_guidance`), camera honesty, topic discipline, no coverage promise.
- **Keep a history in BigQuery.** This is optional; add `--create-table` the first time:
  ```bash
  GOOGLE_CLOUD_PROJECT=$PROJECT_ID uv run --no-sync python -m evals.run_vertex_eval \
    --traces "evals/results/live_traces/live_${TODAY}_${TODAY}.json" \
    --config evals/eval_config_live.yaml --layer live --bq --create-table
  ```

---

## Phase 12: Update, roll back, pause

```bash
cd ~/gemini-live-adk-flood-claims && source deploy/00_variables.sh

# Code or skill change: test, build, deploy (creates a new revision)
GOOGLE_CLOUD_PROJECT=x uv run --no-sync pytest -q -p no:warnings
bash deploy/04_build_image.sh          # new tag every run
export IMAGE=...                       # paste the line it prints
bash deploy/05_deploy_cloud_run.sh
# then re-run T-01…T-13 and G-01…G-03 in the app as a quick regression

# Setting change only (e.g. turning grounding on or off in .env): redeploy the same image
export IMAGE=$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" \
  --format='value(spec.template.spec.containers[0].image)')
bash deploy/05_deploy_cloud_run.sh

# Roll back instantly
gcloud run revisions list --service="$SERVICE_NAME" --region="$REGION"
gcloud run services update-traffic "$SERVICE_NAME" --region="$REGION" --to-revisions=<REVISION>=100

# Pause access (an idle service costs ~$0 anyway)
gcloud iap web remove-iam-policy-binding --member="user:$USER_EMAIL" --role=roles/iap.httpsResourceAccessor \
  --region="$REGION" --resource-type=cloud-run --service="$SERVICE_NAME"

# Refresh the data (monthly is plenty). --upload also deletes stale part files in GCS
# (add --keep-stale-parts to keep them). NOTE: a refresh can change the sampled demo
# policy numbers - re-run the test-data queries in post_deployment_tests.md 1.4 afterwards.
uv run --no-sync python -m data_pipeline.fetch_openfema --gzip --upload --force
uv run --no-sync python -m data_pipeline.load_to_bigquery --steps load,reference,transform

# Refresh FEMA documents (when FEMA publishes a new edition)
uv run --no-sync python -m grounding.fetch_fema_docs --upload && bash deploy/03b_vertex_ai_search.sh
```

---

## Phase 13: Destroy everything

> [!CAUTION]
> This is irreversible. Download any packets you want to keep first.

```bash
cd ~/gemini-live-adk-flood-claims && source deploy/00_variables.sh
bash deploy/99_destroy.sh                      # asks you to type the project id to confirm
```

| Option | Effect |
|---|---|
| *(none)* | Deletes the Cloud Run service, the Vertex AI Search engine and data store (`us`), the image repo, the budget and log metric, the BigQuery dataset, the bucket, and the service account with its roles |
| `--keep-data` | Keeps the BigQuery dataset and the bucket, so you can redeploy later without reloading data |
| `--include-firestore` | Also deletes the Firestore database. It's kept by default because a project has one `(default)` database, and intake documents expire through TTL anyway. |

✅ **Verify nothing is left:**
```bash
gcloud run services list --region="$REGION"
gcloud artifacts repositories list --location="$REGION"
bq ls --project_id="$PROJECT_ID"
gcloud storage ls
```

**Simplest alternative**, if the project exists only for this demo: `gcloud projects delete "$PROJECT_ID"`. This stops all billing, and the project can be restored for 30 days.

Last step: delete the code from Cloud Shell with `rm -rf ~/gemini-live-adk-flood-claims`.

---

## Troubleshooting (Cloud Shell specifics)

| Symptom | Fix |
|---|---|
| `uv: command not found` after reconnecting | Run `source ~/.bashrc`. If it's still missing, re-run step 2.1. |
| A long download stopped when the tab closed | Run it inside `tmux`. `fetch_openfema` resumes where it stopped. |
| `No space left on device` | Delete `~/gemini-live-adk-flood-claims/data/raw` after the upload to GCS (step 6.2) |
| `fetch_fema_docs` says `[MISSING]` / 403 | Expected. Download those PDFs in your browser and upload them (step 7.1). |
| 03b: `PERMISSION_DENIED` on import | The service-agent grant can take a minute to apply. Re-run `bash deploy/03b_vertex_ai_search.sh`. |
| 03b: test query has no `results` / "no documents indexed" | Indexing isn't finished. Wait 10 minutes and re-run 03b (safe), or raise `INDEX_TIMEOUT_S`. |
| 03b: "Import finished with failures" | Read the printed `errorSamples`. Usually a non-PDF or empty file in `gs://$BUCKET/grounding/fema/`; remove it and re-run. |
| 05: `IMAGE ... is not set` | Run `bash deploy/04_build_image.sh` and paste the `export IMAGE=...` line it prints, or reuse the running image (Phase 12). |
| App shows 401 *"sign in"* right after deploy | `CLAIMDESK_IAP_AUDIENCE` doesn't match. Re-run 05 (it computes it), and check with `bash scripts/post_deploy_checks.sh` (check L0-06b). |
| G-tests: no "Checking FEMA guidance…" | Check that `/api/health` shows `"guidance_search": true`. If not, set the two `.env` lines from step 7.3 and redeploy (Phase 12). |
| Logs show `Guidance search unavailable` | The service account lacks `roles/discoveryengine.viewer` (re-run 02), or the engine id is wrong in `.env` |
| BigQuery 403 mentioning a *quota project* | Run `gcloud config set project "$PROJECT_ID"`, then retry |
| Model `NotFound` / `PermissionDenied` | Run `uv run --no-sync python scripts/check_models.py` and follow its hints |
| IAP says *"You don't have access"* when you sign in as yourself | Re-run `bash deploy/06_grant_iap_access.sh`, and check which account the browser is signed in with |
| Anything else | [deploy_runbook.md](docs/deploy_runbook.md), [data_loading.md](docs/data_loading.md), [grounding.md](docs/grounding.md) |
