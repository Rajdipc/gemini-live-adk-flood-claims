"""Live call, tool handler and pipeline-cache tests (all model calls are fakes).

These port the behavioural regressions of the original demo to the new
modules: frozen camera frames, honest (unconfirmed) captures, stale frames,
photo limits, sketch races vs. corrections and camera toggles, camera notices
that never enter the transcript, and pipeline re-runs when facts change
mid-flight.
"""

from __future__ import annotations

import asyncio
import base64
import time
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from google.genai import types
from starlette.websockets import WebSocketDisconnect

from claimdesk.contracts import PolicyRecord
from claimdesk.settings import get_settings
from webapp import intake_session, live_bridge, main, tool_handlers
from webapp.evidence_store import MemoryEvidenceStore
from webapp.intake_session import IntakeRegistry, LiveIntake, append_turn
from webapp.intake_store import IntakeRecord, MemoryIntakeStore
from webapp.trace_logger import ConversationTraceLogger

from test_webapp_api import JPEG, iap, make_result, settings_env  # noqa: F401  shared offline fixtures (tests/ is on sys.path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def registry() -> IntakeRegistry:
    return IntakeRegistry(MemoryIntakeStore(), MemoryEvidenceStore(), ConversationTraceLogger(enabled=False, table_id="t"))


def intake(**runtime) -> LiveIntake:
    return LiveIntake(record=IntakeRecord(intake_id="intake-1", owner="ana@example.com"), **runtime)


def finding(observation="Blank wall", supports=False, kinds=()):
    return tool_handlers.FrameFinding(observation=observation, supports_claimant_description=supports, document_types=list(kinds))


def image_response(data: bytes):
    return NS(candidates=[NS(content=NS(parts=[NS(inline_data=NS(data=data, mime_type="image/png"))]))])


def fake_flash(generate):
    return NS(aio=NS(models=NS(generate_content=generate)))


@pytest.fixture(autouse=True)
def no_real_models(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a real Gemini client must not be created in tests")

    monkeypatch.setattr(tool_handlers, "get_flash_client", forbidden)
    monkeypatch.setattr(live_bridge, "get_live_client", forbidden)


# ---------------------------------------------------------------------------
# Evidence capture
# ---------------------------------------------------------------------------
async def test_exact_frozen_frame_is_verified_and_saved_even_if_a_new_frame_arrives(monkeypatch):
    reg, live = registry(), intake(last_frame=b"\xff\xd8\xffFRAME_A", last_frame_id="a", last_frame_at=time.monotonic())

    async def inspect(image, claimant_said):
        assert image == b"\xff\xd8\xffFRAME_A"
        live.last_frame = b"\xff\xd8\xffFRAME_B"  # camera keeps streaming meanwhile
        return finding("Blank wall", supports=False)

    monkeypatch.setattr(tool_handlers, "inspect_photo", inspect)
    result = await tool_handlers.capture_evidence_photo(live, reg, {"observation": "Huge crack", "confirmed": True, "claimant_description": "a crack"})
    photo = live.record.evidence_photos[0]
    assert await reg.evidence.get(photo["object_path"]) == b"\xff\xd8\xffFRAME_A"
    assert photo["caption"] == "Blank wall"  # independent check wins over the Live model's claim
    assert result["confirmed"] is False
    assert "unconfirmed" in live.record.camera_notes[0]
    assert live.record.revision == 1


async def test_capture_without_claimant_description_is_unconfirmed(monkeypatch):
    reg, live = registry(), intake(last_frame=b"\xff\xd8\xffX", last_frame_at=time.monotonic())

    async def inspect(image, claimant_said):
        return finding("Wall", supports=True)

    monkeypatch.setattr(tool_handlers, "inspect_photo", inspect)
    assert (await tool_handlers.capture_evidence_photo(live, reg, {}))["confirmed"] is False


async def test_stale_frames_and_photo_limit_are_not_captured(monkeypatch):
    reg, live = registry(), intake(last_frame=b"\xff\xd8\xffOLD", last_frame_at=time.monotonic() - 30)
    assert (await tool_handlers.capture_evidence_photo(live, reg, {}))["pinned"] is False
    live.last_frame_at = time.monotonic()
    live.record.evidence_photos = [{"id": str(i)} for i in range(get_settings().max_photos)]
    result = await tool_handlers.capture_evidence_photo(live, reg, {})
    assert result["pinned"] is False and "photos" in result["message"]


async def test_camera_off_forgets_frame_but_keeps_saved_photos():
    live = intake(camera_enabled=True, last_frame=b"frame", last_frame_id="f", last_frame_at=time.monotonic())
    live.record.evidence_photos = [{"id": "kept"}]
    live.set_camera_mode(False)
    assert live.last_frame is None and not live.camera_enabled
    assert live.record.evidence_photos == [{"id": "kept"}]
    assert (await tool_handlers.capture_evidence_photo(live, registry(), {}))["pinned"] is False


# ---------------------------------------------------------------------------
# Sketches
# ---------------------------------------------------------------------------
async def test_older_sketch_cannot_overwrite_a_correction(monkeypatch):
    reg, live = registry(), intake()

    async def generate(**kwargs):
        old = "OLD_SCENE" in kwargs["contents"]
        await asyncio.sleep(0.03 if old else 0.001)
        return image_response(b"old" if old else b"new")

    monkeypatch.setattr(tool_handlers, "get_flash_client", lambda: fake_flash(generate))
    results = await asyncio.gather(
        tool_handlers.render_damage_sketch(live, reg, {"scene_description": "OLD_SCENE", "trigger": "automatic"}),
        tool_handlers.render_damage_sketch(live, reg, {"scene_description": "CORRECTED_SCENE", "trigger": "automatic"}),
    )
    assert live.record.sketch["brief"] == "CORRECTED_SCENE"
    assert results[0]["sketched"] is False
    assert await reg.evidence.get(live.record.sketch["object_path"]) == b"new"


async def test_camera_on_blocks_automatic_sketch_without_a_model_call():
    result = await tool_handlers.render_damage_sketch(intake(camera_enabled=True), registry(), {"scene_description": "Flooded kitchen", "trigger": "automatic"})
    assert result["sketched"] is False  # the autouse fixture would fail on any client creation


async def test_explicit_and_correction_sketches_work_with_camera_on(monkeypatch):
    reg, live = registry(), intake(camera_enabled=True)

    async def generate(**kwargs):
        return image_response(b"sketch")

    monkeypatch.setattr(tool_handlers, "get_flash_client", lambda: fake_flash(generate))
    for trigger, brief in [("explicit_request", "Living room, water from front door"), ("correction", "Water came from the back door")]:
        assert (await tool_handlers.render_damage_sketch(live, reg, {"scene_description": brief, "trigger": trigger}))["sketched"] is True
    assert live.record.sketch["version"] == 2


async def test_camera_toggle_discards_inflight_automatic_sketch(monkeypatch):
    reg, live = registry(), intake()

    async def generate(**kwargs):
        live.set_camera_mode(True)
        live.set_camera_mode(False)
        return image_response(b"outdated")

    monkeypatch.setattr(tool_handlers, "get_flash_client", lambda: fake_flash(generate))
    result = await tool_handlers.render_damage_sketch(live, reg, {"scene_description": "Flooded garage", "trigger": "automatic"})
    assert result["sketched"] is False and live.record.sketch is None


async def test_unchanged_scene_reuses_sketch_and_bad_requests_do_not_draw():
    live = intake()
    live.record.sketch = {"brief": "Flooded kitchen", "version": 1}
    assert (await tool_handlers.render_damage_sketch(live, registry(), {"scene_description": "flooded kitchen", "trigger": "automatic"}))["reused"] is True
    for args in [{}, {"scene_description": "Kitchen", "trigger": "correction"}, {"scene_description": "Kitchen", "trigger": "whatever"}]:
        assert (await tool_handlers.render_damage_sketch(intake(), registry(), args))["sketched"] is False


# ---------------------------------------------------------------------------
# Pipeline cache + registry
# ---------------------------------------------------------------------------
async def test_pipeline_reruns_when_facts_change_in_flight_and_attaches_policy(monkeypatch):
    reg, live = registry(), intake()
    append_turn(live.record, "Claimant", "Original story", "one")
    calls = []

    async def run(conversation, **kwargs):
        calls.append(conversation)
        if len(calls) == 1:
            append_turn(live.record, "Claimant", "Correction: it was 10 inches", "two")
        return make_result()

    monkeypatch.setattr(intake_session, "run_intake_pipeline", run)
    monkeypatch.setattr(intake_session, "lookup_policy", lambda n: PolicyRecord(found=True, policy_number=n, status="active"))
    result = await reg.refresh_pipeline(live)
    assert len(calls) == 2 and "Correction" in calls[1]
    assert live.record.pipeline_revision == live.record.revision
    assert live.record.policy_record["policy_number"] == "FLD-TX-7Q2K9M"
    assert result is live.record.pipeline_result
    # Cached: same revision -> no new run. Agent turns do not bump the revision.
    append_turn(live.record, "Agent", "Thanks, noted.")
    await reg.refresh_pipeline(live)
    assert len(calls) == 2


async def test_conversation_limit_raises_limit_exceeded():
    record = IntakeRecord(intake_id="x", owner="o")
    append_turn(record, "Claimant", "a" * 7999)
    with pytest.raises(intake_session.LimitExceededError):
        for i in range(20):
            append_turn(record, "Claimant", "b" * 7999, f"t{i}")


async def test_sweep_drops_expired_intakes_and_cancels_jobs():
    reg = registry()
    live = await reg.create("ana@example.com")
    job = live.track(asyncio.create_task(asyncio.sleep(100)))
    live.last_frame = b"image"
    live.record.expires_at = time.time() - 1
    await reg.sweep()
    assert live.intake_id not in reg.active
    assert job.cancelled() and live.last_frame is None


async def test_intake_reloads_from_store_after_restart():
    store, evidence = MemoryIntakeStore(), MemoryEvidenceStore()
    first = IntakeRegistry(store, evidence, ConversationTraceLogger(enabled=False, table_id="t"))
    live = await first.create("ana@example.com")
    append_turn(live.record, "Claimant", "Water came in from the bayou")
    await first.persist(live)
    restarted = IntakeRegistry(store, evidence, ConversationTraceLogger(enabled=False, table_id="t"))
    again = await restarted.get_owned(live.intake_id, "ana@example.com")
    assert [t["text"] for t in again.record.transcript][-1] == "Water came in from the bayou"
    with pytest.raises(intake_session.IntakeNotFoundError):
        await restarted.get_owned(live.intake_id, "bob@example.com")


@pytest.fixture(autouse=True)
def forget_live_location():
    """The bridge caches the Live location that worked; isolate every test."""

    live_bridge.remember_live_location(None)
    yield
    live_bridge.remember_live_location(None)


# ---------------------------------------------------------------------------
# WebSocket bridge (fake Gemini Live)
# ---------------------------------------------------------------------------
class FakeLive:
    """Stands in for a google-genai AsyncSession."""

    def __init__(self, script=()):
        self.client_content: list[dict] = []
        self.realtime: list[dict] = []
        self.tool_responses: list = []
        self.script = list(script)

    async def send_client_content(self, **kwargs):
        self.client_content.append(kwargs)

    async def send_realtime_input(self, **kwargs):
        self.realtime.append(kwargs)

    async def send_tool_response(self, **kwargs):
        self.tool_responses.append(kwargs["function_responses"][0])

    async def receive(self):
        while self.script:
            yield self.script.pop(0)
        await asyncio.Event().wait()  # then stay silent forever
        yield None


def install_fake_live(monkeypatch, live: FakeLive) -> dict:
    captured: dict = {}

    class Connection:
        async def __aenter__(self):
            return live

        async def __aexit__(self, *exc):
            return False

    def connect(**kwargs):
        captured.update(kwargs)
        return Connection()

    monkeypatch.setattr(live_bridge, "get_live_client", lambda location=None: NS(aio=NS(live=NS(connect=connect))))
    return captured


def receive_until(ws, predicate, limit=50):
    for _ in range(limit):
        message = ws.receive_json()
        if predicate(message):
            return message
    raise AssertionError("expected message not received")


def drain_until_closed(ws):
    with pytest.raises(WebSocketDisconnect):
        for _ in range(100):
            ws.receive_json()


@pytest.fixture()
def api(monkeypatch):
    async def fake_pipeline(conversation, **kwargs):
        return make_result(received=kwargs.get("received_evidence"))

    monkeypatch.setattr(intake_session, "run_intake_pipeline", fake_pipeline)
    monkeypatch.setattr(intake_session, "lookup_policy", lambda n: PolicyRecord(found=True, policy_number=n, policyholder_name="Ana Lopez", status="active"))
    with TestClient(main.app) as client:
        yield client


def new_intake(client) -> str:
    return client.post("/api/intakes", headers=iap()).json()["intake_id"]


def test_camera_notices_reach_the_model_but_not_the_transcript(api, monkeypatch):
    fake = FakeLive()
    captured = install_fake_live(monkeypatch, fake)
    intake_id = new_intake(api)
    with api.websocket_connect(f"/ws/live?intake_id={intake_id}", headers=iap()) as ws:
        ready = receive_until(ws, lambda m: m["type"] == "ready")
        assert ready["intake_id"] == intake_id
        ws.send_json({"type": "camera_state", "enabled": True})
        ws.send_json({"type": "camera_state", "enabled": False})
        ws.send_json({"type": "close"})
        drain_until_closed(ws)
    assert captured["model"] == "gemini-3.8-live"  # model always comes from settings
    assert captured["config"].speech_config.voice_config.prebuilt_voice_config.voice_name == "Kore"
    assert [c["turn_complete"] for c in fake.client_content] == [False, False]
    assert "camera is ON" in fake.client_content[0]["turns"].parts[0].text
    assert "camera is OFF" in fake.client_content[1]["turns"].parts[0].text
    state = api.get(f"/api/intakes/{intake_id}", headers=iap()).json()["state"]
    assert [t["speaker"] for t in state["transcript"]] == ["Agent"]


def test_text_audio_video_flow_and_pipeline_state(api, monkeypatch):
    fake = FakeLive()
    install_fake_live(monkeypatch, fake)
    intake_id = new_intake(api)
    with api.websocket_connect(f"/ws/live?intake_id={intake_id}", headers=iap()) as ws:
        receive_until(ws, lambda m: m["type"] == "ready")
        ws.send_json({"type": "text", "text": "The bayou flooded my living room", "id": "t1"})
        echo = receive_until(ws, lambda m: m["type"] == "transcript")
        assert echo["final"] and echo["id"] == "t1"
        state = receive_until(ws, lambda m: m["type"] == "state" and m["state"]["revision"] == 1)
        assert state["state"]["fields"]["claimant"]["value"] == "Ana Lopez"
        ws.send_json({"type": "video", "data": base64.b64encode(JPEG).decode()})
        ws.send_json({"type": "audio", "data": base64.b64encode(b"\x00\x00" * 160).decode()})
        ws.send_json({"type": "video", "data": base64.b64encode(b"not a jpeg").decode()})
        error = receive_until(ws, lambda m: m["type"] == "error")
        assert "JPEG" in error["message"]
        ws.send_json({"type": "close"})
        drain_until_closed(ws)
    assert [list(item)[0] for item in fake.realtime] == ["video", "audio"]
    # The typed turn is sent as a complete turn; camera-mode notices (sent when
    # the first frame arrives) are context-only and may follow it.
    typed = [c for c in fake.client_content if c["turns"].parts[0].text == "The bayou flooded my living room"]
    assert len(typed) == 1 and typed[0]["turn_complete"] is True
    registry_intake = main.app.state.registry.active[intake_id]
    assert registry_intake.live_socket is None and registry_intake.camera_enabled is False


def test_tool_call_is_executed_and_answered_with_scheduling(api, monkeypatch):
    call = types.FunctionCall(id="call-1", name="find_policy", args={"policy_number": "FLD-TX-7Q2K9M"})
    fake = FakeLive(script=[types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[call]))])
    install_fake_live(monkeypatch, fake)
    intake_id = new_intake(api)
    with api.websocket_connect(f"/ws/live?intake_id={intake_id}", headers=iap()) as ws:
        done = receive_until(ws, lambda m: m["type"] == "tool" and m["phase"] == "done")
        assert done["name"] == "find_policy" and done["result"]["found"] is True
        deadline = time.monotonic() + 2
        while not fake.tool_responses and time.monotonic() < deadline:
            time.sleep(0.01)
        ws.send_json({"type": "close"})
        drain_until_closed(ws)
    response = fake.tool_responses[0]
    assert response.id == "call-1" and response.name == "find_policy"
    assert response.scheduling == types.FunctionResponseScheduling.WHEN_IDLE  # active policy: no interruption


def test_websocket_rejects_wrong_owner_and_foreign_origin(api, monkeypatch):
    install_fake_live(monkeypatch, FakeLive())
    intake_id = new_intake(api)
    with pytest.raises(WebSocketDisconnect):
        with api.websocket_connect(f"/ws/live?intake_id={intake_id}", headers=iap("bob@example.com")) as ws:
            ws.receive_json()
    with pytest.raises(WebSocketDisconnect):
        with api.websocket_connect(f"/ws/live?intake_id={intake_id}", headers=iap() | {"Origin": "https://evil.example"}) as ws:
            ws.receive_json()


# ---------------------------------------------------------------------------
# Live reconnects (go_away / session resumption) and bridge robustness
# ---------------------------------------------------------------------------
def install_live_sequence(monkeypatch, sessions: list) -> list:
    """Each ``connect`` returns the next fake session; returns the configs used."""

    configs: list = []
    remaining = list(sessions)

    class Connection:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            if isinstance(self.session, BaseException):
                raise self.session
            return self.session

        async def __aexit__(self, *exc):
            return False

    def connect(**kwargs):
        configs.append(kwargs["config"])
        return Connection(remaining.pop(0))

    monkeypatch.setattr(live_bridge, "get_live_client", lambda location=None: NS(aio=NS(live=NS(connect=connect))))
    return configs


def resumption(handle: str):
    return types.LiveServerMessage(session_resumption_update=types.LiveServerSessionResumptionUpdate(new_handle=handle, resumable=True))


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate(), "condition not reached in time"


def test_go_away_resumes_the_same_call_with_the_handle(api, monkeypatch):
    first = FakeLive(script=[resumption("h1"), types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="5s"))])
    second = FakeLive()
    configs = install_live_sequence(monkeypatch, [first, second])
    intake_id = new_intake(api)
    append_turn(main.app.state.registry.active[intake_id].record, "Claimant", "Water came in from the bayou", "earlier")
    seen: list[dict] = []
    with api.websocket_connect(f"/ws/live?intake_id={intake_id}", headers=iap()) as ws:
        seen.append(receive_until(ws, lambda m: m["type"] == "ready"))
        reconnecting = receive_until(ws, lambda m: m["type"] == "status")
        assert reconnecting["code"] == "reconnecting" and reconnecting["attempt"] == 1
        resumed = receive_until(ws, lambda m: m["type"] == "status")
        assert resumed["code"] == "resumed"
        ws.send_json({"type": "text", "text": "It is about 8 inches deep", "id": "t-after"})
        seen.append(receive_until(ws, lambda m: m["type"] == "transcript" and m.get("id") == "t-after"))
        wait_for(lambda: second.client_content)
        ws.send_json({"type": "close"})
        with pytest.raises(WebSocketDisconnect):
            for _ in range(100):
                seen.append(ws.receive_json())
    assert len(configs) == 2
    assert configs[0].session_resumption.handle is None
    assert configs[1].session_resumption.handle == "h1"  # resumed, not a new session
    assert configs[1].context_window_compression.sliding_window is not None
    # The fresh first session got the transcript replayed; the resumed one did not.
    assert first.client_content and first.client_content[0]["turn_complete"] is False
    assert [c["turns"].parts[0].text for c in second.client_content] == ["It is about 8 inches deep"]
    assert not any(m["type"] == "error" for m in seen)
    assert sum(m["type"] == "ready" for m in seen) == 1  # the page never re-initialises


def test_drop_after_handle_reconnects_but_drop_without_handle_ends_the_call(api, monkeypatch):
    class Dropping(FakeLive):
        async def receive(self):
            while self.script:
                yield self.script.pop(0)
            await asyncio.sleep(0.05)
            raise ConnectionError("socket closed")
            yield  # pragma: no cover - makes this an async generator

    # 1) No handle yet: nothing to resume from -> friendly model_unavailable.
    install_live_sequence(monkeypatch, [Dropping()])
    intake_id = new_intake(api)
    with api.websocket_connect(f"/ws/live?intake_id={intake_id}", headers=iap()) as ws:
        error = receive_until(ws, lambda m: m["type"] == "error")
        assert error["code"] == "model_unavailable"
        drain_until_closed(ws)
    # 2) With a handle: the call survives the drop.
    second = FakeLive()
    configs = install_live_sequence(monkeypatch, [Dropping(script=[resumption("h2")]), second])
    intake_id = new_intake(api)
    with api.websocket_connect(f"/ws/live?intake_id={intake_id}", headers=iap()) as ws:
        assert receive_until(ws, lambda m: m["type"] in {"status", "error"})["code"] == "reconnecting"
        assert receive_until(ws, lambda m: m["type"] in {"status", "error"})["code"] == "resumed"
        ws.send_json({"type": "close"})
        drain_until_closed(ws)
    assert configs[1].session_resumption.handle == "h2"


def test_unexpected_tool_crash_still_answers_the_model(api, monkeypatch):
    async def crashing(*args, **kwargs):
        raise RuntimeError("bug in the tool: secret detail")

    monkeypatch.setattr(tool_handlers, "find_policy", crashing)
    call = types.FunctionCall(id="call-x", name="find_policy", args={"policy_number": "FLD-TX-7Q2K9M"})
    fake = FakeLive(script=[types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[call]))])
    install_fake_live(monkeypatch, fake)
    intake_id = new_intake(api)
    with api.websocket_connect(f"/ws/live?intake_id={intake_id}", headers=iap()) as ws:
        failed = receive_until(ws, lambda m: m["type"] == "tool" and m["phase"] != "running")
        assert failed["phase"] == "error"
        assert failed["headline"] == live_bridge.TOOL_CRASH_MESSAGE
        assert "secret" not in str(failed)
        wait_for(lambda: fake.tool_responses)
        ws.send_json({"type": "close"})
        drain_until_closed(ws)
    response = fake.tool_responses[0]
    assert response.id == "call-x" and "error" in response.response  # the model is never left waiting


def test_binary_frames_are_ignored_and_bad_json_gets_a_fixed_message(api, monkeypatch):
    fake = FakeLive()
    install_fake_live(monkeypatch, fake)
    intake_id = new_intake(api)
    with api.websocket_connect(f"/ws/live?intake_id={intake_id}", headers=iap()) as ws:
        receive_until(ws, lambda m: m["type"] == "ready")
        ws.send_bytes(b"\x00\x01binary")
        ws.send_text("{not json")
        error = receive_until(ws, lambda m: m["type"] == "error")
        assert error["message"] == live_bridge.UNREADABLE_MESSAGE  # never the parser's text
        ws.send_json({"type": "text", "text": "Still here", "id": "t2"})
        assert receive_until(ws, lambda m: m["type"] == "transcript")["id"] == "t2"
        ws.send_json({"type": "close"})
        drain_until_closed(ws)


def test_failed_accept_releases_the_call_slot(api):
    intake_id = new_intake(api)
    live = main.app.state.registry.active[intake_id]

    class BrokenSocket:
        headers = {"origin": "http://testserver", "host": "testserver", "x-goog-authenticated-user-email": "accounts.google.com:ana@example.com"}
        query_params = {"intake_id": intake_id}
        app = main.app

        async def accept(self):
            raise RuntimeError("browser vanished mid-handshake")

        async def close(self, code=1000):
            pass

    with pytest.raises(RuntimeError):
        api.portal.call(main.live_call, BrokenSocket())
    assert live.live_socket is None  # the next call for this claim is not refused


def test_websocket_rejects_malformed_intake_id(api, monkeypatch):
    install_fake_live(monkeypatch, FakeLive())
    with pytest.raises(WebSocketDisconnect):
        with api.websocket_connect("/ws/live?intake_id=../../x", headers=iap()) as ws:
            ws.receive_json()


# ---------------------------------------------------------------------------
# Live model location fallback (global -> us-central1)
# ---------------------------------------------------------------------------
class LocationRejected(Exception):
    """Mimics google.genai.errors.APIError for a model not served at a location."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def install_locations(monkeypatch, outcomes: dict):
    """Fake ``get_live_client(location)``; ``outcomes[location]`` is a session or an exception."""

    attempts: list[str] = []

    def client_for(location=None):
        class Connection:
            async def __aenter__(self):
                attempts.append(location)
                outcome = outcomes[location]
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            async def __aexit__(self, *exc):
                return False

        return NS(aio=NS(live=NS(connect=lambda **kwargs: Connection())))

    monkeypatch.setattr(live_bridge, "get_live_client", client_for)
    return attempts


async def test_live_falls_back_once_and_caches_the_working_location(monkeypatch, caplog, settings_env):
    settings_env(LIVE_MODEL_LOCATION="global", LIVE_MODEL_FALLBACK_LOCATION="us-central1")
    session = object()
    attempts = install_locations(monkeypatch, {"global": LocationRejected(404, "Publisher model not found"), "us-central1": session})
    with caplog.at_level("WARNING", logger="webapp.live_bridge"):
        async with live_bridge.open_live_session("gemini-3.8-live", config=None) as (got, location):
            assert got is session and location == "us-central1"
    assert attempts == ["global", "us-central1"]
    warning = next(r for r in caplog.records if r.levelname == "WARNING")
    assert warning.json_fields["location"] == "global"
    assert warning.json_fields["error_type"] == "LocationRejected"
    # The next call goes straight to the location that worked.
    async with live_bridge.open_live_session("gemini-3.8-live", config=None) as (_, location):
        assert location == "us-central1"
    assert attempts == ["global", "us-central1", "us-central1"]


async def test_live_does_not_fall_back_on_unrelated_errors(monkeypatch, settings_env):
    settings_env(LIVE_MODEL_LOCATION="global", LIVE_MODEL_FALLBACK_LOCATION="us-central1")
    attempts = install_locations(monkeypatch, {"global": ConnectionError("network is down"), "us-central1": object()})
    with pytest.raises(ConnectionError):
        async with live_bridge.open_live_session("gemini-3.8-live", config=None):
            pass
    assert attempts == ["global"]


async def test_live_raises_when_every_location_rejects(monkeypatch, settings_env):
    settings_env(LIVE_MODEL_LOCATION="global", LIVE_MODEL_FALLBACK_LOCATION="us-central1")
    install_locations(
        monkeypatch,
        {"global": LocationRejected(400, "invalid argument: unsupported model"), "us-central1": LocationRejected(403, "permission denied")},
    )
    with pytest.raises(LocationRejected):
        async with live_bridge.open_live_session("gemini-3.8-live", config=None):
            pass


def test_location_rejection_classifier():
    assert live_bridge.is_location_rejection(LocationRejected(404, "x"))
    assert live_bridge.is_location_rejection(RuntimeError("model is not supported in this location"))
    assert not live_bridge.is_location_rejection(LocationRejected(429, "resource exhausted"))
    assert not live_bridge.is_location_rejection(TimeoutError("timed out"))


def test_websocket_reports_which_location_served_the_call(api, monkeypatch, settings_env):
    settings_env(LIVE_MODEL_LOCATION="global", LIVE_MODEL_FALLBACK_LOCATION="us-central1")
    install_locations(monkeypatch, {"global": LocationRejected(404, "not found"), "us-central1": FakeLive()})
    intake_id = new_intake(api)
    with api.websocket_connect(f"/ws/live?intake_id={intake_id}", headers=iap()) as ws:
        session = receive_until(ws, lambda m: m["type"] == "session")
        assert session["location"] == "us-central1" and session["model"] == "gemini-3.8-live"
        receive_until(ws, lambda m: m["type"] == "ready")
        ws.send_json({"type": "close"})
        drain_until_closed(ws)


def test_model_unavailable_error_has_a_friendly_code(api, monkeypatch, settings_env):
    settings_env(LIVE_MODEL_LOCATION="global", LIVE_MODEL_FALLBACK_LOCATION="global")
    from google.genai import errors as genai_errors

    install_locations(monkeypatch, {"global": genai_errors.APIError(503, {"error": {"message": "unavailable"}})})
    intake_id = new_intake(api)
    with api.websocket_connect(f"/ws/live?intake_id={intake_id}", headers=iap()) as ws:
        error = receive_until(ws, lambda m: m["type"] == "error")
        assert error["code"] == "model_unavailable"
        assert "saved" in error["message"]
        drain_until_closed(ws)


# ---------------------------------------------------------------------------
# Persona built from settings
# ---------------------------------------------------------------------------
def test_system_instruction_uses_brand_agent_and_states(settings_env):
    from webapp import voice_tools

    settings_env(CLAIMDESK_BRAND_NAME="Harbor Mutual", CLAIMDESK_AGENT_NAME="Sam", CLAIMDESK_SUPPORTED_STATES="TX,FL")
    text = voice_tools.SYSTEM_INSTRUCTION
    assert '"Sam from Harbor Mutual flood claims"' in text
    assert "Texas and Florida" in text and "Colorado" not in text
    assert "{" not in text  # every template slot was filled
    config = voice_tools.build_live_config(get_settings())
    assert config.system_instruction.startswith("You are Sam, the voice of Harbor Mutual flood claims.")
    assert config.speech_config.voice_config.prebuilt_voice_config.voice_name == get_settings().voice_name
    # Backwards-compatible alias used by evals/export_live_traces.py.
    assert voice_tools.DESK_INSTRUCTION == text


def test_spoken_state_list():
    from webapp.voice_tools import spoken_state_list

    assert spoken_state_list(("CO", "TX", "FL", "LA", "NC")) == "Colorado, Texas, Florida, Louisiana and North Carolina"
    assert spoken_state_list(["co"]) == "Colorado"
    assert spoken_state_list([]) == "the states we serve"


def test_greeting_follows_brand(api, settings_env):
    settings_env(CLAIMDESK_BRAND_NAME="Harbor Mutual", CLAIMDESK_AGENT_NAME="Sam")
    state = api.post("/api/intakes", headers=iap()).json()["state"]
    assert state["transcript"][0]["text"].startswith("Hi, this is Sam from Harbor Mutual flood claims.")
