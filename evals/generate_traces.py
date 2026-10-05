"""Run the ClaimDesk workflow on an eval dataset and write agents-cli traces.

WHERE THIS FITS
    The agents-cli eval loop has two steps::

        dataset (prompts + answer keys)
            --[ generate: run the agent ]-->  traces (what the agent did)
            --[ grade: score the traces ]-->  results (scores per metric)

    ``agents-cli eval generate`` normally does step 1 by starting
    ``adk api_server`` and calling it over HTTP. For THIS project that does
    not work yet (agents-cli 1.3.1 + ADK 2.9 Workflow), for three reasons:

    1. A Workflow emits "state only" events (function nodes return
       ``Event(output=..., state=...)`` without text). agents-cli 1.3.1 rejects
       any event without ``content`` ("Malformed agent event"), failing the
       whole case.
    2. It cannot seed session state, but ``check_fields`` needs
       ``state["received_evidence"]`` (server photo captures). Without it
       ADK raises "Missing value for parameter received_evidence".
    3. It needs an ``agents-cli-manifest.yaml`` at the project root.

    So this script does step 1 *in-process*: it runs the same ``Workflow``
    (``claimdesk.intake_pipeline.build_workflow()``, same models from
    ``claimdesk/settings.py``) with ADK's ``Runner``, and writes the trace file
    in exactly the format ``agents-cli eval grade`` reads. Step 2 is plain
    ``agents-cli eval grade`` (see ``docs/evals.md``).

WHAT IS CONTROLLED PER CASE
    * ``session_state`` - seeded into the ADK session (server evidence).
    * ``world`` - the three BigQuery look-ups (policy, benchmark, weather) are
      replaced with the case's stub answers so the eval measures the
      LLM + rules, not today's table contents. ``--live-data`` disables this.
    * ``reference_time`` - used in the prompt, AND as "today" for the rule
      code (``--no-freeze-time`` disables this; see evals/expectations.py).

    THIS CALLS GEMINI (two calls per case: extract + classify) - it needs
    ``GOOGLE_CLOUD_PROJECT`` and ``gcloud auth application-default login``.
    16 cases cost well under $0.05 with gemini-3.8-flash.

RUN IT (from the project root)
    ::

        GOOGLE_CLOUD_PROJECT=my-proj uv run --no-sync python -m evals.generate_traces \\
            --dataset evals/datasets/pipeline_core.json \\
            --dataset evals/datasets/pipeline_edge.json

    Output: ``evals/results/traces/traces_<timestamp>.json`` (git-ignored).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.adk.apps import App  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402

from claimdesk import intake_pipeline as pipeline  # noqa: E402
from claimdesk.contracts import BenchmarkResult, WeatherCheckResult  # noqa: E402
from claimdesk.observability import get_logger  # noqa: E402
from claimdesk.settings import get_settings  # noqa: E402
from evals.expectations import CURRENT_TODAY, CURRENT_WORLD, frozen_rule_dates, reference_date  # noqa: E402

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = PROJECT_ROOT / "evals" / "results" / "traces"
# Final ADK state keys copied into the trace as ``pipeline_state`` (graded by custom_metrics.py).
PIPELINE_STATE_KEYS = ("claim_facts", "field_check", "classification", "water_source", "evidence_decision", "checklist", "risk_gate", "packet")
PACKET_PREFIX = "# Flood Claim Intake Packet"


# ---------------------------------------------------------------------------
# 1. World stubs for the three BigQuery look-ups
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def world_stubs() -> Iterator[None]:
    """Replace the workflow's BigQuery functions with per-case stub answers.

    The workflow module imported ``review_policy_against_claim``,
    ``benchmark_for_state`` and ``check_weather`` by name, so we swap those
    module attributes (the same trick pytest's ``monkeypatch`` uses). The
    stubs read :data:`CURRENT_WORLD`; when no world is set they call the real
    function, so nothing changes outside an eval case.
    """

    originals = (pipeline.review_policy_against_claim, pipeline.benchmark_for_state, pipeline.check_weather)
    real_policy, real_benchmark, real_weather = originals

    def policy_stub(claim: Any) -> list[str]:
        world = CURRENT_WORLD.get()
        return real_policy(claim) if world is None else list(world.get("policy_issues") or [])

    def benchmark_stub(state: str | None) -> BenchmarkResult:
        world = CURRENT_WORLD.get()
        if world is None:
            return real_benchmark(state)
        return BenchmarkResult.model_validate(world.get("benchmark") or {"available": False, "note": "No benchmark in eval world"})

    def weather_stub(zip_code: str | None, loss_date: str | None, **kwargs: Any) -> WeatherCheckResult:
        world = CURRENT_WORLD.get()
        if world is None:
            return real_weather(zip_code, loss_date, **kwargs)
        weather = world.get("weather")
        return WeatherCheckResult.model_validate(weather) if weather else WeatherCheckResult(checked=False, note="No weather data in eval world")

    pipeline.review_policy_against_claim, pipeline.benchmark_for_state, pipeline.check_weather = policy_stub, benchmark_stub, weather_stub
    try:
        yield
    finally:
        pipeline.review_policy_against_claim, pipeline.benchmark_for_state, pipeline.check_weather = originals


# ---------------------------------------------------------------------------
# 2. ADK events -> agents-cli AgentEvent dicts
# ---------------------------------------------------------------------------
def _jsonable(value: Any) -> Any:
    """Make ADK state values (pydantic models, dates...) JSON-serializable."""

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=str))


def to_agent_event(event: Any) -> dict[str, Any] | None:
    """Convert one ADK ``Event`` to the ``AgentEvent`` shape used in traces.

    ``AgentEvent`` = ``{author, content?, event_time?, state_delta?}``. We keep
    state-only events too (with ``state_delta``) - they are how graders can
    see what each rule node decided.
    """

    if getattr(event, "partial", False):
        return None  # streaming fragments; the complete event follows
    out: dict[str, Any] = {"author": event.author or "unknown"}
    if event.content is not None and event.content.parts:
        out["content"] = event.content.model_dump(mode="json", exclude_none=True)
    delta = getattr(getattr(event, "actions", None), "state_delta", None)
    if delta:
        out["state_delta"] = _jsonable(dict(delta))
    if getattr(event, "timestamp", None):
        out["event_time"] = datetime.fromtimestamp(event.timestamp, tz=timezone.utc).isoformat()
    return out if ("content" in out or "state_delta" in out) else None


def final_response(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The packet Markdown event (fallback: last event with text)."""

    with_text = [e for e in events if any(p.get("text") for p in (e.get("content") or {}).get("parts", []))]
    for event in reversed(with_text):
        if event["content"]["parts"][0].get("text", "").startswith(PACKET_PREFIX):
            return event["content"]
    return with_text[-1]["content"] if with_text else None


def agents_map() -> dict[str, dict[str, Any]]:
    """Describe the agents for graders (``agent_data.agents``).

    LLM-judged metrics (e.g. hallucination) read the instructions to know what
    the agent was supposed to do.
    """

    extract, classify = pipeline._llm_nodes()
    return {
        pipeline.APP_NAME: {
            "agent_id": pipeline.APP_NAME,
            "agent_type": "Workflow",
            "description": "Flood claim intake workflow: extract facts, validate, classify, check data, apply rules, write packet.",
            "sub_agents": [extract.name, classify.name],
        },
        extract.name: {"agent_id": extract.name, "agent_type": "LlmAgent", "description": extract.description, "instruction": str(extract.instruction)},
        classify.name: {"agent_id": classify.name, "agent_type": "LlmAgent", "description": classify.description, "instruction": str(classify.instruction)},
    }


# ---------------------------------------------------------------------------
# 3. Running one case
# ---------------------------------------------------------------------------
async def run_case(
    case: dict[str, Any],
    *,
    workflow_factory: Callable[[], Any] = pipeline.build_workflow,
    use_world: bool = True,
    freeze_time: bool = True,
    agents: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the workflow for one eval case and return the case + its trace.

    Each case gets a fresh graph, Runner and in-memory session, exactly like a
    single ``run_intake_pipeline`` call.
    """

    # ContextVars set here are private to this asyncio task (cases can run concurrently).
    CURRENT_WORLD.set(case.get("world") if use_world else None)
    CURRENT_TODAY.set(reference_date(case) if freeze_time else None)

    from google.genai import types as genai_types

    service = InMemorySessionService()
    runner = Runner(app=App(name=pipeline.APP_NAME, root_agent=workflow_factory()), session_service=service)
    session_id = f"eval-{uuid.uuid4().hex[:10]}"
    state = {"received_evidence": [], **(case.get("session_state") or {})}
    await service.create_session(app_name=pipeline.APP_NAME, user_id="eval", session_id=session_id, state=state)
    message = genai_types.Content.model_validate(case["prompt"])

    trace = {k: v for k, v in case.items() if k not in ("agent_data", "responses")}
    events: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        async for event in runner.run_async(user_id="eval", session_id=session_id, new_message=message):
            if (converted := to_agent_event(event)) is not None:
                events.append(converted)
        session = await service.get_session(app_name=pipeline.APP_NAME, user_id="eval", session_id=session_id)
        final_state = dict(session.state) if session else {}
        trace["pipeline_state"] = {k: _jsonable(final_state[k]) for k in PIPELINE_STATE_KEYS if k in final_state}
    except Exception as exc:  # noqa: BLE001 - record the failure in the trace; grading shows it
        log.exception("Eval case failed", extra={"json_fields": {"case": case.get("eval_case_id")}})
        trace["generation_error"] = f"{type(exc).__name__}: {exc}"
    trace["generation_ms"] = int((time.monotonic() - started) * 1000)
    trace["agent_data"] = {"turns": [{"turn_index": 0, "turn_id": "turn_0", "events": events}]}
    if agents:
        trace["agent_data"]["agents"] = agents
    response = final_response(events)
    if response is not None:
        trace["responses"] = [{"response": response}]
    return trace


async def generate(
    cases: list[dict[str, Any]],
    *,
    concurrency: int = 4,
    workflow_factory: Callable[[], Any] = pipeline.build_workflow,
    use_world: bool = True,
    freeze_time: bool = True,
) -> dict[str, Any]:
    """Run every case (``concurrency`` at a time) and return a trace dataset."""

    agents = agents_map()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await run_case(case, workflow_factory=workflow_factory, use_world=use_world, freeze_time=freeze_time, agents=agents)

    with contextlib.ExitStack() as stack:
        if use_world:
            stack.enter_context(world_stubs())
        if freeze_time:
            stack.enter_context(frozen_rule_dates())
        traces = await asyncio.gather(*(asyncio.create_task(one(c)) for c in cases))
    return {"eval_cases": list(traces)}


def load_cases(paths: list[Path], ids: set[str] | None = None, tags: set[str] | None = None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in paths:
        cases += json.loads(Path(path).read_text(encoding="utf-8"))["eval_cases"]
    if ids:
        cases = [c for c in cases if c.get("eval_case_id") in ids]
    if tags:
        cases = [c for c in cases if tags & set(c.get("tags") or [])]
    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ClaimDesk workflow on eval datasets and write agents-cli traces (calls Gemini).")
    parser.add_argument("--dataset", type=Path, action="append", required=True, help="dataset JSON (repeatable)")
    parser.add_argument("--out", type=Path, help="output trace file (default: evals/results/traces/traces_<ts>.json)")
    parser.add_argument("--cases", default="", help="comma-separated eval_case_ids to run")
    parser.add_argument("--tags", default="", help="comma-separated tags; run cases having any of them")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--live-data", action="store_true", help="use the real BigQuery look-ups instead of each case's 'world' stub")
    parser.add_argument("--no-freeze-time", action="store_true", help="rules use the real date.today() instead of the case's reference_time")
    args = parser.parse_args(argv)

    if not get_settings().project_id:
        parser.error("GOOGLE_CLOUD_PROJECT is not set - generating traces calls Gemini on Vertex AI.")
    cases = load_cases(args.dataset, {c for c in args.cases.split(",") if c}, {t for t in args.tags.split(",") if t})
    if not cases:
        parser.error("no eval cases selected")
    log.info("Generating traces", extra={"json_fields": {"cases": len(cases), "model": get_settings().reasoning_model}})
    dataset = asyncio.run(generate(cases, concurrency=args.concurrency, use_world=not args.live_data, freeze_time=not args.no_freeze_time))

    out = args.out or DEFAULT_OUT_DIR / f"traces_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dataset, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    failed = [c["eval_case_id"] for c in dataset["eval_cases"] if c.get("generation_error")]
    print(f"Wrote {len(dataset['eval_cases'])} traces to {out}" + (f"  FAILED: {failed}" if failed else ""))
    print(f"Next: agents-cli eval grade --traces {out} --config evals/eval_config.yaml --output evals/results/grade")
    return 1 if len(failed) == len(dataset["eval_cases"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
