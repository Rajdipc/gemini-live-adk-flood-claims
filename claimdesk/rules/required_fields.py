"""Rule step 1: are the minimum first-notice-of-loss (FNOL) facts present?

An adjuster cannot start work without knowing WHO (name, policy, contact),
WHEN (date of loss), WHERE (address/city) and WHAT (description). Anything
missing here becomes a "red blank" in the notebook and a follow-up question.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..contracts import ClaimFacts, FieldCheck
from ._helpers import as_model, dedupe, is_blank, parse_date

# Field name -> the friendly question the voice agent can ask.
BLOCKING_FIELD_QUESTIONS: dict[str, str] = {
    "policyholder_name": "What is your full name as it appears on the policy?",
    "policy_number": "What is your flood policy number? It's on your declarations page.",
    "contact_method": "What is the best phone number or email for the adjuster to reach you?",
    "date_of_loss": "On what date did the water first enter the building?",
    "loss_address_or_city": "What is the address of the property that flooded?",
    "loss_description": "Can you briefly describe what happened?",
}


def check_required_fields(claim_value: Any, *, today: date | None = None) -> dict[str, Any]:
    """Return a ``FieldCheck`` (as a dict, the format ADK state stores)."""

    claim = as_model(ClaimFacts, claim_value)
    today = today or date.today()
    missing: list[str] = [name for name in BLOCKING_FIELD_QUESTIONS if is_blank(getattr(claim, name))]
    warnings: list[str] = []

    for name in ("date_of_loss", "reported_date"):
        value = getattr(claim, name)
        if is_blank(value):
            continue
        parsed = parse_date(value)
        label = name.replace("_", " ")
        if parsed is None:
            missing.append(f"Confirm a valid calendar date for {label}")
        elif parsed > today:
            missing.append(f"Confirm the future date given for {label}")

    if is_blank(claim.loss_zip_code):
        warnings.append("ZIP code not captured yet; it is needed to check NOAA weather records.")
    if claim.estimated_loss_usd is None:
        warnings.append("Estimated loss amount was not supplied.")

    missing.extend(claim.missing_or_uncertain_facts)
    missing = dedupe(missing)
    return FieldCheck(intake_status="missing_info" if missing else "valid", missing_fields=missing, warnings=dedupe(warnings)).model_dump()


__all__ = ["BLOCKING_FIELD_QUESTIONS", "check_required_fields"]
