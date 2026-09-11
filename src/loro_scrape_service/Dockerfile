FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app/loro_scrape_service

COPY src/core /app/core
COPY src/loro_scrape_service/pyproject.toml .
COPY src/loro_scrape_service/uv.lock .
RUN uv sync --no-editable --frozen --no-dev

COPY src/loro_scrape_service .

CMD ["uv", "run", "main.py"]