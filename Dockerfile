# hark: pipeline + web frontend in one image.
#
# Default command serves the login-walled web UI over the databases in
# /app/data; every pipeline stage is available as a one-shot command, e.g.:
#   docker compose run --rm hark ingest
#   docker compose run --rm hark canon

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

# dependency layer first: rebuilds only when the lockfile changes
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    HARK_DB=/app/data/hark.db \
    HARK_AUTH_DB=/app/data/auth.db

# gosu drops from root to the unprivileged `hark` user after the entrypoint
# fixes ownership of /app/data — Docker creates bind mounts and anonymous
# volumes as root, which uid 8710 can't write to on its own.
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 8710 --no-create-home hark

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME ["/app/data"]
EXPOSE 8710

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["hark", "web", "--bind", "0.0.0.0:8710"]
