"""ClaimDesk - voice-first residential flood claim intake on Google Cloud.

ADK developer tools (``adk web``, ``adk api_server``, ``agents-cli eval``)
look for a variable called ``root_agent`` in this package. We expose it
*lazily* (only built when first asked for) so that importing a small module
such as ``claimdesk.rules`` in unit tests does not construct the whole agent.
"""

from __future__ import annotations

from typing import Any

__all__ = ["root_agent", "app"]


def __getattr__(name: str) -> Any:  # PEP 562: module-level lazy attributes
    if name in {"root_agent", "app"}:
        from . import intake_pipeline

        return getattr(intake_pipeline, name)
    raise AttributeError(name)
