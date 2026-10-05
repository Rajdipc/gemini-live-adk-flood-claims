"""Offline end-to-end test of the ADK workflow graph.

No Google Cloud calls happen here:
  * Gemini is replaced by ``FakeLlm`` - a tiny ADK ``BaseLlm`` that returns
    canned JSON depending on which node is asking.
  * BigQuery look-ups are replaced with monkeypatched functions.

What this proves: the graph wiring (edges, fan-out/fan-in, state passing,
structured outputs) works and produces a valid packet. It does NOT judge
answer quality - that is what evals are for (see ``evals/``).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, AsyncGenerator
from zoneinfo import ZoneInfo

import pytest
from pydantic import Field, ValidationError
from google.adk.apps import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from claimdesk import intake_pipeline as pipeline
from claimdesk.contracts import BenchmarkResult, ClaimClassification, ClaimFacts, WeatherCheckResult
from claimdesk.data_access import bq_client, loss_benchmarks, policy_registry, weather_events
from claimdesk.errors import ConfigurationError, ModelCallError

FACTS = {
    "policyholder_name": "Ana Lopez",
    "policy_number": "FLD-TX-7Q2K9M",
    "contact_method": "ana@example.com",
    "date_of_loss": "2026-09-20",
    "loss_address_or_city": "Houston, TX",
    "loss_state": "TX",
    "loss_zip_code": "77002",
    "loss_description": "Bayou overflowed and 8 inches of water came into the living room.",
    "water_entry_description": "water came from the bayou under the front door",
    "estimated_loss_usd": 15000,
    "summary": "Bayou flooding entered the home. Nobody was hurt.",
    "evidence_records": [{"document_type": "damage_photo", "status": "available"}],
    "safety_facts": [{"category": "injury", "status": "absent", "description": "Nobody hurt"}],
}
CLASSIFICATION = {"claim_type": "home_flood", "severity": "medium", "severity_rationale": "moderate", "water_source": "surface_flood", "water_source_rationale": "bayou"}


class FakeLlm(BaseLlm):
    """Returns ClaimFacts JSON or ClaimClassification JSON based on the schema requested."""

    model: str = "fake"
    facts: dict[str, Any] = Field(default_factory=lambda: dict(FACTS))  # what extract_facts "answers"

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        schema = getattr(llm_request.config, "response_schema", None)
        payload = CLASSIFICATION if schema is ClaimClassification else self.facts
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part.from_text(text=json.dumps(payload))]))


def _build_runner(monkeypatch, *, facts: dict[str, Any] = FACTS, stub_data_access: bool = True):
    """Real workflow graph + fake Gemini. ``stub_data_access=False`` keeps the
    REAL policy/benchmark/weather functions (tests then fake BigQuery itself)."""

    fake = FakeLlm(facts=dict(facts))
    original = pipeline._llm_nodes  # keep a handle to the real builder

    def fake_nodes():
        # Build the real agents (same prompts/schemas) and swap only the model.
        extract, classify = original()
        return extract.model_copy(update={"model": fake}), classify.model_copy(update={"model": fake})

    monkeypatch.setattr(pipeline, "_llm_nodes", fake_nodes)
    if stub_data_access:
        monkeypatch.setattr(pipeline, "review_policy_against_claim", lambda claim: [])
        monkeypatch.setattr(
            pipeline,
            "benchmark_for_state",
            lambda state: BenchmarkResult(available=True, state="TX", sample_size=150000, damage_p50_usd=20000, damage_p90_usd=90000, damage_p95_usd=140000, report_lag_p95_days=60),
        )
        monkeypatch.setattr(pipeline, "check_weather", lambda z, d: WeatherCheckResult(checked=True, events_found=3, event_types=["Flash Flood"], note="3 NOAA events"))
    workflow = pipeline.build_workflow()
    service = InMemorySessionService()
    return Runner(app=App(name=pipeline.APP_NAME, root_agent=workflow), session_service=service), service


async def _run_to_state(runner, service, state: dict[str, Any] | None = None) -> dict[str, Any]:
    await service.create_session(app_name="claimdesk", user_id="u", session_id="s", state=state or {})
    message = types.Content(role="user", parts=[types.Part.from_text(text="CLAIMANT t1: The bayou flooded my house.")])
    async for _event in runner.run_async(user_id="u", session_id="s", new_message=message):
        pass
    return dict((await service.get_session(app_name="claimdesk", user_id="u", session_id="s")).state)


@pytest.fixture()
def offline_runner(monkeypatch):
    return _build_runner(monkeypatch)


async def test_workflow_produces_valid_packet(offline_runner):
    runner, service = offline_runner
    await service.create_session(app_name="claimdesk", user_id="u", session_id="s", state={"received_evidence": [{"id": "ev-1", "document_types": ["damage_photo"]}]})
    message = types.Content(role="user", parts=[types.Part.from_text(text="CLAIMANT t1: The bayou flooded my house.")])
    final_texts = []
    async for event in runner.run_async(user_id="u", session_id="s", new_message=message):
        if event.content and event.content.parts and event.content.parts[0].text and event.content.parts[0].text.startswith("# Flood Claim"):
            final_texts.append(event.content.parts[0].text)
    state = (await service.get_session(app_name="claimdesk", user_id="u", session_id="s")).state

    assert ClaimFacts.model_validate(state["claim_facts"]).loss_zip_code == "77002"
    assert state["water_source"]["nfip_flood_candidate"] is True
    assert state["risk_gate"]["weather"]["events_found"] == 3
    # Server capture registry marked the photo as received:
    photo = next(i for i in state["checklist"]["items"] if i["document_type"] == "damage_photo")
    assert photo["status"] == "received"
    assert state["packet"]["routing_decision"] == "needs_docs"  # water-line photo + inventory still missing
    assert final_texts, "packet markdown should be emitted as a content event"


# ---------------------------------------------------------------------------
# Routing edge cases through the whole graph
# ---------------------------------------------------------------------------
async def test_blank_policy_number_routes_to_needs_docs_not_policy_review(monkeypatch):
    # The REAL review_policy_against_claim runs; BigQuery must not be touched.
    def boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("no BigQuery query for a blank policy number")

    monkeypatch.setattr(policy_registry, "run_query", boom)
    runner, service = _build_runner(monkeypatch, facts={**FACTS, "policy_number": "not specified"})
    monkeypatch.setattr(pipeline, "benchmark_for_state", lambda state: BenchmarkResult(available=False))
    monkeypatch.setattr(pipeline, "check_weather", lambda z, d: WeatherCheckResult(checked=False))
    monkeypatch.setattr(pipeline, "review_policy_against_claim", policy_registry.review_policy_against_claim)

    state = await _run_to_state(runner, service, {"rule_date": "2026-09-29"})

    assert "policy_number" in state["field_check"]["missing_fields"]
    assert "POLICY-001" not in [f["rule_id"] for f in state["evidence_decision"]["findings"]]
    assert state["packet"]["routing_decision"] == "needs_docs"
    assert state["packet"]["next_question_for_claimant"].startswith("What is your flood policy number?")


async def test_offline_run_without_bigquery_still_produces_a_packet(monkeypatch):
    # No project / no credentials: every BigQuery call fails. The three
    # look-ups must degrade (not crash the graph) and a packet is written.
    def no_client():
        raise ConfigurationError("GOOGLE_CLOUD_PROJECT is not set")

    monkeypatch.setattr(bq_client, "get_bq_client", no_client)
    loss_benchmarks.clear_cache()
    weather_events._RESULT_CACHE.clear()
    runner, service = _build_runner(monkeypatch, stub_data_access=False)

    state = await _run_to_state(runner, service, {"rule_date": "2026-09-29"})

    assert state["risk_gate"]["benchmark"]["available"] is False
    assert state["risk_gate"]["weather"] is None  # check ran but was not "checked"
    # Same behaviour as before for a registry outage: a person verifies the policy.
    assert "Policy registry unavailable - verify the policy manually" in [f["message"] for f in state["evidence_decision"]["findings"]]
    assert state["packet"]["routing_decision"] == "policy_review"
    assert state["packet"]["markdown"].startswith("# Flood Claim")


# ---------------------------------------------------------------------------
# run_intake_pipeline: error hygiene and reference time
# ---------------------------------------------------------------------------
class _FailingRunner:
    """Stands in for the ADK Runner; lets a test inspect state, then fails."""

    def __init__(self, error: Exception, seen: dict[str, Any] | None = None) -> None:
        self.error, self.seen = error, seen if seen is not None else {}

    async def run_async(self, *, user_id: str, session_id: str, new_message):
        session = await pipeline._session_service.get_session(app_name=pipeline.APP_NAME, user_id=user_id, session_id=session_id)
        self.seen.update(state=dict(session.state), prompt=new_message.parts[0].text)
        raise self.error
        yield  # pragma: no cover - makes this an async generator


async def _sessions_left(user_id: str) -> int:
    listed = await pipeline._session_service.list_sessions(app_name=pipeline.APP_NAME, user_id=user_id)
    return len(listed.sessions)


async def test_model_call_error_does_not_leak_claimant_data(monkeypatch):
    # A pydantic ValidationError echoes the bad input value - here a name and
    # a phone number. That text must NOT end up in the ModelCallError message
    # (which callers log), but the original stays chained for debugging.
    try:
        ClaimFacts.model_validate({"water_depth_inches": "Ana Lopez 504-555-0199"})
    except ValidationError as exc:
        pii_error = exc
    monkeypatch.setattr(pipeline, "_current_runner", lambda: _FailingRunner(pii_error))

    with pytest.raises(ModelCallError) as info:
        await pipeline.run_intake_pipeline("CLAIMANT t1: hello", intake_id="pii-test")

    message = str(info.value)
    assert "ValidationError" in message and "water_depth_inches" in message
    assert "Ana Lopez" not in message and "504-555-0199" not in message
    assert info.value.__cause__ is pii_error
    assert await _sessions_left("pii-test") == 0


async def test_session_is_deleted_even_if_runner_lookup_fails(monkeypatch):
    def broken_runner():
        raise RuntimeError("could not build runner")

    monkeypatch.setattr(pipeline, "_current_runner", broken_runner)
    with pytest.raises(ModelCallError, match="RuntimeError"):
        await pipeline.run_intake_pipeline("CLAIMANT t1: hello", intake_id="cleanup-test")
    assert await _sessions_left("cleanup-test") == 0


async def test_default_reference_time_uses_the_desk_timezone(monkeypatch):
    # 22:30 in Chicago on Sep 20 is already Sep 21 in UTC (Cloud Run's clock).
    # The prompt and rule_date must say Sep 20.
    fixed = datetime(2026, 9, 20, 22, 30, tzinfo=ZoneInfo("America/Chicago"))
    monkeypatch.setattr(pipeline, "local_now", lambda: fixed)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(pipeline, "_current_runner", lambda: _FailingRunner(RuntimeError("stop"), seen))

    with pytest.raises(ModelCallError):
        await pipeline.run_intake_pipeline("CLAIMANT t1: it flooded yesterday", intake_id="tz-test")

    assert seen["state"]["rule_date"] == "2026-09-20"
    assert "Reference date and time: 2026-09-20T22:30-05:00" in seen["prompt"]
