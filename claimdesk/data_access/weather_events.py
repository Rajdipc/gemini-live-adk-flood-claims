"""NOAA Storm Events corroboration - "did the weather service record flooding
or heavy rain near this ZIP code around the date of loss?"

DATA (two small tables in OUR dataset, built by the data pipeline)
    * ``noaa_flood_events`` - flood-type NOAA Storm Events for the supported
      states since 2015: canonical event type, date, county/zone and (for
      many events) a point. Copied from
      ``bigquery-public-data.noaa_historic_severe_storms``.
    * ``zip_points`` - ZIP code -> county, state and an internal point.
      Copied from ``bigquery-public-data.geo_us_boundaries.zip_codes``.

    WHY COPIES? Our dataset lives in us-central1 (like every other resource)
    and the public datasets live in the US multi-region. BigQuery cannot
    JOIN across locations, so ``data_pipeline/load_to_bigquery.py --steps
    reference`` copies the small subset we need. The copy is only as fresh
    as its last refresh (docs/data_loading.md, "Refresh reference data").

HOW THE MATCH WORKS
    An event "matches" when it is a flood-type event AND starts within
    ``window_days`` of the loss date AND either
      (a) its point is within ``radius_km`` of the ZIP's internal point, or
      (b) it was reported for the same county as the ZIP (many flood events
          are reported per county and have no point). County names are
          compared as letters-only uppercase keys ("ST. CHARLES" ==
          "St. Charles Parish" -> "STCHARLES").

HOW THE RESULT IS USED (read carefully)
    This is a **soft signal** for the human adjuster. "No event found" does
    NOT mean the claim is false: local drainage floods are often not in the
    NOAA database, and NOAA publishes with a lag of a few months. So the
    rule ``CORROB-001`` only adds a note and never changes the routing
    decision by itself.

COST
    ``noaa_flood_events`` is partitioned by month of ``event_date``, so a
    "loss date +/- 3 days" query reads 1-2 monthly partitions (kilobytes);
    ``zip_points`` is clustered by ``zip_code``. Fractions of a cent.
"""

from __future__ import annotations

import threading
from datetime import timedelta

from ..contracts import WeatherCheckResult
from ..errors import ClaimDeskError
from ..observability import get_logger
from ..rules._helpers import parse_date
from ..settings import get_settings, local_now
from .bq_client import run_query, table

log = get_logger(__name__)

# Canonical NOAA event names. The pipeline stores these exact spellings in
# noaa_flood_events.event_type (the public source spells them in lowercase).
FLOOD_EVENT_TYPES = (
    "Flash Flood",
    "Flood",
    "Heavy Rain",
    "Coastal Flood",
    "Lakeshore Flood",
    "Storm Surge/Tide",
    "Tropical Storm",
    "Tropical Depression",
    "Hurricane",
    "Hurricane (Typhoon)",
)

# {zip_points} / {noaa_flood_events} are replaced with our fully-qualified
# table names (from settings) when the query runs; everything the claimant
# said is passed as @parameters, never pasted into the SQL text.
_SQL_TEMPLATE = """
WITH zip AS (
  SELECT point AS pt, county_name_normalized, state_code
  FROM {zip_points}
  WHERE zip_code = @zip
  LIMIT 1
)
SELECT
  s.event_type,
  IF(s.event_point IS NULL OR zip.pt IS NULL, NULL, ST_DISTANCE(s.event_point, zip.pt) / 1000) AS distance_km
FROM {noaa_flood_events} AS s, zip
-- Filtering on the partitioning column event_date lets BigQuery skip every
-- month outside the window (partition pruning -> cheaper and faster).
WHERE s.event_date BETWEEN @date_from AND @date_to
  AND s.event_type IN UNNEST(@event_types)
  AND (
        (s.event_point IS NOT NULL AND zip.pt IS NOT NULL AND ST_DWITHIN(s.event_point, zip.pt, @radius_m))
     OR (s.cz_type = 'C' AND s.state_code = zip.state_code AND s.cz_name_normalized = zip.county_name_normalized)
  )
"""


def build_weather_sql() -> str:
    """The weather query with our own table names filled in (no public datasets)."""

    return _SQL_TEMPLATE.replace("{zip_points}", table("zip_points")).replace("{noaa_flood_events}", table("noaa_flood_events"))


def check_weather(zip_code: str | None, loss_date: str | None, *, window_days: int = 3, radius_km: int = 50) -> WeatherCheckResult:
    """Look for NOAA flood/rain events near a ZIP around the loss date."""

    if not get_settings().enable_weather_check:
        return WeatherCheckResult(checked=False, note="Weather check disabled by configuration")
    zip5 = (zip_code or "").strip()[:5]
    if not (zip5.isdigit() and len(zip5) == 5):
        return WeatherCheckResult(checked=False, note="Need a 5-digit ZIP code to check weather records")
    day = parse_date(loss_date)
    if day is None:
        return WeatherCheckResult(checked=False, note="Need an exact loss date to check weather records")
    # "Today" in the desk's timezone (CLAIMDESK_TIMEZONE), not the server's:
    # Cloud Run runs in UTC, so on a US evening date.today() is already
    # tomorrow and a real "today" loss could look like a future date - or a
    # true future date could slip through.
    if day > local_now().date():
        return WeatherCheckResult(checked=False, note="Loss date is in the future")

    cache_key = (zip5, day.isoformat(), window_days, radius_km)
    with _CACHE_LOCK:
        cached = _RESULT_CACHE.get(cache_key)
    if cached is not None:
        return cached.model_copy()

    params = {
        "zip": zip5,
        "date_from": day - timedelta(days=window_days),
        "date_to": day + timedelta(days=window_days),
        "radius_m": float(radius_km * 1000),
    }
    try:
        rows = run_query(
            build_weather_sql(),
            params,
            array_params={"event_types": list(FLOOD_EVENT_TYPES)},
            label="weather_check",
            timeout_s=10.0,
        )
    except ClaimDeskError:
        # Degrade gracefully: the claim continues without the soft signal
        # (e.g. the reference tables were not built yet). Failures are NOT
        # cached, so the next pipeline run retries.
        log.warning("NOAA weather check unavailable", exc_info=True)
        return WeatherCheckResult(checked=False, window_days=window_days, radius_km=radius_km, note="Weather records unavailable")

    types = sorted({r["event_type"] for r in rows})
    distances = [r["distance_km"] for r in rows if r.get("distance_km") is not None]
    result = WeatherCheckResult(
        checked=True,
        events_found=len(rows),
        event_types=types,
        nearest_event_km=round(min(distances), 1) if distances else None,
        window_days=window_days,
        radius_km=radius_km,
        note=(
            f"{len(rows)} NOAA flood/rain event(s) recorded nearby within ±{window_days} days"
            if rows
            else f"No NOAA flood/rain event recorded within {radius_km} km / ±{window_days} days (soft signal only)"
        ),
    )
    with _CACHE_LOCK:
        if len(_RESULT_CACHE) >= _CACHE_MAX:
            _RESULT_CACHE.pop(next(iter(_RESULT_CACHE)))  # drop the oldest entry
        _RESULT_CACHE[cache_key] = result
    return result.model_copy()


# WHY AN IN-PROCESS CACHE?
#   The workflow re-runs after every claimant turn, usually with the same ZIP
#   and date. BigQuery's own 24h result cache already makes repeats free, but
#   each round trip still costs ~0.5-1 s. NOAA history does not change during
#   a call, so we keep successful answers in memory (per container).
# WHY A LOCK?
#   check_weather runs in worker threads (asyncio.to_thread), possibly for
#   several calls at once. Two threads evicting at the same moment can both
#   pick the same "oldest" key and the second pop() raises KeyError, so every
#   read/write of the dict happens under this lock (never during the query).
_CACHE_MAX = 256
_RESULT_CACHE: dict[tuple[str, str, int, int], WeatherCheckResult] = {}
_CACHE_LOCK = threading.Lock()

__all__ = ["check_weather", "FLOOD_EVENT_TYPES"]
