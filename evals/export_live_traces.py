"""Export real voice-agent conversations from BigQuery as multi-turn eval cases.

WHY (the "live" eval layer)
    Pipeline evals (generate_traces.py) test the claim *workflow* on curated
    transcripts. But the thing claimants actually talk to is the Gemini Live
    voice agent (webapp/): it asks questions, looks at the camera, calls tools.
    Its quality can only be judged on real conversations. The web app records
    every event of every call in BigQuery (``webapp/trace_logger.py``)::

        conversation_traces(intake_id, event_time, seq, event_type, role, text,
                            tool_name, tool_args_json, tool_result_json, service_revision)

    This script reads a date range of those rows and converts each intake
    (one call) into one agents-cli / Vertex AI eval case with ``agent_data``:
    the whole multi-turn trajectory. ``agents-cli eval grade`` (or
    ``evals/run_vertex_eval.py``) then scores the conversations with
    multi-turn metrics - see ``evals/eval_config_live.yaml``.

HOW ROWS BECOME A TRAJECTORY
    ==================  ==========================================================
    event_type          becomes
    ==================  ==========================================================
    claimant_turn       ``user`` event with the text; STARTS A NEW TURN
    agent_turn          agent event (author ``claimdesk_live``) with the text
    tool_call           agent event with a ``function_call`` part
    tool_result         agent event with a ``function_response`` part
    camera_observation  ``user`` event "[CAMERA OBSERVATION] ..." (what the app saw)
    system              ``user`` event "[APP NOTICE] ..." (call started, camera off...)
    pipeline_result     not an event: the LAST one is saved as ``live_outcome``
    ==================  ==========================================================

    The final agent utterance becomes ``responses[0]`` and the voice agent's
    instruction + tool declarations (``webapp/voice_tools.py``) go into
    ``agent_data.agents`` so judges know what the agent was *supposed* to do.

PRIVACY
    Trace text is already masked for e-mails/phones by the web app, but it is
    still claimant data. Output goes to ``evals/results/live_traces/`` (git
    ignored). ``--redact`` additionally masks names/policy numbers crudely.
    Never commit exported conversations; copy a *sanitized* version into a
    dataset when you turn one into a regression case (docs/evals.md).

COST
    One parameterized query, capped by ``maximum_bytes_billed`` (bq_client
    default). Filter by date: the table should be partitioned on
    ``DATE(event_time)`` so only those days are scanned.

RUN IT
    ::

        GOOGLE_CLOUD_PROJECT=my-proj uv run --no-sync python -m evals.export_live_traces \\
            --start-date 2025-07-01 --end-date 2025-07-07 --max-intakes 50
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claimdesk.observability import get_logger, redact  # noqa: E402
from claimdesk.settings import get_settings  # noqa: E402

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = PROJECT_ROOT / "evals" / "results" / "live_traces"
LIVE_AGENT_ID = "claimdesk_live"
TRACE_TABLE = "conversation_traces"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_\-]+$")
_POLICY = re.compile(r"\bFLD-[A-Z]{2}-[A-Z0-9]{4,}\b", re.IGNORECASE)
FULL_TEXT = 1_000_000  # observability.redact() truncates to `limit` chars; we want everything


# ---------------------------------------------------------------------------
# 1. Query
# ---------------------------------------------------------------------------
def trace_query(project: str, dataset: str, table: str = TRACE_TABLE, *, filter_intakes: bool = False, filter_revision: bool = False) -> str:
    """Parameterized SQL for a date range of trace rows.

    ``@start_date`` / ``@end_date`` are DATE parameters (inclusive). The
    optional filters are added only when used, so BigQuery gets a simple query.
    ``@max_intakes`` limits the number of *conversations*, not rows.
    """

    for name in (project, dataset, table):
        if not _IDENTIFIER.match(name or ""):
            raise ValueError(f"Invalid BigQuery identifier: {name!r}")
    fqn = f"`{project}.{dataset}.{table}`"

    def conditions(prefix: str) -> str:
        where = [f"DATE({prefix}event_time) BETWEEN @start_date AND @end_date"]
        if filter_intakes:
            where.append(f"{prefix}intake_id IN UNNEST(@intake_ids)")
        if filter_revision:
            where.append(f"{prefix}service_revision = @service_revision")
        return "\n  AND ".join(where)

    return (
        "WITH picked AS (\n"
        f"  SELECT intake_id, MIN(event_time) AS started FROM {fqn}\n"
        f"  WHERE {conditions('')}\n"
        "  GROUP BY intake_id\n"
        "  ORDER BY started DESC\n"
        "  LIMIT @max_intakes\n"
        ")\n"
        "SELECT t.intake_id, t.event_time, t.seq, t.event_type, t.role, t.text,\n"
        "       t.tool_name, t.tool_args_json, t.tool_result_json, t.service_revision\n"
        f"FROM {fqn} AS t JOIN picked USING (intake_id)\n"
        f"WHERE {conditions('t.')}\n"
        "ORDER BY t.intake_id, t.seq"
    )


def load_rows(
    project: str,
    dataset: str,
    *,
    start_date: date,
    end_date: date,
    intake_ids: list[str] | None = None,
    revision: str | None = None,
    max_intakes: int = 100,
    table: str = TRACE_TABLE,
) -> list[dict[str, Any]]:
    from claimdesk.data_access.bq_client import run_query  # lazy: tests never import BigQuery

    sql = trace_query(project, dataset, table, filter_intakes=bool(intake_ids), filter_revision=bool(revision))
    params: dict[str, Any] = {"start_date": start_date, "end_date": end_date, "max_intakes": int(max_intakes)}
    if revision:
        params["service_revision"] = revision
    return run_query(sql, params, label="export_live_traces", array_params={"intake_ids": intake_ids} if intake_ids else None, timeout_s=120.0)


# ---------------------------------------------------------------------------
# 2. Rows -> eval cases (pure functions, unit-tested offline)
# ---------------------------------------------------------------------------
def _parse_json(text: Any) -> Any:
    """Trace JSON columns may be truncated by the logger; keep raw text then."""

    if text in (None, ""):
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {"raw": str(text)}


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _clean_text(text: Any, do_redact: bool) -> str:
    value = str(text or "")
    if do_redact:
        value = _POLICY.sub("[POLICY]", redact(value, limit=FULL_TEXT))
    return value


def row_to_event(row: dict[str, Any], *, do_redact: bool = False, agent_id: str = LIVE_AGENT_ID) -> dict[str, Any] | None:
    """One trace row -> one ``AgentEvent`` dict (or None for rows we skip)."""

    kind = row.get("event_type")
    event: dict[str, Any] = {}
    if kind == "claimant_turn":
        event = {"author": "user", "content": {"role": "user", "parts": [{"text": _clean_text(row.get("text"), do_redact)}]}}
    elif kind == "agent_turn":
        event = {"author": agent_id, "content": {"role": "model", "parts": [{"text": _clean_text(row.get("text"), do_redact)}]}}
    elif kind == "camera_observation":
        event = {"author": "user", "content": {"role": "user", "parts": [{"text": "[CAMERA OBSERVATION] " + _clean_text(row.get("text"), do_redact)}]}}
    elif kind == "system":
        event = {"author": "user", "content": {"role": "user", "parts": [{"text": "[APP NOTICE] " + _clean_text(row.get("text"), do_redact)}]}}
    elif kind == "tool_call":
        args = _parse_json(row.get("tool_args_json"))
        call = {"name": row.get("tool_name") or "unknown_tool", "args": args if isinstance(args, dict) else {"value": args}}
        event = {"author": agent_id, "content": {"role": "model", "parts": [{"function_call": call}]}}
    elif kind == "tool_result":
        result = _parse_json(row.get("tool_result_json"))
        response = {"name": row.get("tool_name") or "unknown_tool", "response": result if isinstance(result, dict) else {"result": result}}
        event = {"author": agent_id, "content": {"role": "user", "parts": [{"function_response": response}]}}
    else:
        return None  # pipeline_result (handled separately) or unknown types
    if (time := _iso(row.get("event_time"))) is not None:
        event["event_time"] = time
    return event


def live_agents_map() -> dict[str, dict[str, Any]]:
    """The voice agent's instruction + tools (from webapp/voice_tools.py).

    Imported lazily and optional: the webapp package may not be importable in
    every environment; graders then just lack the instruction context.
    """

    try:
        from webapp.voice_tools import DESK_INSTRUCTION, tool_declarations

        tools = [t.model_dump(mode="json", exclude_none=True) for t in tool_declarations()]
        instruction = DESK_INSTRUCTION
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load webapp.voice_tools; exporting without agent config", extra={"json_fields": {"error": str(exc)}})
        tools, instruction = [], None
    config: dict[str, Any] = {"agent_id": LIVE_AGENT_ID, "agent_type": "GeminiLiveAgent", "description": "ClaimDesk voice intake agent (Gemini Live)."}
    if instruction:
        config["instruction"] = instruction
    if tools:
        config["tools"] = tools
    return {LIVE_AGENT_ID: config}


def rows_to_cases(
    rows: Iterable[dict[str, Any]],
    *,
    do_redact: bool = False,
    min_claimant_turns: int = 1,
    agents: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Group trace rows by intake and build one multi-turn eval case per intake."""

    by_intake: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for row in rows:
        by_intake.setdefault(str(row["intake_id"]), []).append(row)

    cases: list[dict[str, Any]] = []
    for intake_id, intake_rows in by_intake.items():
        intake_rows.sort(key=lambda r: (int(r.get("seq") or 0), str(r.get("event_time") or "")))
        turns: list[list[dict[str, Any]]] = [[]]
        claimant_turns = 0
        outcome = None
        for row in intake_rows:
            if row.get("event_type") == "pipeline_result":
                outcome = _parse_json(row.get("tool_result_json"))
                continue
            event = row_to_event(row, do_redact=do_redact)
            if event is None:
                continue
            if row.get("event_type") == "claimant_turn":
                # Every claimant utterance starts a new turn. Whatever came
                # before the first one (app notice, agent greeting) is turn 0.
                claimant_turns += 1
                if turns[-1]:
                    turns.append([])
            turns[-1].append(event)
        if claimant_turns < min_claimant_turns:
            continue
        turns = [t for t in turns if t]
        agent_texts = [e["content"] for t in turns for e in t if e["author"] == LIVE_AGENT_ID and e["content"]["parts"][0].get("text")]
        case: dict[str, Any] = {
            "eval_case_id": f"live_{intake_id}",
            "tags": ["live"],
            "source": f"{TRACE_TABLE} intake {intake_id}",
            "intake_id": intake_id,
            "service_revisions": sorted({str(r.get("service_revision")) for r in intake_rows if r.get("service_revision")}),
            "agent_data": {"turns": [{"turn_index": i, "turn_id": f"turn_{i}", "events": t} for i, t in enumerate(turns)]},
        }
        if agents:
            case["agent_data"]["agents"] = agents
        if agent_texts:
            case["responses"] = [{"response": agent_texts[-1]}]
        if outcome is not None:
            case["live_outcome"] = outcome
        cases.append(case)
    return cases


# ---------------------------------------------------------------------------
# 3. CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export conversation_traces rows as multi-turn eval cases.")
    parser.add_argument("--start-date", type=date.fromisoformat, required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--end-date", type=date.fromisoformat, help="YYYY-MM-DD (inclusive, default = start date)")
    parser.add_argument("--intake-id", action="append", default=[], help="only these intakes (repeatable)")
    parser.add_argument("--revision", help="only one Cloud Run revision (compare releases)")
    parser.add_argument("--max-intakes", type=int, default=100)
    parser.add_argument("--min-claimant-turns", type=int, default=2, help="skip calls where the claimant barely spoke")
    parser.add_argument("--redact", action="store_true", help="also mask policy numbers / phone numbers in text")
    parser.add_argument("--project", help="default: GOOGLE_CLOUD_PROJECT")
    parser.add_argument("--dataset", help="default: settings.bq_dataset")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    settings = get_settings()
    project = args.project or settings.project_id
    if not project:
        parser.error("set GOOGLE_CLOUD_PROJECT or --project")
    end = args.end_date or args.start_date
    if end < args.start_date:
        parser.error("--end-date is before --start-date")
    rows = load_rows(
        project, args.dataset or settings.bq_dataset,
        start_date=args.start_date, end_date=end,
        intake_ids=args.intake_id or None, revision=args.revision, max_intakes=args.max_intakes,
    )
    cases = rows_to_cases(rows, do_redact=args.redact, min_claimant_turns=args.min_claimant_turns, agents=live_agents_map())
    out = args.out or DEFAULT_OUT_DIR / f"live_{args.start_date}_{end}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"eval_cases": cases}, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(f"Exported {len(cases)} conversations ({len(rows)} rows) to {out}")
    print(f"Next: agents-cli eval grade --traces {out} --config evals/eval_config_live.yaml --output evals/results/grade_live")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
