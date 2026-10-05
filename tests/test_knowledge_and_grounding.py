"""Offline tests for the accuracy add-ons: the Agent Skill and Vertex AI Search grounding.

No Google Cloud calls are made: the Vertex AI Search HTTP session is faked.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import pytest

from claimdesk import knowledge
from claimdesk.data_access import guidance_search as gs
from claimdesk.errors import ConfigurationError, DataAccessError

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings_env(monkeypatch):
    """Override settings via env vars for one test (``get_settings`` is cached)."""

    from claimdesk.settings import get_settings

    def apply(**env: object) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Agent Skill
# ---------------------------------------------------------------------------
def test_skill_loads_with_adk_loader():
    from google.adk.skills import load_skill_from_dir

    skill = load_skill_from_dir(knowledge.SKILL_DIR)
    assert skill.name == "nfip-flood-intake"
    expected = set(knowledge.EXTRACTOR_REFERENCES + knowledge.CLASSIFIER_REFERENCES + knowledge.VOICE_REFERENCES)
    assert expected <= set(skill.resources.references)
    assert knowledge.skill_loaded()


def test_skill_files_contain_no_curly_braces():
    # ADK treats {name} as a state placeholder and the voice prompt uses str.format.
    for path in knowledge.SKILL_DIR.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert "{" not in text and "}" not in text, f"curly brace in {path}"


def test_knowledge_bundles_are_brace_free_and_targeted():
    extractor, classifier, voice = knowledge.for_extractor(), knowledge.for_classifier(), knowledge.for_voice()
    for text in (extractor, classifier, voice):
        assert text and "{" not in text and "}" not in text
        assert "<!--" not in text  # editor comments are stripped
    assert "Worked examples: fact extraction" in extractor
    assert "Worked examples: classification" in classifier
    assert "Worked examples" not in voice  # the voice model gets knowledge, not few-shot JSON-ish examples
    assert "Safety escalation" in voice and "Approved language" in voice


def test_pipeline_instructions_keep_adk_placeholders():
    from claimdesk.intake_pipeline import _llm_nodes

    extract, classify = _llm_nodes()
    assert "Worked examples: fact extraction" in extract.instruction
    # The classifier's own placeholders must survive, and no new ones may appear.
    placeholders = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", classify.instruction))
    assert placeholders == {"claim_facts", "field_check"}


def test_skill_can_be_switched_off_for_ab_evals(monkeypatch):
    monkeypatch.setenv("CLAIMDESK_USE_SKILL", "false")
    assert knowledge.for_extractor() == "" and knowledge.for_voice() == ""
    monkeypatch.setenv("CLAIMDESK_USE_SKILL", "true")
    assert knowledge.for_extractor()


def test_missing_skill_degrades_to_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(knowledge, "SKILL_DIR", tmp_path / "nfip-flood-intake")
    knowledge._load.cache_clear()
    try:
        assert knowledge.for_voice() == "" and not knowledge.skill_loaded()
    finally:
        monkeypatch.undo()
        knowledge._load.cache_clear()


# ---------------------------------------------------------------------------
# Vertex AI Search grounding
# ---------------------------------------------------------------------------
SAMPLE_RESPONSE = {
    "results": [
        {
            "document": {
                "id": "doc1",
                "derivedStructData": {
                    "title": "NFIP Claims Handbook",
                    "link": "gs://bucket/grounding/fema/claims_handbook.pdf",
                    "extractive_answers": [{"content": "You must send a signed proof of loss within 60 days.", "pageNumber": "14"}],
                },
            }
        },
        {
            "document": {
                "id": "doc2",
                "derivedStructData": {
                    "title": "SFIP Dwelling Form",
                    "snippets": [{"snippet": "Coverage in a <b>basement</b> is limited&nbsp;to", "snippet_status": "SUCCESS"}],
                },
            }
        },
        {"document": {"id": "doc3", "derivedStructData": {"title": "Empty"}}},
    ]
}


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None, text: str = ""):
        self.status_code, self._payload, self.text = status, payload or {}, text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response, self.calls = response, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def grounding_env(settings_env):
    gs.clear_cache()
    yield lambda **extra: settings_env(
        CLAIMDESK_ENABLE_GUIDANCE_SEARCH="true", CLAIMDESK_SEARCH_ENGINE_ID="fema-nfip-engine", **extra
    )
    gs.clear_cache()


def test_settings_flag_requires_engine_id(settings_env):
    from claimdesk.settings import get_settings

    settings_env(CLAIMDESK_ENABLE_GUIDANCE_SEARCH="true", CLAIMDESK_SEARCH_ENGINE_ID="")
    assert get_settings().enable_guidance_search is False
    settings_env(CLAIMDESK_ENABLE_GUIDANCE_SEARCH="true", CLAIMDESK_SEARCH_ENGINE_ID="e1")
    s = get_settings()
    assert s.enable_guidance_search is True and s.search_location == "us"


def test_endpoint_and_url(grounding_env):
    grounding_env()
    from claimdesk.settings import get_settings

    assert gs.endpoint_host("us") == "us-discoveryengine.googleapis.com"
    assert gs.endpoint_host("global") == "discoveryengine.googleapis.com"
    url = gs.search_url(get_settings())
    assert url.startswith("https://us-discoveryengine.googleapis.com/v1/projects/")
    assert "/locations/us/collections/default_collection/engines/fema-nfip-engine/servingConfigs/default_search:search" in url


def test_parse_prefers_extractive_and_strips_markup():
    result = gs.parse_search_response("proof of loss", SAMPLE_RESPONSE)
    assert result.found and len(result.passages) == 2
    assert result.passages[0].page == "14" and "60 days" in result.passages[0].text
    assert result.passages[1].text == "Coverage in a basement is limited to"
    assert not gs.parse_search_response("x", {}).found


def test_single_document_returns_answer_and_segments():
    # Only one PDF indexed (e.g. just the SFIP form): the short answer can be
    # off-topic while a segment hits the right section, so keep both.
    payload = {
        "results": [
            {
                "document": {
                    "id": "sfip",
                    "derivedStructData": {
                        "title": "sfip_dwelling_form",
                        "extractive_answers": [{"content": "Artwork, photographs, collectibles", "pageNumber": "4"}],
                        "extractive_segments": [
                            {"content": "Drywall for walls and ceilings in a basement", "pageNumber": "4"},
                            {"content": "Artwork, photographs, collectibles", "pageNumber": "4"},  # duplicate
                            {"content": "III. PROPERTY INSURED", "pageNumber": "3"},
                        ],
                    },
                }
            }
        ]
    }
    result = gs.parse_search_response("basement", payload)
    assert [p.text for p in result.passages] == [
        "Artwork, photographs, collectibles",
        "Drywall for walls and ceilings in a basement",
        "III. PROPERTY INSURED",
    ]
    assert gs.build_request_body("q")["contentSearchSpec"]["extractiveContentSpec"]["maxExtractiveSegmentCount"] == 2


def test_passages_round_robin_across_documents():
    def doc(name: str, n: int) -> dict:
        segs = [{"content": f"{name}-{i}", "pageNumber": str(i)} for i in range(n)]
        return {"document": {"id": name, "derivedStructData": {"title": name, "extractive_segments": segs}}}

    result = gs.parse_search_response("q", {"results": [doc("A", 3), doc("B", 3)]})
    # Best passage of each document first, so one long PDF cannot crowd out the other.
    assert [p.text for p in result.passages] == ["A-0", "B-0", "A-1"]


def test_tool_result_tells_model_to_judge_relevance():
    how = gs.GuidanceResult(found=True, question="q").as_tool_result()["how_to_use"]
    assert "directly answer" in how and "without citing FEMA" in how and "Never promise" in how


def test_search_success_and_cache(grounding_env):
    grounding_env()
    session = FakeSession(FakeResponse(200, SAMPLE_RESPONSE))
    first = gs.search_flood_guidance("  proof of loss  deadline ", session=session)
    second = gs.search_flood_guidance("Proof of loss deadline", session=session)
    assert first.found and second is first and len(session.calls) == 1
    _, body, timeout = session.calls[0]
    assert body["query"] == "proof of loss deadline" and timeout == gs._TIMEOUT_SECONDS
    assert first.as_tool_result()["passages"][0]["title"] == "NFIP Claims Handbook"


def test_search_http_error_and_network_error(grounding_env):
    grounding_env()
    with pytest.raises(DataAccessError) as info:
        gs.search_flood_guidance("q1", session=FakeSession(FakeResponse(403, text="PERMISSION_DENIED")))
    assert info.value.retryable is False
    with pytest.raises(DataAccessError) as info:
        gs.search_flood_guidance("q2", session=FakeSession(TimeoutError("slow")))
    assert info.value.retryable is True


def test_search_disabled_raises_configuration_error(settings_env):
    settings_env(CLAIMDESK_ENABLE_GUIDANCE_SEARCH="false")
    with pytest.raises(ConfigurationError):
        gs.search_flood_guidance("anything", session=FakeSession(FakeResponse(200, {})))


def test_voice_tools_offer_guidance_only_when_enabled(settings_env):
    from claimdesk.settings import get_settings
    from webapp import voice_tools

    settings_env(CLAIMDESK_ENABLE_GUIDANCE_SEARCH="false")
    names = [d.name for d in voice_tools.build_live_config(get_settings()).tools[0].function_declarations]
    assert voice_tools.GUIDANCE_TOOL not in names and "lookup_flood_guidance" not in voice_tools.SYSTEM_INSTRUCTION
    settings_env(CLAIMDESK_ENABLE_GUIDANCE_SEARCH="true", CLAIMDESK_SEARCH_ENGINE_ID="e1")
    names = [d.name for d in voice_tools.build_live_config(get_settings()).tools[0].function_declarations]
    assert names[-1] == "lookup_flood_guidance"
    assert voice_tools.active_tool_names(get_settings())[-1] == "lookup_flood_guidance"
    assert "lookup_flood_guidance" in voice_tools.SYSTEM_INSTRUCTION
    assert voice_tools.tool_headline("lookup_flood_guidance", {}, {"found": True, "passages": [1, 2]}) == "FEMA guidance: 2 passages found"


def test_guidance_handler_degrades_on_failure(monkeypatch):
    from webapp import tool_handlers

    class Tracer:
        def __init__(self):
            self.events = []

        def record(self, *args, **kwargs):
            self.events.append((args, kwargs))

    class Registry:
        tracer = Tracer()

    class Intake:
        intake_id = "i-1"

    def boom(question):
        raise DataAccessError("down", retryable=True)

    monkeypatch.setattr(tool_handlers, "search_flood_guidance", boom)
    result = asyncio.run(tool_handlers.lookup_flood_guidance(Intake(), Registry(), {"question": "basement"}))
    assert result["found"] is False and "adjuster" in result["message"]
    assert Registry.tracer.events


@pytest.mark.parametrize("script", ["deploy/03b_vertex_ai_search.sh", "deploy/99_destroy.sh"])
def test_new_scripts_have_valid_bash_syntax(script):
    path = ROOT / script
    assert path.exists(), script
    subprocess.run(["bash", "-n", str(path)], check=True)
