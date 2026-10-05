"""The bridge between the claimant's browser and Gemini Live.

PICTURE IT LIKE THIS::

    browser  <--WebSocket /ws/live-->  this server  <--Live API-->  gemini-3.8-live
     mic PCM16 16 kHz  ------------------>  send_realtime_input(audio)
     camera JPEG ~1 fps ----------------->  send_realtime_input(video)
     typed text ------------------------->  send_client_content(turns)
     speaker PCM 24 kHz  <----------------  server_content.model_turn (audio)
     live transcript     <----------------  input/output transcription
                                            tool_call -> tool_handlers.py

    Two asyncio tasks run side by side for the whole call:
    ``_browser_to_gemini`` and ``_gemini_to_browser``. Tools run as extra
    background tasks so a slow BigQuery query or pipeline run never stops
    audio from flowing in either direction.

VERTEX AI, NOT API KEYS
    ``genai.Client(vertexai=True, project=..., location=<location>)``
    authenticates with Application Default Credentials (the Cloud Run service
    account needs ``roles/aiplatform.user``). The model name comes from
    settings (``gemini-3.8-live``) and is never hard-coded.

LOCATION FALLBACK (``open_live_session``)
    We prefer the ``global`` endpoint (best availability), but a Live model is
    not offered on ``global`` for every project. ``settings.live_locations``
    lists where to try, in order - by default ``("global", "us-central1")``.
    If ``global`` rejects the connection with a "this model/location is not
    available to you" style error (404 not found, 400 invalid argument /
    unsupported, 403 permission), we log ONE structured warning and retry on
    the next location. Whichever location worked is cached in the module
    (``_working_location``) so later calls go straight there and do not pay
    for a failed handshake every time. Other errors (network, quota...) are
    NOT retried elsewhere: a different region would not fix them.

RECONNECTS
    There are two kinds, and they are handled differently:

    1. **Gemini Live reconnects, invisible to the browser.** Vertex AI limits
       every Live *session* and *connection*:

       * without context window compression an audio-only session ends after
         about 15 minutes and an audio + camera session after only about
         2 minutes (``voice_tools.build_live_config`` turns on a sliding
         window, which removes that limit);
       * one WebSocket *connection* lasts only about 10 minutes. Shortly
         before closing it the server sends ``go_away`` (with ``time_left``).

       With ``session_resumption`` enabled, the server keeps sending
       ``session_resumption_update`` messages; we keep the newest resumable
       ``new_handle`` on the intake (memory only). On ``go_away`` - or if the
       server connection drops unexpectedly after we have a handle - the
       bridge opens a NEW Live connection with that handle and carries on.
       The browser WebSocket stays open, nothing is replayed (the server
       restores the conversation from the handle) and the page only shows a
       short "Reconnecting…" status (``{"type": "status", "code":
       "reconnecting"}`` then ``"resumed"``). Attempts are capped
       (``MAX_LIVE_RECONNECTS``) and the call's overall
       ``CLAIMDESK_LIVE_SESSION_MINUTES`` limit (20 by default) still applies.
       If a handle is refused, the next attempt starts a fresh session and
       replays the transcript instead (see 2).

    2. **Browser reconnects.** The network can drop at any time, and a call
       ends at the time limit. The transcript lives on the intake, so when the
       browser reconnects we start a fresh Live session and replay the
       dialogue into it as context.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
import uuid
from collections import deque
from functools import lru_cache
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect
from google.genai import errors as genai_errors
from google.genai import types
from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed  # google-genai's transport

from claimdesk.errors import ClaimDeskError, LimitExceededError, ModelCallError
from claimdesk.observability import bind_intake, get_logger
from claimdesk.settings import get_settings

from . import tool_handlers
from .error_logging import log_failure
from .intake_session import IntakeRegistry, LiveIntake, append_turn
from .voice_tools import GUIDANCE_TOOL, active_tool_names, build_live_config, camera_state_notice, response_scheduling, summarize_for_voice, tool_headline

log = get_logger(__name__)

MAX_PARALLEL_TOOLS = 4
# Per-kind input rate limits: (window seconds, max messages per window).
RATE_LIMITS = {"text": (60, 20), "audio": (1, 100), "video": (1, 5), "camera_state": (1, 5)}
MAX_AUDIO_CHUNK = 128_000
MAX_VIDEO_FRAME = 512_000

# --- Gemini Live reconnects (see "RECONNECTS" above) -------------------------
# At most this many reconnects in a row. A connection that stayed up for
# HEALTHY_CONNECTION_SECONDS resets the count, so the planned ~10-minute
# ``go_away`` never uses up the budget, but a server that keeps dropping us
# right away ends the call instead of looping.
MAX_LIVE_RECONNECTS = 3
HEALTHY_CONNECTION_SECONDS = 60.0
# How long a typed message / camera switch waits for a reconnect to finish.
RECONNECT_WAIT_SECONDS = 20.0
# Mic audio and camera frames are real-time: during a reconnect we drop them
# (a stale half-second of audio is useless) instead of queueing them up.
DROPPABLE_INPUTS = frozenset({"audio", "video"})
# Why a Live connection ended (return values of ``_serve_connection``).
GO_AWAY = "go_away"
CLIENT_CLOSED = "client_closed"
# Errors that mean "the Live connection dropped". ``genai`` turns server-side
# WebSocket closes into ``APIError``; the others are raw transport failures.
LIVE_DROP_ERRORS: tuple[type[BaseException], ...] = (genai_errors.APIError, WebSocketConnectionClosed, ConnectionError, OSError)

# Fixed, user-safe replies. We never echo raw exception text to the browser:
# it can contain internals (parser details, fragments of the input...).
UNREADABLE_MESSAGE = "That message couldn't be read. Please try again."
LIVE_BUSY_MESSAGE = "The line is reconnecting, so that didn't reach the agent. Please send it again in a moment."
TOOL_CRASH_MESSAGE = "That step didn't work just now. Let's keep going, and I can try it again in a moment."


class BadMessage(ValueError):
    """A browser message we reject; ``str(exc)`` is safe to show the claimant."""


class LiveUnavailable(Exception):
    """A browser message could not be delivered because Gemini Live is reconnecting."""


# The Live location that last connected successfully (None = not known yet).
_working_location: str | None = None

# HTTP-ish status codes / phrases that mean "wrong place for this model",
# i.e. worth one retry on the fallback location.
_LOCATION_REJECTION_CODES = {400, 403, 404}
_LOCATION_REJECTION_WORDS = ("not found", "not_found", "unsupported", "not supported", "invalid argument", "invalid_argument", "permission", "1007", "1008")


@lru_cache(maxsize=4)
def get_live_client(location: str | None = None):
    """One Vertex AI client per Live location (cached; clients are reusable)."""

    from google import genai

    settings = get_settings()
    return genai.Client(vertexai=True, project=settings.project_id, location=location or settings.live_model_location)


def is_location_rejection(exc: BaseException) -> bool:
    """True if ``exc`` looks like "this model is not served at this location".

    The Live API connects over a WebSocket, so a rejection can surface either
    as a ``google.genai.errors.APIError`` with an HTTP-style ``code`` or as a
    websocket close/handshake error whose text carries the reason. We check
    both, conservatively.
    """

    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _LOCATION_REJECTION_CODES:
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(word in text for word in _LOCATION_REJECTION_WORDS)


def live_location_order() -> list[str]:
    """Locations to try: the cached working one first, then the rest from settings."""

    ordered = list(get_settings().live_locations)
    if _working_location:
        ordered = [_working_location] + [loc for loc in ordered if loc != _working_location]
    return ordered


def remember_live_location(location: str | None) -> None:
    """Set (or with None, forget) the cached working location."""

    global _working_location
    _working_location = location


@contextlib.asynccontextmanager
async def open_live_session(model: str, config: Any, *, locations: list[str] | None = None) -> AsyncIterator[tuple[Any, str]]:
    """Connect to Gemini Live, falling back across ``settings.live_locations``.

    Yields ``(live_session, location)``. Raises the last connection error if
    every location fails (the caller's boundary logs it once).

    ``locations`` pins the candidates. The bridge uses it when *resuming*: a
    resumption handle belongs to the endpoint that issued it, and a refused
    handle (400) must not be mistaken for "model not offered here" and flip
    the cached location. Fresh sessions keep the normal fallback order.
    """

    locations = list(locations) if locations else live_location_order()
    async with contextlib.AsyncExitStack() as stack:
        session, used = None, None
        for index, location in enumerate(locations):
            try:
                session = await stack.enter_async_context(get_live_client(location).aio.live.connect(model=model, config=config))
            except Exception as exc:
                has_next = index + 1 < len(locations)
                if not has_next or not is_location_rejection(exc):
                    raise
                log.warning(
                    "Live model rejected at location; trying fallback",
                    extra={
                        "json_fields": {
                            "location": location,
                            "fallback_location": locations[index + 1],
                            "error_type": type(exc).__name__,
                            "model": model,
                        }
                    },
                )
                if _working_location == location:
                    remember_live_location(None)  # the cached choice stopped working
                continue
            used = location
            break
        if used != _working_location:
            log.info("Live model location selected", extra={"json_fields": {"location": used, "model": model}})
        remember_live_location(used)
        yield session, used


class LiveCallBridge:
    """State and coroutines for ONE open call (one browser WebSocket)."""

    def __init__(self, websocket: WebSocket, intake: LiveIntake, registry: IntakeRegistry) -> None:
        self.ws = websocket
        self.intake = intake
        self.registry = registry
        self.settings = get_settings()
        self.send_lock = asyncio.Lock()  # one writer at a time on the socket
        self.tasks: set[asyncio.Task] = set()
        self.tool_tasks: dict[str, asyncio.Task] = {}
        self.update_task: asyncio.Task | None = None
        # Streaming transcription arrives in fragments; we build each turn up
        # here and "finalize" it into the transcript when the speaker is done.
        self.pending = {"Claimant": self._fresh_turn(), "Agent": self._fresh_turn()}
        # The Gemini Live connection in use right now. It is replaced when we
        # reconnect (see "RECONNECTS"), while the browser pump keeps running,
        # so browser input always goes to ``self.live_session`` - never to a
        # connection captured when the call started. None while reconnecting.
        self.live_session: Any = None
        self.live_ready = asyncio.Event()  # set while ``live_session`` is usable
        self.browser_pump: asyncio.Task | None = None  # started once per call
        self._attached = False  # did the current connection attempt get as far as serving?
        self._send_failure_logged = False

    # --- helpers ---------------------------------------------------------------
    @staticmethod
    def _fresh_turn() -> dict[str, str]:
        return {"id": uuid.uuid4().hex, "text": ""}

    async def send(self, payload: dict[str, Any]) -> None:
        if self.intake.deleted:
            return
        async with self.send_lock:
            await self.ws.send_json({**payload, "intake_id": self.intake.intake_id})

    def track(self, task: asyncio.Task, *, report: bool = True) -> asyncio.Task:
        """Remember a task; ``report=False`` for tasks whose errors are
        re-raised elsewhere (so each failure is logged exactly once)."""

        self.tasks.add(task)
        self.intake.track(task)

        def _done(done: asyncio.Task) -> None:
            self.tasks.discard(done)
            if report and not done.cancelled() and done.exception() is not None:
                error = done.exception()
                log_failure(log, "Background call task failed", error)

        task.add_done_callback(_done)
        return task

    # --- the current Live connection ---------------------------------------------
    def _attach(self, live_session: Any) -> None:
        self.live_session = live_session
        self._attached = True
        self._send_failure_logged = False
        self.live_ready.set()

    def _detach(self, live_session: Any) -> None:
        if self.live_session is live_session:
            self.live_session = None
            self.live_ready.clear()

    def _remember_handle(self, update: Any) -> None:
        """Keep the newest *resumable* session handle (memory only).

        While the model is mid-generation or running tools the server sends
        ``resumable=False`` with an empty handle; resuming "now" would lose
        data, so we keep the previous handle until a new resumable one comes.
        """

        if getattr(update, "resumable", False) and getattr(update, "new_handle", None):
            self.intake.resumption_handle = update.new_handle

    async def _forward(self, kind: str, send: Callable[[Any], Awaitable[Any]]) -> None:
        """Deliver one browser input to the *current* Live session.

        During a Live reconnect (``live_session`` is None) real-time media is
        dropped, and typed text / camera switches wait up to
        ``RECONNECT_WAIT_SECONDS`` for the new connection. A send that fails
        because the connection is closing is NOT fatal here: the receive side
        (``_gemini_to_browser``) sees the same drop and decides whether to
        reconnect or end the call, so each failure is handled in one place.
        Raises ``LiveUnavailable`` if a non-droppable input still could not
        be delivered.
        """

        for _attempt in range(2):
            live = self.live_session
            if live is None:
                if kind in DROPPABLE_INPUTS:
                    return
                try:
                    await asyncio.wait_for(self.live_ready.wait(), RECONNECT_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    break
                live = self.live_session
                if live is None:
                    continue
            try:
                await send(live)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self._send_failure_logged:  # once per connection, not per audio chunk
                    self._send_failure_logged = True
                    log.warning("Sending to Gemini Live failed; waiting for the receive side to reconnect or end the call", exc_info=True)
                if kind in DROPPABLE_INPUTS:
                    return
                await asyncio.sleep(0.2)  # let the receive side notice the drop and detach
        raise LiveUnavailable(kind)

    async def _answer_tool(self, origin: Any, response: types.FunctionResponse) -> None:
        """Send a tool result to the Live connection that asked for it.

        If Gemini Live has reconnected since the call was made, the new
        connection never saw that ``tool_call``; answering an unknown call id
        could make the server drop the session. The result is still on the
        claim and in the UI, so we log it and skip the model reply.
        """

        current = self.live_session
        if current is not None and current is not origin:
            log.info("Tool finished after Gemini Live reconnected; result kept on the claim only", extra={"json_fields": {"tool": response.name}})
            return
        try:
            await origin.send_tool_response(function_responses=[response])
        except asyncio.CancelledError:
            raise
        except Exception:  # the connection is closing; the receive side handles the drop
            log.warning("Could not deliver a tool result to Gemini Live", exc_info=True, extra={"json_fields": {"tool": response.name}})

    # --- claim pipeline --------------------------------------------------------
    async def _update(self) -> dict[str, Any] | None:
        await self.send({"type": "processing", "active": True})
        try:
            result = await self.registry.refresh_pipeline(self.intake)
            await self.send({"type": "state", "state": self.intake.state()})
            return result
        except asyncio.CancelledError:
            raise
        except ClaimDeskError as exc:
            log_failure(log, "Claim pipeline update failed", exc)  # boundary: log once
            await self.send({"type": "error", "message": f"{exc.user_message} Your conversation is kept; ask the agent to update the file again."})
            return None
        finally:
            with contextlib.suppress(Exception):
                await self.send({"type": "processing", "active": False})

    def request_update(self) -> asyncio.Task:
        """Start a pipeline run unless one is already running (it will pick up
        the newest revision by itself, see ``IntakeRegistry.refresh_pipeline``)."""

        if self.update_task is None or self.update_task.done():
            self.update_task = self.track(asyncio.create_task(self._update()))
        return self.update_task

    async def finalize(self, speaker: str) -> None:
        turn = self.pending[speaker]
        if not turn["text"].strip():
            return
        append_turn(self.intake.record, speaker, turn["text"], turn["id"])
        self.registry.tracer.record(
            self.intake.intake_id, "claimant_turn" if speaker == "Claimant" else "agent_turn", role=speaker.lower(), text=turn["text"]
        )
        await self.send({"type": "transcript", "speaker": speaker, "text": turn["text"], "id": turn["id"], "final": True})
        self.pending[speaker] = self._fresh_turn()
        self.registry.persist_soon(self.intake)
        if speaker == "Claimant":
            self.request_update()

    # --- tools -----------------------------------------------------------------
    async def publish_tool(self, entry: dict[str, Any]) -> None:
        record = self.intake.record
        record.tool_activity = [item for item in record.tool_activity if item["id"] != entry["id"]] + [dict(entry)]
        record.tool_activity = record.tool_activity[-30:]
        await self.send({"type": "tool", **entry})

    async def _dispatch(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Run one tool; returns (result, urgent)."""

        if name == "find_policy":
            return await tool_handlers.find_policy(self.intake, self.registry, args)
        if name == "refresh_intake_packet":
            await self.finalize("Claimant")
            result = await asyncio.shield(self.request_update())
            if not result:
                return {"error": "The claim update failed. Keep talking and try refresh_intake_packet again."}, False
            summary = summarize_for_voice(result)
            return summary, bool(summary["safety_escalation"])
        if name == "capture_evidence_photo":
            result = await tool_handlers.capture_evidence_photo(self.intake, self.registry, args)
            if result.get("pinned"):
                self.request_update()
            return result, False
        if name == "render_damage_sketch":
            return await tool_handlers.render_damage_sketch(self.intake, self.registry, args), False
        if name == GUIDANCE_TOOL and self.settings.enable_guidance_search:
            return await tool_handlers.lookup_flood_guidance(self.intake, self.registry, args), False
        return {"error": f"Unknown tool {name}"}, False

    async def execute_tool(self, call: types.FunctionCall, live_session: Any) -> None:
        started = time.monotonic()
        name, args, call_id = str(call.name or ""), dict(call.args or {}), str(call.id or uuid.uuid4().hex)
        entry = {"id": call_id, "name": name, "args": args, "phase": "running", "headline": tool_headline(name, args, None), "model": self.settings.live_model}
        await self.publish_tool(entry)
        self.registry.tracer.record(self.intake.intake_id, "tool_call", role="agent", tool_name=name, tool_args=args)
        urgent = False
        try:
            result, urgent = await self._dispatch(name, args)
        except asyncio.CancelledError:
            entry.update(phase="cancelled", headline="Cancelled")
            with contextlib.suppress(Exception):
                await self.publish_tool(entry)
            raise
        except ClaimDeskError as exc:
            # Boundary for tool failures: log once (-> Error Reporting), then
            # give the model something it can say out loud.
            log_failure(log, "Tool failed", exc, extra={"json_fields": {"tool": name}})
            result = {"error": f"{name} failed", "say_to_claimant": exc.user_message, "retryable": exc.retryable}
        except Exception:
            # Last-resort boundary for a bug or an unexpected library error.
            # WHY: without an answer the model waits for this result forever
            # and the page shows the step "running" forever. So: log once,
            # then reply with a fixed, user-safe message (never the raw
            # exception text) and mark the step as failed ("error" phase).
            log.exception("Tool crashed unexpectedly", extra={"json_fields": {"tool": name}})
            result = {"error": f"{name} failed", "say_to_claimant": TOOL_CRASH_MESSAGE, "retryable": True}
        scheduling = response_scheduling(urgent=urgent)
        response = types.FunctionResponse(id=call_id, name=name, response=result, scheduling=scheduling)
        try:
            entry.update(
                phase="error" if "error" in result else "done",
                headline=result.get("say_to_claimant") or result.get("error") or tool_headline(name, args, result),
                duration_ms=int((time.monotonic() - started) * 1000),
                result=result,
                scheduling=scheduling.value,
            )
            await self.publish_tool(entry)
            self.registry.tracer.record(self.intake.intake_id, "tool_result", role="system", tool_name=name, tool_result=result)
            self.registry.persist_soon(self.intake)
            await self.send({"type": "state", "state": self.intake.state()})
        finally:
            # Answer the model even if updating the page failed.
            await self._answer_tool(live_session, response)

    async def launch_tool(self, call: types.FunctionCall, live_session: Any) -> None:
        key = str(call.id)
        if key in self.tool_tasks:
            return
        if len(self.tool_tasks) >= MAX_PARALLEL_TOOLS:
            await self._answer_tool(
                live_session, types.FunctionResponse(id=call.id, name=call.name, response={"error": "The team is busy. Wait for current tools to finish."})
            )
            return
        task = self.track(asyncio.create_task(self.execute_tool(call, live_session)))
        self.tool_tasks[key] = task
        task.add_done_callback(lambda _done, k=key: self.tool_tasks.pop(k, None))

    # --- the two pumps -------------------------------------------------------
    async def _camera_notice(self, live_session: Any, enabled: bool) -> None:
        # Context only (turn_complete=False): the model learns the mode without
        # being prompted to reply, and it never enters the claimant transcript.
        await live_session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=camera_state_notice(enabled))]), turn_complete=False
        )

    async def _browser_to_gemini(self) -> None:
        """Browser -> Gemini. Runs ONCE per call, across Live reconnects."""

        windows: dict[str, deque] = {kind: deque() for kind in RATE_LIMITS}
        binary_warned = False
        while True:
            frame = await self.ws.receive()
            if frame.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(frame.get("code") or 1000)
            raw = frame.get("text")
            if raw is None:
                # Our protocol is JSON text only (media is base64 inside it).
                # A stray binary frame is ignored rather than ending the call.
                if not binary_warned:
                    binary_warned = True
                    log.warning("Ignoring binary WebSocket frame from browser")
                continue
            try:
                if len(raw) > self.settings.max_message_bytes:
                    raise LimitExceededError("websocket message too large", user_message="That message was too large to send.")
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise BadMessage("Expected a JSON object")
                kind = message.get("type")
                if kind == "close":
                    await self.finalize("Claimant")
                    await self.finalize("Agent")
                    return
                if kind not in windows:
                    raise BadMessage("Unknown input type")
                now = time.monotonic()
                period, limit = RATE_LIMITS[kind]
                window = windows[kind]
                while window and now - window[0] >= period:
                    window.popleft()
                if len(window) >= limit:
                    raise BadMessage("Input rate limit reached; pause and try again")
                window.append(now)
                self.intake.record.touch()

                if kind == "camera_state":
                    enabled = message.get("enabled")
                    if not isinstance(enabled, bool):
                        raise BadMessage("Camera state must be true or false")
                    if self.intake.set_camera_mode(enabled):
                        await self._forward(kind, lambda live, on=enabled: self._camera_notice(live, on))
                elif kind == "text":
                    await self._handle_text(message)
                else:
                    await self._handle_media(kind, message, now)
            except LimitExceededError as exc:
                await self.send({"type": "error", "message": exc.user_message})
            except BadMessage as exc:  # our own wording: safe to show
                await self.send({"type": "error", "message": str(exc)})
            except LiveUnavailable:
                await self.send({"type": "error", "message": LIVE_BUSY_MESSAGE})
            except (ValueError, TypeError):  # e.g. broken JSON / base64: never echo parser text
                log.warning("Unreadable message from browser", exc_info=True)
                await self.send({"type": "error", "message": UNREADABLE_MESSAGE})

    async def _handle_text(self, message: dict[str, Any]) -> None:
        text = message.get("text", "")
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise BadMessage("Text must contain 1-8000 characters")
        turn_id = message.get("id") or uuid.uuid4().hex
        if not isinstance(turn_id, str) or len(turn_id) > 100:
            raise BadMessage("Invalid turn identifier")
        if any(t.get("id") == turn_id for t in self.intake.record.transcript):
            return  # browser retry of a turn we already have
        append_turn(self.intake.record, "Claimant", text, turn_id)
        self.registry.tracer.record(self.intake.intake_id, "claimant_turn", role="claimant", text=text)
        self.registry.persist_soon(self.intake)
        await self.send({"type": "transcript", "speaker": "Claimant", "text": text, "id": turn_id, "final": True})
        self.request_update()
        turn = types.Content(role="user", parts=[types.Part(text=text)])
        await self._forward("text", lambda live: live.send_client_content(turns=turn, turn_complete=True))

    async def _handle_media(self, kind: str, message: dict[str, Any], now: float) -> None:
        encoded = message.get("data")
        if not isinstance(encoded, str):
            raise BadMessage("Missing media data")
        data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > (MAX_VIDEO_FRAME if kind == "video" else MAX_AUDIO_CHUNK):
            raise BadMessage("Invalid media size")
        if kind == "video":
            if not data.startswith(b"\xff\xd8\xff"):
                raise BadMessage("Camera frames must be JPEG images")
            if self.intake.set_camera_mode(True):
                await self._forward("camera_state", lambda live: self._camera_notice(live, True))
            # Kept even while Live reconnects: capture_evidence_photo uses it.
            self.intake.last_frame, self.intake.last_frame_at, self.intake.last_frame_id = data, now, uuid.uuid4().hex
            blob = types.Blob(data=data, mime_type="image/jpeg")
            await self._forward(kind, lambda live: live.send_realtime_input(video=blob))
        else:
            if len(data) % 2:
                raise BadMessage("Audio must be 16-bit PCM")
            blob = types.Blob(data=data, mime_type="audio/pcm;rate=16000")
            await self._forward(kind, lambda live: live.send_realtime_input(audio=blob))

    async def _gemini_to_browser(self, live_session: Any) -> str:
        """Gemini -> browser for ONE Live connection.

        Returns ``GO_AWAY`` when the server announces it will close this
        connection soon; raises if the connection drops. ``run`` decides what
        happens next (reconnect or end the call).
        """

        while True:  # receive() ends after each model turn; loop for the next
            async for response in live_session.receive():
                update = getattr(response, "session_resumption_update", None)
                if update:
                    self._remember_handle(update)
                go_away = getattr(response, "go_away", None)
                if go_away:
                    # The server closes this connection after ``time_left``.
                    # Reconnect now, while we still control the timing.
                    log.info("Gemini Live sent go_away", extra={"json_fields": {"time_left": go_away.time_left, "resumable": bool(self.intake.resumption_handle)}})
                    return GO_AWAY
                if response.tool_call and response.tool_call.function_calls:
                    await self.finalize("Claimant")
                    for call in response.tool_call.function_calls:
                        await self.launch_tool(call, live_session)
                if response.tool_call_cancellation:
                    for call_id in response.tool_call_cancellation.ids or []:
                        task = self.tool_tasks.get(str(call_id))
                        if task:
                            task.cancel()
                content = response.server_content
                if not content:
                    continue
                for speaker, chunk in (("Claimant", content.input_transcription), ("Agent", content.output_transcription)):
                    if chunk and chunk.text:
                        if speaker == "Agent":
                            await self.finalize("Claimant")
                        if chunk.text != self.pending[speaker]["text"] or speaker == "Claimant":
                            self.pending[speaker]["text"] += chunk.text
                        await self.send({"type": "transcript", "speaker": speaker, **self.pending[speaker], "final": False})
                    if speaker == "Claimant" and chunk and getattr(chunk, "finished", False):
                        await self.finalize(speaker)
                if content.model_turn:
                    await self.finalize("Claimant")
                    for part in content.model_turn.parts or []:
                        if part.inline_data and isinstance(part.inline_data.data, bytes):
                            await self.send({"type": "audio", "data": base64.b64encode(part.inline_data.data).decode("ascii"), "mime_type": part.inline_data.mime_type})
                if content.interrupted:
                    await self.finalize("Agent")
                    await self.send({"type": "interrupted"})
                if getattr(content, "turn_complete", False):
                    await self.finalize("Claimant")
                    await self.finalize("Agent")

    # --- main entry ----------------------------------------------------------
    async def run(self) -> None:
        intake, settings = self.intake, self.settings
        bind_intake(intake.intake_id)
        intake.live_socket = self.ws
        intake.live_model = settings.live_model
        intake.notify = self.send
        # A resumption handle is only reused *inside* one browser call. A new
        # browser connection always starts a fresh Live session and replays
        # the transcript (see "RECONNECTS" 2), so an old handle never matters.
        intake.resumption_handle = None
        self.registry.tracer.record(intake.intake_id, "system", role="system", text="live call started")
        try:
            await self._run_connections()
        except WebSocketDisconnect:
            pass
        except LimitExceededError as exc:
            log.warning("Live call limit reached", extra={"json_fields": {"reason": str(exc)}})
            with contextlib.suppress(Exception):
                # ``code`` is an additive, machine-readable hint so the UI can
                # show a friendly state ("call ended at the time limit").
                await self.send({"type": "error", "code": "time_limit", "message": exc.user_message})
        except (genai_errors.APIError, ModelCallError, ConnectionError, OSError) as exc:
            log_failure(log, "Gemini Live session failed", exc, extra={"json_fields": {"error_type": type(exc).__name__}})
            with contextlib.suppress(Exception):
                await self.send(
                    {
                        "type": "error",
                        "code": "model_unavailable",
                        "message": "The voice agent is temporarily unavailable. Your claim is saved; try reconnecting in a moment.",
                    }
                )
        except Exception:  # last-resort boundary: never leave the socket half-open
            log.exception("Unexpected error in live call")
            with contextlib.suppress(Exception):
                await self.send({"type": "error", "code": "connection_lost", "message": "Live connection ended. Reconnect to continue this intake."})
        finally:
            await self._cleanup()

    def _time_limit_error(self) -> LimitExceededError:
        minutes = self.settings.live_session_minutes
        return LimitExceededError(
            "live call time limit",
            user_message=f"Calls are limited to {minutes} minutes. Your claim is saved; reconnect to continue.",
        )

    async def _run_connections(self) -> None:
        """Keep ONE browser call connected to Gemini Live until it ends.

        Each loop iteration is one Live *connection* (``_serve_connection``).
        When a connection ends with ``go_away``, or drops after the server
        gave us a resumption handle, we open the next one - resuming from the
        handle when we have it - without touching the browser WebSocket.
        Everything else ends the call through ``run``'s error handling.
        """

        intake, loop = self.intake, asyncio.get_running_loop()
        deadline = loop.time() + self.settings.live_session_minutes * 60  # one limit for the whole call
        attempts = 0
        while True:
            resuming = intake.resumption_handle is not None
            started = loop.time()
            self._attached = False
            last_error: BaseException | None = None
            try:
                outcome = await self._serve_connection(deadline, resuming=resuming)
            except LIVE_DROP_ERRORS as exc:
                if self.browser_pump is None:
                    raise  # the call never got going: the claimant sees "temporarily unavailable"
                if self._attached and not intake.resumption_handle:
                    raise  # dropped mid-call before the server sent a handle: nothing to resume from
                if not self._attached and resuming:
                    # The handle was refused (expired / unknown). Next attempt
                    # starts a fresh session and replays the transcript.
                    intake.resumption_handle = None
                outcome, last_error = f"dropped:{type(exc).__name__}", exc
            if outcome == CLIENT_CLOSED:
                return
            if self._attached and loop.time() - started >= HEALTHY_CONNECTION_SECONDS:
                attempts = 0  # that connection was fine; this is a routine reconnect
            attempts += 1
            if attempts > MAX_LIVE_RECONNECTS:
                raise ModelCallError(f"Gemini Live reconnect limit ({MAX_LIVE_RECONNECTS}) reached") from last_error
            if loop.time() >= deadline:
                raise self._time_limit_error()
            # Close out half-finished caption turns from the old connection.
            await self.finalize("Claimant")
            await self.finalize("Agent")
            log.warning(
                "Reconnecting to Gemini Live",
                extra={"json_fields": {"reason": outcome, "attempt": attempts, "resume_with_handle": bool(intake.resumption_handle)}},
            )
            # A status, not an error: the page shows "Reconnecting…" and keeps
            # the call (mic, camera, clock) running.
            await self.send({"type": "status", "code": "reconnecting", "attempt": attempts})
            if attempts > 1:
                await asyncio.sleep(min(2.0, 0.5 * (attempts - 1)))  # brief back-off for repeated failures

    async def _serve_connection(self, deadline: float, *, resuming: bool) -> str:
        """Open one Live connection and pump until it (or the call) ends.

        Returns ``GO_AWAY`` or ``CLIENT_CLOSED``; raises on a dropped
        connection, a browser disconnect or the call time limit.
        """

        intake, settings = self.intake, self.settings
        loop = asyncio.get_running_loop()
        config = build_live_config(settings, camera_enabled=intake.camera_enabled, resumption_handle=intake.resumption_handle)
        # Resume at the same location that issued the handle (see open_live_session).
        pinned = [intake.live_location] if resuming and intake.live_location else None
        async with open_live_session(settings.live_model, config, locations=pinned) as (live_session, location):
            intake.live_location = location
            if not resuming:
                # Fresh session (first connect, or a refused handle): replay
                # the dialogue so the model remembers the call. A resumed
                # session already has it, so nothing is replayed then.
                await self._replay_transcript(live_session)
            self._attach(live_session)
            receiver: asyncio.Task | None = None
            try:
                if self.browser_pump is None:
                    await self.send({"type": "session", "model": settings.live_model, "sketch_model": settings.sketch_model, "tools": active_tool_names(settings), "location": location})
                    await self.send({"type": "state", "state": intake.state()})
                    await self.send({"type": "ready"})
                    self.browser_pump = self.track(asyncio.create_task(self._browser_to_gemini()), report=False)
                else:
                    await self.send({"type": "status", "code": "resumed", "location": location})
                receiver = self.track(asyncio.create_task(self._gemini_to_browser(live_session)), report=False)
                done, _ = await asyncio.wait({self.browser_pump, receiver}, timeout=max(0.0, deadline - loop.time()), return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise self._time_limit_error()
                if self.browser_pump in done:
                    self.browser_pump.result()  # re-raise WebSocketDisconnect etc., if any
                    return CLIENT_CLOSED
                return receiver.result()  # GO_AWAY, or re-raise the drop
            finally:
                self._detach(live_session)
                if receiver is not None and not receiver.done():
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)

    async def _replay_transcript(self, live_session: Any) -> None:
        transcript = self.intake.record.transcript
        if any(t["speaker"] == "Claimant" for t in transcript):
            history = [
                types.Content(role="user" if t["speaker"] == "Claimant" else "model", parts=[types.Part(text=t["text"])])
                for t in transcript
                if t["speaker"] in {"Claimant", "Agent"}
            ]
            await live_session.send_client_content(turns=history, turn_complete=False)

    async def _cleanup(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        intake = self.intake
        for item in intake.record.tool_activity:
            if item["phase"] == "running":
                item.update(phase="cancelled", headline="Connection ended")
        intake.live_socket = None
        intake.notify = None
        intake.set_camera_mode(False)
        intake.record.touch()
        self.registry.tracer.record(intake.intake_id, "system", role="system", text="live call ended")
        if not intake.deleted:
            self.registry.persist_soon(intake)
        with contextlib.suppress(Exception):
            await self.ws.close()


async def run_live_call(websocket: WebSocket, intake: LiveIntake, registry: IntakeRegistry) -> None:
    """Entry point used by ``webapp.main``. The socket must already be accepted."""

    await LiveCallBridge(websocket, intake, registry).run()


__all__ = ["LiveCallBridge", "get_live_client", "is_location_rejection", "open_live_session", "run_live_call"]
