"""Custom *code* metrics for ClaimDesk evals (deterministic Python graders).

WHAT IS A "CODE METRIC"?
    An eval run produces *traces*: for each test case, what the agent was
    asked and everything it produced. A metric turns one trace into a score.
    Some things are best judged by an LLM ("is this summary clear?"), but
    many are just facts we can check with plain code ("did the pipeline route
    this case to ``human_triage`` like the answer key says?"). Code metrics
    are free, instant, and perfectly repeatable - use them whenever you can.

HOW agents-cli CALLS THESE FUNCTIONS
    ``evals/eval_config.yaml`` lists each metric with a tiny
    ``custom_function`` that imports one function from this file and names it
    ``evaluate``. ``agents-cli eval grade`` then calls ``evaluate(instance)``
    once per eval case, where ``instance`` is the eval case as a plain dict::

        {
          "eval_case_id": "core_03_sewer_backup_la",
          "prompt":   {"role": "user", "parts": [{"text": "..."}]},
          "response": {"role": "model", "parts": [{"text": "# Flood Claim Intake Packet ..."}]},
          "agent_data": {"turns": [{"events": [...]}]},   # full trace
          "expected": {...},        # OUR answer key (extra field, see below)
          "pipeline_state": {...},  # final ADK state (extra field, if present)
        }

    Each function returns ``{"score": float, "explanation": str}``. Scores are
    averaged per metric into ``mean_score`` in the results file.

IMPORTANT: STANDARD LIBRARY ONLY
    Local custom functions run *inside the agents-cli process* (its own
    virtual environment, installed with ``uv tool install``), NOT inside this
    project's ``.venv``. So this file must not import ``claimdesk``, pydantic
    or any third-party package. ``tests/test_evals_offline.py`` enforces this.

WHERE DO THE PIPELINE OUTPUTS COME FROM? (most reliable first)
    1. ``instance["pipeline_state"]`` - written by ``evals/generate_traces.py``;
       the exact final ADK session state (claim_facts, water_source, packet...).
    2. ``state_delta`` on trace events - also written by generate_traces.py.
    3. The LLM nodes' JSON text (authors ``extract_facts``/``classify_claim``)
       and the Markdown packet in the final response. This is all that is
       available in traces produced by ``agents-cli eval generate`` over HTTP.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

BLANK_VALUES = {"", "unknown", "not specified", "unspecified", "n/a", "none", "not provided", "null"}
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y")
NUMERIC_FIELDS = {"estimated_loss_usd", "water_depth_inches"}
NUMERIC_REL_TOLERANCE = 0.05  # 5 %: "about 23 thousand" vs 23,400 still counts
NUMERIC_ABS_TOLERANCE = 1.0

# Packet Markdown header lines written by claimdesk/rules/packet_writer.py.
_MD_FIELDS = {
    "claim_type": re.compile(r"\*\*Claim type:\*\*\s*(.+)"),
    "water_source": re.compile(r"\*\*Water source:\*\*\s*(.+)"),
    "intake_status": re.compile(r"\*\*Intake status:\*\*\s*(.+)"),
    "severity": re.compile(r"\*\*Severity:\*\*\s*(.+)"),
    "routing_decision": re.compile(r"\*\*Routing decision:\*\*\s*(.+)"),
}
_LLM_NODE_STATE_KEY = {"extract_facts": "claim_facts", "classify_claim": "classification"}


# ---------------------------------------------------------------------------
# Reading the trace
# ---------------------------------------------------------------------------
def _as_dict(value: Any) -> dict | None:
    """ADK state values can be dicts or JSON strings; return a dict or None."""

    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _events(instance: dict) -> list[dict]:
    agent_data = instance.get("agent_data") or {}
    return [event for turn in agent_data.get("turns") or [] for event in turn.get("events") or []]


def _texts(content: dict | None) -> list[str]:
    return [p["text"] for p in (content or {}).get("parts") or [] if isinstance(p, dict) and p.get("text")]


def response_text(instance: dict) -> str:
    """The final response text (the packet Markdown for pipeline traces)."""

    return "".join(_texts(instance.get("response")))


def parse_packet_markdown(markdown: str) -> dict[str, str]:
    """Recover the packet header fields from the Markdown, e.g. 'Needs Docs' -> 'needs_docs'."""

    found: dict[str, str] = {}
    for key, pattern in _MD_FIELDS.items():
        match = pattern.search(markdown or "")
        if match:
            found[key] = re.sub(r"\s+", "_", match.group(1).strip().lower())
    return found


def pipeline_outputs(instance: dict) -> dict[str, Any]:
    """Collect the pipeline's outputs from whatever the trace contains.

    Returns a dict that may contain ``claim_facts``, ``classification``,
    ``water_source``, ``evidence_decision``, ``risk_gate``, ``packet`` and
    ``packet_markdown_fields``. Earlier (more reliable) sources win.
    """

    out: dict[str, Any] = {}
    for key, value in (instance.get("pipeline_state") or {}).items():
        parsed = _as_dict(value)
        out[key] = parsed if parsed is not None else value
    for event in _events(instance):
        for key, value in (event.get("state_delta") or {}).items():
            parsed = _as_dict(value)
            out.setdefault(key, parsed if parsed is not None else value)
        state_key = _LLM_NODE_STATE_KEY.get(event.get("author", ""))
        if state_key and state_key not in out:
            for text in _texts(event.get("content")):
                parsed = _as_dict(text)
                if parsed is not None:
                    out[state_key] = parsed
    out["packet_markdown_fields"] = parse_packet_markdown(response_text(instance))
    return out


def actual_routing(outputs: dict) -> str | None:
    return (outputs.get("packet") or {}).get("routing_decision") or (outputs.get("risk_gate") or {}).get("final_routing_decision") or outputs["packet_markdown_fields"].get("routing_decision")


def actual_water_source(outputs: dict) -> str | None:
    return (outputs.get("water_source") or {}).get("water_source") or outputs["packet_markdown_fields"].get("water_source") or (outputs.get("classification") or {}).get("water_source")


def actual_claim_type(outputs: dict) -> str | None:
    # The packet's claim type already includes the rule override
    # (home_flood + non-flood water => internal_water), so prefer it.
    return (outputs.get("packet") or {}).get("claim_type") or outputs["packet_markdown_fields"].get("claim_type") or (outputs.get("classification") or {}).get("claim_type")


def _expected(instance: dict) -> dict:
    expected = instance.get("expected")
    if not isinstance(expected, dict):
        raise ValueError(f"Eval case {instance.get('eval_case_id')!r} has no 'expected' answer key.")
    return expected


# ---------------------------------------------------------------------------
# Normalizers used to compare facts fairly
# ---------------------------------------------------------------------------
def is_blank(value: Any) -> bool:
    return value is None or str(value).strip().lower() in BLANK_VALUES


def normalize_date(value: Any) -> str | None:
    text = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", str(value or "").strip(), flags=re.IGNORECASE)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _words(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _alnum(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d[\d,]*\.?\d*", str(value or ""))
    return float(match.group(0).replace(",", "")) if match else None


def fact_matches(field: str, expected: Any, actual: Any) -> bool:
    """Compare one extracted fact with the answer key.

    * ``expected is None``  -> the fact must be ABSENT (the extractor must not
      invent it). This is how edge cases test for hallucinated facts.
    * a list                -> any of the listed values is acceptable.
    * dates                 -> compared as calendar dates, any common format.
    * money / depth         -> within 5 % (or 1 unit).
    * policy numbers, ZIPs  -> letters/digits only (``fld-co 4h7k2p`` == ``FLD-CO-4H7K2P``).
    * address / city / contact -> the expected text must appear in the actual text.
    * everything else       -> same words, ignoring case and punctuation.
    """

    if isinstance(expected, list):
        return any(fact_matches(field, option, actual) for option in expected)
    if expected is None:
        return is_blank(actual)
    if is_blank(actual):
        return False
    if field in {"date_of_loss", "reported_date"}:
        return normalize_date(expected) is not None and normalize_date(expected) == normalize_date(actual)
    if field in NUMERIC_FIELDS:
        exp, act = _number(expected), _number(actual)
        if exp is None or act is None:
            return False
        return abs(exp - act) <= max(NUMERIC_ABS_TOLERANCE, NUMERIC_REL_TOLERANCE * abs(exp))
    if field in {"policy_number", "loss_zip_code"}:
        return _alnum(expected) == _alnum(actual)
    if field == "loss_state":
        return str(expected).strip().upper() == str(actual).strip().upper()
    if field in {"loss_address_or_city", "contact_method"}:
        return _words(expected) in _words(actual)
    return _words(expected) == _words(actual)


# ---------------------------------------------------------------------------
# The metrics (each is referenced from evals/eval_config.yaml)
# ---------------------------------------------------------------------------
def fact_extraction_accuracy(instance: dict) -> dict:
    """Fraction of answer-key facts the extractor got right (0.0 - 1.0)."""

    expected_facts = _expected(instance).get("facts")
    if not isinstance(expected_facts, dict) or not expected_facts:
        raise ValueError(f"Eval case {instance.get('eval_case_id')!r} has no expected.facts to compare.")
    facts = pipeline_outputs(instance).get("claim_facts")
    if not isinstance(facts, dict):
        return {"score": 0.0, "explanation": "No claim_facts found in the trace (did extract_facts run?)."}
    misses = [
        f"{field}: expected {value!r}, got {facts.get(field)!r}"
        for field, value in expected_facts.items()
        if not fact_matches(field, value, facts.get(field))
    ]
    correct = len(expected_facts) - len(misses)
    explanation = f"{correct}/{len(expected_facts)} facts correct."
    if misses:
        explanation += " Mismatches: " + "; ".join(misses)
    return {"score": round(correct / len(expected_facts), 4), "explanation": explanation}


def _exact_label_metric(instance: dict, label: str, actual: str | None, extra_ok_key: str | None = None) -> dict:
    expected = _expected(instance)
    wanted = expected.get(label)
    if wanted is None:
        raise ValueError(f"Eval case {instance.get('eval_case_id')!r} has no expected.{label}.")
    allowed = set(wanted if isinstance(wanted, list) else [wanted])
    if extra_ok_key:
        allowed |= set(expected.get(extra_ok_key) or [])
    ok = actual in allowed
    return {"score": 1.0 if ok else 0.0, "explanation": f"{label}: expected {sorted(allowed)}, got {actual!r}."}


def routing_correct(instance: dict) -> dict:
    """1.0 if the packet's final routing equals the expected routing.

    ``expected.acceptable_routing`` may list extra routes a human reviewer
    would also accept (use sparingly - it weakens the test).
    """

    return _exact_label_metric(instance, "routing", actual_routing(pipeline_outputs(instance)), "acceptable_routing")


def water_source_correct(instance: dict) -> dict:
    """1.0 if the deterministic water-source decision matches the answer key."""

    return _exact_label_metric(instance, "water_source", actual_water_source(pipeline_outputs(instance)))


def claim_type_correct(instance: dict) -> dict:
    """1.0 if the packet's claim type (after rule overrides) matches."""

    return _exact_label_metric(instance, "claim_type", actual_claim_type(pipeline_outputs(instance)))


def packet_refreshed(instance: dict) -> dict:
    """LIVE layer: 1.0 if the voice agent called ``refresh_intake_packet`` at least once.

    A cheap, deterministic companion to the LLM-judged
    ``multi_turn_tool_use_quality``: an intake conversation that never hands
    the facts to the claim pipeline cannot produce a packet.
    """

    calls = [
        part["function_call"].get("name")
        for event in _events(instance)
        for part in (event.get("content") or {}).get("parts") or []
        if isinstance(part, dict) and isinstance(part.get("function_call"), dict)
    ]
    count = calls.count("refresh_intake_packet")
    return {"score": 1.0 if count else 0.0, "explanation": f"refresh_intake_packet called {count} time(s); tools used: {sorted(set(calls)) or 'none'}."}


__all__ = [
    "fact_extraction_accuracy",
    "routing_correct",
    "water_source_correct",
    "claim_type_correct",
    "packet_refreshed",
    "pipeline_outputs",
    "fact_matches",
    "parse_packet_markdown",
]
