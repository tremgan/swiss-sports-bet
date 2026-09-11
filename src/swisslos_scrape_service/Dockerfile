FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app/swisslos_scrape_service

COPY src/core /app/core
COPY src/swisslos_scrape_service/pyproject.toml .
COPY src/swisslos_scrape_service/uv.lock .
RUN uv sync --no-editable --frozen --no-dev

RUN uv run playwright install chromium --with-deps

COPY src/swisslos_scrape_service .

CMD ["uv", "run", "main.py"]