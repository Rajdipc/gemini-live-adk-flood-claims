"""Where evidence *bytes* live: camera frames, uploaded photos, sketches, packet ZIPs.

WHY CLOUD STORAGE?
    Images are binary and can be large; Firestore documents are limited to
    1 MiB. **Cloud Storage (GCS)** is Google's object store: you put bytes at a
    path ("object name") inside a *bucket* and read them back later.

OBJECT LAYOUT (one "folder" per intake)::

    gs://<bucket>/intakes/<intake_id>/evidence/<evidence_id>.jpg
    gs://<bucket>/intakes/<intake_id>/sketch/sketch-v<N>.png
    gs://<bucket>/intakes/<intake_id>/packet/packet.zip

NO SIGNED URLS - ON PURPOSE
    A common pattern is to hand the browser a *signed URL* that grants
    temporary direct access to an object. We deliberately do NOT do that: the
    bucket stays completely private and every image is streamed *through the
    app* (``GET /api/intakes/{id}/evidence/{evidence_id}``), where the IAP
    identity and intake ownership are checked on every request. Simpler IAM
    (no ``iam.serviceAccountTokenCreator`` role needed) and nothing leaks if a
    URL is shared.

BLOCKING CLIENT -> ``asyncio.to_thread``
    ``google-cloud-storage`` is a synchronous (blocking) library. Calling it
    directly inside an ``async def`` would freeze the event loop - and with it
    the claimant's live audio. ``asyncio.to_thread`` runs each call on a worker
    thread instead.

CLEAN-UP
    Configure a bucket **lifecycle rule** (e.g. delete objects older than 30
    days) in the deploy scripts; this app never creates or changes buckets.
"""

from __future__ import annotations

import asyncio
from functools import cached_property
from typing import Protocol

from claimdesk.errors import ConfigurationError, StorageError
from claimdesk.observability import get_logger
from claimdesk.settings import Settings

log = get_logger(__name__)


def evidence_object(intake_id: str, evidence_id: str) -> str:
    return f"intakes/{intake_id}/evidence/{evidence_id}.jpg"


def sketch_object(intake_id: str, version: int) -> str:
    return f"intakes/{intake_id}/sketch/sketch-v{version}.png"


def packet_object(intake_id: str) -> str:
    return f"intakes/{intake_id}/packet/packet.zip"


def storage_failure_types() -> tuple[type[BaseException], ...]:
    """Exception types that mean "the storage service could not be reached".

    WHY MORE THAN ``GoogleAPICallError``? Real outages do not always look like
    an API error response:

    * ``google.auth`` raises ``RefreshError`` / ``TransportError`` /
      ``DefaultCredentialsError`` when credentials cannot be loaded or
      refreshed (e.g. the metadata server hiccups on Cloud Run);
    * ``requests`` (used by ``google-cloud-storage``) raises
      ``ConnectionError`` / ``Timeout`` for network problems;
    * ``google.api_core`` raises ``RetryError`` when its retry budget runs out
      (it is *not* a ``GoogleAPICallError``);
    * sockets raise ``OSError`` / ``TimeoutError``.

    All of them are wrapped into ``StorageError`` so the web layer answers
    with a friendly 503 / "couldn't save" message instead of crashing the
    request or the live call. Imported lazily so the memory backend (tests)
    never loads these libraries.
    """

    from google.api_core import exceptions as gexc
    from google.auth import exceptions as auth_exceptions

    types_: list[type[BaseException]] = [gexc.GoogleAPICallError, gexc.RetryError, auth_exceptions.GoogleAuthError, OSError, TimeoutError]
    try:
        import requests

        types_.append(requests.exceptions.RequestException)
    except ImportError:  # pragma: no cover - requests ships with google-cloud-storage
        pass
    return tuple(types_)


class EvidenceStore(Protocol):
    async def put(self, object_name: str, data: bytes, content_type: str) -> str:
        """Store bytes; return a URI (``gs://...`` or ``memory://...``) for records."""

    async def get(self, object_name: str) -> bytes | None:
        """Return the bytes, or ``None`` if the object does not exist."""

    async def delete_prefix(self, prefix: str) -> None:
        """Delete every object whose name starts with ``prefix``."""


class MemoryEvidenceStore:
    """Dict-backed store for tests / offline development."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, object_name: str, data: bytes, content_type: str) -> str:
        self.objects[object_name] = (bytes(data), content_type)
        return f"memory://{object_name}"

    async def get(self, object_name: str) -> bytes | None:
        item = self.objects.get(object_name)
        return item[0] if item else None

    async def delete_prefix(self, prefix: str) -> None:
        for name in [n for n in self.objects if n.startswith(prefix)]:
            self.objects.pop(name, None)


class GcsEvidenceStore:
    """Private GCS bucket. The Cloud Run service account needs
    ``roles/storage.objectUser`` on the bucket (read + write + delete)."""

    def __init__(self, settings: Settings) -> None:
        if not settings.gcs_bucket:
            raise ConfigurationError("CLAIMDESK_GCS_BUCKET is not set")
        self._settings = settings

    @cached_property
    def _bucket(self):
        # Lazy import + lazy client: creating it sets up auth and HTTP pools,
        # so we do it once, on first use.
        from google.cloud import storage

        client = storage.Client(project=self._settings.project_id or None)
        return client.bucket(self._settings.gcs_bucket)

    async def put(self, object_name: str, data: bytes, content_type: str) -> str:
        def _upload() -> None:
            self._bucket.blob(object_name).upload_from_string(data, content_type=content_type)

        try:
            await asyncio.to_thread(_upload)
        except storage_failure_types() as exc:
            raise StorageError(f"GCS upload failed for {object_name}: {exc}") from exc
        return f"gs://{self._settings.gcs_bucket}/{object_name}"

    async def get(self, object_name: str) -> bytes | None:
        from google.api_core import exceptions as gexc

        def _download() -> bytes:
            # The (lazy) client is created inside the worker thread too, so a
            # slow credential lookup never blocks the event loop.
            return self._bucket.blob(object_name).download_as_bytes()

        try:
            return await asyncio.to_thread(_download)
        except gexc.NotFound:
            return None
        except storage_failure_types() as exc:
            raise StorageError(f"GCS download failed for {object_name}: {exc}") from exc

    async def delete_prefix(self, prefix: str) -> None:
        def _delete() -> None:
            for blob in self._bucket.client.list_blobs(self._bucket, prefix=prefix):
                blob.delete()

        try:
            await asyncio.to_thread(_delete)
        except storage_failure_types() as exc:
            raise StorageError(f"GCS delete failed for prefix {prefix}: {exc}") from exc


def build_evidence_store(settings: Settings) -> EvidenceStore:
    if settings.storage_backend == "memory":
        return MemoryEvidenceStore()
    log.info("Using Cloud Storage evidence store", extra={"json_fields": {"bucket": settings.gcs_bucket}})
    return GcsEvidenceStore(settings)


__all__ = [
    "EvidenceStore",
    "MemoryEvidenceStore",
    "GcsEvidenceStore",
    "build_evidence_store",
    "evidence_object",
    "sketch_object",
    "packet_object",
    "storage_failure_types",
]
