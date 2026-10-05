"""REST API tests for the ClaimDesk web app (no Google Cloud calls).

How these stay offline:
  * ``tests/conftest.py`` forces ``CLAIMDESK_STORAGE_BACKEND=memory``, so
    Firestore/GCS are replaced by dicts and BigQuery traces are disabled.
  * The ADK pipeline, the BigQuery policy lookup and the Gemini photo check
    are monkeypatched with small fakes below.

The IAP identity is simulated with the ``X-Goog-Authenticated-User-Email``
header, exactly as IAP sends it (``accounts.google.com:`` prefix included).
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from claimdesk.contracts import ClaimClassification, ClaimFacts, PolicyRecord
from claimdesk.errors import ModelCallError, StorageError
from claimdesk.rules.evidence_rules import apply_evidence_rules, build_checklist, merge_server_evidence
from claimdesk.rules.packet_writer import write_packet
from claimdesk.rules.required_fields import check_required_fields
from claimdesk.rules.risk_signals import score_risk_signals
from claimdesk.rules.water_source import decide_water_source
from claimdesk.settings import get_settings, local_now
from webapp import intake_session, main, packet_archive, tool_handlers
from webapp.desk_view import build_desk_state
from webapp.intake_store import IntakeRecord

ORIGIN = "http://testserver"
JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" * 10

FACTS = {
    "policyholder_name": "Ana Lopez",
    "policy_number": "FLD-TX-7Q2K9M",
    "contact_method": "ana@example.com",
    "date_of_loss": "2026-09-20",
    "loss_address_or_city": "Houston, TX",
    "loss_state": "TX",
    "loss_zip_code": "77002",
    "loss_description": "The bayou overflowed and 8 inches of water came into the living room.",
    "water_entry_description": "under the front door from the street",
    "estimated_loss_usd": 15000,
    "summary": "Bayou flooding entered the home. Nobody was hurt.",
    "safety_facts": [{"category": "injury", "status": "absent", "description": "Nobody hurt"}],
}
CLASSIFICATION = {"claim_type": "home_flood", "severity": "medium", "severity_rationale": "8 inches inside", "water_source": "surface_flood"}


@pytest.fixture
def settings_env(monkeypatch):
    """Override settings through env vars for one test.

    ``get_settings()`` is cached (``lru_cache``), so after changing env vars
    we clear the cache; on teardown we clear it again so the next test sees
    the original environment (monkeypatch restores the env vars afterwards).
    """

    def apply(**env: object) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


def make_result(facts: dict | None = None, received: list | None = None) -> dict:
    """A realistic pipeline result built with the real deterministic rules."""

    merged = merge_server_evidence(ClaimFacts.model_validate(facts or FACTS).model_dump(), received or [])
    field_check = check_required_fields(merged)
    classification = ClaimClassification.model_validate(CLASSIFICATION).model_dump()
    water = decide_water_source(merged, classification)
    evidence = apply_evidence_rules(merged, field_check, classification, water, policy_issues=[])
    checklist = build_checklist(merged, classification, water)
    risk = score_risk_signals(merged, field_check, classification, water, evidence, benchmark={"available": False})
    packet = write_packet(merged, field_check, classification, water, evidence, checklist, risk)
    return {
        "claim_facts": merged,
        "field_check": field_check,
        "classification": classification,
        "water_source": water,
        "evidence_decision": evidence,
        "checklist": checklist,
        "risk_gate": risk,
        "packet": packet,
        "final_markdown": packet["markdown"],
    }


def iap(email: str = "ana@example.com", **extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-Goog-Authenticated-User-Email": f"accounts.google.com:{email}", **extra}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    calls: dict[str, list] = {"pipeline": [], "policy": []}

    async def fake_pipeline(conversation, *, intake_id=None, received_evidence=None, reference_time=None):
        calls["pipeline"].append(conversation)
        return make_result(received=received_evidence)

    def fake_lookup(number):
        calls["policy"].append(number)
        return PolicyRecord(found=True, policy_number=number, policyholder_name="Ana Lopez", status="active", policy_line="NFIP dwelling", rated_flood_zone="AE")

    async def fake_inspect(image, claimant_said):
        return tool_handlers.FrameFinding(observation="Water line about 8 inches up the drywall", supports_claimant_description=True, document_types=["water_line_photo", "not_a_real_type"])

    monkeypatch.setattr(intake_session, "run_intake_pipeline", fake_pipeline)
    monkeypatch.setattr(intake_session, "lookup_policy", fake_lookup)
    monkeypatch.setattr(tool_handlers, "inspect_photo", fake_inspect)
    monkeypatch.delenv("CLAIMDESK_IAP_AUDIENCE", raising=False)
    return calls


@pytest.fixture()
def client():
    with TestClient(main.app) as test_client:
        yield test_client


def create(client: TestClient, email: str = "ana@example.com") -> str:
    response = client.post("/api/intakes", headers=iap(email))
    assert response.status_code == 200, response.text
    return response.json()["intake_id"]


def upload(client: TestClient, intake_id: str, data: bytes = JPEG, email: str = "ana@example.com", description: str = "water line on the wall"):
    return client.post(
        f"/api/intakes/{intake_id}/photos",
        headers=iap(email),
        files={"photo": ("photo.jpg", data, "image/jpeg")},
        data={"description": description},
    )


# ---------------------------------------------------------------------------
# Health + identity
# ---------------------------------------------------------------------------
def test_health_endpoints_report_configured_models(client):
    for path in ("/api/health", "/healthz"):
        body = client.get(path).json()
        assert body["ok"] is True
        assert body["live_model"] == "gemini-3.8-live"
        assert body["sketch_model"] == "gemini-3.1-flash-image"
        assert body["tools"] == ["find_policy", "refresh_intake_packet", "capture_evidence_photo", "render_damage_sketch"]


def test_create_intake_uses_iap_identity_and_greets(client):
    body = client.post("/api/intakes", headers=iap("Ana@Example.com")).json()
    assert body["user"] == "ana@example.com"  # prefix stripped, lower-cased
    state = body["state"]
    assert state["intake_id"] == body["intake_id"]
    assert [t["speaker"] for t in state["transcript"]] == ["Agent"]
    assert state["progress"] == 0
    assert state["fields"]["evidence"]["status"] == "missing"


def test_other_iap_user_cannot_read_or_fetch_evidence(client):
    intake_id = create(client, "ana@example.com")
    photo = upload(client, intake_id).json()["photo"]
    assert client.get(f"/api/intakes/{intake_id}", headers=iap("ana@example.com")).status_code == 200
    other = client.get(f"/api/intakes/{intake_id}", headers=iap("bob@example.com"))
    assert other.status_code == 404
    assert other.json()["detail"] == "Intake not found or expired. Start a new intake."
    assert client.get(photo["url"], headers=iap("bob@example.com")).status_code == 404
    assert client.get(f"/api/intakes/{intake_id}/packet.zip", headers=iap("bob@example.com")).status_code == 404


def test_local_development_falls_back_to_local_owner(client):
    body = client.post("/api/intakes", headers={"Origin": ORIGIN}).json()
    assert body["user"] == "local-dev"


@pytest.fixture
def fresh_identity_caches(monkeypatch):
    """The IAP claims cache / project number are module globals: reset them per test."""

    monkeypatch.setattr(main, "_iap_token_cache", {})
    monkeypatch.setattr(main, "_project_number_cache", None)
    monkeypatch.setattr(main, "_audience_warning_logged", False)


def signed(email: str = "ana@example.com", jwt: str = "signed.jwt") -> dict[str, str]:
    """Headers IAP adds in production: the e-mail AND the signed JWT."""

    return iap(email, **{"X-Goog-IAP-JWT-Assertion": jwt})


def test_cloud_run_without_iap_jwt_is_rejected(client, monkeypatch, fresh_identity_caches):
    on_cloud_run = dataclasses.replace(main.get_settings(), running_on_cloud_run=True)
    monkeypatch.setattr(main, "get_settings", lambda: on_cloud_run)
    monkeypatch.setattr(main, "_fetch_project_number", lambda: None)
    monkeypatch.setattr(main, "verify_iap_jwt", lambda assertion, audience: {"email": "ana@example.com", "iss": main.IAP_ISSUER})
    assert client.post("/api/intakes", headers={"Origin": ORIGIN}).status_code == 401
    # The plain e-mail header alone is NOT trusted on Cloud Run (fail closed).
    assert client.post("/api/intakes", headers=iap()).status_code == 401
    assert client.post("/api/intakes", headers=signed()).status_code == 200


def test_iap_jwt_is_verified_when_audience_configured(client, monkeypatch, fresh_identity_caches):
    monkeypatch.setenv("CLAIMDESK_IAP_AUDIENCE", "/projects/123/locations/us-central1/services/claimdesk")
    seen = {}

    def fake_verify(assertion, audience):
        seen["args"] = (assertion, audience)
        return {"email": "ana@example.com", "iss": "https://cloud.google.com/iap"}

    monkeypatch.setattr(main, "verify_iap_jwt", fake_verify)
    assert client.post("/api/intakes", headers=iap()).status_code == 401  # no JWT header
    ok = client.post("/api/intakes", headers=signed())
    assert ok.status_code == 200
    assert seen["args"] == ("signed.jwt", "/projects/123/locations/us-central1/services/claimdesk")
    mismatch = client.post("/api/intakes", headers=signed("mallory@example.com"))
    assert mismatch.status_code == 401


def test_cloud_run_derives_iap_audience_and_trusts_only_the_jwt(client, monkeypatch, settings_env, fresh_identity_caches):
    settings_env(K_SERVICE="claimdesk", CLAIMDESK_REGION="us-central1")
    monkeypatch.setattr(main, "_fetch_project_number", lambda: "123")
    seen = []

    def fake_verify(assertion, audience):
        seen.append(audience)
        return {"email": "accounts.google.com:Ana@Example.com", "iss": main.IAP_ISSUER}

    monkeypatch.setattr(main, "verify_iap_jwt", fake_verify)
    # No e-mail header at all: the identity comes from the verified claims.
    body = client.post("/api/intakes", headers={"Origin": ORIGIN, "X-Goog-IAP-JWT-Assertion": "signed.jwt"}).json()
    assert body["user"] == "ana@example.com"
    assert seen == ["/projects/123/locations/us-central1/services/claimdesk"]


def test_unknown_project_number_still_verifies_without_audience(client, monkeypatch, settings_env, fresh_identity_caches):
    settings_env(K_SERVICE="claimdesk")
    monkeypatch.setattr(main, "_fetch_project_number", lambda: None)
    seen = []
    monkeypatch.setattr(main, "verify_iap_jwt", lambda assertion, audience: seen.append(audience) or {"email": "ana@example.com", "iss": main.IAP_ISSUER})
    assert client.post("/api/intakes", headers=signed()).status_code == 200
    assert seen == [None]  # signature + issuer still checked; a warning is logged


def test_iap_key_outage_is_a_retryable_503(client, monkeypatch, settings_env, fresh_identity_caches):
    settings_env(K_SERVICE="claimdesk")
    monkeypatch.setattr(main, "_fetch_project_number", lambda: "123")

    def keys_unreachable(assertion, audience):
        raise main.IdentityUnavailableError("could not fetch IAP public keys")

    monkeypatch.setattr(main, "verify_iap_jwt", keys_unreachable)
    response = client.post("/api/intakes", headers=signed())
    assert response.status_code == 503
    assert response.json()["detail"] == "We couldn't confirm your sign-in just now. Please try again in a moment."


def test_iap_claims_cache_never_outlives_token_expiry(monkeypatch, fresh_identity_caches):
    from google.oauth2 import id_token

    calls = []

    def fake_verify_token(assertion, request, audience=None, certs_url=None):
        calls.append(assertion)
        return {"email": "ana@example.com", "iss": main.IAP_ISSUER, "exp": main.time.time() - 1}

    monkeypatch.setattr(id_token, "verify_token", fake_verify_token)
    monkeypatch.setattr(main, "_get_iap_http_request", lambda: None)
    main.verify_iap_jwt("expired.jwt", "aud")
    main.verify_iap_jwt("expired.jwt", "aud")
    assert calls == ["expired.jwt", "expired.jwt"]  # an expired token is never served from cache
    monkeypatch.setattr(id_token, "verify_token", lambda *a, **k: {"email": "x@example.com", "iss": "https://evil.example"})
    with pytest.raises(main.AccessDeniedError):
        main.verify_iap_jwt("other.jwt", "aud")


def test_malformed_intake_id_is_a_plain_404(client):
    create(client)
    for bad in ("not-a-uuid", "A" * 32, "0" * 31 + "g"):
        response = client.get(f"/api/intakes/{bad}", headers=iap())
        assert response.status_code == 404
        assert response.json()["detail"] == "Intake not found or expired. Start a new intake."


def test_cross_site_or_originless_writes_are_rejected(client):
    base = {"X-Goog-Authenticated-User-Email": "accounts.google.com:ana@example.com"}
    assert client.post("/api/intakes", headers=base).status_code == 403
    assert client.post("/api/intakes", headers=base | {"Origin": "https://evil.example"}).status_code == 403


def test_intake_limit_per_owner(client):
    for _ in range(intake_session.MAX_INTAKES_PER_OWNER):
        create(client)
    response = client.post("/api/intakes", headers=iap())
    assert response.status_code == 429
    assert "Too many open intakes" in response.json()["detail"]
    create(client, "someone-else@example.com")  # limits are per owner


def test_delete_removes_intake_and_photos(client):
    intake_id = create(client)
    upload(client, intake_id)
    evidence = main.app.state.registry.evidence
    assert any(name.startswith(f"intakes/{intake_id}/evidence/") for name in evidence.objects)
    assert client.delete(f"/api/intakes/{intake_id}", headers=iap()).status_code == 200
    assert client.get(f"/api/intakes/{intake_id}", headers=iap()).status_code == 404
    assert not any(name.startswith(f"intakes/{intake_id}/evidence/") for name in evidence.objects)


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------
def test_photo_upload_is_verified_and_served_through_the_app(client):
    intake_id = create(client)
    response = upload(client, intake_id)
    assert response.status_code == 200, response.text
    photo = response.json()["photo"]
    assert photo["url"] == f"/api/intakes/{intake_id}/evidence/{photo['id']}"
    assert "object_path" not in photo and "storage_uri" not in photo  # no bucket paths / signed URLs leak
    assert photo["confirmed"] is True
    assert photo["document_types"] == ["water_line_photo"]  # unknown types dropped
    served = client.get(photo["url"], headers=iap())
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"
    assert served.content == JPEG


def test_photo_upload_rejects_non_jpeg_and_oversize(client, monkeypatch):
    intake_id = create(client)
    assert upload(client, intake_id, data=b"\x89PNG\r\n\x1a\nnot-a-jpeg").status_code == 415
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 50)
    too_big = upload(client, intake_id, data=JPEG)
    assert too_big.status_code == 413
    assert too_big.json()["detail"] == "Photos must be smaller than 5 MB."


def test_photo_limit_raises_limit_exceeded(client, settings_env):
    settings_env(CLAIMDESK_MAX_PHOTOS=1)  # limits come from settings / env vars
    intake_id = create(client)
    assert upload(client, intake_id).status_code == 200
    second = upload(client, intake_id)
    assert second.status_code == 429
    assert "already has 1 photos" in second.json()["detail"]


def test_photo_check_failure_degrades_to_unverified_photo(client, monkeypatch):
    async def broken(image, claimant_said):
        raise ModelCallError("vertex down")

    monkeypatch.setattr(tool_handlers, "inspect_photo", broken)
    intake_id = create(client)
    photo = upload(client, intake_id).json()["photo"]
    assert photo["verified"] is False
    assert photo["confirmed"] is False
    assert photo["document_types"] == []  # unverified photos never satisfy a document


# ---------------------------------------------------------------------------
# Packet
# ---------------------------------------------------------------------------
def test_packet_zip_contains_markdown_json_and_exact_photo(client):
    intake_id = create(client)
    photo = upload(client, intake_id).json()["photo"]
    response = client.get(f"/api/intakes/{intake_id}/packet.zip", headers=iap())
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {"packet.md", "packet.json", f"evidence/{photo['id']}.jpg"} <= names
        assert archive.read(f"evidence/{photo['id']}.jpg") == JPEG
        markdown = archive.read("packet.md").decode()
        assert f"evidence/{photo['id']}.jpg" in markdown
        assert "has not been sent to an adjuster" in markdown
        payload = json.loads(archive.read("packet.json"))
        assert payload["intake_id"] == intake_id
        assert "object_path" not in payload["evidence"][0]
    # The ZIP is also archived to the evidence store (GCS in production).
    stored = main.app.state.registry.evidence.objects[f"intakes/{intake_id}/packet/packet.zip"]
    assert stored[0] == response.content


def test_packet_json_route(client):
    intake_id = create(client)
    body = client.get(f"/api/intakes/{intake_id}/packet", headers=iap()).json()
    assert body["markdown"].startswith("# Flood Claim Intake Packet")
    assert body["packet"]["routing_decision"] == "needs_docs"
    assert "markdown" not in body["packet"]


def test_packet_row_matches_bigquery_schema():
    record = IntakeRecord(intake_id="abc", owner="ana@example.com", pipeline_result=make_result(), revision=3)
    row = packet_archive.packet_row(record, "gs://bucket/intakes/abc/packet/packet.zip")
    assert set(row) == {
        "intake_id", "created_at", "claim_type", "routing_decision", "severity", "intake_status", "policy_number",
        "loss_state", "loss_zip_code", "date_of_loss", "estimated_loss_usd", "missing_count", "packet_gcs_uri", "packet_json",
    }
    assert row["date_of_loss"] == "2026-09-20"
    assert row["estimated_loss_usd"] == 15000.0
    assert isinstance(row["missing_count"], int)
    assert row["claim_type"] == "home_flood"
    assert json.loads(row["packet_json"])["routing_decision"] == row["routing_decision"]
    blank = packet_archive.packet_row(IntakeRecord(intake_id="x", owner="o"), None)
    assert blank["date_of_loss"] is None and blank["policy_number"] is None and blank["estimated_loss_usd"] is None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
def test_storage_failure_returns_safe_message(client, monkeypatch):
    async def broken_save(record):
        raise StorageError("firestore said: projects/secret/databases/(default) PERMISSION_DENIED")

    monkeypatch.setattr(main.app.state.registry.store, "create", broken_save)
    response = client.post("/api/intakes", headers=iap())
    assert response.status_code == 503
    assert response.json()["detail"] == StorageError.user_message
    assert "secret" not in response.text


def test_static_ui_served_and_source_not_exposed(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Demo: flood-only scope, generated policy identities on real FEMA NFIP records" in page.text
    assert "not endorsed by FEMA" in page.text and "fictitious company" in page.text
    script = client.get("/static/claim.js")
    styles = client.get("/static/claim.css")
    assert script.status_code == 200 and styles.status_code == 200
    # The brand comes from /api/config (env vars), never from the static files.
    brand = get_settings().brand_name
    assert brand not in page.text and brand not in script.text
    assert client.get("/main.py").status_code == 404
    assert client.get("/static/../main.py").status_code == 404


def test_public_config_exposes_brand_and_limits(client, settings_env):
    settings_env(CLAIMDESK_BRAND_NAME="Harbor Mutual", CLAIMDESK_MAX_PHOTOS=7, CLAIMDESK_SUPPORTED_STATES="tx, fl")
    response = client.get("/api/config", headers=iap())
    assert response.status_code == 200
    body = response.json()
    assert body["brand_name"] == "Harbor Mutual"
    assert body["max_photos"] == 7
    assert body["supported_states"] == ["TX", "FL"]
    assert body["user"] == "ana@example.com"
    for key in ("agent_display_name", "claims_phone", "brand_tagline", "live_session_minutes", "camera_fps"):
        assert key in body
    # Nothing secret or infrastructure-specific leaks to the browser.
    assert not {"project_id", "gcs_bucket", "bq_dataset"} & set(body)


def test_public_config_requires_identity_on_cloud_run(client, monkeypatch, settings_env):
    settings_env(K_SERVICE="claimdesk")
    assert client.get("/api/config", headers={"Origin": ORIGIN}).status_code == 401


# ---------------------------------------------------------------------------
# Hardening: sketch MIME, ZIP modes, delete vs. background save, storage errors
# ---------------------------------------------------------------------------
def test_sketch_route_only_echoes_known_image_types(client):
    intake_id = create(client)
    registry = main.app.state.registry
    intake = registry.active[intake_id]
    client.portal.call(registry.evidence.put, f"intakes/{intake_id}/sketch/v1.png", b"\x89PNG-bytes", "image/png")
    intake.record.sketch = {"object_path": f"intakes/{intake_id}/sketch/v1.png", "mime_type": "text/html", "brief": "x", "version": 1}
    response = client.get(f"/api/intakes/{intake_id}/sketch", headers=iap())
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"  # never text/html
    intake.record.sketch["mime_type"] = "image/webp"
    assert client.get(f"/api/intakes/{intake_id}/sketch", headers=iap()).headers["content-type"] == "image/webp"


def test_packet_zip_stores_photos_and_deflates_text(client):
    intake_id = create(client)
    photo = upload(client, intake_id).json()["photo"]
    response = client.get(f"/api/intakes/{intake_id}/packet.zip", headers=iap())
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        modes = {info.filename: info.compress_type for info in archive.infolist()}
    assert modes[f"evidence/{photo['id']}.jpg"] == zipfile.ZIP_STORED  # JPEG is already compressed
    assert modes["packet.md"] == zipfile.ZIP_DEFLATED
    assert modes["packet.json"] == zipfile.ZIP_DEFLATED


def test_delete_waits_for_in_flight_background_save(client, monkeypatch):
    intake_id = create(client)
    registry = main.app.state.registry
    store = registry.store
    events: list[str] = []
    real_save, real_delete = store.save, store.delete

    async def slow_save(record):
        events.append("save-start")
        await asyncio.sleep(0.2)
        await real_save(record)
        events.append("save-end")

    async def recording_delete(intake_id):
        events.append("delete")
        await real_delete(intake_id)

    monkeypatch.setattr(store, "save", slow_save)
    monkeypatch.setattr(store, "delete", recording_delete)

    async def scenario():
        intake = await registry.get_owned(intake_id, "ana@example.com")
        registry.persist_soon(intake)
        await asyncio.sleep(0.35)  # the coalesced save is now mid-write
        await registry.discard(intake, purge=True)

    client.portal.call(scenario)
    # The delete runs last, so the in-flight write cannot re-create the doc.
    assert events == ["save-start", "save-end", "delete"]
    assert client.portal.call(store.load, intake_id) is None


def test_gcs_network_and_auth_failures_become_storage_errors():
    import requests
    from google.auth import exceptions as auth_exceptions

    from webapp.evidence_store import GcsEvidenceStore

    class Blob:
        def upload_from_string(self, data, content_type=None):
            raise requests.ConnectionError("network unreachable")

        def download_as_bytes(self):
            raise auth_exceptions.RefreshError("metadata server hiccup")

    class Bucket:
        def blob(self, name):
            return Blob()

    store = GcsEvidenceStore(dataclasses.replace(get_settings(), gcs_bucket="test-bucket"))
    store.__dict__["_bucket"] = Bucket()  # pre-fill the cached_property: no real client
    with pytest.raises(StorageError):
        asyncio.run(store.put("intakes/x/evidence/a.jpg", b"x", "image/jpeg"))
    with pytest.raises(StorageError):
        asyncio.run(store.get("intakes/x/evidence/a.jpg"))


def test_firestore_transport_failures_become_storage_errors():
    import requests

    from webapp.intake_store import FirestoreIntakeStore

    class Document:
        async def get(self):
            raise requests.Timeout("slow network")

        async def set(self, data):
            raise TimeoutError("deadline")

        async def delete(self):
            raise OSError("socket closed")

    class Collection:
        def document(self, name):
            return Document()

    store = object.__new__(FirestoreIntakeStore)  # skip __init__: no real Firestore client
    store._collection = Collection()
    for call in (store.load("abc"), store.save(IntakeRecord(intake_id="abc", owner="o")), store.delete("abc")):
        with pytest.raises(StorageError):
            asyncio.run(call)


def test_safety_row_is_urgent_only_for_present_immediate_hazards():
    def safety_field(safety_facts):
        record = IntakeRecord(intake_id="abc", owner="o", pipeline_result=make_result(FACTS | {"safety_facts": safety_facts}))
        return build_desk_state(record)["fields"]["safety"]

    mold = safety_field([{"category": "mold", "status": "present", "description": "Mold on the drywall"}])
    assert mold["status"] != "urgent" and "Mold on the drywall" in mold["value"]
    unsure_gas = safety_field([{"category": "gas", "status": "uncertain", "description": "Might smell gas"}])
    assert unsure_gas["status"] != "urgent"
    wires = safety_field([{"category": "electric", "status": "present", "description": "Live wires in the water"}])
    assert wires["status"] == "urgent" and "Live wires in the water" in wires["value"]


def test_model_errors_are_logged_without_traceback(caplog):
    import logging

    from webapp.error_logging import log_failure

    logger = logging.getLogger("test.error_logging")
    try:
        try:
            raise ValueError("pydantic said: input_value='Ana Lopez, 555-123-4567'")
        except ValueError as cause:
            raise ModelCallError("Photo check failed: ValidationError") from cause
    except ModelCallError as exc:
        with caplog.at_level(logging.ERROR, logger="test.error_logging"):
            log_failure(logger, "Tool failed", exc)
    record = caplog.records[-1]
    assert record.exc_info is None
    assert "Ana Lopez" not in caplog.text and "Photo check failed" in record.getMessage()
    with caplog.at_level(logging.ERROR, logger="test.error_logging"):
        log_failure(logger, "Bug", RuntimeError("boom"))
    assert caplog.records[-1].exc_info is not None  # real bugs keep their stack trace


def test_pipeline_gets_the_desk_timezone_clock(client, monkeypatch, offline):
    seen = {}

    async def fake_pipeline(conversation, *, intake_id=None, received_evidence=None, reference_time=None):
        seen["reference_time"] = reference_time
        return make_result(received=received_evidence)

    monkeypatch.setattr(intake_session, "run_intake_pipeline", fake_pipeline)
    intake_id = create(client)
    registry = main.app.state.registry
    client.portal.call(registry.refresh_pipeline, registry.active[intake_id])
    assert seen["reference_time"].tzinfo is not None
    assert seen["reference_time"].utcoffset() == local_now().utcoffset()
