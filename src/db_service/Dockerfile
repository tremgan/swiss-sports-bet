FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app/db_service

COPY src/core /app/core
COPY src/db_service/pyproject.toml .
COPY src/db_service/uv.lock .
RUN uv sync --no-editable --frozen --no-dev

COPY src/db_service .

EXPOSE 8000

# Migrations run at start so a deploy can never outrun its own schema.
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn main:app --host 0.0.0.0 --port 8000"]
