"""Rule step 3: document catalog, evidence gates and the first routing decision.

KEY SAFETY PROPERTY - "received" evidence
    An LLM can *say* the claimant has photos, but only the server's capture
    registry (a camera frame the app actually saved to Cloud Storage) can mark a
    document as **received**. ``merge_server_evidence`` enforces that: it
    downgrades any LLM-claimed "received" to "available" and then adds real
    captures. This stops the model from "hallucinating" paperwork into the file.
"""

from __future__ import annotations

import re
from typing import Any

from ..contracts import (
    ChecklistItem,
    ClaimClassification,
    ClaimFacts,
    DocumentChecklist,
    EvidenceDecision,
    FieldCheck,
    RuleFinding,
    SafetyFact,
    WaterSourceDecision,
)
from ..data_access.policy_registry import review_policy_against_claim
from ._helpers import as_model, dedupe

# ---------------------------------------------------------------------------
# Document catalog: stable key -> (label shown to people, why it is needed)
# The extractor prompt lists exactly these keys (see prompts/fact_extractor.md)
# ---------------------------------------------------------------------------
DOCUMENTS: dict[str, tuple[str, str]] = {
    "damage_photo": ("Photos or video of damaged areas before cleanup", "Documents the loss condition."),
    "water_line_photo": ("Photo showing the high-water line or water depth", "Shows how high the flood water rose inside."),
    "contents_inventory": ("List of damaged personal property", "Supports the contents part of the claim."),
    "repair_estimate": ("Repair estimate or contractor assessment", "Supports the building repair cost."),
    "mitigation_invoice": ("Water extraction or drying invoice", "Shows steps taken to prevent further damage."),
    "proof_of_loss": ("Signed Proof of Loss", "Flood policies generally require it within 60 days of the loss; the adjuster helps prepare it."),
    "ownership_receipt": ("Receipts or photos for high-value items", "Supports ownership and value of expensive contents."),
    "third_party_report": ("Any third-party report (plumber, fire department, utility)", "Helps establish the cause of loss."),
}

# Which documents each claim type needs, and with what priority.
REQUIRED_BY_TYPE: dict[str, list[tuple[str, str]]] = {
    "home_flood": [
        ("damage_photo", "required"),
        ("water_line_photo", "required"),
        ("contents_inventory", "required"),
        ("repair_estimate", "recommended"),
        ("mitigation_invoice", "recommended"),
        ("proof_of_loss", "conditional"),
    ],
    # Non-flood outcomes: collect the basics so a human can pick up the file.
    "internal_water": [("damage_photo", "required"), ("third_party_report", "recommended"), ("repair_estimate", "recommended")],
    "out_of_scope": [("damage_photo", "recommended")],
    "unclear": [("damage_photo", "required"), ("repair_estimate", "recommended")],
}


def merge_server_evidence(claim_value: Any, received_evidence: list[dict[str, Any]] | tuple = ()) -> dict[str, Any]:
    """Only server captures may mint ``received`` evidence (see module doc)."""

    claim = as_model(ClaimFacts, claim_value)
    records: list[dict[str, Any]] = []
    for record in claim.evidence_records:
        data = record.model_dump()
        data["evidence_ids"] = []
        if data["status"] == "received":
            data["status"] = "available"
        records.append(data)
    for evidence in received_evidence or ():
        evidence_id = str(evidence.get("id") or "")
        for kind in evidence.get("document_types", []):
            if kind in DOCUMENTS:
                records.append({"document_type": kind, "status": "received", "evidence_ids": [evidence_id] if evidence_id else [], "source_turn_ids": []})
    return claim.model_copy(update={"evidence_records": []}).model_dump() | {"evidence_records": records}


def _latest_record(key: str, claim: ClaimFacts):
    records = [r for r in claim.evidence_records if r.document_type == key]
    received = [r for r in records if r.status == "received" and r.evidence_ids]
    return received[-1] if received else (records[-1] if records else None)


def _is_received(key: str, claim: ClaimFacts) -> bool:
    record = _latest_record(key, claim)
    return bool(record and record.status == "received" and record.evidence_ids)


# ---------------------------------------------------------------------------
# Safety: which hazards stop the intake (skills/.../references/safety.md)
# ---------------------------------------------------------------------------
# Only hazards that can hurt someone *right now* escalate to a human
# immediately. Everything else a flood commonly leaves behind (mold, sewage
# smell, a hazard the claimant is unsure about) is written down for the
# adjuster as a soft note - otherwise "there's some mold on the drywall"
# would page an emergency team for most flood claims.
IMMEDIATE_HAZARD_CATEGORIES = frozenset({"injury", "medical", "electrical", "gas", "unsafe_housing", "rising_water"})

# The extractor writes ``category`` as free text, so a few natural spellings
# are mapped onto the canonical names above.
_CATEGORY_ALIASES = {
    "injured": "injury",
    "trapped": "injury",
    "medical_need": "medical",
    "electric": "electrical",
    "electricity": "electrical",
    "gas_leak": "gas",
    "structural": "unsafe_housing",
    "unsafe_structure": "unsafe_housing",
    "structure": "unsafe_housing",
    "uninhabitable": "unsafe_housing",
    "unsafe_home": "unsafe_housing",
    "flooding_now": "rising_water",
}


def safety_category(raw: str) -> str:
    """``"Unsafe structure"`` -> ``"unsafe_housing"``; unknown text is kept (normalized)."""

    key = "_".join(str(raw or "").strip().lower().replace("-", " ").split())
    return _CATEGORY_ALIASES.get(key, key)


def _is_urgent(fact: SafetyFact) -> bool:
    """Escalate only when status is ``present`` AND the category is immediate.

    "No one is hurt" is *absent* and never counts. "I think I smell gas" is
    ``uncertain``: the voice agent asks a follow-up, and until the claimant
    says the hazard is present it stays a soft note (SAFE-002).
    """

    return fact.status == "present" and safety_category(fact.category) in IMMEDIATE_HAZARD_CATEGORIES


def _urgent_safety(claim: ClaimFacts) -> list[str]:
    return [f.description for f in claim.safety_facts if _is_urgent(f)]


def _noted_safety(claim: ClaimFacts) -> list[str]:
    """Present-but-not-immediate (mold, sewage, other) or uncertain hazards."""

    return [f.description for f in claim.safety_facts if f.status in {"present", "uncertain"} and not _is_urgent(f)]


def _coverage_considerations(claim: ClaimFacts, claim_type: str, water: WaterSourceDecision) -> list[str]:
    """Plain-language review notes. Never a coverage decision."""

    notes: list[str] = []
    if claim_type == "home_flood":
        notes += [
            "Flood coverage generally applies to rising surface water that inundates normally dry land; it generally does not include seepage, drain/sewer backup not caused by flooding, or wind-driven rain.",
            "A signed Proof of Loss is generally due within 60 days of the date of loss.",
        ]
        text = f"{claim.loss_description} {claim.summary} {claim.water_entry_description}".lower()
        if re.search(r"\bbasement\b", text):
            notes.append(
                "Basement losses have limited flood coverage (generally structural elements, utilities and certain equipment; finished walls, flooring and most contents in a basement are generally excluded)."
            )
    elif claim_type == "internal_water" or (not water.nfip_flood_candidate and water.water_source != "unknown"):
        notes.append(f"Water source recorded as '{water.water_source}'. This is usually reviewed under a homeowners policy rather than flood insurance.")
    elif claim_type == "out_of_scope":
        notes.append("This desk handles residential flood claims only. The file is routed to a human for the correct line of business.")
    notes.append("No coverage, payment or liability is confirmed at intake. A licensed adjuster reviews the policy, exclusions and evidence.")
    return notes


def apply_evidence_rules(
    claim_value: Any,
    field_check_value: Any,
    classification_value: Any,
    water_value: Any,
    *,
    policy_issues: list[str] | None = None,
) -> dict[str, Any]:
    """First routing decision.

    ``policy_issues`` can be passed in when the caller already looked the
    policy up (the workflow does this in a parallel branch to save time). If
    omitted, the policy is looked up here.
    """

    claim = as_model(ClaimFacts, claim_value)
    field_check = as_model(FieldCheck, field_check_value)
    classification = as_model(ClaimClassification, classification_value)
    water = as_model(WaterSourceDecision, water_value)

    # If the LLM called it a flood but the water clearly came from inside,
    # the deterministic water-source rule wins.
    claim_type = classification.claim_type
    if claim_type == "home_flood" and not water.nfip_flood_candidate and water.water_source != "unknown":
        claim_type = "internal_water"

    findings: list[RuleFinding] = []
    required_docs: list[str] = []

    def add(rule_id: str, severity: str, message: str, action: str, document: str | None = None) -> None:
        findings.append(RuleFinding(rule_id=rule_id, severity=severity, message=message, action=action, document=document))  # type: ignore[arg-type]
        if document:
            required_docs.append(document)

    if field_check.missing_fields:
        add("INTAKE-001", "medium", "Required intake facts are missing.", "collect_info")

    if claim_type == "out_of_scope":
        add("SCOPE-001", "medium", "Not a flood claim; route to the correct line of business.", "human_triage")
    elif claim_type == "internal_water":
        add("SCOPE-002", "medium", f"Water source '{water.water_source}' is not surface flooding.", "human_triage")

    for key, priority in REQUIRED_BY_TYPE.get(claim_type, REQUIRED_BY_TYPE["unclear"]):
        if priority == "required" and not _is_received(key, claim):
            label, reason = DOCUMENTS[key]
            add("DOC-001", "medium", f"Missing or unconfirmed: {label}. {reason}", "collect_document", label)

    urgent_hazards = _urgent_safety(claim)
    if urgent_hazards:
        add("SAFE-001", "urgent", "An immediate safety hazard (injury, electrical, gas, unsafe structure or rising water) needs human review now.", "emergency_escalation")
    elif _noted_safety(claim):
        # Soft note only: it does not change the route. The adjuster sees it
        # in the packet, and the voice agent can ask a follow-up question.
        add("SAFE-002", "medium", "A possible or non-urgent safety concern (e.g. mold, sewage, or an unconfirmed hazard) was noted; confirm it with the claimant.", "soft_signal")

    policy_issues_found: list[str] = []
    if claim_type in {"home_flood", "unclear"}:
        policy_issues_found = policy_issues if policy_issues is not None else review_policy_against_claim(claim)
        for issue in policy_issues_found:
            add("POLICY-001", "high", issue, "adjuster_review")

    actions = {f.action for f in findings}
    if "emergency_escalation" in actions:
        route = "emergency_escalation"
    elif "human_triage" in actions:
        route = "human_triage"
    elif policy_issues_found:
        route = "policy_review"
    elif field_check.missing_fields or "collect_document" in actions:
        route = "needs_docs"
    else:
        route = "ready_for_adjuster"

    return EvidenceDecision(
        routing_decision=route,  # type: ignore[arg-type]
        required_documents=dedupe(required_docs),
        coverage_considerations=_coverage_considerations(claim, claim_type, water),
        findings=findings,
        audit_trail=[
            "Checked minimum intake fields.",
            f"Classified as {claim_type} ({classification.severity}); water source {water.water_source}.",
            "Applied document, safety and policy-term gates.",
            f"Initial route: {route}.",
        ],
    ).model_dump()


def build_checklist(claim_value: Any, classification_value: Any, water_value: Any) -> dict[str, Any]:
    claim = as_model(ClaimFacts, claim_value)
    classification = as_model(ClaimClassification, classification_value)
    water = as_model(WaterSourceDecision, water_value)
    claim_type = classification.claim_type
    if claim_type == "home_flood" and not water.nfip_flood_candidate and water.water_source != "unknown":
        claim_type = "internal_water"

    items: list[ChecklistItem] = []
    for key, priority in REQUIRED_BY_TYPE.get(claim_type, REQUIRED_BY_TYPE["unclear"]):
        label, reason = DOCUMENTS[key]
        record = _latest_record(key, claim)
        received = _is_received(key, claim)
        items.append(
            ChecklistItem(
                document_type=key,
                item=label,
                reason=reason,
                priority=priority,  # type: ignore[arg-type]
                status=record.status if record else "unknown",
                already_provided=received,
                evidence_ids=record.evidence_ids if (record and received) else [],
            )
        )
    return DocumentChecklist(
        items=items,
        claimant_tip="Show photos on camera when you can. Take pictures before you throw anything away, and note the high-water line.",
    ).model_dump()


__all__ = ["DOCUMENTS", "IMMEDIATE_HAZARD_CATEGORIES", "REQUIRED_BY_TYPE", "merge_server_evidence", "apply_evidence_rules", "build_checklist", "safety_category"]
