"""Layer 1 of the eval flywheel: unit tests for ``claimdesk/rules/required_fields.py``.

WHY TEST RULES SEPARATELY FROM EVALS?
    The LLM steps are non-deterministic, so they are measured with *evals*
    (scores over a dataset). The rule steps are plain Python, so they must be
    *exactly* right every time - that is what ordinary unit tests are for.
    These tests never call Gemini, BigQuery or any other Google Cloud service,
    which is why they run in well under a second and are safe in CI.

WHAT THE RULE DOES (read the source next to this file while reading the tests)
    ``check_required_fields`` takes the ``ClaimFacts`` the extractor produced and
    answers "can an adjuster start work?". Six blocking fields must be present
    (who / when / where / what). Dates must be real calendar dates and not in
    the future. Anything the extractor itself flagged as uncertain is carried
    over as missing. ZIP code and amount are only *warnings*.

    ``today`` is injectable so date checks are deterministic in tests.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from claimdesk.contracts import ClaimFacts, FieldCheck
from claimdesk.rules.required_fields import BLOCKING_FIELD_QUESTIONS, check_required_fields

TODAY = date(2025, 7, 20)

# A claim where every blocking field is filled in (a "happy path" fixture).
COMPLETE = {
    "policyholder_name": "Maria Gonzalez",
    "policy_number": "FLD-CO-4H7K2P",
    "contact_method": "720-555-0142",
    "date_of_loss": "2025-07-14",
    "loss_address_or_city": "1450 S Pearl St, Denver, CO",
    "loss_state": "CO",
    "loss_zip_code": "80210",
    "loss_description": "Heavy rain pooled around the house and 8 inches of water came into the basement.",
    "estimated_loss_usd": 23400,
}


def check(**overrides):
    """Run the rule on COMPLETE with some fields overridden."""

    return check_required_fields({**COMPLETE, **overrides}, today=TODAY)


def test_complete_claim_is_valid_with_no_warnings():
    result = check()
    assert result == {"intake_status": "valid", "missing_fields": [], "warnings": []}
    # The rule returns a plain dict (ADK state stores dicts) that matches the contract.
    FieldCheck.model_validate(result)


@pytest.mark.parametrize("field", list(BLOCKING_FIELD_QUESTIONS))
def test_each_blocking_field_is_required(field):
    result = check(**{field: "not specified"})
    assert result["intake_status"] == "missing_info"
    assert result["missing_fields"] == [field]


@pytest.mark.parametrize("placeholder", ["", "  ", "unknown", "Not Specified", "N/A", "none", "not provided", "unspecified"])
def test_llm_placeholder_strings_count_as_blank(placeholder):
    # The extractor is told to write "not specified", but LLMs vary. All the
    # usual placeholders must be treated as "missing", never as a real value.
    assert check(policy_number=placeholder)["missing_fields"] == ["policy_number"]


def test_empty_input_reports_all_six_blocking_fields_in_order():
    # ``None`` becomes an all-default ClaimFacts (what happens if extraction produced nothing).
    result = check_required_fields(None, today=TODAY)
    assert result["intake_status"] == "missing_info"
    assert result["missing_fields"] == list(BLOCKING_FIELD_QUESTIONS)


def test_accepts_model_instance_and_json_string():
    # ADK state can hold a dict, a JSON string or a model; all must work the same.
    as_model = check_required_fields(ClaimFacts(**COMPLETE), today=TODAY)
    as_json = check_required_fields(json.dumps(COMPLETE), today=TODAY)
    assert as_model == as_json == check()


def test_zip_and_amount_are_warnings_not_blockers():
    result = check(loss_zip_code="not specified", estimated_loss_usd=None)
    assert result["intake_status"] == "valid"
    assert result["missing_fields"] == []
    assert result["warnings"] == [
        "ZIP code not captured yet; it is needed to check NOAA weather records.",
        "Estimated loss amount was not supplied.",
    ]


def test_unparseable_date_of_loss_asks_for_a_valid_date():
    result = check(date_of_loss="the Tuesday after the storm")
    assert result["missing_fields"] == ["Confirm a valid calendar date for date of loss"]


def test_impossible_calendar_date_is_rejected():
    assert check(date_of_loss="2025-02-30")["missing_fields"] == ["Confirm a valid calendar date for date of loss"]


def test_future_date_of_loss_is_flagged():
    result = check(date_of_loss="2025-07-21")  # TODAY + 1
    assert result["missing_fields"] == ["Confirm the future date given for date of loss"]


def test_date_of_loss_equal_to_today_is_fine():
    assert check(date_of_loss="2025-07-20")["intake_status"] == "valid"


def test_reported_date_is_validated_only_when_present():
    assert check(reported_date="not specified")["intake_status"] == "valid"
    assert check(reported_date="soon")["missing_fields"] == ["Confirm a valid calendar date for reported date"]
    assert check(reported_date="2025-08-01")["missing_fields"] == ["Confirm the future date given for reported date"]


@pytest.mark.parametrize("value", ["2025-07-14", "07/14/2025", "07-14-2025", "July 14, 2025", "Jul 14 2025", "July 14th, 2025"])
def test_common_date_spellings_are_accepted(value):
    assert check(date_of_loss=value)["intake_status"] == "valid"


def test_extractor_uncertainties_are_carried_over_and_deduplicated():
    result = check(
        policy_number="not specified",
        missing_or_uncertain_facts=["Exact cause of water entry", "exact cause of water entry", "policy_number"],
    )
    # Case-insensitive de-duplication keeps the first spelling and the order.
    assert result["missing_fields"] == ["policy_number", "Exact cause of water entry"]
    assert result["intake_status"] == "missing_info"


def test_uncertain_facts_alone_make_the_intake_incomplete():
    result = check(missing_or_uncertain_facts=["Which room flooded first"])
    assert result == {"intake_status": "missing_info", "missing_fields": ["Which room flooded first"], "warnings": []}


def test_every_blocking_field_has_a_friendly_question():
    # The packet writer turns the first missing field into the next question
    # for the claimant, so every blocking field needs one.
    assert set(BLOCKING_FIELD_QUESTIONS) == {
        "policyholder_name",
        "policy_number",
        "contact_method",
        "date_of_loss",
        "loss_address_or_city",
        "loss_description",
    }
    # Each entry is a real question (some add a short hint after the "?").
    assert all("?" in q for q in BLOCKING_FIELD_QUESTIONS.values())
