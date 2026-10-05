"""Layer 1: unit tests for ``claimdesk/rules/evidence_rules.py``.

This module makes the FIRST routing decision and owns the document checklist.
Three properties matter most and are pinned down below:

1. **Only the server can mint "received" evidence.** The LLM may *say* the
   claimant has photos; that is "available" at most. A document is
   "received" only when the server's capture registry supplies an evidence id.
2. **Route precedence** (highest first)::

       emergency_escalation > human_triage > policy_review > needs_docs > ready_for_adjuster

3. **The deterministic water-source decision overrides the LLM's claim type**:
   ``home_flood`` + a non-flood water source => treated as ``internal_water``.

Rule ids asserted here (they appear in the packet's audit trail):
    INTAKE-001 missing facts, SCOPE-001 out of scope, SCOPE-002 internal water,
    DOC-001 missing required document, SAFE-001 immediate safety hazard
    (present injury/medical/electrical/gas/unsafe_housing/rising_water),
    SAFE-002 soft safety note (uncertain, mold, sewage, other), POLICY-001
    policy issue.

``policy_issues`` is always passed explicitly (as the workflow does), except in
one test that monkeypatches the BigQuery-backed fallback, so nothing here
touches Google Cloud.
"""

from __future__ import annotations

import pytest

from claimdesk.contracts import DocumentChecklist, EvidenceDecision
from claimdesk.rules import evidence_rules
from claimdesk.rules.evidence_rules import DOCUMENTS, REQUIRED_BY_TYPE, apply_evidence_rules, build_checklist, merge_server_evidence

FLOOD_REQUIRED = [k for k, p in REQUIRED_BY_TYPE["home_flood"] if p == "required"]  # photo, water line, inventory

BASE_FACTS = {
    "policyholder_name": "Andre Thibodeaux",
    "policy_number": "FLD-LA-9R3T6W",
    "contact_method": "504-555-0199",
    "date_of_loss": "2025-06-02",
    "loss_address_or_city": "Metairie, LA",
    "loss_state": "LA",
    "loss_zip_code": "70001",
    "loss_description": "Street flooding came in under the front door, 6 inches in the living room.",
    "water_entry_description": "street flood water under the front door",
    "estimated_loss_usd": 18000,
    "summary": "Street flooding entered the home. Nobody was hurt.",
}
VALID_FIELDS = {"intake_status": "valid", "missing_fields": [], "warnings": []}
MISSING_FIELDS = {"intake_status": "missing_info", "missing_fields": ["policy_number"], "warnings": []}
FLOOD = {"claim_type": "home_flood", "severity": "medium", "severity_rationale": "t", "water_source": "surface_flood"}
FLOOD_WATER = {"water_source": "surface_flood", "nfip_flood_candidate": True, "explanation": "t"}
SUMP_WATER = {"water_source": "sump_pump_failure", "nfip_flood_candidate": False, "explanation": "t", "claimant_message": "..."}
UNKNOWN_WATER = {"water_source": "unknown", "nfip_flood_candidate": False, "explanation": "t", "claimant_message": "?"}


def all_flood_docs_received(**extra) -> dict:
    """Facts whose required flood documents were captured by the server."""

    return merge_server_evidence({**BASE_FACTS, **extra}, [{"id": f"ev-{k}", "document_types": [k]} for k in FLOOD_REQUIRED])


def decide(facts=None, fields=VALID_FIELDS, classification=FLOOD, water=FLOOD_WATER, policy_issues=()):
    result = apply_evidence_rules(facts if facts is not None else BASE_FACTS, fields, classification, water, policy_issues=list(policy_issues))
    EvidenceDecision.model_validate(result)
    return result


def rule_ids(result) -> list[str]:
    return [f["rule_id"] for f in result["findings"]]


# ---------------------------------------------------------------------------
# 1. Server-only "received"
# ---------------------------------------------------------------------------
def test_llm_cannot_mark_evidence_received():
    facts = {**BASE_FACTS, "evidence_records": [{"document_type": "damage_photo", "status": "received", "evidence_ids": ["made-up-by-llm"]}]}
    merged = merge_server_evidence(facts, [])
    record = merged["evidence_records"][0]
    assert record["status"] == "available"  # downgraded
    assert record["evidence_ids"] == []  # LLM-invented ids are stripped


def test_server_capture_adds_received_record():
    merged = merge_server_evidence(BASE_FACTS, [{"id": "cap-123", "document_types": ["water_line_photo", "damage_photo"]}])
    assert merged["evidence_records"] == [
        {"document_type": "water_line_photo", "status": "received", "evidence_ids": ["cap-123"], "source_turn_ids": []},
        {"document_type": "damage_photo", "status": "received", "evidence_ids": ["cap-123"], "source_turn_ids": []},
    ]


def test_server_capture_with_unknown_document_type_is_ignored():
    merged = merge_server_evidence(BASE_FACTS, [{"id": "cap-9", "document_types": ["selfie", "damage_photo"]}])
    assert [r["document_type"] for r in merged["evidence_records"]] == ["damage_photo"]


def test_merge_keeps_other_facts_unchanged():
    merged = merge_server_evidence(BASE_FACTS, None)
    assert {k: merged[k] for k in BASE_FACTS} == BASE_FACTS


def test_available_is_not_enough_to_satisfy_a_required_document():
    facts = merge_server_evidence({**BASE_FACTS, "evidence_records": [{"document_type": k, "status": "available"} for k in FLOOD_REQUIRED]}, [])
    result = decide(facts)
    assert result["routing_decision"] == "needs_docs"
    assert rule_ids(result).count("DOC-001") == 3


def test_checklist_marks_only_server_captures_as_provided():
    facts = merge_server_evidence(
        {**BASE_FACTS, "evidence_records": [{"document_type": "water_line_photo", "status": "available"}]},
        [{"id": "cap-1", "document_types": ["damage_photo"]}],
    )
    checklist = build_checklist(facts, FLOOD, FLOOD_WATER)
    DocumentChecklist.model_validate(checklist)
    items = {i["document_type"]: i for i in checklist["items"]}
    assert [i["document_type"] for i in checklist["items"]] == [k for k, _ in REQUIRED_BY_TYPE["home_flood"]]
    photo = items["damage_photo"]
    assert (photo["status"], photo["already_provided"], photo["evidence_ids"]) == ("received", True, ["cap-1"])
    assert items["water_line_photo"]["status"] == "available" and items["water_line_photo"]["already_provided"] is False
    assert items["contents_inventory"]["status"] == "unknown" and items["contents_inventory"]["evidence_ids"] == []


def test_received_capture_wins_over_a_later_llm_record_for_the_same_document():
    facts = merge_server_evidence({**BASE_FACTS, "evidence_records": [{"document_type": "damage_photo", "status": "missing"}]}, [{"id": "cap-2", "document_types": ["damage_photo"]}])
    item = next(i for i in build_checklist(facts, FLOOD, FLOOD_WATER)["items"] if i["document_type"] == "damage_photo")
    assert item["status"] == "received" and item["already_provided"] is True


# ---------------------------------------------------------------------------
# 2. Route precedence: emergency > human_triage > policy_review > needs_docs > ready
# ---------------------------------------------------------------------------
def test_ready_for_adjuster_when_everything_is_present():
    result = decide(all_flood_docs_received())
    assert result["routing_decision"] == "ready_for_adjuster"
    assert result["findings"] == []
    assert result["required_documents"] == []


def test_missing_documents_route_to_needs_docs():
    result = decide()
    assert result["routing_decision"] == "needs_docs"
    assert rule_ids(result) == ["DOC-001", "DOC-001", "DOC-001"]
    assert result["required_documents"] == [DOCUMENTS[k][0] for k in FLOOD_REQUIRED]
    assert all(f["action"] == "collect_document" for f in result["findings"])


def test_missing_fields_alone_route_to_needs_docs():
    result = decide(all_flood_docs_received(), fields=MISSING_FIELDS)
    assert result["routing_decision"] == "needs_docs"
    assert rule_ids(result) == ["INTAKE-001"]


def test_policy_issue_beats_needs_docs():
    result = decide(fields=MISSING_FIELDS, policy_issues=["Loss date falls outside the recorded policy term"])
    assert result["routing_decision"] == "policy_review"
    policy = [f for f in result["findings"] if f["rule_id"] == "POLICY-001"]
    assert policy == [{"rule_id": "POLICY-001", "severity": "high", "message": "Loss date falls outside the recorded policy term", "action": "adjuster_review", "document": None}]


def test_human_triage_beats_policy_review():
    # Internal water: policy issues are not even evaluated (not a flood file).
    result = decide(classification={**FLOOD, "claim_type": "internal_water"}, water=SUMP_WATER, policy_issues=["Policy number needs verification"])
    assert result["routing_decision"] == "human_triage"
    assert "POLICY-001" not in rule_ids(result)


def test_out_of_scope_goes_to_human_triage():
    result = decide(classification={**FLOOD, "claim_type": "out_of_scope"}, water=UNKNOWN_WATER)
    assert result["routing_decision"] == "human_triage"
    assert "SCOPE-001" in rule_ids(result)
    # Out of scope only *recommends* a photo, so no DOC-001 findings.
    assert "DOC-001" not in rule_ids(result)


@pytest.mark.parametrize("category", ["electrical", "gas", "injury", "medical", "unsafe_housing", "rising_water"])
def test_present_immediate_hazard_beats_everything(category):
    facts = {**BASE_FACTS, "safety_facts": [{"category": category, "status": "present", "description": "Hazard present now"}]}
    result = decide(facts, fields=MISSING_FIELDS, classification={**FLOOD, "claim_type": "out_of_scope"}, water=UNKNOWN_WATER)
    assert result["routing_decision"] == "emergency_escalation"
    safe = next(f for f in result["findings"] if f["rule_id"] == "SAFE-001")
    assert safe["severity"] == "urgent" and safe["action"] == "emergency_escalation"
    assert "SAFE-002" not in rule_ids(result)  # the urgent finding already covers it


def test_gas_smell_present_escalates_on_a_clean_flood_file():
    facts = {**all_flood_docs_received(), "safety_facts": [{"category": "gas", "status": "present", "description": "Strong gas smell in the kitchen"}]}
    assert decide(facts)["routing_decision"] == "emergency_escalation"


@pytest.mark.parametrize("category", ["Unsafe structure", "structural", "Unsafe-Housing", " ELECTRICAL ", "gas leak", "trapped"])
def test_category_spellings_are_normalized(category):
    facts = {**all_flood_docs_received(), "safety_facts": [{"category": category, "status": "present", "description": "x"}]}
    assert decide(facts)["routing_decision"] == "emergency_escalation"
    assert evidence_rules.safety_category(category) in evidence_rules.IMMEDIATE_HAZARD_CATEGORIES


@pytest.mark.parametrize(
    "fact",
    [
        {"category": "electrical", "status": "uncertain", "description": "Not sure if the outlets are still live"},
        {"category": "gas", "status": "uncertain", "description": "Maybe a faint smell"},
        {"category": "mold", "status": "present", "description": "Some mold on the drywall"},
        {"category": "sewage", "status": "present", "description": "Sewage smell in the basement"},
        {"category": "other", "status": "present", "description": "Wet insulation on the floor"},
    ],
)
def test_uncertain_or_non_immediate_hazards_are_a_soft_note_only(fact):
    # A routine "there's some mold" must not page an emergency team. It is
    # recorded as SAFE-002 (soft_signal) and the normal route stands.
    result = decide({**all_flood_docs_received(), "safety_facts": [fact]})
    assert result["routing_decision"] == "ready_for_adjuster"
    note = next(f for f in result["findings"] if f["rule_id"] == "SAFE-002")
    assert note["action"] == "soft_signal" and note["severity"] == "medium"
    assert "SAFE-001" not in rule_ids(result)


def test_absent_hazard_is_not_an_emergency():
    facts = {**all_flood_docs_received(), "safety_facts": [{"category": "injury", "status": "absent", "description": "Nobody was hurt"}]}
    result = decide(facts)
    assert result["routing_decision"] == "ready_for_adjuster"
    assert result["findings"] == []  # absent hazards leave no note at all


def test_full_precedence_ladder():
    """Remove one trigger at a time and watch the route step down the ladder."""

    danger = [{"category": "gas", "status": "present", "description": "Smells gas"}]
    kwargs = dict(fields=MISSING_FIELDS, policy_issues=["Policy number needs verification"])
    assert decide({**BASE_FACTS, "safety_facts": danger}, classification={**FLOOD, "claim_type": "out_of_scope"}, water=UNKNOWN_WATER, **kwargs)["routing_decision"] == "emergency_escalation"
    assert decide(BASE_FACTS, classification={**FLOOD, "claim_type": "out_of_scope"}, water=UNKNOWN_WATER, **kwargs)["routing_decision"] == "human_triage"
    assert decide(BASE_FACTS, **kwargs)["routing_decision"] == "policy_review"
    assert decide(BASE_FACTS, fields=MISSING_FIELDS)["routing_decision"] == "needs_docs"
    assert decide(all_flood_docs_received())["routing_decision"] == "ready_for_adjuster"


# ---------------------------------------------------------------------------
# 3. The water-source decision overrides the LLM's claim type
# ---------------------------------------------------------------------------
def test_home_flood_with_non_flood_water_becomes_internal_water():
    result = decide(all_flood_docs_received(), classification=FLOOD, water=SUMP_WATER, policy_issues=["Policy number needs verification"])
    assert result["routing_decision"] == "human_triage"
    assert "SCOPE-002" in rule_ids(result)
    assert "POLICY-001" not in rule_ids(result)  # policy gate is skipped for internal water
    assert "Classified as internal_water (medium); water source sump_pump_failure." in result["audit_trail"]
    # Internal-water document list applies: third-party report is recommended, not required.
    assert result["required_documents"] == []  # damage_photo already received
    assert any("homeowners policy" in note for note in result["coverage_considerations"])


def test_home_flood_with_unknown_water_stays_home_flood():
    result = decide(BASE_FACTS, water=UNKNOWN_WATER)
    assert "SCOPE-002" not in rule_ids(result)
    assert "Classified as home_flood (medium); water source unknown." in result["audit_trail"]
    assert result["routing_decision"] == "needs_docs"


def test_checklist_follows_the_override_too():
    checklist = build_checklist(BASE_FACTS, FLOOD, SUMP_WATER)
    assert [i["document_type"] for i in checklist["items"]] == [k for k, _ in REQUIRED_BY_TYPE["internal_water"]]


def test_unclear_claims_use_the_unclear_document_list_and_policy_gate():
    result = decide(classification={**FLOOD, "claim_type": "unclear"}, water=UNKNOWN_WATER, policy_issues=["Confirm an exact loss date for policy review"])
    assert result["routing_decision"] == "policy_review"
    assert rule_ids(result) == ["DOC-001", "POLICY-001"]  # only damage_photo is required for unclear


def test_policy_lookup_fallback_only_runs_for_flood_or_unclear(monkeypatch):
    calls = []
    monkeypatch.setattr(evidence_rules, "review_policy_against_claim", lambda claim: calls.append(claim.policy_number) or ["Policy number needs verification"])
    flood = apply_evidence_rules(BASE_FACTS, VALID_FIELDS, FLOOD, FLOOD_WATER)  # policy_issues omitted -> lookup
    assert calls == ["FLD-LA-9R3T6W"] and flood["routing_decision"] == "policy_review"
    apply_evidence_rules(BASE_FACTS, VALID_FIELDS, {**FLOOD, "claim_type": "internal_water"}, SUMP_WATER)
    assert calls == ["FLD-LA-9R3T6W"]  # not called again for internal water


# ---------------------------------------------------------------------------
# 4. Review notes never decide coverage
# ---------------------------------------------------------------------------
def test_coverage_considerations_always_end_with_no_decision_note():
    for classification, water in [(FLOOD, FLOOD_WATER), (FLOOD, SUMP_WATER), ({**FLOOD, "claim_type": "out_of_scope"}, UNKNOWN_WATER)]:
        notes = decide(classification=classification, water=water)["coverage_considerations"]
        assert notes[-1].startswith("No coverage, payment or liability is confirmed at intake.")


def test_basement_note_only_for_basement_floods():
    basement = {**BASE_FACTS, "loss_description": "Water filled the basement to 2 feet."}
    assert any("Basement losses" in n for n in decide(basement)["coverage_considerations"])
    assert not any("Basement losses" in n for n in decide()["coverage_considerations"])


def test_required_documents_are_deduplicated_labels():
    result = decide()
    assert len(result["required_documents"]) == len(set(result["required_documents"]))
    assert set(result["required_documents"]) <= {label for label, _ in DOCUMENTS.values()}
