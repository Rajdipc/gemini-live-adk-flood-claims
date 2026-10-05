"""Layer 1: unit tests for ``claimdesk/rules/water_source.py``.

WHY THIS RULE EXISTS
    NFIP flood insurance covers *rising surface water from outside*. Sump pump
    failures, sewer/drain backups, burst pipes, seepage and wind-driven rain
    are generally NOT flood (FEMA non-payment codes 02, 03, 16...). The rule
    turns the classifier's opinion into a deterministic, auditable decision.

THE CONTRACT WE PIN DOWN HERE
    1. The LLM classifier is the PRIMARY source. Its answer is used as-is,
       even if the claimant's words contain other keywords.
    2. Only when the classifier says ``unknown`` do keyword patterns run, and
       they are used only when EXACTLY ONE water source matches. Mixed signals
       stay ``unknown`` so a human decides (the true cause decides coverage).
       If the classifier's rationale itself says "mixed"/"uncertain", the
       keyword fallback is skipped. Storm names ("hurricane") are not keywords.
    3. Only ``surface_flood`` is an NFIP flood candidate.
    4. ``claimant_message`` (what the voice agent may say) never promises or
       denies coverage.
"""

from __future__ import annotations

import re

import pytest

from claimdesk.contracts import WaterSourceDecision
from claimdesk.rules.water_source import FRIENDLY, KEYWORDS, decide_water_source

NON_FLOOD_SOURCES = ["sump_pump_failure", "sewer_or_drain_backup", "internal_plumbing", "seepage", "roof_or_wind_driven_rain"]

# Phrases that would amount to a coverage promise or denial. The agent must
# never say these at intake - only a licensed adjuster can decide coverage.
PROMISE_OR_DENIAL = re.compile(
    r"\b(you are|you're|you will be|this is|it is|it's) (fully |definitely )?(covered|not covered|denied|approved)\b"
    r"|\bwe will (pay|reimburse|cover)\b|\bguarantee|\bwill be paid\b|\bclaim (is|has been) (approved|denied)\b",
    re.IGNORECASE,
)


def classification(source: str = "unknown", rationale: str = "", claim_type: str = "home_flood") -> dict:
    return {"claim_type": claim_type, "severity": "medium", "severity_rationale": "test", "water_source": source, "water_source_rationale": rationale}


def facts(water_entry: str = "not specified", description: str = "not specified", summary: str = "not specified") -> dict:
    return {"water_entry_description": water_entry, "loss_description": description, "summary": summary}


# ---------------------------------------------------------------------------
# 1. The classifier is primary
# ---------------------------------------------------------------------------
def test_surface_flood_from_classifier_is_an_nfip_candidate():
    result = decide_water_source(facts("the creek came over its banks"), classification("surface_flood", "creek overflow"))
    WaterSourceDecision.model_validate(result)
    assert result["water_source"] == "surface_flood"
    assert result["nfip_flood_candidate"] is True
    assert result["explanation"] == "Classifier: creek overflow"
    assert result["claimant_message"] == ""  # nothing special to tell a flood claimant


def test_classifier_wins_over_contradicting_keywords():
    # The claimant mentions a sump pump, but the classifier read the whole
    # story and decided surface flood. Keywords must NOT override it.
    result = decide_water_source(facts("the sump pump was running but the river came in the front door"), classification("surface_flood"))
    assert result["water_source"] == "surface_flood"
    assert result["explanation"] == "Classifier: no rationale given"


def test_classifier_non_flood_answer_is_kept_even_if_text_says_river():
    result = decide_water_source(facts("near the river, but the sump pump died"), classification("sump_pump_failure", "pump failed"))
    assert result["water_source"] == "sump_pump_failure"
    assert result["nfip_flood_candidate"] is False


@pytest.mark.parametrize("source", NON_FLOOD_SOURCES)
def test_every_non_flood_source_is_not_an_nfip_candidate(source):
    result = decide_water_source(facts(), classification(source))
    assert result["water_source"] == source
    assert result["nfip_flood_candidate"] is False
    assert FRIENDLY[source] in result["claimant_message"]


# ---------------------------------------------------------------------------
# 2. Keyword fallback only when the classifier said "unknown"
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("Water came up the bayou and into the living room", "surface_flood"),
        ("storm surge pushed water through the garage", "surface_flood"),
        ("the sump failed during the storm", "sump_pump_failure"),
        ("the floor drain started gurgling and dirty water came out", "sewer_or_drain_backup"),
        ("a pipe under the kitchen sink burst", "internal_plumbing"),
        ("water was seeping through the foundation wall", "seepage"),
        ("shingles came off and rain came through the roof", "roof_or_wind_driven_rain"),
        ("After the wildfire last year, heavy rain sent mud and water down the hillside and through our back wall.", "surface_flood"),
    ],
)
def test_keyword_fallback_uses_the_single_matching_source(text, expected):
    result = decide_water_source(facts(water_entry=text), classification("unknown"))
    assert result["water_source"] == expected
    assert result["explanation"] == f"Keyword fallback matched '{expected}' in the claimant's description"
    assert result["nfip_flood_candidate"] is (expected == "surface_flood")


def test_street_under_water_plus_floor_drain_stays_unknown():
    result = decide_water_source(
        facts(water_entry="The whole street was under two feet of water and then the basement floor drain started pouring water in."),
        classification("unknown", "both general flood outside and drain backup"),
    )
    assert result["water_source"] == "unknown"
    assert "Mixed water-source signals" in result["explanation"]
    assert result["nfip_flood_candidate"] is False


def test_fallback_reads_description_and_summary_too():
    result = decide_water_source(facts(description="Sewer backed up into the basement"), classification("unknown"))
    assert result["water_source"] == "sewer_or_drain_backup"
    result = decide_water_source(facts(summary="A washing machine hose split."), classification("unknown"))
    assert result["water_source"] == "internal_plumbing"


def test_mixed_signals_stay_unknown_and_ask_the_claimant():
    result = decide_water_source(facts("the river rose and then the sump pump failed"), classification("unknown"))
    assert result["water_source"] == "unknown"
    assert result["nfip_flood_candidate"] is False
    assert result["explanation"] == "Mixed water-source signals (sump_pump_failure, surface_flood); adjuster must determine the cause"
    assert "from outside" in result["claimant_message"] and "from inside" in result["claimant_message"]


def test_no_keywords_stays_unknown_with_classifier_explanation():
    result = decide_water_source(facts("there was water everywhere"), classification("unknown", "not enough detail"))
    assert result["water_source"] == "unknown"
    assert result["explanation"] == "Classifier: not enough detail"
    assert result["claimant_message"].endswith("?")


def test_overflowing_toilet_counts_as_mixed_not_flood():
    # "overflow" is a surface-flood keyword and "toilet" a sewer keyword, so a
    # plain overflowing toilet is *mixed* -> unknown -> the agent asks. That is
    # the safe direction (it never becomes an NFIP flood by keyword alone).
    result = decide_water_source(facts("the toilet overflowed"), classification("unknown"))
    assert result["water_source"] == "unknown"
    assert result["nfip_flood_candidate"] is False


def test_keywords_are_case_insensitive():
    assert decide_water_source(facts("STORM SURGE water in the den"), classification("unknown"))["water_source"] == "surface_flood"


@pytest.mark.parametrize("text", ["HURRICANE water in the den", "The tropical storm hit and there was water everywhere", "Water got in during Hurricane Ida"])
def test_storm_names_alone_are_not_surface_flood(text):
    # A hurricane can bring wind-driven rain (not flood) as well as rising
    # water (flood). Naming the storm says nothing about how water got in.
    result = decide_water_source(facts(text), classification("unknown"))
    assert result["water_source"] == "unknown"
    assert result["nfip_flood_candidate"] is False


def test_hurricane_with_rain_through_the_roof_is_wind_driven_rain_not_mixed():
    result = decide_water_source(facts("the hurricane tore off shingles and rain came through the roof"), classification("unknown"))
    assert result["water_source"] == "roof_or_wind_driven_rain"


def test_power_surge_is_not_storm_surge():
    assert decide_water_source(facts("a power surge knocked out the lights and there was water on the floor"), classification("unknown"))["water_source"] == "unknown"
    # Before, "surge" + "sump" looked like mixed signals; now it is just the sump.
    assert decide_water_source(facts("a power surge killed the sump and the basement filled"), classification("unknown"))["water_source"] == "sump_pump_failure"


@pytest.mark.parametrize("rationale", ["uncertain - could be the creek or a drain", "multiple possible sources", "Unclear from the story", "mixed signals"])
def test_mixed_or_uncertain_classifier_rationale_skips_the_keyword_fallback(rationale):
    # Only ONE keyword source matches ("creek"), but the classifier read the
    # whole story and said it cannot tell - that judgement wins.
    result = decide_water_source(facts("the creek came over its banks"), classification("unknown", rationale))
    assert result["water_source"] == "unknown"
    assert result["nfip_flood_candidate"] is False
    assert result["explanation"].startswith("Mixed water-source signals (classifier:")


def test_keyword_table_covers_every_non_unknown_source():
    assert set(KEYWORDS) == {"surface_flood", *NON_FLOOD_SOURCES}


def test_accepts_missing_inputs():
    # None -> default ClaimFacts; the classification must still be provided.
    result = decide_water_source(None, classification("unknown"))
    assert result["water_source"] == "unknown"


# ---------------------------------------------------------------------------
# 3. What the agent may say: never a coverage promise or denial
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("source", ["surface_flood", "unknown", *NON_FLOOD_SOURCES])
def test_claimant_message_never_promises_or_denies_coverage(source):
    message = decide_water_source(facts(), classification(source))["claimant_message"]
    assert not PROMISE_OR_DENIAL.search(message), message


@pytest.mark.parametrize("source", NON_FLOOD_SOURCES)
def test_non_flood_message_explains_without_deciding(source):
    message = decide_water_source(facts(), classification(source))["claimant_message"]
    assert "a person will review which policy applies" in message
    assert "I can't confirm coverage either way." in message
    assert "generally" in message and "usually" in message  # hedged language only


def test_promise_detector_itself_catches_bad_phrases():
    # Guard the guard: make sure the regex above would actually fail a bad message.
    for bad in ["Good news, you're covered.", "We will pay for the carpet.", "Your claim is approved", "This is not covered."]:
        assert PROMISE_OR_DENIAL.search(bad), bad
