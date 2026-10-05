"""Grade traces with the Vertex AI Gen AI evaluation service and store results in BigQuery / GCS.

WHY THIS EXISTS (when agents-cli already has `eval grade`)
    ``agents-cli eval grade`` prints a table and writes JSON/HTML files on
    YOUR laptop. That is perfect while iterating. For a team, you also want
    every run in ONE queryable place, so you can chart quality over time,
    compare Cloud Run revisions, and alert on regressions. This script runs
    the same grading (same config files, same metrics, same SDK call
    ``client.evals.evaluate``) and then:

    * writes the full result JSON to ``evals/results/vertex/`` (and optionally
      ``gs://<bucket>/<prefix>/``), and
    * appends one row per (case, metric) to BigQuery
      ``{project}.{dataset}.eval_results``.

WHICH PYTHON RUNS IT
    It needs ``google-cloud-aiplatform[evaluation]>=2.0`` (the ``vertexai``
    package) and ``pyyaml``. Both are in the project's ``dev`` extra::

           uv sync --extra dev --extra data
           uv run --no-sync python -m evals.run_vertex_eval ...

    The interpreter that ships with agents-cli has them too
    (``~/.local/share/uv/tools/google-agents-cli/bin/python -m evals.run_vertex_eval ...``,
    run from the project root; only ``claimdesk.settings`` and
    ``claimdesk.observability`` are imported, and both are stdlib-only).

WHERE THE MODELS ARE
    The evaluation service calls its default judge model for LLM-judged
    metrics - no judge model is configured here. Endpoint location comes from
    ``settings.model_location`` ("global"), overridable with ``--location``.

EXAMPLES
    ::

        # pipeline traces (from evals/generate_traces.py)
        GOOGLE_CLOUD_PROJECT=my-proj ~/.local/share/uv/tools/google-agents-cli/bin/python -m evals.run_vertex_eval \\
            --traces evals/results/traces/traces_20250716_101500.json --config evals/eval_config.yaml \\
            --layer pipeline --bq --gcs-prefix gs://my-bucket/evals

        # live traces (from evals/export_live_traces.py)
        ... --traces evals/results/live_traces/live_2025-07-01_2025-07-07.json \\
            --config evals/eval_config_live.yaml --layer live --bq

    Add ``--create-table`` the first time (creates ``eval_results``,
    partitioned by day). BigQuery *load jobs* are free; you pay only for
    storage (a few MB per thousand runs) and the judge-model tokens.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claimdesk.observability import get_logger  # noqa: E402  (stdlib-only module)
from claimdesk.settings import get_settings  # noqa: E402  (stdlib-only module)

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVALS_DIR = PROJECT_ROOT / "evals"
DEFAULT_OUT_DIR = EVALS_DIR / "results" / "vertex"
RESULTS_TABLE = "eval_results"

# BigQuery schema of eval_results: one row per (run, eval case, metric).
RESULTS_SCHEMA: list[tuple[str, str, str]] = [
    ("run_id", "STRING", "REQUIRED"),
    ("created_at", "TIMESTAMP", "REQUIRED"),
    ("layer", "STRING", "NULLABLE"),  # pipeline | live
    ("traces_file", "STRING", "NULLABLE"),
    ("config_file", "STRING", "NULLABLE"),
    ("git_sha", "STRING", "NULLABLE"),
    ("reasoning_model", "STRING", "NULLABLE"),  # the AGENT's model (for context; not the judge)
    ("eval_case_id", "STRING", "NULLABLE"),
    ("intake_id", "STRING", "NULLABLE"),  # live cases only
    ("service_revision", "STRING", "NULLABLE"),  # live cases only
    ("tags", "STRING", "REPEATED"),
    ("metric_name", "STRING", "REQUIRED"),
    ("score", "FLOAT64", "NULLABLE"),
    ("explanation", "STRING", "NULLABLE"),
    ("error_message", "STRING", "NULLABLE"),
]


# ---------------------------------------------------------------------------
# 1. Config -> metric specs (pure; unit-tested without vertexai)
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class MetricSpec:
    """One metric from an eval config.

    kind: ``builtin`` (a Vertex AI metric name), ``code`` (custom_function
    source), or ``llm`` (prompt_template judge; ``payload`` holds its fields).
    """

    name: str
    kind: str
    payload: Any = None


def load_metric_specs(config_path: Path, only: list[str] | None = None) -> list[MetricSpec]:
    """Read an agents-cli eval config (``metrics_to_run`` + ``custom_metrics``)."""

    import yaml  # available in both the project .venv and the agents-cli venv

    data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    custom = {m["name"]: m for m in data.get("custom_metrics") or [] if isinstance(m, dict) and m.get("name")}
    specs: list[MetricSpec] = []
    for name in only or data.get("metrics_to_run") or []:
        entry = custom.get(name)
        if entry is None:
            specs.append(MetricSpec(name, "builtin"))
        elif "custom_function" in entry:
            if entry.get("execution", "local") != "local":
                raise ValueError(f"{name}: only local custom_function metrics are supported here")
            specs.append(MetricSpec(name, "code", entry["custom_function"]))
        elif "prompt_template" in entry:
            specs.append(MetricSpec(name, "llm", {k: v for k, v in entry.items()}))
        else:
            raise ValueError(f"{name}: custom metric needs custom_function or prompt_template")
    if not specs:
        raise ValueError(f"{config_path}: no metrics to run")
    return specs


def compile_code_metric(source: str, name: str) -> Any:
    """Exec a custom_function shim and return its ``evaluate`` callable."""

    os.environ.setdefault("CLAIMDESK_EVALS_DIR", str(EVALS_DIR))  # shims find evals/custom_metrics.py
    namespace: dict[str, Any] = {}
    exec(compile(source, f"<custom_metric:{name}>", "exec"), namespace)  # noqa: S102 - our own config file
    fn = namespace.get("evaluate")
    if not callable(fn):
        raise ValueError(f"{name}: custom_function must define evaluate(instance)")
    return fn


def to_vertex_metrics(specs: list[MetricSpec], vertex_types: Any) -> list[Any]:
    """Build the objects ``client.evals.evaluate(metrics=...)`` expects (same as agents-cli)."""

    metrics: list[Any] = []
    for spec in specs:
        if spec.kind == "builtin":
            metrics.append(spec.name)  # e.g. "hallucination", "multi_turn_task_success"
        elif spec.kind == "code":
            metrics.append(vertex_types.Metric(name=spec.name, custom_function=compile_code_metric(spec.payload, spec.name)))
        else:
            metrics.append(vertex_types.LLMMetric.model_validate(spec.payload))
    return metrics


# ---------------------------------------------------------------------------
# 2. Result -> BigQuery rows (pure; unit-tested)
# ---------------------------------------------------------------------------
def flatten_results(
    result: dict[str, Any],
    cases: list[dict[str, Any]],
    *,
    run_id: str,
    created_at: str,
    layer: str,
    traces_file: str = "",
    config_file: str = "",
    git_sha: str | None = None,
    reasoning_model: str | None = None,
) -> list[dict[str, Any]]:
    """Turn an ``EvaluationResult`` (as a dict) into ``eval_results`` rows.

    The SDK reports results by ``eval_case_index`` (position in the dataset),
    so we map that back to the case to get its id / tags / intake.
    """

    rows: list[dict[str, Any]] = []
    for case_result in result.get("eval_case_results") or []:
        index = case_result.get("eval_case_index")
        case = cases[index] if isinstance(index, int) and 0 <= index < len(cases) else {}
        revisions = case.get("service_revisions") or []
        for candidate in case_result.get("response_candidate_results") or []:
            for name, metric in (candidate.get("metric_results") or {}).items():
                score = metric.get("score")
                rows.append({
                    "run_id": run_id,
                    "created_at": created_at,
                    "layer": layer,
                    "traces_file": traces_file,
                    "config_file": config_file,
                    "git_sha": git_sha,
                    "reasoning_model": reasoning_model,
                    "eval_case_id": case.get("eval_case_id"),
                    "intake_id": case.get("intake_id"),
                    "service_revision": ",".join(revisions) or None,
                    "tags": [str(t) for t in case.get("tags") or []],
                    "metric_name": metric.get("metric_name") or name,
                    "score": float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
                    "explanation": metric.get("explanation"),
                    "error_message": metric.get("error_message"),
                })
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Mean score and error count per metric (what you'd chart over time)."""

    summary: dict[str, dict[str, float]] = {}
    for row in rows:
        s = summary.setdefault(row["metric_name"], {"cases": 0, "errors": 0, "sum": 0.0, "scored": 0})
        s["cases"] += 1
        if row["score"] is None:
            s["errors"] += 1
        else:
            s["sum"] += row["score"]
            s["scored"] += 1
    return {name: {"cases": s["cases"], "errors": s["errors"], "mean_score": round(s["sum"] / s["scored"], 4) if s["scored"] else None} for name, s in summary.items()}


# ---------------------------------------------------------------------------
# 3. Side effects: evaluate, BigQuery, GCS
# ---------------------------------------------------------------------------
def _git_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5, check=True).stdout.strip() or None
    except Exception:  # noqa: BLE001 - not a git checkout, git missing...
        return None


def write_bigquery(rows: list[dict[str, Any]], project: str, dataset: str, *, location: str, create: bool) -> None:
    """Append rows with a (free) load job; optionally create the table."""

    from google.cloud import bigquery

    client = bigquery.Client(project=project, location=location)
    table_id = f"{project}.{dataset}.{RESULTS_TABLE}"
    schema = [bigquery.SchemaField(n, t, mode=m) for n, t, m in RESULTS_SCHEMA]
    if create:
        table = bigquery.Table(table_id, schema=schema)
        table.time_partitioning = bigquery.TimePartitioning(field="created_at")  # scan only the days you query
        table.clustering_fields = ["layer", "metric_name"]
        client.create_table(table, exists_ok=True)
    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
        labels={"app": "claimdesk", "feature": "eval-results"},
    )
    client.load_table_from_json(rows, table_id, job_config=job_config).result(timeout=120)
    log.info("Wrote eval results to BigQuery", extra={"json_fields": {"table": table_id, "rows": len(rows)}})


def upload_gcs(local: Path, gcs_prefix: str, project: str) -> str:
    from google.cloud import storage

    if not gcs_prefix.startswith("gs://"):
        raise ValueError("--gcs-prefix must look like gs://bucket/path")
    bucket_name, _, prefix = gcs_prefix[5:].partition("/")
    blob_name = f"{prefix.rstrip('/')}/{local.name}" if prefix else local.name
    storage.Client(project=project).bucket(bucket_name).blob(blob_name).upload_from_filename(str(local), content_type="application/json")
    return f"gs://{bucket_name}/{blob_name}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade traces with the Vertex AI Gen AI evaluation service; store results in BigQuery/GCS.")
    parser.add_argument("--traces", type=Path, required=True, help="trace file (EvaluationDataset JSON)")
    parser.add_argument("--config", type=Path, required=True, help="evals/eval_config.yaml or evals/eval_config_live.yaml")
    parser.add_argument("--metrics", default="", help="comma-separated subset of metrics")
    parser.add_argument("--layer", choices=["pipeline", "live"], default="pipeline")
    parser.add_argument("--location", help="eval service location (default: settings.model_location)")
    parser.add_argument("--bq", action="store_true", help="append rows to {project}.{dataset}.eval_results")
    parser.add_argument("--create-table", action="store_true", help="create eval_results if missing (first run)")
    parser.add_argument("--gcs-prefix", help="also upload the result JSON, e.g. gs://my-bucket/evals")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)

    try:
        import vertexai
        from vertexai import types as vertex_types
    except ImportError:
        parser.error(
            "the 'vertexai' package (google-cloud-aiplatform[evaluation]>=2.0) is not installed in this Python.\n"
            "Install the dev extra (uv sync --extra dev --extra data) and use: uv run --no-sync python -m evals.run_vertex_eval ...\n"
            "or run with ~/.local/share/uv/tools/google-agents-cli/bin/python -m evals.run_vertex_eval ..."
        )

    settings = get_settings()
    specs = load_metric_specs(args.config, [m for m in args.metrics.split(",") if m] or None)
    metrics = to_vertex_metrics(specs, vertex_types)
    data = json.loads(args.traces.read_text(encoding="utf-8"))
    cases = data["eval_cases"]
    dataset = vertex_types.EvaluationDataset.model_validate({"eval_cases": cases})

    needs_gcp = any(s.kind != "code" for s in specs) or args.bq or args.gcs_prefix
    if needs_gcp and not settings.project_id:
        parser.error("GOOGLE_CLOUD_PROJECT is required for built-in / LLM-judge metrics, --bq and --gcs-prefix")
    client = vertexai.Client(project=settings.project_id, location=args.location or settings.model_location) if needs_gcp else vertexai.Client(project=None, location=None)
    log.info("Running Vertex AI evaluation", extra={"json_fields": {"cases": len(cases), "metrics": [s.name for s in specs]}})
    result = client.evals.evaluate(dataset=dataset, metrics=metrics)
    result_dict = result.model_dump(mode="json", exclude_none=True)

    run_id = f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    local = args.out_dir / f"vertex_{args.layer}_{run_id}.json"
    local.write_text(json.dumps(result_dict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    rows = flatten_results(
        result_dict, cases,
        run_id=run_id, created_at=datetime.now(timezone.utc).isoformat(), layer=args.layer,
        traces_file=str(args.traces), config_file=str(args.config), git_sha=_git_sha(), reasoning_model=settings.reasoning_model,
    )
    for name, s in summarize(rows).items():
        print(f"{name:32s} mean={s['mean_score']}  cases={s['cases']}  errors={s['errors']}")
    print(f"Saved {local}")
    if args.gcs_prefix:
        print(f"Uploaded {upload_gcs(local, args.gcs_prefix, settings.project_id)}")
    if args.bq:
        write_bigquery(rows, settings.project_id, settings.bq_dataset, location=settings.bq_location, create=args.create_table)
        print(f"Appended {len(rows)} rows to {settings.project_id}.{settings.bq_dataset}.{RESULTS_TABLE} (run_id={run_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
