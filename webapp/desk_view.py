"""Turn an intake (transcript + latest pipeline result) into what the browser draws.

WHY A SEPARATE "VIEW" MODULE?
    The claim pipeline (``claimdesk/intake_pipeline.py``) returns rich,
    structured data meant for adjusters and rules. The notebook UI needs a
    flatter shape: labelled fields with a status colour, a progress number,
    a feed of rule events, and packet Markdown with links to the evidence.
    Keeping that translation here - as *pure functions with no I/O* - means:

    * the web routes and the live bridge build the UI state the same way,
    * unit tests can check it without Firestore, GCS or Gemini,
    * business rules are NOT duplicated: we only *read* rule outputs and reuse
      helpers from ``claimdesk.rules``.
"""

from __future__ import annotations

from typing import Any

from claimdesk.contracts import ClaimClassification, ClaimFacts, PolicyRecord
from claimdesk.data_access.policy_registry import policy_status_headline
from claimdesk.rules._helpers import is_blank
from claimdesk.rules.evidence_rules import DOCUMENTS, IMMEDIATE_HAZARD_CATEGORIES, apply_evidence_rules, build_checklist, safety_category
from claimdesk.rules.packet_writer import write_packet
from claimdesk.rules.required_fields import BLOCKING_FIELD_QUESTIONS, check_required_fields
from claimdesk.rules.risk_signals import score_risk_signals
from claimdesk.rules.water_source import decide_water_source
from claimdesk.settings import get_settings

from .intake_store import IntakeRecord

def greeting() -> str:
    """The agent's first line, written into every new transcript.

    Built from settings so it matches the brand and agent name the voice
    agent uses (see ``voice_tools.build_system_instruction``).
    """

    s = get_settings()
    return (
        f"Hi, this is {s.agent_display_name} from {s.brand_name} flood claims. I can start your claim while we talk. "
        "First - are you and everyone in the home safe right now?"
    )


def blank_pipeline_result() -> dict[str, Any]:
    """The packet before anyone has spoken, built with the real rules.

    The original demo had a hand-written "initial state". Here we simply run
    the deterministic rule functions on an empty ``ClaimFacts`` - no Gemini
    call, no BigQuery call (``policy_issues=[]`` and no benchmark/weather) -
    so the empty notebook shows exactly the checklist the rules would ask for.
    """

    facts = ClaimFacts().model_dump()
    field_check = check_required_fields(facts)
    classification = ClaimClassification(
        claim_type="unclear", severity="low", severity_rationale="No conversation yet."
    ).model_dump()
    water = decide_water_source(facts, classification)
    evidence = apply_evidence_rules(facts, field_check, classification, water, policy_issues=[])
    checklist = build_checklist(facts, classification, water)
    risk = score_risk_signals(facts, field_check, classification, water, evidence, benchmark={"available": False}, weather=None)
    packet = write_packet(facts, field_check, classification, water, evidence, checklist, risk)
    return {
        "claim_facts": facts,
        "field_check": field_check,
        "classification": classification,
        "water_source": water,
        "evidence_decision": evidence,
        "checklist": checklist,
        "risk_gate": risk,
        "packet": packet,
        "final_markdown": packet["markdown"],
    }


# ---------------------------------------------------------------------------
# Small field helpers
# ---------------------------------------------------------------------------
def _status(value: Any, urgent: bool = False) -> str:
    if urgent:
        return "urgent"
    if is_blank(value) or str(value).strip().lower() in {"not captured yet"}:
        return "missing"
    return "complete"


def _field(label: str, value: Any, source: str = "Claim team extraction", urgent: bool = False) -> dict[str, str]:
    status = _status(value, urgent=urgent)
    return {
        "label": label,
        "value": str(value) if status != "missing" else f"Missing: {label.lower()}",
        "status": status,
        "source": "-" if status == "missing" else source,
    }


def _money(value: Any) -> str:
    return f"${int(value):,}" if isinstance(value, (int, float)) else "?"


def _location(facts: ClaimFacts) -> str:
    parts = [p for p in (facts.loss_address_or_city, facts.loss_state, facts.loss_zip_code) if not is_blank(p)]
    return ", ".join(dict.fromkeys(parts))  # dict.fromkeys = de-duplicate, keep order


def _safety_text(facts: ClaimFacts) -> tuple[str, bool]:
    """Safety row text and whether it is *urgent*.

    WHY reuse the claim team's rule? The desk must agree with the safety gate:
    only a hazard that is ``present`` AND in an immediate category (injury,
    gas, live electricity, ...) is urgent. Mold, sewage or an "I'm not sure"
    hazard is still shown, but as a note, so the row doesn't cry wolf on
    almost every flood claim.
    """

    urgent: list[str] = []
    noted: list[str] = []
    for fact in facts.safety_facts:
        if fact.status == "present" and safety_category(fact.category) in IMMEDIATE_HAZARD_CATEGORIES:
            urgent.append(fact.description)
        elif fact.status in {"present", "uncertain"}:
            noted.append(fact.description)
    if urgent:
        return "; ".join(urgent + [f"Noted: {text}" for text in noted]), True
    if noted:
        return "Noted: " + "; ".join(noted), False
    if any(f.status == "absent" for f in facts.safety_facts):
        return "No injuries or hazards reported", False
    return "", False


def _evidence_text(facts: ClaimFacts) -> str:
    items = []
    for record in facts.evidence_records:
        if record.status in {"available", "received", "planned"}:
            label = DOCUMENTS.get(record.document_type, (record.document_type, ""))[0]
            items.append(f"{label} ({record.status})")
    items += [d for d in facts.documents_mentioned if d]
    return ", ".join(dict.fromkeys(items))


def _policy_fields(record: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """Rows sourced from the background ``find_policy`` look-up (BigQuery)."""

    source = "Policy registry (BigQuery)"
    empty = {
        "policyStatus": _field("Policy status", ""),
        "policyTerm": _field("Policy term", ""),
        "floodZone": _field("Flood zone", ""),
        "coverage": _field("Coverage limits", ""),
        "deductible": _field("Deductibles", ""),
    }
    if not record:
        return empty
    policy = PolicyRecord.model_validate(record)
    if not policy.found:
        return empty | {"policyStatus": _field("Policy status", "Not found - confirm number", source=source, urgent=True)}
    return {
        "policyStatus": _field("Policy status", policy_status_headline(policy), source=source, urgent=policy.status != "active"),
        "policyTerm": _field("Policy term", f"{policy.effective_start} to {policy.effective_end}", source=source),
        "floodZone": _field("Flood zone", policy.rated_flood_zone, source=source),
        "coverage": _field(
            "Coverage limits",
            f"Building {_money(policy.building_coverage_usd)} / Contents {_money(policy.contents_coverage_usd)}",
            source=source,
        ),
        "deductible": _field(
            "Deductibles",
            f"Building {_money(policy.building_deductible_usd)} / Contents {_money(policy.contents_deductible_usd)}",
            source=source,
        ),
    }


def _events(record: IntakeRecord, result: dict[str, Any]) -> list[dict[str, str]]:
    """Rule findings and routing changes, for the activity feed / packet."""

    if record.pipeline_revision is None:
        return []
    events = [
        {
            "tone": "success",
            "title": "Claim team update complete",
            "detail": f"Facts re-extracted with {get_settings().reasoning_model} and rules re-applied.",
            "rule": "LLM-001",
        }
    ]
    for finding in result["evidence_decision"].get("findings", []):
        tone = {"emergency_escalation": "danger", "soft_signal": "info"}.get(finding["action"], "warning")
        events.append({"tone": tone, "title": finding["message"], "detail": f"Action: {finding['action']}.", "rule": finding["rule_id"]})
    for signal in result["risk_gate"].get("signals", []):
        tone = {"emergency_escalation": "danger", "soft_signal": "info"}.get(signal["action"], "warning")
        events.append({"tone": tone, "title": signal["message"], "detail": "Risk / corroboration signal.", "rule": signal["rule_id"]})
    if record.previous_route and record.previous_route != record.route:
        events.append(
            {
                "tone": "danger" if record.route == "emergency_escalation" else "success",
                "title": "Routing changed",
                "detail": f"{record.previous_route} -> {record.route}.",
                "rule": "ROUTE-001",
            }
        )
    return events


# ---------------------------------------------------------------------------
# Evidence + packet
# ---------------------------------------------------------------------------
def evidence_url(intake_id: str, evidence_id: str) -> str:
    return f"/api/intakes/{intake_id}/evidence/{evidence_id}"


def public_photo(intake_id: str, photo: dict[str, Any]) -> dict[str, Any]:
    """Photo metadata safe for the browser: no storage paths, plus an app URL."""

    hidden = {"object_path", "storage_uri"}
    return {k: v for k, v in photo.items() if k not in hidden} | {"url": evidence_url(intake_id, photo["id"])}


def public_sketch(intake_id: str, sketch: dict[str, Any] | None) -> dict[str, Any] | None:
    if not sketch:
        return None
    hidden = {"object_path", "storage_uri"}
    return {k: v for k, v in sketch.items() if k not in hidden} | {"url": f"/api/intakes/{intake_id}/sketch?v={sketch['version']}"}


def evidence_manifest(record: IntakeRecord) -> list[dict[str, Any]]:
    """Photo metadata for ``packet.json`` (no bytes, no internal paths)."""

    hidden = {"object_path", "storage_uri"}
    return [{k: v for k, v in photo.items() if k not in hidden} for photo in record.evidence_photos]


def packet_markdown(record: IntakeRecord, result: dict[str, Any]) -> str:
    """Pipeline packet Markdown + links to the evidence files inside the ZIP."""

    text = result["packet"]["markdown"].rstrip() + "\n\n## Captured evidence\n"
    for photo in record.evidence_photos:
        verdict = "supported by the image" if photo.get("confirmed") else "not confirmed by the image"
        text += f"- [{photo['id']}](evidence/{photo['id']}.jpg): {photo.get('caption', '')} - {verdict}; {photo.get('source', 'camera')} {photo.get('captured_at', '')}\n"
    if record.sketch:
        text += f"- [Sketch v{record.sketch['version']}](sketch.png): generated illustration of the claimant's account, not a photograph.\n"
    if not record.evidence_photos and not record.sketch:
        text += "No evidence captured.\n"
    text += "\nThis demo prepares a packet only; it has not been sent to an adjuster.\n"
    return text


def current_result(record: IntakeRecord) -> dict[str, Any]:
    return record.pipeline_result or blank_pipeline_result()


def build_desk_state(record: IntakeRecord, *, live_model: str | None = None) -> dict[str, Any]:
    """The JSON the notebook UI renders (sent over REST and the WebSocket)."""

    result = current_result(record)
    facts = ClaimFacts.model_validate(result["claim_facts"])
    classification = ClaimClassification.model_validate(result["classification"])
    field_check = result["field_check"]
    checklist_items = result["checklist"].get("items", [])
    packet = result["packet"]
    route = result["risk_gate"]["final_routing_decision"]

    safety, urgent = _safety_text(facts)
    evidence_text = _evidence_text(facts) or "Not captured yet"
    raw_water = result["water_source"].get("water_source", "unknown").replace("_", " ")
    water_display = (
        "unknown (needs review)"
        if raw_water == "unknown" and (not is_blank(facts.water_entry_description) or not is_blank(facts.loss_description))
        else raw_water
    )
    fields = {
        "claimant": _field("Policyholder name", facts.policyholder_name),
        "policy": _field("Policy number", facts.policy_number),
        "contact": _field("Contact method", facts.contact_method),
        "claimType": _field("Claim type", packet["claim_type"].replace("_", " ")),
        "waterSource": _field("Water source", water_display),
        "date": _field("Date of loss", facts.date_of_loss),
        "reported": _field("Reported date", facts.reported_date),
        "location": _field("Loss location", _location(facts)),
        "description": _field("What happened", facts.loss_description),
        "waterEntry": _field("How water got in", facts.water_entry_description),
        "waterDepth": _field("Water depth", f"{facts.water_depth_inches:g} inches" if facts.water_depth_inches is not None else ""),
        "estimate": _field("Estimated loss", _money(facts.estimated_loss_usd) if facts.estimated_loss_usd is not None else ""),
        "safety": _field("Safety", safety, source="Claim team + safety gate", urgent=urgent),
        "evidence": _field("Evidence available", evidence_text),
        **_policy_fields(record.policy_record),
    }

    # Progress = required facts that are present and valid + documents received,
    # divided by everything required. Same formula as the original demo.
    missing = field_check.get("missing_fields", [])
    required = list(BLOCKING_FIELD_QUESTIONS)
    valid = sum(not is_blank(getattr(facts, key)) and key not in missing for key in required)
    if any("date" in str(item).lower() for item in missing) and not is_blank(facts.date_of_loss):
        valid = max(0, valid - 1)
    provided = sum(bool(item.get("already_provided")) for item in checklist_items)
    progress = round(100 * (valid + provided) / max(1, len(required) + len(checklist_items)))

    photos = [public_photo(record.intake_id, p) for p in record.evidence_photos]
    return {
        "intake_id": record.intake_id,
        "revision": record.revision,
        "route": route,
        "progress": progress,
        "fields": fields,
        "transcript": record.transcript,
        "events": _events(record, result),
        "policy": record.policy_record,
        "tool_activity": record.tool_activity[-12:],
        "live_model": live_model,
        "missing_blockers": missing,
        "documents": checklist_items,
        "evidence_photos": photos,
        "evidence_manifest": evidence_manifest(record),
        "camera_notes": record.camera_notes,
        "sketch": public_sketch(record.intake_id, record.sketch),
        "severity": classification.severity,
        "claim_type": packet["claim_type"].replace("_", " "),
        "handoff": {
            "Summary": packet["adjuster_summary"],
            "Priority": f"{classification.severity.title()} - {classification.severity_rationale}",
            "Required actions": ", ".join(i["item"] for i in checklist_items if not i.get("already_provided")) or "No outstanding documents.",
            "Attachments": evidence_text,
            "Next best action": packet["next_question_for_claimant"],
        },
        "packet_markdown": packet_markdown(record, result),
        "model": get_settings().reasoning_model,
    }


__all__ = [
    "greeting",
    "blank_pipeline_result",
    "build_desk_state",
    "current_result",
    "evidence_manifest",
    "evidence_url",
    "packet_markdown",
    "public_photo",
    "public_sketch",
]
