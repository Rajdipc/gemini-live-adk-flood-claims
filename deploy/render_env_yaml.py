#!/usr/bin/env python3
"""Turn the RUNTIME section of ``.env`` into a Cloud Run ``--env-vars-file``.

WHY?
    ``gcloud run deploy --set-env-vars A=1,B=2`` breaks on values that contain
    commas, spaces or parentheses (``CO,TX,FL``, ``Demo Tideline``,
    ``(default)``). ``--env-vars-file`` takes a YAML map instead and has no
    such problems. Generating it from ``.env`` means Cloud Run gets exactly the
    values you tested locally - one source of truth.

WHAT IS INCLUDED
    Keys starting with ``GOOGLE_``, ``LIVE_MODEL_`` or ``CLAIMDESK_``.
    ``DEPLOY_*`` keys (deploy-only) are skipped. A few values are forced for
    Cloud Run (``--set KEY=VALUE``), e.g. the storage backend must be ``gcp``.

USAGE (called by deploy/05_deploy_cloud_run.sh)
    python3 deploy/render_env_yaml.py --out /tmp/claimdesk-env.yaml \
        --set CLAIMDESK_STORAGE_BACKEND=gcp --set CLAIMDESK_ENABLE_CLOUD_TRACE=true

Standard library only, so it runs with any python3.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RUNTIME_PREFIXES = ("GOOGLE_", "LIVE_MODEL_", "CLAIMDESK_")
# Cloud Run sets these itself or they must never be copied from a laptop.
NEVER_SEND = {"GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GENAI_USE_ENTERPRISE", "GOOGLE_GENAI_USE_VERTEXAI"}


def parse_env_file(path: Path) -> dict[str, str]:
    """Same rules as claimdesk.settings.load_dotenv_if_present."""

    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def runtime_values(values: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
    selected = {k: v for k, v in values.items() if k.startswith(RUNTIME_PREFIXES) and k not in NEVER_SEND and v != ""}
    selected.update(overrides)
    return dict(sorted(selected.items()))


def to_yaml(values: dict[str, str]) -> str:
    # JSON strings are valid YAML scalars -> safe quoting for any character.
    return "".join(f"{key}: {json.dumps(value)}\n" for key, value in values.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=str(Path(__file__).resolve().parent.parent / ".env"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="force a value (repeatable)")
    args = parser.parse_args(argv)

    env_path = Path(args.env_file)
    if not env_path.is_file():
        print(f"{env_path} not found - run: cp .env.example .env", file=sys.stderr)
        return 2
    overrides = dict(item.split("=", 1) for item in args.set)
    values = runtime_values(parse_env_file(env_path), overrides)
    Path(args.out).write_text(to_yaml(values), encoding="utf-8")
    print(f"Wrote {len(values)} runtime variables to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
