"""Central configuration for ClaimDesk.

WHY A SETTINGS MODULE?
    Every value that differs between your laptop and Cloud Run (project id,
    bucket name, brand, limits...) is read from *environment variables* in ONE
    place. The rest of the code imports ``get_settings()`` and never touches
    ``os.environ`` directly. This is the "12-factor app" pattern and it is how
    Cloud Run expects you to configure a service.

ONE ENV FILE FOR EVERYTHING
    ``.env`` (copied from ``.env.example``) is the single source of truth:
      * the Python app reads it here (``load_dotenv_if_present``),
      * the deploy scripts ``source`` it (``deploy/00_variables.sh``),
      * Cloud Run receives the runtime keys through ``--env-vars-file``
        generated from it (``deploy/05_deploy_cloud_run.sh``).
    Real environment variables always win over values in ``.env``.

REGION POLICY
    * **All GCP resources live in ``us-central1``** (``CLAIMDESK_REGION``):
      Cloud Run, Artifact Registry, Cloud Storage, Firestore **and BigQuery**.
      (The NOAA/ZIP public reference data is copied into our us-central1
      dataset by the data pipeline, so no query ever leaves the region.)
    * **Gemini models use the ``global`` endpoint** (``GOOGLE_CLOUD_LOCATION``,
      ``LIVE_MODEL_LOCATION``). Global routes each request to wherever Google
      has capacity -> best availability, fewest "429 resource exhausted".
    * The Live (streaming voice) model may not be offered on ``global`` for
      every project. The app therefore retries ONE time on
      ``LIVE_MODEL_FALLBACK_LOCATION`` (defaults to ``CLAIMDESK_REGION`` =
      us-central1) if the global connection is rejected. Run
      ``scripts/check_models.py`` once to see what your project supports.
    * **Vertex AI Search** (grounding on FEMA NFIP documents) is the one
      deliberate exception: its data stores exist only in ``global``/``us``/
      ``eu``, so it lives in the **``us`` multi-region**
      (``CLAIMDESK_SEARCH_LOCATION``). Data still stays in the United States.

PRODUCT NAME
    The app is presented as **"Demo Tideline"** (``CLAIMDESK_BRAND_NAME``), a
    fictitious flood insurer. ``claimdesk`` remains the internal code name
    (Python package, BigQuery dataset) because ADK and SQL refer to it.

IMPORTANT: model *names* are fixed by requirement and must not change:
    gemini-3.8-live, gemini-3.8-flash, gemini-3.1-flash-image
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, tzinfo
from datetime import timezone as dt_timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The five in-scope states span Eastern (FL, NC), Central (TX, LA) and
# Mountain (CO) time. Central is the middle ground: at most one hour off for
# any of them, so "today" can only be wrong in the hour around midnight.
DEFAULT_TIMEZONE = "America/Chicago"


def load_dotenv_if_present(path: Path | None = None) -> None:
    """Load ``KEY=VALUE`` lines from ``.env`` without overriding real env vars.

    We deliberately avoid an extra dependency (python-dotenv). Lines starting
    with ``#`` are comments. Quotes around values are stripped. Inline
    comments are NOT supported (keep comments on their own line).
    """

    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    try:
        return float(value) if value else default
    except ValueError:
        return default


def _str(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


@dataclass(frozen=True)
class Settings:
    """Immutable bag of configuration values (``frozen=True`` = read-only)."""

    # --- Google Cloud project -------------------------------------------------
    project_id: str
    region: str  # us-central1: Cloud Run, GCS, Firestore, BigQuery, Artifact Registry

    # --- Gemini models on Vertex AI (names MUST NOT change) ------------------
    model_location: str  # endpoint for flash + image models ("global")
    live_model_location: str  # endpoint for the Live model ("global")
    live_model_fallback_location: str  # tried once if global rejects Live ("us-central1")
    live_model: str
    reasoning_model: str  # extraction, classification, photo verification
    sketch_model: str
    voice_name: str

    # --- Data services --------------------------------------------------------
    bq_dataset: str  # our dataset, e.g. "claimdesk"
    bq_location: str  # "us-central1" (same as region)
    gcs_bucket: str  # private bucket for photos / sketches / packets
    firestore_database: str  # "(default)" unless you created a named one
    firestore_collection: str

    # --- Business scope -------------------------------------------------------
    supported_states: tuple[str, ...]  # two-letter codes, e.g. ("CO","TX","FL","LA","NC")

    # --- Brand (fictitious company shown in the UI and spoken by the agent) ---
    brand_name: str
    brand_tagline: str
    agent_display_name: str  # the voice agent's first name
    claims_phone: str  # fictitious 555 number shown in the UI

    # --- Limits (per Cloud Run instance / per intake) -------------------------
    max_sessions: int
    max_photos: int
    max_message_bytes: int
    live_session_minutes: int
    idle_expiry_minutes: int
    camera_fps: float
    frame_max_age_seconds: float

    # --- Grounding: Vertex AI Search over FEMA NFIP documents -------------------
    # The ONE deliberate exception to the us-central1 rule: Vertex AI Search
    # data stores only exist in "global", "us" or "eu" multi-regions. We use
    # "us" (data stays in the United States). See docs/grounding.md.
    search_location: str  # "us"
    search_engine_id: str  # the Vertex AI Search app (engine) id, e.g. "demo-tideline-search"
    enable_guidance_search: bool  # the lookup_flood_guidance voice tool

    # --- Behaviour switches ---------------------------------------------------
    storage_backend: str  # "gcp" (Firestore+GCS) or "memory" (tests / offline dev)
    enable_cloud_trace: bool
    enable_weather_check: bool
    log_level: str
    # IANA timezone used for "today" / "yesterday" (e.g. "America/Chicago").
    # Cloud Run's clock is UTC; see local_now() below for why that matters.
    timezone: str

    # --- Runtime detection ----------------------------------------------------
    running_on_cloud_run: bool  # Cloud Run always sets K_SERVICE

    @property
    def bq_prefix(self) -> str:
        """Fully-qualified dataset prefix used in SQL, e.g. ``my-proj.claimdesk``."""

        return f"{self.project_id}.{self.bq_dataset}"

    @property
    def live_locations(self) -> tuple[str, ...]:
        """Locations to try for the Live model, in order, without duplicates."""

        ordered = [self.live_model_location, self.live_model_fallback_location]
        return tuple(dict.fromkeys(loc for loc in ordered if loc))

    def public_config(self) -> dict[str, object]:
        """Non-secret values the browser UI may show (brand, limits, scope)."""

        return {
            "brand_name": self.brand_name,
            "brand_tagline": self.brand_tagline,
            "agent_display_name": self.agent_display_name,
            "claims_phone": self.claims_phone,
            "supported_states": list(self.supported_states),
            "max_photos": self.max_photos,
            "live_session_minutes": self.live_session_minutes,
            "camera_fps": self.camera_fps,
            "frame_max_age_seconds": self.frame_max_age_seconds,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build settings once and cache them for the life of the process."""

    load_dotenv_if_present()
    region = _str("CLAIMDESK_REGION", "us-central1")
    states = tuple(s.strip().upper() for s in _str("CLAIMDESK_SUPPORTED_STATES", "CO,TX,FL,LA,NC").split(",") if s.strip())
    return Settings(
        project_id=os.getenv("GOOGLE_CLOUD_PROJECT", "").strip(),
        region=region,
        model_location=_str("GOOGLE_CLOUD_LOCATION", "global"),
        live_model_location=_str("LIVE_MODEL_LOCATION", "global"),
        live_model_fallback_location=_str("LIVE_MODEL_FALLBACK_LOCATION", region),
        live_model=_str("CLAIMDESK_LIVE_MODEL", "gemini-3.8-live"),
        reasoning_model=_str("CLAIMDESK_REASONING_MODEL", "gemini-3.8-flash"),
        sketch_model=_str("CLAIMDESK_SKETCH_MODEL", "gemini-3.1-flash-image"),
        voice_name=_str("CLAIMDESK_VOICE", "Kore"),
        bq_dataset=_str("CLAIMDESK_BQ_DATASET", "claimdesk"),
        # BigQuery lives in the same region as everything else.
        bq_location=_str("CLAIMDESK_BQ_LOCATION", region),
        gcs_bucket=_str("CLAIMDESK_GCS_BUCKET", ""),
        firestore_database=_str("CLAIMDESK_FIRESTORE_DATABASE", "(default)"),
        firestore_collection=_str("CLAIMDESK_FIRESTORE_COLLECTION", "intakes"),
        supported_states=states,
        brand_name=_str("CLAIMDESK_BRAND_NAME", "Demo Tideline"),
        brand_tagline=_str("CLAIMDESK_BRAND_TAGLINE", "Flood claims, handled with care."),
        agent_display_name=_str("CLAIMDESK_AGENT_NAME", "Maya"),
        claims_phone=_str("CLAIMDESK_CLAIMS_PHONE", "1-800-555-0142"),
        max_sessions=_int("CLAIMDESK_MAX_SESSIONS", 32),
        max_photos=_int("CLAIMDESK_MAX_PHOTOS", 20),
        max_message_bytes=_int("CLAIMDESK_MAX_MESSAGE_BYTES", 800_000),
        live_session_minutes=_int("CLAIMDESK_LIVE_SESSION_MINUTES", 20),
        idle_expiry_minutes=_int("CLAIMDESK_IDLE_EXPIRY_MINUTES", 30),
        camera_fps=_float("CLAIMDESK_CAMERA_FPS", 1.0),
        frame_max_age_seconds=_float("CLAIMDESK_FRAME_MAX_AGE_SECONDS", 12.0),
        search_location=_str("CLAIMDESK_SEARCH_LOCATION", "us"),
        search_engine_id=_str("CLAIMDESK_SEARCH_ENGINE_ID", ""),
        # On only when explicitly enabled AND an engine id is configured, so a
        # fresh checkout (no Vertex AI Search yet) never calls a missing engine.
        enable_guidance_search=_bool("CLAIMDESK_ENABLE_GUIDANCE_SEARCH", False) and bool(_str("CLAIMDESK_SEARCH_ENGINE_ID", "")),
        storage_backend=_str("CLAIMDESK_STORAGE_BACKEND", "gcp").lower(),
        enable_cloud_trace=_bool("CLAIMDESK_ENABLE_CLOUD_TRACE", True),
        enable_weather_check=_bool("CLAIMDESK_ENABLE_WEATHER_CHECK", True),
        log_level=_str("CLAIMDESK_LOG_LEVEL", "INFO").upper(),
        timezone=_str("CLAIMDESK_TIMEZONE", DEFAULT_TIMEZONE),
        running_on_cloud_run=bool(os.getenv("K_SERVICE")),
    )


@lru_cache(maxsize=8)
def _load_zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # Slim container images may ship without the IANA database
        # (/usr/share/zoneinfo). The ``tzdata`` PyPI package provides it; if
        # neither is there, or the name is misspelled, we fall back to UTC
        # instead of crashing. Cached, so the warning is logged once per name.
        logging.getLogger(__name__).warning("Timezone %r could not be loaded; falling back to UTC", name)
        return dt_timezone.utc


def local_zone() -> tzinfo:
    """The desk's timezone (``CLAIMDESK_TIMEZONE``), or UTC if it can't be loaded."""

    return _load_zone(get_settings().timezone or DEFAULT_TIMEZONE)


def local_now() -> datetime:
    """Timezone-aware "now" in the desk's timezone.

    WHY NOT ``datetime.now()`` / ``date.today()``?
        Those use the *server's* clock zone, and Cloud Run runs in UTC. At
        8 pm in Houston it is already tomorrow in UTC, so a claimant saying
        "it flooded yesterday" would get the wrong date, and a loss that
        happened today could look like a future date. The pipeline passes
        this value to Gemini as the reference date and to the rules.
    """

    return datetime.now(local_zone())


def ensure_vertex_ai_env() -> None:
    """Tell the ``google-genai`` SDK (and ADK) to use Vertex AI, not API keys.

    ADK's ``LlmAgent`` builds its own Gemini client from these variables, so
    we set them before any agent runs. With ``GOOGLE_GENAI_USE_ENTERPRISE=True``
    authentication uses Application Default Credentials (ADC):
      * on your laptop -> ``gcloud auth application-default login``
      * on Cloud Run   -> the service account attached to the service
    No API key is ever stored anywhere.
    """

    settings = get_settings()
    # google-genai >= 2.x / ADK 2.x read GOOGLE_GENAI_USE_ENTERPRISE (Vertex AI
    # is now part of "Gemini Enterprise Agent Platform"). The older name
    # GOOGLE_GENAI_USE_VERTEXAI still works but logs a deprecation warning, so
    # we only set the new one. Both mean "use Vertex AI with ADC".
    os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "True"
    if settings.project_id:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", settings.project_id)
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", settings.model_location)


__all__ = [
    "DEFAULT_TIMEZONE",
    "PROJECT_ROOT",
    "Settings",
    "ensure_vertex_ai_env",
    "get_settings",
    "load_dotenv_if_present",
    "local_now",
    "local_zone",
]
