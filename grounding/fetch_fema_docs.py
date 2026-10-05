"""Collect FEMA NFIP PDFs and upload them to Cloud Storage for Vertex AI Search.

WHAT IT DOES (step by step)
    1. Reads ``grounding/fema_documents.json`` (the list of documents).
    2. For each document that has a direct ``url`` and is not yet in
       ``grounding/raw/``, tries to download it.
    3. Prints which documents are still missing, with the FEMA page where
       you can download them by hand.
    4. With ``--upload``, copies every PDF in ``grounding/raw/`` to
       ``gs://$CLAIMDESK_GCS_BUCKET/grounding/fema/``. That folder is what
       ``deploy/03b_vertex_ai_search.sh`` imports into Vertex AI Search.

WHY A MANUAL FALLBACK?
    fema.gov sits behind a bot-protection CDN that sometimes answers scripted
    downloads with ``403 Forbidden``, even from Cloud Shell. When that
    happens: open the landing page in your browser, download the PDF, then
    upload it to Cloud Shell (three-dot menu > Upload) into ``grounding/raw/``
    with the file name shown. Re-run this script with ``--upload``.

USAGE (Cloud Shell, from the project folder)
    uv run python -m grounding.fetch_fema_docs            # download what we can
    uv run python -m grounding.fetch_fema_docs --upload   # ... and upload to GCS

The PDFs are US Government works (public domain). This product uses FEMA
documents but is not endorsed by FEMA.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from claimdesk.observability import get_logger, setup_logging
from claimdesk.settings import load_dotenv_if_present

log = get_logger("grounding.fetch_fema_docs")

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "fema_documents.json"
RAW_DIR = HERE / "raw"
GCS_PREFIX = "grounding/fema/"
# A normal browser user agent: FEMA's CDN rejects obviously scripted clients.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Accept": "application/pdf,*/*;q=0.8",
}


def load_manifest(path: Path = MANIFEST) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["documents"]


def is_pdf(path: Path) -> bool:
    """True if the file starts with the PDF magic bytes (a 403 page is HTML)."""

    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def download(doc: dict, raw_dir: Path = RAW_DIR) -> bool:
    """Try to download one document. Returns True if a valid PDF is now on disk."""

    import requests

    target = raw_dir / doc["file"]
    if is_pdf(target):
        log.info("Already present", extra={"json_fields": {"file": doc["file"]}})
        return True
    if not doc.get("url"):
        return False
    try:
        response = requests.get(doc["url"], headers=_HEADERS, timeout=60)
    except requests.RequestException as exc:
        log.warning("Download failed", extra={"json_fields": {"file": doc["file"], "error": repr(exc)}})
        return False
    if response.status_code != 200 or not response.content.startswith(b"%PDF-"):
        log.warning(
            "FEMA did not return a PDF (often 403 bot protection); download it by hand",
            extra={"json_fields": {"file": doc["file"], "status": response.status_code}},
        )
        return False
    raw_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)
    log.info("Downloaded", extra={"json_fields": {"file": doc["file"], "bytes": len(response.content)}})
    return True


def upload(bucket_name: str, raw_dir: Path = RAW_DIR) -> list[str]:
    """Upload every valid PDF in ``raw_dir`` to ``gs://bucket/grounding/fema/``."""

    from google.cloud import storage

    bucket = storage.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None).bucket(bucket_name)
    uploaded = []
    for pdf in sorted(raw_dir.glob("*.pdf")):
        if not is_pdf(pdf):
            print(f"  skipping {pdf.name}: not a real PDF (maybe an error page)")
            continue
        blob = bucket.blob(GCS_PREFIX + pdf.name)
        blob.upload_from_filename(str(pdf), content_type="application/pdf")
        uploaded.append(f"gs://{bucket_name}/{blob.name}")
    return uploaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--upload", action="store_true", help="upload grounding/raw/*.pdf to the evidence bucket")
    args = parser.parse_args(argv)

    load_dotenv_if_present()
    setup_logging()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    missing_required = []
    missing_optional = []
    print("FEMA NFIP documents:")
    for doc in load_manifest():
        ok = download(doc)
        mark = "OK     " if ok else "MISSING"
        print(f"  [{mark}] {doc['file']:<34} {doc['title']}")
        if not ok:
            print(f"            download by hand from: {doc['landing_page']}")
            print(f"            save it as: grounding/raw/{doc['file']}")
            (missing_required if doc.get("required") else missing_optional).append(doc["file"])

    if args.upload:
        bucket = os.getenv("CLAIMDESK_GCS_BUCKET", "")
        if not bucket:
            print("CLAIMDESK_GCS_BUCKET is not set (check .env)", file=sys.stderr)
            return 2
        uploaded = upload(bucket)
        print(f"Uploaded {len(uploaded)} file(s):")
        for uri in uploaded:
            print(f"  {uri}")
        if not uploaded:
            return 1

    if missing_optional:
        # Not an error: grounding works with the SFIP Dwelling Form alone. The
        # extra documents improve answers to "what should I keep / photograph?"
        # and "what happens next?" questions. See docs/grounding.md.
        print(
            f"\nOptional documents not present: {', '.join(missing_optional)}.\n"
            "Grounding works without them; add them later for better coverage (download by hand, then re-run)."
        )
    if missing_required:
        print(f"\nStill missing required documents: {', '.join(missing_required)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
