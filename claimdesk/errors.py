"""Application exception types.

WHY CUSTOM EXCEPTIONS?
    A raw ``google.api_core.exceptions.Forbidden`` deep inside a BigQuery call
    tells a developer something, but it should never reach the claimant's
    screen. We wrap low-level failures in a small hierarchy that carries:

    * ``user_message`` - safe, friendly text the UI may show
    * ``retryable``    - whether trying again later might succeed
    * the original exception as ``__cause__`` (via ``raise ... from exc``) so
      the full stack trace still reaches Cloud Logging / Error Reporting.

GUIDING RULES used throughout the code base
    1. Catch the *narrowest* exception you can handle, as close to the cause
       as possible, and re-raise as one of these types with ``from exc``.
    2. Log exactly once, at the boundary that decides what to do (usually the
       web layer or a tool handler) with ``logger.exception(...)`` - this is
       what creates the Error Reporting entry.
    3. Non-critical helpers (weather check, benchmarks) *degrade gracefully*:
       they log a WARNING and return "unknown" instead of failing the claim.
"""

from __future__ import annotations


class ClaimDeskError(Exception):
    """Base class for every expected, handled failure in ClaimDesk."""

    user_message = "Something went wrong on our side. Please try again in a moment."
    retryable = False

    def __init__(self, message: str, *, user_message: str | None = None, retryable: bool | None = None) -> None:
        super().__init__(message)
        if user_message is not None:
            self.user_message = user_message
        if retryable is not None:
            self.retryable = retryable


class ConfigurationError(ClaimDeskError):
    """A required setting (project, bucket, dataset) is missing or invalid."""

    user_message = "The service is not configured correctly. The operator has been notified."


class DataAccessError(ClaimDeskError):
    """BigQuery could not be queried (permissions, quota, network)."""

    user_message = "I couldn't reach the policy records just now. I'll keep taking your details."
    retryable = True


class StorageError(ClaimDeskError):
    """Firestore or Cloud Storage read/write failed."""

    user_message = "I couldn't save that just now. Please try again."
    retryable = True


class ModelCallError(ClaimDeskError):
    """A Gemini call on Vertex AI failed or returned unusable output."""

    user_message = "The assistant is having trouble right now. Please try again."
    retryable = True


class IntakeNotFoundError(ClaimDeskError):
    """The requested intake does not exist or belongs to someone else."""

    user_message = "That intake is not available. Start a new intake."


class LimitExceededError(ClaimDeskError):
    """A safety limit (photos per intake, message size, sessions) was reached."""

    user_message = "A limit for this demo was reached."


__all__ = [
    "ClaimDeskError",
    "ConfigurationError",
    "DataAccessError",
    "StorageError",
    "ModelCallError",
    "IntakeNotFoundError",
    "LimitExceededError",
]
