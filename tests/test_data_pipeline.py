"""Offline tests for data_pipeline/ (no network, no Google Cloud).

WHAT IS CHECKED
    * SQL placeholder substitution is safe and complete.
    * The code tables in data_pipeline/nfip_codes.py and the CASE expressions
      in data_pipeline/sql/*.sql are identical (they are maintained by hand
      in two places, so a test must catch drift).
    * The SQL produces exactly the columns claimdesk/data_access queries
      (the "table contract").
    * Generated policy numbers always normalize the same way in SQL and in
      claimdesk.normalize_policy_number.
    * The OpenFEMA URL builder, page planner and resumable downloader.
    * The NOAA / ZIP reference copy (EXPORT DATA in US -> bucket -> our
      us-central1 dataset) and the weather check that reads ONLY our copies.
"""

from __future__ import annotations

import gzip
import json
import random
import re
from pathlib import Path

import pytest

from claimdesk.contracts import BenchmarkResult, PolicyRecord
from claimdesk.data_access import loss_benchmarks, policy_registry
from claimdesk.data_access.policy_registry import normalize_policy_number
from data_pipeline import fetch_openfema as fetch
from data_pipeline import load_to_bigquery as loader
from data_pipeline import nfip_codes as codes

SQL_DIR = Path(loader.SQL_DIR)


def sql_text(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def function_block(sql: str, name: str) -> str:
    """Text of ``CREATE TEMP FUNCTION <name>(...) AS (...);`` up to the ``;``."""

    match = re.search(rf"CREATE TEMP FUNCTION {name}\(.*?\);\n", sql, flags=re.S)
    assert match, f"temp function {name} not found"
    return match.group(0)


def when_map(block: str, value_pattern: str = r"'([^']*)'|(\d+)") -> dict[str, str]:
    """Parse ``WHEN <key> THEN <value>`` pairs from a CASE block."""

    pairs = re.findall(rf"WHEN\s+('?[\w]+'?)\s+THEN\s+({value_pattern})", block)
    result = {}
    for key, value, *_ in pairs:
        result[key.strip("'")] = value.strip("'")
    return result


def final_select_aliases(sql: str) -> set[str]:
    """Output column names of the last SELECT in a CREATE TABLE ... AS file."""

    tail = sql[sql.rfind("\nSELECT") :]
    tail = tail[: tail.rfind("\nFROM")]
    names = set()
    for line in tail.splitlines():
        line = line.split("--")[0].strip().rstrip(",")
        if not line or line == "SELECT":
            continue
        alias = re.search(r"\bAS\s+(\w+)$", line)
        names.add(alias.group(1) if alias else line.split(".")[-1])
    return names


# ---------------------------------------------------------------------------
# SQL placeholder substitution
# ---------------------------------------------------------------------------
def test_render_sql_replaces_all_placeholders_and_keeps_regex_braces():
    text = "SELECT 1 FROM `{project}.{dataset}.t` WHERE REGEXP_CONTAINS(z, r'^\\d{5}') -- {location}"
    out = loader.render_sql(text, project="my-proj", dataset="claimdesk", location="US")
    assert out == "SELECT 1 FROM `my-proj.claimdesk.t` WHERE REGEXP_CONTAINS(z, r'^\\d{5}') -- US"


def test_render_sql_rejects_typos_and_bad_values():
    with pytest.raises(ValueError, match="Unknown SQL placeholders"):
        loader.render_sql("SELECT * FROM `{projcet}.x`", project="p", dataset="d")
    with pytest.raises(ValueError, match="needs a value"):
        loader.render_sql("SELECT 1", project="", dataset="d")
    with pytest.raises(ValueError, match="Unsafe value"):
        loader.render_sql("SELECT 1", project="p`; DROP", dataset="d")


@pytest.mark.parametrize("path", sorted(SQL_DIR.glob("*.sql")), ids=lambda p: p.name)
def test_every_sql_file_renders_cleanly(path):
    out = loader.render_sql(path.read_text(encoding="utf-8"), project="unit-proj", dataset="claimdesk", location="us-central1")
    assert "{project}" not in out and "{dataset}" not in out
    assert "`unit-proj.claimdesk." in out


def test_transform_files_run_in_numeric_order_and_skip_create():
    names = [p.stem for p in loader.transform_sql_files()]
    assert names == ["10_policy_registry", "20_loss_benchmarks", "30_claims_reference", "40_eval_seed_claims"]
    assert [p.stem for p in loader.transform_sql_files(only=["20_loss_benchmarks.sql"])] == ["20_loss_benchmarks"]
    with pytest.raises(ValueError):
        loader.transform_sql_files(only=["99_nope"])


def test_40_depends_only_on_tables_built_earlier():
    sql = sql_text("40_eval_seed_claims.sql")
    assert "{dataset}.policy_registry`" in sql and "{dataset}.nfip_claims_clean`" in sql
    assert "raw_nfip" not in sql


# ---------------------------------------------------------------------------
# Code tables: Python == SQL
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sql_file", ["10_policy_registry.sql", "30_claims_reference.sql"])
def test_deductible_decode_matches_python(sql_file):
    mapping = when_map(function_block(sql_text(sql_file), "decode_deductible"))
    assert {k: int(v) for k, v in mapping.items()} == codes.DEDUCTIBLE_CODES_USD


def test_deductible_examples():
    assert codes.decode_deductible("F") == 1250
    assert codes.decode_deductible(" a ") == 10000
    assert codes.decode_deductible("9") == 750
    assert codes.decode_deductible(None) is None
    assert codes.decode_deductible("Z") is None


def test_cause_of_damage_labels_match_python():
    mapping = when_map(function_block(sql_text("30_claims_reference.sql"), "cause_label"))
    assert mapping == codes.CAUSE_OF_DAMAGE


def test_cause_groups_match_python():
    mapping = when_map(function_block(sql_text("30_claims_reference.sql"), "cause_group"))
    assert mapping == codes.CAUSE_GROUP
    assert "ELSE '" + codes.DEFAULT_CAUSE_GROUP + "'" in function_block(sql_text("30_claims_reference.sql"), "cause_group")


def test_non_payment_labels_match_python():
    mapping = when_map(function_block(sql_text("30_claims_reference.sql"), "non_payment_label"))
    assert mapping == codes.NON_PAYMENT_REASON
    # The codes that explain "water damage that is not an NFIP flood".
    for code in ("01", "02", "03", "06", "12", "13", "15", "16", "20"):
        assert code in codes.NON_PAYMENT_REASON


def test_policy_line_mapping_matches_python():
    mapping = when_map(function_block(sql_text("10_policy_registry.sql"), "policy_line"))
    assert {int(k): v for k, v in mapping.items()} == codes.POLICY_LINE_BY_OCCUPANCY


@pytest.mark.parametrize("sql_file", ["20_loss_benchmarks.sql", "30_claims_reference.sql"])
def test_residential_occupancy_lists_match_python(sql_file):
    match = re.search(r"occupancyType IN \(([\d,\s]+)\)", sql_text(sql_file))
    assert match
    assert {int(x) for x in match.group(1).split(",")} == set(codes.RESIDENTIAL_OCCUPANCY_TYPES)


def _sql_string_array(sql: str, func: str) -> list[str]:
    return re.findall(r"'([^']+)'", function_block(sql, func))


def test_name_lists_match_python():
    sql = sql_text("10_policy_registry.sql")
    assert _sql_string_array(sql, "first_names") == list(codes.FIRST_NAMES)
    assert _sql_string_array(sql, "last_names") == list(codes.LAST_NAMES)
    assert len(set(codes.FIRST_NAMES)) == len(codes.FIRST_NAMES)
    assert len(set(codes.LAST_NAMES)) == len(codes.LAST_NAMES)


def test_policy_alphabet_and_code_space_match_sql():
    block = function_block(sql_text("10_policy_registry.sql"), "policy_code")
    assert f"'{codes.POLICY_ALPHABET}'" in block
    assert str(codes.POLICY_CODE_SPACE) in block
    assert "GENERATE_ARRAY(0, 5)" in block  # 6 characters
    assert codes.POLICY_CODE_SPACE == 30**6
    assert not set("0O1ILU") & set(codes.POLICY_ALPHABET)
    assert len(set(codes.POLICY_ALPHABET)) == 30


def test_clean_city_examples():
    assert codes.clean_city("NA", "HOUSTON, CITY OF") == "Houston"
    assert codes.clean_city("Currently Unavailable", "HARDIN COUNTY *") == "Hardin County"
    assert codes.clean_city(None, "SOUTH HOUSTON,CITY OF") == "South Houston"
    assert codes.clean_city("NA", "DENVER, CITY AND COUNTY OF") == "Denver"
    assert codes.clean_city("austin", "TRAVIS COUNTY *") == "Austin"
    assert codes.clean_city("NA", None) is None


# ---------------------------------------------------------------------------
# Generated policy numbers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("fingerprint", "expected"),
    [
        (0, "222222"),
        (1, "222223"),
        (-1, "222223"),  # SQL: ABS(MOD(-1, M)) = 1
        (29, "22222Z"),
        (30, "222232"),
        (codes.POLICY_CODE_SPACE, "222222"),
        (codes.POLICY_CODE_SPACE - 1, "ZZZZZZ"),
    ],
)
def test_policy_code_from_fingerprint_known_values(fingerprint, expected):
    assert codes.policy_code_from_fingerprint(fingerprint) == expected


@pytest.mark.parametrize(
    ("record_id", "fp_policy", "fp_first", "fp_last", "code", "name"),
    [
        # Values captured from BigQuery itself:
        #   SELECT FARM_FINGERPRINT(CONCAT('claimdesk-policy:', '1', ':0')), ...
        # and the SQL policy_code()/fictional_name() output for the same ids.
        (1, 1706022892164805915, -440062470950516807, -4495747921064020933, "3A2YSV", "Hector Navarro"),
        (217138213, -8001209667263224385, -6318691491770137013, 4362033628960658814, "DPWTW7", "Nolan Okafor"),
    ],
)
def test_python_mirror_matches_values_computed_by_bigquery(record_id, fp_policy, fp_first, fp_last, code, name):
    assert codes.policy_code_from_fingerprint(fp_policy) == code
    assert codes.fictional_name(fp_first, fp_last) == name


def test_policy_code_handles_full_int64_range():
    for fp in (2**63 - 1, -(2**63)):
        code = codes.policy_code_from_fingerprint(fp)
        assert len(code) == 6 and set(code) <= set(codes.POLICY_ALPHABET)


def test_generated_numbers_normalize_identically_in_sql_and_claimdesk():
    rng = random.Random(42)
    for _ in range(3000):
        state = rng.choice(fetch.DEFAULT_STATES)
        number = codes.format_policy_number(state, codes.policy_code_from_fingerprint(rng.getrandbits(64) - 2**63))
        assert re.fullmatch(r"FLD-[A-Z]{2}-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{6}", number)
        key = codes.policy_number_key(number)
        assert normalize_policy_number(number) == key
        # The caller may say it in lowercase with spaces instead of dashes.
        assert normalize_policy_number(number.lower().replace("-", " ")) == key


@pytest.mark.parametrize("number", ["FLD-TX-7Q2K9M", "FLD-CO-THREEX", "FLD-LA-SEVENZ", "FLD-NC-222222", "FLD-FL-ZZZZZZ"])
def test_normalize_samples_documented_in_sql(number):
    # Codes that contain digit words (THREE, SEVEN) are still left alone
    # because the word is glued to other letters (\b boundaries).
    assert normalize_policy_number(number) == codes.policy_number_key(number) == number.replace("-", "")


def test_fictional_name_uses_the_lists():
    name = codes.fictional_name(123, -456)
    first, last = name.split(" ")
    assert first == codes.FIRST_NAMES[123 % len(codes.FIRST_NAMES)]
    assert last == codes.LAST_NAMES[456 % len(codes.LAST_NAMES)]


# ---------------------------------------------------------------------------
# Table contracts: SQL output == what claimdesk/data_access reads
# ---------------------------------------------------------------------------
REGISTRY_COLUMNS = {
    "policy_number", "policyholder_name", "status", "policy_line", "property_state", "reported_city",
    "reported_zip_code", "rated_flood_zone", "effective_start", "effective_end", "building_coverage_usd",
    "contents_coverage_usd", "building_deductible_usd", "contents_deductible_usd", "primary_residence", "source_record_id",
}


def test_policy_registry_contract():
    produced = final_select_aliases(sql_text("10_policy_registry.sql"))
    assert REGISTRY_COLUMNS | {"policy_number_key"} <= produced
    # Every column the app selects is produced ...
    lookup_sql = policy_registry._LOOKUP_SQL
    for column in REGISTRY_COLUMNS | {"policy_number_key"}:
        assert re.search(rf"\b{column}\b", lookup_sql), column
    # ... and maps 1:1 onto the PolicyRecord model.
    assert set(PolicyRecord.model_fields) - {"found", "message"} == REGISTRY_COLUMNS
    assert "CLUSTER BY policy_number_key" in sql_text("10_policy_registry.sql")


def test_policy_status_values_match_app_expectations():
    # The SQL produces exactly these statuses ('pending' = term starts in the
    # future). policy_status_headline() must return readable text for each
    # (unknown ones fall back to str.title(), e.g. "Pending").
    sql = sql_text("10_policy_registry.sql")
    status_case = re.search(r"CASE\s+WHEN cancellation_date.*?END\s+AS status\b", sql, flags=re.S).group(0)
    statuses = set(re.findall(r"'(\w+)'", status_case))
    assert statuses == {"active", "pending", "expired", "cancelled"}
    for status in statuses:
        headline = policy_registry.policy_status_headline(PolicyRecord(found=True, status=status))
        assert headline and headline[0].isupper(), status


def test_loss_benchmarks_contract():
    produced = final_select_aliases(sql_text("20_loss_benchmarks.sql"))
    wanted = set(BenchmarkResult.model_fields) - {"available", "note"}
    assert wanted <= produced
    for column in wanted:
        assert re.search(rf"\b{column}\b", loss_benchmarks._SQL), column
    assert "'ALL'" in sql_text("20_loss_benchmarks.sql")


def test_app_tables_contract():
    sql = sql_text("00_create_dataset_and_tables.sql")
    assert "CREATE SCHEMA IF NOT EXISTS `{project}.{dataset}`" in sql
    assert "location = '{location}'" in sql

    def columns(table: str) -> list[str]:
        block = sql[sql.index(f"{{dataset}}.{table}`") :]
        block = block[block.index("(") + 1 : block.index("\n)")]
        return [line.split()[0] for line in block.strip().splitlines() if line.strip()]

    assert columns("intake_packets") == [
        "intake_id", "created_at", "claim_type", "routing_decision", "severity", "intake_status", "policy_number",
        "loss_state", "loss_zip_code", "date_of_loss", "estimated_loss_usd", "missing_count", "packet_gcs_uri", "packet_json",
    ]
    assert columns("conversation_traces") == [
        "intake_id", "event_time", "seq", "event_type", "role", "text", "tool_name", "tool_args_json", "tool_result_json", "service_revision",
    ]
    assert "PARTITION BY DATE(created_at)" in sql
    assert "PARTITION BY DATE(event_time)\nCLUSTER BY intake_id" in sql
    assert "partition_expiration_days = 30" in sql


def test_eval_seed_columns_documented():
    produced = final_select_aliases(sql_text("40_eval_seed_claims.sql"))
    required = {
        "eval_case_id", "state", "date_of_loss", "reported_city", "reported_zip_code", "cause_of_damage", "water_depth_inches",
        "building_damage_usd", "contents_damage_usd", "amount_paid_usd", "non_payment_reason_building", "flood_event",
        "matched_policy_number", "matched_policyholder_name", "match_level", "loss_within_policy_term",
    }
    assert required <= produced
    doc = (loader.PIPELINE_DIR.parent / "docs" / "data_dictionary.md").read_text(encoding="utf-8")
    missing = [c for c in sorted(produced) if f"`{c}`" not in doc]
    assert not missing, f"document these eval_seed_claims columns in docs/data_dictionary.md: {missing}"


# ---------------------------------------------------------------------------
# Raw schemas
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("spec", [fetch.POLICIES, fetch.CLAIMS], ids=lambda s: s.key)
def test_raw_schemas_are_valid_and_drive_select(spec):
    schema = loader.load_schema_json(spec.schema_file)
    names = [c["name"] for c in schema]
    assert names[0] == "id" and schema[0]["mode"] == "REQUIRED"
    assert len(names) == len(set(names))
    assert {c["type"] for c in schema} <= {"STRING", "INTEGER", "FLOAT", "BOOLEAN"}
    assert spec.fields == names
    assert spec.state_field in names and spec.date_field in names


def test_sql_only_reads_columns_that_exist_in_raw_schemas():
    policy_cols = set(fetch.POLICIES.fields)
    claim_cols = set(fetch.CLAIMS.fields)
    camel = re.compile(r"\b([a-z]+[A-Z]\w*)\b")  # FEMA fields are camelCase
    for name, cols in [("10_policy_registry.sql", policy_cols), ("20_loss_benchmarks.sql", claim_cols), ("30_claims_reference.sql", claim_cols)]:
        body = "\n".join(line.split("--")[0] for line in sql_text(name).splitlines())
        body = re.sub(r"'[^']*'", "''", body)  # ignore string literals
        used = set(camel.findall(body))
        assert used <= cols, f"{name} uses unknown raw columns: {sorted(used - cols)}"


def test_load_job_config():
    config = loader.build_load_job_config(loader.load_schema_json("raw_nfip_claims.json"))
    assert config.source_format == "NEWLINE_DELIMITED_JSON"
    assert config.write_disposition == "WRITE_TRUNCATE"
    assert config.ignore_unknown_values is True
    assert [f.name for f in config.schema] == fetch.CLAIMS.fields


def test_only_part_files_are_loaded():
    names = [
        "raw/openfema/nfip_claims/state=TX/part-00000.jsonl.gz",
        "raw/openfema/nfip_claims/state=TX/part-00001.jsonl",
        "raw/openfema/nfip_claims/state=TX/_manifest.json",
        "raw/openfema/nfip_claims/state=TX/_SUCCESS.json",
        "raw/openfema/nfip_claims/state=TX/part-00002.jsonl.gz.tmp",
        "raw/openfema/nfip_claims/notes.txt",
    ]
    assert loader.filter_part_object_names(names) == names[:2]


def test_source_uri():
    assert loader.source_uri("gs://my-bucket/", "raw/openfema/", "nfip_claims") == "gs://my-bucket/raw/openfema/nfip_claims/*"
    with pytest.raises(ValueError):
        loader.source_uri("", "raw/openfema", "nfip_claims")


# ---------------------------------------------------------------------------
# OpenFEMA request building + paging
# ---------------------------------------------------------------------------
def test_build_filter_uses_the_right_state_field():
    assert fetch.build_filter(fetch.POLICIES, "tx", "2025-01-01") == "propertyState eq 'TX' and policyEffectiveDate ge '2025-01-01'"
    assert fetch.build_filter(fetch.CLAIMS, "FL", "2015-01-01") == "state eq 'FL' and dateOfLoss ge '2015-01-01'"


@pytest.mark.parametrize(("state", "since"), [("TEX", "2025-01-01"), ("T'", "2025-01-01"), ("TX", "2025-1-1"), ("TX", "2025-01-01' or 1 eq 1")])
def test_build_filter_rejects_bad_input(state, since):
    with pytest.raises(ValueError):
        fetch.build_filter(fetch.CLAIMS, state, since)


def test_build_page_url():
    url = fetch.build_page_url(fetch.CLAIMS, "CO", "2015-01-01", top=10000, skip=20000)
    assert url.startswith("https://www.fema.gov/api/open/v3/NfipClaims?")
    assert "$filter=state%20eq%20'CO'%20and%20dateOfLoss%20ge%20'2015-01-01'" in url
    assert "$top=10000" in url and "$skip=20000" in url and "$format=json" in url
    assert "$select=id,state," in url
    assert "+" not in url  # OpenFEMA wants %20 for spaces
    assert "$orderby" not in url and "$inlinecount" not in url


def test_query_params_options():
    params = fetch.build_query_params(fetch.POLICIES, "NC", "2025-01-01", top=1, include_count=True, select=["id"], order_by="id")
    assert params["$inlinecount"] == "allpages"
    assert params["$select"] == "id"
    assert params["$orderby"] == "id"
    with pytest.raises(ValueError):
        fetch.build_query_params(fetch.POLICIES, "NC", "2025-01-01", top=10001)
    with pytest.raises(ValueError):
        fetch.build_query_params(fetch.POLICIES, "NC", "2025-01-01", top=10, skip=-1)


def test_plan_pages_takes_everything_when_small():
    pages = fetch.plan_pages(23_586, 10_000, 50_000)
    assert [(p.skip, p.top) for p in pages] == [(0, 10_000), (10_000, 10_000), (20_000, 3_586)]
    assert fetch.plan_pages(0) == []


def test_plan_pages_samples_evenly_without_overlap():
    total, cap = 1_966_465, 50_000
    pages = fetch.plan_pages(total, 10_000, cap)
    assert sum(p.top for p in pages) == cap
    assert len(pages) == 5
    for a, b in zip(pages, pages[1:]):
        assert a.skip + a.top <= b.skip  # no overlap
    assert pages[-1].skip + pages[-1].top <= total
    assert pages[-1].skip > total * 0.7  # reaches the end of the range


def test_plan_pages_uncapped_and_awkward_cap():
    assert len(fetch.plan_pages(151_225, 10_000, 0)) == 16
    # Cap just above one page: cheaper to take everything than risk overlaps.
    pages = fetch.plan_pages(10_500, 10_000, 10_001)
    assert [(p.skip, p.top) for p in pages] == [(0, 10_000), (10_000, 500)]


def test_paths_and_object_names(tmp_path):
    assert fetch.local_state_dir(tmp_path, fetch.CLAIMS, "TX") == tmp_path / "openfema" / "nfip_claims" / "state=TX"
    assert fetch.gcs_object_name("/raw/openfema/", fetch.POLICIES, "FL", "part-00001.jsonl.gz") == "raw/openfema/nfip_policies/state=FL/part-00001.jsonl.gz"


@pytest.mark.parametrize("compress", [True, False])
def test_write_ndjson_is_atomic_and_readable(tmp_path, compress):
    path = tmp_path / ("x.jsonl.gz" if compress else "x.jsonl")
    count = fetch.write_ndjson(path, [{"id": 1, "state": "TX"}, {"id": 2, "state": "CO"}], compress=compress)
    assert count == 2
    assert not list(tmp_path.glob("*.tmp"))
    opener = gzip.open if compress else open
    with opener(path, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    assert rows == [{"id": 1, "state": "TX"}, {"id": 2, "state": "CO"}]


def test_download_state_is_resumable(tmp_path, monkeypatch):
    calls = {"count": 0, "pages": []}

    def fake_count(session, spec, state, since, *, timeout_s):
        calls["count"] += 1
        return 25

    def fake_fetch(session, spec, params, *, timeout_s, attempts=3):
        skip, top = int(params["$skip"]), int(params["$top"])
        calls["pages"].append(skip)
        if len(calls["pages"]) == 2 and not calls.get("failed"):
            calls["failed"] = True
            raise RuntimeError("network down")  # simulate a crash mid-download
        return {spec.entity: [{"id": i} for i in range(skip, min(skip + top, 25))]}

    monkeypatch.setattr(fetch, "fetch_total_count", fake_count)
    monkeypatch.setattr(fetch, "fetch_json", fake_fetch)
    kwargs = dict(since="2015-01-01", out_dir=tmp_path, max_records=0, page_size=10, compress=True, force=False, timeout_s=1)

    with pytest.raises(RuntimeError):
        fetch.download_state(None, fetch.CLAIMS, "CO", **kwargs)
    state_dir = fetch.local_state_dir(tmp_path, fetch.CLAIMS, "CO")
    assert (state_dir / "part-00000.jsonl.gz").exists()
    assert not (state_dir / "_SUCCESS.json").exists()

    files = fetch.download_state(None, fetch.CLAIMS, "CO", **kwargs)  # resume
    assert [f.name for f in files] == ["part-00000.jsonl.gz", "part-00001.jsonl.gz", "part-00002.jsonl.gz"]
    assert calls["count"] == 1  # the page plan was reused, not re-counted
    assert calls["pages"] == [0, 10, 10, 20]  # page 0 was not downloaded twice

    before = list(calls["pages"])
    fetch.download_state(None, fetch.CLAIMS, "CO", **kwargs)  # already complete
    assert calls["pages"] == before

    ids = []
    for f in files:
        with gzip.open(f, "rt", encoding="utf-8") as handle:
            ids += [json.loads(line)["id"] for line in handle]
    assert ids == list(range(25))


def _plan(**overrides):
    kwargs = dict(query_filter="state eq 'CO' and dateOfLoss ge '2015-01-01'", fields=["id"], page_size=10, max_records=0, order_by=None, compress=True)
    kwargs.update(overrides)
    return fetch.build_manifest_plan(**kwargs)


def test_manifest_plan_includes_compress():
    plan = _plan()
    assert plan["compress"] is True
    manifest = {"entity": "NfipClaims", "total_count": 25, "pages": [], **plan}
    assert fetch.manifest_matches(manifest, plan)
    assert not fetch.manifest_matches(manifest, _plan(compress=False))  # --gzip toggled
    assert not fetch.manifest_matches(manifest, _plan(max_records=5))
    assert not fetch.manifest_matches(None, plan)
    old_manifest = {k: v for k, v in manifest.items() if k != "compress"}  # written by an older version
    assert not fetch.manifest_matches(old_manifest, plan)


def test_toggling_gzip_replans_the_state(tmp_path, monkeypatch):
    counts = {"n": 0}

    def fake_count(session, spec, state, since, *, timeout_s):
        counts["n"] += 1
        return 5

    monkeypatch.setattr(fetch, "fetch_total_count", fake_count)
    monkeypatch.setattr(fetch, "fetch_json", lambda session, spec, params, *, timeout_s, attempts=3: {spec.entity: [{"id": 1}]})
    kwargs = dict(since="2015-01-01", out_dir=tmp_path, max_records=0, page_size=10, force=False, timeout_s=1)
    first = fetch.download_state(None, fetch.CLAIMS, "CO", compress=True, **kwargs)
    second = fetch.download_state(None, fetch.CLAIMS, "CO", compress=False, **kwargs)
    assert [f.name for f in first] == ["part-00000.jsonl.gz"]
    assert [f.name for f in second] == ["part-00000.jsonl"]
    assert counts["n"] == 2  # re-planned instead of "already downloaded"
    state_dir = fetch.local_state_dir(tmp_path, fetch.CLAIMS, "CO")
    assert sorted(p.name for p in state_dir.glob("part-*")) == ["part-00000.jsonl"]  # old .gz removed
    manifest = json.loads((state_dir / "_manifest.json").read_text())
    assert manifest["compress"] is False


def test_select_stale_gcs_parts_only_touches_old_parts_of_one_state():
    prefix = fetch.gcs_state_prefix("raw/openfema/", fetch.CLAIMS, "TX")
    assert prefix == "raw/openfema/nfip_claims/state=TX/"
    existing = [
        prefix + "part-00000.jsonl.gz",
        prefix + "part-00001.jsonl.gz",
        prefix + "part-00002.jsonl.gz",  # left over from a bigger earlier sample
        prefix + "part-00000.jsonl",  # left over from a run without --gzip
        prefix + "_manifest.json",  # bookkeeping: never deleted
        prefix + "sub/part-00009.jsonl.gz",  # not directly in the folder
        "raw/openfema/nfip_claims/state=TXX/part-00007.jsonl.gz",  # another "folder"
        "raw/openfema/nfip_claims/state=CO/part-00005.jsonl.gz",  # another state
    ]
    current = ["part-00000.jsonl.gz", "part-00001.jsonl.gz"]
    assert fetch.select_stale_gcs_parts(existing, prefix, current) == [
        prefix + "part-00000.jsonl",
        prefix + "part-00002.jsonl.gz",
    ]
    assert fetch.select_stale_gcs_parts([], prefix, current) == []
    # A state with no rows this time: every old part file is stale.
    assert fetch.select_stale_gcs_parts(existing[:2], prefix, []) == existing[:2]


def test_md5_skip_compares_checksums_not_sizes(tmp_path):
    import base64
    import hashlib

    path = tmp_path / "part-00000.jsonl"
    path.write_bytes(b'{"id":1}\n')
    local = fetch.local_md5_base64(path)
    assert local == base64.b64encode(hashlib.md5(b'{"id":1}\n').digest()).decode()
    assert not fetch.needs_upload(local, local)
    path.write_bytes(b'{"id":2}\n')  # same size, different content
    assert fetch.needs_upload(local, fetch.local_md5_base64(path))
    assert fetch.needs_upload(None, local)


def test_upload_files_skips_identical_and_deletes_stale(tmp_path, monkeypatch):
    import sys
    import types

    part0, part1 = tmp_path / "part-00000.jsonl", tmp_path / "part-00001.jsonl"
    part0.write_bytes(b'{"id":1}\n')
    part1.write_bytes(b'{"id":2}\n')
    prefix = "raw/openfema/nfip_claims/state=CO/"
    store = {
        prefix + "part-00000.jsonl": fetch.local_md5_base64(part0),  # identical -> skipped
        prefix + "part-00001.jsonl": "stale-md5==",  # same name, different bytes -> re-uploaded
        prefix + "part-00002.jsonl": "x",  # not in this download -> deleted
        prefix + "_SUCCESS.json": "y",  # bookkeeping -> kept
    }
    uploads, deletes = [], []

    class FakeBlob:
        def __init__(self, name):
            self.name, self.md5_hash = name, store.get(name)

        def upload_from_filename(self, filename, content_type=None):
            uploads.append(self.name)

        def delete(self):
            deletes.append(self.name)

    class FakeBucket:
        def get_blob(self, name):
            return FakeBlob(name) if name in store else None

        def blob(self, name):
            return FakeBlob(name)

    class FakeClient:
        def __init__(self, project=None):
            pass

        def bucket(self, name):
            return FakeBucket()

        def list_blobs(self, bucket_name, prefix=""):
            return [FakeBlob(n) for n in store if n.startswith(prefix)]

    fake_storage = types.SimpleNamespace(Client=FakeClient)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)
    import google.cloud

    monkeypatch.setattr(google.cloud, "storage", fake_storage, raising=False)
    n = fetch.upload_files([part0, part1], bucket_name="b", prefix="raw/openfema", spec=fetch.CLAIMS, state="CO")
    assert n == 1 and uploads == [prefix + "part-00001.jsonl"]
    assert deletes == [prefix + "part-00002.jsonl"]

    uploads.clear(), deletes.clear()
    fetch.upload_files([part0, part1], bucket_name="b", prefix="raw/openfema", spec=fetch.CLAIMS, state="CO", delete_stale=False)
    assert deletes == []


# ---------------------------------------------------------------------------
# Reference data: NOAA flood events + ZIP points copied into us-central1
# ---------------------------------------------------------------------------
REF_DIR = Path(loader.REFERENCE_SQL_DIR)
ALL_YEARS = list(range(2015, 2027))


def output_columns(select_sql: str) -> list[str]:
    """Output names of the LAST ``SELECT ... FROM`` in ``select_sql``.

    Handles ``expr AS name`` lines and bare ``name`` lines; continuation
    lines of multi-line expressions are skipped.
    """

    tail = select_sql[select_sql.rfind("\nSELECT") :]
    tail = tail[: tail.find("\nFROM")]
    names = []
    for line in tail.splitlines():
        line = line.split("--")[0].strip().rstrip(",")
        if line in ("", "SELECT"):
            continue
        alias = re.search(r"\bAS\s+(\w+)$", line)
        if alias:
            names.append(alias.group(1))
        elif re.fullmatch(r"\w+", line):
            names.append(line)
    return names


def render_export(ref, **overrides):
    kwargs = dict(
        project="unit-proj",
        dataset="claimdesk",
        location="us-central1",
        bucket="unit-bucket",
        run_id="20260924T101500Z",
        states=("CO", "TX", "FL", "LA", "NC"),
        years=ALL_YEARS,
    )
    kwargs.update(overrides)
    return loader.render_reference_export(ref, **kwargs)


def build_blocks() -> dict[str, str]:
    """``{table_name: CREATE ... ; block}`` from 03_build_reference_tables.sql."""

    sql = (REF_DIR / loader.BUILD_REFERENCE_SQL).read_text(encoding="utf-8")
    blocks = {}
    for chunk in sql.split("CREATE OR REPLACE TABLE ")[1:]:
        name = re.match(r"`\{project\}\.\{dataset\}\.(\w+)`", chunk).group(1)
        blocks[name] = chunk.split(";\n")[0]
    return blocks


def test_steps_run_reference_before_transform():
    assert loader.STEPS == ("create", "load", "reference", "transform")
    # Reference SQL lives in a sub-folder, so it is never picked up as a transform.
    assert all(p.parent == SQL_DIR for p in loader.transform_sql_files())


@pytest.mark.parametrize("ref", loader.REFERENCE_TABLES, ids=lambda r: r.name)
def test_reference_exports_render_to_our_bucket_in_parquet(ref):
    sql = render_export(ref)
    assert sql.lstrip().startswith("--") and "EXPORT DATA OPTIONS" in sql
    assert f"uri = 'gs://unit-bucket/reference/{ref.name}/run=20260924T101500Z/part-*.parquet'" in sql
    assert "format = 'PARQUET'" in sql
    assert "state_code IN UNNEST(['CO', 'TX', 'FL', 'LA', 'NC'])" in sql
    assert not re.search(r"\{[a-z_]+\}", sql)
    # EXPORT DATA refuses wildcard ("meta") tables.
    assert "storms_*`" not in sql and "_TABLE_SUFFIX" not in sql


def test_noaa_export_reads_every_requested_year_and_canonical_event_types():
    from claimdesk.data_access.weather_events import FLOOD_EVENT_TYPES

    sql = render_export(loader.REFERENCE_TABLES[0], years=[2016, 2015, 2024])
    tables = re.findall(r"noaa_historic_severe_storms\.storms_(\d{4})`", sql)
    assert tables == ["2015", "2016", "2024"]
    for event_type in FLOOD_EVENT_TYPES:
        assert f"'{event_type}'" in sql
    # The source spells event types in lowercase -> compared with LOWER().
    assert "LOWER(s.event_type)" in sql


def test_noaa_export_maps_state_by_fips_not_by_the_truncated_name():
    """Public NOAA `state` holds only 2 letters ("Te", "No"...), so the
    export must join on the FIPS number and take the name from zip_codes."""

    sql = render_export(loader.REFERENCE_TABLES[0])
    assert "st.state_fips = SAFE_CAST(s.state_fips_code AS INT64)" in sql
    assert "st.state_name AS state" in sql
    assert "s.state " not in sql and "s.state," not in sql


@pytest.mark.parametrize("ref", loader.REFERENCE_TABLES, ids=lambda r: r.name)
def test_export_columns_match_staging_schema(ref):
    exported = output_columns(render_export(ref))
    schema = [c["name"] for c in loader.load_schema_json(ref.schema_file)]
    assert exported == schema
    for col in loader.load_schema_json(ref.schema_file):
        assert col["type"] in {"STRING", "INTEGER", "FLOAT", "DATE", "DATETIME"}
        assert col.get("description")


def test_event_begin_time_is_loaded_as_datetime():
    """Parquet stores DATETIME as a zone-less timestamp; without an explicit
    schema BigQuery would load it as TIMESTAMP."""

    schema = {c["name"]: c["type"] for c in loader.load_schema_json("stg_noaa_flood_events.json")}
    assert schema["event_begin_time"] == "DATETIME"
    config = loader.build_parquet_load_job_config(loader.load_schema_json("stg_noaa_flood_events.json"))
    assert config.source_format == "PARQUET"
    assert config.write_disposition == "WRITE_TRUNCATE"


def test_build_sql_renders_and_builds_both_tables():
    sql = loader.render_sql(
        (REF_DIR / loader.BUILD_REFERENCE_SQL).read_text(encoding="utf-8"),
        project="unit-proj",
        dataset="claimdesk",
        location="us-central1",
    )
    assert "`bigquery-public-data" not in sql  # only mentioned in descriptions
    assert "CREATE OR REPLACE TABLE `unit-proj.claimdesk.noaa_flood_events`" in sql
    assert "PARTITION BY DATE_TRUNC(event_date, MONTH)" in sql
    assert "CLUSTER BY state_code, event_type" in sql
    assert "CREATE OR REPLACE TABLE `unit-proj.claimdesk.zip_points`\nCLUSTER BY zip_code" in sql
    assert sql.count("SAFE.ST_GEOGPOINT(lon, lat)") == 2
    for ref in loader.REFERENCE_TABLES:
        assert f"FROM `unit-proj.claimdesk.{ref.staging_table}`" in sql
        assert f"DROP TABLE IF EXISTS `unit-proj.claimdesk.{ref.staging_table}`" in sql


def test_build_tables_provide_every_column_the_weather_query_uses():
    from claimdesk.data_access import weather_events

    blocks = build_blocks()
    noaa = set(output_columns(blocks["noaa_flood_events"]))
    zips = set(output_columns(blocks["zip_points"]))
    assert {"event_type", "event_date", "event_point", "cz_type", "state_code", "cz_name_normalized"} <= noaa
    assert {"zip_code", "point", "county_name_normalized", "state_code"} <= zips
    used_noaa = set(re.findall(r"\bs\.(\w+)", weather_events._SQL_TEMPLATE))
    used_zip = {"zip_code", "point", "county_name_normalized", "state_code"}
    assert used_noaa <= noaa, used_noaa - noaa
    assert used_zip <= zips
    # Every staging column is carried into the final table.
    for ref in loader.REFERENCE_TABLES:
        staged = {c["name"] for c in loader.load_schema_json(ref.schema_file)}
        assert staged <= set(output_columns(blocks[ref.name])), staged - set(output_columns(blocks[ref.name]))


def test_county_keys_use_the_same_normalization_on_both_sides():
    zip_sql = (REF_DIR / "02_export_zip_points.sql").read_text(encoding="utf-8")
    build_sql = (REF_DIR / loader.BUILD_REFERENCE_SQL).read_text(encoding="utf-8")
    assert "r'[^A-Z]', ''" in zip_sql
    assert "REGEXP_REPLACE(cz_name, r'[^A-Z]', '') AS cz_name_normalized" in build_sql
    assert "UPPER(s.cz_name) AS cz_name" in (REF_DIR / "01_export_noaa_flood_events.sql").read_text(encoding="utf-8")

    def zip_key(county: str) -> str:  # Python mirror of 02_export_zip_points.sql
        base = re.sub(r" (County|Parish|Borough|Census Area|Municipality|city)$", "", county)
        return re.sub(r"[^A-Z]", "", base.upper())

    def noaa_key(cz_name: str) -> str:  # Python mirror of 03_build_reference_tables.sql
        return re.sub(r"[^A-Z]", "", cz_name.upper())

    assert zip_key("St. Charles Parish") == noaa_key("ST. CHARLES") == "STCHARLES"
    assert zip_key("DeSoto County") == noaa_key("DE SOTO") == "DESOTO"
    assert zip_key("New Hanover County") == noaa_key("NEW HANOVER")


def test_sql_string_array_and_state_codes_are_injection_safe():
    assert loader.sql_string_array(["CO", "Storm Surge/Tide", "Hurricane (Typhoon)"]) == "['CO', 'Storm Surge/Tide', 'Hurricane (Typhoon)']"
    for bad in (["TX'; DROP TABLE x; --"], ["a\\b"], []):
        with pytest.raises(ValueError):
            loader.sql_string_array(bad)
    assert loader.state_codes([" tx", "co ", ""]) == ["TX", "CO"]
    for bad in (["TEX"], ["T1"], []):
        with pytest.raises(ValueError):
            loader.state_codes(bad)


def test_noaa_years_from_table_list_and_fallback():
    tables = ["storms_1950", "storms_2014", "storms_2015", "storms_2026", "tornado_paths", "storms_2020", "wind_reports"]
    assert loader.noaa_years(2015, tables, this_year=2030) == [2015, 2020, 2026]
    assert loader.noaa_years(2024, None, this_year=2026) == [2024, 2025, 2026]
    with pytest.raises(ValueError):
        loader.storms_union_sql([])


def test_run_id_and_reference_uri():
    from datetime import datetime, timezone

    run_id = loader.new_run_id(datetime(2026, 9, 24, 10, 15, 0, tzinfo=timezone.utc))
    assert run_id == "20260924T101500Z"
    assert re.fullmatch(r"\d{8}T\d{6}Z", loader.new_run_id())
    assert loader.reference_uri("gs://b/", "zip_points", run_id) == "gs://b/reference/zip_points/run=20260924T101500Z/*.parquet"
    with pytest.raises(ValueError):
        loader.reference_uri("", "zip_points", run_id)


def test_default_states_come_from_settings():
    from claimdesk.settings import get_settings

    assert fetch.DEFAULT_STATES == tuple(get_settings().supported_states)


def test_print_only_reference_step_runs_exports_in_us(capsys):
    rc = loader.main(["--steps", "reference", "--dry-run", "--print-only", "--bucket", "unit-bucket", "--run-id", "20260924T101500Z"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("EXPORT DATA OPTIONS") == 2
    assert "01_export_noaa_flood_events.sql (job location US)" in out
    assert "03_build_reference_tables.sql (job location us-central1)" in out
    assert "gs://unit-bucket/reference/noaa_flood_events/run=20260924T101500Z/*.parquet -> " in out


def test_location_warning_compares_with_region(monkeypatch, capsys):
    import dataclasses

    from claimdesk.settings import get_settings

    base = get_settings()
    monkeypatch.setattr(loader, "get_settings", lambda: dataclasses.replace(base, bq_location="us-central1", region="us-central1"))
    assert loader.main(["--steps", "create", "--dry-run", "--print-only"]) == 0
    assert "WARNING" not in capsys.readouterr().err
    monkeypatch.setattr(loader, "get_settings", lambda: dataclasses.replace(base, bq_location="US", region="us-central1"))
    assert loader.main(["--steps", "create", "--dry-run", "--print-only"]) == 0
    assert "differs from CLAIMDESK_REGION" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Weather check: runtime SQL reads ONLY our dataset
# ---------------------------------------------------------------------------
def test_weather_sql_uses_only_our_tables():
    from claimdesk.data_access import weather_events
    from claimdesk.settings import get_settings

    sql = weather_events.build_weather_sql()
    prefix = get_settings().bq_prefix
    assert f"`{prefix}.zip_points`" in sql
    assert f"`{prefix}.noaa_flood_events`" in sql
    assert "bigquery-public-data" not in sql
    assert "_TABLE_SUFFIX" not in sql and "storms_" not in sql
    assert "s.event_date BETWEEN @date_from AND @date_to" in sql  # partition pruning
    assert not re.search(r"\{[a-z_]+\}", sql)


@pytest.fixture()
def weather(monkeypatch):
    from claimdesk.data_access import weather_events

    weather_events._RESULT_CACHE.clear()
    calls = []

    def fake_run_query(sql, params, **kwargs):
        calls.append((sql, params, kwargs))
        return [
            {"event_type": "Flash Flood", "distance_km": 12.34},
            {"event_type": "Flood", "distance_km": None},
        ]

    monkeypatch.setattr(weather_events, "run_query", fake_run_query)
    yield weather_events, calls
    weather_events._RESULT_CACHE.clear()


def test_check_weather_parameters_and_result(weather):
    from datetime import date

    module, calls = weather
    result = module.check_weather("77002-1234", "2024-07-08", window_days=3, radius_km=50)
    assert result.checked and result.events_found == 2
    assert result.event_types == ["Flash Flood", "Flood"]
    assert result.nearest_event_km == 12.3
    sql, params, kwargs = calls[0]
    assert params == {"zip": "77002", "date_from": date(2024, 7, 5), "date_to": date(2024, 7, 11), "radius_m": 50000.0}
    assert kwargs["array_params"] == {"event_types": list(module.FLOOD_EVENT_TYPES)}
    # Second call with the same inputs is served from the in-process cache.
    module.check_weather("77002", "2024-07-08")
    assert len(calls) == 1


def test_check_weather_degrades_and_does_not_cache_failures(monkeypatch, weather):
    from claimdesk.errors import DataAccessError

    module, calls = weather

    def boom(*args, **kwargs):
        raise DataAccessError("table not found: zip_points")

    monkeypatch.setattr(module, "run_query", boom)
    result = module.check_weather("77002", "2024-07-08")
    assert not result.checked and result.note == "Weather records unavailable"
    assert module._RESULT_CACHE == {}


def test_check_weather_input_guards(weather):
    module, calls = weather
    assert not module.check_weather("7700", "2024-07-08").checked
    assert not module.check_weather("77002", "July 8").checked
    assert not module.check_weather("77002", "2999-01-01").checked
    assert calls == []
