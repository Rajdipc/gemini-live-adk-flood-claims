# Scaling from 1 user to ~1,000 users

Today ClaimDesk is set up for **one person: you**. This page explains why each setting was chosen and what to change to serve about 1,000 users (roughly 50 simultaneous calls at peak).

## 1. Current single-user configuration

| Setting | Value now | Why |
|---|---|---|
| Cloud Run `--max-instances` | `1` | Hard cost cap. All live sessions sit in one container, so no cross-instance coordination is needed. |
| Cloud Run `--min-instances` | `0` | Pay nothing when idle. The first request after a quiet period waits for a cold start of about 3–6 s. |
| `--concurrency` | `20` | One user opens a WebSocket plus a few REST polls. |
| CPU / memory | 1 vCPU / 2 GiB | Audio relay, JSON, and small images. |
| App `MAX_SESSIONS` | 32 | Protects the single container. |
| IAP access | `user:you` only | Private demo. |
| Firestore / GCS / BigQuery | Serverless, no tuning needed | These scale automatically. |

## 2. What changes for about 1,000 users

### 2.1 Cloud Run

```bash
# min-instances=1  -> no cold start for the first caller of the day
# max-instances=10 -> ~10 live calls per instance x 10 = ~100 concurrent calls headroom
# concurrency=40   -> each call = 1 WebSocket + a few REST requests
# cpu/memory       -> more audio streams per container
gcloud run services update claimdesk --region=us-central1 \
  --min-instances=1 \
  --max-instances=10 \
  --concurrency=40 \
  --cpu=2 --memory=4Gi
```

- **Why this works.** Intake state already lives in **Firestore** and files in **GCS**, not in container memory. Any instance can serve REST calls for any intake.
- **What stays per-instance.** The open **Gemini Live WebSocket** for a call is pinned to one container. `--session-affinity` (already set) keeps a browser on the same container. The app refuses cleanly (`LimitExceededError`) when a container is full, and the browser retries.
- **Tune with data.** Load-test (§3). Then set `MAX_SESSIONS` per container to what 2 vCPU can handle (typically 10–20 audio relays). Set `max-instances` to peak ÷ that number, plus about 30% headroom.

### 2.2 Gemini quotas: the real bottleneck

Check **Console → IAM & Admin → Quotas**, filtered by *Vertex AI API*.

| Quota | Why it matters | Action |
|---|---|---|
| Live API concurrent sessions (per project; `global`, plus `us-central1` if the fallback is used) | 50 simultaneous calls need 50 live sessions | Request increases in advance |
| `gemini-3.8-flash` requests per minute | About 50 calls × (2 LLM nodes per turn + photo checks) ≈ 200–400 RPM at peak | The `global` endpoint already spreads the load. Request more if you see 429s. |
| `gemini-3.1-flash-image` RPM | Sketches | Keep sketches on-demand |
| Tokens per minute | Same | Consider **Provisioned Throughput** for predictable latency and cost at this scale |

The code already handles 429s: ADK retries, `RetryConfig` covers the BigQuery nodes, and model failures degrade gracefully while the call continues.

### 2.3 Data layer

| Component | Change |
|---|---|
| Policy lookups (BigQuery) | BigQuery is built for analytics. Point lookups take ~0.5–1 s and count against concurrent-query limits. At scale, copy `policy_registry` into **Firestore** (a key-value lookup in a few ms) with a nightly sync job, or add an in-memory LRU cache. |
| `loss_benchmarks` | Already cached in memory per container. |
| NOAA check | Already cached in memory, plus the BigQuery 24h cache. |
| `conversation_traces` streaming | Fine up to thousands of rows per second. For higher volume, switch to the **BigQuery Storage Write API**. |
| Firestore | Keep documents under 1 MiB (the transcript is trimmed). Keep the TTL policy on. |

### 2.4 Session and ADK

- `InMemorySessionService` holds only the short-lived per-run workflow session, which is deleted after each run. That's fine at scale.
- To keep ADK sessions long-term (for auditing or replay), switch to `VertexAiSessionService` (Agent Platform Sessions) or a database session service.

### 2.5 Access, security, operations

| Area | Change |
|---|---|
| IAP | Grant a **Google Group** (`group:claims-team@...`) instead of individual users. Add or remove people in the group, not in IAM. |
| Model Armor | Deferred for now, by your choice. Revisit before real users: prompt-injection and PII screening. |
| Rate limiting | Per-user limits on intake creation, e.g. a Firestore counter, or Cloud Armor with an external load balancer. |
| Monitoring | Alert on 5xx rate, p95 latency, live-session count, Gemini 429s, and error-log metric `claimdesk_errors`. |
| SLOs | Define them in Cloud Monitoring, e.g. 99% of calls connect in under 3 s. |
| Budget | Raise the budget (see [cost_analysis.md](cost_analysis.md) §3) and add a Pub/Sub budget notification if you want automatic actions. |
| CI/CD | Move from manual scripts to Cloud Build triggers or `agents-cli infra cicd` with staging and prod projects. |
| Evals | Run the pipeline eval suite on every build and block deploys on regression (see [evals.md](evals.md)). |
| Second region | Resilience through a second Cloud Run region behind a global load balancer. **Conflicts with the us-central1-only policy**, so only consider it if that policy changes. |
| Pub/Sub | Add it when a downstream claims system needs to consume packets asynchronously. |

## 3. How to load-test before raising limits

1. Deploy to a **staging** service with the new limits.
2. Browser-based voice calls are hard to fake. Test the pieces:
   - **REST plus workflow.** Use [Locust](https://locust.io) or `hey` against `/api/...` with an IAP-authorized identity token. Point it at a copy with `CLAIMDESK_STORAGE_BACKEND=gcp`.
   - **Live API headroom.** Write a small script that opens N `client.aio.live.connect()` sessions and streams a WAV file. Watch for quota errors.
3. Watch Cloud Run → Metrics (instance count, CPU, request latency) and Vertex AI → quotas.
4. Increase `max-instances` and `MAX_SESSIONS` until p95 latency stays within target.

## 4. Summary checklist

- [ ] `min-instances=1`, `max-instances≈10`, `cpu=2`, `memory=4Gi`, `concurrency≈40`
- [ ] Live API concurrent-session and model RPM/TPM quota increases approved
- [ ] Policy registry cached in Firestore or memory
- [ ] IAP granted to a Google Group
- [ ] Model Armor decision revisited
- [ ] Alerts, SLOs, and a bigger budget
- [ ] CI/CD with evals as a release gate
- [ ] Load test passed
