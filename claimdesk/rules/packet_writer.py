"""Rule step 5: build the adjuster hand-off packet (structured + Markdown).

The packet is what a human adjuster would receive. It deliberately contains:
  * a scope note (flood desk only; what happens to non-flood losses),
  * the data sources used (real FEMA/NOAA data vs generated identity fields),
  * a disclaimer that nothing is promised,
  * the full audit trail, so every decision can be traced back to a rule.
"""

from __future__ import annotations

from typing import Any

from ..contracts import (
    ClaimClassification,
    ClaimFacts,
    DocumentChecklist,
    EvidenceDecision,
    FieldCheck,
    IntakePacket,
    RiskGate,
    WaterSourceDecision,
)
from ._helpers import as_model, dedupe
from .required_fields import BLOCKING_FIELD_QUESTIONS

SCOPE_NOTE = (
    "ClaimDesk v2 is a residential **flood** intake desk built on FEMA NFIP data. Internal water "
    "(burst pipes, sump pump failure, drain/sewer backup, seepage) and non-property claims (auto, theft, "
    "travel, medical) are recorded and routed to human triage."
)

DATA_NOTE = (
    "Policy attributes (term, limits, deductibles, flood zone, city/ZIP) come from real FEMA OpenFEMA "
    "NfipPolicies v3 records; policy numbers and policyholder names are generated because FEMA redacts "
    "personal data. Thresholds come from FEMA NfipClaims v3. Weather corroboration uses NOAA Storm Events "
    "(BigQuery public dataset)."
)


def _next_question(route: str, missing: list[str], required_documents: list[str], water: WaterSourceDecision) -> str:
    if route == "emergency_escalation":
        return "If anyone is in danger, please call 911 first. A person on our team should review this right away."
    if route == "human_triage" and water.claimant_message:
        return water.claimant_message
    if route == "human_triage":
        return "This desk handles flood claims, so a colleague will pick up your file. Is there anything else about the damage I should note?"
    if route == "policy_review":
        return "Could you confirm the policy number, the name on the policy, and the date the water came in?"
    for name in missing:
        if name in BLOCKING_FIELD_QUESTIONS:
            return BLOCKING_FIELD_QUESTIONS[name]
    if water.water_source == "unknown" and water.claimant_message:
        return water.claimant_message
    if missing:
        return f"Could you help me with one more detail: {missing[0]}?"
    if required_documents:
        return f"Do you have this available now: {required_documents[0]}?"
    return "Thank you. Your file has what an adjuster needs to start. Keep photos, receipts and damaged items until the adjuster has seen them."


def write_packet(
    claim_value: Any,
    field_check_value: Any,
    classification_value: Any,
    water_value: Any,
    evidence_value: Any,
    checklist_value: Any,
    risk_value: Any,
) -> dict[str, Any]:
    claim = as_model(ClaimFacts, claim_value)
    field_check = as_model(FieldCheck, field_check_value)
    classification = as_model(ClaimClassification, classification_value)
    water = as_model(WaterSourceDecision, water_value)
    evidence = as_model(EvidenceDecision, evidence_value)
    checklist = as_model(DocumentChecklist, checklist_value)
    risk = as_model(RiskGate, risk_value)

    route = risk.final_routing_decision
    missing = dedupe(field_check.missing_fields)
    claim_type = classification.claim_type
    if claim_type == "home_flood" and not water.nfip_flood_candidate and water.water_source != "unknown":
        claim_type = "internal_water"

    summary = (
        f"{claim.policyholder_name} reported a {claim_type.replace('_', ' ')} loss at {claim.loss_address_or_city}"
        f"{' (' + claim.loss_zip_code + ')' if claim.loss_zip_code not in ('', 'not specified') else ''} "
        f"on {claim.date_of_loss}. Water source: {water.water_source.replace('_', ' ')}. "
        f"Summary: {claim.summary if claim.summary != 'not specified' else claim.loss_description}. "
        f"Estimated loss: {f'${claim.estimated_loss_usd:,.0f}' if claim.estimated_loss_usd is not None else 'not supplied'}."
    )
    next_q = _next_question(route, missing, evidence.required_documents, water)

    def bullets(lines: list[str], empty: str) -> str:
        return "\n".join(f"- {line}" for line in lines) if lines else f"- {empty}"

    benchmark_line = "- Benchmarks unavailable."
    if risk.benchmark and risk.benchmark.available:
        b = risk.benchmark
        benchmark_line = f"- NFIP claims in {b.state} (n={b.sample_size:,}): median damage ${b.damage_p50_usd or 0:,.0f}, p95 ${b.damage_p95_usd or 0:,.0f}."
    weather_line = f"- {risk.weather.note}" if risk.weather else "- Weather check not run (needs a flood claim with ZIP and exact date)."

    markdown = f"""# Flood Claim Intake Packet

**Claim type:** {claim_type.replace('_', ' ').title()}  
**Water source:** {water.water_source.replace('_', ' ')}  
**Intake status:** {field_check.intake_status.replace('_', ' ').title()}  
**Severity:** {classification.severity.title()}  
**Routing decision:** {route.replace('_', ' ').title()}

> {SCOPE_NOTE}

## Adjuster summary
{summary}

## Missing information
{bullets(missing, 'No required intake fields are missing.')}

## Document checklist
{bullets([f"[{i.status}] **{i.item}** ({i.priority}) - {i.reason}" for i in checklist.items], 'No documents identified.')}

## Review considerations (not a coverage decision)
{bullets(evidence.coverage_considerations, 'None.')}

## Rule findings
{bullets([f"`{f.rule_id}` [{f.severity}] {f.message}" for f in evidence.findings], 'None.')}

## Risk and corroboration signals
{bullets([f"`{s.rule_id}` [{s.severity}] {s.message}" for s in risk.signals], 'No signals triggered.')}

## Real-world context
{benchmark_line}
{weather_line}

## Next question for the claimant
{next_q}

## Data sources
{DATA_NOTE}

## Audit trail
{chr(10).join(f"{n}. {entry}" for n, entry in enumerate(risk.audit_trail, start=1))}

---
This packet is an intake triage record for demonstration and education. It does not confirm coverage, payment, liability or legal rights.
"""

    return IntakePacket(
        claim_type=claim_type,  # type: ignore[arg-type]
        intake_status=field_check.intake_status,
        severity=classification.severity,
        routing_decision=route,
        missing_information=missing,
        required_documents=checklist.items,
        coverage_considerations=evidence.coverage_considerations,
        adjuster_summary=summary,
        next_question_for_claimant=next_q,
        audit_trail=risk.audit_trail,
        markdown=markdown,
    ).model_dump()


__all__ = ["write_packet", "SCOPE_NOTE", "DATA_NOTE"]
