"""ClaimDesk web server: FastAPI app for Cloud Run (REST + WebSocket + static UI).

HOW A REQUEST FLOWS ON GOOGLE CLOUD
===================================
::

    browser --HTTPS--> Identity-Aware Proxy (IAP) --> Cloud Run (this app)
                         |  checks the Google sign-in and the IAM role
                         |  "IAP-secured Web App User"; adds headers:
                         |    X-Goog-Authenticated-User-Email: accounts.google.com:you@example.com
                         |    X-Goog-IAP-JWT-Assertion: <signed JWT>
                         v

    * **IAP does the real authentication.** Requests that do not pass IAP never
      reach the container (when IAP is the only way in). We use the signed-in
      e-mail as the *owner* of each intake: users can only see their own
      intakes.
    * **On Cloud Run we fail closed and trust only the signed JWT.** The plain
      ``X-Goog-Authenticated-User-Email`` header is NOT signed: if the service
      were ever reachable without IAP (a mis-set ingress or IAM binding),
      anyone could send it. So on Cloud Run (``K_SERVICE`` is set) every
      request must carry a valid ``X-Goog-IAP-JWT-Assertion``; we verify its
      signature against Google's public keys, its issuer
      (``https://cloud.google.com/iap``) and its audience, and take the
      e-mail from the verified claims (``verify_iap_jwt``).
      The expected audience for IAP on Cloud Run is
      ``/projects/<PROJECT_NUMBER>/locations/<REGION>/services/<SERVICE>``
      (https://cloud.google.com/iap/docs/signed-headers-howto). Set
      ``CLAIMDESK_IAP_AUDIENCE`` to pin it; otherwise it is derived from the
      metadata server (project number), ``CLAIMDESK_REGION`` and ``K_SERVICE``
      (``expected_iap_audience``). If it cannot be derived we still verify the
      signature and issuer, and log a warning that the audience is unchecked.
    * **Locally** (not on Cloud Run) there is no IAP, so the owner falls back
      to ``local-dev`` unless a test sends the header explicitly. Setting
      ``CLAIMDESK_IAP_AUDIENCE`` locally turns on the same JWT check.

ROUTES
======
    GET    /                                   notebook UI (static files)
    GET    /api/health, /healthz               liveness + model names
    GET    /api/config                         brand, supported states, limits (for the UI)
    POST   /api/intakes                        start an intake
    GET    /api/intakes/{id}                   current UI state
    DELETE /api/intakes/{id}                   discard an intake (+ its photos)
    POST   /api/intakes/{id}/photos            upload a JPEG as evidence
    GET    /api/intakes/{id}/evidence/{eid}    photo bytes (streamed via the app)
    GET    /api/intakes/{id}/sketch            sketch bytes
    GET    /api/intakes/{id}/packet            packet as JSON
    GET    /api/intakes/{id}/packet.zip        packet ZIP download (+ archive)
    WS     /ws/live?intake_id=...              the live voice/camera call

    Note: Cloud Run's front end reserves some paths ending in "z" (such as
    ``/healthz``), so external checks should use ``/api/health``. ``/healthz``
    is kept for local tooling and parity.

OBSERVABILITY
=============
    ``setup_logging()`` makes every log line structured JSON on Cloud Run;
    the middleware binds the Cloud Trace id from ``X-Cloud-Trace-Context`` /
    ``traceparent`` so logs and traces link up; any ``logger.exception`` becomes
    an Error Reporting entry (see ``claimdesk/observability.py``).

Run locally::

    uv run --no-sync uvicorn webapp.main:app --port 8080
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from claimdesk.errors import (
    ClaimDeskError,
    ConfigurationError,
    DataAccessError,
    IntakeNotFoundError,
    LimitExceededError,
    ModelCallError,
    StorageError,
)
from claimdesk.observability import bind_request_context, get_logger, setup_logging, setup_tracing
from claimdesk.settings import get_settings, load_dotenv_if_present

# Configure env + logging before importing modules that log at import time.
load_dotenv_if_present()
setup_logging()

from . import tool_handlers  # noqa: E402
from .desk_view import current_result, evidence_manifest, packet_markdown  # noqa: E402
from .error_logging import log_failure  # noqa: E402
from .evidence_store import build_evidence_store  # noqa: E402
from .intake_session import IntakeRegistry, LiveIntake, demo_limits, photo_limit_error  # noqa: E402
from .intake_store import build_intake_store  # noqa: E402
from .live_bridge import run_live_call  # noqa: E402
from .packet_archive import archive_packet  # noqa: E402
from .trace_logger import get_trace_logger  # noqa: E402
from claimdesk import knowledge  # noqa: E402
from .voice_tools import active_tool_names  # noqa: E402

log = get_logger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# HTTP-level limits. The product limits (sessions, photos, message size, call
# length, idle expiry, frame age) come from settings / env vars - see
# ``intake_session.demo_limits()`` and ``claimdesk/settings.py``.
MAX_JSON_BODY_BYTES = 16_000
MAX_UPLOAD_BYTES = 5_000_000
SWEEP_INTERVAL_SECONDS = 60
SKETCH_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})

# ---------------------------------------------------------------------------
# Identity (IAP)
# ---------------------------------------------------------------------------
IAP_EMAIL_HEADER = "x-goog-authenticated-user-email"
IAP_JWT_HEADER = "x-goog-iap-jwt-assertion"
IAP_PUBLIC_KEYS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
LOCAL_OWNER = "local-dev"
# The metadata server answers only from inside Google Cloud (Cloud Run too).
METADATA_PROJECT_NUMBER_URL = "http://metadata.google.internal/computeMetadata/v1/project/numeric-project-id"
# Intake ids are ``uuid.uuid4().hex`` (see ``IntakeRegistry.create``): 32
# lowercase hex characters. Anything else is rejected before touching storage.
INTAKE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
NOT_FOUND_MESSAGE = "Intake not found or expired. Start a new intake."


class AccessDeniedError(ClaimDeskError):
    """The request carries no (valid) IAP identity."""

    user_message = "Please sign in to use the claim desk."


class IdentityUnavailableError(ClaimDeskError):
    """We could not *check* the sign-in (IAP public keys unreachable) -> HTTP 503.

    Different from ``AccessDeniedError`` (401) on purpose: a network blip on
    our side must not tell a correctly signed-in user that they are signed out.
    """

    user_message = "We couldn't confirm your sign-in just now. Please try again in a moment."
    retryable = True


class PayloadTooLargeError(LimitExceededError):
    """An upload or request body is bigger than allowed (HTTP 413)."""

    user_message = "That file is too large."


_iap_http_request: Any = None
# (assertion, audience) -> (cache-until epoch seconds, verified claims)
_iap_token_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_IAP_CACHE_TTL_SECONDS = 60.0
_IAP_CACHE_MAX_ENTRIES = 128
# (monotonic time fetched, project number or None). A failed lookup is
# retried after _PROJECT_NUMBER_RETRY_SECONDS; a successful one is kept.
_project_number_cache: tuple[float, str | None] | None = None
_PROJECT_NUMBER_RETRY_SECONDS = 300.0
_audience_warning_logged = False


def _get_iap_http_request() -> Any:
    global _iap_http_request
    if _iap_http_request is None:
        import google.auth.transport.requests

        _iap_http_request = google.auth.transport.requests.Request()
    return _iap_http_request


def verify_iap_jwt(assertion: str, audience: str | None) -> dict[str, Any]:
    """Verify IAP's signed header JWT and return its claims.

    IAP signs ``X-Goog-IAP-JWT-Assertion`` with ES256 keys published at
    ``IAP_PUBLIC_KEYS_URL``. ``verify_token`` checks the signature, expiry
    and (when ``audience`` is not None) the ``aud`` claim; we check the issuer.
    This is a *blocking* HTTP call (it downloads the public keys), so async
    code calls it through ``asyncio.to_thread``. A shared ``Request`` and a
    short claims cache avoid re-downloading the keys on every request; the
    cache never outlives the token's own ``exp``.

    Raises ``AccessDeniedError`` for a bad token and
    ``IdentityUnavailableError`` if the public keys cannot be fetched.
    """

    from google.auth import exceptions as auth_exceptions
    from google.oauth2 import id_token

    cache_key = (assertion, audience or "")
    cached = _iap_token_cache.get(cache_key)
    if cached and time.time() < cached[0]:
        return dict(cached[1])

    try:
        claims = id_token.verify_token(
            assertion,
            _get_iap_http_request(),
            audience=audience,
            certs_url=IAP_PUBLIC_KEYS_URL,
        )
    except auth_exceptions.TransportError as exc:  # must come before GoogleAuthError (its parent)
        raise IdentityUnavailableError(f"Could not fetch IAP public keys: {exc}") from exc
    except (ValueError, auth_exceptions.GoogleAuthError) as exc:
        raise AccessDeniedError(f"IAP JWT verification failed: {exc}") from exc
    except Exception as exc:  # fail closed on anything unexpected from the JWT libraries
        raise AccessDeniedError(f"IAP JWT verification failed: {type(exc).__name__}") from exc
    if claims.get("iss") != IAP_ISSUER:
        raise AccessDeniedError(f"Unexpected IAP JWT issuer: {claims.get('iss')!r}")
    now = time.time()
    try:
        cache_until = min(now + _IAP_CACHE_TTL_SECONDS, float(claims.get("exp") or 0))
    except (TypeError, ValueError):
        cache_until = 0.0
    if cache_until > now:
        if len(_iap_token_cache) >= _IAP_CACHE_MAX_ENTRIES:
            _iap_token_cache.pop(next(iter(_iap_token_cache)))
        _iap_token_cache[cache_key] = (cache_until, dict(claims))
    return dict(claims)


def _fetch_project_number() -> str | None:
    """Ask the metadata server for the numeric project id (BLOCKING; ~ms on Cloud Run)."""

    request = urllib.request.Request(METADATA_PROJECT_NUMBER_URL, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310 - fixed internal URL
            text = response.read().decode("ascii", "replace").strip()
    except (OSError, ValueError):
        return None
    return text if text.isdigit() else None


async def project_number() -> str | None:
    """Cached project number, fetched off the event loop (None if unknown)."""

    global _project_number_cache
    now = time.monotonic()
    cached = _project_number_cache
    if cached is not None and (cached[1] is not None or now - cached[0] < _PROJECT_NUMBER_RETRY_SECONDS):
        return cached[1]
    number = await asyncio.to_thread(_fetch_project_number)
    _project_number_cache = (now, number)
    return number


async def expected_iap_audience() -> str | None:
    """The ``aud`` IAP puts in its JWT for this service, or None if unknown.

    ``CLAIMDESK_IAP_AUDIENCE`` wins when set. Otherwise, for IAP enabled
    directly on Cloud Run, Google documents the audience as
    ``/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME``:
    the project number comes from the metadata server, the region from
    ``CLAIMDESK_REGION`` (Cloud Run runs in that region in this repo) and the
    service name from ``K_SERVICE`` (set by Cloud Run itself).
    """

    global _audience_warning_logged
    configured = os.getenv("CLAIMDESK_IAP_AUDIENCE", "").strip()
    if configured:
        return configured
    service = os.getenv("K_SERVICE", "").strip()
    region = get_settings().region
    number = await project_number() if service else None
    if service and region and number:
        return f"/projects/{number}/locations/{region}/services/{service}"
    if not _audience_warning_logged:
        _audience_warning_logged = True
        log.warning(
            "IAP JWT audience could not be derived; checking signature and issuer only. Set CLAIMDESK_IAP_AUDIENCE to also check the audience.",
            extra={"json_fields": {"service": service or None, "region": region or None, "project_number_known": bool(number)}},
        )
    return None


def _email_from_header(value: str | None) -> str:
    """``accounts.google.com:you@example.com`` -> ``you@example.com``."""

    text = (value or "").strip()
    if ":" in text:
        text = text.split(":", 1)[1]
    return text.strip().lower()


async def identify_user(headers: Any) -> str:
    """Return the owner identity for a request (HTTP or WebSocket).

    On Cloud Run (or whenever ``CLAIMDESK_IAP_AUDIENCE`` is set) this FAILS
    CLOSED: no valid signed JWT -> ``AccessDeniedError`` (401), and the
    e-mail always comes from the verified claims. Locally there is no IAP, so
    the plain header (tests) or ``local-dev`` is used.
    """

    header_email = _email_from_header(headers.get(IAP_EMAIL_HEADER))
    must_verify = get_settings().running_on_cloud_run or bool(os.getenv("CLAIMDESK_IAP_AUDIENCE", "").strip())
    if not must_verify:
        return header_email or LOCAL_OWNER
    assertion = headers.get(IAP_JWT_HEADER)
    if not assertion:
        # On Cloud Run every request should come through IAP. No JWT means
        # IAP is bypassed or not configured - refuse rather than trust an
        # unsigned header or share one anonymous owner.
        raise AccessDeniedError("request reached the app without an IAP JWT assertion")
    audience = await expected_iap_audience()
    claims = await asyncio.to_thread(verify_iap_jwt, assertion, audience)
    email = _email_from_header(str(claims.get("email", "")))
    if not email:
        raise AccessDeniedError("IAP JWT has no email claim")
    if header_email and header_email != email:
        raise AccessDeniedError("IAP e-mail header does not match the signed JWT")
    return email


async def current_owner(request: Request) -> str:
    return await identify_user(request.headers)


def registry_of(request: Request) -> IntakeRegistry:
    return request.app.state.registry


def checked_intake_id(intake_id: str | None) -> str:
    """Reject malformed ids with the same 404 as "not found" (reveals nothing)."""

    if not INTAKE_ID_PATTERN.fullmatch(intake_id or ""):
        raise IntakeNotFoundError("malformed intake id", user_message=NOT_FOUND_MESSAGE)
    return str(intake_id)


async def owned_intake(request: Request, intake_id: str, owner: str) -> LiveIntake:
    return await registry_of(request).get_owned(checked_intake_id(intake_id), owner)


def _origin_allowed(origin: str | None, host: str | None) -> bool:
    """Same-origin check (blocks cross-site form posts / WebSocket hijacking).

    Compares host names only: Cloud Run terminates TLS, so the app sees
    ``http`` while the browser's Origin says ``https``.
    """

    if not origin:
        return False
    extra = {o.strip() for o in os.getenv("CLAIMDESK_ALLOWED_ORIGINS", "").split(",") if o.strip()}
    return origin in extra or urlparse(origin).netloc == (host or "")


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_tracing()
    settings = get_settings()
    tracer = get_trace_logger()
    app.state.registry = IntakeRegistry(build_intake_store(settings), build_evidence_store(settings), tracer)

    async def sweep_forever() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                await app.state.registry.sweep()
            except ClaimDeskError:
                log.exception("Idle intake sweep failed")

    sweeper = asyncio.create_task(sweep_forever())
    log.info(
        "ClaimDesk web app started",
        extra={"json_fields": {"live_model": settings.live_model, "storage_backend": settings.storage_backend, "limits": demo_limits()}},
    )
    try:
        yield
    finally:
        sweeper.cancel()
        await asyncio.gather(sweeper, return_exceptions=True)
        await app.state.registry.shutdown()
        await tracer.stop()


app = FastAPI(title="ClaimDesk flood intake", lifespan=lifespan)


@app.middleware("http")
async def request_guard(request: Request, call_next):
    """Runs for every HTTP request, before the route."""

    # 1) Link every log line of this request to its Cloud Trace trace.
    bind_request_context(trace_header=request.headers.get("x-cloud-trace-context"), traceparent=request.headers.get("traceparent"))
    # 2) State-changing requests must come from our own page.
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not _origin_allowed(request.headers.get("origin"), request.headers.get("host")):
        return JSONResponse({"detail": "Origin not allowed."}, status_code=403)
    # 3) Cheap body-size guard before anything is parsed.
    try:
        length = int(request.headers.get("content-length") or 0)
    except ValueError:
        return JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)
    limit = MAX_UPLOAD_BYTES + 64_000 if request.url.path.endswith("/photos") else MAX_JSON_BODY_BYTES
    if request.url.path.startswith("/api") and length > limit:
        return JSONResponse({"detail": "Request too large."}, status_code=413)
    response = await call_next(request)
    # Claim data is personal: never let browsers or proxies cache it.
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def _status_for(exc: ClaimDeskError) -> int:
    if isinstance(exc, AccessDeniedError):
        return 401
    if isinstance(exc, IdentityUnavailableError):
        return 503
    if isinstance(exc, IntakeNotFoundError):
        return 404
    if isinstance(exc, PayloadTooLargeError):
        return 413
    if isinstance(exc, LimitExceededError):
        return 429
    if isinstance(exc, ModelCallError):
        return 502
    if isinstance(exc, (StorageError, DataAccessError)):
        return 503
    if isinstance(exc, ConfigurationError):
        return 500
    return 400


@app.exception_handler(ClaimDeskError)
async def claimdesk_error_handler(request: Request, exc: ClaimDeskError) -> JSONResponse:
    """The single place REST errors are logged and turned into safe messages."""

    status = _status_for(exc)
    fields = {"json_fields": {"path": request.url.path, "status": status, "error_type": type(exc).__name__}}
    if status >= 500:
        log_failure(log, "Request failed", exc, extra=fields)  # -> Error Reporting (no trace for model errors: PII)
    else:
        log.warning("Request rejected: %s", exc, extra=fields)
    return JSONResponse({"detail": exc.user_message, "retryable": exc.retryable}, status_code=status)


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error", exc_info=exc, extra={"json_fields": {"path": request.url.path}})
    return JSONResponse({"detail": ClaimDeskError.user_message, "retryable": True}, status_code=500)


# ---------------------------------------------------------------------------
# REST routes
# ---------------------------------------------------------------------------
def _health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "ok": True,
        "live_model": settings.live_model,
        "reasoning_model": settings.reasoning_model,
        "sketch_model": settings.sketch_model,
        "voice": settings.voice_name,
        "tools": active_tool_names(settings),
        "storage_backend": settings.storage_backend,
        # Accuracy add-ons, so an operator can confirm them after a deploy:
        "skill_loaded": knowledge.skill_loaded() and knowledge.skill_enabled(),  # skills/nfip-flood-intake baked into the image
        "guidance_search": settings.enable_guidance_search,  # Vertex AI Search grounding on/off
    }


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    return _health()


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return _health()


@app.get("/api/config")
async def public_config(owner: str = Depends(current_owner)) -> dict[str, Any]:
    """Brand + limits the browser needs to render itself.

    WHY AN ENDPOINT? The brand (company name, agent name, phone), the
    supported states and the limits are *configuration* (env vars read by
    ``claimdesk/settings.py``). Serving them here means the HTML/JS never
    hard-code them: change ``CLAIMDESK_BRAND_NAME`` and redeploy, and the page
    follows. ``settings.public_config()`` contains nothing secret, but it still
    sits behind the same IAP identity check as every other API route, and we
    also return who is signed in so the header can show it.
    """

    return {**get_settings().public_config(), "user": owner}


@app.post("/api/intakes")
async def create_intake(request: Request, owner: str = Depends(current_owner)) -> dict[str, Any]:
    intake = await registry_of(request).create(owner)
    return {"intake_id": intake.intake_id, "user": owner, "live_model": get_settings().live_model, "state": intake.state()}


@app.get("/api/intakes/{intake_id}")
async def read_intake(intake_id: str, request: Request, owner: str = Depends(current_owner)) -> dict[str, Any]:
    intake = await owned_intake(request, intake_id, owner)
    return {"intake_id": intake_id, "user": owner, "state": intake.state()}


@app.delete("/api/intakes/{intake_id}")
async def delete_intake(intake_id: str, request: Request, owner: str = Depends(current_owner)) -> dict[str, Any]:
    registry = registry_of(request)
    intake = await owned_intake(request, intake_id, owner)
    await registry.discard(intake, purge=True)
    return {"deleted": True}


async def _refresh_after_upload(registry: IntakeRegistry, intake: LiveIntake) -> None:
    """Background: re-run the pipeline so the checklist sees the new photo."""

    try:
        await registry.refresh_pipeline(intake)
    except asyncio.CancelledError:
        raise
    except ClaimDeskError as exc:
        log_failure(log, "Pipeline refresh after photo upload failed", exc)
        return
    if intake.notify is not None:  # a live call is open: push the new state
        with contextlib.suppress(Exception):
            await intake.notify({"type": "state", "state": intake.state()})


@app.post("/api/intakes/{intake_id}/photos")
async def upload_photo(
    intake_id: str,
    request: Request,
    photo: UploadFile = File(...),
    description: str = Form(""),
    owner: str = Depends(current_owner),
) -> dict[str, Any]:
    """Add a JPEG from the claimant's device (e.g. a photo taken before cleanup)."""

    registry = registry_of(request)
    intake = await owned_intake(request, intake_id, owner)
    if len(intake.record.evidence_photos) >= get_settings().max_photos:
        raise photo_limit_error()
    data = await photo.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise PayloadTooLargeError("upload too large", user_message="Photos must be smaller than 5 MB.")
    if not data.startswith(b"\xff\xd8\xff"):
        raise HTTPException(status_code=415, detail="Photos must be JPEG images.")
    saved = await tool_handlers.store_evidence_photo(intake, registry, data, claimant_said=description[:1000], source="upload")
    await registry.persist(intake)
    intake.track(asyncio.create_task(_refresh_after_upload(registry, intake)))
    state = intake.state()
    return {"photo": next(p for p in state["evidence_photos"] if p["id"] == saved["id"]), "state": state}


@app.get("/api/intakes/{intake_id}/evidence/{evidence_id}")
async def evidence_bytes(intake_id: str, evidence_id: str, request: Request, owner: str = Depends(current_owner)) -> Response:
    registry = registry_of(request)
    intake = await owned_intake(request, intake_id, owner)
    photo = next((p for p in intake.record.evidence_photos if p["id"] == evidence_id), None)
    data = await registry.evidence.get(photo["object_path"]) if photo else None
    if data is None:
        raise HTTPException(status_code=404, detail="Evidence not found.")
    return Response(content=data, media_type="image/jpeg")


@app.get("/api/intakes/{intake_id}/sketch")
async def sketch_bytes(intake_id: str, request: Request, owner: str = Depends(current_owner)) -> Response:
    registry = registry_of(request)
    intake = await owned_intake(request, intake_id, owner)
    sketch = intake.record.sketch
    data = await registry.evidence.get(sketch["object_path"]) if sketch else None
    if data is None:
        raise HTTPException(status_code=404, detail="No sketch yet.")
    # The stored MIME type came from the image model's response. Only echo
    # known image types, so a surprising value (e.g. text/html) can never make
    # the browser treat these bytes as a page.
    mime_type = sketch.get("mime_type")
    return Response(content=data, media_type=mime_type if mime_type in SKETCH_MIME_TYPES else "image/png")


@app.get("/api/intakes/{intake_id}/packet")
async def packet_json(intake_id: str, request: Request, owner: str = Depends(current_owner)) -> dict[str, Any]:
    intake = await owned_intake(request, intake_id, owner)
    result = current_result(intake.record)
    return {
        "intake_id": intake_id,
        "markdown": packet_markdown(intake.record, result),
        "packet": {k: v for k, v in result["packet"].items() if k != "markdown"},
        "evidence": evidence_manifest(intake.record),
    }


@app.get("/api/intakes/{intake_id}/packet.zip")
async def packet_zip(intake_id: str, request: Request, owner: str = Depends(current_owner)) -> Response:
    registry = registry_of(request)
    intake = await owned_intake(request, intake_id, owner)
    data, _uri = await archive_packet(intake.record, registry.evidence)
    try:
        await registry.persist(intake)  # remembers packet_gcs_uri
    except StorageError:
        log.exception("Could not save intake after packet download")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="flood-claim-packet-{intake_id[:8]}.zip"'},
    )


# ---------------------------------------------------------------------------
# WebSocket: the live call
# ---------------------------------------------------------------------------
@app.websocket("/ws/live")
async def live_call(websocket: WebSocket) -> None:
    bind_request_context(trace_header=websocket.headers.get("x-cloud-trace-context"), traceparent=websocket.headers.get("traceparent"))
    # Browsers always send Origin on WebSocket upgrades; checking it stops
    # another site from opening a call with the user's IAP cookie.
    if not _origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=1008)
        return
    registry: IntakeRegistry = websocket.app.state.registry
    try:
        owner = await identify_user(websocket.headers)
        intake = await registry.get_owned(checked_intake_id(websocket.query_params.get("intake_id", "")), owner)
    except (AccessDeniedError, IntakeNotFoundError) as exc:
        log.warning("Live call rejected: %s", exc)
        await websocket.close(code=1008)
        return
    except IdentityUnavailableError as exc:
        # Google's sign-in keys were briefly unreachable: not the user's
        # fault and not a bug, so a warning (no stack trace) is enough.
        log.warning("Live call identity check unavailable: %s", exc)
        await websocket.close(code=1011)
        return
    except ClaimDeskError as exc:
        log_failure(log, "Live call could not load intake", exc)
        await websocket.close(code=1011)
        return
    if intake.live_socket is not None:  # one call per intake at a time
        await websocket.close(code=1008)
        return
    intake.live_socket = websocket  # claim the slot before awaiting accept()
    try:
        await websocket.accept()
        await run_live_call(websocket, intake, registry)
    finally:
        # WHY: if accept() fails (browser vanished mid-handshake) the bridge
        # never starts, so nothing else would clear the slot and every later
        # call for this claim would be refused as "already in a call". Only
        # clear it if it is still ours (a newer call may own it by now).
        if intake.live_socket is websocket:
            intake.live_socket = None
            intake.notify = None


# ---------------------------------------------------------------------------
# Static UI (mounted last so API routes take precedence)
# ---------------------------------------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


__all__ = ["IdentityUnavailableError", "app", "expected_iap_audience", "identify_user", "verify_iap_jwt"]
