"""Runtime hardening of the data-access layer (no Google Cloud needed).

WHAT IS PINNED DOWN HERE
    1. ``bq_client.run_query`` turns EVERY failure - missing project,
       missing/expired credentials, network errors, query errors - into
       ``DataAccessError`` (original kept as ``__cause__``).
    2. The three pipeline look-ups degrade instead of crashing:
       policy review -> "verify manually" issue, benchmarks -> ``available=False``,
       weather -> ``checked=False``.
    3. Caches keep only successful answers and are safe across threads.
    4. "Today" follows ``CLAIMDESK_TIMEZONE`` (Cloud Run itself runs in UTC).
    5. Log redaction masks phone numbers but not dates or policy numbers.

HOW BIGQUERY IS FAKED
    ``monkeypatch`` swaps ``bq_client.get_bq_client`` for a function that
    raises, or returns a tiny fake client. Nothing leaves your machine.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import requests
from google.auth.exceptions import DefaultCredentialsError, RefreshError, TransportError

from claimdesk import settings as settings_module
from claimdesk.contracts import ClaimFacts
from claimdesk.data_access import bq_client, loss_benchmarks, policy_registry, weather_events
from claimdesk.errors import ConfigurationError, DataAccessError
from claimdesk.observability import redact

CLAIM = ClaimFacts(policyholder_name="Avery Bennett", policy_number="FLD-TX-7Q2K9M", date_of_loss="2025-09-14", loss_state="TX")


class _FakeJob:
    def __init__(self, error: Exception | None = None, rows: list[dict] | None = None) -> None:
        self.error, self.rows = error, rows or []
        self.total_bytes_processed, self.cache_hit = 0, True

    def result(self, timeout: float):
        if self.error:
            raise self.error
        return [_Row(r) for r in self.rows]


class _Row(dict):
    """bigquery.Row exposes .items(); a dict already does."""


class _FakeClient:
    def __init__(self, *, query_error: Exception | None = None, result_error: Exception | None = None) -> None:
        self.query_error, self.result_error = query_error, result_error

    def query(self, sql, job_config=None):
        if self.query_error:
            raise self.query_error
        return _FakeJob(self.result_error)


@pytest.fixture(autouse=True)
def _fresh_caches():
    loss_benchmarks.clear_cache()
    weather_events._RESULT_CACHE.clear()
    yield
    loss_benchmarks.clear_cache()
    weather_events._RESULT_CACHE.clear()


def _client_raises(monkeypatch, error: Exception) -> None:
    def factory():
        raise error

    monkeypatch.setattr(bq_client, "get_bq_client", factory)


# ---------------------------------------------------------------------------
# 1. run_query wraps everything
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    [ConfigurationError("GOOGLE_CLOUD_PROJECT is not set"), DefaultCredentialsError("no ADC"), TransportError("metadata server unreachable")],
    ids=["no-project", "no-credentials", "transport"],
)
def test_client_creation_errors_become_data_access_error(monkeypatch, error):
    _client_raises(monkeypatch, error)
    with pytest.raises(DataAccessError) as info:
        bq_client.run_query("SELECT 1", label="t")
    assert info.value.__cause__ is error
    assert info.value.retryable is False  # retrying will not create a project or credentials


@pytest.mark.parametrize(
    "client",
    [
        _FakeClient(query_error=RefreshError("token expired")),
        _FakeClient(query_error=requests.exceptions.ConnectionError("connection reset")),
        _FakeClient(result_error=TransportError("socket closed")),
        _FakeClient(result_error=TimeoutError()),
        _FakeClient(query_error=ValueError("something unexpected")),
    ],
    ids=["refresh", "requests", "transport", "timeout", "unexpected"],
)
def test_query_errors_become_data_access_error(monkeypatch, client):
    monkeypatch.setattr(bq_client, "get_bq_client", lambda: client)
    with pytest.raises(DataAccessError) as info:
        bq_client.run_query("SELECT 1", label="policy_lookup")
    assert "policy_lookup" in str(info.value)
    assert info.value.__cause__ is not None


# ---------------------------------------------------------------------------
# 2. The three callers degrade
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    [ConfigurationError("no project"), DefaultCredentialsError("no ADC"), RefreshError("expired"), requests.exceptions.ConnectionError("down")],
    ids=["config", "credentials", "refresh", "network"],
)
def test_all_three_lookups_degrade_when_bigquery_fails(monkeypatch, error):
    _client_raises(monkeypatch, error)

    assert policy_registry.review_policy_against_claim(CLAIM) == ["Policy registry unavailable - verify the policy manually"]

    benchmark = loss_benchmarks.benchmark_for_state("TX")
    assert benchmark.available is False and benchmark.note == "Benchmark service unavailable"

    monkeypatch.setattr(weather_events, "get_settings", lambda: type("S", (), {"enable_weather_check": True})())
    weather = weather_events.check_weather("77002", "2024-07-08")
    assert weather.checked is False and weather.note == "Weather records unavailable"


def test_live_policy_lookup_still_raises_data_access_error(monkeypatch):
    # The voice tool handler catches DataAccessError and says a friendly
    # sentence; a missing project must reach it as that type too.
    _client_raises(monkeypatch, ConfigurationError("no project"))
    with pytest.raises(DataAccessError):
        policy_registry.lookup_policy("FLD-TX-7Q2K9M")


# ---------------------------------------------------------------------------
# 3. Caches
# ---------------------------------------------------------------------------
BENCH_ROW = {"state": "TX", "sample_size": 1000, "damage_p50_usd": 1.0, "damage_p90_usd": 2.0, "damage_p95_usd": 3.0, "report_lag_p95_days": 30.0}


def test_benchmark_cache_keeps_only_successful_answers(monkeypatch):
    answers = [[], [dict(BENCH_ROW)]]
    calls: list[str] = []

    def fake_run_query(sql, params, **kwargs):
        calls.append(params["state"])
        return answers.pop(0) if answers else [dict(BENCH_ROW)]

    monkeypatch.setattr(loss_benchmarks, "run_query", fake_run_query)

    assert loss_benchmarks.benchmark_for_state("TX").available is False  # table empty (pipeline not run yet)
    assert loss_benchmarks.benchmark_for_state("TX").available is True  # NOT stuck on the empty answer
    assert loss_benchmarks.benchmark_for_state("TX").available is True  # served from cache
    assert calls == ["TX", "TX"]


def test_benchmark_failures_are_not_cached(monkeypatch):
    calls: list[int] = []

    def flaky(sql, params, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise DataAccessError("blip")
        return [dict(BENCH_ROW)]

    monkeypatch.setattr(loss_benchmarks, "run_query", flaky)
    assert loss_benchmarks.benchmark_for_state("TX").available is False
    assert loss_benchmarks.benchmark_for_state("TX").available is True


def test_weather_cache_is_thread_safe_under_eviction(monkeypatch):
    # Many threads inserting distinct keys into a tiny cache forces constant
    # eviction. Without the lock two threads can pop the same "oldest" key.
    monkeypatch.setattr(weather_events, "_CACHE_MAX", 4)
    monkeypatch.setattr(weather_events, "run_query", lambda *a, **k: [])
    monkeypatch.setattr(weather_events, "get_settings", lambda: type("S", (), {"enable_weather_check": True})())
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            weather_events.check_weather("77002", (datetime(2020, 1, 1) + timedelta(days=n)).date().isoformat())
        except BaseException as exc:  # pragma: no cover - the failure we guard against
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(weather_events._RESULT_CACHE) <= 4


# ---------------------------------------------------------------------------
# 4. Timezone
# ---------------------------------------------------------------------------
@pytest.fixture
def fresh_settings(monkeypatch):
    """Rebuild settings from env only (ignore a developer's .env)."""

    monkeypatch.setattr(settings_module, "load_dotenv_if_present", lambda path=None: None)
    settings_module.get_settings.cache_clear()
    settings_module._load_zone.cache_clear()
    yield
    settings_module.get_settings.cache_clear()
    settings_module._load_zone.cache_clear()


def test_timezone_defaults_to_central(monkeypatch, fresh_settings):
    monkeypatch.delenv("CLAIMDESK_TIMEZONE", raising=False)
    assert settings_module.get_settings().timezone == "America/Chicago"
    now = settings_module.local_now()
    assert str(now.tzinfo) == "America/Chicago"
    assert now.utcoffset() in (timedelta(hours=-5), timedelta(hours=-6))  # CDT / CST


def test_timezone_can_be_configured(monkeypatch, fresh_settings):
    monkeypatch.setenv("CLAIMDESK_TIMEZONE", "America/Denver")
    assert str(settings_module.local_now().tzinfo) == "America/Denver"


def test_unknown_timezone_falls_back_to_utc_with_a_warning(monkeypatch, fresh_settings, caplog):
    monkeypatch.setenv("CLAIMDESK_TIMEZONE", "Mars/Olympus_Mons")
    with caplog.at_level(logging.WARNING):
        now = settings_module.local_now()
    assert now.utcoffset() == timedelta(0)
    assert "falling back to UTC" in caplog.text


def test_weather_future_check_uses_desk_today_not_server_today(monkeypatch):
    # 21:00 in Chicago on Sep 20 = 02:00 UTC on Sep 21. A loss "on the 21st"
    # is still in the future for the claimant.
    monkeypatch.setattr(weather_events, "local_now", lambda: datetime(2026, 9, 20, 21, 0, tzinfo=ZoneInfo("America/Chicago")))
    monkeypatch.setattr(weather_events, "get_settings", lambda: type("S", (), {"enable_weather_check": True})())
    monkeypatch.setattr(weather_events, "run_query", lambda *a, **k: [])
    assert weather_events.check_weather("77002", "2026-09-21").note == "Loss date is in the future"
    assert weather_events.check_weather("77002", "2026-09-20").checked is True
    assert weather_events.check_weather("77002", "09/20/2026").checked is True  # non-ISO dates accepted too


# ---------------------------------------------------------------------------
# 5. Log redaction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["504-555-0199", "(504) 555-0199", "+1 504.555.0199", "5045550199", "1-800-555-0142", "+44 20 7946 0958"])
def test_redact_masks_phone_numbers(text):
    assert redact(f"call {text} please") == "call [phone] please"


@pytest.mark.parametrize("text", ["loss on 2026-09-20", "policy FLD-TX-7Q2K9M", "policy FLD-CO-234567", "ZIP 77002-1234", "id 218494902", "$15,000"])
def test_redact_keeps_dates_policy_numbers_and_zips(text):
    assert redact(text) == text


def test_redact_masks_email():
    assert redact("mail ana@example.com") == "mail [email]"
