"""FEMA NFIP code tables + the rules that generate fictional identities.

WHY IS THIS FILE HERE?
    FEMA publishes many NFIP fields as short *codes* (``buildingDeductibleCode
    = "F"`` means a \\$1,250 deductible). The SQL in ``sql/`` decodes them with
    ``CASE`` expressions. This module holds the SAME tables in Python so that:

      * unit tests can check that the SQL and Python copies never drift apart
        (``tests/test_data_pipeline.py`` parses the SQL files), and
      * ``docs/data_dictionary.md`` has one obvious place to point at.

    Source of the code meanings: FEMA's OpenFEMA data dictionaries for
    ``NfipPolicies`` v3 and ``NfipClaims`` v3. The field list for any dataset
    is itself available from the API, e.g.
    https://www.fema.gov/api/open/v1/OpenFemaDataSetFields?$filter=openFemaDataSet%20eq%20%27NfipClaims%27%20and%20datasetVersion%20eq%203
    and the human-readable pages are linked from
    https://www.fema.gov/about/openfema/data-sets (the v2 "FIMA NFIP Redacted
    Claims/Policies" pages describe the same codes; v3 renamed the endpoints).

GENERATED IDENTITIES (important!)
    FEMA removes names and policy numbers from NFIP data for privacy. ClaimDesk
    needs *something* a caller can read out, so ``sql/10_policy_registry.sql``
    generates, from each record's real ``id``:

      * ``policy_number``      ``FLD-<STATE>-XXXXXX`` (6 voice-friendly chars)
      * ``policyholder_name``  a fictional "First Last" name

    BigQuery's ``FARM_FINGERPRINT`` (a fast, stable 64-bit hash) turns the id
    into a number; the functions below show - in plain Python - exactly how
    that number becomes letters. The Python versions take the fingerprint as
    input because ``FARM_FINGERPRINT`` itself only exists inside BigQuery.

This product uses the FEMA OpenFEMA API, but is not endorsed by FEMA.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Deductible codes (buildingDeductibleCode / contentsDeductibleCode) -> USD
# ---------------------------------------------------------------------------
DEDUCTIBLE_CODES_USD: dict[str, int] = {
    "0": 500,
    "1": 1000,
    "2": 2000,
    "3": 3000,
    "4": 4000,
    "5": 5000,
    "9": 750,
    "A": 10000,
    "B": 15000,
    "C": 20000,
    "D": 25000,
    "E": 50000,
    "F": 1250,
    "G": 1500,
    "H": 200,
}

# ---------------------------------------------------------------------------
# causeOfDamage (claims) -> readable label
# ---------------------------------------------------------------------------
CAUSE_OF_DAMAGE: dict[str, str] = {
    "0": "Other causes",
    "1": "Tidal water overflow",
    "2": "Stream, river, or lake overflow",
    "3": "Alluvial fan overflow",
    "4": "Accumulation of rainfall or snowmelt",
    "7": "Erosion - demolition",
    "8": "Erosion - removal",
    "9": "Earth movement, landslide, land subsidence, sinkholes",
    "A": "Closed basin lake",
    "B": "Expedited claim handling - without site inspection",
    "C": "Expedited claim handling - follow-up site inspection",
    "D": "Expedited claim handling - remote adjustment pilot",
}

# A coarser grouping used to *stratify* the eval sample (so the ~200 seed
# claims contain coastal, river and rain floods, not just the most common one).
CAUSE_GROUP: dict[str, str] = {
    "1": "coastal",
    "2": "riverine",
    "3": "riverine",
    "A": "riverine",
    "4": "rainfall",
}
DEFAULT_CAUSE_GROUP = "other"

# ---------------------------------------------------------------------------
# nonPaymentReasonBuilding / nonPaymentReasonContents -> readable label
# (FEMA's full legal-value list for NfipClaims v3). The ones most relevant to
# a flood desk are 01, 02, 03, 06, 12, 13, 15, 16 and 20: they explain why
# "water damage" is not always an NFIP "flood" loss.
# ---------------------------------------------------------------------------
NON_PAYMENT_REASON: dict[str, str] = {
    "01": "Damage below deductible",
    "02": "Seepage (not a flood)",
    "03": "Backup of drains (not a flood)",
    "04": "Shrubs not covered",
    "05": "Sea wall",
    "06": "Not an actual flood",
    "07": "Loss in progress",
    "08": "Failure to pursue claim",
    "09": "Debris removal only",
    "10": "Fire",
    "11": "Fence damage",
    "12": "Hydrostatic pressure",
    "13": "Drainage clogged",
    "14": "Boat piers",
    "15": "Damage occurred before policy inception",
    "16": "Wind damage (not flood)",
    "17": "Erosion type not included in flood definition",
    "18": "Landslide",
    "19": "Mudflow type not included in flood definition",
    "20": "No demonstrable damage",
    "97": "Other",
    "98": "Error - claim deleted",
    "99": "Erroneous assignment",
}

# ---------------------------------------------------------------------------
# occupancyType -> label, and which ones are residential
# (2-digit codes are used by Risk Rating 2.0 policies, 1-digit by older ones)
# ---------------------------------------------------------------------------
OCCUPANCY_TYPE: dict[int, str] = {
    1: "Single family residence",
    2: "2-4 unit residential building",
    3: "Residential building with more than 4 units",
    4: "Non-residential building",
    6: "Non-residential business",
    11: "Single-family residential building",
    12: "Residential non-condo building, 2-4 units",
    13: "Residential non-condo building, 5+ units",
    14: "Residential manufactured / mobile home",
    15: "Residential condominium association building",
    16: "Single residential unit within a multi-unit building",
    17: "Non-residential manufactured / mobile home",
    18: "Non-residential building",
}

# ClaimDesk v2 is a *residential* flood desk, so the registry, benchmarks and
# eval seeds keep only these occupancy types (see docs/data_dictionary.md).
# The keys of POLICY_LINE_BY_OCCUPANCY ARE the residential set.
POLICY_LINE_BY_OCCUPANCY: dict[int, str] = {
    1: "NFIP Dwelling - Single Family",
    11: "NFIP Dwelling - Single Family",
    2: "NFIP Dwelling - 2-4 Family",
    12: "NFIP Dwelling - 2-4 Family",
    14: "NFIP Dwelling - Manufactured Home",
    16: "NFIP Dwelling - Residential Unit",
    3: "NFIP General Property - Other Residential",
    13: "NFIP General Property - Other Residential",
    15: "NFIP RCBAP - Condominium Association",
}
RESIDENTIAL_OCCUPANCY_TYPES: frozenset[int] = frozenset(POLICY_LINE_BY_OCCUPANCY)

# ---------------------------------------------------------------------------
# Generated identity rules (mirrors sql/10_policy_registry.sql)
# ---------------------------------------------------------------------------
# No 0/O, 1/I/L or U: speech-to-text confuses them.
POLICY_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
POLICY_CODE_LENGTH = 6
POLICY_CODE_SPACE = len(POLICY_ALPHABET) ** POLICY_CODE_LENGTH  # 30^6 = 729,000,000

# Short, common, easy-to-spell names. Combinations are FICTIONAL - they are
# chosen by a hash, not taken from FEMA (which has no names at all).
FIRST_NAMES: tuple[str, ...] = (
    "Avery", "Blake", "Carmen", "Dana", "Elena", "Felix", "Grace", "Hector",
    "Iris", "Jonah", "Keira", "Lucas", "Maya", "Nolan", "Olivia", "Priya",
    "Quinn", "Rosa", "Samuel", "Tessa", "Victor", "Wendy", "Xavier", "Yara",
    "Zane", "Amara", "Brooke", "Caleb", "Dmitri", "Esther", "Farah", "Gavin",
    "Hana", "Isaac", "Jade", "Kofi", "Lena", "Marcus", "Nadia", "Omar",
)
LAST_NAMES: tuple[str, ...] = (
    "Alvarez", "Bennett", "Castillo", "Dawson", "Ellison", "Fischer", "Garner",
    "Holloway", "Ibarra", "Jennings", "Kowalski", "Lindqvist", "Mercer",
    "Navarro", "Okafor", "Prescott", "Quintero", "Ramsey", "Sorensen",
    "Thornton", "Underwood", "Valdez", "Whitaker", "Yamamoto", "Zeller",
    "Ashby", "Brennan", "Calloway", "Delgado", "Everett", "Fairbanks",
    "Galloway", "Hartley", "Iverson", "Kendrick", "Langston", "Montoya",
    "Pemberton", "Radcliffe", "Sterling",
)

# Values FEMA uses when it has redacted the city name.
REDACTED_CITY_VALUES = frozenset({"", "NA", "N/A", "CURRENTLY UNAVAILABLE", "UNKNOWN"})


def policy_code_from_fingerprint(fingerprint: int) -> str:
    """Turn a signed 64-bit hash into 6 characters of ``POLICY_ALPHABET``.

    Same arithmetic as the SQL temp function ``policy_code``:
    ``n = ABS(MOD(fp, 30^6))`` then write ``n`` in base 30, most significant
    digit first. (``abs(fp) % M`` in Python equals SQL's ``ABS(MOD(fp, M))``
    because SQL's MOD keeps the sign of the dividend.)
    """

    n = abs(fingerprint) % POLICY_CODE_SPACE
    base = len(POLICY_ALPHABET)
    chars = [POLICY_ALPHABET[(n // base**k) % base] for k in range(POLICY_CODE_LENGTH - 1, -1, -1)]
    return "".join(chars)


def format_policy_number(state: str, code: str) -> str:
    """``("tx", "7Q2K9M") -> "FLD-TX-7Q2K9M"``."""

    return f"FLD-{state.strip().upper()}-{code}"


def policy_number_key(policy_number: str) -> str:
    """SQL's ``REGEXP_REPLACE(UPPER(x), r'[^A-Z0-9]', '')`` in Python.

    Must agree with ``claimdesk.data_access.policy_registry.normalize_policy_number``
    for every generated number (tested).
    """

    return re.sub(r"[^A-Z0-9]", "", policy_number.upper())


def fictional_name(first_fingerprint: int, last_fingerprint: int) -> str:
    """Pick "First Last" from the name lists, like the SQL does."""

    return f"{FIRST_NAMES[abs(first_fingerprint) % len(FIRST_NAMES)]} {LAST_NAMES[abs(last_fingerprint) % len(LAST_NAMES)]}"


def decode_deductible(code: str | None) -> int | None:
    """``"F" -> 1250``; unknown or empty codes -> ``None``."""

    return DEDUCTIBLE_CODES_USD.get(str(code or "").strip().upper())


def clean_city(reported_city: str | None, community_name: str | None) -> str | None:
    """Best readable place name for a record (mirrors SQL ``clean_city``).

    FEMA redacts ``reportedCity`` in most v3 rows (``"NA"`` or ``"Currently
    Unavailable"``). When that happens we fall back to the NFIP *community*
    name, which is public: ``"HOUSTON, CITY OF" -> "Houston"``,
    ``"HARDIN COUNTY *" -> "Hardin County"``.
    """

    reported = str(reported_city or "").strip()
    if reported.upper() not in REDACTED_CITY_VALUES:
        return reported.title()
    community = re.sub(r",.*$", "", str(community_name or "").replace("*", "")).strip()
    return community.title() or None


__all__ = [
    "CAUSE_GROUP",
    "CAUSE_OF_DAMAGE",
    "DEDUCTIBLE_CODES_USD",
    "FIRST_NAMES",
    "LAST_NAMES",
    "NON_PAYMENT_REASON",
    "OCCUPANCY_TYPE",
    "POLICY_ALPHABET",
    "POLICY_LINE_BY_OCCUPANCY",
    "RESIDENTIAL_OCCUPANCY_TYPES",
    "clean_city",
    "decode_deductible",
    "fictional_name",
    "format_policy_number",
    "policy_code_from_fingerprint",
    "policy_number_key",
]
