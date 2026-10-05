"""Package the adjuster hand-off (ZIP) and archive it: Cloud Storage + BigQuery.

WHEN DOES THIS RUN?
    When the claimant (or operator) downloads the packet
    (``GET /api/intakes/{id}/packet.zip``). Download = "this packet is final
    enough to hand over", so that is the moment we archive it.

WHAT GETS ARCHIVED
    1. The ZIP itself -> ``gs://<bucket>/intakes/<id>/packet/packet.zip``::

           packet.md     human-readable packet (Markdown)
           packet.json   structured packet + claim facts + evidence manifest
           evidence/<evidence_id>.jpg  every captured / uploaded photo
           sketch.png    the generated illustration (if any)

    2. One summary row -> BigQuery table ``{project}.{dataset}.intake_packets``
       so you can build dashboards and evals with SQL, e.g. "how many packets
       were routed to policy review last week?". The table is created by the
       data/deploy scripts, never by this app.

WHY STREAMING INSERTS (``insert_rows_json``)?
    It is the simplest way to append a row from an app: one HTTPS call, row
    queryable within seconds, no load jobs to manage. We pass a ``row_id``
    (``<intake_id>-r<revision>``) so BigQuery de-duplicates retries on a
    best-effort basis.

FAILURE POLICY
    Archiving is a side effect of a download. If GCS or BigQuery fails we log
    the error (Error Reporting picks it up) and STILL return the ZIP to the
    user: a storage hiccup must never cost the claimant their packet.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any

from claimdesk.errors import StorageError
from claimdesk.observability import get_logger
from claimdesk.rules._helpers import is_blank, parse_date
from claimdesk.settings import get_settings

from .desk_view import current_result, evidence_manifest, packet_markdown
from .evidence_store import EvidenceStore, packet_object
from .intake_store import IntakeRecord

log = get_logger(__name__)

PACKETS_TABLE = "intake_packets"


async def build_packet_zip(record: IntakeRecord, evidence: EvidenceStore) -> bytes:
    """Assemble the ZIP in memory. Raises StorageError if evidence bytes are unreadable."""

    result = current_result(record)
    manifest = evidence_manifest(record)
    payload = {
        "intake_id": record.intake_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "revision": record.revision,
        "packet": {k: v for k, v in result["packet"].items() if k != "markdown"},
        "claim_facts": result["claim_facts"],
        "policy_record": record.policy_record,
        "evidence": manifest,
        "sketch": {k: v for k, v in (record.sketch or {}).items() if k not in {"object_path", "storage_uri"}} or None,
        "disclaimer": "Demo intake packet. It does not confirm coverage, payment or liability and was not sent to an adjuster.",
    }
    files: list[tuple[str, bytes]] = []
    for photo in record.evidence_photos:
        data = await evidence.get(photo["object_path"])
        if data is None:
            log.warning("Evidence object missing from store", extra={"json_fields": {"evidence_id": photo["id"]}})
            continue
        files.append((f"evidence/{photo['id']}.jpg", data))
    if record.sketch:
        data = await evidence.get(record.sketch["object_path"])
        if data is not None:
            files.append(("sketch.png", data))

    # Text is rendered here, on the event loop, so it is a consistent snapshot
    # of the record (the live call keeps changing it). Only the CPU-heavy
    # compression moves to a worker thread - see ``_zip_bytes``.
    texts = [("packet.md", packet_markdown(record, result)), ("packet.json", json.dumps(payload, indent=2, default=str))]
    return await asyncio.to_thread(_zip_bytes, texts, files)


def _zip_bytes(texts: list[tuple[str, str]], images: list[tuple[str, bytes]]) -> bytes:
    """Write the ZIP (runs in a worker thread).

    WHY A THREAD? Compressing several MB of photos takes long enough to stall
    the event loop, and every open live call on this instance would hear the
    audio stutter. ``asyncio.to_thread`` keeps the loop free.

    WHY TWO COMPRESSION MODES? JPEG and PNG are already compressed, so
    DEFLATE only burns CPU for ~0% gain: store them as-is (``ZIP_STORED``).
    Markdown and JSON shrink a lot, so they are deflated.
    """

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in texts:
            archive.writestr(name, text, compress_type=zipfile.ZIP_DEFLATED)
        for name, data in images:
            archive.writestr(name, data, compress_type=zipfile.ZIP_STORED)
    return buffer.getvalue()


def packet_row(record: IntakeRecord, packet_gcs_uri: str | None) -> dict[str, Any]:
    """One ``intake_packets`` row (pure function -> easy to unit test)."""

    result = current_result(record)
    facts = result["claim_facts"]
    packet = result["packet"]
    loss_date = parse_date(facts.get("date_of_loss"))
    estimate = facts.get("estimated_loss_usd")

    def text(key: str) -> str | None:
        value = facts.get(key)
        return None if is_blank(value) else str(value)

    return {
        "intake_id": record.intake_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "claim_type": packet["claim_type"],
        "routing_decision": packet["routing_decision"],
        "severity": packet["severity"],
        "intake_status": packet["intake_status"],
        "policy_number": text("policy_number"),
        "loss_state": text("loss_state"),
        "loss_zip_code": text("loss_zip_code"),
        "date_of_loss": loss_date.isoformat() if loss_date else None,  # BigQuery DATE as "YYYY-MM-DD"
        "estimated_loss_usd": float(estimate) if isinstance(estimate, (int, float)) else None,
        "missing_count": len(result["field_check"].get("missing_fields", [])),
        "packet_gcs_uri": packet_gcs_uri,
        "packet_json": json.dumps({k: v for k, v in packet.items() if k != "markdown"}, default=str),
    }


async def insert_packet_row(row: dict[str, Any], *, row_id: str) -> None:
    """Stream one row into BigQuery. Raises StorageError on failure."""

    from claimdesk.data_access.bq_client import get_bq_client

    settings = get_settings()
    table_id = f"{settings.project_id}.{settings.bq_dataset}.{PACKETS_TABLE}"
    try:
        client = get_bq_client()
        errors = await asyncio.to_thread(client.insert_rows_json, table_id, [row], row_ids=[row_id])
    except Exception as exc:  # google-api-core raises many types; wrap once with the cause
        raise StorageError(f"BigQuery insert into {table_id} failed: {exc}") from exc
    if errors:
        raise StorageError(f"BigQuery rejected packet row: {str(errors)[:500]}")


async def archive_packet(record: IntakeRecord, evidence: EvidenceStore) -> tuple[bytes, str | None]:
    """Build the ZIP, save it, record it in BigQuery. Returns (zip_bytes, gcs_uri).

    Only building the ZIP may raise (StorageError, if photos can't be read).
    Archive failures are logged and swallowed - see "FAILURE POLICY" above.
    """

    data = await build_packet_zip(record, evidence)
    uri: str | None = None
    try:
        uri = await evidence.put(packet_object(record.intake_id), data, "application/zip")
        record.packet_gcs_uri = uri
    except StorageError:
        log.exception("Could not save packet ZIP to Cloud Storage")
    if get_settings().storage_backend == "gcp":
        try:
            await insert_packet_row(packet_row(record, uri), row_id=f"{record.intake_id}-r{record.revision}")
        except StorageError:
            log.exception("Could not record packet in BigQuery")
    else:
        log.info("Memory backend: skipping BigQuery packet row", extra={"json_fields": {"packet_uri": uri}})
    return data, uri


__all__ = ["PACKETS_TABLE", "build_packet_zip", "packet_row", "insert_packet_row", "archive_packet"]
