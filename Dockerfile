# =============================================================================
# ClaimDesk container image (runs on Cloud Run)
# =============================================================================
# WHAT IS THIS?
#   A Dockerfile is a recipe for a container image. Cloud Build follows this
#   recipe (deploy/04_build_image.sh), pushes the image to Artifact Registry,
#   and Cloud Run starts containers from it.
#
# WHY TWO STAGES?
#   Stage 1 ("builder") has the tooling to install dependencies (uv).
#   Stage 2 ("runtime") copies only the finished virtual environment + code.
#   The final image is smaller, starts faster (cold starts!) and has less
#   attack surface.
# =============================================================================

# ---------- Stage 1: install dependencies with uv ----------------------------
FROM python:3.12-slim AS builder

# uv is a very fast Python package manager (replaces pip + venv).
# Pinned to a fixed version (the one used to develop this project) so a new
# uv release can never change how the image is built. Bump it on purpose.
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_DEFAULT_INDEX=https://pypi.org/simple

# Copy only the dependency manifest first. Docker caches this layer, so
# changing application code does NOT re-download every dependency.
# Only the runtime dependencies are installed: the optional extras `dev`
# (tests, evals) and `data` (data load) are skipped unless asked for with
# --extra, so no flag is needed to leave them out.
COPY pyproject.toml README.md ./
RUN uv sync --no-install-project

# Now copy the source and install the project itself.
COPY claimdesk ./claimdesk
COPY webapp ./webapp
# The nfip-flood-intake Agent Skill (domain knowledge + few-shot examples),
# read at start-up by claimdesk/knowledge.py.
COPY skills ./skills
RUN uv sync

# ---------- Stage 2: minimal runtime image -----------------------------------
FROM python:3.12-slim AS runtime

# Run as a non-root user: if the app were ever compromised, the attacker
# would not be root inside the container.
RUN useradd --create-home --uid 10001 claimdesk
WORKDIR /app

COPY --from=builder --chown=claimdesk:claimdesk /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

USER claimdesk
EXPOSE 8080

# Cloud Run injects $PORT (8080 by default). One uvicorn worker per container:
# live voice sessions keep in-process state (the open Gemini Live socket), and
# Cloud Run scales by adding *containers*, not workers.
# --proxy-headers: trust X-Forwarded-* from Google's front end so URLs/scheme
# are correct behind IAP.  --timeout-keep-alive: keep WebSockets healthy.
CMD ["sh", "-c", "exec uvicorn webapp.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 75"]
