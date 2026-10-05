"""Layer 1: unit tests for ``claimdesk/rules/risk_signals.py``.

This is the LAST rule step. It adds signals on top of the evidence decision
and computes the FINAL route. Signal glossary (from the module docstring):

    TIMING-001  reported before the loss date            -> siu_review
    TIMING-002  reported later than p95 of real claims   -> siu_review
    LOSS-001    estimate above p95                        -> adjuster_review (no route change)
    EVID-001    estimate above p90 with zero evidence     -> siu_review
    FACTS-001   vague language                            -> soft_signal
    CORROB-001  NOAA found no flood/rain event nearby     -> soft_signal ONLY
    SAFETY-001  evidence step escalated                   -> emergency_escalation
    INTAKE-002  core loss facts missing                   -> soft_signal

Final route precedence::

    emergency_escalation > special_investigation (any siu_review) > evidence route

Thresholds come from a BigQuery benchmark (real NFIP claims). Here the
benchmark and NOAA weather results are *passed in* as tiny fake dicts, which
is exactly why the function was written to be pure: no database needed.
``today`` is injected so "days since loss" is deterministic.
"""

from __future__ import annotations

from datetime import date

import pytest

from claimdesk.contracts import RiskGate
from claimdesk.rules.risk_signals import FALLBACK_DAMAGE_P90_USD, FALLBACK_DAMAGE_P95_USD, FALLBACK_REPORT_LAG_P95_DAYS, score_risk_signals

TODAY = date(2025, 9, 1)
BENCHMARK = {"available": True, "state": "NC", "sample_size": 42000, "damage_p50_usd": 18000, "damage_p90_usd": 60000, "damage_p95_usd": 110000, "report_lag_p95_days": 30}
UNAVAILABLE = {"available": False, "note": "Benchmark service unavailable"}

FACTS = {
    "policyholder_name": "Keisha Brown",
    "date_of_loss": "2025-08-28",
    "reported_date": "2025-08-30",
    "loss_state": "NC",
    "loss_description": "River water came into the first floor.",
    "summary": "Neuse River flooding entered the home.",
    "estimated_loss_usd": 20000,
    "evidence_records": [{"document_type": "damage_photo", "status": "available"}],
}
FIELDS = {"intake_status": "valid", "missing_fields": [], "warnings": []}
FLOOD = {"claim_type": "home_flood", "severity": "medium", "severity_rationale": "t", "water_source": "surface_flood"}
FLOOD_WATER = {"water_source": "surface_flood", "nfip_flood_candidate": True, "explanation": "t"}


def evidence(route: str = "needs_docs") -> dict:
    return {"routing_decision": route, "audit_trail": ["Initial route: " + route + "."]}


def score(facts=None, *, fields=FIELDS, classification=FLOOD, water=FLOOD_WATER, route="needs_docs", benchmark=BENCHMARK, weather=None, **fact_overrides):
    result = score_risk_signals(
        {**(facts or FACTS), **fact_overrides}, fields, classification, water, evidence(route), benchmark=benchmark, weather=weather, today=TODAY
    )
    RiskGate.model_validate(result)
    return result


def ids(result) -> list[str]:
    return [s["rule_id"] for s in result["signals"]]


def test_clean_claim_keeps_the_evidence_route():
    result = score()
    assert result["signals"] == []
    assert result["final_routing_decision"] == "needs_docs"
    assert result["audit_trail"][0] == "Initial route: needs_docs."  # evidence audit is carried forward
    assert result["audit_trail"][-1] == "Final route: needs_docs."


# ---------------------------------------------------------------------------
# Benchmarks and fallbacks
# ---------------------------------------------------------------------------
def test_benchmark_thresholds_are_used_and_audited():
    result = score()
    assert "Thresholds from NFIP claims benchmark (NC, n=42000): p90=$60,000, p95=$110,000, report-lag p95=30 days." in result["audit_trail"]
    assert result["benchmark"]["available"] is True


def test_unavailable_benchmark_uses_fallbacks_and_says_so():
    result = score(benchmark=UNAVAILABLE)
    assert "Benchmarks unavailable; used conservative fallback thresholds." in result["audit_trail"]
    # Just above the fallback p95 -> LOSS-001; just at it -> nothing.
    assert "LOSS-001" in ids(score(benchmark=UNAVAILABLE, estimated_loss_usd=FALLBACK_DAMAGE_P95_USD + 1))
    assert "LOSS-001" not in ids(score(benchmark=UNAVAILABLE, estimated_loss_usd=FALLBACK_DAMAGE_P95_USD))


def test_fallback_report_lag_is_90_days():
    loss = date.fromordinal(TODAY.toordinal() - int(FALLBACK_REPORT_LAG_P95_DAYS) - 1).isoformat()
    assert "TIMING-002" in ids(score(benchmark=UNAVAILABLE, date_of_loss=loss, reported_date="not specified"))
    loss_ok = date.fromordinal(TODAY.toordinal() - int(FALLBACK_REPORT_LAG_P95_DAYS)).isoformat()
    assert "TIMING-002" not in ids(score(benchmark=UNAVAILABLE, date_of_loss=loss_ok, reported_date="not specified"))


def test_partial_benchmark_falls_back_per_threshold():
    partial = {**BENCHMARK, "damage_p95_usd": None}
    result = score(benchmark=partial, estimated_loss_usd=FALLBACK_DAMAGE_P95_USD + 1)
    assert "LOSS-001" in ids(result)
    assert f"p95=${FALLBACK_DAMAGE_P95_USD:,.0f}" in " ".join(result["audit_trail"])


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
def test_reported_before_loss_is_timing_001_and_siu():
    result = score(date_of_loss="2025-08-28", reported_date="2025-08-20")
    assert ids(result) == ["TIMING-001"]
    assert result["final_routing_decision"] == "special_investigation"


def test_late_report_is_timing_002():
    result = score(date_of_loss="2025-07-01", reported_date="2025-08-01")  # 31 days > p95 30
    assert ids(result) == ["TIMING-002"]
    assert "Reported 31 days after the loss" in result["signals"][0]["message"]
    assert result["final_routing_decision"] == "special_investigation"


def test_report_exactly_at_p95_is_not_late():
    assert ids(score(date_of_loss="2025-07-02", reported_date="2025-08-01")) == []  # 30 days


def test_missing_reported_date_means_today():
    # Reporting date defaults to the injected "today" (the day of the call).
    assert ids(score(date_of_loss="2025-07-15", reported_date="not specified")) == ["TIMING-002"]  # 48 days before TODAY
    assert ids(score(date_of_loss="2025-08-25", reported_date="not specified")) == []


def test_unparseable_loss_date_skips_timing_checks():
    assert ids(score(date_of_loss="last spring", reported_date="2025-08-30")) == []


def test_future_loss_date_without_reported_date_is_not_siu():
    # A mis-spoken future date with no stated report date is most likely a
    # slip; required_fields asks the agent to confirm it. No SIU signal.
    result = score(date_of_loss="2025-09-15", reported_date="not specified")
    assert ids(result) == []
    assert result["final_routing_decision"] != "special_investigation"


def test_stated_report_date_before_loss_date_is_timing_001():
    result = score(date_of_loss="2025-08-20", reported_date="2025-08-10")
    assert "TIMING-001" in ids(result)
    assert result["final_routing_decision"] == "special_investigation"


# ---------------------------------------------------------------------------
# Amounts and evidence
# ---------------------------------------------------------------------------
def test_loss_above_p95_is_adjuster_review_without_route_change():
    result = score(estimated_loss_usd=150000)
    assert ids(result) == ["LOSS-001"]
    assert result["signals"][0]["action"] == "adjuster_review"
    assert result["final_routing_decision"] == "needs_docs"


def test_high_loss_without_any_evidence_is_evid_001_siu():
    result = score(estimated_loss_usd=75000, evidence_records=[])  # > p90 60k, < p95
    assert ids(result) == ["EVID-001"]
    assert result["final_routing_decision"] == "special_investigation"


@pytest.mark.parametrize("status", ["available", "received"])
def test_any_available_or_received_evidence_prevents_evid_001(status):
    assert ids(score(estimated_loss_usd=75000, evidence_records=[{"document_type": "damage_photo", "status": status}])) == []


@pytest.mark.parametrize("status", ["missing", "planned", "unknown"])
def test_planned_or_missing_evidence_does_not_count(status):
    assert ids(score(estimated_loss_usd=75000, evidence_records=[{"document_type": "damage_photo", "status": status}])) == ["EVID-001"]


def test_very_high_loss_without_evidence_gets_both_loss_and_evid():
    result = score(estimated_loss_usd=250000, evidence_records=[])
    assert ids(result) == ["LOSS-001", "EVID-001"]
    assert result["final_routing_decision"] == "special_investigation"


def test_no_amount_means_no_amount_signals():
    assert ids(score(estimated_loss_usd=None, evidence_records=[])) == []


def test_fallback_p90_applies_to_evid_001():
    assert ids(score(benchmark=UNAVAILABLE, estimated_loss_usd=FALLBACK_DAMAGE_P90_USD + 1, evidence_records=[])) == ["EVID-001"]


# ---------------------------------------------------------------------------
# Soft signals never change the route
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("phrase", ["I'm not sure when", "I don't remember", "maybe two feet", "no idea how deep", "I dont remember"])
def test_vague_language_is_a_soft_signal(phrase):
    result = score(loss_description=f"Water came in, {phrase}.")
    assert ids(result) == ["FACTS-001"]
    assert result["signals"][0]["action"] == "soft_signal"
    assert result["final_routing_decision"] == "needs_docs"


def test_no_noaa_event_is_corrob_001_soft_only():
    weather = {"checked": True, "events_found": 0, "note": "No NOAA flood or heavy-rain events within 50 km and 3 days."}
    result = score(weather=weather, route="ready_for_adjuster")
    assert ids(result) == ["CORROB-001"]
    assert result["signals"][0] == {"rule_id": "CORROB-001", "severity": "low", "message": weather["note"], "action": "soft_signal", "document": None}
    assert result["final_routing_decision"] == "ready_for_adjuster"  # never a denial or SIU on its own
    assert f"NOAA check: {weather['note']}" in result["audit_trail"]


def test_noaa_events_found_adds_audit_but_no_signal():
    result = score(weather={"checked": True, "events_found": 2, "note": "2 NOAA events"})
    assert ids(result) == []
    assert "NOAA check: 2 NOAA events" in result["audit_trail"]


@pytest.mark.parametrize(
    "weather, classification, water",
    [
        (None, FLOOD, FLOOD_WATER),  # check not run
        ({"checked": False, "note": "Need a ZIP"}, FLOOD, FLOOD_WATER),  # check skipped
        ({"checked": True, "events_found": 0, "note": "none"}, {**FLOOD, "claim_type": "internal_water"}, FLOOD_WATER),
        ({"checked": True, "events_found": 0, "note": "none"}, FLOOD, {"water_source": "unknown", "nfip_flood_candidate": False, "explanation": "t"}),
    ],
)
def test_corroboration_only_for_checked_flood_claims(weather, classification, water):
    assert "CORROB-001" not in ids(score(weather=weather, classification=classification, water=water))


def test_missing_core_facts_is_intake_002_soft():
    fields = {"intake_status": "missing_info", "missing_fields": ["date_of_loss"], "warnings": []}
    result = score(fields=fields)
    assert ids(result) == ["INTAKE-002"]
    assert result["final_routing_decision"] == "needs_docs"


def test_non_core_missing_field_is_not_intake_002():
    fields = {"intake_status": "missing_info", "missing_fields": ["policy_number"], "warnings": []}
    assert ids(score(fields=fields)) == []


# ---------------------------------------------------------------------------
# Safety and final precedence
# ---------------------------------------------------------------------------
def test_safety_escalation_is_carried_as_safety_001():
    result = score(route="emergency_escalation")
    assert ids(result) == ["SAFETY-001"]
    assert result["final_routing_decision"] == "emergency_escalation"


def test_emergency_beats_siu():
    result = score(route="emergency_escalation", date_of_loss="2025-08-28", reported_date="2025-08-01", estimated_loss_usd=250000, evidence_records=[])
    assert {"TIMING-001", "EVID-001", "SAFETY-001"} <= set(ids(result))
    assert result["final_routing_decision"] == "emergency_escalation"


@pytest.mark.parametrize("route", ["policy_review", "needs_docs", "ready_for_adjuster"])
def test_siu_beats_flood_desk_evidence_routes(route):
    result = score(route=route, date_of_loss="2025-05-01", reported_date="2025-08-30")
    assert result["final_routing_decision"] == "special_investigation"


def test_human_triage_is_not_overridden_by_siu():
    # Out-of-scope / non-flood files already go to a person who sees every
    # signal; the SIU signal stays visible but does not change the route.
    result = score(route="human_triage", date_of_loss="2025-05-01", reported_date="2025-08-30")
    assert result["final_routing_decision"] == "human_triage"
    assert "TIMING-002" in ids(result)


@pytest.mark.parametrize("route", ["human_triage", "policy_review", "needs_docs", "ready_for_adjuster"])
def test_without_signals_the_evidence_route_is_final(route):
    assert score(route=route)["final_routing_decision"] == route


def test_missing_benchmark_uses_fallbacks():
    # Direct callers may pass None; it is treated like "unavailable".
    result = score_risk_signals(FACTS, FIELDS, FLOOD, FLOOD_WATER, evidence(), benchmark=None, today=TODAY)
    assert any("fallback" in line for line in result["audit_trail"])


def test_soft_safety_note_from_evidence_step_does_not_become_safety_001():
    # Mold / uncertain hazards are SAFE-002 soft notes in the evidence step
    # (rules/evidence_rules.py), so the evidence route is NOT emergency and
    # the risk gate must not invent a SAFETY-001 escalation either.
    from claimdesk.rules.evidence_rules import apply_evidence_rules

    facts = {**FACTS, "safety_facts": [{"category": "mold", "status": "present", "description": "Mold on the drywall"}]}
    decision = apply_evidence_rules(facts, FIELDS, FLOOD, FLOOD_WATER, policy_issues=[])
    assert decision["routing_decision"] != "emergency_escalation"
    result = score_risk_signals(facts, FIELDS, FLOOD, FLOOD_WATER, decision, benchmark=BENCHMARK, today=TODAY)
    assert "SAFETY-001" not in ids(result)
    assert result["final_routing_decision"] != "emergency_escalation"
