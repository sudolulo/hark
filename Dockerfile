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
# hark depends on adscrub as a local path dependency (../adscrub, editable —
# see pyproject.toml [tool.uv.sources] and uv.lock's `source = { editable =
# "../adscrub" }`). That path is resolved relative to /app (this image's
# WORKDIR, where hark's own pyproject.toml/uv.lock land), so adscrub's source
# needs to exist at /adscrub in this image. The build context is NOT this
# repo alone — it's a staging directory containing both `hark/` and
# `adscrub/` (git-archive-clean, no .venv/data/.git), assembled by
# scripts/build-image.sh. Building this Dockerfile directly against just this
# repo's own directory will fail to resolve the dependency; use that script
# instead of `docker build .` here.
#
# Build with --build-arg GPU=1 (or `docker compose -f compose.yaml -f compose.gpu.yaml
# build`) to pull in the cuBLAS/cuDNN extra for faster-whisper's CUDA path — only
# needed on a host that actually passes a GPU through (see CLAUDE.md).

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
ARG GPU=0

# adscrub's source first — the editable path dependency needs it present
# before the very first `uv sync` (which installs dependencies, including
# this one, even with --no-install-project), and changes to adscrub's source
# should invalidate this layer same as changes to hark's own lockfile do.
COPY adscrub /adscrub
COPY hark/pyproject.toml hark/uv.lock hark/README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$GPU" = "1" ]; then uv sync --frozen --no-dev --no-install-project --extra gpu; \
    else uv sync --frozen --no-dev --no-install-project; fi

COPY hark/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$GPU" = "1" ]; then uv sync --frozen --no-dev --extra gpu; \
    else uv sync --frozen --no-dev; fi

ENV PATH="/app/.venv/bin:$PATH" \
    HARK_DB=/app/data/hark.db \
    HARK_AUTH_DB=/app/data/auth.db \
    HARK_DATA_DIR=/app/data \
    HF_HOME=/app/data/.hf-cache \
    LD_LIBRARY_PATH="/app/.venv/lib/python3.13/site-packages/nvidia/cublas/lib:/app/.venv/lib/python3.13/site-packages/nvidia/cudnn/lib"
# adscrub's `gpu` extra installs nvidia-cublas-cu12/nvidia-cudnn-cu12 as pip
# wheels — they bundle their .so files under site-packages, not any path the
# dynamic linker searches by default, so ctranslate2 fails at inference time
# with "Library libcublas.so.12 is not found" even though the packages are
# installed. Harmless to set unconditionally on non-GPU builds: the linker
# just skips a LD_LIBRARY_PATH entry that doesn't exist.
#
# LD_LIBRARY_PATH alone proved insufficient in production: the NVIDIA
# container runtime's own environment injection (triggered by `runtime:
# nvidia` / device reservations) can clobber it before the app process ever
# sees it. Registering the same paths in the system linker cache survives
# that, since dlopen() consults /etc/ld.so.cache independent of any env var.
RUN if [ "$GPU" = "1" ]; then \
      echo "/app/.venv/lib/python3.13/site-packages/nvidia/cublas/lib" > /etc/ld.so.conf.d/nvidia-cublas.conf && \
      echo "/app/.venv/lib/python3.13/site-packages/nvidia/cudnn/lib" > /etc/ld.so.conf.d/nvidia-cudnn.conf && \
      ldconfig; \
    fi
# `hark` is --no-create-home (see below), so huggingface_hub's default cache
# location (~/.cache/huggingface) resolves to an unwritable /home/hark. Every
# Whisper model load then fails to persist its revision-check bookkeeping and
# re-hits the HF Hub API from scratch — which is what actually exhausted the
# anonymous rate limit in production, not a missing GPU. Redirecting into
# /app/data both fixes the write and makes the download persist across
# container restarts instead of re-fetching every time.

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

COPY hark/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME ["/app/data"]
EXPOSE 8710

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["hark", "web", "--bind", "0.0.0.0:8710"]
