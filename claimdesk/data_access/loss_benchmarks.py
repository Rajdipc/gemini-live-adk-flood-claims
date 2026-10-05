"""Data-driven thresholds from ~460k real NFIP claims (2015+, five states).

WHY?
    The original app used fixed numbers: "estimate >= $25,000 is high",
    "reported > 90 days after loss is suspicious". Those numbers were guesses.
    Here the thresholds come from what actually happened in real flood
    claims in the same state:

    * ``damage_p95_usd``      - 95% of real claims in this state had building +
                                contents damage below this. An estimate above
                                it is *unusual* (not wrong!) -> adjuster review.
    * ``report_lag_p95_days`` - 95% of real claims were opened within this many
                                days of the loss. Later reports get a soft flag.

    The table ``loss_benchmarks`` is built by
    ``data_pipeline/sql/20_loss_benchmarks.sql``.

GRACEFUL DEGRADATION
    If BigQuery is unreachable we return ``available=False`` and the rules
    fall back to conservative defaults (see ``rules/risk_signals.py``). A
    missing benchmark must never block a claimant.
"""

from __future__ import annotations

import threading

from ..contracts import BenchmarkResult
from ..errors import ClaimDeskError
from ..observability import get_logger
from .bq_client import run_query, table

log = get_logger(__name__)

_SQL = """
SELECT state, sample_size, damage_p50_usd, damage_p90_usd, damage_p95_usd, report_lag_p95_days
FROM {table}
WHERE state IN (@state, 'ALL')
ORDER BY IF(state = @state, 0, 1)
LIMIT 1
"""

# WHY NOT functools.lru_cache?
#   lru_cache remembers *every* return value forever - including "no rows"
#   (e.g. the first call happened before the data pipeline finished). That
#   answer would then stick until the container restarts. So we cache only
#   real benchmarks; "unavailable" is recomputed next time (a cheap query).
#   The lock matters because pipeline runs execute this in worker threads
#   (asyncio.to_thread) and a plain dict is not safe to resize concurrently.
_CACHE_MAX = 64
_cache: dict[str, BenchmarkResult] = {}
_cache_lock = threading.Lock()


def _query(state: str) -> BenchmarkResult:
    rows = run_query(_SQL.format(table=table("loss_benchmarks")), {"state": state}, label="loss_benchmarks")
    if not rows:
        return BenchmarkResult(available=False, state=state, note="No benchmark rows found")
    row = rows[0]
    note = "" if row["state"] == state else f"No {state} benchmark; used all-states benchmark"
    return BenchmarkResult(available=True, note=note, **row)


def _cached(state: str) -> BenchmarkResult:
    with _cache_lock:
        hit = _cache.get(state)
    if hit is not None:
        return hit.model_copy()
    result = _query(state)  # outside the lock: never hold a lock during network I/O
    if result.available:
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX:
                _cache.pop(next(iter(_cache)))  # drop the oldest entry
            _cache[state] = result
    return result.model_copy()


def clear_cache() -> None:
    """Forget cached benchmarks (used by tests)."""

    with _cache_lock:
        _cache.clear()


def benchmark_for_state(state: str | None) -> BenchmarkResult:
    """Return benchmarks for a 2-letter state code (cached per process)."""

    code = (state or "").strip().upper()
    if len(code) != 2:
        code = "ALL"
    try:
        return _cached(code)
    except ClaimDeskError:
        log.warning("Loss benchmarks unavailable; using fallback thresholds", exc_info=True)
        return BenchmarkResult(available=False, state=code, note="Benchmark service unavailable")


__all__ = ["benchmark_for_state", "clear_cache"]
