"""Data contracts (Pydantic models) shared by the pipeline, rules and web app.

WHAT IS PYDANTIC AND WHY USE IT HERE?
    Pydantic models are Python classes that *validate* data. When Gemini
    returns JSON, ``ClaimFacts.model_validate(json)`` either gives us a clean,
    typed object or raises an error - we never pass half-broken dicts around.
    ADK also uses these classes as ``output_schema`` so Gemini is *forced* to
    answer in exactly this shape (structured output).

SCOPE OF VERSION 2 (important, also stated in README.md)
    ClaimDesk v2 handles **residential flood claims** backed by real FEMA NFIP
    data. Two other outcomes exist on purpose:
      * ``internal_water`` - burst pipe, sump pump failure, drain/sewer backup,
        seepage. NFIP does NOT cover these (FEMA's own non-payment codes
        include "02 Seepage" and "03 Backup drains"); they usually belong to a
        homeowners policy -> routed to ``human_triage``.
      * ``out_of_scope``  - auto, theft, travel, medical... -> ``human_triage``.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enumerations (Literal = "only these exact strings are allowed")
# ---------------------------------------------------------------------------
ClaimType = Literal["home_flood", "internal_water", "out_of_scope", "unclear"]

WaterSource = Literal[
    "surface_flood",  # rising water from outside: river, rain accumulation, storm surge -> NFIP
    "sump_pump_failure",  # NOT NFIP flood
    "sewer_or_drain_backup",  # NOT NFIP flood (FEMA non-payment code 03)
    "internal_plumbing",  # burst pipe, appliance leak -> NOT NFIP flood
    "seepage",  # groundwater seeping through walls -> NOT NFIP flood (code 02)
    "roof_or_wind_driven_rain",  # NOT NFIP flood (wind damage, code 16)
    "unknown",
]

Severity = Literal["low", "medium", "high", "urgent"]
IntakeStatus = Literal["valid", "missing_info"]
EvidenceStatus = Literal["unknown", "missing", "planned", "available", "received"]

RoutingDecision = Literal[
    "ready_for_adjuster",  # all core facts + documents present
    "needs_docs",  # keep collecting
    "policy_review",  # policy not found / lapsed / loss outside term / name mismatch
    "special_investigation",  # SIU: timing or evidence patterns worth a closer look
    "emergency_escalation",  # injury / unsafe home -> human immediately
    "human_triage",  # out of scope for a flood desk (internal water, auto, ...)
]

RuleAction = Literal[
    "collect_info",
    "collect_document",
    "adjuster_review",
    "siu_review",
    "emergency_escalation",
    "human_triage",
    "soft_signal",  # informational only, never changes routing on its own
]


# ---------------------------------------------------------------------------
# 1. What the claimant told us (produced by the ExtractFacts LLM step)
# ---------------------------------------------------------------------------
class EvidenceRecord(BaseModel):
    document_type: str = Field(description="Canonical key from the document catalog, e.g. damage_photo.")
    status: EvidenceStatus = "unknown"
    source_turn_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list, description="Server capture IDs. Only the server may fill this.")


class SafetyFact(BaseModel):
    # Only injury/medical/electrical/gas/unsafe_housing/rising_water with
    # status "present" escalate to emergency (rules/evidence_rules.py);
    # mold, sewage, other and anything "uncertain" become a soft note.
    category: str = Field(
        description="One of: injury, medical, electrical, gas, unsafe_housing, rising_water, sewage, mold, other."
    )
    status: Literal["present", "absent", "uncertain"]
    description: str
    source_turn_ids: list[str] = Field(default_factory=list)


class FactSource(BaseModel):
    field: str
    source_turn_ids: list[str] = Field(default_factory=list)


class ClaimFacts(BaseModel):
    """Normalized facts extracted from the conversation (was ``ClaimNarrative``)."""

    policyholder_name: str = Field(default="not specified")
    policy_number: str = Field(default="not specified")
    contact_method: str = Field(default="not specified")
    date_of_loss: str = Field(default="not specified", description="YYYY-MM-DD or 'not specified'.")
    reported_date: str = Field(default="not specified", description="YYYY-MM-DD or 'not specified'.")
    loss_address_or_city: str = Field(default="not specified")
    loss_state: str = Field(default="not specified", description="Two-letter US state code, e.g. TX.")
    loss_zip_code: str = Field(default="not specified", description="5-digit ZIP code or 'not specified'.")
    loss_description: str = Field(default="not specified")
    water_entry_description: str = Field(
        default="not specified",
        description="In the claimant's words: where and how the water got in (river, street, drain, pipe, sump...).",
    )
    water_depth_inches: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    estimated_loss_usd: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    injuries_or_safety_concerns: list[str] = Field(default_factory=list)
    parties_involved: list[str] = Field(default_factory=list)
    documents_mentioned: list[str] = Field(default_factory=list)
    missing_or_uncertain_facts: list[str] = Field(default_factory=list)
    summary: str = Field(default="not specified", description="Two-sentence factual summary.")
    evidence_records: list[EvidenceRecord] = Field(default_factory=list)
    safety_facts: list[SafetyFact] = Field(default_factory=list)
    fact_sources: list[FactSource] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 2. Outputs of the deterministic rule steps
# ---------------------------------------------------------------------------
class FieldCheck(BaseModel):
    intake_status: IntakeStatus
    missing_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ClaimClassification(BaseModel):
    """Produced by the ClassifyClaim LLM step."""

    claim_type: ClaimType
    severity: Severity
    severity_rationale: str
    water_source: WaterSource = "unknown"
    water_source_rationale: str = ""
    claimant_needs: list[str] = Field(default_factory=list)


class WaterSourceDecision(BaseModel):
    """Deterministic interpretation of the water source (rules/water_source.py)."""

    water_source: WaterSource
    nfip_flood_candidate: bool
    explanation: str
    claimant_message: str = ""


class PolicyRecord(BaseModel):
    """One row of ``claimdesk.policy_registry`` as returned by lookup_policy."""

    found: bool
    policy_number: str = ""
    policyholder_name: str = ""
    status: str = "unknown"  # active | pending | expired | cancelled
    policy_line: str = ""
    property_state: str = ""
    reported_city: str = ""
    reported_zip_code: str = ""
    rated_flood_zone: str = ""
    effective_start: str = ""
    effective_end: str = ""
    building_coverage_usd: Optional[int] = None
    contents_coverage_usd: Optional[int] = None
    building_deductible_usd: Optional[int] = None
    contents_deductible_usd: Optional[int] = None
    primary_residence: Optional[bool] = None
    source_record_id: str = ""  # the real OpenFEMA NfipPolicies v3 `id`
    message: str = ""


class BenchmarkResult(BaseModel):
    """How a claim compares with real NFIP claims (rules use it for thresholds)."""

    available: bool
    state: str = ""
    sample_size: int = 0
    damage_p50_usd: Optional[float] = None
    damage_p90_usd: Optional[float] = None
    damage_p95_usd: Optional[float] = None
    report_lag_p95_days: Optional[float] = None
    note: str = ""


class WeatherCheckResult(BaseModel):
    """NOAA Storm Events corroboration (a SOFT signal, never a denial)."""

    checked: bool
    events_found: int = 0
    event_types: list[str] = Field(default_factory=list)
    nearest_event_km: Optional[float] = None
    window_days: int = 3
    radius_km: int = 50
    note: str = ""


class RuleFinding(BaseModel):
    rule_id: str
    severity: Severity
    message: str
    action: RuleAction
    document: Optional[str] = None


class EvidenceDecision(BaseModel):
    routing_decision: RoutingDecision
    required_documents: list[str] = Field(default_factory=list)
    coverage_considerations: list[str] = Field(default_factory=list)
    findings: list[RuleFinding] = Field(default_factory=list)
    audit_trail: list[str] = Field(default_factory=list)


class ChecklistItem(BaseModel):
    document_type: str
    item: str
    reason: str
    priority: Literal["required", "recommended", "conditional"]
    status: EvidenceStatus = "unknown"
    already_provided: bool = False
    evidence_ids: list[str] = Field(default_factory=list)


class DocumentChecklist(BaseModel):
    items: list[ChecklistItem] = Field(default_factory=list)
    claimant_tip: str = ""


class RiskGate(BaseModel):
    final_routing_decision: RoutingDecision
    signals: list[RuleFinding] = Field(default_factory=list)
    benchmark: Optional[BenchmarkResult] = None
    weather: Optional[WeatherCheckResult] = None
    audit_trail: list[str] = Field(default_factory=list)


class IntakePacket(BaseModel):
    """Final adjuster hand-off packet (was ``ClaimIntakePacket``)."""

    claim_type: ClaimType
    intake_status: IntakeStatus
    severity: Severity
    routing_decision: RoutingDecision
    missing_information: list[str] = Field(default_factory=list)
    required_documents: list[ChecklistItem] = Field(default_factory=list)
    coverage_considerations: list[str] = Field(default_factory=list)
    adjuster_summary: str
    next_question_for_claimant: str
    audit_trail: list[str] = Field(default_factory=list)
    markdown: str


__all__ = [
    "ClaimType",
    "WaterSource",
    "Severity",
    "RoutingDecision",
    "EvidenceRecord",
    "SafetyFact",
    "FactSource",
    "ClaimFacts",
    "FieldCheck",
    "ClaimClassification",
    "WaterSourceDecision",
    "PolicyRecord",
    "BenchmarkResult",
    "WeatherCheckResult",
    "RuleFinding",
    "EvidenceDecision",
    "ChecklistItem",
    "DocumentChecklist",
    "RiskGate",
    "IntakePacket",
]
