"""Build pipeline eval cases from real-looking NFIP claim rows.

WHAT THIS DOES (beginner overview)
    Hand-written cases (``evals/datasets/*.json``) are great but few. This
    script scales the dataset up from the BigQuery table
    ``{project}.claimdesk.eval_seed_claims``: every row is a (FEMA-like) flood
    claim - state, date, cause of damage, depth, damage amounts - plus a
    fictional policy number / policyholder name.

    For every row we:

    1. **Synthesize a claimant conversation** from templates, role-labeled
       exactly like production input::

           AGENT t1: Flood claim desk. Is everyone safe right now?
           CLAIMANT t2: Yes, we're all safe.
           ...

       Templates are *deterministic*: the same row always gives the same
       transcript (a hash of the row picks the "twist" - missing ZIP, no
       estimate yet, photos only planned, an injury...). Deterministic data
       means two eval runs differ only because the *agent* changed.
    2. **Write the ideal LLM outputs** ("golden outputs") the extractor and
       classifier should produce for that conversation.
    3. **Derive the answer key** (``expected``) by running the golden outputs
       through the real rule code (:func:`evals.expectations.derive_expected`)
       - so labels can never disagree with the rules.
    4. **Validate** every case and write an agents-cli ``EvaluationDataset``
       JSON file (the same format as ``evals/datasets/pipeline_core.json``).

    The FEMA ``cause_of_damage`` / ``non_payment_reason`` labels decide the
    water source: a row paid as "Stream, river, or lake overflow" becomes a
    surface-flood story; a row denied as "Backup drains" becomes a sewer
    backup story (NFIP does not pay that), and so on. Rows whose cause is not
    water at all (earth movement...) are skipped with a warning.

OPTIONAL: --use-gemini
    Template sentences all sound alike. With ``--use-gemini`` the CLAIMANT
    lines are paraphrased by ``settings.reasoning_model`` (the same model the
    agent uses - models are never hard-coded here). Paraphrasing is guarded:
    if a rewritten line loses a protected token (policy number, ZIP, amount,
    name, city, date) we keep the template line. This mode CALLS A MODEL and
    needs Vertex AI credentials; it costs a few cents per 100 cases.

RUN IT (from the project root)
    Offline, no GCP needed (uses the sample seed file)::

        uv run --no-sync python -m evals.build_eval_cases \\
            --offline evals/seeds/sample_eval_seed_claims.json \\
            --out evals/results/generated/seed_cases.json

    From BigQuery (needs ``gcloud auth application-default login``)::

        GOOGLE_CLOUD_PROJECT=my-proj uv run --no-sync python -m evals.build_eval_cases \\
            --limit 50 --state TX

    Output goes to ``evals/results/`` by default, which is git-ignored:
    generated cases are derived from real claim rows, so treat them as data,
    not code. Promote hand-checked cases into ``evals/datasets/`` yourself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

if __package__ in (None, ""):  # allow `python evals/build_eval_cases.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claimdesk.observability import get_logger  # noqa: E402
from claimdesk.settings import get_settings  # noqa: E402
from evals.expectations import (  # noqa: E402
    build_pipeline_prompt,
    default_world,
    derive_expected,
    validate_dataset,
)

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / "evals" / "results" / "generated" / "seed_cases.json"
SEED_TABLE = "eval_seed_claims"
# Columns read from eval_seed_claims (see docs/data_dictionary.md section 6 and
# data_pipeline/sql/40_eval_seed_claims.sql). The first block is the minimum a
# seed row needs (the offline sample has only these); the second block is
# optional and makes cases richer: policy match quality -> policy_review cases,
# the real report lag, the claimant's total estimate.
SEED_COLUMNS = (
    "state", "date_of_loss", "reported_city", "reported_zip_code", "cause_of_damage",
    "water_depth", "building_damage_usd", "contents_damage_usd", "amount_paid_usd",
    "non_payment_reason", "flood_event", "matched_policy_number", "matched_policyholder_name",
)
OPTIONAL_SEED_COLUMNS = (
    "eval_case_id", "stratum", "report_lag_days", "total_damage_usd", "claim_outcome",
    "matched_policy_status", "matched_policy_city", "matched_policy_zip_code",
    "matched_policy_effective_start", "matched_policy_effective_end",
    "loss_within_policy_term", "match_level",
)
# Issue texts produced by claimdesk/data_access/policy_registry.py
# (review_policy_against_claim). Any issue routes the claim to policy_review.
ISSUE_OUTSIDE_TERM = "Loss date falls outside the recorded policy term"
ISSUE_CANCELLED = "Policy is recorded as cancelled - human review required"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_\-]+$")  # BigQuery project/dataset/table names

STATE_NAMES = {
    "AL": "Alabama", "CA": "California", "CO": "Colorado", "FL": "Florida", "GA": "Georgia",
    "LA": "Louisiana", "MS": "Mississippi", "NC": "North Carolina", "NJ": "New Jersey",
    "NY": "New York", "PA": "Pennsylvania", "SC": "South Carolina", "TX": "Texas", "VA": "Virginia",
}
AREA_CODES = {"CO": "720", "TX": "713", "FL": "239", "LA": "504", "NC": "919"}

# The five deterministic "twists" (see module docstring).
TWISTS = ("complete", "no_zip", "no_amount", "photos_planned", "injury")


# ---------------------------------------------------------------------------
# 1. Loading seed rows
# ---------------------------------------------------------------------------
def load_offline_rows(path: Path) -> list[dict[str, Any]]:
    """Read a local seed file: ``{"rows": [...]}`` or a bare list of rows."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("rows", []) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a list of rows or an object with 'rows'")
    return rows


def seed_query(project: str, dataset: str, table: str) -> str:
    """SQL for the seed table. Identifiers are validated; VALUES are parameters.

    WHY PARAMETERS: BigQuery query parameters (``@state``, ``@limit``) are sent
    separately from the SQL text, so a value can never change the query
    (no SQL injection) and identical queries hit the 24 h result cache.
    Table names cannot be parameters, so we validate them with a regex.
    """

    for name in (project, dataset, table):
        if not _IDENTIFIER.match(name or ""):
            raise ValueError(f"Invalid BigQuery identifier: {name!r}")
    columns = ", ".join(SEED_COLUMNS + OPTIONAL_SEED_COLUMNS)
    return (
        f"SELECT {columns}\n"
        f"FROM `{project}.{dataset}.{table}`\n"
        "WHERE (@state = '' OR state = @state)\n"
        "  AND (@clean_only = FALSE OR (match_level = 'zip_and_term' AND matched_policy_status != 'cancelled'))\n"
        "ORDER BY state, stratum, date_of_loss, eval_case_id\n"  # stable order -> stable case ids
        "LIMIT @limit"
    )


def load_bigquery_rows(project: str, dataset: str, table: str, *, limit: int, state: str, clean_only: bool = False) -> list[dict[str, Any]]:
    """Query the seed table (one small, capped, labeled BigQuery job)."""

    from claimdesk.data_access.bq_client import run_query  # lazy: offline mode never imports BigQuery

    sql = seed_query(project, dataset, table)
    log.info("Reading eval seeds from BigQuery", extra={"json_fields": {"table": f"{project}.{dataset}.{table}", "limit": limit, "state": state}})
    return run_query(sql, {"state": state.upper(), "limit": int(limit), "clean_only": bool(clean_only)}, label="eval_seed_claims", timeout_s=60.0)


# ---------------------------------------------------------------------------
# 2. Mapping FEMA labels -> the story we tell
# ---------------------------------------------------------------------------
def plan_water(row: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(water_source, how the water got in)`` or ``None`` to skip.

    ``non_payment_reason`` wins over ``cause_of_damage``: FEMA records the
    *event* as the cause (e.g. "Accumulation of rainfall") but the reason it
    was not paid tells us what actually happened inside the house.
    """

    reason = str(row.get("non_payment_reason") or "").lower()
    cause = str(row.get("cause_of_damage") or "").lower()
    if "backup" in reason or "back up" in reason:
        return "sewer_or_drain_backup", "the floor drain in the basement backed up with dirty water during the storm"
    if "seepage" in reason:
        return "seepage", "water seeped in through the basement walls from the ground - nothing came over the doors or the street"
    if "wind" in reason:
        return "roof_or_wind_driven_rain", "the wind tore shingles off the roof and rain poured in through the bedroom ceiling"
    if any(k in cause for k in ("earth movement", "landslide", "subsidence", "sinkhole")):
        return None  # not a water loss; the flood desk can't tell this story
    if any(k in cause for k in ("tidal", "surge", "wave", "tsunami")):
        return "surface_flood", "storm surge pushed water up from the bay and it came in under the doors"
    if any(k in cause for k in ("stream", "river", "lake", "overflow", "alluvial")):
        return "surface_flood", "the creek behind our street overflowed its banks and the water came into the house"
    if any(k in cause for k in ("rainfall", "snowmelt", "accumulation")):
        return "surface_flood", "heavy rain flooded the street and the water came in under the front door"
    if "mudflow" in cause or "mud" in cause:
        return "surface_flood", "a mudflow came down the hillside and pushed water and mud through the back door"
    if "erosion" in cause:
        return "surface_flood", "the shoreline gave way in the storm and flood water reached the house"
    return None


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _long_date(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"  # "July 14, 2025" (no locale surprises)


def _depth_phrase(inches: float | None) -> str:
    if not inches:
        return "There was no standing water, but the carpets and drywall got soaked."
    inches = int(round(inches))
    if inches % 12 == 0:
        feet = inches // 12
        return f"The water got about {feet} foot deep inside." if feet == 1 else f"The water got about {feet} feet deep inside."
    return f"The water got about {inches} inches deep inside."


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _digest(*parts: Any) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest(), 16)


def pick_twist(row: dict[str, Any], enabled: bool = True) -> str:
    """Deterministically pick a twist from the row's identity (not random!)."""

    if not enabled:
        return "complete"
    return TWISTS[_digest(row.get("state"), row.get("date_of_loss"), row.get("matched_policy_number")) % len(TWISTS)]


# ---------------------------------------------------------------------------
# 3. One seed row -> one eval case
# ---------------------------------------------------------------------------
class _Transcript:
    """Collect role-labeled lines with increasing turn ids (t1, t2, ...)."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def add(self, role: str, text: str) -> str:
        self.lines.append((role, text))
        return f"t{len(self.lines)}"

    def render(self) -> str:
        return "\n".join(f"{role} t{i}: {text}" for i, (role, text) in enumerate(self.lines, start=1))


def policy_plan(row: dict[str, Any]) -> tuple[list[str], str | None, str | None]:
    """Decide policy issues and which city/ZIP the caller says.

    Returns ``(policy_issues, city_override, zip_override)``. Follows the tip in
    docs/data_dictionary.md section 6:

    * ``loss_within_policy_term = FALSE`` (``match_level = 'zip_only'``): the
      registry will report "Loss date falls outside the recorded policy term"
      -> expected route ``policy_review``.
    * ``matched_policy_status = 'cancelled'`` -> also ``policy_review``.
    * ``match_level = 'state_and_term'``: the policy is for a different ZIP in
      the same state, so the caller gives the POLICY's city/ZIP to keep facts
      consistent with the registry.

    Rows without these columns (the offline sample) count as clean matches.
    """

    issues: list[str] = []
    within = row.get("loss_within_policy_term")
    if within is not None and not bool(within):
        issues.append(ISSUE_OUTSIDE_TERM)
    if str(row.get("matched_policy_status") or "").lower() == "cancelled":
        issues.append(ISSUE_CANCELLED)
    if row.get("match_level") == "state_and_term" and row.get("matched_policy_zip_code"):
        return issues, (row.get("matched_policy_city") or None), str(row["matched_policy_zip_code"])[:5]
    return issues, None, None


def build_case(row: dict[str, Any], index: int, *, twists: bool = True, real_report_lag: bool = False) -> dict[str, Any] | None:
    """Turn one seed row into an eval case (``None`` if the row can't be used)."""

    missing = [c for c in ("state", "date_of_loss", "reported_city", "matched_policy_number", "matched_policyholder_name") if not row.get(c)]
    if missing:
        log.warning("Skipping seed row with missing columns", extra={"json_fields": {"index": index, "missing": missing}})
        return None
    plan = plan_water(row)
    if plan is None:
        log.warning("Skipping non-water seed row", extra={"json_fields": {"index": index, "cause": row.get("cause_of_damage"), "reason": row.get("non_payment_reason")}})
        return None
    water_source, entry = plan

    state = str(row["state"]).upper()
    loss_date = _as_date(row["date_of_loss"])
    city = str(row["reported_city"]).strip()
    zip_code = str(row.get("reported_zip_code") or "").strip()[:5] or None
    policy_issues, city_override, zip_override = policy_plan(row)
    city, zip_code = (str(city_override).strip() if city_override else city), (zip_override or zip_code)
    name = str(row["matched_policyholder_name"]).strip()
    policy = str(row["matched_policy_number"]).strip()
    depth = float(row["water_depth"]) if row.get("water_depth") not in (None, "") else None
    depth = depth or None  # 0 inches -> "no standing water" -> not specified
    total = row.get("total_damage_usd")
    if total in (None, ""):
        total = float(row.get("building_damage_usd") or 0) + float(row.get("contents_damage_usd") or 0)
    amount = int(round(float(total))) or None
    twist = pick_twist(row, twists)
    if twist == "no_zip" and zip_code is None:
        twist = "complete"
    phone = f"{AREA_CODES.get(state, '555')}-555-01{_digest(policy) % 100:02d}"  # 555-01xx = fictional
    state_name = STATE_NAMES.get(state, state)

    say_zip = zip_code if twist != "no_zip" else None
    say_amount = amount if twist != "no_amount" else None

    t = _Transcript()
    t.add("AGENT", "Flood claim desk, this is the intake assistant. Is everyone safe right now?")
    safety_facts: list[dict[str, Any]] = []
    concerns: list[str] = []
    if twist == "injury":
        safety_turn = t.add("CLAIMANT", "Not really - my husband slipped on the wet basement stairs and hurt his back, and the power is still on down there.")
        safety_facts = [
            {"category": "injury", "status": "present", "description": "Husband hurt his back slipping on wet stairs", "source_turn_ids": [safety_turn]},
            {"category": "electrical", "status": "present", "description": "Power still on in the flooded basement", "source_turn_ids": [safety_turn]},
        ]
        concerns = ["Husband hurt his back on the wet stairs", "Power still on in the flooded basement"]
        t.add("AGENT", "I'm sorry. If he needs care please call 911, and stay out of the basement while the power is on. Can I have your name and policy number?")
    else:
        safety_turn = t.add("CLAIMANT", "Yes, we're all safe. Nobody was hurt.")
        safety_facts = [{"category": "injury", "status": "absent", "description": "Nobody was hurt", "source_turn_ids": [safety_turn]}]
        t.add("AGENT", "Glad to hear it. Can I have your name and policy number?")
    t.add("CLAIMANT", f"It's {name}, policy number {policy}.")
    t.add("AGENT", "Thank you. What happened, and when?")
    event = f" It was part of the {row['flood_event']} flooding." if row.get("flood_event") else ""
    t.add("CLAIMANT", f"On {_long_date(loss_date)}, {entry}. {_depth_phrase(depth)}{event}")
    t.add("AGENT", "Where is the property?")
    if say_zip:
        t.add("CLAIMANT", f"{city}, {state_name} {say_zip}.")
    else:
        t.add("CLAIMANT", f"{city}, {state_name}. I don't remember the ZIP code offhand.")
    t.add("AGENT", "Do you have a rough estimate of the damage so far?")
    if say_amount:
        t.add("CLAIMANT", f"The contractor said roughly ${say_amount:,} for the building and our things.")
    else:
        t.add("CLAIMANT", "Not yet, nobody has looked at it.")
    t.add("AGENT", "Do you have any photos of the damage?")
    if twist == "complete":
        photo_turn = t.add("CLAIMANT", "Yes, I took photos of the damage and the water line on the wall with the app camera, and I made a list of the damaged contents.")
        evidence = [
            {"document_type": "damage_photo", "status": "available", "source_turn_ids": [photo_turn]},
            {"document_type": "water_line_photo", "status": "available", "source_turn_ids": [photo_turn]},
            {"document_type": "contents_inventory", "status": "available", "source_turn_ids": [photo_turn]},
        ]
        # Captures made through the app are *server* evidence: only the server
        # may mark documents "received" (see claimdesk/rules/evidence_rules.py).
        received = [
            {"id": f"cap-seed{index:03d}-a", "document_types": ["damage_photo", "water_line_photo"]},
            {"id": f"cap-seed{index:03d}-b", "document_types": ["contents_inventory"]},
        ]
    elif twist == "photos_planned":
        photo_turn = t.add("CLAIMANT", "Not yet. I'll take photos this afternoon once the water is gone.")
        evidence = [{"document_type": "damage_photo", "status": "planned", "source_turn_ids": [photo_turn]}]
        received = []
    else:
        photo_turn = t.add("CLAIMANT", "I have some photos of the damage on my phone.")
        evidence = [{"document_type": "damage_photo", "status": "available", "source_turn_ids": [photo_turn]}]
        received = []
    t.add("AGENT", "What's the best number to reach you?")
    t.add("CLAIMANT", f"{phone}.")

    # The call happens 2 days after the loss by default. --real-report-lag uses
    # FEMA's real open_date - date_of_loss instead, which exercises the
    # late-report rule (TIMING-002) on realistic lags.
    lag_days = int(row["report_lag_days"]) if real_report_lag and row.get("report_lag_days") not in (None, "") else 2
    reference_time = datetime.combine(loss_date + timedelta(days=max(0, lag_days)), datetime.min.time()).replace(hour=10)
    claim_type = "home_flood" if water_source == "surface_flood" else "internal_water"
    severity = "urgent" if twist == "injury" else ("high" if (depth or 0) >= 24 or (amount or 0) >= 75000 else "medium")
    missing_facts = [label for label, gone in (("loss_zip_code", not say_zip), ("estimated_loss_usd", not say_amount)) if gone]
    golden_facts = {
        "policyholder_name": name,
        "policy_number": policy,
        "contact_method": phone,
        "date_of_loss": loss_date.isoformat(),
        "loss_address_or_city": f"{city}, {state}",
        "loss_state": state,
        "loss_zip_code": say_zip or "not specified",
        "loss_description": f"{entry[0].upper()}{entry[1:]}.",
        "water_entry_description": entry,
        "water_depth_inches": depth,
        "estimated_loss_usd": say_amount,
        "injuries_or_safety_concerns": concerns,
        "missing_or_uncertain_facts": missing_facts,
        "summary": f"{entry[0].upper()}{entry[1:]} on {loss_date.isoformat()} in {city}, {state}.",
        "evidence_records": evidence,
        "safety_facts": safety_facts,
    }
    classification = {
        "claim_type": claim_type,
        "severity": severity,
        "severity_rationale": f"{twist} scenario, depth {depth or 0} in",
        "water_source": water_source,
        "water_source_rationale": entry,
    }
    case: dict[str, Any] = {
        "eval_case_id": f"seed_{index:03d}_{_slug(row.get('eval_case_id') or state)}_{water_source}_{twist}",
        "description": f"{row.get('cause_of_damage')} / non-payment: {row.get('non_payment_reason') or 'none'} ({twist}).",
        "tags": ["generated", state, water_source, twist] + (["policy_issue"] if policy_issues else []),
        "source": f"evals/build_eval_cases.py from {SEED_TABLE} row {index} (templates; identities fictional)",
        "reference_time": reference_time.isoformat(timespec="minutes"),
        "prompt": build_pipeline_prompt(t.render(), reference_time.isoformat(timespec="minutes")),
        "session_state": {"received_evidence": received},
        "world": default_world(state, flood=water_source == "surface_flood", policy_issues=policy_issues),
        "golden_outputs": {"claim_facts": golden_facts, "classification": classification},
    }
    derived = derive_expected(case)
    case["expected"] = {
        "claim_type": derived["claim_type"],
        "water_source": derived["water_source"],
        "routing": derived["routing"],
        "signals": derived["signals"],
        "facts": {
            "policyholder_name": name,
            "policy_number": policy,
            "date_of_loss": loss_date.isoformat(),
            "loss_address_or_city": city,
            "loss_state": state,
            "loss_zip_code": say_zip,  # None = the agent must leave it blank (no invented ZIPs)
            "water_depth_inches": depth,
            "estimated_loss_usd": say_amount,
            "contact_method": phone,
        },
    }
    return case


def build_dataset(rows: Iterable[dict[str, Any]], *, twists: bool = True, real_report_lag: bool = False) -> dict[str, Any]:
    """Build and validate an ``EvaluationDataset`` dict.

    NOTE: the top level holds ONLY ``eval_cases``. The Vertex AI SDK's
    ``EvaluationDataset`` model forbids unknown top-level keys (a "name" or
    "description" there would make ``agents-cli eval grade`` reject the file),
    while each *case* may carry extra fields.
    """

    cases = [c for i, row in enumerate(rows, start=1) if (c := build_case(row, i, twists=twists, real_report_lag=real_report_lag)) is not None]
    dataset = {"eval_cases": cases}
    problems = validate_dataset(dataset) if cases else ["no usable seed rows"]
    if problems:
        raise ValueError("Generated dataset is invalid:\n  " + "\n  ".join(problems))
    return dataset


# ---------------------------------------------------------------------------
# 4. Optional Gemini paraphrasing (--use-gemini)
# ---------------------------------------------------------------------------
_LINE = re.compile(r"^(CLAIMANT|AGENT) (t\d+): (.*)$")


def _protected_tokens(case: dict[str, Any]) -> list[str]:
    f = case["expected"]["facts"]
    tokens = [f["policy_number"], f["policyholder_name"], f["loss_address_or_city"], f["contact_method"]]
    if f.get("loss_zip_code"):
        tokens.append(f["loss_zip_code"])
    if f.get("estimated_loss_usd"):
        tokens.append(f"{int(f['estimated_loss_usd']):,}")
    tokens.append(_long_date(date.fromisoformat(f["date_of_loss"])))
    return [str(x) for x in tokens if x]


def paraphrase_case(case: dict[str, Any], client: Any, model: str, seed: int = 7) -> bool:
    """Rewrite the CLAIMANT lines in a more natural voice. Returns True if changed."""

    from google.genai import types  # lazy: only needed with --use-gemini

    prompt_text = case["prompt"]["parts"][0]["text"]
    header, _, transcript = prompt_text.partition("Conversation (source of truth; do not invent facts):\n")
    lines = transcript.splitlines()
    claimant = [(i, m) for i, line in enumerate(lines) if (m := _LINE.match(line)) and m.group(1) == "CLAIMANT"]
    request = (
        "Rewrite each claimant utterance so it sounds like a real, slightly stressed person on a phone call. "
        "Keep every fact, name, number, policy id, ZIP code, dollar amount and date EXACTLY as written. "
        "Do not add new facts. Return a JSON array of strings, same length and order.\n\n"
        + json.dumps([m.group(3) for _, m in claimant])
    )
    response = client.models.generate_content(
        model=model,
        contents=request,
        config=types.GenerateContentConfig(temperature=0.3, seed=seed, response_mime_type="application/json"),
    )
    try:
        rewritten = json.loads(response.text or "[]")
    except json.JSONDecodeError:
        return False
    if not isinstance(rewritten, list) or len(rewritten) != len(claimant):
        return False
    protected = _protected_tokens(case)
    changed = False
    for (i, m), new in zip(claimant, rewritten):
        original = m.group(3)
        # Guard: every protected token present in the original line must survive.
        if isinstance(new, str) and new.strip() and all(tok not in original or tok in new for tok in protected):
            lines[i] = f"CLAIMANT {m.group(2)}: {new.strip()}"
            changed = changed or new.strip() != original
    if changed:
        case["prompt"]["parts"][0]["text"] = f"{header}Conversation (source of truth; do not invent facts):\n" + "\n".join(lines)
        case["source"] += f"; claimant lines paraphrased by {model}"
        case["tags"].append("paraphrased")
    return changed


def paraphrase_dataset(dataset: dict[str, Any]) -> int:
    from google import genai  # lazy

    settings = get_settings()
    if not settings.project_id:
        raise SystemExit("--use-gemini needs GOOGLE_CLOUD_PROJECT (and `gcloud auth application-default login`).")
    client = genai.Client(vertexai=True, project=settings.project_id, location=settings.model_location)
    count = 0
    for case in dataset["eval_cases"]:
        try:
            count += paraphrase_case(case, client, settings.reasoning_model)
        except Exception:  # noqa: BLE001 - keep the template line; one bad call must not kill the batch
            log.exception("Paraphrase failed; keeping template transcript", extra={"json_fields": {"case": case["eval_case_id"]}})
    problems = validate_dataset(dataset)
    if problems:
        raise ValueError("Paraphrased dataset is invalid:\n  " + "\n  ".join(problems))
    return count


# ---------------------------------------------------------------------------
# 5. CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", type=Path, metavar="SEED_JSON", help="read seed rows from a local JSON file instead of BigQuery")
    parser.add_argument("--project", help="GCP project (default: GOOGLE_CLOUD_PROJECT)")
    parser.add_argument("--dataset", help="BigQuery dataset (default: settings.bq_dataset)")
    parser.add_argument("--table", default=SEED_TABLE)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--state", default="", help="only rows for this two-letter state")
    parser.add_argument("--no-twists", action="store_true", help="every case uses the 'complete' story")
    parser.add_argument("--clean-only", action="store_true", help="BigQuery mode: only zip_and_term, non-cancelled policy matches")
    parser.add_argument("--real-report-lag", action="store_true", help="call happens report_lag_days after the loss (default: 2 days)")
    parser.add_argument("--use-gemini", action="store_true", help="paraphrase CLAIMANT lines with settings.reasoning_model (calls a model)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    if args.offline:
        rows = load_offline_rows(args.offline)
        if args.state:
            rows = [r for r in rows if str(r.get("state", "")).upper() == args.state.upper()]
        rows = rows[: args.limit]
    else:
        settings = get_settings()
        project = args.project or settings.project_id
        if not project:
            parser.error("set GOOGLE_CLOUD_PROJECT or --project (or use --offline)")
        rows = load_bigquery_rows(project, args.dataset or settings.bq_dataset, args.table, limit=args.limit, state=args.state, clean_only=args.clean_only)

    dataset = build_dataset(rows, twists=not args.no_twists, real_report_lag=args.real_report_lag)
    if args.use_gemini:
        changed = paraphrase_dataset(dataset)
        log.info("Paraphrased cases", extra={"json_fields": {"changed": changed}})

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(dataset, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    routes: dict[str, int] = {}
    for case in dataset["eval_cases"]:
        routes[case["expected"]["routing"]] = routes.get(case["expected"]["routing"], 0) + 1
    print(f"Wrote {len(dataset['eval_cases'])} cases to {args.out}  routing mix: {routes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
