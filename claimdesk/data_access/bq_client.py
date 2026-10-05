"""One shared BigQuery client + a safe query helper.

BEGINNER NOTES
--------------
* **Client reuse.** Creating a ``bigquery.Client`` sets up auth and HTTP
  connection pools, so we create ONE per process (``lru_cache``) and reuse it.
* **Parameterized queries.** We never paste user input into SQL strings.
  ``@policy_key`` style parameters are sent separately from the SQL text,
  which makes SQL injection impossible (a claimant could *say* anything!).
* **Location.** Our dataset lives in ``us-central1``, the same region as
  Cloud Run, Cloud Storage and Firestore, so every query stays in-region.
  BigQuery can only join tables stored in the same location; the public
  NOAA and ZIP-code datasets live in the ``US`` multi-region, so the data
  pipeline copies the small subset we need into our dataset
  (``noaa_flood_events``, ``zip_points``). At runtime we never touch
  ``bigquery-public-data``.
* **Retries.** The BigQuery client already retries transient errors
  (HTTP 5xx, rate limits) with exponential backoff. We add an overall
  ``timeout`` so a slow query can never hang a live phone call.
* **Cost guard.** ``maximum_bytes_billed`` makes BigQuery *refuse* to run a
  query that would scan more than the limit, so a bug can't run up a bill.
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any, Iterable

from google.api_core import exceptions as gexc
from google.cloud import bigquery

from ..errors import ConfigurationError, DataAccessError
from ..observability import get_logger
from ..settings import get_settings

log = get_logger(__name__)

# 1 GiB is far more than any query in this app needs (they scan a few MB).
DEFAULT_MAX_BYTES_BILLED = 1 * 1024**3


@lru_cache(maxsize=1)
def get_bq_client() -> bigquery.Client:
    settings = get_settings()
    if not settings.project_id:
        raise ConfigurationError("GOOGLE_CLOUD_PROJECT is not set")
    return bigquery.Client(project=settings.project_id, location=settings.bq_location)


def _param(name: str, value: Any) -> bigquery.ScalarQueryParameter:
    """Map a Python value to a typed BigQuery parameter."""

    if isinstance(value, bool):
        kind = "BOOL"
    elif isinstance(value, int):
        kind = "INT64"
    elif isinstance(value, float):
        kind = "FLOAT64"
    elif hasattr(value, "isoformat") and not isinstance(value, str):
        kind = "DATE"
    else:
        kind = "STRING"
    return bigquery.ScalarQueryParameter(name, kind, value)


def run_query(
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    label: str,
    array_params: dict[str, list[str]] | None = None,
    timeout_s: float = 8.0,
    max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED,
) -> list[dict[str, Any]]:
    """Run a parameterized query and return rows as plain dicts.

    ``label`` is attached to the BigQuery job (visible in the console under
    Job history and in billing exports) so you can see which feature spent
    what. ``array_params`` holds ARRAY<STRING> parameters (used with
    ``IN UNNEST(@name)``). Raises :class:`DataAccessError` on any failure.

    WHY WRAP *EVERYTHING*?
        Callers only catch our own error types. Before, a missing project id
        (``ConfigurationError``), missing credentials
        (``google.auth.exceptions.DefaultCredentialsError``), an expired token
        (``RefreshError``) or a network drop (``TransportError`` /
        ``requests.ConnectionError``) escaped as foreign exceptions and failed
        the whole workflow node, so no packet was produced at all. Now every
        failure becomes ``DataAccessError`` (original kept as ``__cause__``)
        and the weather check, benchmarks and policy review degrade instead.
    """

    try:
        client = get_bq_client()
    except ConfigurationError as exc:
        # e.g. GOOGLE_CLOUD_PROJECT unset during an offline run. Not retryable:
        # trying again will not make the setting appear.
        raise DataAccessError(f"BigQuery is not configured: {exc}", retryable=False) from exc
    except Exception as exc:  # DefaultCredentialsError, TransportError, bad location...
        raise DataAccessError(f"BigQuery client could not be created: {type(exc).__name__}: {exc}", retryable=False) from exc

    started = time.monotonic()
    try:
        query_parameters: list[Any] = [_param(k, v) for k, v in (params or {}).items()]
        query_parameters += [bigquery.ArrayQueryParameter(k, "STRING", v) for k, v in (array_params or {}).items()]
        job_config = bigquery.QueryJobConfig(
            query_parameters=query_parameters,
            maximum_bytes_billed=max_bytes_billed,
            labels={"app": "claimdesk", "feature": label.replace("_", "-")[:63]},
            use_query_cache=True,  # identical queries within 24h are free
        )
        job = client.query(sql, job_config=job_config)
        rows: Iterable[bigquery.Row] = job.result(timeout=timeout_s)
        result = [dict(row.items()) for row in rows]
    except (gexc.GoogleAPICallError, gexc.RetryError, TimeoutError) as exc:
        # Do not log here with .exception(): the caller decides whether this
        # is fatal (policy lookup) or degradable (weather check) and logs once.
        raise DataAccessError(f"BigQuery query '{label}' failed: {exc}") from exc
    except Exception as exc:  # RefreshError, TransportError, requests errors, anything unexpected
        raise DataAccessError(f"BigQuery query '{label}' failed: {type(exc).__name__}: {exc}") from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "BigQuery query finished",
        extra={
            "json_fields": {
                "bq_label": label,
                "rows": len(result),
                "elapsed_ms": elapsed_ms,
                "bytes_processed": getattr(job, "total_bytes_processed", None),
                "cache_hit": getattr(job, "cache_hit", None),
            }
        },
    )
    return result


def table(name: str) -> str:
    """Return a back-ticked fully-qualified table name for our dataset."""

    return f"`{get_settings().bq_prefix}.{name}`"


__all__ = ["get_bq_client", "run_query", "table"]
