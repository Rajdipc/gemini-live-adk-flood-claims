"""Tests for the single-config-file tooling (.env -> app settings / Cloud Run YAML).

Why test config? A typo here silently deploys the wrong region or leaks a
deploy-only value into the running service. These checks keep the region
policy honest: every GCP resource in us-central1, models on "global".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from claimdesk import settings as settings_module

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"


def _load_renderer():
    spec = importlib.util.spec_from_file_location("render_env_yaml", ROOT / "deploy" / "render_env_yaml.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


renderer = _load_renderer()


def test_example_region_policy():
    values = renderer.parse_env_file(EXAMPLE)
    assert values["CLAIMDESK_REGION"] == "us-central1"
    assert values["CLAIMDESK_BQ_LOCATION"] == "us-central1"
    assert values["GOOGLE_CLOUD_LOCATION"] == "global"
    assert values["LIVE_MODEL_LOCATION"] == "global"
    assert values["LIVE_MODEL_FALLBACK_LOCATION"] == "us-central1"


def test_example_keeps_fixed_models():
    values = renderer.parse_env_file(EXAMPLE)
    assert values["CLAIMDESK_LIVE_MODEL"] == "gemini-3.8-live"
    assert values["CLAIMDESK_REASONING_MODEL"] == "gemini-3.8-flash"
    assert values["CLAIMDESK_SKETCH_MODEL"] == "gemini-3.1-flash-image"


def test_runtime_yaml_excludes_deploy_keys_and_applies_overrides():
    values = renderer.parse_env_file(EXAMPLE)
    runtime = renderer.runtime_values(values, {"CLAIMDESK_STORAGE_BACKEND": "gcp"})
    assert not any(key.startswith("DEPLOY_") for key in runtime)
    assert runtime["CLAIMDESK_STORAGE_BACKEND"] == "gcp"
    yaml_text = renderer.to_yaml(runtime)
    assert 'CLAIMDESK_BRAND_NAME: "Demo Tideline"' in yaml_text
    assert 'CLAIMDESK_FIRESTORE_DATABASE: "(default)"' in yaml_text


def test_settings_defaults_follow_region(monkeypatch):
    for key in ("CLAIMDESK_REGION", "CLAIMDESK_BQ_LOCATION", "LIVE_MODEL_LOCATION", "LIVE_MODEL_FALLBACK_LOCATION", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(settings_module, "load_dotenv_if_present", lambda *a, **k: None)
    settings_module.get_settings.cache_clear()
    try:
        s = settings_module.get_settings()
        assert s.region == "us-central1"
        assert s.bq_location == "us-central1"
        assert s.model_location == "global"
        assert s.live_locations == ("global", "us-central1")
    finally:
        settings_module.get_settings.cache_clear()


@pytest.mark.parametrize("raw,expected", [("CO, tx ,FL", ("CO", "TX", "FL")), ("", ("CO", "TX", "FL", "LA", "NC"))])
def test_supported_states_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("CLAIMDESK_SUPPORTED_STATES", raw)
    monkeypatch.setattr(settings_module, "load_dotenv_if_present", lambda *a, **k: None)
    settings_module.get_settings.cache_clear()
    try:
        assert settings_module.get_settings().supported_states == expected
    finally:
        settings_module.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Deploy scripts (text-level checks; nothing is executed against GCP)
# ---------------------------------------------------------------------------
DEPLOY = ROOT / "deploy"


def _script(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def _code_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_image_tag_is_not_frozen_when_variables_are_sourced():
    code = _code_lines(_script("00_variables.sh"))
    assert "export IMAGE=" not in code and "IMAGE_TAG" not in code
    assert "export IMAGE_REPO=" in code
    build = _code_lines(_script("04_build_image.sh"))
    assert "date -u" in build and "${IMAGE_REPO}:${TAG}" in build
    assert 'echo "export IMAGE=${BUILD_IMAGE}"' in build
    assert '"${IMAGE:?' in _script("05_deploy_cloud_run.sh")


def test_deploy_sets_documented_iap_audience_and_cleans_up():
    text = _script("05_deploy_cloud_run.sh")
    assert 'IAP_AUDIENCE="/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${SERVICE_NAME}"' in text
    assert '--set CLAIMDESK_IAP_AUDIENCE="${IAP_AUDIENCE}"' in text
    assert "trap 'rm -f \"${ENV_YAML}\"' EXIT" in text
    assert "--no-allow-unauthenticated" in text and "--iap" in text
    # The override really lands in the YAML even though .env leaves it empty.
    values = renderer.parse_env_file(EXAMPLE)
    runtime = renderer.runtime_values(values, {"CLAIMDESK_IAP_AUDIENCE": "/projects/1/locations/us-central1/services/demo-tideline"})
    assert runtime["CLAIMDESK_IAP_AUDIENCE"] == "/projects/1/locations/us-central1/services/demo-tideline"


def test_search_ids_match_env_example():
    values = renderer.parse_env_file(EXAMPLE)
    for name in ("00_variables.sh", "03b_vertex_ai_search.sh", "99_destroy.sh"):
        text = _script(name)
        assert values["DEPLOY_SEARCH_DATA_STORE_ID"] in text, name
        assert values["CLAIMDESK_SEARCH_ENGINE_ID"] in text, name
    assert values["CLAIMDESK_SEARCH_LOCATION"] == "us"


def _op_status_program() -> str:
    text = _script("03b_vertex_ai_search.sh")
    start = text.index("read -r -d '' OP_STATUS_PY <<'PY' || true\n") + len("read -r -d '' OP_STATUS_PY <<'PY' || true\n")
    return text[start : text.index("\nPY\n", start)]


@pytest.mark.parametrize(
    "operation,first_line,detail",
    [
        ({"name": "op", "done": False}, "RUNNING", None),
        ({"name": "op", "done": True, "response": {}}, "OK", None),
        ({"name": "op", "done": True, "error": {"code": 7, "message": "PERMISSION_DENIED"}}, "FAILED", "PERMISSION_DENIED"),
        (
            {"done": True, "metadata": {"successCount": "0", "failureCount": "2", "totalCount": "2"},
             "response": {"errorSamples": [{"code": 3, "message": "Unsupported file"}]}},
            "FAILED",
            "Unsupported file",
        ),
        ({"done": True, "metadata": {"successCount": "3", "failureCount": "1"}}, "PARTIAL", "successCount=3"),
        ({"done": True, "metadata": {"successCount": "4", "totalCount": "4"}}, "OK", "failureCount=0"),
    ],
)
def test_03b_operation_status_parser(operation, first_line, detail):
    import json
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", _op_status_program()], input=json.dumps(operation), capture_output=True, text=True, check=True
    )
    lines = result.stdout.splitlines()
    assert lines[0] == first_line
    if detail:
        assert detail in result.stdout


def test_03b_polls_operations_and_fails_without_documents():
    code = _code_lines(_script("03b_vertex_ai_search.sh"))
    assert "sleep 20" not in code
    assert code.count("wait_for_operation \"$(") == 3  # data store, import, engine
    assert "https://${HOST}/v1/${op_name}" in code
    assert "grep -c" not in code  # documents are counted by parsing JSON
    assert "ERROR: no documents in data store" in code
