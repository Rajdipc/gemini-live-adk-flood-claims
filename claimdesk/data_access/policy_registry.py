"""Policy lookup against the BigQuery ``policy_registry`` table.

Replaces the original hard-coded ``POLICY_RECORDS`` dictionary.

WHERE DOES THE DATA COME FROM?
    ``data_pipeline/sql/10_policy_registry.sql`` builds ``policy_registry``
    from **real FEMA NFIP v3 policy records** (dates, coverage limits,
    deductibles, flood zone, city/ZIP are all real). Because FEMA removes
    personal data, the SQL generates two fields deterministically from each
    record's real ``id``:
      * ``policy_number``      e.g. ``FLD-TX-7Q2K9M``
      * ``policyholder_name``  a fictional name
    Same record -> same number/name every time, so demos are repeatable.

VOICE-FRIENDLY POLICY NUMBERS
    Generated numbers use only the characters ``23456789ABCDEFGHJKMNPQRSTVWXYZ``
    (no 0/O, 1/I/L or U). Speech-to-text often confuses those, so we removed
    them from the alphabet entirely. Normalization then just uppercases and
    strips spaces/dashes: "fld tx 7q2k9m" -> "FLDTX7Q2K9M".
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..contracts import ClaimFacts, PolicyRecord
from ..errors import ClaimDeskError
from ..observability import get_logger
from ..rules._helpers import is_blank, parse_date
from .bq_client import run_query, table

log = get_logger(__name__)

# Spoken digits that speech-to-text sometimes writes as words.
_SPOKEN = {"ZERO": "0", "ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5", "SIX": "6", "SEVEN": "7", "EIGHT": "8", "NINE": "9"}
# Spoken separators ("F L D dash T X ...") carry no information - drop them.
_SPOKEN_SEPARATORS = ("DASH", "HYPHEN", "MINUS")


def normalize_policy_number(value: str | None) -> str:
    """Collapse spacing/punctuation/case so voice transcripts match the key."""

    text = str(value or "").upper()
    for word in _SPOKEN_SEPARATORS:
        text = re.sub(rf"\b{word}\b", " ", text)
    for word, digit in _SPOKEN.items():
        text = re.sub(rf"\b{word}\b", digit, text)
    return re.sub(r"[^A-Z0-9]", "", text)


_LOOKUP_SQL = """
SELECT
  policy_number, policyholder_name, status, policy_line, property_state,
  reported_city, reported_zip_code, rated_flood_zone,
  CAST(effective_start AS STRING) AS effective_start,
  CAST(effective_end AS STRING)   AS effective_end,
  building_coverage_usd, contents_coverage_usd,
  building_deductible_usd, contents_deductible_usd,
  primary_residence, source_record_id
FROM {table}
WHERE policy_number_key = @policy_key
LIMIT 1
"""


def lookup_policy(policy_number: str | None) -> PolicyRecord:
    """Return the policy for a spoken/typed number, or a not-found record.

    Raises :class:`DataAccessError` if BigQuery is unreachable - the caller
    (the live tool handler) turns that into a friendly spoken message.
    """

    key = normalize_policy_number(policy_number)
    # ``is_blank`` catches the placeholders the extractor LLM writes when the
    # claimant has not said a number yet ("not specified", "unknown", ...).
    # Without this check "not specified" would normalize to "NOTSPECIFIED",
    # cost a BigQuery query and come back as "No policy matched that number".
    if not key or is_blank(policy_number):
        return PolicyRecord(
            found=False,
            message="No policy number was provided. Ask the claimant to read it from their declarations page.",
        )
    rows = run_query(_LOOKUP_SQL.format(table=table("policy_registry")), {"policy_key": key}, label="policy_lookup")
    if not rows:
        return PolicyRecord(
            found=False,
            policy_number=str(policy_number or "").strip(),
            message="No policy matched that number. Ask the claimant to confirm it character by character.",
        )
    row: dict[str, Any] = rows[0]
    row["source_record_id"] = str(row.get("source_record_id") or "")
    # Defensive: the SQL guarantees non-NULL strings today, but if a column is
    # ever NULL we drop it so the PolicyRecord default applies instead of a
    # pydantic ValidationError crashing the live tool call.
    clean = {k: v for k, v in row.items() if v is not None}
    return PolicyRecord(found=True, **clean)


def policy_status_headline(record: PolicyRecord) -> str:
    """Short text for the notebook margin and the spoken confirmation."""

    if not record.found:
        return "Not found"
    # "pending" = the term starts in the future (10_policy_registry.sql). A loss
    # before the start date is also flagged by the term check below.
    return {
        "active": "Active",
        "pending": "Not yet in effect - human review",
        "expired": "Expired - human review",
        "cancelled": "Cancelled - human review",
    }.get(record.status, record.status.title())


# ---------------------------------------------------------------------------
# Tolerant comparisons
# ---------------------------------------------------------------------------
# People (and speech-to-text) say the same thing in many ways. A strict
# string comparison turned normal answers into "policy issues" that sent the
# claim to policy review, so each comparison below first normalizes both sides.

# Two-letter USPS codes for the 50 states, DC and the territories. The desk
# serves only CLAIMDESK_SUPPORTED_STATES, but the extractor may hear any state
# ("we're in Texas") and we just need to compare it with the registry's code.
US_STATE_CODES: dict[str, str] = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA",
    "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "PUERTO RICO": "PR", "GUAM": "GU", "US VIRGIN ISLANDS": "VI", "VIRGIN ISLANDS": "VI",
    "AMERICAN SAMOA": "AS", "NORTHERN MARIANA ISLANDS": "MP",
}
_VALID_STATE_CODES = frozenset(US_STATE_CODES.values())


def normalize_state(value: str | None) -> str:
    """``"Texas"``, ``"tx"``, ``"T.X."`` -> ``"TX"``. Unknown/blank -> ``""``.

    Returning ``""`` for anything we cannot recognise means "don't compare":
    a garbled state must never create a policy issue on its own.
    """

    if is_blank(value):
        return ""
    text = " ".join(re.sub(r"[^A-Za-z ]", "", str(value)).upper().split())
    if text in _VALID_STATE_CODES:
        return text
    return US_STATE_CODES.get(text, "")


# Titles and suffixes carry no identity ("Dr. Avery Bennett Jr." == "Avery Bennett").
_NAME_NOISE = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr", "jr", "sr", "ii", "iii", "iv"})


def _name_tokens(text: str | None) -> list[str]:
    """Lower-case letter tokens without accents, apostrophes, titles or suffixes.

    ``"José O'Brien-Díaz, Jr."`` -> ``["jose", "obrien", "diaz"]``.
    """

    ascii_text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    ascii_text = re.sub(r"['\u2019`]", "", ascii_text)  # O'Brien -> obrien, not "o" + "brien"
    return [t for t in re.findall(r"[a-z]+", ascii_text) if t not in _NAME_NOISE]


def _surname_tokens(recorded: str) -> set[str]:
    """Surname tokens of a registry name ("First Last", or "Last, First")."""

    raw = str(recorded or "")
    if "," in raw:
        return set(_name_tokens(raw.partition(",")[0]))
    words = [w for w in raw.split() if _name_tokens(w)]
    return set(_name_tokens(words[-1])) if words else set()


def _given_compatible(a: str, b: str) -> bool:
    """Same given name, or one side is just its initial ("A" ~ "Avery")."""

    return a == b or (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b))


def names_match(claimed: str | None, recorded: str | None) -> bool:
    """True unless the two names are *clearly* different people.

    Token-set based, so word order, case, punctuation, "Last, First" and
    middle names/initials do not matter:

        "Smith, John" ~ "John Smith"        "john q. smith" ~ "John Smith"
        "J Smith"     ~ "John Smith"        "Jane Smith"    !~ "John Smith"
        "John Brown"  !~ "John Smith"   (surname does not overlap)

    Nicknames ("Bill" vs "William") still count as different; the adjuster
    sees the issue text and decides - we cannot know every nickname.
    """

    claim = set(_name_tokens(claimed))
    record = set(_name_tokens(recorded))
    if not claim or not record:
        return True  # nothing to compare -> not an issue
    surname = _surname_tokens(str(recorded or ""))
    if surname and not (surname & claim):
        return False
    claim_given = claim - surname
    record_given = record - surname
    if not claim_given or not record_given:
        return True  # only a surname was given on one side, and it matched
    return any(_given_compatible(a, b) for a in claim_given for b in record_given)


def review_policy_against_claim(claim: ClaimFacts) -> list[str]:
    """Compare the claim with the policy record. Returns human-readable issues.

    IMPORTANT: coverage periods are compared with the **date of loss**, never
    with today's date. A policy that expired last month still covers a flood
    that happened while it was active.

    No policy number yet (``"not specified"``) -> no issue and no query: that
    is simply a missing field, which ``required_fields`` already asks for.
    Flagging it here would send every early-call packet to policy review.

    If BigQuery is down (or not configured) we return an issue instead of
    raising, so the rest of the pipeline can still produce a packet (routed to
    policy review so a person verifies the policy).
    """

    if is_blank(claim.policy_number) or not normalize_policy_number(claim.policy_number):
        return []

    try:
        record = lookup_policy(claim.policy_number)
    except ClaimDeskError:
        # ClaimDeskError (not only DataAccessError) so a ConfigurationError
        # also degrades instead of failing the workflow node.
        log.warning("Policy review skipped: registry unavailable", exc_info=True)
        return ["Policy registry unavailable - verify the policy manually"]

    if not record.found:
        return ["Policy number needs verification"]

    issues: list[str] = []
    # parse_date accepts "2025-09-14", "09/14/2025", "September 14th, 2025"...
    loss = parse_date(claim.date_of_loss)
    start, end = parse_date(record.effective_start), parse_date(record.effective_end)
    if loss is None:
        issues.append("Confirm an exact loss date for policy review")
    elif start and end and not (start <= loss <= end):
        issues.append("Loss date falls outside the recorded policy term")
    if record.status == "cancelled":
        issues.append("Policy is recorded as cancelled - human review required")
    if not is_blank(claim.policyholder_name) and not names_match(claim.policyholder_name, record.policyholder_name):
        issues.append("Claimant name differs from the policy record")
    claim_state, record_state = normalize_state(claim.loss_state), normalize_state(record.property_state)
    if claim_state and record_state and claim_state != record_state:
        issues.append("Loss state differs from the insured property state")
    return issues


__all__ = [
    "US_STATE_CODES",
    "lookup_policy",
    "names_match",
    "normalize_policy_number",
    "normalize_state",
    "policy_status_headline",
    "review_policy_against_claim",
]
