"""Tests for claimdesk/data_access/policy_registry.py (no BigQuery needed).

HOW THESE TESTS AVOID GOOGLE CLOUD
    ``lookup_policy`` normally runs a BigQuery query. Here we use pytest's
    ``monkeypatch`` fixture to swap in a fake ``lookup_policy`` (or a fake
    ``run_query``) for the duration of one test. Nothing leaves your machine.
"""

from __future__ import annotations

import pytest

from claimdesk.contracts import ClaimFacts, PolicyRecord
from claimdesk.data_access import policy_registry as registry
from claimdesk.errors import ConfigurationError, DataAccessError

# A realistic registry row, shaped exactly like data_pipeline/sql/10_policy_registry.sql output.
ACTIVE_POLICY = PolicyRecord(
    found=True,
    policy_number="FLD-TX-7Q2K9M",
    policyholder_name="Avery Bennett",
    status="active",
    policy_line="NFIP Dwelling - Single Family",
    property_state="TX",
    reported_city="Houston",
    reported_zip_code="77006",
    rated_flood_zone="AE",
    effective_start="2025-06-01",
    effective_end="2026-06-01",
    building_coverage_usd=250000,
    contents_coverage_usd=100000,
    building_deductible_usd=1250,
    contents_deductible_usd=1000,
    primary_residence=True,
    source_record_id="218494902",
)


def _claim(**overrides) -> ClaimFacts:
    base = {
        "policyholder_name": "Avery Bennett",
        "policy_number": "FLD-TX-7Q2K9M",
        "date_of_loss": "2025-09-14",
        "loss_state": "TX",
        "loss_zip_code": "77006",
    }
    base.update(overrides)
    return ClaimFacts(**base)


@pytest.fixture
def registry_returns(monkeypatch):
    """Make ``lookup_policy`` return a given record (or raise)."""

    def _install(result):
        def fake_lookup(policy_number):
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(registry, "lookup_policy", fake_lookup)

    return _install


# ---------------------------------------------------------------------------
# normalize_policy_number
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("FLD-TX-7Q2K9M", "FLDTX7Q2K9M"),
        ("fld tx 7q2k9m", "FLDTX7Q2K9M"),
        ("  Fld_Tx.7Q2K9M ", "FLDTX7Q2K9M"),
        ("F L D T X seven Q two K nine M", "FLDTX7Q2K9M"),
        ("fld co two three four five six seven", "FLDCO234567"),
        ("FLD-NC-ZERO", "FLDNC0"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_policy_number(spoken, expected):
    assert registry.normalize_policy_number(spoken) == expected


def test_normalize_does_not_touch_digit_words_inside_codes():
    # \b word boundaries: "SEVENTY" or "TWOK" must not become digits.
    assert registry.normalize_policy_number("FLD-TX-TWOK99") == "FLDTXTWOK99"


# ---------------------------------------------------------------------------
# policy_status_headline
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "headline"),
    [
        ("active", "Active"),
        ("expired", "Expired - human review"),
        ("cancelled", "Cancelled - human review"),
        ("pending", "Not yet in effect - human review"),
        ("suspended", "Suspended"),  # unknown statuses are title-cased, not hidden
    ],
)
def test_policy_status_headline(status, headline):
    assert registry.policy_status_headline(ACTIVE_POLICY.model_copy(update={"status": status})) == headline


def test_policy_status_headline_not_found():
    assert registry.policy_status_headline(PolicyRecord(found=False)) == "Not found"


# ---------------------------------------------------------------------------
# review_policy_against_claim
# ---------------------------------------------------------------------------
def test_review_clean_claim_has_no_issues(registry_returns):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim()) == []


def test_review_registry_unavailable_degrades_gracefully(registry_returns):
    registry_returns(DataAccessError("BigQuery down"))
    assert registry.review_policy_against_claim(_claim()) == ["Policy registry unavailable - verify the policy manually"]


def test_review_policy_not_found(registry_returns):
    registry_returns(PolicyRecord(found=False))
    assert registry.review_policy_against_claim(_claim()) == ["Policy number needs verification"]


@pytest.mark.parametrize("loss_date", ["2025-05-31", "2026-06-02"])
def test_review_loss_outside_term(registry_returns, loss_date):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(date_of_loss=loss_date)) == ["Loss date falls outside the recorded policy term"]


@pytest.mark.parametrize("loss_date", ["2025-06-01", "2026-06-01"])
def test_review_term_boundaries_are_inclusive(registry_returns, loss_date):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(date_of_loss=loss_date)) == []


def test_review_expired_policy_still_covers_loss_inside_term(registry_returns):
    # Coverage is judged by the DATE OF LOSS, not today's status.
    registry_returns(ACTIVE_POLICY.model_copy(update={"status": "expired"}))
    assert registry.review_policy_against_claim(_claim(date_of_loss="2025-09-14")) == []


def test_review_unknown_loss_date(registry_returns):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(date_of_loss="not specified")) == ["Confirm an exact loss date for policy review"]


def test_review_cancelled_policy(registry_returns):
    registry_returns(ACTIVE_POLICY.model_copy(update={"status": "cancelled"}))
    assert registry.review_policy_against_claim(_claim()) == ["Policy is recorded as cancelled - human review required"]


def test_review_name_mismatch(registry_returns):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(policyholder_name="Jordan Smith")) == ["Claimant name differs from the policy record"]


@pytest.mark.parametrize("name", ["avery bennett", "AVERY  BENNETT", "Avery-Bennett", "not specified", ""])
def test_review_name_comparison_ignores_case_spacing_and_unknowns(registry_returns, name):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(policyholder_name=name)) == []


def test_review_state_mismatch(registry_returns):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(loss_state="la")) == ["Loss state differs from the insured property state"]


def test_review_state_unknown_is_not_an_issue(registry_returns):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(loss_state="not specified")) == []


def test_review_reports_multiple_issues_in_order(registry_returns):
    registry_returns(ACTIVE_POLICY.model_copy(update={"status": "cancelled"}))
    issues = registry.review_policy_against_claim(_claim(date_of_loss="2024-01-01", policyholder_name="Jordan Smith", loss_state="FL"))
    assert issues == [
        "Loss date falls outside the recorded policy term",
        "Policy is recorded as cancelled - human review required",
        "Claimant name differs from the policy record",
        "Loss state differs from the insured property state",
    ]


# ---------------------------------------------------------------------------
# lookup_policy (BigQuery call replaced by a fake run_query)
# ---------------------------------------------------------------------------
def test_lookup_policy_empty_input_skips_bigquery(monkeypatch):
    def boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("run_query should not be called for an empty policy number")

    monkeypatch.setattr(registry, "run_query", boom)
    record = registry.lookup_policy("  ")
    assert record.found is False
    assert "No policy number" in record.message


def test_lookup_policy_sends_normalized_key_as_parameter(monkeypatch):
    calls = []
    row = ACTIVE_POLICY.model_dump(exclude={"found", "message"})
    row["source_record_id"] = 218494902  # BigQuery could hand back a non-string

    def fake_run_query(sql, params, **kwargs):
        calls.append((sql, params, kwargs))
        return [dict(row)]

    monkeypatch.setattr(registry, "run_query", fake_run_query)
    record = registry.lookup_policy("fld tx 7q2k9m")

    sql, params, kwargs = calls[0]
    assert params == {"policy_key": "FLDTX7Q2K9M"}
    assert "policy_number_key = @policy_key" in sql
    assert ".policy_registry`" in sql
    assert kwargs["label"] == "policy_lookup"
    assert record.found is True
    assert record.source_record_id == "218494902"
    assert record.policy_number == "FLD-TX-7Q2K9M"


def test_lookup_policy_no_rows(monkeypatch):
    monkeypatch.setattr(registry, "run_query", lambda sql, params, **kwargs: [])
    record = registry.lookup_policy("FLD-TX-XXXXXX")
    assert record.found is False
    assert record.policy_number == "FLD-TX-XXXXXX"


# ---------------------------------------------------------------------------
# Blank / placeholder policy numbers: no BigQuery call, no policy issue
# ---------------------------------------------------------------------------
@pytest.fixture
def no_bigquery(monkeypatch):
    def boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("BigQuery must not be queried for a blank policy number")

    monkeypatch.setattr(registry, "run_query", boom)


@pytest.mark.parametrize("blank", ["not specified", "Not Specified", "unknown", "", "   ", "N/A", "none"])
def test_blank_policy_number_is_not_a_policy_issue(no_bigquery, blank):
    # Early in a call the extractor writes "not specified". That is a missing
    # field (required_fields asks for it), NOT a policy problem - otherwise
    # every early packet would route to policy_review.
    assert registry.review_policy_against_claim(_claim(policy_number=blank)) == []


@pytest.mark.parametrize("blank", ["not specified", "unknown", "n/a"])
def test_lookup_policy_placeholder_skips_bigquery(no_bigquery, blank):
    record = registry.lookup_policy(blank)
    assert record.found is False
    assert "No policy number" in record.message


def test_review_any_claimdesk_error_degrades(registry_returns):
    # ConfigurationError (e.g. no project id offline) must degrade exactly
    # like DataAccessError instead of failing the workflow node.
    registry_returns(ConfigurationError("GOOGLE_CLOUD_PROJECT is not set"))
    assert registry.review_policy_against_claim(_claim()) == ["Policy registry unavailable - verify the policy manually"]


# ---------------------------------------------------------------------------
# Tolerant comparisons: dates, states, names
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("loss_date", ["09/14/2025", "September 14, 2025", "Sep 14th, 2025", "2025-09-14"])
def test_review_accepts_common_date_formats(registry_returns, loss_date):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(date_of_loss=loss_date)) == []


def test_review_non_iso_date_outside_term_is_still_caught(registry_returns):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(date_of_loss="05/31/2025")) == ["Loss date falls outside the recorded policy term"]


@pytest.mark.parametrize("state", ["Texas", "texas", "TX", "tx", "T.X.", " Texas "])
def test_review_full_state_name_matches_code(registry_returns, state):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(loss_state=state)) == []


@pytest.mark.parametrize("state", ["Louisiana", "North Carolina", "FL"])
def test_review_different_state_by_name_is_flagged(registry_returns, state):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(loss_state=state)) == ["Loss state differs from the insured property state"]


@pytest.mark.parametrize("state", ["unknown", "", "Gulf Coast", "Tex"])
def test_review_unrecognised_state_is_not_compared(registry_returns, state):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(loss_state=state)) == []


@pytest.mark.parametrize(
    "value, code",
    [("Texas", "TX"), ("north carolina", "NC"), ("Colorado", "CO"), ("Florida", "FL"), ("Louisiana", "LA"), ("co", "CO"), ("XX", ""), ("not specified", "")],
)
def test_normalize_state(value, code):
    assert registry.normalize_state(value) == code


@pytest.mark.parametrize(
    "name",
    ["Bennett, Avery", "BENNETT, AVERY", "Avery J. Bennett", "Avery James Bennett", "A. Bennett", "Dr. Avery Bennett Jr.", "Bennett"],
)
def test_review_name_variants_are_not_a_mismatch(registry_returns, name):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(policyholder_name=name)) == []


@pytest.mark.parametrize("name", ["Jordan Bennett", "Avery Smith", "J. Bennett", "Smith, Jordan"])
def test_review_clearly_different_names_are_flagged(registry_returns, name):
    registry_returns(ACTIVE_POLICY)
    assert registry.review_policy_against_claim(_claim(policyholder_name=name)) == ["Claimant name differs from the policy record"]


@pytest.mark.parametrize(
    "claimed, recorded, same",
    [
        ("Smith, John", "John Smith", True),
        ("john q. smith", "John Smith", True),
        ("José García", "Jose Garcia", True),
        ("Liam O'Brien", "Liam OBrien", True),
        ("Maria Lopez-Diaz", "Maria Lopez", True),
        ("Jane Smith", "John Smith", False),
        ("John Brown", "John Smith", False),
    ],
)
def test_names_match(claimed, recorded, same):
    assert registry.names_match(claimed, recorded) is same
