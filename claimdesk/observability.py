"""Logging, error reporting and tracing, the Google Cloud way.

READ THIS FIRST (beginner notes)
================================
On Cloud Run you do NOT need to send logs over the network yourself. Anything
your container prints to stdout/stderr is collected by Cloud Run and shipped
to **Cloud Logging** automatically. The trick is to print **one JSON object per
line** with a few special keys. Cloud Logging then understands:

* ``severity``       -> INFO / WARNING / ERROR ... (enables filtering & alerts)
* ``message``        -> the human readable text
* ``logging.googleapis.com/trace`` -> links the log line to a Cloud Trace
  trace, so in the console you can click from a request to all its logs
* any other keys     -> searchable structured fields (``jsonPayload.intake_id``)

``google.cloud.logging.handlers.StructuredLogHandler`` produces exactly that
format, so we use it on Cloud Run. It is Google's recommended approach for
Cloud Run / GKE / Cloud Functions because it is synchronous (no background
thread that can lose logs when the instance is frozen) and cheap.

ERROR REPORTING
---------------
**Error Reporting** groups exceptions, counts them and can alert you. It picks
up a log line automatically when the line is ERROR (or worse) *and* contains a
stack trace. We make this explicit and reliable by adding the special
``@type: ...ReportedErrorEvent`` key plus a ``serviceContext`` (service name and
revision) to every error log that carries an exception. Result: every
``logger.exception(...)`` in this code base becomes an Error Reporting entry.

CLOUD TRACE
-----------
ADK emits OpenTelemetry "spans" for each agent step, LLM call and tool call.
``setup_tracing()`` installs an exporter that sends those spans to **Cloud
Trace**, where you can see a waterfall of how long each step took.

LOCAL DEVELOPMENT
-----------------
On your laptop JSON logs are hard to read, so we print a compact coloured-free
text format instead. Same logger calls, different output. Nothing to change in
the rest of the code.

PRIVACY
-------
Claim conversations contain personal data (names, phones, e-mails). Never log
full transcripts. Use ``redact()`` for any free text you log and prefer IDs
(``intake_id``) over content.
"""

from __future__ import annotations

import contextvars
import logging
import re
import sys
from typing import Any

from .settings import get_settings

# ---------------------------------------------------------------------------
# Request-scoped context
# ---------------------------------------------------------------------------
# A ContextVar is like a global variable that is private to the current
# asyncio task / request. The web layer sets these at the start of each HTTP
# request or WebSocket session; every log line written while handling that
# request automatically carries them. No need to pass IDs around by hand.
_trace_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace", default=None)
_span_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("span", default=None)
_intake_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("intake_id", default=None)

ERROR_EVENT_TYPE = "type.googleapis.com/google.devtools.clouderrorreporting.v1beta1.ReportedErrorEvent"

_configured = False


def bind_request_context(
    *, trace_header: str | None = None, traceparent: str | None = None, intake_id: str | None = None
) -> None:
    """Attach trace + intake identifiers to all logs of the current request.

    Cloud Run adds two headers to every incoming request:
      * ``traceparent``           (W3C standard) ``00-<trace_id>-<span_id>-01``
      * ``X-Cloud-Trace-Context`` (legacy)      ``<trace_id>/<span_id>;o=1``
    We read either and turn it into ``projects/<project>/traces/<trace_id>``,
    which is the exact string Cloud Logging needs for log <-> trace linking.
    """

    trace_id = span_id = None
    if traceparent:
        parts = traceparent.split("-")
        if len(parts) >= 3:
            trace_id, span_id = parts[1], parts[2]
    elif trace_header:
        head = trace_header.split(";")[0]
        trace_id, _, span_id = head.partition("/")
    project = get_settings().project_id
    if trace_id and project:
        _trace_ctx.set(f"projects/{project}/traces/{trace_id}")
        _span_ctx.set(span_id or None)
    if intake_id:
        _intake_ctx.set(intake_id)


def bind_intake(intake_id: str | None) -> None:
    """Set only the intake id (used when a WebSocket resumes an intake)."""

    _intake_ctx.set(intake_id)


class _ContextFilter(logging.Filter):
    """Copies ContextVar values onto every ``LogRecord``.

    ``StructuredLogHandler`` looks for ``record.trace``, ``record.span_id``
    and ``record.json_fields`` and turns them into the special JSON keys.
    """

    def __init__(self, service: str | None, revision: str | None) -> None:
        super().__init__()
        self._service_context = {"service": service or "claimdesk-local", "version": revision or "dev"}

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API name
        trace = _trace_ctx.get()
        if trace and not getattr(record, "trace", None):
            record.trace = trace
            record.span_id = _span_ctx.get()
        fields: dict[str, Any] = dict(getattr(record, "json_fields", None) or {})
        intake_id = _intake_ctx.get()
        if intake_id:
            fields.setdefault("intake_id", intake_id)
        fields.setdefault("logger", record.name)
        # Make exceptions show up in Error Reporting (see module docstring).
        if record.levelno >= logging.ERROR and record.exc_info:
            fields["@type"] = ERROR_EVENT_TYPE
            fields["serviceContext"] = self._service_context
        record.json_fields = fields
        return True


class _LocalFormatter(logging.Formatter):
    """Readable one-line format for your terminal."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in (getattr(record, "json_fields", {}) or {}).items() if k not in {"logger", "@type", "serviceContext"}}
        return f"{base}  {extras}" if extras else base


def setup_logging() -> None:
    """Configure the root logger once per process. Safe to call many times."""

    global _configured
    if _configured:
        return
    settings = get_settings()
    import os

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.log_level)

    if settings.running_on_cloud_run:
        # Import lazily so unit tests do not need the library configured.
        from google.cloud.logging.handlers import StructuredLogHandler

        handler: logging.Handler = StructuredLogHandler(project_id=settings.project_id or None, stream=sys.stdout)
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_LocalFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))

    handler.addFilter(_ContextFilter(os.getenv("K_SERVICE"), os.getenv("K_REVISION")))
    root.addHandler(handler)

    # Third-party libraries can be very chatty at INFO. Keep them at WARNING so
    # your logs (and your Cloud Logging bill) stay focused on the app.
    for noisy in ("httpx", "httpcore", "urllib3", "google.auth", "google.api_core", "websockets", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def setup_tracing() -> None:
    """Export OpenTelemetry spans (ADK + our own) to Cloud Trace.

    Only enabled on Cloud Run (or when you explicitly opt in locally), because
    exporting from a laptop needs the ``cloudtrace.agent`` role on your user.
    Failure to set up tracing must never crash the app, so errors are logged
    as warnings and the app continues without traces.
    """

    settings = get_settings()
    log = logging.getLogger(__name__)
    if not settings.enable_cloud_trace or not settings.project_id:
        log.info("Cloud Trace export disabled")
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        import os

        provider = TracerProvider(resource=Resource.create({"service.name": os.getenv("K_SERVICE", "claimdesk-local")}))
        # BatchSpanProcessor sends spans in the background in small batches,
        # so tracing adds almost no latency to user requests.
        provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter(project_id=settings.project_id)))
        trace.set_tracer_provider(provider)
        log.info("Cloud Trace export enabled")
    except Exception:  # pragma: no cover - depends on environment
        log.warning("Could not enable Cloud Trace; continuing without traces", exc_info=True)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger. Usage: ``log = get_logger(__name__)``."""

    return logging.getLogger(name)


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Phone shapes only: North American numbers ("504-555-0199", "(504) 555 0199",
# "+1 504.555.0199", "5045550199") and "+"-prefixed international numbers.
# The old "any 8+ digits with separators" pattern also masked ISO dates
# ("2026-09-20") and ZIP+4 codes, which made logs hard to debug. The
# look-arounds stop a match from starting/ending inside a longer token.
_PHONE = re.compile(
    r"(?<![\w-])(?:"
    r"(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}"
    r"|\+\d{1,3}(?:[\s.-]?\d{2,4}){2,5}"
    r")(?![\w-])"
)


def redact(text: Any, limit: int = 160) -> str:
    """Mask e-mails / phone numbers and truncate free text before logging it."""

    value = _PHONE.sub("[phone]", _EMAIL.sub("[email]", str(text or "")))
    return value if len(value) <= limit else value[: limit - 1] + "…"


__all__ = ["setup_logging", "setup_tracing", "get_logger", "bind_request_context", "bind_intake", "redact"]
