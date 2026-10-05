# Cost analysis

> [!IMPORTANT]
> Prices below are **list prices checked in September 2026, in USD**, for planning only. Always confirm them on the official pages before you rely on them: [Vertex AI generative AI pricing](https://cloud.google.com/vertex-ai/generative-ai/pricing), [Cloud Run pricing](https://cloud.google.com/run/pricing), [BigQuery pricing](https://cloud.google.com/bigquery/pricing), and the [Google Cloud Pricing Calculator](https://cloud.google.com/products/calculator).
> The Gemini 3.8 series is on **introductory pricing until 2026-12-31**. From 2027-01-01, list prices are scheduled to **double**. The tables below show both.

## 1. TL;DR

| Scenario | Gemini (models) | Everything else on GCP | Total / month |
|---|---|---|---|
| **You alone**: ~20 test calls of 10 min each per month | ≈ \$14 (≈ \$28 from 2027) | ≈ \$0–1, mostly within free tiers | **≈ \$15** (≈ \$29) |
| **1,000 users**: 2 calls each per month = 2,000 calls | ≈ \$1,400 (≈ \$2,800) | ≈ \$150–250 | **≈ \$1.6k** (≈ \$3.0k) |
| **One-time data load** (FEMA + BigQuery) | – | ≈ \$0, within free tiers | **≈ \$0** |
| **One eval run** (16 pipeline cases plus a few live traces) | ≈ \$0.30 (agent) + ≈ \$0.10 (judge) | ≈ \$0 | **< \$1** |

**The models are about 90% of the cost.** Cloud Run, BigQuery, Firestore, Storage, Logging, and Trace are nearly free at single-user scale. With scale-to-zero, an idle month costs roughly **\$0.15**, which is just Artifact Registry image storage.

## 2. Where the money goes in one call

Assumptions for one **10-minute voice call**:
- The claimant talks about 5 min and the agent about 4 min.
- The camera is on for 3 min at 1 frame per second.
- The claimant takes 15 turns, so the ADK workflow runs about 15 times.
- 3 evidence photos are verified.
- 1 damage sketch is drawn.

### 2.1 Gemini (Vertex AI)

| Component | Model | Volume per call | Unit price (intro → 2027) | Cost per call |
|---|---|---|---|---|
| Claimant audio in | `gemini-3.8-live` | ~5 min ≈ 7.5k tokens (~25 tokens/s) | \$3.00 → \$6.00 per 1M audio-in | \$0.02 |
| Agent audio out | `gemini-3.8-live` | ~4 min ≈ 6k tokens | \$12.00 → \$24.00 per 1M audio-out | \$0.07 |
| Camera frames | `gemini-3.8-live` | 180 frames × ~258 tokens ≈ 46k tokens | \$3.00 → \$6.00 per 1M | \$0.14 |
| Live context re-processing | `gemini-3.8-live` | Each turn re-reads the session context. Assume about 2× the input above. | same | \$0.16 |
| Intake workflow | `gemini-3.8-flash` | 15 runs × 2 LLM nodes × (~3k in + ~0.8k out incl. thinking) | \$0.75 / \$3.75 → \$1.50 / \$7.50 per 1M | \$0.16 |
| Photo verification | `gemini-3.8-flash` | 3 × (~1.5k in + 0.3k out) | same | \$0.01 |
| Damage sketch | `gemini-3.1-flash-image` | 1 image | ~\$0.04–0.15 per image (resolution-dependent) | \$0.10 |
| **Total** | | | | **≈ \$0.66** (range \$0.40–1.20) → **≈ \$1.30 in 2027** |

The two big levers are **camera frames** and **how often the workflow re-runs**. See §5.

### 2.2 Google Cloud infrastructure: single user

| Service | What we use | Price basis | Monthly |
|---|---|---|---|
| **Cloud Run** | 1 vCPU / 2 GiB, instance-based billing (`--no-cpu-throttling`), `min=0`, `max=1`. 20 calls × (10 min + ~15 min idle before scale-down) ≈ 8.3 h. | \$0.000018 per vCPU-s, \$0.000002 per GiB-s (Tier 1). Free: 240k vCPU-s and 450k GiB-s per month. | **\$0**, within free tier |
| **BigQuery storage** | Raw FEMA extracts plus derived tables, all in `us-central1`: < 2 GB. The NOAA/ZIP copies are ≈17k and ≈5k rows. | \$0.02 per GB-month (active logical). First 10 GiB free. | **\$0** |
| **BigQuery queries** | Policy lookups on a clustered table (KB each). Benchmarks are cached. The NOAA check reads only our month-partitioned `noaa_flood_events` copy (KBs) and is cached in memory. The one-time `EXPORT DATA` from the `US` public datasets scans ≈105 MB. | \$6.25 per TiB on-demand. First 1 TiB/month free. Every query is capped at 1 GiB via `maximum_bytes_billed`. | **\$0** |
| **BigQuery streaming inserts** | `conversation_traces` and `intake_packets` rows: a few MB | \$0.01 per 200 MB | **< \$0.01** |
| **Firestore** | ~1 document per intake, a few hundred writes per call | Free: 50k reads, 20k writes, and 1 GiB per day | **\$0** |
| **Cloud Storage** | Photos, sketches, and ZIPs. 30-day lifecycle. < 1 GB. | \$0.020 per GB-month (us-central1 Standard) | **< \$0.05** |
| **Artifact Registry** | ~400 MB image × 5 kept by the cleanup policy | 0.5 GB free, then \$0.10 per GB-month | **≈ \$0.15** |
| **Cloud Build** | ~3 min per build | 2,500 build-min per month free (e2-standard-2) | **\$0** |
| **Cloud Logging** | Structured JSON logs, well under 1 GiB | 50 GiB per project per month free | **\$0** |
| **Cloud Trace** | OTel spans | First 2.5M spans per month free | **\$0** |
| **Error Reporting** | Grouped exceptions | Free (you pay only for the underlying logs) | **\$0** |
| **Vertex AI Search** (optional grounding, `us`) | Enterprise edition search. About 2 guidance look-ups per call × 20 calls = 40 queries/month, plus a few PDFs indexed (≈ 50 MB). | ≈ \$4 per 1,000 queries (Enterprise). Index storage within the free allowance (check the [pricing page](https://cloud.google.com/generative-ai-app-builder/pricing)). | **≈ \$0.20** |
| **IAP** | Protects the Cloud Run service | No charge for IAP on Cloud Run | **\$0** |
| **Networking** | Audio/video to your browser: ~20 MB per call | Internet egress about \$0.12 per GB after the free 1 GB | **\$0** |
| **Total infra** | | | **≈ \$0.40 / month** (≈ \$0.20 without grounding) |

### 2.3 One-time data load (`docs/data_loading.md`)

| Step | Cost |
|---|---|
| Download from the OpenFEMA API | Free (public API). Only your own network. |
| Upload NDJSON to GCS (~1–2 GB) | Ingress is free. Storage is ~\$0.04/month. Delete `raw/` afterwards if you like. |
| `bq load` from GCS | Load jobs are **free** (shared slot pool). |
| Transform SQL (`10`–`40`) | Scans about 1–3 GB, inside the 1 TiB free tier. |
| NOAA and geo public datasets | You pay only for bytes your queries scan (free tier). No storage cost. |

### 2.4 Evals (`docs/evals.md`)

- **Pipeline evals.** The workflow runs once per case: 16 cases × ~\$0.02 ≈ \$0.30. The judge model scores each case: ≈ \$0.10.
- **Live trace evals** reuse conversations already stored in BigQuery, so you pay only for the judge: about \$0.01 per conversation.
- Nightly evals every day would cost **< \$15/month**. Running them on demand while learning is cheaper.

## 3. Scaling to 1,000 users: cost view

Assume 1,000 users, 2 calls each per month, with a peak of about **50 simultaneous calls**. The config changes are in [scaling_to_1000_users.md](scaling_to_1000_users.md).

| Item | Calculation | Monthly (intro prices) |
|---|---|---|
| Gemini | 2,000 calls × \$0.66 | **≈ \$1,320** |
| Cloud Run: always-on baseline | `min-instances=1`, 2 vCPU / 4 GiB, 730 h. Idle min-instances are billed at a lower rate. | ≈ \$60–115 |
| Cloud Run: call traffic | 2,000 calls × 25 min ÷ ~10 calls per instance ≈ 85 instance-hours × ~\$0.16/h | ≈ \$15 |
| Firestore | ~0.5M writes and ~1M reads | ≈ \$2 |
| BigQuery | Still mostly free tier. Traces are about 1 GB. | ≈ \$1–5 |
| Cloud Storage | ~20 GB with the 30-day lifecycle | ≈ \$0.50 |
| Logging | May pass 50 GiB if DEBUG logging is left on. Keep INFO. | \$0–25 |
| Egress | 2,000 × 20 MB = 40 GB | ≈ \$5 |
| Vertex AI Search (grounding) | 2,000 calls × ~2 queries = 4,000 queries × \$4 per 1,000 | ≈ \$16 |
| **Total** | | **≈ \$1.4k–1.5k** (≈ \$2.8k from 2027) |

## 4. Guardrails already built in

| Guardrail | Where |
|---|---|
| `--max-instances=1` hard-caps Cloud Run spend. | `deploy/05_deploy_cloud_run.sh` |
| `--min-instances=0`: \$0 compute when idle. | same |
| Every BigQuery query has `maximum_bytes_billed = 1 GiB`. A runaway query is refused, not billed. | `claimdesk/data_access/bq_client.py` |
| BigQuery query cache on, benchmark cache, NOAA result cache. | `bq_client.py`, `loss_benchmarks.py`, `weather_events.py` |
| Live session capped at 20 min. Idle intakes expire after 30 min. Max photos per intake. | `webapp/` limits |
| GCS lifecycle deletes `intakes/` after 30 days. Traces partition-expire after 30 days. Firestore TTL. | `deploy/03_*.sh`, `data_pipeline/sql/00_*.sql` |
| Artifact Registry keeps the 5 newest images, plus any image younger than 30 days. | `deploy/04_build_image.sh` |
| Budget email alerts at 50/90/100% of \$25. | `deploy/07_budget_and_alerts.sh` |
| Job labels `app=claimdesk,feature=...` on every BigQuery job, and `app=claimdesk` on Cloud Run, so cost is attributable in the billing export. | `bq_client.py`, `05_deploy_cloud_run.sh` |

## 5. Cost levers if you need them (the models stay the same)

1. **Camera frame rate.** 1 fps → 0.5 fps halves the biggest Live cost line. Or send frames only while the claimant is showing something.
2. **Workflow re-runs.** Run only when the extracted facts actually change (the web app already caches per fact revision). A 1–2 s debounce could go further.
3. **Live context window compression.** Enable `context_window_compression` in the Live config for long calls so old turns aren't re-billed in full.
4. **Sketch on demand only.** Draw the sketch when the claimant or adjuster asks, not automatically.
5. **Batch evals.** Run nightly evals through batch prediction, where supported, at about 50% of the online price.
6. **Billing export to BigQuery.** Turn it on in Console → Billing → Billing export. Then query cost by `labels.app = 'claimdesk'`.

## 6. How to see real cost after you deploy

- **Console → Billing → Reports.** Filter by project, group by *Service* or by label `app`.
- **Console → Vertex AI → Dashboard.** Shows request and token counts per model.
- **BigQuery → Job history.** Shows bytes processed per job (label `feature`).
- A ready-made query, once billing export is enabled:

```sql
SELECT service.description AS service, SUM(cost) AS usd
FROM `YOUR_BILLING_PROJECT.billing_export.gcp_billing_export_v1_*`
WHERE project.id = 'YOUR_PROJECT' AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY service ORDER BY usd DESC;
```
