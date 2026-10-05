"""Rule step 4: timing, high-loss, safety, SIU and weather signals.

Thresholds come from REAL data (``data_access/loss_benchmarks.py``) instead of
the guessed constants in the original app. If benchmarks are unavailable the
``FALLBACK_*`` values below are used and the audit trail says so.

Signal glossary
    TIMING-001  stated report date before the loss date           -> SIU review
    TIMING-002  reported later than 95% of real claims in state  -> SIU review
    LOSS-001    estimate above the state's 95th percentile       -> adjuster review
    EVID-001    estimate above the 90th percentile, zero evidence -> SIU review
    FACTS-001   vague language ("not sure", "maybe")             -> soft signal
    CORROB-001  no NOAA flood/rain event nearby                  -> soft signal ONLY
    SAFETY-001  immediate hazard escalated by the evidence step   -> emergency
                (SAFE-001 only; SAFE-002 soft notes never escalate)
    INTAKE-002  core loss facts missing                           -> soft signal

"SIU" = Special Investigations Unit. Routing a file there is NOT an accusation;
it means a specialist takes a second look.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..contracts import BenchmarkResult, ClaimClassification, ClaimFacts, EvidenceDecision, FieldCheck, RiskGate, RuleFinding, WaterSourceDecision, WeatherCheckResult
from ._helpers import as_model, has_any, parse_date

FALLBACK_DAMAGE_P90_USD = 50_000.0
FALLBACK_DAMAGE_P95_USD = 100_000.0
FALLBACK_REPORT_LAG_P95_DAYS = 90.0

def score_risk_signals(
    claim_value: Any,
    field_check_value: Any,
    classification_value: Any,
    water_value: Any,
    evidence_value: Any,
    *,
    benchmark: Any,
    weather: Any = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Compute signals and the final routing decision.

    ``benchmark`` and ``weather`` are *passed in* (already fetched from
    BigQuery by the workflow's parallel "real-world context" branch) instead
    of being looked up here. That keeps this function pure: unit tests just
    pass tiny fake values, no database needed. ``weather`` is ``None`` when no
    check was run (e.g. not a flood claim, or no ZIP code yet).
    """

    claim = as_model(ClaimFacts, claim_value)
    field_check = as_model(FieldCheck, field_check_value)
    classification = as_model(ClaimClassification, classification_value)
    water = as_model(WaterSourceDecision, water_value)
    evidence = as_model(EvidenceDecision, evidence_value)
    today = today or date.today()

    signals: list[RuleFinding] = []
    audit: list[str] = list(evidence.audit_trail)

    def signal(rule_id: str, severity: str, message: str, action: str) -> None:
        signals.append(RuleFinding(rule_id=rule_id, severity=severity, message=message, action=action))  # type: ignore[arg-type]

    # --- Benchmarks (real NFIP data) ---------------------------------------
    # ``None`` (direct callers) is treated like "unavailable". A value of 0 is
    # never a meaningful threshold, so it also falls back to the defaults.
    benchmark = as_model(BenchmarkResult, benchmark or {"available": False})
    p90 = benchmark.damage_p90_usd if benchmark.available and benchmark.damage_p90_usd else FALLBACK_DAMAGE_P90_USD
    p95 = benchmark.damage_p95_usd if benchmark.available and benchmark.damage_p95_usd else FALLBACK_DAMAGE_P95_USD
    lag95 = benchmark.report_lag_p95_days if benchmark.available and benchmark.report_lag_p95_days else FALLBACK_REPORT_LAG_P95_DAYS
    audit.append(
        f"Thresholds from NFIP claims benchmark ({benchmark.state}, n={benchmark.sample_size}): p90=${p90:,.0f}, p95=${p95:,.0f}, report-lag p95={lag95:.0f} days."
        if benchmark.available
        else "Benchmarks unavailable; used conservative fallback thresholds."
    )

    # --- Timing ---------------------------------------------------------------
    loss_date = parse_date(claim.date_of_loss)
    stated_report_date = parse_date(claim.reported_date)
    report_date = stated_report_date or today  # reporting = today's call if not stated
    # TIMING-001 needs an explicitly stated report date. A loss date in the
    # future with no report date is far more likely a slip of the tongue; the
    # required-fields step already asks the agent to confirm the date.
    if loss_date and stated_report_date and stated_report_date < loss_date:
        signal("TIMING-001", "high", "Reported date is before the loss date.", "siu_review")
    if loss_date and (report_date - loss_date).days > lag95:
        signal("TIMING-002", "medium", f"Reported {(report_date - loss_date).days} days after the loss; 95% of similar claims are reported within {lag95:.0f} days.", "siu_review")

    # --- Amounts --------------------------------------------------------------
    amount = claim.estimated_loss_usd
    if amount is not None and amount > p95:
        # Informational for the adjuster; does not change the route by itself.
        signal("LOSS-001", "high", f"Estimate ${amount:,.0f} is above the 95th percentile (${p95:,.0f}) of real claims in this state.", "adjuster_review")
    has_any_evidence = any(r.status in {"available", "received"} for r in claim.evidence_records)
    if amount is not None and amount > p90 and not has_any_evidence:
        signal("EVID-001", "high", "High estimate with no photos or documents available yet.", "siu_review")

    # --- Language -------------------------------------------------------------
    text = f"{claim.loss_description} {claim.summary}"
    if has_any(text, [r"\bnot sure\b", r"\bdon'?t remember\b", r"\bmaybe\b", r"\bno idea\b"]):
        signal("FACTS-001", "medium", "Some key facts are uncertain and need follow-up.", "soft_signal")

    # --- NOAA weather corroboration (soft) -------------------------------------
    weather_result = as_model(WeatherCheckResult, weather) if weather else None
    if weather_result and weather_result.checked and classification.claim_type == "home_flood" and water.nfip_flood_candidate:
        if weather_result.events_found == 0:
            signal("CORROB-001", "low", weather_result.note, "soft_signal")
        audit.append(f"NOAA check: {weather_result.note}")

    # --- Safety + missing core facts --------------------------------------------
    if evidence.routing_decision == "emergency_escalation":
        signal("SAFETY-001", "urgent", "Safety, injury or habitability issue requires immediate human review.", "emergency_escalation")
    if any(f in field_check.missing_fields for f in ("date_of_loss", "loss_address_or_city", "loss_description")):
        signal("INTAKE-002", "medium", "Core loss facts are missing; keep the conversation follow-up oriented.", "soft_signal")

    # --- Final routing (order = priority) -----------------------------------------
    #   emergency_escalation > human_triage > special_investigation > evidence route
    # human_triage (not a flood-desk claim) already sends the file to a person
    # who sees every signal, so SIU signals do not override it.
    actions = {s.action for s in signals}
    if "emergency_escalation" in actions:
        final = "emergency_escalation"
    elif evidence.routing_decision == "human_triage":
        final = "human_triage"
    elif "siu_review" in actions:
        final = "special_investigation"
    else:
        final = evidence.routing_decision
    audit.append(f"Final route: {final}.")

    return RiskGate(final_routing_decision=final, signals=signals, benchmark=benchmark, weather=weather_result, audit_trail=audit).model_dump()  # type: ignore[arg-type]


__all__ = ["score_risk_signals", "FALLBACK_DAMAGE_P90_USD", "FALLBACK_DAMAGE_P95_USD", "FALLBACK_REPORT_LAG_P95_DAYS"]
