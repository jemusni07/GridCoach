# GridCoach backend — FastAPI + uv. Generic image for any container host; SQLite path via DATABASE_PATH.
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

# Mount a persistent volume at /data and set DATABASE_PATH=/data/gridcoach.db to keep state across deploys.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
