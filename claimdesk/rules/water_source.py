"""Rule step 2: where did the water come from? (NEW in v2)

WHY THIS MATTERS
    The National Flood Insurance Program covers *flood*: rising surface water
    that inundates normally dry land (river/stream overflow, accumulated
    rainfall, storm surge, mudflow). It generally does NOT cover water that
    starts inside the building or comes up through the plumbing. FEMA's own
    claims data proves it - claims closed without payment carry reason codes
    such as **02 "Seepage"**, **03 "Backup drains"** and **16 "Not insured,
    wind damage"** (see docs/data_dictionary.md).

    Those losses are usually handled under a *homeowners* policy (often via a
    water-backup endorsement). ClaimDesk v2 is a flood desk, so it:
      1. recognises the non-flood water sources,
      2. explains the difference politely WITHOUT promising or denying
         coverage, and
      3. routes the file to ``human_triage``.

HOW THE DECISION IS MADE
    * Primary: the ClassifyClaim LLM step returns ``water_source``.
    * Backup: if the LLM said "unknown", keyword patterns on the claimant's
      own words are used - but only if exactly one source matches. Mixed
      signals (e.g. "the river rose and the sump failed") stay "unknown" and
      are left to the adjuster, because the real cause decides coverage.
    * The backup is skipped entirely when the classifier's own rationale says
      the source is mixed or uncertain ("both a street flood and a drain
      backup"). The classifier read the whole story; a single keyword must
      not overrule its "I can't tell".

WHAT IS *NOT* A SURFACE-FLOOD KEYWORD (on purpose)
    "hurricane" and "tropical storm" name a storm, not a water source: a
    hurricane can cause wind-driven rain through the roof (not flood) just as
    well as rising water (flood) - see the skill's water_sources.md. A bare
    "surge" also matches "power surge". Only "storm surge" (coastal flood
    water pushed ashore) counts.
"""

from __future__ import annotations

import re
from typing import Any

from ..contracts import ClaimClassification, ClaimFacts, WaterSourceDecision
from ._helpers import as_model, has_any

KEYWORDS: dict[str, list[str]] = {
    "surface_flood": [
        r"\briver\b", r"\bcreek\b", r"\bbayou\b", r"\bstream\b", r"\blake\b", r"\bstorm surge\b",
        r"street (was )?flood", r"street (was )?under .*water", r"yard (was )?(flood|under .*water)",
        r"rising water", r"flash flood", r"\boverflow", r"water came in from (the )?outside",
        r"\bmudflow\b", r"\bmudslide\b", r"\bmud\b.*(water|rain|hill|burn)",
        r"rain(water)? (poured|came) in under the door",
    ],
    "sump_pump_failure": [r"\bsump\b"],
    "sewer_or_drain_backup": [r"back(ed)?[ -]?up", r"\bsewer\b", r"floor drain", r"\btoilet\b"],
    "internal_plumbing": [r"\bpipe\b", r"\bburst\b", r"water heater", r"washing machine", r"dishwasher", r"\bleak(ing|ed)?\b"],
    "seepage": [r"\bseep"],
    "roof_or_wind_driven_rain": [r"\broof\b", r"wind[- ]driven", r"blew in", r"window (broke|blew)"],
}

FRIENDLY: dict[str, str] = {
    "sump_pump_failure": "a sump pump failure",
    "sewer_or_drain_backup": "a sewer or drain backup",
    "internal_plumbing": "a plumbing or appliance leak",
    "seepage": "groundwater seepage",
    "roof_or_wind_driven_rain": "rain entering through the roof or windows",
}


# Words in the classifier's water_source_rationale that mean "I saw more than
# one possible source" or "I can't tell" -> keep "unknown", skip keywords.
_MIXED_RATIONALE = re.compile(
    r"\b(mixed|multiple|both|several|more than one|two (possible )?sources|uncertain|unclear|not clear|ambiguous|conflicting)\b",
    re.IGNORECASE,
)


def _keyword_sources(text: str) -> set[str]:
    return {source for source, patterns in KEYWORDS.items() if has_any(text, patterns)}


def decide_water_source(claim_value: Any, classification_value: Any) -> dict[str, Any]:
    claim = as_model(ClaimFacts, claim_value)
    classification = as_model(ClaimClassification, classification_value)
    source = classification.water_source
    rationale = classification.water_source_rationale
    how = f"Classifier: {rationale or 'no rationale given'}"

    if source == "unknown" and _MIXED_RATIONALE.search(rationale):
        how = f"Mixed water-source signals (classifier: {rationale}); adjuster must determine the cause"
    elif source == "unknown":
        text = " ".join([claim.water_entry_description, claim.loss_description, claim.summary])
        matches = _keyword_sources(text)
        if len(matches) == 1:
            source = matches.pop()  # type: ignore[assignment]
            how = f"Keyword fallback matched '{source}' in the claimant's description"
        elif len(matches) > 1:
            how = f"Mixed water-source signals ({', '.join(sorted(matches))}); adjuster must determine the cause"

    if source == "surface_flood":
        return WaterSourceDecision(water_source=source, nfip_flood_candidate=True, explanation=how).model_dump()
    if source == "unknown":
        return WaterSourceDecision(
            water_source=source,
            nfip_flood_candidate=False,
            explanation=how,
            claimant_message="Can you tell me where the water came in - from outside, like a river or the street, or from inside, like a drain, pipe or sump pump?",
        ).model_dump()
    return WaterSourceDecision(
        water_source=source,
        nfip_flood_candidate=False,
        explanation=how,
        claimant_message=(
            f"From what you've described, the water came from {FRIENDLY.get(source, source)}. "
            "Flood insurance generally applies to rising water from outside, so this kind of loss is usually "
            "reviewed under a homeowners policy instead. I'll write everything down and a person will review "
            "which policy applies. I can't confirm coverage either way."
        ),
    ).model_dump()


__all__ = ["decide_water_source", "KEYWORDS"]
