# GridCoach backend — FastAPI + uv. Built remotely by `fly deploy`; no local Docker needed.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Dependencies first so they cache independently of app code.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
COPY scripts ./scripts

# SQLite lives on the mounted volume (see fly.toml); override locally with DATABASE_PATH.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
