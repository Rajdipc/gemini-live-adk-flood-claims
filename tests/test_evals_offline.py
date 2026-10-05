"""Offline tests for the eval suite in ``evals/`` (no GCP, no model calls).

What these tests protect:

* **Datasets** load, follow the agents-cli / Vertex AI ``EvaluationDataset``
  schema, and their answer keys (``expected``) agree with the REAL rule code
  (re-derived from each case's golden LLM outputs).
* **Custom metrics** are standard-library only (agents-cli runs them in its own
  virtualenv) and score fake traces correctly through every fallback path.
* **Configs** parse, every code-metric shim compiles, and LLM-judge templates
  use supported placeholders without pinning a judge model.
* **build_eval_cases --offline** produces valid, deterministic cases.
* **generate_traces** produces a trace file agents-cli can grade: we run the
  real workflow graph with a "golden" fake LLM and check every metric = 1.0.
* **export_live_traces / run_vertex_eval** pure conversion functions.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
import yaml
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from claimdesk import intake_pipeline as pipeline  # noqa: E402
from claimdesk.contracts import ClaimClassification  # noqa: E402
from evals import build_eval_cases as builder  # noqa: E402
from evals import custom_metrics as cm  # noqa: E402
from evals import export_live_traces as exporter  # noqa: E402
from evals import generate_traces as gt  # noqa: E402
from evals import run_vertex_eval as rve  # noqa: E402
from evals.expectations import (  # noqa: E402
    CURRENT_WORLD,
    ROUTES,
    WATER_SOURCES,
    derive_expected,
    validate_dataset,
)

EVALS = PROJECT_ROOT / "evals"
DATASETS = [EVALS / "datasets" / "pipeline_core.json", EVALS / "datasets" / "pipeline_edge.json"]
SEED_FILE = EVALS / "seeds" / "sample_eval_seed_claims.json"
LABEL_METRICS = ["fact_extraction_accuracy", "routing_correct", "water_source_correct", "claim_type_correct"]

# From `agents-cli eval metric list` (agents-cli 1.3.1), lower-cased as used in configs.
BUILTIN_METRICS = {
    "final_response_match", "final_response_quality", "final_response_reference_free", "general_quality",
    "grounding", "hallucination", "instruction_following", "multi_turn_general_quality", "multi_turn_task_success",
    "multi_turn_text_quality", "multi_turn_tool_use_quality", "multi_turn_trajectory_quality", "safety",
    "text_quality", "tool_use_quality",
}
# Field sets of the vertexai 2.0.0 trace models, which use extra="forbid".
AGENT_EVENT_KEYS = {"author", "content", "event_time", "state_delta", "active_tools"}
TURN_KEYS = {"turn_index", "turn_id", "events"}
AGENT_DATA_KEYS = {"agents", "turns"}
AGENT_CONFIG_KEYS = {"agent_id", "agent_type", "description", "instruction", "tools", "sub_agents"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _all_cases() -> list[dict]:
    return [c for path in DATASETS for c in _load(path)["eval_cases"]]


def _instance(trace_case: dict) -> dict:
    """What agents-cli passes to evaluate(): the case minus responses, plus `response`."""

    instance = {k: v for k, v in trace_case.items() if k != "responses"}
    instance["response"] = (trace_case.get("responses") or [{}])[0].get("response")
    return instance


def _assert_trace_schema(dataset: dict) -> None:
    assert set(dataset) == {"eval_cases"}
    for case in dataset["eval_cases"]:
        agent_data = case["agent_data"]
        assert set(agent_data) <= AGENT_DATA_KEYS
        for config in (agent_data.get("agents") or {}).values():
            assert set(config) <= AGENT_CONFIG_KEYS
        for turn in agent_data["turns"]:
            assert set(turn) <= TURN_KEYS
            for event in turn["events"]:
                assert set(event) <= AGENT_EVENT_KEYS, event.keys()
                assert event.get("author")
        json.dumps(case)  # must be JSON-serializable


# ---------------------------------------------------------------------------
# 1. Datasets
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", DATASETS, ids=lambda p: p.name)
def test_dataset_is_a_valid_evaluation_dataset(path):
    dataset = _load(path)
    assert set(dataset) == {"eval_cases"}, "EvaluationDataset forbids other top-level keys"
    assert validate_dataset(dataset) == []


def test_case_ids_are_unique_across_datasets():
    ids = [c["eval_case_id"] for c in _all_cases()]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", _all_cases(), ids=lambda c: c["eval_case_id"])
def test_expected_labels_match_the_rules(case):
    derived = derive_expected(case)
    expected = case["expected"]
    for label in ("claim_type", "routing"):
        assert expected[label] == derived[label], label
    wanted = expected["water_source"] if isinstance(expected["water_source"], list) else [expected["water_source"]]
    assert derived["water_source"] in wanted


@pytest.mark.parametrize("case", _all_cases(), ids=lambda c: c["eval_case_id"])
def test_expected_facts_agree_with_golden_extraction(case):
    golden = case["golden_outputs"]["claim_facts"]
    for field, value in case["expected"]["facts"].items():
        assert cm.fact_matches(field, value, golden.get(field)), f"{field}: {value!r} vs golden {golden.get(field)!r}"


def test_datasets_cover_every_route_and_water_source():
    cases = _all_cases()
    routes = {c["expected"]["routing"] for c in cases}
    sources = {s for c in cases for s in (c["expected"]["water_source"] if isinstance(c["expected"]["water_source"], list) else [c["expected"]["water_source"]])}
    assert routes == ROUTES
    assert sources == WATER_SOURCES


def test_validate_dataset_reports_problems():
    bad = {"name": "x", "eval_cases": [{"eval_case_id": "a", "prompt": {"role": "user", "parts": [{"text": "hi"}]}, "reference_time": "nope"}]}
    problems = validate_dataset(bad)
    assert any("top-level" in p for p in problems)
    assert any("CLAIMANT t" in p for p in problems)
    assert any("reference_time" in p for p in problems)
    assert any("missing expected" in p for p in problems)


# ---------------------------------------------------------------------------
# 2. Custom metrics
# ---------------------------------------------------------------------------
def test_custom_metrics_imports_only_the_standard_library():
    tree = ast.parse((EVALS / "custom_metrics.py").read_text(encoding="utf-8"))
    modules = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    modules |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module and node.level == 0}
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}, modules - set(sys.stdlib_module_names)


EXPECTED = {
    "claim_type": "home_flood",
    "water_source": "surface_flood",
    "routing": "needs_docs",
    "facts": {"policy_number": "FLD-TX-7Q2K9M", "date_of_loss": "2025-06-02", "loss_zip_code": "77096", "estimated_loss_usd": 20000, "contact_method": None},
}
GOOD_FACTS = {"policy_number": "fld-tx 7q2k9m", "date_of_loss": "June 2, 2025", "loss_zip_code": "77096", "estimated_loss_usd": 20500, "contact_method": "not specified"}
PACKET_MD = "# Flood Claim Intake Packet\n\n**Claim type:** Home Flood\n**Water source:** surface flood\n**Routing decision:** Needs Docs\n"


def test_metrics_read_pipeline_state_first():
    instance = {
        "expected": EXPECTED,
        "pipeline_state": {
            "claim_facts": GOOD_FACTS,
            "water_source": {"water_source": "surface_flood"},
            "packet": {"claim_type": "home_flood", "routing_decision": "needs_docs"},
        },
    }
    assert [getattr(cm, m)(instance)["score"] for m in LABEL_METRICS] == [1.0, 1.0, 1.0, 1.0]


def test_metrics_fall_back_to_state_delta_events():
    events = [
        {"author": "claimdesk", "state_delta": {"claim_facts": json.dumps(GOOD_FACTS)}},
        {"author": "claimdesk", "state_delta": {"water_source": {"water_source": "seepage"}}},
        {"author": "claimdesk", "state_delta": {"packet": {"claim_type": "internal_water", "routing_decision": "human_triage"}}},
    ]
    instance = {"expected": EXPECTED, "agent_data": {"turns": [{"events": events}]}}
    assert cm.fact_extraction_accuracy(instance)["score"] == 1.0
    assert cm.routing_correct(instance)["score"] == 0.0
    assert cm.water_source_correct(instance)["score"] == 0.0
    assert "human_triage" in cm.routing_correct(instance)["explanation"]


def test_metrics_fall_back_to_llm_json_and_packet_markdown():
    """Traces from plain HTTP runs only have LLM node text and the final Markdown."""

    events = [{"author": "extract_facts", "content": {"role": "model", "parts": [{"text": json.dumps(GOOD_FACTS)}]}}]
    instance = {"expected": EXPECTED, "agent_data": {"turns": [{"events": events}]}, "response": {"role": "model", "parts": [{"text": PACKET_MD}]}}
    assert [getattr(cm, m)(instance)["score"] for m in LABEL_METRICS] == [1.0, 1.0, 1.0, 1.0]


def test_fact_extraction_counts_partial_and_invented_facts():
    facts = dict(GOOD_FACTS, loss_zip_code="77002", contact_method="713-555-0100")  # wrong ZIP + invented contact
    result = cm.fact_extraction_accuracy({"expected": EXPECTED, "pipeline_state": {"claim_facts": facts}})
    assert result["score"] == 0.6
    assert "loss_zip_code" in result["explanation"] and "contact_method" in result["explanation"]


def test_fact_extraction_without_facts_scores_zero():
    assert cm.fact_extraction_accuracy({"expected": EXPECTED})["score"] == 0.0


@pytest.mark.parametrize(
    "field,expected,actual,ok",
    [
        ("date_of_loss", "2025-07-14", "07/14/2025", True),
        ("date_of_loss", "2025-07-14", "July 14th, 2025", True),
        ("date_of_loss", "2025-07-14", "2025-07-15", False),
        ("estimated_loss_usd", 23400, "$23,000", True),
        ("estimated_loss_usd", 23400, 20000, False),
        ("water_depth_inches", 6, 6.5, True),
        ("loss_zip_code", "80302", "80302", True),
        ("loss_zip_code", None, "not specified", True),
        ("loss_zip_code", None, "80302", False),
        ("loss_address_or_city", "Boulder", "2240 Walnut Street, Boulder, CO", True),
        ("loss_state", "co", "CO", True),
        ("policyholder_name", ["Maria Gonzalez", "Maria Gonzales"], "maria gonzales", True),
    ],
)
def test_fact_matches(field, expected, actual, ok):
    assert cm.fact_matches(field, expected, actual) is ok


def test_routing_accepts_listed_alternatives():
    instance = {"expected": dict(EXPECTED, acceptable_routing=["policy_review"]), "pipeline_state": {"packet": {"routing_decision": "policy_review"}}}
    assert cm.routing_correct(instance)["score"] == 1.0


def test_metrics_require_an_answer_key():
    with pytest.raises(ValueError):
        cm.routing_correct({"pipeline_state": {}})


def test_packet_refreshed_counts_tool_calls():
    call = {"author": "claimdesk_live", "content": {"role": "model", "parts": [{"function_call": {"name": "refresh_intake_packet", "args": {}}}]}}
    assert cm.packet_refreshed({"agent_data": {"turns": [{"events": [call]}]}})["score"] == 1.0
    assert cm.packet_refreshed({"agent_data": {"turns": []}})["score"] == 0.0


def test_parse_packet_markdown():
    assert cm.parse_packet_markdown(PACKET_MD) == {"claim_type": "home_flood", "water_source": "surface_flood", "routing_decision": "needs_docs"}


# ---------------------------------------------------------------------------
# 3. Eval configs
# ---------------------------------------------------------------------------
CONFIGS = {"eval_config.yaml": "pipeline", "eval_config_live.yaml": "live"}


@pytest.mark.parametrize("name", CONFIGS)
def test_config_metrics_are_defined(name):
    data = yaml.safe_load((EVALS / name).read_text(encoding="utf-8"))
    custom = {m["name"] for m in data["custom_metrics"]}
    for metric in data["metrics_to_run"]:
        assert metric in custom or metric in BUILTIN_METRICS, metric
    assert not custom & BUILTIN_METRICS, "custom names must not shadow built-ins"


@pytest.mark.parametrize("name", CONFIGS)
def test_config_code_shims_compile_and_llm_judges_use_default_model(name, monkeypatch):
    monkeypatch.setenv("CLAIMDESK_EVALS_DIR", str(EVALS))
    specs = rve.load_metric_specs(EVALS / name)
    for spec in specs:
        if spec.kind == "code":
            evaluate = rve.compile_code_metric(spec.payload, spec.name)
            assert callable(evaluate) and evaluate.__name__ == spec.name
        elif spec.kind == "llm":
            template = spec.payload["prompt_template"]
            assert "judge_model" not in spec.payload, "keep the evaluation service's default judge"
            wanted = "{agent_data}" if CONFIGS[name] == "live" else "{response}"
            assert wanted in template
            assert '"score"' in template


def test_code_shim_works_from_the_project_root(monkeypatch):
    """Without CLAIMDESK_EVALS_DIR the shim finds evals/ relative to the working directory."""

    monkeypatch.delenv("CLAIMDESK_EVALS_DIR", raising=False)
    monkeypatch.chdir(PROJECT_ROOT)
    source = next(s.payload for s in rve.load_metric_specs(EVALS / "eval_config.yaml") if s.name == "routing_correct")
    namespace: dict = {}
    exec(compile(source, "<shim>", "exec"), namespace)
    assert namespace["evaluate"]({"expected": EXPECTED, "pipeline_state": {"packet": {"routing_decision": "needs_docs"}}})["score"] == 1.0


# ---------------------------------------------------------------------------
# 4. build_eval_cases --offline
# ---------------------------------------------------------------------------
def test_build_eval_cases_offline_writes_valid_deterministic_cases(tmp_path, capsys):
    out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
    assert builder.main(["--offline", str(SEED_FILE), "--out", str(out1)]) == 0
    assert builder.main(["--offline", str(SEED_FILE), "--out", str(out2)]) == 0
    assert out1.read_text() == out2.read_text(), "same seeds must give byte-identical cases"

    dataset = _load(out1)
    assert validate_dataset(dataset) == []
    rows = builder.load_offline_rows(SEED_FILE)
    assert len(dataset["eval_cases"]) == len(rows) - 1  # the earth-movement row is skipped
    for case in dataset["eval_cases"]:
        derived = derive_expected(case)
        assert (case["expected"]["routing"], case["expected"]["water_source"]) == (derived["routing"], derived["water_source"])
        transcript = case["prompt"]["parts"][0]["text"]
        assert "CLAIMANT t2:" in transcript and "AGENT t1:" in transcript
        assert case["expected"]["facts"]["policy_number"] in transcript


def test_builder_maps_fema_labels_to_water_sources():
    plan = builder.plan_water
    assert plan({"cause_of_damage": "Tidal water overflow"})[0] == "surface_flood"
    assert plan({"cause_of_damage": "Accumulation of rainfall or snowmelt", "non_payment_reason": "Backup drains"})[0] == "sewer_or_drain_backup"
    assert plan({"cause_of_damage": "Accumulation of rainfall or snowmelt", "non_payment_reason": "Seepage (not a flood)"})[0] == "seepage"
    assert plan({"cause_of_damage": "Accumulation of rainfall or snowmelt", "non_payment_reason": "Not insured, wind damage"})[0] == "roof_or_wind_driven_rain"
    assert plan({"cause_of_damage": "Earth movement, landslide, land subsidence, sinkholes, etc."}) is None


def test_builder_policy_match_columns_drive_policy_review_and_location():
    rows = {r.get("eval_case_id"): r for r in builder.load_offline_rows(SEED_FILE)}
    lapsed = builder.build_case(rows["nfip-sample-la-lapsed"], 1)
    assert lapsed["world"]["policy_issues"] == [builder.ISSUE_OUTSIDE_TERM]
    assert lapsed["expected"]["routing"] == "policy_review"

    moved = builder.build_case(rows["nfip-sample-tx-state-term"], 2, twists=False)
    assert moved["expected"]["facts"]["loss_address_or_city"] == "Baytown"  # the POLICY's city
    assert moved["expected"]["facts"]["loss_zip_code"] == "77520"
    assert moved["world"]["policy_issues"] == []


def test_builder_injury_twist_escalates_and_complete_twist_seeds_server_evidence():
    row = builder.load_offline_rows(SEED_FILE)[0]
    injury = builder.build_case(row, 1)
    complete = builder.build_case(row, 1, twists=False)
    if builder.pick_twist(row) == "injury":
        assert injury["expected"]["routing"] == "emergency_escalation"
    assert complete["session_state"]["received_evidence"], "app captures are server evidence"
    assert complete["expected"]["routing"] == "ready_for_adjuster"


def test_builder_seed_query_is_parameterized():
    sql = builder.seed_query("my-proj", "claimdesk", "eval_seed_claims")
    assert "@state" in sql and "@limit" in sql and "@clean_only" in sql
    with pytest.raises(ValueError):
        builder.seed_query("my-proj`; DROP TABLE x; --", "claimdesk", "eval_seed_claims")


# ---------------------------------------------------------------------------
# 5. generate_traces with a golden fake LLM (real graph + rules, no Gemini)
# ---------------------------------------------------------------------------
class GoldenLlm(BaseLlm):
    """Answers each LLM node with the running case's golden output.

    The running case is identified through ``CURRENT_WORLD`` (set per case by
    generate_traces), so concurrent cases don't mix up.
    """

    model: str = "golden-fake"
    cases: list = []

    async def generate_content_async(self, llm_request, stream=False):
        world = CURRENT_WORLD.get()
        case = next(c for c in self.cases if c["world"] is world)
        key = "classification" if getattr(llm_request.config, "response_schema", None) is ClaimClassification else "claim_facts"
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part.from_text(text=json.dumps(case["golden_outputs"][key]))]))


def _golden_factory(cases):
    fake = GoldenLlm(cases=cases)
    original = pipeline._llm_nodes

    def factory():
        extract, classify = original()
        nodes = (extract.model_copy(update={"model": fake}), classify.model_copy(update={"model": fake}))
        pipeline._llm_nodes = lambda: nodes
        try:
            return pipeline.build_workflow()
        finally:
            pipeline._llm_nodes = original

    return factory


async def test_generate_traces_with_golden_llm_scores_perfectly():
    cases = gt.load_cases(DATASETS)
    real_functions = (pipeline.review_policy_against_claim, pipeline.benchmark_for_state, pipeline.check_weather)
    dataset = await gt.generate(cases, workflow_factory=_golden_factory(cases))

    assert (pipeline.review_policy_against_claim, pipeline.benchmark_for_state, pipeline.check_weather) == real_functions, "stubs restored"
    _assert_trace_schema(dataset)
    assert len(dataset["eval_cases"]) == len(cases)
    for trace in dataset["eval_cases"]:
        assert "generation_error" not in trace, trace.get("generation_error")
        assert trace["responses"][0]["response"]["parts"][0]["text"].startswith(gt.PACKET_PREFIX)
        assert set(gt.PIPELINE_STATE_KEYS) <= set(trace["pipeline_state"])
        scores = {m: getattr(cm, m)(_instance(trace))["score"] for m in LABEL_METRICS}
        assert scores == dict.fromkeys(LABEL_METRICS, 1.0), (trace["eval_case_id"], scores)
        signals = [s["rule_id"] for s in trace["pipeline_state"]["risk_gate"]["signals"]]
        assert signals == trace["expected"]["signals"] if "signals" in trace["expected"] else True


async def test_generate_traces_freezes_rule_dates():
    """core_01 happened in 2025; with frozen dates it is NOT a late report."""

    case = next(c for c in _all_cases() if c["eval_case_id"] == "core_01_surface_flood_ready_co")
    frozen = await gt.generate([case], workflow_factory=_golden_factory([case]))
    signals = [s["rule_id"] for s in frozen["eval_cases"][0]["pipeline_state"]["risk_gate"]["signals"]]
    assert "TIMING-002" not in signals


async def test_generate_traces_records_failures_per_case():
    case = dict(_all_cases()[0], session_state={"received_evidence": ["not-a-dict"]})  # malformed item -> AttributeError in merge_server_evidence
    dataset = await gt.generate([case], workflow_factory=_golden_factory([case]))
    assert "generation_error" in dataset["eval_cases"][0]


async def test_workflow_runs_without_seeded_received_evidence():
    """Regression: adk web / api_server start with EMPTY state. check_fields and
    apply_rules must use their defaults (received_evidence=None, rule_date=None)."""

    from google.adk.apps import App
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    case = _all_cases()[0]
    service = InMemorySessionService()
    runner = Runner(app=App(name=pipeline.APP_NAME, root_agent=_golden_factory([case])()), session_service=service)
    await service.create_session(app_name=pipeline.APP_NAME, user_id="u", session_id="s", state={})
    token = CURRENT_WORLD.set(case["world"])
    try:
        with gt.world_stubs():  # keep the test offline: stubbed BigQuery look-ups
            async for _ in runner.run_async(user_id="u", session_id="s", new_message=types.Content.model_validate(case["prompt"])):
                pass
    finally:
        CURRENT_WORLD.reset(token)
    state = (await service.get_session(app_name=pipeline.APP_NAME, user_id="u", session_id="s")).state
    assert "packet" in state


# ---------------------------------------------------------------------------
# 6. export_live_traces (pure conversion)
# ---------------------------------------------------------------------------
def _row(seq, event_type, text=None, **kw):
    return {"intake_id": "in-1", "event_time": f"2025-07-01T10:00:{seq:02d}+00:00", "seq": seq, "event_type": event_type, "text": text, "service_revision": "claimdesk-00007", **kw}


LIVE_ROWS = [
    _row(1, "system", "live call started", role="system"),
    _row(2, "agent_turn", "Flood claim desk. Is everyone safe?", role="agent"),
    _row(3, "claimant_turn", "Yes. The bayou flooded my house, policy FLD-TX-7Q2K9M.", role="claimant"),
    _row(4, "tool_call", tool_name="find_policy", tool_args_json='{"policy_number": "FLD-TX-7Q2K9M"}', role="agent"),
    _row(5, "tool_result", tool_name="find_policy", tool_result_json='{"found": true', role="system"),  # truncated JSON
    _row(6, "agent_turn", "Thanks, I found your policy. Can you show me the water line?", role="agent"),
    _row(7, "claimant_turn", "Sure, here it is.", role="claimant"),
    _row(8, "camera_observation", "Water line about 8 inches on drywall", role="system"),
    _row(9, "tool_call", tool_name="refresh_intake_packet", tool_args_json="{}", role="agent"),
    _row(10, "pipeline_result", tool_result_json='{"routing_decision": "needs_docs", "claim_type": "home_flood"}', role="system"),
    _row(11, "agent_turn", "I can see the water line. An adjuster will review your claim.", role="agent"),
    {"intake_id": "in-2", "event_time": "2025-07-01T11:00:00+00:00", "seq": 1, "event_type": "agent_turn", "text": "Hello?", "service_revision": "r"},
]


def test_rows_to_cases_builds_multi_turn_trajectories():
    cases = exporter.rows_to_cases(LIVE_ROWS, min_claimant_turns=1, agents={"claimdesk_live": {"agent_id": "claimdesk_live"}})
    assert [c["eval_case_id"] for c in cases] == ["live_in-1"]  # in-2 has no claimant turn
    case = cases[0]
    _assert_trace_schema({"eval_cases": cases})
    turns = case["agent_data"]["turns"]
    assert len(turns) == 3  # greeting turn + one turn per claimant utterance
    assert turns[1]["events"][0]["author"] == "user"
    call = turns[1]["events"][1]["content"]["parts"][0]["function_call"]
    assert call == {"name": "find_policy", "args": {"policy_number": "FLD-TX-7Q2K9M"}}
    response = turns[1]["events"][2]["content"]["parts"][0]["function_response"]
    assert response["response"] == {"raw": '{"found": true'}
    assert turns[2]["events"][1]["content"]["parts"][0]["text"].startswith("[CAMERA OBSERVATION]")
    assert turns[0]["events"][0]["content"]["parts"][0]["text"] == "[APP NOTICE] live call started"
    assert case["live_outcome"]["routing_decision"] == "needs_docs"
    assert case["responses"][0]["response"]["parts"][0]["text"].startswith("I can see the water line")
    assert case["service_revisions"] == ["claimdesk-00007"]
    assert cm.packet_refreshed(_instance(case))["score"] == 1.0


def test_rows_to_cases_min_turns_and_redaction():
    assert exporter.rows_to_cases(LIVE_ROWS, min_claimant_turns=3) == []
    case = exporter.rows_to_cases(LIVE_ROWS, do_redact=True)[0]
    assert "FLD-TX-7Q2K9M" not in case["agent_data"]["turns"][1]["events"][0]["content"]["parts"][0]["text"]


def test_trace_query_is_parameterized():
    sql = exporter.trace_query("my-proj", "claimdesk", filter_intakes=True, filter_revision=True)
    for param in ("@start_date", "@end_date", "@max_intakes", "@intake_ids", "@service_revision"):
        assert param in sql
    assert "DATE(t.event_time)" in sql
    with pytest.raises(ValueError):
        exporter.trace_query("bad project", "claimdesk")


def test_live_agents_map_uses_the_voice_agent_config():
    config = exporter.live_agents_map()[exporter.LIVE_AGENT_ID]
    assert set(config) <= AGENT_CONFIG_KEYS
    json.dumps(config)


# ---------------------------------------------------------------------------
# 7. run_vertex_eval (pure parts)
# ---------------------------------------------------------------------------
def test_flatten_results_maps_case_index_to_case_fields():
    cases = [{"eval_case_id": "a", "tags": ["x"]}, {"eval_case_id": "live_in-1", "intake_id": "in-1", "service_revisions": ["r7"]}]
    result = {
        "eval_case_results": [
            {"eval_case_index": 1, "response_candidate_results": [{"metric_results": {
                "routing_correct": {"metric_name": "routing_correct", "score": 1.0, "explanation": "ok"},
                "hallucination": {"metric_name": "hallucination", "error_message": "quota"},
            }}]},
            {"eval_case_index": 0, "response_candidate_results": [{"metric_results": {"routing_correct": {"score": 0.0}}}]},
        ]
    }
    rows = rve.flatten_results(result, cases, run_id="r1", created_at="2025-07-01T00:00:00+00:00", layer="live")
    assert len(rows) == 3
    assert {r["eval_case_id"] for r in rows} == {"a", "live_in-1"}
    live = next(r for r in rows if r["metric_name"] == "hallucination")
    assert live["score"] is None and live["error_message"] == "quota" and live["service_revision"] == "r7"
    assert {n for n, _, _ in rve.RESULTS_SCHEMA} == set(rows[0])
    summary = rve.summarize(rows)
    assert summary["routing_correct"] == {"cases": 2, "errors": 0, "mean_score": 0.5}
    assert summary["hallucination"]["errors"] == 1


def test_load_metric_specs_kinds_and_subset():
    specs = {s.name: s.kind for s in rve.load_metric_specs(EVALS / "eval_config.yaml")}
    assert specs["routing_correct"] == "code"
    assert specs["no_coverage_promise"] == "llm"
    assert specs["hallucination"] == "builtin"
    subset = rve.load_metric_specs(EVALS / "eval_config.yaml", ["routing_correct"])
    assert [s.name for s in subset] == ["routing_correct"]
