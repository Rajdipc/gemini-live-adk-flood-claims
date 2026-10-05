"""Step 2 of the data load: raw files in Cloud Storage -> BigQuery tables.

WHAT THIS SCRIPT DOES
    1. ``create``    runs ``sql/00_create_dataset_and_tables.sql``: creates the
                     ``claimdesk`` dataset in **us-central1** (our region for
                     everything) plus the two tables the app writes to
                     (packets, traces).
    2. ``load``      loads the NDJSON files that ``fetch_openfema.py --upload``
                     put in ``gs://$CLAIMDESK_GCS_BUCKET/raw/openfema/`` into two
                     *staging* tables, ``raw_nfip_policies`` and
                     ``raw_nfip_claims``, using the explicit schemas in
                     ``data_pipeline/schemas/`` (``WRITE_TRUNCATE`` = replace
                     the table contents every run, so re-running is safe).
    3. ``reference`` copies the public NOAA storm events and ZIP code points
                     into our dataset (``noaa_flood_events``, ``zip_points``):
                       a. EXPORT DATA jobs run in location ``US`` (where the
                          public data lives) and write Parquet files to
                          ``gs://$CLAIMDESK_GCS_BUCKET/reference/...``;
                       b. load jobs (in us-central1) read those files into
                          staging tables;
                       c. ``sql/reference/03_build_reference_tables.sql``
                          builds the final tables and drops the staging ones.
    4. ``transform`` runs ``sql/10_*.sql``, ``20_*``, ``30_*``, ``40_*`` in order
                     to build ``policy_registry``, ``loss_benchmarks``,
                     ``nfip_claims_clean`` and ``eval_seed_claims``.

    ``--dry-run`` changes NOTHING: it prints every SQL statement and load job,
    and asks BigQuery to *validate* the SQL and estimate bytes scanned
    (a dry-run query is free). Validation of later steps can fail on a
    fresh project simply because the tables don't exist yet - that's expected.

PLACEHOLDERS
    SQL files contain ``{project}``, ``{dataset}`` and ``{location}``. They are
    replaced with values from ``claimdesk.settings`` (env vars
    ``GOOGLE_CLOUD_PROJECT``, ``CLAIMDESK_BQ_DATASET``, ``CLAIMDESK_BQ_LOCATION``).
    The reference export files also use ``{bucket}``, ``{run_id}``,
    ``{states}``, ``{event_types}`` and ``{storms_tables}``.
    We use plain string replacement (not ``str.format``) because SQL regexes
    such as ``\\\\d{5}`` also contain curly braces.

WHY THE REFERENCE COPY? (the short version - docs/data_loading.md has more)
    BigQuery can only JOIN tables stored in the same location. The public
    NOAA / ZIP tables are in the ``US`` multi-region, our dataset is in
    ``us-central1``. So we copy the small filtered subset we need (~17k
    events + ~5k ZIPs) once, and refresh it when we want newer NOAA data.
    A job in ``US`` writing to a ``us-central1`` bucket, and a us-central1
    bucket loading into a us-central1 dataset, are both "colocated" pairs in
    BigQuery's location rules -> no data transfer charges.

PERMISSIONS YOU NEED (your own user, via ``gcloud auth application-default login``)
    BigQuery Job User + BigQuery Data Editor on the project (Owner also works),
    and Storage Object Admin on the bucket (the export WRITES files; the
    loads only read).

EXAMPLES
    uv run python -m data_pipeline.load_to_bigquery --dry-run       # look first
    uv run python -m data_pipeline.load_to_bigquery                 # all 4 steps
    uv run python -m data_pipeline.load_to_bigquery --steps reference   # refresh NOAA/ZIP copy
    uv run python -m data_pipeline.load_to_bigquery --steps transform --only 20_loss_benchmarks
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from claimdesk.observability import get_logger, setup_logging
from claimdesk.settings import get_settings

log = get_logger("data_pipeline.load_to_bigquery")

PIPELINE_DIR = Path(__file__).resolve().parent
SQL_DIR = PIPELINE_DIR / "sql"
REFERENCE_SQL_DIR = SQL_DIR / "reference"
SCHEMA_DIR = PIPELINE_DIR / "schemas"
CREATE_SQL = "00_create_dataset_and_tables.sql"
BUILD_REFERENCE_SQL = "03_build_reference_tables.sql"
DEFAULT_GCS_PREFIX = "raw/openfema"
REFERENCE_GCS_PREFIX = "reference"
STEPS = ("create", "load", "reference", "transform")

# The public datasets live in the BigQuery "US" multi-region, so the EXPORT
# DATA jobs that read them must RUN there (they only write files to our
# us-central1 bucket; nothing is stored in US).
PUBLIC_DATA_LOCATION = "US"
NOAA_DATASET = "bigquery-public-data.noaa_historic_severe_storms"
NOAA_FIRST_YEAR = 2015
# Only these source columns are read from each yearly NOAA table.
NOAA_COLUMNS = (
    "event_id",
    "event_type",
    "state_fips_code",
    "cz_type",
    "cz_fips_code",
    "cz_name",
    "event_begin_time",
    "event_point",
    "event_latitude",
    "event_longitude",
    "damage_property",
    "flood_cause",
)

# Anything that still looks like {placeholder} after rendering is a mistake.
_LEFTOVER_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
_SAFE_VALUE = re.compile(r"[A-Za-z0-9_\-.:]+")
_JOB_LABELS = {"app": "claimdesk", "feature": "data-pipeline"}


@dataclass(frozen=True)
class StagingTable:
    """A raw table loaded 1:1 from the OpenFEMA NDJSON files."""

    table: str  # BigQuery table name
    slug: str  # folder name used by fetch_openfema.py
    schema_file: str  # data_pipeline/schemas/<file>


STAGING_TABLES = (
    StagingTable("raw_nfip_policies", "nfip_policies", "raw_nfip_policies.json"),
    StagingTable("raw_nfip_claims", "nfip_claims", "raw_nfip_claims.json"),
)


@dataclass(frozen=True)
class ReferenceTable:
    """One public table we copy into our dataset (see the ``reference`` step)."""

    name: str  # final table name AND the GCS folder name, e.g. "zip_points"
    export_sql: str  # sql/reference/<file> (runs in location US)
    staging_table: str  # table the Parquet files are loaded into first
    schema_file: str  # data_pipeline/schemas/<file> (explicit load schema)


REFERENCE_TABLES = (
    ReferenceTable("noaa_flood_events", "01_export_noaa_flood_events.sql", "stg_noaa_flood_events", "stg_noaa_flood_events.json"),
    ReferenceTable("zip_points", "02_export_zip_points.sql", "stg_zip_points", "stg_zip_points.json"),
)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested, no Google Cloud needed)
# ---------------------------------------------------------------------------
def render_sql(
    text: str,
    *,
    project: str,
    dataset: str,
    location: str = "us-central1",
    extra: dict[str, str] | None = None,
    sql_fragments: dict[str, str] | None = None,
) -> str:
    """Replace ``{project}``, ``{dataset}``, ``{location}`` (and friends) in a SQL file.

    * ``extra`` - more simple values such as ``{"bucket": "...", "run_id": "..."}``;
      they get the same safety check as project/dataset.
    * ``sql_fragments`` - ready-made SQL pieces (array literals, the NOAA
      UNION ALL). Only pass values built by the helpers in this module
      (``sql_string_array``, ``storms_union_sql``), never raw user input.

    Raises ``ValueError`` for empty values, for values containing characters
    that could break out of a backtick-quoted name, or for leftover
    ``{placeholders}`` (typos such as ``{projcet}``).
    """

    values = {"project": project, "dataset": dataset, "location": location, **(extra or {})}
    for name, value in values.items():
        if not value:
            raise ValueError(f"SQL placeholder {{{name}}} needs a value (is GOOGLE_CLOUD_PROJECT set?)")
        if not _SAFE_VALUE.fullmatch(value):
            raise ValueError(f"Unsafe value for {{{name}}}: {value!r}")
    rendered = text
    for name, value in {**values, **(sql_fragments or {})}.items():
        rendered = rendered.replace("{" + name + "}", value)
    leftover = _LEFTOVER_PLACEHOLDER.findall(rendered)
    if leftover:
        raise ValueError(f"Unknown SQL placeholders: {sorted(set(leftover))}")
    return rendered


_ARRAY_ITEM = re.compile(r"[A-Za-z0-9 ()/\-]+")


def sql_string_array(values: Iterable[str]) -> str:
    """``["CO", "TX"]`` -> the SQL literal ``['CO', 'TX']``.

    Every item must be plain text (letters, digits, space, ``()/-``), so a
    quote can never sneak in and change the meaning of the SQL.
    """

    items = list(values)
    if not items:
        raise ValueError("Need at least one value for a SQL array")
    for item in items:
        if not _ARRAY_ITEM.fullmatch(item):
            raise ValueError(f"Unsafe SQL array item: {item!r}")
    return "[" + ", ".join(f"'{item}'" for item in items) + "]"


def state_codes(states: Iterable[str]) -> list[str]:
    """Validate and upper-case 2-letter state codes (e.g. from settings)."""

    codes = [s.strip().upper() for s in states if s and s.strip()]
    bad = [s for s in codes if not re.fullmatch(r"[A-Z]{2}", s)]
    if bad or not codes:
        raise ValueError(f"Supported states must be 2-letter codes, got {codes!r}")
    return codes


def storms_union_sql(years: Iterable[int]) -> str:
    """Build ``(SELECT <cols> FROM storms_2015 UNION ALL ... )`` for EXPORT DATA.

    Why not ``storms_*``? EXPORT DATA refuses wildcard ("meta") tables, so we
    name each yearly table. We also select the same explicit column list
    from every year so small schema differences between years can't break
    the UNION.
    """

    years = sorted({int(y) for y in years})
    if not years:
        raise ValueError("Need at least one NOAA year")
    cols = ", ".join(NOAA_COLUMNS)
    selects = [f"    SELECT {cols}\n    FROM `{NOAA_DATASET}.storms_{year:04d}`" for year in years]
    return "(\n" + "\n    UNION ALL\n".join(selects) + "\n  )"


def noaa_years(first_year: int, available_tables: Iterable[str] | None, *, this_year: int) -> list[int]:
    """Which yearly NOAA tables to read.

    ``available_tables`` is the list of table names in the public dataset
    (``storms_2015``, ..., ``tornado_paths`` ...). When we could not list
    them (``--print-only``), assume every year up to ``this_year`` exists.
    """

    if available_tables is None:
        return list(range(first_year, this_year + 1))
    found = sorted(int(m.group(1)) for name in available_tables if (m := re.fullmatch(r"storms_(\d{4})", name)))
    return [y for y in found if y >= first_year]


def new_run_id(now: datetime | None = None) -> str:
    """A folder name for one export run, e.g. ``20260924T101500Z``.

    Each refresh writes to a NEW folder and loads only that folder, so files
    left over from an older run can never be mixed in.
    """

    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def reference_uri(bucket: str, name: str, run_id: str) -> str:
    """``gs://bucket/reference/<name>/run=<run_id>/*.parquet`` (load-job wildcard)."""

    bucket = bucket.removeprefix("gs://").strip("/")
    if not bucket:
        raise ValueError("A GCS bucket is required (set CLAIMDESK_GCS_BUCKET or pass --bucket)")
    return f"gs://{bucket}/{REFERENCE_GCS_PREFIX}/{name}/run={run_id}/*.parquet"


def render_reference_export(
    ref: ReferenceTable,
    *,
    project: str,
    dataset: str,
    location: str,
    bucket: str,
    run_id: str,
    states: Iterable[str],
    years: Iterable[int],
) -> str:
    """Render one ``sql/reference/0N_export_*.sql`` file with all placeholders."""

    from claimdesk.data_access.weather_events import FLOOD_EVENT_TYPES

    text = (REFERENCE_SQL_DIR / ref.export_sql).read_text(encoding="utf-8")
    return render_sql(
        text,
        project=project,
        dataset=dataset,
        location=location,
        extra={"bucket": bucket.removeprefix("gs://").strip("/"), "run_id": run_id},
        sql_fragments={
            "states": sql_string_array(state_codes(states)),
            "event_types": sql_string_array(FLOOD_EVENT_TYPES),
            "storms_tables": storms_union_sql(years),
        },
    )


def transform_sql_files(sql_dir: Path = SQL_DIR, only: list[str] | None = None) -> list[Path]:
    """The transform files, in run order (10_, 20_, 30_, 40_ ...).

    ``only`` filters by file stem, e.g. ``["20_loss_benchmarks"]``.
    """

    files = sorted(p for p in sql_dir.glob("*.sql") if p.name != CREATE_SQL and re.match(r"^\d{2}_", p.name))
    if only:
        wanted = {name.removesuffix(".sql") for name in only}
        files = [p for p in files if p.stem in wanted]
        missing = wanted - {p.stem for p in files}
        if missing:
            raise ValueError(f"No such SQL file(s): {sorted(missing)}")
    return files


def load_schema_json(schema_file: str) -> list[dict[str, Any]]:
    return json.loads((SCHEMA_DIR / schema_file).read_text(encoding="utf-8"))


def source_uri(bucket: str, prefix: str, slug: str) -> str:
    """``gs://bucket/raw/openfema/nfip_claims/*`` - the folder, as a wildcard.

    Shown in messages and used by the ``bq load`` CLI path. The Python loader
    passes the exact list of ``part-*`` files instead (see ``list_part_uris``).
    """

    bucket = bucket.removeprefix("gs://").strip("/")
    if not bucket:
        raise ValueError("A GCS bucket is required (set CLAIMDESK_GCS_BUCKET or pass --bucket)")
    return f"gs://{bucket}/{prefix.strip('/')}/{slug}/*"


_PART_FILE = re.compile(r"/state=[A-Z]{2}/part-\d{5}\.jsonl(\.gz)?$")


def filter_part_object_names(names: list[str]) -> list[str]:
    """Keep only data files (``.../state=TX/part-00000.jsonl[.gz]``).

    If you copied the folder with ``gcloud storage cp -r``, the bucket also
    holds ``_manifest.json`` / ``_SUCCESS.json`` bookkeeping files; loading
    those as NDJSON rows would fail, so we skip them.
    """

    return sorted(name for name in names if _PART_FILE.search(name))


def list_part_uris(bucket: str, prefix: str, slug: str) -> list[str]:
    """List ``gs://`` URIs of all part files for one dataset (read-only call)."""

    from google.cloud import storage

    bucket = bucket.removeprefix("gs://").strip("/")
    client = storage.Client(project=get_settings().project_id or None)
    names = [blob.name for blob in client.list_blobs(bucket, prefix=f"{prefix.strip('/')}/{slug}/")]
    return [f"gs://{bucket}/{name}" for name in filter_part_object_names(names)]


# ---------------------------------------------------------------------------
# BigQuery helpers
# ---------------------------------------------------------------------------
def to_schema_fields(schema_json: list[dict[str, Any]]):
    """JSON schema (same format as ``bq load --schema``) -> ``SchemaField`` list."""

    from google.cloud import bigquery

    return [
        bigquery.SchemaField(col["name"], col["type"], mode=col.get("mode", "NULLABLE"), description=col.get("description"))
        for col in schema_json
    ]


def build_load_job_config(schema_json: list[dict[str, Any]]):
    """How BigQuery should read the NDJSON files.

    * ``NEWLINE_DELIMITED_JSON`` - one JSON object per line (gzip is detected
      automatically from the file contents).
    * explicit ``schema`` - no guessing ("autodetect") so types never drift.
    * ``WRITE_TRUNCATE`` - replace the table's contents on every run.
    * ``ignore_unknown_values`` - extra fields FEMA may add later are skipped
      instead of failing the whole load.
    """

    from google.cloud import bigquery

    return bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=to_schema_fields(schema_json),
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
        ignore_unknown_values=True,
        max_bad_records=0,
        labels=_JOB_LABELS,
    )


def build_parquet_load_job_config(schema_json: list[dict[str, Any]]):
    """How BigQuery should read the reference Parquet files (``reference`` step).

    Parquet files carry their own column types, but we still pass the
    explicit schema: BigQuery exports DATETIME columns to Parquet as a
    "timestamp without time zone", and without a schema the load would turn
    ``event_begin_time`` into a TIMESTAMP (wrong: NOAA times are LOCAL time).
    """

    from google.cloud import bigquery

    return bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        schema=to_schema_fields(schema_json),
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
        labels=_JOB_LABELS,
    )


def make_client():
    from google.cloud import bigquery

    settings = get_settings()
    if not settings.project_id:
        raise SystemExit("GOOGLE_CLOUD_PROJECT is not set. Put it in .env or export it (see docs/data_loading.md).")
    return bigquery.Client(project=settings.project_id, location=settings.bq_location)


def run_sql(client, name: str, sql: str, *, dry_run: bool, max_bytes_billed: int, location: str | None = None) -> None:
    """Run (or dry-run) one SQL file. Multi-statement scripts are fine.

    ``location`` overrides where the job runs. Normally ``None`` = the
    client's default (our dataset's location, us-central1). The reference
    EXPORT DATA jobs pass ``"US"`` because they read US public tables.
    """

    from google.cloud import bigquery

    config = bigquery.QueryJobConfig(
        dry_run=dry_run,
        use_query_cache=False,
        labels=_JOB_LABELS,
    )
    # Only set the cap on real runs. Passing maximum_bytes_billed=None makes
    # the client send the literal string "None", which the API rejects.
    if not dry_run:
        config.maximum_bytes_billed = max_bytes_billed
    job = client.query(sql, job_config=config, location=location or client.location)
    if dry_run:
        log.info("Dry run OK", extra={"json_fields": {"sql_file": name, "bytes_processed_estimate": job.total_bytes_processed}})
        print(f"  [dry-run OK] {name}: would process ~{(job.total_bytes_processed or 0) / 1e6:,.1f} MB")
        return
    job.result()  # wait; raises on SQL errors
    billed = job.total_bytes_billed or 0
    log.info("SQL finished", extra={"json_fields": {"sql_file": name, "job_id": job.job_id, "bytes_billed": billed, "location": job.location}})
    print(f"  [done] {name}: billed {billed / 1e6:,.1f} MB (job {job.job_id}, location {job.location})")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def step_create(client, *, project: str, dataset: str, location: str, dry_run: bool, max_bytes_billed: int) -> None:
    print(f"\n== Step 1/4: create dataset {project}.{dataset} (location {location}) and app tables ==")
    sql = render_sql((SQL_DIR / CREATE_SQL).read_text(encoding="utf-8"), project=project, dataset=dataset, location=location)
    if dry_run:
        print(sql)
    _try_sql(client, CREATE_SQL, sql, dry_run=dry_run, max_bytes_billed=max_bytes_billed)


def step_load(client, *, project: str, dataset: str, bucket: str, prefix: str, dry_run: bool) -> None:
    print("\n== Step 2/4: load raw NDJSON from Cloud Storage into staging tables ==")
    for staging in STAGING_TABLES:
        folder = source_uri(bucket, prefix, staging.slug)
        table_id = f"{project}.{dataset}.{staging.table}"
        schema_json = load_schema_json(staging.schema_file)
        # Listing objects is read-only; skip it only in --print-only mode.
        uris: list[str] = []
        if client is not None:
            try:
                uris = list_part_uris(bucket, prefix, staging.slug)
            except Exception as exc:  # noqa: BLE001 - reported below
                if not dry_run:
                    raise
                print(f"  [dry-run] could not list {folder}: {str(exc).splitlines()[0][:200]}")
        if dry_run:
            found = f"{len(uris)} part file(s)" if client is not None else "files not listed (--print-only)"
            print(f"  [dry-run] would load {folder} ({found}) -> {table_id} (NEWLINE_DELIMITED_JSON, WRITE_TRUNCATE, {len(schema_json)} columns from schemas/{staging.schema_file})")
            continue
        if not uris:
            raise SystemExit(f"No part-*.jsonl[.gz] files under {folder}. Run fetch_openfema.py --upload first (docs/data_loading.md step 5).")
        job = client.load_table_from_uri(uris, table_id, job_config=build_load_job_config(schema_json))
        job.result()  # wait; raises with row-level details if a record is bad
        log.info("Load finished", extra={"json_fields": {"table": table_id, "rows": job.output_rows, "files": len(uris), "source": folder}})
        print(f"  [done] {table_id}: {job.output_rows:,d} rows from {len(uris)} file(s) in {folder}")


def list_noaa_tables(client) -> list[str] | None:
    """Names of the tables in the public NOAA dataset (a read-only API call).

    Returns ``None`` when there is no client (``--print-only``).
    """

    if client is None:
        return None
    return [t.table_id for t in client.list_tables(NOAA_DATASET)]


def step_reference(
    client,
    *,
    project: str,
    dataset: str,
    location: str,
    bucket: str,
    states: Iterable[str],
    first_year: int,
    run_id: str,
    dry_run: bool,
    max_bytes_billed: int,
) -> None:
    """Copy NOAA flood events + ZIP points from the US public datasets into our dataset.

    a. EXPORT DATA (job location US) -> Parquet files in our bucket
    b. load the files (job location = our dataset's location) -> staging tables
    c. build the final tables with sql/reference/03_build_reference_tables.sql
    """

    print(f"\n== Step 3/4: copy NOAA + ZIP reference data into {project}.{dataset} (run {run_id}) ==")
    if not bucket.removeprefix("gs://").strip("/"):
        raise SystemExit("The reference step needs a bucket: set CLAIMDESK_GCS_BUCKET or pass --bucket.")
    this_year = datetime.now(timezone.utc).year
    try:
        available = list_noaa_tables(client)
    except Exception as exc:  # noqa: BLE001 - fall back to the year range
        print(f"  [warning] could not list {NOAA_DATASET}: {str(exc).splitlines()[0][:200]}; assuming years {first_year}-{this_year}")
        available = None
    years = noaa_years(first_year, available, this_year=this_year)
    print(f"  NOAA years: {years[0]}-{years[-1]} ({len(years)} yearly tables); states: {', '.join(state_codes(states))}")

    # a. EXPORT DATA - runs in US, writes to gs://<bucket>/reference/<name>/run=<id>/
    for ref in REFERENCE_TABLES:
        sql = render_reference_export(
            ref, project=project, dataset=dataset, location=location, bucket=bucket, run_id=run_id, states=states, years=years
        )
        if dry_run:
            print(f"\n----- reference/{ref.export_sql} (job location {PUBLIC_DATA_LOCATION}) -----\n{sql}")
        _try_sql(client, ref.export_sql, sql, dry_run=dry_run, max_bytes_billed=max_bytes_billed, location=PUBLIC_DATA_LOCATION)

    # b. load the Parquet files into staging tables in OUR location
    for ref in REFERENCE_TABLES:
        uri = reference_uri(bucket, ref.name, run_id)
        table_id = f"{project}.{dataset}.{ref.staging_table}"
        schema_json = load_schema_json(ref.schema_file)
        if dry_run:
            print(f"  [dry-run] would load {uri} -> {table_id} (PARQUET, WRITE_TRUNCATE, {len(schema_json)} columns from schemas/{ref.schema_file}, location {location})")
            continue
        job = client.load_table_from_uri(uri, table_id, job_config=build_parquet_load_job_config(schema_json), location=location)
        job.result()
        log.info("Reference load finished", extra={"json_fields": {"table": table_id, "rows": job.output_rows, "source": uri}})
        print(f"  [done] {table_id}: {job.output_rows:,d} rows from {uri}")

    # c. build noaa_flood_events + zip_points (and drop the staging tables)
    sql = render_sql((REFERENCE_SQL_DIR / BUILD_REFERENCE_SQL).read_text(encoding="utf-8"), project=project, dataset=dataset, location=location)
    if dry_run:
        print(f"\n----- reference/{BUILD_REFERENCE_SQL} (job location {location}) -----\n{sql}")
    _try_sql(client, BUILD_REFERENCE_SQL, sql, dry_run=dry_run, max_bytes_billed=max_bytes_billed)


def step_transform(client, *, project: str, dataset: str, location: str, only: list[str] | None, dry_run: bool, max_bytes_billed: int) -> None:
    print("\n== Step 4/4: build app tables with SQL ==")
    for path in transform_sql_files(only=only):
        sql = render_sql(path.read_text(encoding="utf-8"), project=project, dataset=dataset, location=location)
        if dry_run:
            print(f"\n----- {path.name} -----\n{sql}")
        _try_sql(client, path.name, sql, dry_run=dry_run, max_bytes_billed=max_bytes_billed)


def _try_sql(client, name: str, sql: str, *, dry_run: bool, max_bytes_billed: int, location: str | None = None) -> None:
    """In dry-run mode a validation error is reported, not fatal."""

    if client is None:
        print(f"  [print-only] {name}: not validated (no BigQuery client)")
        return
    if not dry_run:
        run_sql(client, name, sql, dry_run=False, max_bytes_billed=max_bytes_billed, location=location)
        return
    try:
        run_sql(client, name, sql, dry_run=True, max_bytes_billed=max_bytes_billed, location=location)
    except Exception as exc:  # noqa: BLE001 - dry-run is best effort
        print(f"  [dry-run could not validate] {name}: {str(exc).splitlines()[0][:300]}")
        print("    (Normal on a fresh project: the tables this file reads are created by earlier steps.)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m data_pipeline.load_to_bigquery",
        description="Create the ClaimDesk BigQuery dataset, load raw OpenFEMA files from GCS, copy NOAA/ZIP reference data, and run the transform SQL.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--steps", default=",".join(STEPS), help=f"Comma-separated subset of: {', '.join(STEPS)}.")
    parser.add_argument("--only", default="", help="Transform step only: comma-separated SQL file stems, e.g. 10_policy_registry.")
    parser.add_argument("--bucket", default=settings.gcs_bucket, help="GCS bucket (us-central1) holding raw/openfema/ and reference/ (default: $CLAIMDESK_GCS_BUCKET).")
    parser.add_argument("--gcs-prefix", default=DEFAULT_GCS_PREFIX, help="Object prefix used by fetch_openfema.py --upload.")
    parser.add_argument("--noaa-from-year", type=int, default=NOAA_FIRST_YEAR, help="Reference step: first NOAA year to copy.")
    parser.add_argument("--run-id", default="", help="Reference step: export folder name (default: current UTC time, e.g. 20260924T101500Z).")
    parser.add_argument("--dry-run", action="store_true", help="Print SQL and load jobs; ask BigQuery to validate only. Changes nothing.")
    parser.add_argument("--print-only", action="store_true", help="With --dry-run: do not contact BigQuery at all.")
    parser.add_argument("--max-gb", type=float, default=20.0, help="Safety cap per SQL job (create, reference export/build, transform): BigQuery refuses any single query that would bill more than this many GiB. Not applied to --dry-run or to load jobs (loads are free).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    settings = get_settings()
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    unknown = set(steps) - set(STEPS)
    if unknown:
        print(f"ERROR: unknown step(s) {sorted(unknown)}; choose from {STEPS}", file=sys.stderr)
        return 2
    if not settings.project_id:
        print("ERROR: GOOGLE_CLOUD_PROJECT is not set (see docs/data_loading.md step 1).", file=sys.stderr)
        return 2
    if settings.bq_location.lower() != settings.region.lower():
        print(
            f"WARNING: CLAIMDESK_BQ_LOCATION={settings.bq_location!r} differs from CLAIMDESK_REGION={settings.region!r}. "
            "The project keeps ALL resources in one region; the dataset, the bucket and Cloud Run should match.",
            file=sys.stderr,
        )

    client = None if (args.dry_run and args.print_only) else make_client()
    common = {"project": settings.project_id, "dataset": settings.bq_dataset}
    max_bytes = int(args.max_gb * 1024**3)
    only = [s.strip() for s in args.only.split(",") if s.strip()] or None

    print("This product uses the FEMA OpenFEMA API, but is not endorsed by FEMA.")
    if args.dry_run:
        print("DRY RUN - nothing will be created, loaded or changed.")
    if "create" in steps:
        step_create(client, location=settings.bq_location, dry_run=args.dry_run, max_bytes_billed=max_bytes, **common)
    if "load" in steps:
        step_load(client, bucket=args.bucket, prefix=args.gcs_prefix, dry_run=args.dry_run, **common)
    if "reference" in steps:
        step_reference(
            client,
            location=settings.bq_location,
            bucket=args.bucket,
            states=settings.supported_states,
            first_year=args.noaa_from_year,
            run_id=args.run_id or new_run_id(),
            dry_run=args.dry_run,
            max_bytes_billed=max_bytes,
            **common,
        )
    if "transform" in steps:
        step_transform(client, location=settings.bq_location, only=only, dry_run=args.dry_run, max_bytes_billed=max_bytes, **common)
    print("\nAll requested steps finished." + ("" if args.dry_run else " Verify with the queries in docs/data_loading.md."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
