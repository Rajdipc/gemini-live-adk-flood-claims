"""Where the *serializable* state of an intake lives (Firestore or memory).

WHY PERSIST AT ALL?
    Cloud Run can stop an instance at any time (scale-in, new revision
    deployed, maintenance). If the claimant's transcript and latest packet only
    lived in a Python dict they would vanish. So every intake is also written
    to **Firestore**, a serverless document database: one *document* per
    intake, keyed by ``intake_id``, inside one *collection*.

WHAT IS STORED vs WHAT STAYS IN MEMORY
    Stored (``IntakeRecord``): owner, transcript, revision counters, latest
    pipeline result, policy record, evidence/sketch *metadata*, tool activity,
    timestamps. Everything is plain JSON-friendly data.

    NOT stored: the open WebSocket, the Gemini Live session, the latest camera
    frame, asyncio locks and tasks. Those are live objects that only make sense
    inside one running process; ``webapp/intake_session.py`` keeps them.
    Photo *bytes* go to Cloud Storage (``webapp/evidence_store.py``), because
    Firestore documents are limited to 1 MiB.

AUTOMATIC CLEAN-UP (TTL)
    Each document carries ``expires_at`` (a Firestore timestamp). Create a
    Firestore **TTL policy** on that field once, and Firestore deletes expired
    intakes for you (usually within 24 h of expiry), e.g.::

        gcloud firestore fields ttls update expires_at \
            --collection-group=intakes --enable-ttl

    (Run by the deploy scripts, not by this app. This module never creates
    GCP resources.)

CHOOSING A BACKEND
    ``settings.storage_backend``: ``"gcp"`` -> Firestore, ``"memory"`` -> a
    dict (unit tests and offline development; data is lost on restart).
"""

from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Protocol

from claimdesk.errors import StorageError
from claimdesk.observability import get_logger
from claimdesk.settings import Settings, get_settings

from .evidence_store import storage_failure_types

log = get_logger(__name__)


def idle_ttl_seconds() -> float:
    """How long an untouched intake lives (CLAIMDESK_IDLE_EXPIRY_MINUTES, default 30).

    Read on every call rather than stored in a module constant so a changed
    setting (or a test override) is honoured without re-importing.
    """

    return get_settings().idle_expiry_minutes * 60


@dataclass
class IntakeRecord:
    """Everything about one intake that must survive a restart.

    Times are Unix epoch seconds (floats) in Python; they are converted to
    real Firestore timestamps on write so the TTL policy can use them.
    """

    intake_id: str
    owner: str
    transcript: list[dict[str, Any]] = field(default_factory=list)
    # ``revision`` increases when claimant facts change (a claimant turn or a
    # captured photo). The pipeline result is cached per revision so the agent
    # talking does not re-trigger expensive fact extraction.
    revision: int = 0
    pipeline_result: dict[str, Any] | None = None
    pipeline_revision: int | None = None
    route: str = "needs_docs"
    previous_route: str | None = None
    policy_record: dict[str, Any] | None = None
    evidence_photos: list[dict[str, Any]] = field(default_factory=list)
    camera_notes: list[str] = field(default_factory=list)
    sketch: dict[str, Any] | None = None
    sketch_revision: int = 0
    tool_activity: list[dict[str, Any]] = field(default_factory=list)
    packet_gcs_uri: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + idle_ttl_seconds())

    def touch(self) -> None:
        """Mark activity: pushes the idle expiry into the future."""

        self.updated_at = time.time()
        self.expires_at = self.updated_at + idle_ttl_seconds()

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    # --- (de)serialization ---------------------------------------------------
    def to_document(self) -> dict[str, Any]:
        """Firestore-friendly dict: epoch floats become timezone-aware datetimes."""

        data = asdict(self)
        for key in ("created_at", "updated_at", "expires_at"):
            data[key] = datetime.fromtimestamp(data[key], tz=timezone.utc)
        return data

    @classmethod
    def from_document(cls, data: dict[str, Any]) -> "IntakeRecord":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        for key in ("created_at", "updated_at", "expires_at"):
            value = clean.get(key)
            if isinstance(value, datetime):
                clean[key] = value.timestamp()
        return cls(**clean)


class IntakeStore(Protocol):
    """The four operations the web app needs. Both backends implement them."""

    async def create(self, record: IntakeRecord) -> None: ...

    async def load(self, intake_id: str) -> IntakeRecord | None: ...

    async def save(self, record: IntakeRecord) -> None: ...

    async def delete(self, intake_id: str) -> None: ...


class MemoryIntakeStore:
    """Dict-backed store for tests and offline development.

    Records are deep-copied in and out so callers cannot accidentally share
    mutable state with the "database" - the same behaviour as a real DB.
    """

    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: IntakeRecord) -> None:
        await self.save(record)

    async def load(self, intake_id: str) -> IntakeRecord | None:
        async with self._lock:
            doc = self._docs.get(intake_id)
            if doc is None:
                return None
            record = IntakeRecord.from_document(copy.deepcopy(doc))
            if record.expired:
                # Mimic Firestore TTL: expired documents disappear.
                self._docs.pop(intake_id, None)
                return None
            return record

    async def save(self, record: IntakeRecord) -> None:
        async with self._lock:
            self._docs[record.intake_id] = copy.deepcopy(record.to_document())

    async def delete(self, intake_id: str) -> None:
        async with self._lock:
            self._docs.pop(intake_id, None)


class FirestoreIntakeStore:
    """Firestore (Native mode) implementation using the *async* client.

    ``google.cloud.firestore.AsyncClient`` speaks gRPC without blocking the
    event loop, so audio keeps streaming while we save. Authentication uses
    Application Default Credentials (your ``gcloud auth application-default
    login`` locally, the Cloud Run service account in production). The
    service account needs ``roles/datastore.user``.
    """

    def __init__(self, settings: Settings) -> None:
        # Imported lazily so the memory backend (tests) never loads gRPC.
        from google.cloud import firestore

        self._client = firestore.AsyncClient(project=settings.project_id or None, database=settings.firestore_database)
        self._collection = self._client.collection(settings.firestore_collection)

    async def create(self, record: IntakeRecord) -> None:
        await self.save(record)

    async def load(self, intake_id: str) -> IntakeRecord | None:
        try:
            snapshot = await self._collection.document(intake_id).get()
        except storage_failure_types() as exc:  # API errors + auth/transport failures, see evidence_store
            raise StorageError(f"Firestore read failed for intake {intake_id}: {exc}") from exc
        if not snapshot.exists:
            return None
        record = IntakeRecord.from_document(snapshot.to_dict() or {})
        # TTL deletion is asynchronous (can lag by hours), so check ourselves.
        return None if record.expired else record

    async def save(self, record: IntakeRecord) -> None:
        try:
            # ``set`` replaces the whole document: simple and idempotent.
            await self._collection.document(record.intake_id).set(record.to_document())
        except storage_failure_types() as exc:
            raise StorageError(f"Firestore write failed for intake {record.intake_id}: {exc}") from exc

    async def delete(self, intake_id: str) -> None:
        try:
            await self._collection.document(intake_id).delete()
        except storage_failure_types() as exc:
            raise StorageError(f"Firestore delete failed for intake {intake_id}: {exc}") from exc


def build_intake_store(settings: Settings) -> IntakeStore:
    """Pick the backend from ``CLAIMDESK_STORAGE_BACKEND``."""

    if settings.storage_backend == "memory":
        log.info("Using in-memory intake store (data is lost on restart)")
        return MemoryIntakeStore()
    log.info(
        "Using Firestore intake store",
        extra={"json_fields": {"database": settings.firestore_database, "collection": settings.firestore_collection}},
    )
    return FirestoreIntakeStore(settings)


__all__ = [
    "idle_ttl_seconds",
    "IntakeRecord",
    "IntakeStore",
    "MemoryIntakeStore",
    "FirestoreIntakeStore",
    "build_intake_store",
]
