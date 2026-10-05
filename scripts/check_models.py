"""Phase 0: confirm the three Gemini models are reachable from YOUR project.

WHY RUN THIS FIRST?
    Model availability differs by project and endpoint location. Before you
    build or deploy anything, this script makes one tiny, cheap call to each
    model at each candidate location and prints a table like:

        model                     location      result
        gemini-3.8-flash          global        OK   (412 ms)
        gemini-3.8-flash          us-central1   OK   (380 ms)
        gemini-3.1-flash-image    global        OK   (2.1 s)
        gemini-3.8-live           global        OK   (connect 700 ms)
        gemini-3.8-live           us-central1   OK   (connect 650 ms)

    Policy for this project: models use the ``global`` endpoint; every GCP
    resource lives in ``us-central1``. If the Live model is NOT available on
    ``global`` for your project, the app automatically retries once on
    ``LIVE_MODEL_FALLBACK_LOCATION`` (us-central1) - this table tells you in
    advance which of the two will be used.

    The model NAMES are fixed by requirement; this script only tests
    *locations*. It never changes a model.

COST
    A few hundred tokens + one tiny image generation: well under US$0.05.

RUN
    gcloud auth application-default login
    uv run --no-sync python scripts/check_models.py
    uv run --no-sync python scripts/check_models.py --skip-image   # cheaper
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from claimdesk.settings import get_settings  # noqa: E402

LOCATIONS = ("global", "us-central1")


def _client(project: str, location: str) -> genai.Client:
    # vertexai=True -> Vertex AI endpoint + Application Default Credentials.
    return genai.Client(vertexai=True, project=project, location=location)


def check_text(project: str, location: str, model: str) -> str:
    started = time.monotonic()
    response = _client(project, location).models.generate_content(
        model=model,
        contents="Reply with the single word: ready",
        config=types.GenerateContentConfig(max_output_tokens=10, temperature=0),
    )
    elapsed = int((time.monotonic() - started) * 1000)
    return f"OK   ({elapsed} ms) -> {(response.text or '').strip()[:20]!r}"


def check_image(project: str, location: str, model: str) -> str:
    started = time.monotonic()
    response = _client(project, location).models.generate_content(
        model=model,
        contents="A simple black line drawing of a house on white background.",
        config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"]),
    )
    parts = response.candidates[0].content.parts if response.candidates else []
    has_image = any(getattr(p, "inline_data", None) for p in parts or [])
    elapsed = time.monotonic() - started
    return f"{'OK  ' if has_image else 'WARN'} ({elapsed:.1f} s){'' if has_image else ' no image returned'}"


async def check_live(project: str, location: str, model: str, voice: str) -> str:
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
    )
    started = time.monotonic()
    async with _client(project, location).aio.live.connect(model=model, config=config):
        elapsed = int((time.monotonic() - started) * 1000)
    return f"OK   (connect {elapsed} ms)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-image", action="store_true", help="skip the image model (slowest/most expensive check)")
    parser.add_argument("--skip-live", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.project_id:
        print("Set GOOGLE_CLOUD_PROJECT (in .env or your shell) first.", file=sys.stderr)
        return 2

    print(f"Project: {settings.project_id}\n")
    print(f"{'model':<26}{'location':<14}result")
    failures = 0
    checks: list[tuple[str, str, object]] = []
    for location in LOCATIONS:
        checks.append((settings.reasoning_model, location, lambda loc=location: check_text(settings.project_id, loc, settings.reasoning_model)))
        if not args.skip_image:
            checks.append((settings.sketch_model, location, lambda loc=location: check_image(settings.project_id, loc, settings.sketch_model)))
        if not args.skip_live:
            checks.append(
                (settings.live_model, location, lambda loc=location: asyncio.run(check_live(settings.project_id, loc, settings.live_model, settings.voice_name)))
            )

    for model, location, run in checks:
        try:
            result = run()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - this is a diagnostic script; show every failure
            failures += 1
            result = f"FAIL {type(exc).__name__}: {str(exc)[:120]}"
        print(f"{model:<26}{location:<14}{result}")

    print(
        "\nExpected .env (see .env.example):\n"
        "  GOOGLE_CLOUD_LOCATION=global          (flash + image)\n"
        "  LIVE_MODEL_LOCATION=global            (live; app falls back to LIVE_MODEL_FALLBACK_LOCATION)\n"
        "  LIVE_MODEL_FALLBACK_LOCATION=us-central1\n"
        "Common failures: PermissionDenied -> enable aiplatform.googleapis.com / grant roles/aiplatform.user;\n"
        "NotFound -> model not offered at that location for your project."
    )
    return 1 if failures == len(checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
