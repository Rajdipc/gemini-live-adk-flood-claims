"""Shared helpers for ClaimDesk pipeline evals: prompt format, world stubs,
answer-key derivation and dataset validation.

WHY THIS MODULE EXISTS
    A pipeline eval case needs three things besides the conversation:

    1. **The exact input format** the production code uses. ``run_intake_pipeline``
       wraps the transcript in a small header with a reference date; eval
       cases use the same wrapper (:func:`build_pipeline_prompt`) so the model
       sees exactly what it sees in production.
    2. **A "world"** - the answers the BigQuery look-ups would give (policy
       issues, NFIP loss benchmark, NOAA weather). Pipeline evals should
       measure the *LLM + rules*, not whatever happens to be in your tables
       today, so by default ``evals/generate_traces.py`` replaces the three
       look-ups with the per-case ``world`` stub. (``--live-data`` turns the
       real look-ups back on.)
    3. **An answer key** (``expected``). The routing we expect is not guessed:
       :func:`derive_expected` feeds the case's hand-written *golden* LLM
       outputs (what a perfect extractor/classifier would return) through the
       real rule code. ``tests/test_evals_offline.py`` checks every dataset's
       ``expected`` block against this derivation, so a hand-edited label can
       never silently drift away from the rules.

EVAL CASE FORMAT (agents-cli ``EvaluationDataset``)
    Standard fields understood by agents-cli / the Vertex AI eval SDK::

        eval_case_id   str                      unique, stable id
        prompt         Content                  {"role": "user", "parts": [{"text": ...}]}

    Extra fields (allowed by the schema, passed through to metrics untouched)::

        expected        answer key graded by evals/custom_metrics.py
        world           BigQuery stubs used by evals/generate_traces.py
        session_state   initial ADK session state (server evidence captures)
        reference_time  ISO date-time the conversation "happens" at
        golden_outputs  ideal extractor/classifier outputs (tests only)
        tags, source    free-form labels for slicing results

This module runs in the project's ``.venv`` (it imports ``claimdesk``); the
metric code in ``custom_metrics.py`` deliberately does not.
"""

from __future__ import annotations

import contextlib
import contextvars
from datetime import date, datetime
from typing import Any, Iterator, get_args

from claimdesk.contracts import ClaimClassification, ClaimFacts, ClaimType, RoutingDecision, WaterSource
from claimdesk.rules import required_fields as _required_fields_module
from claimdesk.rules import risk_signals as _risk_signals_module
from claimdesk.rules._helpers import is_blank
from claimdesk.rules.evidence_rules import apply_evidence_rules, build_checklist, merge_server_evidence
from claimdesk.rules.packet_writer import write_packet
from claimdesk.rules.required_fields import check_required_fields
from claimdesk.rules.risk_signals import score_risk_signals
from claimdesk.rules.water_source import decide_water_source

CLAIM_TYPES = set(get_args(ClaimType))
WATER_SOURCES = set(get_args(WaterSource))
ROUTES = set(get_args(RoutingDecision))
FACT_FIELDS = set(ClaimFacts.model_fields)

# ---------------------------------------------------------------------------
# 1. Prompt format (mirrors claimdesk.intake_pipeline.run_intake_pipeline)
# ---------------------------------------------------------------------------
PROMPT_TEMPLATE = "Reference date and time: {now}\n\nConversation (source of truth; do not invent facts):\n{text}"


def build_pipeline_prompt(transcript: str, reference_time: str) -> dict[str, Any]:
    """Return the ``prompt`` Content for an eval case.

    ``reference_time`` is ISO 8601 (e.g. ``2025-07-16T10:00``). The model uses
    it to resolve "yesterday" / "last night", exactly as in production.
    """

    now = datetime.fromisoformat(reference_time).isoformat(timespec="minutes")
    return {"role": "user", "parts": [{"text": PROMPT_TEMPLATE.format(now=now, text=transcript.strip())}]}


def transcript_from_prompt(prompt: dict[str, Any]) -> str:
    """Inverse of :func:`build_pipeline_prompt` (used by tests and trace tools)."""

    text = "".join(p.get("text", "") for p in prompt.get("parts", []))
    marker = "Conversation (source of truth; do not invent facts):\n"
    return text.split(marker, 1)[1] if marker in text else text


def reference_date(case: dict[str, Any]) -> date:
    return datetime.fromisoformat(case["reference_time"]).date()


# ---------------------------------------------------------------------------
# 2. World stubs (what BigQuery would have answered)
# ---------------------------------------------------------------------------
# ILLUSTRATIVE numbers, NOT real FEMA benchmarks. They are in a realistic
# range so thresholds behave sensibly; use --live-data for the real table.
STUB_BENCHMARKS: dict[str, dict[str, Any]] = {
    "CO": {"sample_size": 3100, "damage_p50_usd": 15000, "damage_p90_usd": 60000, "damage_p95_usd": 95000, "report_lag_p95_days": 60},
    "TX": {"sample_size": 150000, "damage_p50_usd": 22000, "damage_p90_usd": 85000, "damage_p95_usd": 130000, "report_lag_p95_days": 45},
    "FL": {"sample_size": 120000, "damage_p50_usd": 25000, "damage_p90_usd": 100000, "damage_p95_usd": 150000, "report_lag_p95_days": 60},
    "LA": {"sample_size": 180000, "damage_p50_usd": 28000, "damage_p90_usd": 95000, "damage_p95_usd": 140000, "report_lag_p95_days": 50},
    "NC": {"sample_size": 40000, "damage_p50_usd": 20000, "damage_p90_usd": 80000, "damage_p95_usd": 120000, "report_lag_p95_days": 60},
}


def stub_benchmark(state: str | None) -> dict[str, Any]:
    code = str(state or "").upper()
    if code not in STUB_BENCHMARKS:
        return {"available": False, "state": code, "note": "No stub benchmark for this state"}
    return {"available": True, "state": code, "note": "Eval stub (illustrative, not real FEMA numbers)", **STUB_BENCHMARKS[code]}


def default_world(state: str | None, *, flood: bool = True, policy_issues: list[str] | None = None) -> dict[str, Any]:
    """A clean world: policy matches, benchmark for the state, NOAA saw rain."""

    return {
        "policy_issues": list(policy_issues or []),
        "benchmark": stub_benchmark(state),
        "weather": (
            {"checked": True, "events_found": 2, "event_types": ["Flash Flood", "Heavy Rain"], "nearest_event_km": 8.0, "note": "2 NOAA flood/rain events within 50 km and 3 days (eval stub)."}
            if flood
            else None
        ),
    }


# The world for the case currently running. A ContextVar (not a global) so
# several cases can run concurrently in one process; asyncio.to_thread copies
# the context, so the stubbed BigQuery functions see the right case.
CURRENT_WORLD: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("claimdesk_eval_world", default=None)
CURRENT_TODAY: contextvars.ContextVar[date | None] = contextvars.ContextVar("claimdesk_eval_today", default=None)


def weather_input(world: dict[str, Any], claim_facts: dict[str, Any], water: dict[str, Any]) -> dict[str, Any] | None:
    """Mirror the workflow's gating: weather only for flood candidates with ZIP + date, and only if checked."""

    if not water.get("nfip_flood_candidate") or is_blank(claim_facts.get("loss_zip_code")) or is_blank(claim_facts.get("date_of_loss")):
        return None
    weather = world.get("weather")
    return weather if weather and weather.get("checked") else None


# ---------------------------------------------------------------------------
# 3. Frozen "today" for the rules
# ---------------------------------------------------------------------------
class _FrozenDate(date):
    """``date`` subclass whose ``today()`` returns the case's reference date.

    WHY: ``required_fields`` and ``risk_signals`` call ``date.today()``. When
    you replay a conversation from months ago, "today" must be the day of the
    call - otherwise every old case looks like a late report (TIMING-002).
    """

    @classmethod
    def today(cls) -> date:  # type: ignore[override]
        frozen = CURRENT_TODAY.get()
        return frozen if frozen is not None else date.today()


@contextlib.contextmanager
def frozen_rule_dates() -> Iterator[None]:
    """Patch ``date`` inside the two rule modules for the duration of the block.

    Per-case dates are then set with ``CURRENT_TODAY.set(...)``. This is the
    same idea as the ``freezegun`` library, limited to our own modules.
    """

    originals = (_required_fields_module.date, _risk_signals_module.date)
    _required_fields_module.date = _FrozenDate  # type: ignore[misc]
    _risk_signals_module.date = _FrozenDate  # type: ignore[misc]
    try:
        yield
    finally:
        _required_fields_module.date, _risk_signals_module.date = originals  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 4. Answer-key derivation (golden LLM outputs -> real rules -> labels)
# ---------------------------------------------------------------------------
def run_rules(
    claim_facts: dict[str, Any],
    classification: dict[str, Any],
    *,
    world: dict[str, Any],
    today: date,
    received_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run every deterministic node of the workflow, in order, without ADK.

    Same order and inputs as ``claimdesk/intake_pipeline.py``:
    check_fields -> decide_water -> (world look-ups) -> apply_rules -> publish_packet.
    """

    merged = merge_server_evidence(claim_facts, received_evidence or [])
    field_check = check_required_fields(merged, today=today)
    water = decide_water_source(merged, classification)
    evidence = apply_evidence_rules(merged, field_check, classification, water, policy_issues=list(world.get("policy_issues") or []))
    checklist = build_checklist(merged, classification, water)
    risk = score_risk_signals(
        merged, field_check, classification, water, evidence,
        benchmark=world.get("benchmark") or {"available": False},
        weather=weather_input(world, merged, water),
        today=today,
    )
    packet = write_packet(merged, field_check, classification, water, evidence, checklist, risk)
    return {"claim_facts": merged, "field_check": field_check, "water_source": water, "evidence_decision": evidence, "checklist": checklist, "risk_gate": risk, "packet": packet}


def derive_expected(case: dict[str, Any]) -> dict[str, Any]:
    """Labels the rules produce for the case's golden outputs and world."""

    golden = case["golden_outputs"]
    facts = ClaimFacts.model_validate(golden["claim_facts"]).model_dump()
    classification = ClaimClassification.model_validate(golden["classification"]).model_dump()
    state = run_rules(
        facts,
        classification,
        world=case.get("world") or default_world(facts.get("loss_state")),
        today=reference_date(case),
        received_evidence=(case.get("session_state") or {}).get("received_evidence"),
    )
    return {
        "claim_type": state["packet"]["claim_type"],
        "water_source": state["water_source"]["water_source"],
        "routing": state["packet"]["routing_decision"],
        "signals": [s["rule_id"] for s in state["risk_gate"]["signals"]],
    }


# ---------------------------------------------------------------------------
# 5. Structural validation of a dataset (used by tests and the builder)
# ---------------------------------------------------------------------------
def validate_case(case: dict[str, Any]) -> list[str]:
    """Return a list of problems (empty list = valid).

    Checks what agents-cli needs (id + a user ``prompt``) and what our
    metrics need (a well-formed ``expected`` block).
    """

    problems: list[str] = []
    cid = case.get("eval_case_id") or "<no id>"
    if not case.get("eval_case_id"):
        problems.append("missing eval_case_id")
    prompt = case.get("prompt") or {}
    if case.get("agent_data"):
        problems.append(f"{cid}: pipeline cases must use 'prompt', not 'agent_data'")
    if prompt.get("role") != "user" or not any(p.get("text") for p in prompt.get("parts", [])):
        problems.append(f"{cid}: prompt must be a user Content with text")
    elif "CLAIMANT t" not in transcript_from_prompt(prompt):
        problems.append(f"{cid}: transcript must contain role-labeled lines like 'CLAIMANT t1: ...'")
    try:
        reference_date(case)
    except (KeyError, TypeError, ValueError):
        problems.append(f"{cid}: reference_time must be an ISO date-time")

    expected = case.get("expected")
    if not isinstance(expected, dict):
        return problems + [f"{cid}: missing expected"]

    def _check(label: str, allowed: set[str]) -> None:
        value = expected.get(label)
        values = value if isinstance(value, list) else [value]
        if not values or any(v not in allowed for v in values):
            problems.append(f"{cid}: expected.{label}={value!r} not in {sorted(allowed)}")

    _check("claim_type", CLAIM_TYPES)
    _check("water_source", WATER_SOURCES)
    _check("routing", ROUTES)
    for route in expected.get("acceptable_routing") or []:
        if route not in ROUTES:
            problems.append(f"{cid}: acceptable_routing contains unknown route {route!r}")
    facts = expected.get("facts")
    if not isinstance(facts, dict) or not facts:
        problems.append(f"{cid}: expected.facts must be a non-empty object")
    else:
        unknown = sorted(set(facts) - FACT_FIELDS)
        if unknown:
            problems.append(f"{cid}: expected.facts has unknown ClaimFacts fields {unknown}")
    for evidence in (case.get("session_state") or {}).get("received_evidence") or []:
        if not evidence.get("id") or not evidence.get("document_types"):
            problems.append(f"{cid}: each received_evidence item needs 'id' and 'document_types'")
    return problems


# Top-level keys the Vertex AI SDK's EvaluationDataset accepts (extra="forbid").
DATASET_TOP_LEVEL_KEYS = {"eval_cases", "candidate_name"}


def validate_dataset(dataset: dict[str, Any]) -> list[str]:
    cases = dataset.get("eval_cases")
    if not isinstance(cases, list) or not cases:
        return ["dataset must contain a non-empty 'eval_cases' list"]
    problems = [p for case in cases for p in validate_case(case)]
    unknown_top = sorted(set(dataset) - DATASET_TOP_LEVEL_KEYS)
    if unknown_top:
        problems.append(f"unknown top-level keys {unknown_top}: EvaluationDataset only allows {sorted(DATASET_TOP_LEVEL_KEYS)}")
    ids = [c.get("eval_case_id") for c in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        problems.append(f"duplicate eval_case_id values: {duplicates}")
    return problems


__all__ = [
    "PROMPT_TEMPLATE",
    "build_pipeline_prompt",
    "transcript_from_prompt",
    "reference_date",
    "STUB_BENCHMARKS",
    "stub_benchmark",
    "default_world",
    "CURRENT_WORLD",
    "CURRENT_TODAY",
    "weather_input",
    "frozen_rule_dates",
    "run_rules",
    "derive_expected",
    "validate_case",
    "validate_dataset",
    "DATASET_TOP_LEVEL_KEYS",
]
