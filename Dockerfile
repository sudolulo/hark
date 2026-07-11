# hark: pipeline + web frontend in one image.
#
# Default command serves the dashboard (login-walled) + feed/audio routes
# (token-gated) over the databases in /app/data; every pipeline stage is
# available as a one-shot command, e.g.:
#   docker compose run --rm hark ingest
#   docker compose run --rm hark canon
#   docker compose run --rm hark chapters
#   docker compose run --rm hark transcribe
#   docker compose run --rm hark detect-ads
#   docker compose run --rm hark cut
#
# KNOWN GAP, not solved here: hark depends on adscrub as a local path
# dependency (../adscrub, editable — see pyproject.toml [tool.uv.sources]).
# This build context only COPYs hark's own files, so `uv sync` below will
# fail to resolve that dependency as written. Real fix is a packaging
# decision (git dependency + deploy key, vendoring a built adscrub wheel into
# this context, or a small multi-repo build script) — see docs/PLAN.md open
# questions. Don't paper over this by quietly dropping the dependency.
#
# Build with --build-arg GPU=1 (or `docker compose -f compose.yaml -f compose.gpu.yaml
# build`) to pull in the cuBLAS/cuDNN extra for faster-whisper's CUDA path — only
# needed on a host that actually passes a GPU through (see CLAUDE.md).

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
ARG GPU=0

# dependency layer first: rebuilds only when the lockfile changes
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$GPU" = "1" ]; then uv sync --frozen --no-dev --no-install-project --extra gpu; \
    else uv sync --frozen --no-dev --no-install-project; fi

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$GPU" = "1" ]; then uv sync --frozen --no-dev --extra gpu; \
    else uv sync --frozen --no-dev; fi

ENV PATH="/app/.venv/bin:$PATH" \
    HARK_DB=/app/data/hark.db \
    HARK_AUTH_DB=/app/data/auth.db \
    HARK_DATA_DIR=/app/data

# gosu drops from root to the unprivileged `hark` user after the entrypoint
# fixes ownership of /app/data — Docker creates bind mounts and anonymous
# volumes as root, which this user can't write to on its own. uid/gid 568
# matches TrueNAS SCALE's standard "apps" account, so files land owned by
# the same user/group as every other app on that host; harmless elsewhere.
# ffmpeg is for adscrub's cut.py, called as a library (see cli.py).
RUN apt-get update && apt-get install -y --no-install-recommends gosu ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 568 hark \
    && useradd --system --uid 568 --gid 568 --no-create-home hark

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME ["/app/data"]
EXPOSE 8710

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["hark", "web", "--bind", "0.0.0.0:8710"]
