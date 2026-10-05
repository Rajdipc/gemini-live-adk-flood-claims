"""Domain knowledge loader: turns the ``nfip-flood-intake`` Agent Skill into prompt text.

WHAT THIS MODULE DOES
    The folder ``skills/nfip-flood-intake/`` is an *Agent Skill* (the open
    format Google ADK supports): a ``SKILL.md`` with YAML front-matter plus
    ``references/*.md`` files. We load it with ADK's own loader,
    ``google.adk.skills.load_skill_from_dir``. The loader validates the
    folder: the front-matter ``name`` must equal the folder name, and a
    ``description`` is required.

    Then we hand each model only the parts it needs:

    ===================  ==================================================
    Consumer             Knowledge injected
    ===================  ==================================================
    extract_facts node   water_sources + extraction_examples
    classify_claim node  flood_basics + water_sources + classification_examples
    Live voice agent     skill body + flood_basics + water_sources +
                         documents_and_deadlines + safety + approved_language
    ===================  ==================================================

WHY INJECT TEXT INSTEAD OF USING ADK's ``SkillToolset``?
    ``SkillToolset`` lets an agent *call a tool* to read a skill on demand.
    That suits chatty agents. Our two pipeline agents use ``output_schema``
    (structured JSON output) and must answer in a single call, and the Live
    voice model should not spend a slow tool round-trip on basics it needs
    every call. Few-shot examples also work best when they sit directly
    in the instruction. So we read the skill once, at start-up, and append
    it to the instructions.

WHY STRIP CURLY BRACES?
    ADK treats ``{name}`` inside an instruction as a state placeholder (the
    classifier prompt relies on ``{claim_facts}``), and the voice prompt is
    filled with Python ``str.format``. A stray brace in a knowledge file would
    crash or silently corrupt a prompt, so :func:`_brace_free` removes any
    that slip in. A unit test also checks the files contain none.

MEASURING THE GAIN (A/B)
    Set ``CLAIMDESK_USE_SKILL=false`` to run WITHOUT the skill, e.g. for a
    "before" pipeline eval, then run again with the default (true) and
    compare the scores. See RUNBOOK Phase 8.

FAILURE BEHAVIOUR
    If the skill folder is missing (for example someone forgot to copy it
    into the container image), we log one warning and return empty strings.
    The agent then still works, just without the extra knowledge. Accuracy
    drops, but claimants are never blocked.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from .observability import get_logger
from .settings import PROJECT_ROOT

log = get_logger(__name__)

SKILL_NAME = "nfip-flood-intake"
SKILL_DIR: Path = PROJECT_ROOT / "skills" / SKILL_NAME

# Which reference files each consumer receives (order = order in the prompt).
EXTRACTOR_REFERENCES = ("water_sources.md", "extraction_examples.md")
CLASSIFIER_REFERENCES = ("flood_basics.md", "water_sources.md", "classification_examples.md")
VOICE_REFERENCES = (
    "flood_basics.md",
    "water_sources.md",
    "documents_and_deadlines.md",
    "safety.md",
    "approved_language.md",
)

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _brace_free(text: str) -> str:
    """Remove ``{`` and ``}`` so the text is safe inside ADK/``str.format`` templates."""

    return text.replace("{", "(").replace("}", ")")


def _clean(text: str) -> str:
    """Drop HTML comments (notes for human editors) and trim blank lines."""

    return _brace_free(_HTML_COMMENT.sub("", text)).strip()


@lru_cache(maxsize=1)
def _load() -> tuple[str, dict[str, str]]:
    """Load the skill once per process: ``(body, references)``.

    ``lru_cache`` makes this a one-time cost; the files never change while
    the container runs.
    """

    try:
        # Imported lazily so that importing claimdesk never fails if an older
        # ADK without the skills module is installed.
        from google.adk.skills import load_skill_from_dir

        skill = load_skill_from_dir(SKILL_DIR)
    except Exception as exc:  # noqa: BLE001 - degrade, never block a claimant
        log.warning(
            "Agent skill could not be loaded; continuing without domain knowledge",
            extra={"json_fields": {"skill": SKILL_NAME, "path": str(SKILL_DIR), "error": repr(exc)}},
        )
        return "", {}

    references = {name: _clean(str(body)) for name, body in (skill.resources.references or {}).items()}
    log.info(
        "Agent skill loaded",
        extra={"json_fields": {"skill": skill.name, "references": sorted(references)}},
    )
    return _clean(skill.instructions or ""), references


def skill_enabled() -> bool:
    """``CLAIMDESK_USE_SKILL`` (default true). Read each time so tests/evals can flip it."""

    return os.getenv("CLAIMDESK_USE_SKILL", "true").strip().lower() not in {"0", "false", "no", "off"}


def _bundle(title: str, names: tuple[str, ...], *, include_body: bool = False) -> str:
    if not skill_enabled():
        return ""
    body, references = _load()
    parts = [references[n] for n in names if references.get(n)]
    if include_body and body:
        parts.insert(0, body)
    if not parts:
        return ""
    separator = "\n\n---\n\n"
    return f"\n\n## {title}\n\n" + separator.join(parts) + "\n"


def for_extractor() -> str:
    """Knowledge appended to the fact-extractor instruction."""

    return _bundle("Domain knowledge and worked examples (from the nfip-flood-intake skill)", EXTRACTOR_REFERENCES)


def for_classifier() -> str:
    """Knowledge appended to the classifier instruction."""

    return _bundle("Domain knowledge and worked examples (from the nfip-flood-intake skill)", CLASSIFIER_REFERENCES)


def for_voice() -> str:
    """Knowledge appended to Maya's Live system instruction."""

    return _bundle("Flood intake knowledge (from the nfip-flood-intake skill)", VOICE_REFERENCES, include_body=True)


def skill_loaded() -> bool:
    """True when the skill folder was found and parsed (used by /healthz and tests)."""

    body, references = _load()
    return bool(body or references)


__all__ = [
    "SKILL_DIR",
    "SKILL_NAME",
    "for_classifier",
    "for_extractor",
    "for_voice",
    "skill_enabled",
    "skill_loaded",
]
