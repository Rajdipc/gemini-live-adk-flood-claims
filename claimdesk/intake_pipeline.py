"""The ADK claim workflow - the "brain" behind the voice agent.

WHAT IS ADK?
    The Agent Development Kit is Google's open-source framework for building
    agents. ADK 2.x offers a graph-based **Workflow** API (the recommended
    replacement for the older ``SequentialAgent``):

    * **Nodes** do work. A node can be an ``LlmAgent`` (calls Gemini) or a plain
      Python function (our deterministic business rules).
    * **Edges** say what runs next. A tuple of nodes on the right-hand side
      runs them **in parallel** (fan-out); a ``JoinNode`` waits for all of them
      (fan-in).
    * **State** is a shared dictionary for one run. A function node receives
      state values simply by naming a parameter after the key
      (``def f(claim_facts: dict)`` gets ``state["claim_facts"]``), and writes
      state by returning ``Event(output=..., state={...})``.

THE GRAPH

    START (conversation text)
      |
      v
    extract_facts      LLM  gemini-3.8-flash  -> state.claim_facts
      |
    check_fields       rules                  -> state.field_check
      |
    classify_claim     LLM  gemini-3.8-flash  -> state.classification
      |
    decide_water       rules                  -> state.water_source
      |
      +--> fetch_policy_issues   (BigQuery: policy_registry)       \\
      +--> fetch_benchmark       (BigQuery: loss_benchmarks)        } in parallel
      +--> fetch_weather         (BigQuery: NOAA public data)      /
      |
    gather_context     JoinNode (waits for all three)
      |
    apply_rules        rules -> evidence_decision, checklist, risk_gate
      |
    publish_packet     rules -> state.packet (+ Markdown shown in ADK Web)

    Running the three BigQuery look-ups in parallel means the claimant waits
    for the slowest one (~1 s) instead of all three added together.

HOW THE WEB APP USES IT
    ``run_intake_pipeline()`` runs the whole graph for a snapshot of the
    conversation and returns every output as plain dicts. The live voice agent
    calls it in the background (``refresh_intake_packet`` tool) and after every
    claimant turn.

TRY IT ON ITS OWN
    ``uv run adk web .`` from the project root opens ADK's developer UI; pick
    ``claimdesk`` and paste a claim story. You'll see each node's output.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date, datetime
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import FunctionNode, JoinNode, RetryConfig, Workflow
from google.genai import types as genai_types

from .contracts import (
    ClaimClassification,
    ClaimFacts,
    DocumentChecklist,
    EvidenceDecision,
    FieldCheck,
    IntakePacket,
    RiskGate,
    WaterSourceDecision,
)
from .data_access.loss_benchmarks import benchmark_for_state
from .data_access.policy_registry import review_policy_against_claim
from .data_access.weather_events import check_weather
from . import knowledge
from .errors import ModelCallError
from .observability import get_logger, redact
from .rules._helpers import as_model, is_blank
from .rules.evidence_rules import apply_evidence_rules, build_checklist, merge_server_evidence
from .rules.packet_writer import write_packet
from .rules.required_fields import check_required_fields
from .rules.risk_signals import score_risk_signals
from .rules.water_source import decide_water_source
from .settings import PROJECT_ROOT, ensure_vertex_ai_env, get_settings, local_now

log = get_logger(__name__)
ensure_vertex_ai_env()  # make ADK's Gemini client use Vertex AI + ADC (no API key)

APP_NAME = "claimdesk"  # must match the package folder name (ADK tooling relies on it)
PROMPTS_DIR = PROJECT_ROOT / "claimdesk" / "prompts"

# Low temperature = more consistent extraction. This tunes *how* the model
# samples; it does not change *which* model is used.
_LLM_CONFIG = genai_types.GenerateContentConfig(temperature=0.1)

# BigQuery look-ups get ONE quick retry (max_attempts=2 = first try + 1 retry);
# a transient network blip should not cost the claimant a policy check. The
# data-access helpers already turn most failures into a graceful "unavailable"
# result, so this retry mostly covers node-level surprises such as a timeout.
_BQ_RETRY = RetryConfig(max_attempts=2, initial_delay=0.5, max_delay=2.0)


def _prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# LLM nodes (the only two places Gemini is called in the pipeline)
# ---------------------------------------------------------------------------
def _llm_nodes() -> tuple[LlmAgent, LlmAgent]:
    model = get_settings().reasoning_model  # gemini-3.8-flash (unchanged)
    # Each instruction = task prompt (claimdesk/prompts/*.md) + domain
    # knowledge and worked examples from the nfip-flood-intake Agent Skill
    # (skills/nfip-flood-intake, loaded by claimdesk/knowledge.py). The skill
    # text is brace-free, so ADK's {placeholder} templating is unaffected.
    extract = LlmAgent(
        name="extract_facts",
        model=model,
        description="Turns the role-labeled conversation into structured ClaimFacts.",
        instruction=_prompt("fact_extractor.md") + knowledge.for_extractor(),
        output_schema=ClaimFacts,  # Gemini must answer with exactly this JSON shape
        output_key="claim_facts",  # ...and ADK stores it in state["claim_facts"]
        generate_content_config=_LLM_CONFIG,
    )
    classify = LlmAgent(
        name="classify_claim",
        model=model,
        description="Classifies claim type, water source and severity.",
        # {claim_facts} and {field_check} are filled in from state by ADK.
        instruction=_prompt("claim_classifier.md") + knowledge.for_classifier(),
        output_schema=ClaimClassification,
        output_key="classification",
        generate_content_config=_LLM_CONFIG,
    )
    return extract, classify


# ---------------------------------------------------------------------------
# Function nodes. Parameters named like state keys are filled from state.
# Nodes that call BigQuery are async and use asyncio.to_thread(): BigQuery's
# Python client is *blocking*, and the web server must keep streaming audio
# to the claimant while a query runs.
# ---------------------------------------------------------------------------
def _rule_day(rule_date: str | None) -> date | None:
    """``rule_date`` (ISO string in state) -> date. ``None`` = use today.

    Pinning the date makes a run reproducible: replaying an old conversation
    in evals gives the same timing signals it gave on the day of the call.
    """

    try:
        return date.fromisoformat(rule_date) if rule_date else None
    except ValueError:
        return None


def check_fields(claim_facts: dict, received_evidence: list | None = None, rule_date: str | None = None) -> Event:
    """Merge server-captured evidence into the facts, then check required fields.

    ``received_evidence`` and ``rule_date`` have defaults so the workflow also
    runs from ``adk web`` / ``adk api_server``, where nobody seeds that state.
    """

    merged = merge_server_evidence(claim_facts, received_evidence or [])
    field_check = check_required_fields(merged, today=_rule_day(rule_date))
    return Event(output=field_check, state={"claim_facts": merged, "field_check": field_check})


def decide_water(claim_facts: dict, classification: dict) -> Event:
    decision = decide_water_source(claim_facts, classification)
    return Event(output=decision, state={"water_source": decision})


async def fetch_policy_issues(claim_facts: dict) -> list[str]:
    """Compare the claim with the BigQuery policy record (term, name, state)."""

    return await asyncio.to_thread(review_policy_against_claim, as_model(ClaimFacts, claim_facts))


async def fetch_benchmark(claim_facts: dict) -> dict:
    """Real NFIP thresholds for the claim's state."""

    result = await asyncio.to_thread(benchmark_for_state, claim_facts.get("loss_state"))
    return result.model_dump()


async def fetch_weather(claim_facts: dict, water_source: dict) -> dict | None:
    """NOAA check - only for likely flood claims with a ZIP and an exact date."""

    if not water_source.get("nfip_flood_candidate") or is_blank(claim_facts.get("loss_zip_code")) or is_blank(claim_facts.get("date_of_loss")):
        return {"checked": False, "note": "Weather check needs a flood claim with ZIP code and exact date."}
    result = await asyncio.to_thread(check_weather, claim_facts["loss_zip_code"], claim_facts["date_of_loss"])
    return result.model_dump()


def apply_rules(node_input: dict, claim_facts: dict, field_check: dict, classification: dict, water_source: dict, rule_date: str | None = None) -> Event:
    """Run the deterministic rules. ``node_input`` is the JoinNode's output:
    ``{"fetch_policy_issues": [...], "fetch_benchmark": {...}, "fetch_weather": {...}}``."""

    evidence = apply_evidence_rules(claim_facts, field_check, classification, water_source, policy_issues=node_input.get("fetch_policy_issues") or [])
    checklist = build_checklist(claim_facts, classification, water_source)
    weather = node_input.get("fetch_weather")
    risk = score_risk_signals(
        claim_facts,
        field_check,
        classification,
        water_source,
        evidence,
        benchmark=node_input.get("fetch_benchmark") or {"available": False},
        weather=weather if weather and weather.get("checked") else None,
        today=_rule_day(rule_date),
    )
    return Event(output=risk, state={"evidence_decision": evidence, "checklist": checklist, "risk_gate": risk})


def publish_packet(claim_facts: dict, field_check: dict, classification: dict, water_source: dict, evidence_decision: dict, checklist: dict, risk_gate: dict):
    """Write the packet. Yields a *content* event (shown in ADK Web and used as
    the "final response" by evals) and then the output/state event."""

    packet = write_packet(claim_facts, field_check, classification, water_source, evidence_decision, checklist, risk_gate)
    yield Event(content=genai_types.Content(role="model", parts=[genai_types.Part.from_text(text=packet["markdown"])]))
    yield Event(output=packet, state={"packet": packet, "final_markdown": packet["markdown"]})


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------
def build_workflow() -> Workflow:
    extract, classify = _llm_nodes()
    policy = FunctionNode(func=fetch_policy_issues, name="fetch_policy_issues", retry_config=_BQ_RETRY, timeout=15.0)
    benchmark = FunctionNode(func=fetch_benchmark, name="fetch_benchmark", retry_config=_BQ_RETRY, timeout=15.0)
    weather = FunctionNode(func=fetch_weather, name="fetch_weather", retry_config=_BQ_RETRY, timeout=20.0)
    gather = JoinNode(name="gather_context")
    return Workflow(
        name=APP_NAME,
        description="Flood claim intake: extract facts, validate, classify, check real-world data, apply rules, write packet.",
        edges=[
            ("START", extract),
            (extract, check_fields),
            (check_fields, classify),
            (classify, decide_water),
            (decide_water, (policy, benchmark, weather)),  # fan-out: run in parallel
            ((policy, benchmark, weather), gather),  # fan-in: wait for all three
            (gather, apply_rules),
            (apply_rules, publish_packet),
        ],
    )


root_agent = build_workflow()
app = App(name=APP_NAME, root_agent=root_agent)

# One session service for the whole process. The runner is cached per
# (reasoning_model, skill_enabled) so toggling CLAIMDESK_USE_SKILL in tests or
# evals takes effect without restarting the process.
_session_service = InMemorySessionService()
_runners: dict[tuple[str, bool], Runner] = {(get_settings().reasoning_model, knowledge.skill_enabled()): Runner(app=app, session_service=_session_service)}


def _current_runner() -> Runner:
    key = (get_settings().reasoning_model, knowledge.skill_enabled())
    runner = _runners.get(key)
    if runner is None:
        runner = Runner(app=App(name=APP_NAME, root_agent=build_workflow()), session_service=_session_service)
        _runners[key] = runner
    return runner


_OUTPUT_MODELS = {
    "claim_facts": ClaimFacts,
    "field_check": FieldCheck,
    "classification": ClaimClassification,
    "water_source": WaterSourceDecision,
    "evidence_decision": EvidenceDecision,
    "checklist": DocumentChecklist,
    "risk_gate": RiskGate,
    "packet": IntakePacket,
}


def _clean(model: Any, value: Any) -> dict[str, Any]:
    """Validate once more so the web app only ever sees well-formed data."""

    parsed = model.model_validate_json(value) if isinstance(value, str) else model.model_validate(value)
    return parsed.model_dump()


def _safe_hint(exc: BaseException) -> str:
    """A PII-free hint about *why* the pipeline failed, for logs.

    Pydantic errors: only the error type and field path of each problem
    (e.g. ``missing at claim_facts.date_of_loss``), never the input values.
    Anything else: the first line of the message, with e-mails/phones masked
    by ``redact()`` and cut to 80 characters.
    """

    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            parts = [f"{e.get('type', 'error')} at {'.'.join(str(p) for p in e.get('loc', ())) or 'root'}" for e in errors()[:3]]
            return "; ".join(parts) or "validation error"
        except Exception:  # not a pydantic error after all
            pass
    first_line = str(exc).splitlines()[0] if str(exc) else ""
    return redact(first_line, limit=80) or "no details"


async def run_intake_pipeline(
    conversation: str,
    *,
    intake_id: str | None = None,
    received_evidence: list[dict[str, Any]] | None = None,
    reference_time: datetime | None = None,
) -> dict[str, Any]:
    """Run the workflow on a conversation snapshot and return all outputs.

    Raises :class:`ModelCallError` if Gemini/ADK fails or returns unusable
    output; the caller keeps the previous packet and the call continues.
    """

    text = (conversation or "").strip()
    if not text:
        raise ValueError("conversation is empty")
    user_id = intake_id or "anonymous"
    session_id = f"run-{uuid.uuid4().hex[:12]}"
    # Default "now" is in the desk's timezone (CLAIMDESK_TIMEZONE), not the
    # server's: Cloud Run is UTC, which would shift "yesterday" by a day on
    # US evenings. See settings.local_now().
    reference = reference_time or local_now()
    now = reference.isoformat(timespec="minutes")
    # The same reference date drives the prompt AND the rules (timing checks),
    # so a replayed conversation is judged as of the day it happened.
    initial_state = {"received_evidence": received_evidence or [], "rule_date": reference.date().isoformat()}
    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part.from_text(text=f"Reference date and time: {now}\n\nConversation (source of truth; do not invent facts):\n{text}")],
    )
    started = time.monotonic()
    try:
        # Session creation and runner lookup are inside the try so that ANY
        # failure still reaches the ``finally`` below and the session is
        # deleted (InMemorySessionService would otherwise keep it forever).
        await _session_service.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id, state=initial_state)
        runner = _current_runner()
        async for _event in runner.run_async(user_id=user_id, session_id=session_id, new_message=message):
            pass
        session = await _session_service.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        state = dict(session.state) if session else {}
        missing = [key for key in _OUTPUT_MODELS if key not in state]
        if missing:
            raise ModelCallError(f"Workflow finished without outputs: {missing}")
        result = {key: _clean(model, state[key]) for key, model in _OUTPUT_MODELS.items()}
    except ModelCallError:
        raise
    except Exception as exc:  # Gemini/ADK raise many types; wrap once, keep the cause
        # PRIVACY: str(exc) can contain claimant data - a pydantic
        # ValidationError echoes the offending ``input_value`` (a name, a
        # phone number...). The message is logged by callers, so it carries
        # only the exception type plus a short redacted hint. The full
        # original stays attached as __cause__ for local debugging.
        raise ModelCallError(f"Intake pipeline failed: {type(exc).__name__} ({_safe_hint(exc)})") from exc
    finally:
        try:
            await _session_service.delete_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        except Exception:  # never mask the real error; the session may not exist yet
            log.debug("Pipeline session cleanup skipped", exc_info=True)

    log.info(
        "Intake pipeline completed",
        extra={
            "json_fields": {
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "claim_type": result["packet"]["claim_type"],
                "routing": result["packet"]["routing_decision"],
                "missing_fields": len(result["field_check"]["missing_fields"]),
            }
        },
    )
    result["final_markdown"] = result["packet"]["markdown"]
    return result


__all__ = ["APP_NAME", "app", "root_agent", "build_workflow", "run_intake_pipeline"]
