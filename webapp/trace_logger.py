"""Record every conversation event in BigQuery (``conversation_traces``) for evals.

WHY?
    To improve an agent you need to *see* what it did in real conversations:
    what the claimant said, what the agent replied, which tools it called with
    which arguments, and what the pipeline decided. Later, ``evals/`` can turn
    these rows into evaluation datasets (e.g. "did the agent ever promise
    coverage?", "how often did find_policy fail?").

TABLE ``{project}.{dataset}.conversation_traces`` (created by the data/deploy
scripts, NOT by this app)::

    intake_id STRING, event_time TIMESTAMP, seq INT64, event_type STRING,
    role STRING, text STRING, tool_name STRING, tool_args_json STRING,
    tool_result_json STRING, service_revision STRING

    event_type is one of: claimant_turn, agent_turn, camera_observation,
    tool_call, tool_result, pipeline_result, system

HOW IT STAYS FAST AND SAFE
    * ``record()`` only appends to an in-memory list - it never waits for the
      network, so the live call is never slowed down.
    * A background task flushes the list every couple of seconds (or when it
      gets large) with ``insert_rows_json`` (BigQuery *streaming inserts*:
      rows are queryable within seconds). The BigQuery client is blocking, so
      the insert runs in a worker thread via ``asyncio.to_thread``.
    * Failures are logged as warnings and the rows are dropped - tracing must
      never break a claimant's call.
    * PII: e-mails and phone numbers are masked with ``observability.redact``
      before anything leaves the process.
    * With the ``memory`` storage backend (tests / offline) nothing is sent to
      BigQuery; events are written to the debug log instead.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from claimdesk.observability import get_logger, redact
from claimdesk.settings import get_settings

log = get_logger(__name__)

EVENT_TYPES = {"claimant_turn", "agent_turn", "camera_observation", "tool_call", "tool_result", "pipeline_result", "system"}

FLUSH_INTERVAL_SECONDS = 2.0
MAX_BATCH_ROWS = 200
TEXT_LIMIT = 8000
JSON_LIMIT = 16000


def _safe_json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return redact(text, limit=JSON_LIMIT)


class ConversationTraceLogger:
    """Batched, async-safe writer. One instance per process (``get_trace_logger``)."""

    def __init__(self, *, enabled: bool, table_id: str) -> None:
        self.enabled = enabled
        self.table_id = table_id
        self._rows: list[dict[str, Any]] = []
        # Per-intake sequence numbers so events can be ordered exactly even
        # when two share the same timestamp.
        self._seq: dict[str, itertools.count] = defaultdict(lambda: itertools.count(1))
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._revision = os.getenv("K_REVISION", "local")

    # --- public API ------------------------------------------------------------
    def record(
        self,
        intake_id: str,
        event_type: str,
        *,
        role: str | None = None,
        text: str | None = None,
        tool_name: str | None = None,
        tool_args: Any = None,
        tool_result: Any = None,
    ) -> None:
        """Queue one event. Never blocks, never raises."""

        if event_type not in EVENT_TYPES:
            log.warning("Unknown trace event type", extra={"json_fields": {"event_type": event_type}})
            return
        row = {
            "intake_id": intake_id,
            "event_time": datetime.now(timezone.utc).isoformat(),
            "seq": next(self._seq[intake_id]),
            "event_type": event_type,
            "role": role,
            "text": redact(text, limit=TEXT_LIMIT) if text is not None else None,
            "tool_name": tool_name,
            "tool_args_json": _safe_json(tool_args),
            "tool_result_json": _safe_json(tool_result),
            "service_revision": self._revision,
        }
        if not self.enabled:
            log.debug("trace event", extra={"json_fields": {k: row[k] for k in ("intake_id", "seq", "event_type", "tool_name")}})
            return
        self._rows.append(row)
        self._ensure_task()
        if len(self._rows) >= MAX_BATCH_ROWS:
            self._wake.set()

    def forget(self, intake_id: str) -> None:
        """Drop the sequence counter of a finished intake (keeps memory bounded)."""

        self._seq.pop(intake_id, None)

    async def flush(self) -> None:
        """Send everything queued so far (called periodically and at shutdown)."""

        if not self._rows:
            return
        rows, self._rows = self._rows, []
        try:
            from claimdesk.data_access.bq_client import get_bq_client

            client = get_bq_client()
            errors = await asyncio.to_thread(client.insert_rows_json, self.table_id, rows)
            if errors:
                log.warning("Some conversation trace rows were rejected", extra={"json_fields": {"errors": str(errors)[:500], "rows": len(rows)}})
        except Exception:  # tracing is best-effort by design (see module doc)
            log.warning("Could not write conversation traces", exc_info=True, extra={"json_fields": {"rows": len(rows)}})

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self.flush()

    # --- internals -------------------------------------------------------------
    def _ensure_task(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop (e.g. a sync unit test); rows flush later
        self._wake = asyncio.Event()
        self._task = loop.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=FLUSH_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            await self.flush()


_instance: ConversationTraceLogger | None = None


def get_trace_logger() -> ConversationTraceLogger:
    """Process-wide logger; disabled automatically for the memory backend."""

    global _instance
    if _instance is None:
        settings = get_settings()
        _instance = ConversationTraceLogger(
            enabled=settings.storage_backend == "gcp",
            table_id=f"{settings.project_id}.{settings.bq_dataset}.conversation_traces",
        )
    return _instance


__all__ = ["ConversationTraceLogger", "EVENT_TYPES", "get_trace_logger"]
