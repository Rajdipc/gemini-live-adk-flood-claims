"""Step 1 of the data load: download FEMA NFIP v3 data from the OpenFEMA API.

WHAT THIS SCRIPT DOES (in plain words)
    For each state (default CO, TX, FL, LA, NC) it asks FEMA's free public
    API for flood insurance **policies** and **claims**, page by page, and
    saves them as *newline-delimited JSON* ("NDJSON": one JSON object per
    line - the format BigQuery loads natively) under ``data/raw/``. With
    ``--upload`` it also copies each file to your Cloud Storage bucket,
    where ``load_to_bigquery.py`` picks them up.

    No Google Cloud resources are created by this script. ``--upload`` only
    writes *objects* into a bucket you created yourself beforehand
    (``docs/data_loading.md`` step 3).

THE API IN 30 SECONDS
    OpenFEMA speaks "OData" - query options go in the URL:
        $filter   which rows      propertyState eq 'TX' and policyEffectiveDate ge '2025-01-01'
        $select   which columns   id,propertyState,reportedZipCode,...
        $orderby  explicit sort   id  (optional: FEMA already returns rows in
                                        id order, and sorting can time out on
                                        the huge policies table)
        $top      page size       max 10,000
        $skip     where to start  0, 10000, 20000, ...
        $inlinecount=allpages     also return the total row count
    Endpoints (v3 - the old v2 ``FimaNfip*`` endpoints are removed 2026-10-15):
        https://www.fema.gov/api/open/v3/NfipPolicies   (~74.7M rows total)
        https://www.fema.gov/api/open/v3/NfipClaims     (~2.73M rows total)

SAMPLING (keeps it cheap)
    Florida alone has ~2M policies that started in 2025+. ``--max-per-state``
    (default 50,000) caps policies per state. Instead of taking the first
    50,000 rows (which would all be from January 2025), we take whole pages
    spread *evenly* across the full result - a "systematic sample" that
    covers the whole date range. Claims are small enough to take all
    (``--max-claims-per-state 0`` = no cap).

RESUMABLE
    Every page is saved to its own file (``part-00003.jsonl.gz``), written to a
    temporary name first and renamed only when complete. If the script is
    interrupted (Wi-Fi drop, Ctrl+C), just run the same command again:
    finished pages are skipped. A ``_manifest.json`` per state remembers the
    page plan, and ``_SUCCESS.json`` marks a state as fully downloaded.
    Changing the plan (dates, caps, page size, ``--gzip``, ...) re-plans that
    state from scratch.

UPLOAD (``--upload``)
    Files whose MD5 already matches the object in the bucket are skipped.
    After a state is uploaded, any OTHER ``part-*`` objects left in that
    state's folder by an earlier run are deleted, because
    ``load_to_bigquery.py`` loads every part file it finds there
    (``--keep-stale-parts`` turns this off).

EXAMPLES
    # See how many rows each state has (tiny requests, downloads nothing)
    uv run python -m data_pipeline.fetch_openfema --counts-only

    # Small smoke test: 2 states, 1,000 policies each, all their claims
    uv run python -m data_pipeline.fetch_openfema --states CO,NC --max-per-state 1000

    # The real thing, gzip-compressed, uploaded to gs://$CLAIMDESK_GCS_BUCKET/raw/openfema/
    uv run python -m data_pipeline.fetch_openfema --gzip --upload

Attribution: This product uses the FEMA OpenFEMA API, but is not endorsed by
FEMA. NFIP data is redacted by FEMA for privacy (no names, no policy
numbers, rounded coordinates).
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlencode

from claimdesk.observability import get_logger, setup_logging
from claimdesk.settings import PROJECT_ROOT, get_settings

log = get_logger("data_pipeline.fetch_openfema")

OPENFEMA_BASE_URL = "https://www.fema.gov/api/open/v3"
MAX_PAGE_SIZE = 10_000  # OpenFEMA's hard limit for $top
# The states we serve come from ONE place: CLAIMDESK_SUPPORTED_STATES in .env
# (default "CO,TX,FL,LA,NC"), read by claimdesk.settings. The same list drives
# the NOAA/ZIP reference copy in load_to_bigquery.py.
DEFAULT_STATES: tuple[str, ...] = tuple(get_settings().supported_states)
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "raw"
DEFAULT_GCS_PREFIX = "raw/openfema"
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
USER_AGENT = "claimdesk-data-pipeline/0.1 (educational demo; uses the FEMA OpenFEMA API)"

_STATE_RE = re.compile(r"^[A-Z]{2}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ODATA_SAFE = "$,'()"


# ---------------------------------------------------------------------------
# What to download
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DatasetSpec:
    """Everything that differs between the policies and claims downloads."""

    key: str  # CLI name: "policies" / "claims"
    entity: str  # OpenFEMA entity name, also the JSON key holding the rows
    slug: str  # folder + BigQuery staging table suffix (raw_<slug>)
    state_field: str  # the column holding the 2-letter state
    date_field: str  # the column we filter with "ge <since>"
    schema_file: str  # data_pipeline/schemas/<file> - also defines $select

    @property
    def fields(self) -> list[str]:
        """Columns to request (``$select``) = the columns of the BigQuery schema.

        Reading them from the schema JSON keeps ONE source of truth: if you
        add a column to the schema, the download automatically includes it.
        """

        schema = json.loads((SCHEMA_DIR / self.schema_file).read_text(encoding="utf-8"))
        return [column["name"] for column in schema]


POLICIES = DatasetSpec(
    key="policies",
    entity="NfipPolicies",
    slug="nfip_policies",
    state_field="propertyState",
    date_field="policyEffectiveDate",
    schema_file="raw_nfip_policies.json",
)
CLAIMS = DatasetSpec(
    key="claims",
    entity="NfipClaims",
    slug="nfip_claims",
    state_field="state",  # NB: claims use "state", policies "propertyState"
    date_field="dateOfLoss",
    schema_file="raw_nfip_claims.json",
)
DATASETS: dict[str, DatasetSpec] = {POLICIES.key: POLICIES, CLAIMS.key: CLAIMS}


@dataclass(frozen=True)
class PagePlan:
    """One page to download: its sequence number, offset and size."""

    index: int
    skip: int
    top: int

    @property
    def filename_stem(self) -> str:
        return f"part-{self.index:05d}"


# ---------------------------------------------------------------------------
# Pure helpers (no network) - these are unit-tested
# ---------------------------------------------------------------------------
def build_filter(spec: DatasetSpec, state: str, since: str) -> str:
    """OData filter, e.g. ``state eq 'TX' and dateOfLoss ge '2015-01-01'``.

    Inputs are validated with strict patterns first, so nothing unexpected
    can ever be pasted into the URL.
    """

    state = state.strip().upper()
    if not _STATE_RE.match(state):
        raise ValueError(f"State must be a 2-letter code, got {state!r}")
    if not _DATE_RE.match(since):
        raise ValueError(f"Date must be YYYY-MM-DD, got {since!r}")
    return f"{spec.state_field} eq '{state}' and {spec.date_field} ge '{since}'"


def build_query_params(
    spec: DatasetSpec,
    state: str,
    since: str,
    *,
    top: int,
    skip: int = 0,
    include_count: bool = False,
    select: Iterable[str] | None = None,
    order_by: str | None = None,
) -> dict[str, str]:
    """The OData query options for one request, as a plain dict.

    ``order_by="id"`` forces an explicit sort. In practice OpenFEMA already
    returns rows in ``id`` order, and an explicit ``$orderby`` combined with a
    wide ``$select`` on the 74.7M-row policies table can make FEMA's server
    time out, so it is OFF by default (``--order-by-id`` turns it on). Either
    way the SQL keeps one row per ``id``, so a rare duplicate caused by FEMA
    refreshing data mid-download is harmless.
    """

    if not 0 < top <= MAX_PAGE_SIZE:
        raise ValueError(f"$top must be between 1 and {MAX_PAGE_SIZE}")
    if skip < 0:
        raise ValueError("$skip must be >= 0")
    params = {
        "$filter": build_filter(spec, state, since),
        "$select": ",".join(select if select is not None else spec.fields),
        "$top": str(top),
        "$skip": str(skip),
        "$format": "json",
    }
    if order_by:
        params["$orderby"] = order_by
    if include_count:
        params["$inlinecount"] = "allpages"
    return params


def encode_params(params: dict[str, str]) -> str:
    """URL-encode OData options. ``quote`` writes spaces as ``%20`` (OpenFEMA
    does not accept ``+``); the OData characters ``$ , ' ( )`` stay readable."""

    return urlencode(params, quote_via=quote, safe=_ODATA_SAFE)


def build_page_url(spec: DatasetSpec, state: str, since: str, *, top: int, skip: int = 0, include_count: bool = False, select: Iterable[str] | None = None) -> str:
    """Full request URL (handy for logging and for pasting into a browser)."""

    params = build_query_params(spec, state, since, top=top, skip=skip, include_count=include_count, select=select)
    return f"{OPENFEMA_BASE_URL}/{spec.entity}?{encode_params(params)}"


def plan_pages(total: int, page_size: int = MAX_PAGE_SIZE, max_records: int = 0) -> list[PagePlan]:
    """Decide which pages to download.

    * ``max_records == 0`` or ``total <= max_records``: every page, in order.
    * otherwise: ``ceil(max_records / page_size)`` pages spread evenly across
      the whole result (systematic sample). Pages never overlap.
    """

    if total <= 0:
        return []
    if not 0 < page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    wanted = total if max_records <= 0 else min(total, max_records)
    n_pages = math.ceil(wanted / page_size)
    if wanted == total or n_pages * page_size >= total:
        # Cheap enough to take everything in order.
        return [PagePlan(i, i * page_size, min(page_size, total - i * page_size)) for i in range(math.ceil(total / page_size))]
    stride = total // n_pages  # >= page_size because n_pages * page_size < total
    pages: list[PagePlan] = []
    remaining = wanted
    for i in range(n_pages):
        top = min(page_size, remaining)
        pages.append(PagePlan(i, i * stride, top))
        remaining -= top
    return pages


def local_state_dir(out_dir: Path, spec: DatasetSpec, state: str) -> Path:
    """``data/raw/openfema/nfip_claims/state=TX`` (Hive-style folder naming)."""

    return out_dir / "openfema" / spec.slug / f"state={state}"


def gcs_object_name(prefix: str, spec: DatasetSpec, state: str, filename: str) -> str:
    """``raw/openfema/nfip_claims/state=TX/part-00000.jsonl.gz``."""

    return f"{prefix.strip('/')}/{spec.slug}/state={state}/{filename}"


def gcs_state_prefix(prefix: str, spec: DatasetSpec, state: str) -> str:
    """``raw/openfema/nfip_claims/state=TX/`` (the "folder" of one state)."""

    return f"{prefix.strip('/')}/{spec.slug}/state={state}/"


def build_manifest_plan(
    *, query_filter: str, fields: list[str], page_size: int, max_records: int, order_by: str | None, compress: bool
) -> dict[str, Any]:
    """The settings that decide WHICH files a download produces.

    If any of them changes between runs, the old part files no longer match
    the request, so the state is planned and downloaded again. ``compress``
    is part of it because ``--gzip`` changes the file names
    (``.jsonl`` vs ``.jsonl.gz``).
    """

    return {
        "filter": query_filter,
        "fields": list(fields),
        "page_size": page_size,
        "max_records": max_records,
        "order_by": order_by,
        "compress": compress,
    }


def manifest_matches(manifest: dict[str, Any] | None, plan: dict[str, Any]) -> bool:
    """True if a saved ``_manifest.json`` was made with exactly this plan.

    Manifests written by older versions have no ``compress`` key, so they
    never match and the state is simply re-planned (safe, just slower).
    """

    if not manifest:
        return False
    return all(key in manifest and manifest[key] == value for key, value in plan.items())


def select_stale_gcs_parts(existing_names: Iterable[str], state_prefix: str, current_filenames: Iterable[str]) -> list[str]:
    """Which ``part-*`` objects under one state's GCS folder are left over.

    Example: an earlier run uploaded ``part-00000..part-00007``, then a
    ``--force`` re-download with a smaller sample produced only
    ``part-00000..part-00004``. ``load_to_bigquery.py`` loads EVERY
    ``part-*`` file in the folder, so ``part-00005..07`` would silently add
    old rows. The same happens when switching ``--gzip`` on or off
    (``part-00000.jsonl`` next to ``part-00000.jsonl.gz``).

    Only objects directly inside ``state_prefix`` whose file name starts with
    ``part-`` are considered; anything else in the bucket is never touched.
    """

    keep = set(current_filenames)
    stale: list[str] = []
    for name in existing_names:
        if not name.startswith(state_prefix):
            continue
        filename = name[len(state_prefix) :]
        if "/" in filename or not filename.startswith("part-"):
            continue
        if filename not in keep:
            stale.append(name)
    return sorted(stale)


def local_md5_base64(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Base64 MD5 of a local file: the same format as ``Blob.md5_hash`` in GCS.

    Comparing checksums (not just sizes) means a re-downloaded page with
    different rows but the same byte count is still uploaded.
    """

    digest = hashlib.md5(usedforsecurity=False)  # integrity check, not security
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def needs_upload(remote_md5_base64: str | None, local_md5: str) -> bool:
    """Upload unless GCS already holds an object with the identical MD5."""

    return not remote_md5_base64 or remote_md5_base64 != local_md5


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def make_session(total_retries: int = 6, backoff_factor: float = 2.0):
    """A ``requests.Session`` that retries flaky requests automatically.

    ``urllib3.Retry`` re-sends a GET when FEMA answers 429 (too many
    requests) or 5xx (server hiccup), waiting 2s, 4s, 8s, ... between tries
    and honouring any ``Retry-After`` header. Connection resets are retried
    too.
    """

    import requests  # imported lazily: only the `data` extra installs it
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=total_retries,
        status=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_json(session, spec: DatasetSpec, params: dict[str, str], *, timeout_s: float, attempts: int = 3) -> dict[str, Any]:
    """GET one page and return the decoded JSON body.

    ``make_session`` already retries HTTP-level errors. This outer loop also
    retries the rarer case where the connection drops *mid-body* (truncated
    JSON), which urllib3 cannot see.
    """

    import requests

    url = f"{OPENFEMA_BASE_URL}/{spec.entity}"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, params=encode_params(params), timeout=timeout_s)
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError, ValueError) as exc:
            last_error = exc
            wait = 2**attempt
            log.warning("OpenFEMA page failed, retrying", extra={"json_fields": {"entity": spec.entity, "attempt": attempt, "wait_s": wait, "error": str(exc)[:200]}})
            time.sleep(wait)
    raise RuntimeError(f"OpenFEMA request failed after {attempts} attempts: {last_error}")


def fetch_total_count(session, spec: DatasetSpec, state: str, since: str, *, timeout_s: float) -> int:
    """Ask for 1 row + ``$inlinecount=allpages`` to learn how many rows match."""

    params = build_query_params(spec, state, since, top=1, include_count=True, select=["id"])
    body = fetch_json(session, spec, params, timeout_s=timeout_s)
    return int(body.get("metadata", {}).get("count", 0))


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def write_ndjson(path: Path, records: Iterable[dict[str, Any]], *, compress: bool) -> int:
    """Write records atomically: to ``<name>.tmp`` first, then rename.

    A half-written file therefore never has the final name, which is what
    makes "skip files that already exist" a safe resume strategy.
    """

    tmp = path.with_name(path.name + ".tmp")
    count = 0
    opener = gzip.open if compress else open
    with opener(tmp, "wt", encoding="utf-8") as handle:  # type: ignore[operator]
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False))
            handle.write("\n")
            count += 1
    os.replace(tmp, path)  # atomic on the same filesystem
    return count


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# One state of one dataset
# ---------------------------------------------------------------------------
def download_state(
    session,
    spec: DatasetSpec,
    state: str,
    *,
    since: str,
    out_dir: Path,
    max_records: int,
    page_size: int,
    compress: bool,
    force: bool,
    timeout_s: float,
    order_by: str | None = None,
) -> list[Path]:
    """Download (or resume) one state. Returns the list of part files."""

    state_dir = local_state_dir(out_dir, spec, state)
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_path, success_path = state_dir / "_manifest.json", state_dir / "_SUCCESS.json"
    suffix = ".jsonl.gz" if compress else ".jsonl"
    query_filter = build_filter(spec, state, since)

    plan = build_manifest_plan(
        query_filter=query_filter, fields=spec.fields, page_size=page_size, max_records=max_records, order_by=order_by, compress=compress
    )
    manifest = None if force else _read_json(manifest_path)
    same_plan = manifest_matches(manifest, plan)
    if same_plan and success_path.exists():
        files = sorted(state_dir.glob(f"part-*{suffix}"))
        log.info("Already downloaded, skipping", extra={"json_fields": {"entity": spec.entity, "state": state, "files": len(files)}})
        return files

    if not same_plan:
        # New (or changed) request: ask FEMA how many rows match and plan pages.
        for stale in list(state_dir.glob("part-*")) + [success_path]:
            stale.unlink(missing_ok=True)
        total = fetch_total_count(session, spec, state, since, timeout_s=timeout_s)
        pages = plan_pages(total, page_size, max_records)
        manifest = {
            "entity": spec.entity,
            "api_version": "v3",
            "state": state,
            **plan,  # filter, fields, page_size, max_records, order_by, compress
            "total_count": total,
            "pages": [page.__dict__ for page in pages],
            "planned_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(manifest_path, manifest)
    assert manifest is not None
    pages = [PagePlan(**page) for page in manifest["pages"]]
    log.info(
        "Downloading state",
        extra={"json_fields": {"entity": spec.entity, "state": state, "total_count": manifest["total_count"], "pages": len(pages), "sampled": len(pages) * page_size < manifest["total_count"]}},
    )

    files: list[Path] = []
    rows_written = 0
    for page in pages:
        path = state_dir / f"{page.filename_stem}{suffix}"
        files.append(path)
        if path.exists():
            continue  # finished in an earlier run
        params = build_query_params(spec, state, since, top=page.top, skip=page.skip, order_by=order_by)
        started = time.monotonic()
        body = fetch_json(session, spec, params, timeout_s=timeout_s)
        records = body.get(spec.entity, [])
        if not records:
            log.warning("Empty page (data may have been refreshed since planning)", extra={"json_fields": {"entity": spec.entity, "state": state, "skip": page.skip}})
        count = write_ndjson(path, records, compress=compress)
        rows_written += count
        log.info(
            "Page saved",
            extra={"json_fields": {"entity": spec.entity, "state": state, "page": page.index + 1, "of": len(pages), "rows": count, "elapsed_ms": int((time.monotonic() - started) * 1000), "file": path.name}},
        )

    _write_json(success_path, {"completed_at": datetime.now(timezone.utc).isoformat(), "files": [p.name for p in files], "rows_written_this_run": rows_written})
    return files


# ---------------------------------------------------------------------------
# Upload to Cloud Storage
# ---------------------------------------------------------------------------
def upload_files(files: list[Path], *, bucket_name: str, prefix: str, spec: DatasetSpec, state: str, delete_stale: bool = True) -> int:
    """Copy part files to ``gs://<bucket>/<prefix>/<slug>/state=XX/``.

    * Files already in the bucket with the same MD5 checksum are skipped
      (resumable, and a changed file is re-uploaded even if its size is equal).
    * Only ``part-*`` files are uploaded - never the manifest/success markers -
      so a ``gs://.../nfip_claims/*`` wildcard in the BigQuery load matches
      data files only.
    * After uploading, OLD ``part-*`` objects in the same state folder that
      are not part of this download are DELETED (``delete_stale``), because
      the loader reads every part file in the folder (see
      ``select_stale_gcs_parts``). Only that one state's ``part-*`` objects
      are ever deleted.

    Authentication: Application Default Credentials (``gcloud auth
    application-default login``). You need write access to the bucket
    (e.g. ``roles/storage.objectAdmin`` on it; project Owner also works).
    """

    from google.cloud import storage  # lazy import: only needed with --upload

    client = storage.Client(project=get_settings().project_id or None)
    bucket = client.bucket(bucket_name)
    uploaded = 0
    for path in files:
        name = gcs_object_name(prefix, spec, state, path.name)
        existing = bucket.get_blob(name)
        if existing is not None and not needs_upload(existing.md5_hash, local_md5_base64(path)):
            continue
        blob = bucket.blob(name)
        # Store .gz files as plain gzip objects (no Content-Encoding header):
        # BigQuery detects and decompresses them itself during the load.
        content_type = "application/gzip" if path.suffix == ".gz" else "application/x-ndjson"
        blob.upload_from_filename(str(path), content_type=content_type)
        uploaded += 1
        log.info("Uploaded", extra={"json_fields": {"gcs_uri": f"gs://{bucket_name}/{name}", "bytes": path.stat().st_size}})

    if delete_stale:
        state_prefix = gcs_state_prefix(prefix, spec, state)
        existing_names = [blob.name for blob in client.list_blobs(bucket_name, prefix=state_prefix)]
        for name in select_stale_gcs_parts(existing_names, state_prefix, [p.name for p in files]):
            bucket.blob(name).delete()
            log.info("Deleted stale part file", extra={"json_fields": {"gcs_uri": f"gs://{bucket_name}/{name}"}})
            print(f"  deleted stale gs://{bucket_name}/{name} (not part of the current download)")
    return uploaded


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m data_pipeline.fetch_openfema",
        description="Download FEMA NFIP v3 policies/claims from OpenFEMA to NDJSON (optionally upload to GCS).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dataset", choices=["policies", "claims", "all"], default="all", help="What to download.")
    parser.add_argument("--states", default=",".join(get_settings().supported_states), help="Comma-separated 2-letter state codes (default: $CLAIMDESK_SUPPORTED_STATES).")
    parser.add_argument("--policies-since", default="2025-01-01", help="Keep policies with policyEffectiveDate >= this date.")
    parser.add_argument("--claims-since", default="2015-01-01", help="Keep claims with dateOfLoss >= this date.")
    parser.add_argument("--max-per-state", type=int, default=50_000, help="Max policies per state (0 = no cap). Sampled evenly across the result.")
    parser.add_argument("--max-claims-per-state", type=int, default=0, help="Max claims per state (0 = all).")
    parser.add_argument("--page-size", type=int, default=MAX_PAGE_SIZE, help=f"Rows per request (1..{MAX_PAGE_SIZE}).")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Local folder for the NDJSON files.")
    parser.add_argument("--gzip", action="store_true", help="Write .jsonl.gz (about 10x smaller).")
    parser.add_argument("--force", action="store_true", help="Ignore previous progress and download again.")
    parser.add_argument("--timeout", type=float, default=180.0, help="Seconds to wait for one page.")
    parser.add_argument("--order-by-id", action="store_true", help="Add $orderby=id to every page. FEMA already returns id order; explicit sorting of the policies table with many columns can take minutes or time out.")
    parser.add_argument("--counts-only", action="store_true", help="Only print how many rows match per state; download nothing.")
    parser.add_argument("--upload", action="store_true", help="Also upload files to gs://BUCKET/<gcs-prefix>/... Old part-* objects of the same state that are not in the current download are deleted, so the loader never mixes runs.")
    parser.add_argument("--keep-stale-parts", action="store_true", help="With --upload: do NOT delete old part-* objects from the state folders in GCS.")
    parser.add_argument("--bucket", default=os.getenv("CLAIMDESK_GCS_BUCKET", ""), help="GCS bucket name (default: $CLAIMDESK_GCS_BUCKET).")
    parser.add_argument("--gcs-prefix", default=DEFAULT_GCS_PREFIX, help="Object prefix inside the bucket.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    states = [s.strip().upper() for s in args.states.split(",") if s.strip()]
    specs = list(DATASETS.values()) if args.dataset == "all" else [DATASETS[args.dataset]]
    bucket = args.bucket.removeprefix("gs://").strip("/")
    if args.upload and not bucket:
        print("ERROR: --upload needs a bucket: pass --bucket or set CLAIMDESK_GCS_BUCKET.", file=sys.stderr)
        return 2

    session = make_session()
    print("This product uses the FEMA OpenFEMA API, but is not endorsed by FEMA.")
    for spec in specs:
        since = args.policies_since if spec is POLICIES else args.claims_since
        cap = args.max_per_state if spec is POLICIES else args.max_claims_per_state
        for state in states:
            if args.counts_only:
                total = fetch_total_count(session, spec, state, since, timeout_s=args.timeout)
                print(f"{spec.entity:13s} {state}  {spec.date_field} >= {since}: {total:>10,d} rows" + (f"  (will sample {min(total, cap):,d})" if cap and total > cap else ""))
                continue
            files = download_state(
                session,
                spec,
                state,
                since=since,
                out_dir=args.out_dir,
                max_records=cap,
                page_size=args.page_size,
                compress=args.gzip,
                force=args.force,
                timeout_s=args.timeout,
                order_by="id" if args.order_by_id else None,
            )
            if args.upload:
                upload_files(files, bucket_name=bucket, prefix=args.gcs_prefix, spec=spec, state=state, delete_stale=not args.keep_stale_parts)
    if not args.counts_only:
        print(f"Done. Files are under {args.out_dir / 'openfema'}" + (f" and gs://{bucket}/{args.gcs_prefix}/" if args.upload else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
