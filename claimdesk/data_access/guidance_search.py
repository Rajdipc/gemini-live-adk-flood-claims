"""Grounded FEMA NFIP guidance via Vertex AI Search (Discovery Engine).

WHAT THIS IS
    When a claimant asks a *general* question ("does flood insurance cover my
    basement?", "when is the proof of loss due?"), Maya calls the
    ``lookup_flood_guidance`` tool, which calls :func:`search_flood_guidance`.
    That searches a **Vertex AI Search** engine that has indexed FEMA's own
    published NFIP documents (the Standard Flood Insurance Policy form, the
    NFIP Claims Handbook and manuals). The result is a handful of short
    passages with titles and page numbers. Maya answers *from those passages*
    instead of from memory. This is called **grounding**.

    Set-up (data store, import, engine) is done once by
    ``deploy/03b_vertex_ai_search.sh``; see ``docs/grounding.md``.

WHY THE REST API AND NOT A NEW CLIENT LIBRARY?
    ``google-cloud-discoveryengine`` is a large dependency. The REST call is
    one POST, and ``google-auth`` (already installed) gives us an
    ``AuthorizedSession`` that attaches the Cloud Run service account's
    OAuth token automatically (Application Default Credentials). Fewer
    dependencies mean a smaller image and fewer upgrades.

REGION (the one exception)
    Vertex AI Search data stores exist only in ``global``, ``us`` or ``eu``.
    We use the ``us`` multi-region, served by the regional endpoint
    ``https://us-discoveryengine.googleapis.com``. Everything else in the
    project stays in ``us-central1``.

FAILURE BEHAVIOUR
    Grounding is a nice-to-have in a live call. A slow or failing search must
    never stall the conversation, so the call has a short timeout and every
    failure becomes a ``DataAccessError``. The tool handler turns that into
    ``found: false`` and Maya says the adjuster will explain.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..errors import ConfigurationError, DataAccessError
from ..observability import get_logger
from ..settings import Settings, get_settings

log = get_logger(__name__)

_TIMEOUT_SECONDS = 8.0  # a live caller should not wait longer than this
_MAX_PASSAGES = 3  # enough to answer, small enough for the Live model's context
_MAX_PASSAGE_CHARS = 700
_CACHE_TTL_SECONDS = 600.0
_CACHE_MAX_ENTRIES = 128

# Answer cache shared by all worker threads (search runs in asyncio.to_thread).
# The lock keeps "evict oldest + insert" atomic; it is never held during the
# HTTP call. (HTTP sessions are per thread, see _authorized_session.)
_cache: dict[str, tuple[float, "GuidanceResult"]] = {}
_cache_lock = threading.Lock()


@dataclass
class GuidancePassage:
    """One cited passage from a FEMA document."""

    title: str
    text: str
    page: str = ""
    source_uri: str = ""


@dataclass
class GuidanceResult:
    """What the voice tool returns to Gemini Live (converted with :meth:`as_tool_result`)."""

    found: bool
    question: str
    passages: list[GuidancePassage] = field(default_factory=list)
    message: str = ""

    def as_tool_result(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "question": self.question,
            "passages": [asdict(p) for p in self.passages],
            "message": self.message,
            "how_to_use": (
                "Answer in one or two plain sentences based only on these passages. Say it is FEMA's general "
                "NFIP guidance and that the adjuster applies the claimant's actual policy. Never promise "
                "coverage or payment for this claim."
            ),
        }


def endpoint_host(location: str) -> str:
    """``us`` -> ``us-discoveryengine.googleapis.com``; ``global`` -> ``discoveryengine.googleapis.com``."""

    loc = (location or "global").strip().lower()
    return "discoveryengine.googleapis.com" if loc == "global" else f"{loc}-discoveryengine.googleapis.com"


def search_url(settings: Settings) -> str:
    """The ``:search`` URL of the engine's default serving config."""

    if not settings.project_id or not settings.search_engine_id:
        raise ConfigurationError("GOOGLE_CLOUD_PROJECT and CLAIMDESK_SEARCH_ENGINE_ID must be set for guidance search")
    return (
        f"https://{endpoint_host(settings.search_location)}/v1/projects/{settings.project_id}"
        f"/locations/{settings.search_location}/collections/default_collection/engines/"
        f"{settings.search_engine_id}/servingConfigs/default_search:search"
    )


def build_request_body(question: str) -> dict[str, Any]:
    """Request body: ask for extractive segments (Enterprise tier) and snippets (any tier)."""

    return {
        "query": question,
        "pageSize": 5,
        "contentSearchSpec": {
            "snippetSpec": {"returnSnippet": True},
            "extractiveContentSpec": {"maxExtractiveSegmentCount": 1, "maxExtractiveAnswerCount": 1},
        },
    }


def _strip_markup(text: str) -> str:
    # Snippets contain <b>highlight</b> tags and HTML entities.
    for token, replacement in (("<b>", ""), ("</b>", ""), ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&"), ("&nbsp;", " ")):
        text = text.replace(token, replacement)
    return " ".join(text.split())


def parse_search_response(question: str, payload: dict[str, Any]) -> GuidanceResult:
    """Turn the raw JSON response into a small :class:`GuidanceResult`.

    Priority per document: extractive answer (most precise), then extractive
    segment, then snippet. Only the first passage per document is kept so a
    single long PDF cannot crowd out the others.
    """

    passages: list[GuidancePassage] = []
    for item in payload.get("results", []) or []:
        doc = item.get("document", {}) or {}
        data = doc.get("derivedStructData", {}) or {}
        title = str(data.get("title") or doc.get("id") or "FEMA NFIP document")
        link = str(data.get("link") or "")
        text, page = "", ""
        for key in ("extractive_answers", "extractive_segments"):
            entries = data.get(key) or []
            if entries:
                text = str(entries[0].get("content", ""))
                page = str(entries[0].get("pageNumber", "") or "")
                break
        if not text:
            snippets = data.get("snippets") or []
            if snippets and snippets[0].get("snippet_status", "SUCCESS") == "SUCCESS":
                text = str(snippets[0].get("snippet", ""))
        text = _strip_markup(text)
        if not text:
            continue
        passages.append(GuidancePassage(title=title, text=text[:_MAX_PASSAGE_CHARS], page=page, source_uri=link))
        if len(passages) >= _MAX_PASSAGES:
            break

    if not passages:
        return GuidanceResult(found=False, question=question, message="No matching FEMA guidance was found.")
    return GuidanceResult(found=True, question=question, passages=passages)


_thread_local = threading.local()


def _authorized_session() -> Any:
    """Thread-local ``AuthorizedSession`` (``requests.Session`` is not thread-safe across workers)."""

    sess = getattr(_thread_local, "session", None)
    if sess is None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        sess = AuthorizedSession(credentials)
        _thread_local.session = sess
    return sess


def _cache_get(key: str) -> GuidanceResult | None:
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    return None


def _cache_put(key: str, value: GuidanceResult) -> None:
    with _cache_lock:
        if key not in _cache and len(_cache) >= _CACHE_MAX_ENTRIES:
            _cache.pop(next(iter(_cache)))  # drop the oldest entry
        _cache[key] = (time.monotonic(), value)


def search_flood_guidance(question: str, *, settings: Settings | None = None, session: Any = None) -> GuidanceResult:
    """Search FEMA NFIP documents. **Blocking**: call it with ``asyncio.to_thread``.

    Args:
        question: a short search query, e.g. "basement contents coverage".
        settings: override for tests; defaults to :func:`get_settings`.
        session: override for tests (anything with ``.post(url, json=, timeout=, headers=)``).

    Raises:
        ConfigurationError: grounding is not enabled or not configured.
        DataAccessError: the search call failed (network, permission, quota).
    """

    settings = settings or get_settings()
    question = " ".join((question or "").split())[:300]
    if not question:
        return GuidanceResult(found=False, question="", message="No question was given.")
    if not settings.enable_guidance_search:
        raise ConfigurationError("Guidance search is disabled (CLAIMDESK_ENABLE_GUIDANCE_SEARCH)")

    key = question.lower()
    cached = _cache_get(key)
    if cached is not None:
        return cached

    url = search_url(settings)
    started = time.monotonic()
    try:
        http = session or _authorized_session()
        headers = {"X-Goog-User-Project": settings.project_id} if settings.project_id else {}
        response = http.post(url, json=build_request_body(question), headers=headers, timeout=_TIMEOUT_SECONDS)
        if response.status_code >= 400:
            # Keep only the start of the body: enough to diagnose, no flooding.
            raise DataAccessError(
                f"Vertex AI Search returned HTTP {response.status_code}: {response.text[:300]}",
                retryable=response.status_code in (429, 500, 502, 503, 504),
            )
        result = parse_search_response(question, response.json())
    except DataAccessError:
        raise
    except Exception as exc:  # network errors, timeouts, bad JSON, credential problems
        raise DataAccessError(f"Vertex AI Search call failed: {exc!r}", retryable=True) from exc

    log.info(
        "Guidance search completed",
        extra={
            "json_fields": {
                "tool": "lookup_flood_guidance",
                "found": result.found,
                "passages": len(result.passages),
                "latency_ms": round((time.monotonic() - started) * 1000),
                "search_location": settings.search_location,
            }
        },
    )
    _cache_put(key, result)
    return result


def clear_cache() -> None:
    """Forget cached answers (used by tests)."""

    with _cache_lock:
        _cache.clear()


__all__ = [
    "GuidancePassage",
    "GuidanceResult",
    "build_request_body",
    "clear_cache",
    "endpoint_host",
    "parse_search_response",
    "search_flood_guidance",
    "search_url",
]
