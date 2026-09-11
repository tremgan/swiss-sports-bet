FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app/dashboard

COPY src/core /app/core
COPY src/dashboard/pyproject.toml .
COPY src/dashboard/uv.lock .
RUN uv sync --no-editable --frozen --no-dev

COPY src/dashboard .

EXPOSE 8501

CMD ["uv", "run", "streamlit", "run", "main.py", "--server.address=0.0.0.0"]