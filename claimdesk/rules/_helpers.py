"""Small helpers shared by the rule modules (pure functions, no I/O)."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)

BLANK_VALUES = {"", "unknown", "not specified", "unspecified", "n/a", "none", "not provided"}


def is_blank(value: Any) -> bool:
    """True for empty values and the placeholder strings the LLM uses."""

    return str(value or "").strip().lower() in BLANK_VALUES


def dedupe(items: list[str]) -> list[str]:
    """Remove duplicates (case-insensitive) while keeping the original order."""

    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y")


def parse_date(value: Any) -> date | None:
    """Parse the handful of date formats people and LLMs produce."""

    text = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", str(value or "").strip(), flags=re.IGNORECASE)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def as_model(model_type: type[M], value: Any) -> M:
    """Accept a model instance, a dict, a JSON string or None and return a model.

    ADK stores step outputs in session state as dicts or JSON strings, so every
    rule starts by normalizing its inputs with this helper.
    """

    if isinstance(value, model_type):
        return value
    if value is None:
        return model_type()
    if isinstance(value, str):
        return model_type.model_validate_json(value)
    return model_type.model_validate(value)


def has_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)
