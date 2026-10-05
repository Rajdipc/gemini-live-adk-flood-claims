"""One place that decides *how much* of a failure is safe to log.

WHY does this exist?
    ``log.exception(...)`` writes the full stack trace, including every
    chained cause (``raise ModelCallError(...) from exc``). For Gemini calls
    that cause is often a pydantic ``ValidationError`` or an API error whose
    text quotes the model's output - which can contain the claimant's name,
    address or phone number. Stack traces go to Cloud Logging and Error
    Reporting, where we must not keep personal data.

    So for ``ModelCallError`` we log only our own short message (masked with
    ``redact`` as a second safety net) and *no* traceback. Every other
    unexpected error still gets the full trace, because that is what you need
    to fix a real bug.
"""

from __future__ import annotations

import logging
from typing import Any

from claimdesk.errors import ModelCallError
from claimdesk.observability import redact


def log_failure(logger: logging.Logger, message: str, exc: BaseException, *, extra: dict[str, Any] | None = None) -> None:
    """Log ``exc`` once at ERROR level, without a traceback for model errors."""

    if isinstance(exc, ModelCallError):
        logger.error("%s: %s", message, redact(str(exc), limit=300), extra=extra)
    else:
        logger.error(message, exc_info=(type(exc), exc, exc.__traceback__), extra=extra)


__all__ = ["log_failure"]
